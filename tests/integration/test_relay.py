"""Integration tests for Relay track linking brain proposals to brawn verifiers (IT-04..IT-06)."""

from pathlib import Path
import pytest

from rally.server import Coordinator

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rally"


def setup_relay(tmp_path: Path) -> Coordinator:
    coord = Coordinator(tmp_path / "relay.db")
    coord.add_project(FIXTURES_DIR / "a_math_physics.json")
    coord.add_participant("carol", role="worker")
    coord.add_participant("dave", role="worker")
    coord.add_participant("bob", role="worker")
    return coord


def test_it04_brain_proposal_creates_linked_brawn_wu(tmp_path: Path):
    """IT-04: Brain proposal passing Gate 1 creates one linked brawn verify WU with parent_id."""
    coord = setup_relay(tmp_path)
    conn = coord._get_conn()

    lease = coord.lease("carol", family="google", track="brain", rallies=["rally-a"])
    assert lease is not None
    assert lease["wu"]["id"] == "A1"

    # Carol submits valid closed form expression
    sub_res = coord.submit(lease["lease_id"], raw_output={"expr": "(n*(n+1)//2)**2"})
    assert sub_res["status"] == 200

    # Linked brawn verify WU VA1 is created with parent_id = 'A1'
    child = conn.execute("SELECT * FROM work_units WHERE parent_id = 'A1'").fetchone()
    assert child is not None
    assert child["id"] == "VA1"
    assert child["stage"] == "brawn"
    assert child["status"] == "open"

    # Parent WU status is verifying
    parent = conn.execute("SELECT status FROM work_units WHERE id = 'A1'").fetchone()
    assert parent["status"] == "verifying"


def test_it05_brawn_verify_failure_reopens_brain_wu_with_feedback(tmp_path: Path):
    """IT-05: When brawn verify fails, brain WU reopens with feedback and excludes failing proposer."""
    coord = setup_relay(tmp_path)
    conn = coord._get_conn()

    # Fast forward A1..A3
    conn.execute("UPDATE work_units SET status = 'committed' WHERE id IN ('A1', 'A2', 'A3')")

    # Carol proposes dt = 2.5 on A4
    lease_c = coord.lease("carol", family="google", track="brain", rallies=["rally-a"])
    assert lease_c is not None
    assert lease_c["wu"]["id"] == "A4"
    coord.submit(lease_c["lease_id"], raw_output={"dt": 2.5})

    # Dave verifies VA4a with fail verdict
    va4 = conn.execute("SELECT id FROM work_units WHERE parent_id = 'A4'").fetchone()
    assert va4 is not None
    lease_d = coord.lease("dave", family="brawn", track="brawn", rallies=["rally-a"])
    assert lease_d is not None
    assert lease_d["wu"]["id"] == va4["id"]
    coord.submit(lease_d["lease_id"], raw_output={"verdict": "fail", "reason": "diverged"})

    # Brain WU A4 is reopened with feedback text
    parent = conn.execute("SELECT status, feedback FROM work_units WHERE id = 'A4'").fetchone()
    assert parent["status"] == "open"
    assert "diverges" in parent["feedback"]

    # Carol is excluded from taking A4 again
    carol_lease_retry = coord.lease("carol", family="google", track="brain", rallies=["rally-a"])
    assert carol_lease_retry is None

    # Bob can lease A4 with the feedback visible
    bob_lease = coord.lease("bob", family="openai", track="brain", rallies=["rally-a"])
    assert bob_lease is not None
    assert bob_lease["wu"]["id"] == "A4"
    assert "Feedback:" in bob_lease["prompt"]


def test_it06_brawn_verify_pass_commits_parent_and_awards_points(tmp_path: Path):
    """IT-06: When brawn verify passes, brain WU commits and awards Rally Points to both proposer and verifier."""
    coord = setup_relay(tmp_path)
    conn = coord._get_conn()

    # Carol proposes A1
    lease_c = coord.lease("carol", family="google", track="brain", rallies=["rally-a"])
    assert lease_c is not None
    assert lease_c["wu"]["id"] == "A1"
    coord.submit(lease_c["lease_id"], raw_output={"expr": "(n*(n+1)//2)**2"})

    # Dave verifies VA1 with pass verdict
    lease_d = coord.lease("dave", family="brawn", track="brawn", rallies=["rally-a"])
    assert lease_d is not None
    assert lease_d["wu"]["id"] == "VA1"
    coord.submit(lease_d["lease_id"], raw_output={"verdict": "pass"})

    # Parent A1 commits and child VA1 commits
    a1_row = conn.execute("SELECT status, result FROM work_units WHERE id = 'A1'").fetchone()
    assert a1_row["status"] == "committed"

    va1_row = conn.execute("SELECT status FROM work_units WHERE id = 'VA1'").fetchone()
    assert va1_row["status"] == "committed"

    # Both Carol and Dave earn 1 point each
    state = coord.get_final_state()
    assert state["participants"]["carol"]["points"] == 1
    assert state["participants"]["dave"]["points"] == 1
