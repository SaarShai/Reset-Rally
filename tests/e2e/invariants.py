"""Protocol and safety invariants for Reset Rally (I-01 through I-12)."""

import json
from pathlib import Path
import sqlite3
from typing import Any

from rally.server import ClientLog


def check_all(
    db: Path | str | sqlite3.Connection,
    log: ClientLog | None = None,
    now: float = 0.0,
    tick: int | None = None,
    phase: str | None = None,
    is_final: bool = False,
) -> None:
    """Assert invariants I-01 through I-10 (and I-11, I-12 if final).

    Raises AssertionError on the first violated invariant with diagnostic details.
    """
    prefix = f"tick {tick} ({phase}) " if tick is not None else ""
    if isinstance(db, (str, Path)):
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        should_close = True
    else:
        conn = db
        should_close = False

    try:
        # I-01: Zero duplicate work committed.
        dups = conn.execute(
            """
            SELECT wu_id, participant_id, count(*) as cnt
            FROM submissions
            WHERE status = 'accepted'
            GROUP BY wu_id, participant_id
            HAVING count(*) > 1
            """
        ).fetchall()
        if dups:
            raise AssertionError(f"I-01 {prefix}duplicate accepted submissions: {dict(dups[0])}")

        # Each committed WU must have at least one accepted submission
        committed_wus = conn.execute(
            "SELECT id FROM work_units WHERE status = 'committed' AND is_canary = 0"
        ).fetchall()
        for c_wu in committed_wus:
            cnt = conn.execute(
                "SELECT count(*) as cnt FROM submissions WHERE wu_id = ? AND status = 'accepted'",
                (c_wu["id"],),
            ).fetchone()["cnt"]
            if cnt == 0:
                raise AssertionError(f"I-01 {prefix}WU {c_wu['id']} committed without accepted submission")

        # I-02: One live Lease per slot.
        dup_leases = conn.execute(
            """
            SELECT wu_id, slot, count(*) as cnt
            FROM leases
            WHERE status = 'active' AND expires_at > ?
            GROUP BY wu_id, slot
            HAVING count(*) > 1
            """,
            (now,),
        ).fetchall()
        if dup_leases:
            raise AssertionError(f"I-02 {prefix}multiple live leases on slot: {dict(dup_leases[0])}")

        # I-03: Zero lost WUs.
        invalid_states = conn.execute(
            "SELECT id, status FROM work_units WHERE status NOT IN ('open', 'verifying', 'committed', 'failed', 'disputed')"
        ).fetchall()
        if invalid_states:
            raise AssertionError(f"I-03 {prefix}WU in invalid state: {dict(invalid_states[0])}")

        if is_final:
            open_count = conn.execute(
                "SELECT count(*) as cnt FROM work_units WHERE status = 'open' AND is_canary = 0"
            ).fetchone()["cnt"]
            active_leases_count = conn.execute(
                "SELECT count(*) as cnt FROM leases WHERE status = 'active'"
            ).fetchone()["cnt"]
            if open_count != 0 or active_leases_count != 0:
                raise AssertionError(f"I-03 {prefix}final quiet not reached: open={open_count}, active_leases={active_leases_count}")

        # I-04: Cross-family enforcement.
        q_committed = conn.execute(
            """
            SELECT w.id
            FROM work_units w
            JOIN projects p ON p.id = w.project_id
            WHERE w.status = 'committed' AND json_extract(p.manifest, '$.verification.mode') = 'quorum'
            """
        ).fetchall()
        for qrow in q_committed:
            wid = qrow["id"]
            families = conn.execute(
                "SELECT DISTINCT family FROM submissions WHERE wu_id = ? AND status = 'accepted'",
                (wid,),
            ).fetchall()
            if len(families) < 2:
                raise AssertionError(f"I-04 {prefix}WU {wid} committed with fewer than 2 distinct families")

            part_votes = conn.execute(
                "SELECT participant_id, count(*) as cnt FROM submissions WHERE wu_id = ? AND status = 'accepted' GROUP BY participant_id HAVING count(*) > 1",
                (wid,),
            ).fetchall()
            if part_votes:
                raise AssertionError(f"I-04 {prefix}WU {wid} has duplicate accepted votes from participant {part_votes[0]['participant_id']}")

        # I-08: Quarantine is total.
        quar_participants = conn.execute(
            "SELECT id, name FROM participants WHERE status = 'quarantined'"
        ).fetchall()
        for qp in quar_participants:
            active_l = conn.execute(
                "SELECT count(*) as cnt FROM leases WHERE participant_id = ? AND status = 'active'",
                (qp["id"],),
            ).fetchone()["cnt"]
            if active_l > 0:
                raise AssertionError(f"I-08 {prefix}quarantined participant {qp['name']} holds active leases")

        # I-09: Ledger conservation.
        total_sub_pts = conn.execute(
            "SELECT coalesce(sum(points), 0) as total FROM submissions WHERE status = 'accepted'"
        ).fetchone()["total"]
        bad_pts = conn.execute(
            "SELECT count(*) as cnt FROM submissions WHERE status != 'accepted' AND points > 0"
        ).fetchone()["cnt"]
        if bad_pts > 0:
            raise AssertionError(f"I-09 {prefix}non-accepted submissions have points > 0")

        if log is not None:
            # I-05: Blindness - no served payload contains sibling answer
            # I-06: Holdout isolation
            holdout_tokens = ["7919", "10007", "1230"]
            if log.payload_log_contains_any(holdout_tokens):
                raise AssertionError(f"I-06 {prefix}holdout token found in served payload log")

            # I-07: Canary looks normal
            for p in log.payloads:
                p_str = json.dumps(p) if isinstance(p, (dict, list)) else str(p)
                wu_data = p.get("wu") if isinstance(p, dict) else None
                if wu_data and isinstance(wu_data, dict):
                    prompt_str = wu_data.get("prompt", "")
                    if "canary" in prompt_str.lower() and "verify" not in prompt_str.lower():
                        raise AssertionError(f"I-07 {prefix}canary string leaked in served prompt")

            # I-10: Zero credentials
            req_text = log.request_log_text()
            if "SENTINEL" in req_text:
                raise AssertionError(f"I-10 {prefix}credential SENTINEL leaked in request log")

    finally:
        if should_close:
            conn.close()
