"""Unit tests for lease scheduling, expiry, release, and exclusivity rules (L-01..L-10)."""

import json
from pathlib import Path
import pytest

from rally import LEASE_TTL, MAX_ATTEMPTS
from rally.server import Coordinator, DirectClient

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rally"


def setup_coordinator(tmp_path: Path, clock_val: float = 1000.0) -> tuple[Coordinator, list[float]]:
    t = [clock_val]
    now_fn = lambda: t[0]
    coord = Coordinator(tmp_path / "test.db", now=now_fn)
    coord.add_project(FIXTURES_DIR / "a_math_physics.json")
    coord.add_project(FIXTURES_DIR / "b_code_opt.json")
    coord.add_project(FIXTURES_DIR / "c_corpus.json")
    return coord, t


def test_l01_lowest_open_id_and_distinct_wus(tmp_path: Path):
    """L-01: Two participants get different WUs; the lowest open ID goes first."""
    coord, _ = setup_coordinator(tmp_path)
    coord.add_participant("alice", role="worker")
    coord.add_participant("bob", role="worker")

    lease1 = coord.lease("alice", family="anthropic", track="brain", rallies=["rally-a"])
    assert lease1 is not None
    assert lease1["wu"]["id"] == "A1"

    lease2 = coord.lease("bob", family="openai", track="brain", rallies=["rally-a"])
    assert lease2 is not None
    assert lease2["wu"]["id"] == "A2"


def test_l02_expiry_boundary(tmp_path: Path):
    """L-02: Lease active at t + TTL - 1s, expired at t + TTL (<= boundary)."""
    coord, clock = setup_coordinator(tmp_path, 1000.0)
    coord.add_participant("alice")

    lease = coord.lease("alice", family="anthropic", track="brain", rallies=["rally-a"])
    assert lease is not None
    lid = lease["lease_id"]

    # At t + TTL - 1s -> active
    clock[0] = 1000.0 + LEASE_TTL - 1
    conn = coord._get_conn()
    coord._expire_leases(conn)
    row = conn.execute("SELECT status FROM leases WHERE id = ?", (lid,)).fetchone()
    assert row["status"] == "active"

    # At t + TTL -> expired
    clock[0] = 1000.0 + LEASE_TTL
    coord._expire_leases(conn)
    row = conn.execute("SELECT status FROM leases WHERE id = ?", (lid,)).fetchone()
    assert row["status"] == "expired"


def test_l03_heartbeat_live_and_expired(tmp_path: Path):
    """L-03: Heartbeat on live lease extends TTL; heartbeat after expiry returns 409."""
    coord, clock = setup_coordinator(tmp_path, 1000.0)
    coord.add_participant("alice")

    lease = coord.lease("alice", family="anthropic", track="brain", rallies=["rally-a"])
    assert lease is not None
    lid = lease["lease_id"]

    # Advance time halfway and heartbeat
    clock[0] = 1400.0
    res = coord.heartbeat(lid)
    assert res["status"] == 200
    assert res["expires_at"] == int(1400.0 + LEASE_TTL)

    # Advance past expiry and heartbeat
    clock[0] = 1400.0 + LEASE_TTL + 10.0
    res_expired = coord.heartbeat(lid)
    assert res_expired["status"] == 409


def test_l04_submit_after_expiry(tmp_path: Path):
    """L-04: Submit after expiry returns 409 lease_expired; no submission row or points."""
    coord, clock = setup_coordinator(tmp_path, 1000.0)
    coord.add_participant("alice")

    lease = coord.lease("alice", family="anthropic", track="brain", rallies=["rally-a"])
    assert lease is not None
    lid = lease["lease_id"]

    clock[0] = 1000.0 + LEASE_TTL + 5.0
    res = coord.submit(lid, raw_output={"expr": "(n*(n+1)//2)**2"})
    assert res["status"] == 409
    assert res["code"] == "lease_expired"

    conn = coord._get_conn()
    sub_row = conn.execute("SELECT * FROM submissions WHERE lease_id = ?", (lid,)).fetchone()
    assert sub_row is None


def test_l05_release_reopens_slot(tmp_path: Path):
    """L-05: Release opens WU slot immediately and attempt is not counted as failure."""
    coord, _ = setup_coordinator(tmp_path)
    coord.add_participant("alice")
    coord.add_participant("bob")

    lease = coord.lease("alice", family="anthropic", track="brain", rallies=["rally-a"])
    assert lease is not None
    assert lease["wu"]["id"] == "A1"

    coord.release(lease["lease_id"], reason="rate_limited")

    # Bob can now immediately lease A1
    lease_bob = coord.lease("bob", family="openai", track="brain", rallies=["rally-a"])
    assert lease_bob is not None
    assert lease_bob["wu"]["id"] == "A1"


def test_l06_max_attempts_marks_failed(tmp_path: Path):
    """L-06: A slot expiring MAX_ATTEMPTS times marks WU failed with reason max_attempts."""
    coord, clock = setup_coordinator(tmp_path, 1000.0)
    for p in ("p1", "p2", "p3"):
        coord.add_participant(p)

    # 3 participants lease A1 sequentially and each expires
    for p in ("p1", "p2", "p3"):
        lease = coord.lease(p, family=f"fam_{p}", track="brain", rallies=["rally-a"])
        assert lease is not None
        assert lease["wu"]["id"] == "A1"
        clock[0] += LEASE_TTL + 10.0
        conn = coord._get_conn()
        coord._expire_leases(conn)

    conn = coord._get_conn()
    wu = conn.execute("SELECT * FROM work_units WHERE id = 'A1'").fetchone()
    assert wu["status"] == "failed"
    assert "max_attempts" in (wu["feedback"] or "")


def test_l07_family_exclusivity_on_quorum(tmp_path: Path):
    """L-07: Alice holds C1 slot 1; Alice cannot get C1 again; second anthropic participant cannot get C1."""
    coord, _ = setup_coordinator(tmp_path)
    coord.add_participant("alice")
    coord.add_participant("alice2")
    coord.add_participant("bob")

    # Alice leases C1
    lease_a1 = coord.lease("alice", family="anthropic", track="brain", rallies=["rally-c"])
    assert lease_a1 is not None
    assert lease_a1["wu"]["id"] == "C1"

    # Alice asks again -> gets C2 (cannot fill two slots of C1)
    lease_a2 = coord.lease("alice", family="anthropic", track="brain", rallies=["rally-c"])
    assert lease_a2 is not None
    assert lease_a2["wu"]["id"] == "C2"

    # A second anthropic participant asks -> distinct_families blocks them from C1 and C2
    lease_a3 = coord.lease("alice2", family="anthropic", track="brain", rallies=["rally-c"])
    assert lease_a3 is not None
    assert lease_a3["wu"]["id"] == "C3"

    # Bob (openai) can take slot 2 of C1
    lease_bob = coord.lease("bob", family="openai", track="brain", rallies=["rally-c"])
    assert lease_bob is not None
    assert lease_bob["wu"]["id"] == "C1"


def test_l08_failing_proposer_excluded_from_retry(tmp_path: Path):
    """L-08: Carol's A4 proposal failed verify; Carol asks for A; Carol never gets A4 again."""
    coord, _ = setup_coordinator(tmp_path)
    coord.add_participant("carol")
    coord.add_participant("dave")
    coord.add_participant("bob")

    # Fast forward A1..A3 to leave A4 as open
    conn = coord._get_conn()
    conn.execute("UPDATE work_units SET status = 'committed' WHERE id IN ('A1', 'A2', 'A3')")

    lease_carol = coord.lease("carol", family="google", track="brain", rallies=["rally-a"])
    assert lease_carol is not None
    assert lease_carol["wu"]["id"] == "A4"

    # Carol submits dt = 2.5
    coord.submit(lease_carol["lease_id"], raw_output={"dt": 2.5})

    # Dave verifies VA4a with fail verdict
    va4_row = conn.execute("SELECT id FROM work_units WHERE parent_id = 'A4'").fetchone()
    assert va4_row is not None
    lease_dave = coord.lease("dave", family="brawn", track="brawn", rallies=["rally-a"])
    assert lease_dave is not None
    assert lease_dave["wu"]["id"] == va4_row["id"]
    coord.submit(lease_dave["lease_id"], raw_output={"verdict": "fail", "reason": "diverged"})

    # Parent A4 is open again, but Carol is excluded
    lease_carol_retry = coord.lease("carol", family="google", track="brain", rallies=["rally-a"])
    assert lease_carol_retry is None

    # Bob can lease A4
    lease_bob = coord.lease("bob", family="openai", track="brain", rallies=["rally-a"])
    assert lease_bob is not None
    assert lease_bob["wu"]["id"] == "A4"


def test_l09_quarantined_participant_gets_403(tmp_path: Path):
    """L-09: Quarantined participant gets 403 on lease request."""
    coord, _ = setup_coordinator(tmp_path)
    coord.add_participant("eve", trust=0.1)
    conn = coord._get_conn()
    conn.execute("UPDATE participants SET status = 'quarantined' WHERE name = 'eve'")

    res = coord.lease("eve", family="deepseek", track="brain", rallies=["rally-c"])
    assert res is not None
    assert res.get("status") == 403
    assert res.get("error") == "quarantined"


def test_l10_rally_preference_order(tmp_path: Path):
    """L-10: Participant subscribed to [B, C] gets B work before C work."""
    coord, _ = setup_coordinator(tmp_path)
    coord.add_participant("alice")

    lease = coord.lease("alice", family="anthropic", track="brain", rallies=["rally-b", "rally-c"])
    assert lease is not None
    assert lease["wu"]["project"] == "rally-b"
