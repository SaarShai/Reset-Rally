"""Integration tests for participant quarantine and rollback of uncommitted consensus (IT-08)."""

from pathlib import Path
import pytest

from rally.server import Coordinator

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rally"


def test_it08_quarantine_rolls_back_consensus(tmp_path: Path):
    """IT-08: Eve votes 9 on C4 with Bob 9 -> commit; Eve fails canary -> C4 reopens and points reverse."""
    coord = Coordinator(tmp_path / "quarantine.db")
    coord.add_project(FIXTURES_DIR / "c_corpus.json")
    coord.add_participant("bob", role="worker")
    coord.add_participant("eve", role="worker", trust=0.3)

    # Mark C1, C2, C3 committed so C4 is the active open WU
    conn = coord._get_conn()
    conn.execute("UPDATE work_units SET status = 'committed' WHERE id IN ('C1', 'C2', 'C3')")

    # Bob leases and submits on C4
    l_bob = coord.lease("bob", family="openai", track="brain", rallies=["rally-c"])
    assert l_bob is not None
    assert l_bob["wu"]["id"] == "C4"
    ans_bob = {"component": "QT-9", "supply_v": 9.0, "quote": "The QT-9 module uses a 9 V supply."}
    coord.submit(l_bob["lease_id"], raw_output=ans_bob)

    # Eve leases and submits matching on C4 -> consensus reached
    l_eve = coord.lease("eve", family="deepseek", track="brain", rallies=["rally-c"])
    assert l_eve is not None
    assert l_eve["wu"]["id"] == "C4"
    ans_eve = {"component": "QT-9", "supply_v": 9.0, "quote": "The QT-9 module uses a 9 V supply."}
    coord.submit(l_eve["lease_id"], raw_output=ans_eve)

    # C4 is committed; both Bob and Eve have 1 point
    st_commit = coord.get_final_state()
    assert st_commit["wu_states"]["C4"] == "committed"
    assert st_commit["participants"]["bob"]["points"] == 1
    assert st_commit["participants"]["eve"]["points"] == 1

    # Eve leases a canary K1 and submits wrong answer (spoofed 12 instead of 1.8)
    # Canary schedule seam or direct canary lease
    conn = coord._get_conn()
    canary_k1 = conn.execute("SELECT * FROM work_units WHERE id = 'K1'").fetchone()
    assert canary_k1 is not None

    # Force Eve to lease K1
    coord.canary_picker = lambda part, rally, seq: "K1"
    l_canary = coord.lease("eve", family="deepseek", track="brain", rallies=["rally-c"])
    assert l_canary is not None
    assert l_canary["wu"]["id"] == "K1"

    # Eve submits wrong canary answer -> trust drops below 0.2 -> quarantined
    wrong_ans = {"component": "KC-7 core", "supply_v": 12.0, "quote": "takes a 12 V adapter"}
    coord.submit(l_canary["lease_id"], raw_output=wrong_ans)

    # Eve must now be quarantined
    st_quar = coord.get_final_state()
    assert st_quar["participants"]["eve"]["status"] == "quarantined"
    assert st_quar["participants"]["eve"]["trust"] < 0.2

    # C4 consensus is rolled back: C4 goes back to 'open' and points reverse
    assert st_quar["wu_states"]["C4"] == "open"
    assert st_quar["participants"]["bob"]["points"] == 0
    assert st_quar["participants"]["eve"]["points"] == 0

    # Quarantined Eve gets 403 on next lease request
    l_blocked = coord.lease("eve", family="deepseek", track="brain", rallies=["rally-c"])
    assert l_blocked is not None
    assert l_blocked["status"] == 403
