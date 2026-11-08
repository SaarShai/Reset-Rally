"""End-to-end 5-participant mock rally test suite (E-01 through E-12)."""

import json
from pathlib import Path
import pytest

from .invariants import check_all
from .rally_script import run

EXPECTED = json.loads((Path(__file__).parents[1] / "fixtures/rally/expected_final.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_e01_rally_end_state_and_invariants(seed, server, clock, shims):
    """E-01: Seeds 0, 1, 2 finish cleanly with invariants verified on every tick and match expected_final.json."""
    result = run(server, clock, seed=seed, mode="inproc", after_tick=check_all)
    assert result.final_state() == EXPECTED


@pytest.fixture(scope="module")
def rally(tmp_path_factory):
    """Module-scoped shared seed-0 rally run for granular assertions."""
    tmp = tmp_path_factory.mktemp("rally")
    return run.fresh(tmp, seed=0, mode="inproc")


def test_e02_alice_drains_then_stops_on_quota(rally):
    """E-02: Alice: 0 lease calls in P0; 4 submits; 1 release; stopped:quota; 0 held leases."""
    a = rally.participant("alice")
    assert a.lease_calls_before("18:00") == 0
    assert (a.submits, a.releases, a.state, a.held_leases) == (4, 1, "stopped:quota", 0)


def test_e03_tiebreak_goes_to_third_family(rally):
    """E-03: C3 votes differ between anthropic and openai; Carol (google, trust 0.6) resolves tiebreak = 3.3."""
    c3 = rally.wu("C3")
    assert {v.family for v in c3.votes} == {"anthropic", "openai", "google"}
    assert c3.tiebreaker is not None
    assert c3.tiebreaker.participant == "carol"
    assert c3.value == 3.3


def test_e04_eve_dropout_loses_no_work(rally):
    """E-04: Eve A1 lease expires at 18:15; stale submit gets 409; Carol commits A1."""
    a1 = rally.wu("A1")
    assert a1.leases[0].participant == "eve"
    assert a1.leases[0].state == "expired"
    assert rally.response("eve", "submit_stale").status == 409
    assert a1.committed_by == "carol"


def test_e05_eve_gate1_then_canary_quarantine(rally):
    """E-05: Eve receives gate1:json (422), gate1:quote_span (422), and is quarantined upon failing K1."""
    codes = [r.code for r in rally.responses("eve") if r.status in (403, 409, 422)]
    assert codes[:4] == ["lease_expired", "gate1:json", "gate1:quote_span", "quarantined"]


def test_e06_canary_catch_rate(rally):
    """E-06: 100% Canary catch rate (1/1 spoofed caught) and 0% false fail rate (3/3 honest pass)."""
    assert rally.canary_stats() == {"spoofed": 1, "caught": 1, "honest": 3, "false_fail": 0}


def test_e07_dave_relay_verification_and_a4_recovery(rally):
    """E-07: Dave verifies all relay WUs; VA4a = fail; A4 recovers and commits with Bob's dt = 0.1."""
    st = rally.final_state()
    assert st["wu_states"]["VA4a"] == "committed"
    assert st["wu_states"]["VA4b"] == "committed"
    assert st["committed_values"]["A4"]["dt"] == 0.1


def test_e08_holdout_disqualifies_p_bob_1(rally):
    """E-08: P-bob-1 disqualified by holdout; best is P-bob-2; holdout cases never served."""
    st = rally.final_state()
    assert st["counters"]["holdout_disqualifications"] == 1
    assert st["committed_values"]["B_best"] == "P-bob-2"
    assert not rally.payload_log_contains_any(rally.holdout_values())


def test_e09_rally_points_distribution(rally):
    """E-09: Exact points match expected ledger allocation; sum is 22."""
    assert rally.points() == {"alice": 4, "bob": 5, "carol": 5, "dave": 8, "eve": 0}
    assert sum(rally.points().values()) == 22


def test_e10_no_credential_reaches_coordinator(rally):
    """E-10: Zero credentials - SENTINEL string never appears in request log."""
    assert "SENTINEL" not in rally.request_log_text()


def test_e11_drain_reaches_quiet_on_time(rally):
    """E-11: Simulation drain completes with zero open and zero leased WUs."""
    st = rally.final_state()
    assert st["open_count"] == 0
    assert st["leased_count"] == 0
    assert st["failed_count"] == 0


def test_e12_subprocess_mode_replays_inproc(tmp_path, rally):
    """E-12: Subprocess mode and in-process mode produce identical event-log SHA-256 for seed 0."""
    sub = run.fresh(tmp_path, seed=0, mode="subprocess")
    assert sub.event_log_sha256() == rally.event_log_sha256()
