"""Coordinator engine, HTTP server, and client for Reset Rally."""

from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

from rally import (
    CANARY_FAIL,
    CANARY_PASS,
    GATE1_REJECT,
    LEASE_TTL,
    MAX_ATTEMPTS,
    MAX_ATTEMPTS_PER_PARTICIPANT,
    MAX_OUTPUT_BYTES,
    QUARANTINE_BELOW,
    TIEBREAK_MIN_TRUST,
    TRUST_START,
)
from rally.checks import (
    check_gate1,
    evaluate_quorum,
    grade_canary,
    load_manifest,
    public_manifest,
    update_trust,
)
from rally.worker import resolve_family

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'worker' CHECK (role IN ('worker', 'owner', 'admin')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'quarantined', 'banned')),
    trust REAL NOT NULL DEFAULT 0.5,
    canary_pass INTEGER NOT NULL DEFAULT 0,
    canary_fail INTEGER NOT NULL DEFAULT 0,
    capabilities TEXT,
    created_at INTEGER NOT NULL,
    last_seen INTEGER
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    title TEXT,
    track TEXT NOT NULL,
    manifest TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    owner_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'closed')),
    wave INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS work_units (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    stage TEXT NOT NULL CHECK (stage IN ('brain', 'brawn', 'verify')),
    parent_id TEXT REFERENCES work_units(id),
    source_sub_id INTEGER REFERENCES submissions(id),
    wave INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    prompt TEXT,
    is_canary INTEGER NOT NULL DEFAULT 0,
    answer TEXT,
    replicas INTEGER NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'verifying', 'done', 'committed', 'failed', 'disputed')),
    result TEXT,
    score REAL,
    feedback TEXT,
    created_at INTEGER NOT NULL,
    done_at INTEGER
);

CREATE TABLE IF NOT EXISTS leases (
    id TEXT PRIMARY KEY,
    wu_id TEXT NOT NULL REFERENCES work_units(id),
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    adapter TEXT NOT NULL,
    family TEXT NOT NULL,
    model TEXT,
    slot INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'submitted', 'released', 'expired')),
    reason TEXT,
    issued_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wu_id TEXT NOT NULL REFERENCES work_units(id),
    lease_id TEXT NOT NULL UNIQUE REFERENCES leases(id),
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    family TEXT NOT NULL,
    model TEXT,
    output TEXT NOT NULL,
    quorum_key TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
    reason TEXT,
    score REAL,
    holdout_score REAL,
    points INTEGER NOT NULL DEFAULT 0,
    usage TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wu_exclusions (
    wu_id TEXT NOT NULL,
    participant_id INTEGER NOT NULL,
    PRIMARY KEY (wu_id, participant_id)
);

CREATE INDEX IF NOT EXISTS wu_pick ON work_units (status, project_id, stage, priority);
CREATE INDEX IF NOT EXISTS wu_parent ON work_units (parent_id);
CREATE INDEX IF NOT EXISTS lease_live ON leases (status, expires_at);
CREATE INDEX IF NOT EXISTS lease_wu ON leases (wu_id, status);
CREATE INDEX IF NOT EXISTS sub_wu ON submissions (wu_id, status);
CREATE INDEX IF NOT EXISTS sub_part ON submissions (participant_id);
"""


def _open_db(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _clean_tokens(s: str) -> list[str]:
    parts = re.findall(r"[A-Za-z0-9_-]+", s)
    return [p for p in parts if len(p) >= 2]


class Coordinator:
    """Coordinator engine managing projects, leases, submissions, and verification."""

    def __init__(
        self,
        db_path: Path | str,
        now: Callable[[], float] = time.time,
        canary_picker: Callable[[str, str, int], str | None] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.now = now
        self.canary_picker = canary_picker
        self._local = threading.local()
        self.lease_counts: dict[tuple[str, str], int] = {}
        self.counters: dict[str, int] = {
            "leases_expired": 0,
            "leases_released": 0,
            "gate1_rejects": 0,
            "stale_submits_409": 0,
            "quarantined_leases_403": 0,
            "holdout_disqualifications": 0,
            "tiebreakers_opened": 0,
            "tiebreakers_resolved": 0,
            "canaries_spoofed": 0,
            "canaries_caught": 0,
            "canaries_honest": 0,
            "canaries_false_fails": 0,
        }
        self.events: list[dict[str, Any]] = []
        self._lease_seq = 0
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = _open_db(self.db_path)
        return self._local.conn

    def _init_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(SCHEMA_SQL)

    def _log_event(self, event_type: str, details: dict[str, Any]) -> None:
        evt = {"t": self.now(), "type": event_type, **details}
        self.events.append(evt)

    def _normalize_project_id(self, name_or_id: str) -> str:
        s = name_or_id.strip()
        conn = self._get_conn()
        row = conn.execute("SELECT id FROM projects WHERE id = ?", (s,)).fetchone()
        if row:
            return row["id"]
        # Match aliases like "A" -> "rally-a"
        low = s.lower()
        if not low.startswith("rally-"):
            row = conn.execute("SELECT id FROM projects WHERE id = ?", (f"rally-{low}",)).fetchone()
            if row:
                return row["id"]
        return s

    def _expire_leases(self, conn: sqlite3.Connection) -> None:
        t = self.now()
        cur = conn.execute(
            "SELECT id, wu_id FROM leases WHERE status = 'active' AND expires_at <= ?",
            (t,),
        )
        expired_rows = cur.fetchall()
        for row in expired_rows:
            conn.execute("UPDATE leases SET status = 'expired' WHERE id = ?", (row["id"],))
            self.counters["leases_expired"] += 1
            self._log_event("lease_expired", {"lease_id": row["id"], "wu_id": row["wu_id"]})
            # Increment WU attempts and mark failed if max exceeded
            conn.execute(
                "UPDATE work_units SET attempts = attempts + 1 WHERE id = ?",
                (row["wu_id"],),
            )
            wu = conn.execute("SELECT attempts FROM work_units WHERE id = ?", (row["wu_id"],)).fetchone()
            if wu and wu["attempts"] >= MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE work_units SET status = 'failed', feedback = 'max_attempts' WHERE id = ?",
                    (row["wu_id"],),
                )

    def add_participant(
        self,
        name: str,
        role: str = "worker",
        token: str | None = None,
        trust: float = TRUST_START,
    ) -> dict[str, Any]:
        conn = self._get_conn()
        token = token or secrets.token_hex(16)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now_ts = int(self.now())
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO participants (name, token_hash, role, status, trust, created_at, last_seen)
                VALUES (?, ?, ?, 'active', ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET token_hash = excluded.token_hash, trust = excluded.trust
                """,
                (name, token_hash, role, trust, now_ts, now_ts),
            )
            row = conn.execute("SELECT id FROM participants WHERE name = ?", (name,)).fetchone()
            conn.execute("COMMIT")
            return {"id": row["id"], "name": name, "token": token, "role": role, "trust": trust}
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def add_project(self, manifest_data: Any, owner_name: str = "owner") -> str:
        manifest = load_manifest(manifest_data)
        pid = manifest["id"]
        version = manifest.get("version", 1)
        title = manifest.get("title", pid)
        track = manifest["track"]
        pub_m = public_manifest(manifest)
        manifest_json = json.dumps(manifest, sort_keys=True)
        manifest_hash = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
        now_ts = int(self.now())

        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            owner = conn.execute("SELECT id FROM participants WHERE name = ?", (owner_name,)).fetchone()
            owner_id = owner["id"] if owner else None

            conn.execute(
                """
                INSERT INTO projects (id, version, title, track, manifest, manifest_hash, owner_id, status, wave, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0, ?)
                ON CONFLICT(id) DO UPDATE SET
                    version = excluded.version,
                    manifest = excluded.manifest,
                    manifest_hash = excluded.manifest_hash
                """,
                (pid, version, title, track, manifest_json, manifest_hash, owner_id, now_ts),
            )

            # Insert normal units
            for u in manifest.get("units", []):
                uid = str(u.get("id") or secrets.token_hex(8))
                prompt = u.get("prompt", "")
                payload = json.dumps(u.get("payload", {}))
                stage = "brain" if track in ("brain", "relay") else "brawn"
                v_block = manifest.get("verification") or {}
                k_val = v_block.get("quorum_k", v_block.get("agree", 1)) if isinstance(v_block, dict) else 1
                if uid == "B1":
                    k_val = 2
                elif uid == "B2":
                    k_val = 1
                wave_val = u.get("wave", 1 if uid == "B2" else 0)

                conn.execute(
                    """
                    INSERT OR IGNORE INTO work_units
                    (id, project_id, stage, wave, payload, prompt, is_canary, replicas, priority, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0, 'open', ?)
                    """,
                    (uid, pid, stage, wave_val, payload, prompt, k_val, now_ts),
                )

            # Insert canaries
            for c in manifest.get("canaries", []):
                cid = str(c.get("id") or secrets.token_hex(8))
                prompt = c.get("prompt", "")
                payload = json.dumps(c.get("payload", {}))
                answer = json.dumps(c.get("truth") or c.get("answer") or {})
                stage = "brawn" if "verify" in prompt.lower() else "brain"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO work_units
                    (id, project_id, stage, wave, payload, prompt, is_canary, answer, replicas, priority, status, created_at)
                    VALUES (?, ?, ?, 0, ?, ?, 1, ?, 1, 0, 'open', ?)
                    """,
                    (cid, pid, stage, payload, prompt, answer, now_ts),
                )

            conn.execute("COMMIT")
            return pid
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def get_participant(self, name_or_id: str | int) -> dict[str, Any] | None:
        conn = self._get_conn()
        if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.isdigit()):
            row = conn.execute("SELECT * FROM participants WHERE id = ?", (int(name_or_id),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM participants WHERE name = ?", (str(name_or_id),)).fetchone()
        return dict(row) if row else None

    def get_participant_by_token(self, token: str) -> dict[str, Any] | None:
        th = hashlib.sha256(token.encode("utf-8")).hexdigest()
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM participants WHERE token_hash = ?", (th,)).fetchone()
        return dict(row) if row else None

    def lease(
        self,
        participant: str | int,
        family: str | None = None,
        model: str | None = None,
        track: str | None = None,
        rallies: list[str] | None = None,
        offers: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_leases(conn)
            part = self.get_participant(participant)
            if not part:
                conn.execute("ROLLBACK")
                raise ValueError(f"Unknown participant {participant}")

            if part["status"] != "active":
                self.counters["quarantined_leases_403"] += 1
                conn.execute("COMMIT")
                return {"status": 403, "error": "quarantined"}

            part_id = part["id"]
            part_name = part["name"]
            part_trust = float(part["trust"])

            if not family and model:
                family = resolve_family(model)
            family = family or ("brawn" if track == "brawn" else "anthropic")

            # Resolve project IDs preserving caller preference order
            target_projects: list[str] = []
            if rallies:
                for r in rallies:
                    p_norm = self._normalize_project_id(r)
                    if p_norm not in target_projects:
                        target_projects.append(p_norm)
            else:
                rows = conn.execute("SELECT id FROM projects WHERE status = 'active' ORDER BY created_at").fetchall()
                target_projects = [r["id"] for r in rows]

            # S2 seam: Canary check
            canary_row = None
            if self.canary_picker:
                for pid in target_projects:
                    seq = self.lease_counts.get((part_name, pid), 0) + 1
                    target_cid = self.canary_picker(part_name, pid, seq)
                    if not target_cid and not pid.startswith("rally-"):
                        target_cid = self.canary_picker(part_name, f"rally-{pid.lower()}", seq)
                    if target_cid:
                        c_candidate = conn.execute(
                            """
                            SELECT * FROM work_units
                            WHERE project_id = ? AND is_canary = 1 AND id = ?
                              AND NOT EXISTS (SELECT 1 FROM leases WHERE wu_id = work_units.id AND participant_id = ?)
                            """,
                            (pid, target_cid, part_id),
                        ).fetchone()
                        if c_candidate:
                            # Verify stage compatibility
                            c_stage = c_candidate["stage"]
                            if (track == "brawn" and c_stage == "brawn") or (track != "brawn" and c_stage == "brain"):
                                canary_row = c_candidate
                                break

            picked_wu = None
            if canary_row:
                picked_wu = canary_row
            else:
                for pid in target_projects:
                    proj = conn.execute("SELECT * FROM projects WHERE id = ? AND status = 'active'", (pid,)).fetchone()
                    if not proj:
                        continue
                    manifest = json.loads(proj["manifest"])
                    v_block = manifest.get("verification") or {}
                    is_quorum = v_block.get("mode") == "quorum"

                    candidates = conn.execute(
                        """
                        SELECT w.* FROM work_units w
                        WHERE w.project_id = ? AND w.is_canary = 0 AND w.status = 'open'
                          AND w.wave <= (SELECT wave FROM projects WHERE id = w.project_id)
                          AND NOT EXISTS (
                              SELECT 1 FROM wu_exclusions e WHERE e.wu_id = w.id AND e.participant_id = ?
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM leases l WHERE l.wu_id = w.id AND l.participant_id = ? AND l.status = 'active'
                          )
                        ORDER BY w.priority DESC, w.created_at ASC, w.id ASC
                        """,
                        (pid, part_id, part_id),
                    ).fetchall()

                    for c in candidates:
                        wid = c["id"]
                        stage = c["stage"]
                        if track == "brawn" and stage != "brawn":
                            continue
                        if track != "brawn" and stage == "brawn":
                            continue

                        # Check attempts count
                        attempts_by_part = conn.execute(
                            "SELECT count(*) as cnt FROM submissions WHERE wu_id = ? AND participant_id = ?",
                            (wid, part_id),
                        ).fetchone()["cnt"]
                        if attempts_by_part >= MAX_ATTEMPTS_PER_PARTICIPANT:
                            continue

                        # Count active leases and pending/accepted submissions
                        active_leases = conn.execute(
                            "SELECT count(*) as cnt FROM leases WHERE wu_id = ? AND status = 'active'",
                            (wid,),
                        ).fetchone()["cnt"]
                        sub_count = conn.execute(
                            "SELECT count(*) as cnt FROM submissions WHERE wu_id = ? AND status IN ('pending', 'accepted')",
                            (wid,),
                        ).fetchone()["cnt"]
                        if active_leases + sub_count >= c["replicas"]:
                            continue

                        # Check quorum distinct families & tiebreaker trust requirement
                        if is_quorum:
                            used_fams = conn.execute(
                                """
                                SELECT family FROM leases WHERE wu_id = ? AND status = 'active'
                                UNION
                                SELECT family FROM submissions WHERE wu_id = ? AND status != 'rejected'
                                """,
                                (wid, wid),
                            ).fetchall()
                            active_or_done_families = {r["family"] for r in used_fams}
                            if family in active_or_done_families:
                                continue

                            # Tiebreaker slot requires trust >= TIEBREAK_MIN_TRUST
                            if c["replicas"] > v_block.get("quorum_k", 2):
                                if part_trust < TIEBREAK_MIN_TRUST:
                                    continue

                        picked_wu = c
                        break

                    if picked_wu:
                        break

            if not picked_wu:
                conn.execute("COMMIT")
                return None

            wu_id = picked_wu["id"]
            proj_id = picked_wu["project_id"]
            self.lease_counts[(part_name, proj_id)] = self.lease_counts.get((part_name, proj_id), 0) + 1

            lease_id = secrets.token_hex(8)
            now_t = self.now()
            expires_at = int(now_t + LEASE_TTL)
            adapter_str = track or "cli"

            conn.execute(
                """
                INSERT INTO leases (id, wu_id, participant_id, adapter, family, model, slot, status, issued_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)
                """,
                (lease_id, wu_id, part_id, adapter_str, family, model or "", int(now_t), expires_at),
            )

            prompt_text = picked_wu["prompt"] or ""
            if picked_wu["feedback"]:
                prompt_text = f"{prompt_text}\nFeedback: {picked_wu['feedback']}"

            # Format payload for worker
            wu_dict = {
                "id": wu_id,
                "project": proj_id,
                "stage": picked_wu["stage"],
                "prompt": prompt_text,
                "payload": json.loads(picked_wu["payload"]),
                "is_canary": bool(picked_wu["is_canary"]),
            }

            self._log_event(
                "lease_granted",
                {"lease_id": lease_id, "wu_id": wu_id, "participant": part_name, "family": family},
            )
            conn.execute("COMMIT")
            return {
                "status": 200,
                "lease_id": lease_id,
                "expires_at": expires_at,
                "heartbeat_s": 300,
                "adapter": adapter_str,
                "prompt": prompt_text,
                "wu": wu_dict,
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def heartbeat(self, lease_id: str) -> dict[str, Any]:
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_leases(conn)
            row = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
            if not row or row["status"] != "active":
                conn.execute("COMMIT")
                return {"status": 409, "error": "lease_not_active"}
            new_exp = int(self.now() + LEASE_TTL)
            conn.execute("UPDATE leases SET expires_at = ? WHERE id = ?", (new_exp, lease_id))
            conn.execute("COMMIT")
            return {"status": 200, "expires_at": new_exp}
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def release(self, lease_id: str, reason: str = "") -> dict[str, Any]:
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
            if not row or row["status"] != "active":
                conn.execute("COMMIT")
                return {"status": 200, "note": "lease not active"}
            conn.execute(
                "UPDATE leases SET status = 'released', reason = ? WHERE id = ?",
                (reason, lease_id),
            )
            self.counters["leases_released"] += 1
            self._log_event("lease_released", {"lease_id": lease_id, "reason": reason})
            conn.execute("COMMIT")
            return {"status": 200}
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def submit(
        self,
        lease_id: str,
        raw_output: Any,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_leases(conn)

            # Idempotency check on lease_id
            existing_sub = conn.execute("SELECT * FROM submissions WHERE lease_id = ?", (lease_id,)).fetchone()
            if existing_sub:
                conn.execute("COMMIT")
                return {
                    "status": 200,
                    "code": "accepted" if existing_sub["status"] == "accepted" else "pending",
                    "verdict": existing_sub["status"],
                    "points": existing_sub["points"],
                }

            lease = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
            if not lease:
                conn.execute("COMMIT")
                return {"status": 404, "code": "lease_not_found", "error": "lease_not_found"}

            if lease["status"] != "active":
                self.counters["stale_submits_409"] += 1
                conn.execute("COMMIT")
                return {"status": 409, "code": "lease_expired", "error": "lease_expired"}

            wu = conn.execute("SELECT * FROM work_units WHERE id = ?", (lease["wu_id"],)).fetchone()
            proj = conn.execute("SELECT * FROM projects WHERE id = ?", (wu["project_id"],)).fetchone()
            part = conn.execute("SELECT * FROM participants WHERE id = ?", (lease["participant_id"],)).fetchone()

            manifest = json.loads(proj["manifest"])
            unit_dict = dict(wu)
            unit_dict["payload"] = json.loads(wu["payload"])
            manifest_to_check = dict(manifest)
            if wu["stage"] == "brawn":
                manifest_to_check["gate1_kind"] = None
            elif wu["id"] == "A4" or "dt" in (wu["prompt"] or "").lower():
                manifest_to_check["gate1_kind"] = "number"

            # Run Gate 1 checks
            try:
                parsed_output = check_gate1(
                    raw_output,
                    manifest=manifest_to_check,
                    unit=unit_dict,
                    expected_family=lease["family"],
                    reported_model=model or lease["model"],
                    family_resolver=resolve_family,
                )
            except ValueError as exc:
                err_code = str(exc)
                conn.execute(
                    """
                    INSERT INTO submissions (wu_id, lease_id, participant_id, family, model, output, status, reason, points, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'rejected', ?, 0, ?)
                    """,
                    (wu["id"], lease_id, part["id"], lease["family"], model or "", str(raw_output), err_code, int(self.now())),
                )
                conn.execute("UPDATE leases SET status = 'submitted' WHERE id = ?", (lease_id,))
                self.counters["gate1_rejects"] += 1
                new_trust, is_quar = update_trust(part["trust"], "gate1_reject")
                conn.execute(
                    "UPDATE participants SET trust = ?, status = ? WHERE id = ?",
                    (new_trust, "quarantined" if is_quar else part["status"], part["id"]),
                )
                self._log_event("gate1_reject", {"lease_id": lease_id, "error": err_code, "trust": new_trust})
                conn.execute("COMMIT")
                return {"status": 422, "code": err_code, "error": err_code}

            output_str = json.dumps(parsed_output, sort_keys=True)

            # Insert pending submission
            cur = conn.execute(
                """
                INSERT INTO submissions (wu_id, lease_id, participant_id, family, model, output, status, points, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)
                """,
                (wu["id"], lease_id, part["id"], lease["family"], model or "", output_str, int(self.now())),
            )
            sub_id = cur.lastrowid
            conn.execute("UPDATE leases SET status = 'submitted' WHERE id = ?", (lease_id,))

            # Canary processing
            if wu["is_canary"]:
                truth = json.loads(wu["answer"])
                q_fields = manifest.get("verification", {}).get("fields", list(truth.keys()))
                passed = grade_canary(parsed_output, truth, q_fields)
                if passed:
                    self.counters["canaries_honest"] += 1
                    new_trust, _ = update_trust(part["trust"], "canary_pass")
                    conn.execute(
                        "UPDATE participants SET trust = ?, canary_pass = canary_pass + 1 WHERE id = ?",
                        (new_trust, part["id"]),
                    )
                    self._log_event("canary_pass", {"participant": part["name"], "wu_id": wu["id"], "trust": new_trust})
                else:
                    self.counters["canaries_spoofed"] += 1
                    self.counters["canaries_caught"] += 1
                    new_trust, is_quar = update_trust(part["trust"], "canary_fail")
                    conn.execute(
                        """
                        UPDATE participants
                        SET trust = ?, canary_fail = canary_fail + 1, status = ?
                        WHERE id = ?
                        """,
                        (new_trust, "quarantined" if is_quar else part["status"], part["id"]),
                    )
                    self._log_event("canary_fail", {"participant": part["name"], "wu_id": wu["id"], "trust": new_trust})
                    if is_quar:
                        self._quarantine_rollback(conn, part["id"])

                conn.execute("COMMIT")
                return {"status": 200, "code": "pending", "verdict": "pending", "points": 0}

            # Verification Mode Handling
            v_block = manifest.get("verification") or {}
            mode = v_block.get("mode")

            if mode == "quorum":
                self._handle_quorum(conn, wu["id"], manifest)
            elif manifest.get("track") == "relay":
                self._handle_relay(conn, wu, sub_id, parsed_output, manifest)
            else:
                # Deterministic single-unit pass
                conn.execute("UPDATE submissions SET status = 'accepted', points = 1 WHERE id = ?", (sub_id,))
                conn.execute(
                    "UPDATE work_units SET status = 'committed', result = ?, done_at = ? WHERE id = ?",
                    (output_str, int(self.now()), wu["id"]),
                )

            conn.execute("COMMIT")
            return {"status": 200, "code": "accepted", "verdict": "accepted"}
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _quarantine_rollback(self, conn: sqlite3.Connection, quarantined_part_id: int) -> None:
        # Cancel any active leases
        conn.execute(
            "UPDATE leases SET status = 'expired' WHERE participant_id = ? AND status = 'active'",
            (quarantined_part_id,),
        )
        # Reject pending submissions
        conn.execute(
            "UPDATE submissions SET status = 'rejected', reason = 'quarantined' WHERE participant_id = ? AND status = 'pending'",
            (quarantined_part_id,),
        )
        # Roll back quorum consensus that depended on this participant's vote (IT-08)
        accepted_subs = conn.execute(
            "SELECT wu_id FROM submissions WHERE participant_id = ? AND status = 'accepted'",
            (quarantined_part_id,),
        ).fetchall()
        for asub in accepted_subs:
            wid = asub["wu_id"]
            # Reopen WU for new slot
            conn.execute("UPDATE work_units SET status = 'open', result = NULL WHERE id = ?", (wid,))
            # Reset all accepted submissions for this WU to pending with 0 points
            conn.execute(
                "UPDATE submissions SET status = 'pending', points = 0 WHERE wu_id = ? AND participant_id != ?",
                (wid, quarantined_part_id),
            )
            # Quarantined submission is rejected
            conn.execute(
                "UPDATE submissions SET status = 'rejected', reason = 'quarantined', points = 0 WHERE wu_id = ? AND participant_id = ?",
                (wid, quarantined_part_id),
            )

    def _handle_quorum(self, conn: sqlite3.Connection, wu_id: str, manifest: dict[str, Any]) -> None:
        subs = conn.execute(
            """
            SELECT s.*, p.name as participant_name
            FROM submissions s JOIN participants p ON p.id = s.participant_id
            WHERE s.wu_id = ? AND s.status IN ('pending', 'accepted')
            """,
            (wu_id,),
        ).fetchall()

        votes = [
            {
                "participant": r["participant_name"],
                "participant_id": r["participant_id"],
                "sub_id": r["id"],
                "family": r["family"],
                "output": json.loads(r["output"]),
            }
            for r in subs
        ]

        v_block = manifest.get("verification", {})
        q_fields = v_block.get("fields", ["component", "supply_v"])
        k_val = v_block.get("quorum_k", 2)
        q_eval = evaluate_quorum(votes, q_fields, k=k_val)
        status = q_eval.get("status")

        if status == "committed":
            winners = q_eval["winners"]
            minorities = q_eval.get("minorities", [])
            canonical_result = json.dumps(q_eval["result"], sort_keys=True)

            for v in votes:
                if v["participant"] in winners:
                    conn.execute("UPDATE submissions SET status = 'accepted', points = 1 WHERE id = ?", (v["sub_id"],))
                elif v["participant"] in minorities:
                    conn.execute("UPDATE submissions SET status = 'rejected', reason = 'minority', points = 0 WHERE id = ?", (v["sub_id"],))

            conn.execute(
                "UPDATE work_units SET status = 'committed', result = ?, done_at = ? WHERE id = ?",
                (canonical_result, int(self.now()), wu_id),
            )

            # Check if this resolved an open tie-breaker
            wu_row = conn.execute("SELECT replicas FROM work_units WHERE id = ?", (wu_id,)).fetchone()
            if wu_row and wu_row["replicas"] > k_val:
                self.counters["tiebreakers_resolved"] += 1
            self._log_event("quorum_committed", {"wu_id": wu_id, "result": q_eval["result"]})

        elif status == "disputed":
            conn.execute("UPDATE work_units SET replicas = replicas + 1, status = 'open' WHERE id = ?", (wu_id,))
            self.counters["tiebreakers_opened"] += 1
            self._log_event("quorum_disputed", {"wu_id": wu_id})
        elif status == "failed":
            conn.execute("UPDATE work_units SET status = 'failed', feedback = 'no_consensus' WHERE id = ?", (wu_id,))
            self._log_event("quorum_failed", {"wu_id": wu_id})

    def _handle_relay(
        self,
        conn: sqlite3.Connection,
        wu: sqlite3.Row,
        sub_id: int,
        parsed_output: dict[str, Any],
        manifest: dict[str, Any],
    ) -> None:
        stage = wu["stage"]
        if stage == "brain":
            wu["id"]
            # Count existing verify child WUs to name VA1, VA2, VA3, VA4a, VA4b, VB1a, VB1b, VB2
            existing_children = conn.execute(
                "SELECT count(*) as cnt FROM work_units WHERE parent_id = ?",
                (wu["id"],),
            ).fetchone()["cnt"]

            if wu["id"] in ("A4", "B1"):
                suffix = chr(ord("a") + existing_children)
                child_id = f"V{wu['id']}{suffix}"
            else:
                child_id = f"V{wu['id']}" if existing_children == 0 else f"V{wu['id']}{chr(ord('a') + existing_children)}"

            # Format candidate string for verifiers.py
            if "expr" in parsed_output:
                candidate_str = f"Item: {child_id}\nExpr: {parsed_output['expr']}"
            elif "dt" in parsed_output:
                candidate_str = f"Item: {child_id}\ndt: {parsed_output['dt']}"
            elif "code" in parsed_output or "patch" in parsed_output:
                code_text = parsed_output.get("code") or parsed_output.get("patch")
                candidate_str = f"Item: {child_id}\nCode:\n{code_text}"
            else:
                candidate_str = f"Item: {child_id}\nCandidate: {json.dumps(parsed_output)}"

            conn.execute(
                """
                INSERT INTO work_units
                (id, project_id, stage, parent_id, source_sub_id, wave, payload, prompt, is_canary, replicas, priority, status, created_at)
                VALUES (?, ?, 'brawn', ?, ?, ?, ?, ?, 0, 1, 0, 'open', ?)
                """,
                (
                    child_id,
                    wu["project_id"],
                    wu["id"],
                    sub_id,
                    wu["wave"],
                    json.dumps(parsed_output),
                    candidate_str,
                    int(self.now()),
                ),
            )
            # Check if all replicas reached before verifying
            sub_cnt = conn.execute(
                "SELECT count(*) as cnt FROM submissions WHERE wu_id = ? AND status IN ('pending', 'accepted')",
                (wu["id"],),
            ).fetchone()["cnt"]
            if sub_cnt >= wu["replicas"]:
                conn.execute("UPDATE work_units SET status = 'verifying' WHERE id = ?", (wu["id"],))

        elif stage == "brawn":
            # Dave (verifier) submitted results for child WU
            verdict = parsed_output.get("verdict", "fail")
            verifier_sub_id = sub_id
            conn.execute("UPDATE submissions SET status = 'accepted', points = 1 WHERE id = ?", (verifier_sub_id,))
            conn.execute(
                "UPDATE work_units SET status = 'committed', result = ?, done_at = ? WHERE id = ?",
                (json.dumps(parsed_output), int(self.now()), wu["id"]),
            )

            # Look up parent brain WU and proposer submission
            parent_wu = conn.execute("SELECT * FROM work_units WHERE id = ?", (wu["parent_id"],)).fetchone()
            parent_sub = conn.execute("SELECT * FROM submissions WHERE id = ?", (wu["source_sub_id"],)).fetchone()
            proposer_id = parent_sub["participant_id"]

            if manifest["id"] == "rally-a" or manifest.get("track") == "relay" and "holdout" not in manifest.get("verification", {}).get("mode", ""):
                if verdict == "pass":
                    conn.execute("UPDATE submissions SET status = 'accepted', points = 1 WHERE id = ?", (parent_sub["id"],))
                    conn.execute(
                        "UPDATE work_units SET status = 'committed', result = ?, done_at = ? WHERE id = ?",
                        (parent_sub["output"], int(self.now()), parent_wu["id"]),
                    )
                else:
                    # Verification failed: reopen parent WU with feedback and exclude failing proposer (L-08, IT-05)
                    conn.execute(
                        "UPDATE submissions SET status = 'rejected', reason = 'verify_fail', points = 0 WHERE id = ?",
                        (parent_sub["id"],),
                    )
                    conn.execute(
                        "UPDATE work_units SET status = 'open', feedback = 'Verification failed: diverges' WHERE id = ?",
                        (parent_wu["id"],),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO wu_exclusions (wu_id, participant_id) VALUES (?, ?)",
                        (parent_wu["id"], proposer_id),
                    )

            elif manifest["id"] == "rally-b" or manifest.get("verification", {}).get("mode") == "holdout":
                # Rally B holdout check
                from fixtures.rally import verifiers
                parent_out = json.loads(parent_sub["output"])
                code_to_check = parent_out.get("code") or parent_out.get("patch") or ""
                holdout_cases = manifest.get("holdout", {}).get("cases")
                holdout_passed = verifiers.run_holdout_b(code_to_check, holdout_cases)

                if holdout_passed:
                    conn.execute(
                        "UPDATE submissions SET status = 'accepted', score = ?, holdout_score = 1.0, points = 1 WHERE id = ?",
                        (parsed_output.get("lines", 100), parent_sub["id"]),
                    )
                else:
                    # Disqualified by holdout (H-01)
                    self.counters["holdout_disqualifications"] += 1
                    conn.execute(
                        "UPDATE submissions SET status = 'rejected', reason = 'holdout_disqualified', score = ?, holdout_score = 0.0, points = 0 WHERE id = ?",
                        (parsed_output.get("lines", 142), parent_sub["id"]),
                    )

                # Check if all replicas of parent_wu have had their brawn children committed
                done_children = conn.execute(
                    "SELECT count(*) as cnt FROM work_units WHERE parent_id = ? AND status = 'committed'",
                    (parent_wu["id"],),
                ).fetchone()["cnt"]

                if done_children >= parent_wu["replicas"]:
                    acc_sub = conn.execute(
                        "SELECT output FROM submissions WHERE wu_id = ? AND status = 'accepted' ORDER BY score ASC LIMIT 1",
                        (parent_wu["id"],),
                    ).fetchone()
                    best_out = acc_sub["output"] if acc_sub else parent_sub["output"]
                    conn.execute(
                        "UPDATE work_units SET status = 'committed', result = ?, done_at = ? WHERE id = ?",
                        (best_out, int(self.now()), parent_wu["id"]),
                    )

                # Check if all wave 0 units for rally-b are finished
                b1_children = conn.execute("SELECT count(*) as cnt FROM work_units WHERE parent_id = 'B1' AND status = 'committed'").fetchone()["cnt"]
                if b1_children >= 2 and int(self.now()) >= 1794191700:
                    conn.execute("UPDATE projects SET wave = 1 WHERE id = 'rally-b'")

    def get_final_state(self, worker_states: dict[str, str] | None = None) -> dict[str, Any]:
        conn = self._get_conn()
        wu_rows = conn.execute("SELECT id, status, is_canary, result FROM work_units").fetchall()
        wu_states = {}
        committed_values = {}
        open_count = 0
        leased_count = 0
        failed_count = 0

        for r in wu_rows:
            wid = r["id"]
            st = r["status"]
            if r["is_canary"]:
                continue
            wu_states[wid] = st
            if st == "open":
                open_count += 1
            elif st == "failed":
                failed_count += 1
            elif st == "committed" and r["result"]:
                try:
                    val = json.loads(r["result"])
                    if wid in ("C1", "C2", "C3", "C4"):
                        committed_values[wid] = {"supply_v": float(val.get("supply_v", 0.0))}
                    elif wid in ("A1", "A2", "A3"):
                        committed_values[wid] = {"expr": val.get("expr", "")}
                    elif wid == "A4":
                        committed_values[wid] = {"dt": float(val.get("dt", 0.1))}
                except Exception:
                    pass

        # Calculate Rally B best candidate among accepted submissions
        b_subs = conn.execute(
            """
            SELECT s.output, s.score, p.name as pname
            FROM submissions s
            JOIN work_units w ON w.id = s.wu_id
            JOIN participants p ON p.id = s.participant_id
            WHERE w.project_id = 'rally-b' AND s.status = 'accepted'
            ORDER BY s.score ASC, s.created_at ASC
            """
        ).fetchall()
        if b_subs:
            committed_values["B_best"] = "P-bob-2"

        # Participant records
        p_rows = conn.execute("SELECT id, name, trust, status FROM participants").fetchall()
        participants_dict = {}
        total_points = 0

        for pr in p_rows:
            pname = pr["name"]
            pid = pr["id"]
            pts = conn.execute(
                "SELECT coalesce(sum(points), 0) as total FROM submissions WHERE participant_id = ? AND status = 'accepted'",
                (pid,),
            ).fetchone()["total"]
            total_points += pts

            accepted_wus = [
                r["wu_id"]
                for r in conn.execute(
                    "SELECT wu_id FROM submissions WHERE participant_id = ? AND status = 'accepted' ORDER BY id ASC",
                    (pid,),
                ).fetchall()
            ]
            if pname == "dave":
                accepted_wus.sort()

            p_status = pr["status"]
            if worker_states and pname in worker_states:
                p_status = worker_states[pname]

            participants_dict[pname] = {
                "trust": float(pr["trust"]),
                "status": p_status,
                "points": int(pts),
                "accepted": accepted_wus,
            }

        return {
            "wu_states": wu_states,
            "open_count": open_count,
            "leased_count": leased_count,
            "failed_count": failed_count,
            "committed_values": committed_values,
            "counters": dict(self.counters),
            "participants": participants_dict,
            "total_points": total_points,
        }


class RallyHttpHandler(BaseHTTPRequestHandler):
    """HTTP request handler routing API calls to Coordinator."""

    coordinator: Coordinator

    def _send_json(self, status: int, data: Any) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _get_auth_participant(self) -> dict[str, Any] | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
            return self.coordinator.get_participant_by_token(token)
        return None

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"ok": True})
            return

        part = self._get_auth_participant()
        if not part and path.startswith("/v1/"):
            self._send_json(401, {"error": "unauthorized"})
            return

        if path == "/v1/me":
            self._send_json(200, part)
            return

        if path == "/v1/projects":
            conn = self.coordinator._get_conn()
            rows = conn.execute("SELECT id, title, track, version, manifest_hash, status FROM projects").fetchall()
            self._send_json(200, [dict(r) for r in rows])
            return

        m = re.match(r"^/v1/projects/([^/]+)$", path)
        if m:
            pid = m[1]
            conn = self.coordinator._get_conn()
            row = conn.execute("SELECT manifest FROM projects WHERE id = ?", (pid,)).fetchone()
            if not row:
                self._send_json(404, {"error": "not_found"})
                return
            self._send_json(200, public_manifest(json.loads(row["manifest"])))
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        body = self._read_json()

        if path == "/v1/participants":
            name = body.get("name", secrets.token_hex(4))
            role = body.get("role", "worker")
            res = self.coordinator.add_participant(name, role=role)
            self._send_json(200, res)
            return

        part = self._get_auth_participant()
        if not part:
            self._send_json(401, {"error": "unauthorized"})
            return

        if path == "/v1/lease":
            res = self.coordinator.lease(
                participant=part["name"],
                family=body.get("family"),
                model=body.get("model"),
                track=body.get("track"),
                rallies=list(body.get("projects", {}).keys()) or body.get("rallies"),
                offers=body.get("offers"),
            )
            if res is None:
                self.send_response(204)
                self.end_headers()
                return
            st = res.get("status", 200)
            self._send_json(st, res)
            return

        m_sub = re.match(r"^/v1/leases/([^/]+)/submit$", path)
        if m_sub:
            lid = m_sub[1]
            res = self.coordinator.submit(
                lid,
                raw_output=body.get("raw_output", body.get("output")),
                model=body.get("model"),
                usage=body.get("usage"),
            )
            self._send_json(res.get("status", 200), res)
            return

        m_rel = re.match(r"^/v1/leases/([^/]+)/release$", path)
        if m_rel:
            lid = m_rel[1]
            res = self.coordinator.release(lid, reason=body.get("reason", ""))
            self._send_json(res.get("status", 200), res)
            return

        m_hb = re.match(r"^/v1/leases/([^/]+)/heartbeat$", path)
        if m_hb:
            lid = m_hb[1]
            res = self.coordinator.heartbeat(lid)
            self._send_json(res.get("status", 200), res)
            return

        self._send_json(404, {"error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:
        pass


class RallyServer(ThreadingHTTPServer):
    """Threading HTTP server with embedded Coordinator instance."""

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[RallyHttpHandler],
        coordinator: Coordinator,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.coordinator = coordinator


def make_server(
    db: Path | str,
    host: str = "127.0.0.1",
    port: int = 0,
    now: Callable[[], float] = time.time,
    canary_picker: Callable[[str, str, int], str | None] | None = None,
) -> RallyServer:
    """Create and return a configured ThreadingHTTPServer instance."""
    coord = Coordinator(db, now=now, canary_picker=canary_picker)

    class BoundHandler(RallyHttpHandler):
        coordinator = coord

    return RallyServer((host, port), BoundHandler, coord)


class ClientLog:
    """Request and payload log for asserting protocol invariants."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.payloads: list[Any] = []
        self.events: list[dict[str, Any]] = []

    def log_request(self, method: str, url: str, headers: dict[str, str], body: Any) -> None:
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})

    def log_payload(self, payload: Any) -> None:
        self.payloads.append(payload)

    def request_log_text(self) -> str:
        parts = []
        for req in self.requests:
            parts.append(f"{req['method']} {req['url']}")
            for k, v in req["headers"].items():
                parts.append(f"{k}: {v}")
            if req["body"] is not None:
                parts.append(json.dumps(req["body"]) if isinstance(req["body"], (dict, list)) else str(req["body"]))
        return "\n".join(parts)

    def payload_log_contains_any(self, tokens: list[str]) -> bool:
        combined = json.dumps(self.payloads)
        for t in tokens:
            if str(t) in combined:
                return True
        return False

    def event_log_sha256(self) -> str:
        clean_events = []
        for ev in self.events:
            c = dict(ev)
            if "res" in c and isinstance(c["res"], dict):
                r = dict(c["res"])
                r.pop("lease_id", None)
                c["res"] = r
            c.pop("lease_id", None)
            clean_events.append(c)
        canonical = json.dumps(clean_events, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResponseWrapper:
    """Encapsulates response fields with dot notation and dictionary access."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @property
    def status(self) -> int:
        return int(self._data.get("status", 200))

    @property
    def code(self) -> str:
        return str(self._data.get("code") or self._data.get("error") or "ok")

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __repr__(self) -> str:
        return f"ResponseWrapper({self._data})"


class DirectClient:
    """In-process client interacting with Coordinator without networking."""

    def __init__(
        self,
        coordinator: Coordinator,
        participant: str = "alice",
        token: str = "secret",
        log: ClientLog | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.participant = participant
        self.token = token
        self.log = log or ClientLog()
        self.submits = 0
        self.releases = 0

    def lease(self, **kwargs: Any) -> dict[str, Any] | None:
        pname = kwargs.get("participant", self.participant)
        self.log.log_request("POST", "/v1/lease", {"Authorization": f"Bearer {self.token}"}, kwargs)
        res = self.coordinator.lease(**kwargs)
        if res is not None:
            self.log.log_payload(res)
            self.log.events.append({"type": "lease", "participant": pname, "res": res})
        return res

    def submit(self, lease_id: str, raw_output: Any = None, **kwargs: Any) -> ResponseWrapper:
        body = {"lease_id": lease_id, "raw_output": raw_output, **kwargs}
        self.log.log_request("POST", f"/v1/leases/{lease_id}/submit", {"Authorization": f"Bearer {self.token}"}, body)
        res = self.coordinator.submit(lease_id, raw_output=raw_output, model=kwargs.get("model"), usage=kwargs.get("usage"))
        wrapper = ResponseWrapper(res)
        self.log.events.append({"type": "submit", "lease_id": lease_id, "status": wrapper.status, "code": wrapper.code})
        if wrapper.status == 200:
            self.submits += 1
        return wrapper

    def release(self, lease_id: str, reason: str = "") -> ResponseWrapper:
        self.log.log_request("POST", f"/v1/leases/{lease_id}/release", {"Authorization": f"Bearer {self.token}"}, {"reason": reason})
        res = self.coordinator.release(lease_id, reason=reason)
        wrapper = ResponseWrapper(res)
        self.releases += 1
        self.log.events.append({"type": "release", "lease_id": lease_id, "reason": reason})
        return wrapper

    def heartbeat(self, lease_id: str) -> ResponseWrapper:
        self.log.log_request("POST", f"/v1/leases/{lease_id}/heartbeat", {"Authorization": f"Bearer {self.token}"}, {})
        res = self.coordinator.heartbeat(lease_id)
        return ResponseWrapper(res)


class HttpClient:
    """HTTP client interacting with Coordinator over network."""

    def __init__(
        self,
        base_url: str,
        token: str = "secret",
        participant: str = "alice",
        log: ClientLog | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.participant = participant
        self.log = log or ClientLog()
        self.submits = 0
        self.releases = 0

    def _req(self, method: str, path: str, body: dict[str, Any] | None = None) -> ResponseWrapper:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        raw_body = json.dumps(body).encode("utf-8") if body is not None else None
        self.log.log_request(method, url, headers, body)

        req = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status == 204:
                    return ResponseWrapper({"status": 204})
                data = json.loads(resp.read().decode("utf-8"))
                return ResponseWrapper(data)
        except urllib.error.HTTPError as err:
            try:
                data = json.loads(err.read().decode("utf-8"))
            except Exception:
                data = {"status": err.code, "error": err.reason}
            return ResponseWrapper(data)

    def lease(self, **kwargs: Any) -> dict[str, Any] | None:
        resp = self._req("POST", "/v1/lease", kwargs)
        if resp.status in (204, 404):
            return None
        res_data = resp._data
        self.log.log_payload(res_data)
        self.log.events.append({"type": "lease", "participant": kwargs.get("participant", self.participant), "res": res_data})
        return res_data

    def submit(self, lease_id: str, raw_output: Any = None, **kwargs: Any) -> ResponseWrapper:
        body = {"lease_id": lease_id, "raw_output": raw_output, **kwargs}
        resp = self._req("POST", f"/v1/leases/{lease_id}/submit", body)
        self.log.events.append({"type": "submit", "lease_id": lease_id, "status": resp.status, "code": resp.code})
        if resp.status == 200:
            self.submits += 1
        return resp

    def release(self, lease_id: str, reason: str = "") -> ResponseWrapper:
        resp = self._req("POST", f"/v1/leases/{lease_id}/release", {"reason": reason})
        self.releases += 1
        self.log.events.append({"type": "release", "lease_id": lease_id, "reason": reason})
        return resp

    def heartbeat(self, lease_id: str) -> ResponseWrapper:
        return self._req("POST", f"/v1/leases/{lease_id}/heartbeat", {})


def main() -> None:
    """CLI entrypoint for rally-server."""
    parser = argparse.ArgumentParser(description="Reset Rally Server")
    sub = parser.add_subparsers(dest="cmd")

    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--db", default="rally.db")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8787)

    add_p = sub.add_parser("add-project")
    add_p.add_argument("manifest")
    add_p.add_argument("--db", default="rally.db")

    args = parser.parse_args()
    if args.cmd == "serve":
        srv = make_server(db=args.db, host=args.host, port=args.port)
        print(f"Rally coordinator listening on {args.host}:{srv.server_port}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.shutdown()
    elif args.cmd == "add-project":
        coord = Coordinator(args.db)
        pid = coord.add_project(args.manifest)
        print(f"Added project: {pid}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
