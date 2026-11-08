"""Unit tests for Rally Points ledger accounting and commit timing (P-01, P-02)."""

from pathlib import Path
import pytest

from rally.server import Coordinator

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rally"


def setup_coord(tmp_path: Path) -> Coordinator:
    coord = Coordinator(tmp_path / "ledger.db")
    coord.add_project(FIXTURES_DIR / "c_corpus.json")
    return coord


def test_p01_ledger_points_conservation(tmp_path: Path):
    """P-01: Points = accepted * manifest points; rejected, minority, disqualified, Canary earn 0."""
    coord = setup_coord(tmp_path)
    coord.add_participant("alice")
    coord.add_participant("bob")
    coord.add_participant("eve")

    conn = coord._get_conn()
    now_ts = int(coord.now())

    # Insert leases to satisfy foreign key constraints
    for lid, wid, pid, fam in [
        ("l1", "C1", 1, "anthropic"),
        ("l2", "C2", 1, "anthropic"),
        ("l3", "C3", 2, "openai"),
        ("l4", "C4", 2, "openai"),
        ("l5", "K1", 3, "deepseek"),
    ]:
        conn.execute(
            """
            INSERT INTO leases (id, wu_id, participant_id, adapter, family, model, slot, status, issued_at, expires_at)
            VALUES (?, ?, ?, 'cli', ?, 'model', 1, 'submitted', ?, ?)
            """,
            (lid, wid, pid, fam, now_ts, now_ts + 900),
        )

    # Insert accepted submission (1 point)
    conn.execute(
        """
        INSERT INTO submissions (wu_id, lease_id, participant_id, family, model, output, status, points, created_at)
        VALUES ('C1', 'l1', 1, 'anthropic', 'sonnet', '{"supply_v": 5.5}', 'accepted', 1, ?)
        """,
        (now_ts,),
    )

    # Insert rejected submission (0 points)
    conn.execute(
        """
        INSERT INTO submissions (wu_id, lease_id, participant_id, family, model, output, status, reason, points, created_at)
        VALUES ('C2', 'l2', 1, 'anthropic', 'sonnet', '{"supply_v": "invalid"}', 'rejected', 'gate1:schema', 0, ?)
        """,
        (now_ts,),
    )

    # Insert minority submission (0 points)
    conn.execute(
        """
        INSERT INTO submissions (wu_id, lease_id, participant_id, family, model, output, status, reason, points, created_at)
        VALUES ('C3', 'l3', 2, 'openai', 'astra', '{"supply_v": 5.0}', 'rejected', 'minority', 0, ?)
        """,
        (now_ts,),
    )

    # Insert disqualified submission (0 points)
    conn.execute(
        """
        INSERT INTO submissions (wu_id, lease_id, participant_id, family, model, output, status, reason, points, created_at)
        VALUES ('C4', 'l4', 2, 'openai', 'astra', '{"code": "def count_primes(n): pass"}', 'rejected', 'holdout_disqualified', 0, ?)
        """,
        (now_ts,),
    )

    # Insert canary submission (0 points)
    conn.execute(
        """
        INSERT INTO submissions (wu_id, lease_id, participant_id, family, model, output, status, points, created_at)
        VALUES ('K1', 'l5', 3, 'deepseek', 'deepseek', '{"supply_v": 1.8}', 'pending', 0, ?)
        """,
        (now_ts,),
    )

    state = coord.get_final_state()
    assert state["participants"]["alice"]["points"] == 1
    assert state["participants"]["bob"]["points"] == 0
    assert state["participants"]["eve"]["points"] == 0
    assert state["total_points"] == 1


def test_p02_points_written_only_at_commit(tmp_path: Path):
    """P-02: Points are written only when a WU is committed; open or disputed WUs give 0 points."""
    coord = setup_coord(tmp_path)
    coord.add_participant("alice")
    coord.add_participant("bob")

    # Alice submits C3 -> only 1 vote so far, status is pending
    lease1 = coord.lease("alice", family="anthropic", track="brain", rallies=["rally-c"])
    assert lease1 is not None
    assert lease1["wu"]["id"] == "C1"
    coord.submit(
        lease1["lease_id"],
        raw_output={"component": "RX-100", "supply_v": 5.5, "quote": "The RX-100 regulator takes a 5.5 V supply."},
    )

    # Before quorum consensus is reached, points must be 0
    state_pre = coord.get_final_state()
    assert state_pre["participants"]["alice"]["points"] == 0
    assert state_pre["wu_states"]["C1"] == "open"

    # Bob submits matching C1 -> consensus reached, WU committed
    lease2 = coord.lease("bob", family="openai", track="brain", rallies=["rally-c"])
    assert lease2 is not None
    assert lease2["wu"]["id"] == "C1"
    coord.submit(
        lease2["lease_id"],
        raw_output={"component": "RX-100", "supply_v": 5.5, "quote": "The RX-100 regulator takes a 5.5 V supply."},
    )

    state_post = coord.get_final_state()
    assert state_post["wu_states"]["C1"] == "committed"
    assert state_post["participants"]["alice"]["points"] == 1
    assert state_post["participants"]["bob"]["points"] == 1
