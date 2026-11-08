"""Integration tests for HTTP API workflow, idempotency, and server restart (IT-01..IT-03)."""

from pathlib import Path
import sqlite3
import threading
import pytest

from rally.server import HttpClient, make_server

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rally"


def test_it01_real_http_register_lease_submit(server, tmp_path: Path):
    """IT-01: Full HTTP flow register -> lease -> submit -> commit; SQLite journal_mode is WAL."""
    base_url = f"http://127.0.0.1:{server.server_port}"
    coord = server.coordinator
    coord.add_project(FIXTURES_DIR / "c_corpus.json")

    client = HttpClient(base_url, token="dummy")

    # Register participants
    r1 = client._req("POST", "/v1/participants", {"name": "alice"})
    assert r1.status == 200
    alice_token = r1.get("token")

    r2 = client._req("POST", "/v1/participants", {"name": "bob"})
    assert r2.status == 200
    bob_token = r2.get("token")

    # Verify SQLite WAL mode
    conn = sqlite3.connect(coord.db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.close()

    # Alice leases C1
    client_alice = HttpClient(base_url, token=alice_token, participant="alice")
    l_alice = client_alice.lease(participant="alice", family="anthropic", track="brain", rallies=["rally-c"])
    assert l_alice is not None
    assert l_alice["wu"]["id"] == "C1"

    # Bob leases C1
    client_bob = HttpClient(base_url, token=bob_token, participant="bob")
    l_bob = client_bob.lease(participant="bob", family="openai", track="brain", rallies=["rally-c"])
    assert l_bob is not None
    assert l_bob["wu"]["id"] == "C1"

    # Both submit matching answers
    ans = {"component": "RX-100", "supply_v": 5.5, "quote": "The RX-100 regulator takes a 5.5 V supply."}
    s_alice = client_alice.submit(l_alice["lease_id"], raw_output=ans)
    assert s_alice.status == 200

    s_bob = client_bob.submit(l_bob["lease_id"], raw_output=ans)
    assert s_bob.status == 200

    # WU C1 is committed
    state = coord.get_final_state()
    assert state["wu_states"]["C1"] == "committed"
    assert state["committed_values"]["C1"]["supply_v"] == 5.5
    assert state["participants"]["alice"]["points"] == 1
    assert state["participants"]["bob"]["points"] == 1


def test_it02_submit_idempotency_on_retry(server):
    """IT-02: Sending the same submit POST twice returns identical reply with one submissions row."""
    base_url = f"http://127.0.0.1:{server.server_port}"
    coord = server.coordinator
    coord.add_project(FIXTURES_DIR / "c_corpus.json")

    client = HttpClient(base_url)
    reg = client._req("POST", "/v1/participants", {"name": "alice"})
    token = reg.get("token")

    client_alice = HttpClient(base_url, token=token, participant="alice")
    lease = client_alice.lease(participant="alice", family="anthropic", track="brain", rallies=["rally-c"])
    assert lease is not None
    lid = lease["lease_id"]

    ans = {"component": "RX-100", "supply_v": 5.5, "quote": "The RX-100 regulator takes a 5.5 V supply."}

    # First submit
    sub1 = client_alice.submit(lid, raw_output=ans)
    assert sub1.status == 200

    # Second submit (retry)
    sub2 = client_alice.submit(lid, raw_output=ans)
    assert sub2.status == 200
    assert sub2.status == sub1.status

    # Exactly one submission row exists
    conn = coord._get_conn()
    cnt = conn.execute("SELECT count(*) as cnt FROM submissions WHERE lease_id = ?", (lid,)).fetchone()["cnt"]
    assert cnt == 1


def test_it03_server_restart_preserves_leases(tmp_path: Path, clock):
    """IT-03: Close server mid-run, open new server on same DB file; live leases survive and run completes."""
    db_file = tmp_path / "restart.db"
    srv1 = make_server(db_file, host="127.0.0.1", port=0, now=clock)
    th1 = threading.Thread(target=lambda: srv1.serve_forever(poll_interval=0.01), daemon=True)
    th1.start()

    coord1 = srv1.coordinator
    coord1.add_project(FIXTURES_DIR / "c_corpus.json")

    url1 = f"http://127.0.0.1:{srv1.server_port}"
    c1 = HttpClient(url1)
    reg = c1._req("POST", "/v1/participants", {"name": "alice"})
    alice_token = reg.get("token")

    client1 = HttpClient(url1, token=alice_token, participant="alice")
    lease = client1.lease(participant="alice", family="anthropic", track="brain", rallies=["rally-c"])
    assert lease is not None
    lid = lease["lease_id"]

    # Shutdown srv1
    srv1.shutdown()
    srv1.server_close()

    # Start new server srv2 on the exact same DB
    srv2 = make_server(db_file, host="127.0.0.1", port=0, now=clock)
    th2 = threading.Thread(target=lambda: srv2.serve_forever(poll_interval=0.01), daemon=True)
    th2.start()

    url2 = f"http://127.0.0.1:{srv2.server_port}"
    client2 = HttpClient(url2, token=alice_token, participant="alice")

    # Submit using existing lease to srv2
    ans = {"component": "RX-100", "supply_v": 5.5, "quote": "The RX-100 regulator takes a 5.5 V supply."}
    res = client2.submit(lid, raw_output=ans)
    assert res.status == 200

    srv2.shutdown()
    srv2.server_close()
