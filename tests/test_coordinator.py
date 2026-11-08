"""Coordinator behavior through its public methods, with a FakeClock and a file database."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing

import pytest

from conftest import (DROP, QUOTE, TEXT, FakeClock, add_people, make_canaries, make_det_manifest, make_manifest,
                      make_output, make_units, offer)
from rally.checks import ManifestError
from rally.server import ApiError, Coordinator, load_project_dir, main

FAM = {"tst-w1": "anthropic", "tst-w2": "openai", "tst-w3": "google", "tst-w4": "deepseek"}
PTS = make_manifest()["stages"]["brain"]["points"]
TTL = make_manifest()["stages"]["brain"]["lease_ttl_s"]
SECRET_TEXT = "The hidden sample shows alpha at 1.5 units."  # canary payload, so a test can tell it apart
PENDING = (200, {"status": "pending", "points": 0})


def setup(coord, manifest=None, units=1, canaries=(), workers=4):
    people = add_people(coord, workers)
    m = manifest or make_manifest()
    coord.add_project(m, make_units(units), list(canaries))
    return people, {m["id"]: coord.project(m["id"])["manifest_hash"]}


def lease(coord, people, who, pins, family=None, **kw):
    return coord.lease(people[who]["id"], pins, [offer(family or FAM[who])], **kw)


def submit(coord, people, who, got, output=None):
    return coord.submit(people[who]["id"], got["lease_id"], make_output() if output is None else output)


def api_error(fn, *args):
    with pytest.raises(ApiError) as e:
        fn(*args)
    return e.value.code, e.value.error


def export_rows(coord, people, project_id):
    return {r["wu"]: r for r in coord.export(people["tst-owner"]["id"], project_id)}


# ---------- participants and projects ----------


def test_participants_tokens_and_invites(coord):
    token = coord.add_participant("tst-p1")
    me = coord.auth(token)
    assert (me["name"], me["role"], me["status"]) == ("tst-p1", "worker", "active")
    assert coord.auth("not-a-token") is None and coord.auth(None) is None
    assert api_error(coord.add_participant, "tst-p1") == (409, "name_taken")
    code = coord.create_invite()
    assert api_error(coord.register, "tst-p1", code) == (409, "name_taken")  # a taken name does not use up the invite
    got = coord.register("tst-p2", code)
    assert coord.auth(got["token"])["id"] == got["id"]
    assert api_error(coord.register, "tst-p3", code) == (403, "bad_invite")
    assert api_error(coord.register, "tst-p3", "made-up") == (403, "bad_invite")


def test_add_project_rules(coord):
    add_people(coord)
    with pytest.raises(ManifestError, match="^canaries"):
        coord.add_project(make_manifest(canary={"rate": 0.1}), make_units(1), [])
    with pytest.raises(ManifestError, match="^owner"):
        coord.add_project(make_manifest(owner="tst-nobody"), make_units(1), [])
    with pytest.raises(ManifestError, match="^owner"):
        coord.add_project(make_manifest(owner="tst-w1"), make_units(1), [])
    with pytest.raises(ManifestError, match="^track"):
        coord.add_project(make_manifest(track="relay"), make_units(1), [])
    with pytest.raises(ManifestError, match=r"^units\[0\]"):
        coord.add_project(make_manifest(), [{"payload": "not an object"}], [])
    assert coord.add_project(make_manifest(), make_units(2), []) == "tst-quorum"
    first = coord.project("tst-quorum")["manifest_hash"]
    with pytest.raises(ManifestError, match="^version"):
        coord.add_project(make_manifest(title="same version"), make_units(1), [])
    coord.add_project(make_manifest(version=2, title="new title"), make_units(5), [])
    view = coord.project("tst-quorum")
    assert view["version"] == 2 and view["title"] == "new title" and view["manifest_hash"] != first
    assert coord.projects()[0]["progress"] == {"open": 2}  # an upgrade adds no units
    assert api_error(coord.project, "tst-missing") == (404, "unknown_project")


def test_projects_list_shows_active_projects_without_canaries(coord):
    add_people(coord)
    coord.add_project(make_manifest(canary={"rate": 0.5}), make_units(3), make_canaries(2))
    coord.add_project(make_det_manifest(), make_units(1), [])
    listed = {p["id"]: p for p in coord.projects()}
    assert set(listed) == {"tst-quorum", "tst-det"}
    q = listed["tst-quorum"]
    assert q["progress"] == {"open": 3} and q["track"] == "brain" and q["version"] == 1
    assert q["manifest_hash"] == coord.project("tst-quorum")["manifest_hash"]
    assert "canaries" not in coord.project("tst-quorum")
    coord.set_project_status("tst-det", "paused")
    assert [p["id"] for p in coord.projects()] == ["tst-quorum"]
    assert api_error(coord.set_project_status, "tst-det", "gone") == (400, "bad_request")
    assert api_error(coord.set_project_status, "tst-missing", "closed") == (404, "unknown_project")


# ---------- lease routing ----------


def test_slot_rule_fills_replicas_then_returns_none(coord):
    people, pins = setup(coord, units=1)
    a, b = lease(coord, people, "tst-w1", pins), lease(coord, people, "tst-w2", pins)
    assert a["wu"]["id"] == b["wu"]["id"]
    assert lease(coord, people, "tst-w3", pins) is None  # replicas 2 are taken


def test_lease_once_never_gives_a_participant_the_same_wu_twice(coord):
    people, pins = setup(coord, units=2)
    a = lease(coord, people, "tst-w1", pins)
    coord.release(people["tst-w1"]["id"], a["lease_id"], "error")
    b = lease(coord, people, "tst-w1", pins)
    assert a["wu"]["id"] != b["wu"]["id"]
    coord.release(people["tst-w1"]["id"], b["lease_id"], "error")
    assert lease(coord, people, "tst-w1", pins) is None
    assert lease(coord, people, "tst-w2", pins)["wu"]["id"] in {a["wu"]["id"], b["wu"]["id"]}


def test_two_same_family_workers_never_fill_one_quorum_wu(coord):
    people, pins = setup(coord, units=1)
    a = lease(coord, people, "tst-w1", pins)
    assert lease(coord, people, "tst-w2", pins, family="anthropic") is None
    submit(coord, people, "tst-w1", a)
    assert lease(coord, people, "tst-w2", pins, family="anthropic") is None  # a pending vote keeps its family
    got = coord.lease(people["tst-w3"]["id"], pins, [offer("anthropic"), offer("openai", adapter="codex", model="m1")])
    assert (got["adapter"], got["wu"]["id"]) == ("codex", a["wu"]["id"])
    assert submit(coord, people, "tst-w3", got) == (200, {"status": "accepted", "points": PTS})
    sub = export_rows(coord, people, "tst-quorum")[a["wu"]["id"]]["submissions"][1]
    assert (sub["participant"], sub["family"], sub["model"]) == ("tst-w3", "openai", "m1")


def test_same_family_may_fill_a_wu_when_distinct_families_is_off(coord):
    people, pins = setup(coord, make_manifest(verify__quorum__distinct_families=False), units=1)
    a = lease(coord, people, "tst-w1", pins)
    b = lease(coord, people, "tst-w2", pins, family="anthropic")
    assert a["wu"]["id"] == b["wu"]["id"]
    assert submit(coord, people, "tst-w1", a) == PENDING
    assert submit(coord, people, "tst-w2", b) == (200, {"status": "accepted", "points": PTS})


def test_offer_must_fit_family_mode_kind_and_max_ttl(coord, clock):
    m = make_det_manifest(stages__brain__needs={"families": ["openai"]})
    people, pins = setup(coord, m, units=2)
    w, timeout = people["tst-w1"]["id"], m["stages"]["brain"]["timeout_s"]
    assert coord.lease(w, pins, [offer("anthropic")]) is None
    assert coord.lease(w, pins, [{**offer("openai"), "modes": ["patch"]}]) is None
    assert coord.lease(w, pins, [{**offer("openai"), "kind": "exec"}]) is None
    assert coord.lease(w, pins, [offer("openai")], max_ttl_s=timeout - 1) is None
    got = coord.lease(w, pins, [offer("anthropic"), offer("openai", adapter="codex")], max_ttl_s=timeout)
    ttl = min(TTL, timeout + 60)
    assert got["adapter"] == "codex"
    assert got["expires_at"] == clock.now() + ttl and got["heartbeat_s"] == min(300, ttl // 3)
    other = lease(coord, people, "tst-w2", pins)  # the second unit: the first is leased
    assert other["expires_at"] == clock.now() + TTL and other["heartbeat_s"] == min(300, TTL // 3)


def test_lease_rejects_bad_requests(coord):
    people, pins = setup(coord)
    w = people["tst-w1"]["id"]
    assert api_error(coord.lease, w, ["tst-quorum"], [offer("openai")]) == (400, "bad_request")
    assert api_error(coord.lease, w, pins, {"adapter": "x"}) == (400, "bad_request")
    assert api_error(coord.lease, w, pins, [{"kind": "llm", "family": 3, "adapter": "x"}]) == (400, "bad_request")
    assert api_error(coord.lease, w, pins, [offer("openai")], "10") == (400, "bad_request")
    assert api_error(coord.lease, people["tst-admin"]["id"], pins, [offer("openai")]) == (403, "role")
    assert coord.lease(people["tst-owner"]["id"], pins, [offer("openai")]) is not None  # owners may work too


def test_lease_skips_projects_whose_pinned_hash_differs(coord):
    people, pins = setup(coord)
    assert lease(coord, people, "tst-w1", {"tst-quorum": "sha256:" + "0" * 64}) is None
    assert lease(coord, people, "tst-w1", {"tst-other": pins["tst-quorum"]}) is None
    assert lease(coord, people, "tst-w1", {}) is None
    coord.add_project(make_manifest(version=2), [], [])
    assert lease(coord, people, "tst-w1", pins) is None
    assert lease(coord, people, "tst-w1", {"tst-quorum": coord.project("tst-quorum")["manifest_hash"]}) is not None


def test_paused_and_closed_projects_give_no_work(coord):
    people, pins = setup(coord, units=2)
    coord.set_project_status("tst-quorum", "paused")
    assert lease(coord, people, "tst-w1", pins) is None
    coord.set_project_status("tst-quorum", "active")
    assert lease(coord, people, "tst-w1", pins) is not None
    coord.set_project_status("tst-quorum", "closed")
    assert lease(coord, people, "tst-w2", pins) is None


def test_lease_prefers_projects_in_request_order(coord):
    people = add_people(coord)
    coord.add_project(make_manifest(id="tst-proj-a"), make_units(2), [])
    top = {"id": "tst-b-top", "payload": {"text": TEXT}, "priority": 5}  # priority never beats request order
    coord.add_project(make_det_manifest(id="tst-proj-b"), [top, *make_units(2)], [])
    ha, hb = (coord.project(p)["manifest_hash"] for p in ("tst-proj-a", "tst-proj-b"))
    assert lease(coord, people, "tst-w2", {"tst-proj-a": ha, "tst-proj-b": hb})["wu"]["project"] == "tst-proj-a"
    assert lease(coord, people, "tst-w1", {"tst-proj-b": hb, "tst-proj-a": ha})["wu"]["project"] == "tst-proj-b"
    got = lease(coord, people, "tst-w3", {"tst-proj-a": ha, "tst-proj-b": hb})
    assert (got["wu"]["project"], got["wu"]["manifest_hash"]) == ("tst-proj-a", ha)


def test_lease_order_priority_then_open_quorums(coord):
    people = add_people(coord)
    units = [{"id": "tst-low", "payload": {"text": TEXT}},
             {"id": "tst-high", "payload": {"text": "Top sample: alpha at 1.5 units."}, "priority": 5},
             {"id": "tst-mid", "payload": {"text": TEXT}}]
    coord.add_project(make_manifest(), units, [])
    pins = {"tst-quorum": coord.project("tst-quorum")["manifest_hash"]}
    w1 = people["tst-w1"]["id"]
    first = lease(coord, people, "tst-w1", pins)
    assert "Top sample" in first["wu"]["prompt"]
    coord.release(w1, first["lease_id"], "error")
    low = lease(coord, people, "tst-w1", pins)  # ties go by unit file order
    coord.release(w1, low["lease_id"], "error")
    voted = lease(coord, people, "tst-w1", pins)  # tst-mid: without the vote it sorts last
    assert len({first["wu"]["id"], low["wu"]["id"], voted["wu"]["id"]}) == 3
    submit(coord, people, "tst-w1", voted)
    top = lease(coord, people, "tst-w2", pins)
    assert top["wu"]["id"] == first["wu"]["id"]  # priority beats an open quorum
    submit(coord, people, "tst-w2", top)
    assert lease(coord, people, "tst-w2", pins)["wu"]["id"] == voted["wu"]["id"]  # then WUs that have a vote


def test_ties_are_leased_in_unit_file_order(coord):
    people, pins = setup(coord, make_det_manifest(), units=8)
    leased = []
    while got := lease(coord, people, "tst-w1", pins):  # the random WU id never sets the order
        submit(coord, people, "tst-w1", got)
        leased.append(got["wu"]["id"])
    refs = {r["wu"]: r["ref"] for r in coord.export(people["tst-owner"]["id"], "tst-det")}
    assert [refs[i] for i in leased] == [u["id"] for u in make_units(8)]


def test_many_wus_with_a_family_vote_do_not_starve_that_family(coord):
    people, pins = setup(coord, units=52)
    voted = set()
    for _ in range(51):  # more than 50 open WUs that hold an anthropic vote; they sort before the free WU
        got = lease(coord, people, "tst-w1", pins)
        submit(coord, people, "tst-w1", got)
        voted.add(got["wu"]["id"])
    free = lease(coord, people, "tst-w2", pins, family="anthropic")
    assert free is not None and free["wu"]["id"] not in voted
    assert lease(coord, people, "tst-w3", pins, family="anthropic") is None


def test_lease_reply_has_the_contract_shape(coord):
    people, pins = setup(coord, units=1)
    got = lease(coord, people, "tst-w1", pins)
    assert set(got) == {"lease_id", "expires_at", "heartbeat_s", "adapter", "wu"}
    wu, stage = got["wu"], make_manifest()["stages"]["brain"]
    assert re.fullmatch("[0-9a-f]{16}", wu["id"]) and re.fullmatch("[0-9a-f]{16}", got["lease_id"])
    assert (wu["stage"], wu["mode"], wu["schema"], wu["timeout_s"]) == ("brain", "json", stage["schema"], stage["timeout_s"])
    assert (wu["workspace"], wu["image"], wu["command"], wu["files"]) == (None, None, None, {})
    assert TEXT in wu["prompt"] and wu["prompt"].endswith(json.dumps(stage["schema"], sort_keys=True))
    assert "tst-u1" not in json.dumps(got)  # the owner's ref never reaches a worker
    assert coord.me(people["tst-w1"]["id"])["active_leases"] == [got["lease_id"]]


# ---------- expiry, heartbeat, release, ownership ----------


def test_lease_at_expires_at_is_still_active_and_expires_one_second_later(coord, clock):
    people, pins = setup(coord, make_det_manifest(), units=1)
    a = lease(coord, people, "tst-w1", pins)
    clock.advance(a["expires_at"] - clock.now())
    assert lease(coord, people, "tst-w2", pins) is None
    clock.advance(1)
    assert lease(coord, people, "tst-w2", pins)["wu"]["id"] == a["wu"]["id"]


def test_late_submit_is_accepted_while_the_slot_is_free(coord, clock):
    people, pins = setup(coord, make_det_manifest(), units=1)
    a = lease(coord, people, "tst-w1", pins)
    clock.advance(TTL + 1)  # past expires_at, never marked expired
    assert submit(coord, people, "tst-w1", a) == (200, {"status": "accepted", "points": PTS})


def test_late_submit_on_an_expired_lease_takes_a_free_quorum_slot(coord, clock):
    people, pins = setup(coord, units=1)
    a = lease(coord, people, "tst-w1", pins)
    clock.advance(TTL + 1)
    assert lease(coord, people, "tst-w2", pins) is not None  # this marks w1's lease expired
    assert submit(coord, people, "tst-w1", a) == PENDING


def test_a_lease_past_expires_at_never_holds_a_slot_against_a_late_submit(coord, clock):
    people, pins = setup(coord, make_det_manifest(), units=1)
    a = lease(coord, people, "tst-w1", pins)
    clock.advance(TTL + 1)
    b = lease(coord, people, "tst-w2", pins)  # this marks a expired
    clock.advance(TTL + 1)  # b is past expires_at, still marked active: no lease call ran
    assert submit(coord, people, "tst-w1", a) == (200, {"status": "accepted", "points": PTS})
    assert api_error(submit, coord, people, "tst-w2", b) == (409, "lease_expired")


def test_late_submit_is_409_when_the_slot_was_taken(coord, clock):
    people, pins = setup(coord, make_det_manifest(), units=1)
    a = lease(coord, people, "tst-w1", pins)
    clock.advance(TTL + 1)
    b = lease(coord, people, "tst-w2", pins)
    assert api_error(submit, coord, people, "tst-w1", a) == (409, "lease_expired")
    assert submit(coord, people, "tst-w2", b) == (200, {"status": "accepted", "points": PTS})


def test_a_lease_at_expires_at_still_holds_its_slot_against_a_late_submit(coord, clock):
    people, pins = setup(coord, units=1)
    a, b = lease(coord, people, "tst-w1", pins), lease(coord, people, "tst-w2", pins)
    assert submit(coord, people, "tst-w2", b) == PENDING
    clock.advance(TTL + 1)
    c = lease(coord, people, "tst-w3", pins)  # takes a's slot
    clock.advance(c["expires_at"] - clock.now())  # c's lease is live up to and at expires_at
    assert api_error(submit, coord, people, "tst-w1", a) == (409, "lease_expired")
    assert submit(coord, people, "tst-w3", c) == (200, {"status": "accepted", "points": PTS})


def test_a_closed_wu_never_locks_its_leaseholder_out_of_the_project(coord):
    people, pins = setup(coord, make_manifest(verify__max_attempts=1), units=2)
    live = lease(coord, people, "tst-w1", pins)
    bad = lease(coord, people, "tst-w2", pins)
    assert bad["wu"]["id"] == live["wu"]["id"]
    assert submit(coord, people, "tst-w2", bad, make_output(quotes=["x"]))[0] == 422
    assert api_error(submit, coord, people, "tst-w1", live) == (409, "wu_closed")
    nxt = lease(coord, people, "tst-w1", pins)  # the closed WU's lease is still active
    assert nxt is not None and nxt["wu"]["id"] != live["wu"]["id"]


def test_a_participant_may_hold_two_leases_in_one_project(coord):
    people, pins = setup(coord, units=2)
    a, b = lease(coord, people, "tst-w1", pins), lease(coord, people, "tst-w1", pins)
    assert a is not None and b is not None and a["wu"]["id"] != b["wu"]["id"]


def test_late_submit_of_a_same_family_vote_is_refused(coord, clock):
    people, pins = setup(coord, units=1)
    a = lease(coord, people, "tst-w1", pins)
    clock.advance(TTL + 1)
    b = lease(coord, people, "tst-w2", pins, family="anthropic")  # w1's lease expires here: its family is free again
    assert b["wu"]["id"] == a["wu"]["id"]
    assert api_error(submit, coord, people, "tst-w1", a) == (409, "lease_expired")  # a slot is free, the family is not
    assert submit(coord, people, "tst-w2", b) == PENDING


def test_late_same_family_vote_goes_on_when_distinct_families_is_off(coord, clock):
    people, pins = setup(coord, make_manifest(verify__quorum__distinct_families=False), units=1)
    a = lease(coord, people, "tst-w1", pins)
    b = lease(coord, people, "tst-w2", pins, family="anthropic")
    assert submit(coord, people, "tst-w2", b) == PENDING
    clock.advance(TTL + 1)
    assert submit(coord, people, "tst-w1", a) == (200, {"status": "accepted", "points": PTS})


def test_release_frees_the_slot_at_once(coord):
    people, pins = setup(coord, make_det_manifest(), units=1)
    a = lease(coord, people, "tst-w1", pins)
    assert lease(coord, people, "tst-w2", pins) is None
    assert coord.release(people["tst-w1"]["id"], a["lease_id"], "rate_limited") == {"ok": True}
    assert lease(coord, people, "tst-w2", pins)["wu"]["id"] == a["wu"]["id"]
    assert api_error(coord.release, people["tst-w1"]["id"], a["lease_id"], "again") == (409, "lease_not_active")
    assert api_error(submit, coord, people, "tst-w1", a) == (409, "lease_not_active")


def test_release_reason_must_be_short_text(coord):
    people, pins = setup(coord, make_det_manifest(), units=1)
    a = lease(coord, people, "tst-w1", pins)
    assert api_error(coord.release, people["tst-w1"]["id"], a["lease_id"], "x" * 65) == (400, "bad_request")
    assert api_error(coord.release, people["tst-w1"]["id"], a["lease_id"], None) == (400, "bad_request")
    assert coord.release(people["tst-w1"]["id"], a["lease_id"], "x" * 64) == {"ok": True}


def test_heartbeat_extends_and_is_409_after_expiry(coord, clock):
    people, pins = setup(coord, make_det_manifest(), units=1)
    w, a = people["tst-w1"]["id"], lease(coord, people, "tst-w1", pins)
    clock.advance(TTL - 100)
    assert coord.heartbeat(w, a["lease_id"]) == {"expires_at": clock.now() + TTL}
    clock.advance(TTL)  # exactly at expires_at
    assert coord.heartbeat(w, a["lease_id"]) == {"expires_at": clock.now() + TTL}
    clock.advance(TTL + 1)
    assert api_error(coord.heartbeat, w, a["lease_id"]) == (409, "lease_not_active")
    assert api_error(coord.heartbeat, w, a["lease_id"]) == (409, "lease_not_active")
    assert coord.me(w)["active_leases"] == []
    assert lease(coord, people, "tst-w2", pins) is not None


def test_lease_calls_by_another_participant_are_404(coord):
    people, pins = setup(coord, make_det_manifest(), units=1)
    a, other = lease(coord, people, "tst-w1", pins), people["tst-w2"]["id"]
    assert api_error(coord.heartbeat, other, a["lease_id"]) == (404, "unknown_lease")
    assert api_error(coord.release, other, a["lease_id"], "stop") == (404, "unknown_lease")
    assert api_error(coord.submit, other, a["lease_id"], make_output()) == (404, "unknown_lease")
    assert api_error(coord.submit, people["tst-w1"]["id"], "0" * 16, make_output()) == (404, "unknown_lease")
    assert submit(coord, people, "tst-w1", a) == (200, {"status": "accepted", "points": PTS})


# ---------- submit ----------


def test_replayed_submit_returns_the_first_reply_after_the_wu_is_done(coord):
    people, pins = setup(coord, units=1)
    a, b = lease(coord, people, "tst-w1", pins), lease(coord, people, "tst-w2", pins)
    assert submit(coord, people, "tst-w1", a) == PENDING
    assert submit(coord, people, "tst-w2", b) == (200, {"status": "accepted", "points": PTS})
    assert submit(coord, people, "tst-w1", a, make_output(kind="beta")) == PENDING  # the stored first reply
    row = export_rows(coord, people, "tst-quorum")[a["wu"]["id"]]
    assert row["status"] == "done" and [s["status"] for s in row["submissions"]] == ["accepted", "accepted"]


def test_submit_twice_returns_the_first_reply(coord):
    people, pins = setup(coord, make_det_manifest(), units=2)
    a = lease(coord, people, "tst-w1", pins)
    first = submit(coord, people, "tst-w1", a)
    assert submit(coord, people, "tst-w1", a, make_output(kind="beta")) == first
    assert coord.me(people["tst-w1"]["id"])["points"] == PTS
    b = lease(coord, people, "tst-w2", pins)
    bad = submit(coord, people, "tst-w2", b, make_output(quotes=["not in the text"]))
    assert bad[0] == 422 and submit(coord, people, "tst-w2", b) == bad


SHA_BETA = hashlib.sha256(b"beta").hexdigest()
CASES = [  # (extra checks, bad output, reason, good output)
    ([], {**make_output(), "pad": "x" * 262144}, "size", make_output()),
    ([], [make_output()], "schema: $: not an object", make_output()),
    ([], {"kind": "alpha", "quotes": [QUOTE]}, "schema: $.value: required", make_output()),
    ([], make_output(kind="gamma"), "schema: $.kind: not in enum", make_output()),
    ([], make_output(quotes=["alpha at 9 units"]), "check:quote_span:quotes", make_output()),
    ([{"type": "regex", "field": "kind", "pattern": "beta|unclear"}], make_output(), "check:regex:kind",
     make_output(kind="beta")),
    ([{"type": "exact", "field": "kind", "value": "Beta "}], make_output(), "check:exact:kind", make_output(kind="beta")),
    ([{"type": "numeric", "field": "value", "min": 2, "max": 3}], make_output(), "check:numeric:value",
     make_output(value=2.5)),
    ([{"type": "hash", "field": "kind", "sha256": SHA_BETA}], make_output(), "check:hash:kind", make_output(kind="beta")),
    ([{"type": "regex", "field": "note", "pattern": ".*"}], make_output(), "check:regex:note",
     {**make_output(), "note": "x"}),
]


@pytest.mark.parametrize("checks,bad,reason,good", CASES, ids=[c[2] for c in CASES])
def test_bad_outputs_get_422_with_their_reason(coord, checks, bad, reason, good):
    m = make_det_manifest()
    m["verify"]["checks"] += checks
    people, pins = setup(coord, m, units=1)
    a = lease(coord, people, "tst-w1", pins)
    assert submit(coord, people, "tst-w1", a, bad) == (422, {"status": "rejected", "reason": reason, "points": 0})
    b = lease(coord, people, "tst-w2", pins)
    assert submit(coord, people, "tst-w2", b, good) == (200, {"status": "accepted", "points": PTS})


def test_output_of_exactly_max_output_is_not_too_large(coord):
    from rally.server import MAX_OUTPUT
    people, pins = setup(coord, units=1)
    out = make_output()
    out["pad"] = ""
    out["pad"] = "x" * (MAX_OUTPUT - len(json.dumps(out)))
    assert len(json.dumps(out)) == MAX_OUTPUT
    assert submit(coord, people, "tst-w1", lease(coord, people, "tst-w1", pins), out) == PENDING
    out["pad"] += "x"
    got = lease(coord, people, "tst-w2", pins)
    assert submit(coord, people, "tst-w2", got, out)[1]["reason"] == "size"


def test_wu_fails_after_three_rejected_attempts(coord):
    people, pins = setup(coord, make_det_manifest(), units=1)
    bad = make_output(quotes=["tst-missing"])
    for who in ("tst-w1", "tst-w2"):
        assert submit(coord, people, who, lease(coord, people, who, pins), bad)[0] == 422
    assert coord.projects()[0]["progress"] == {"open": 1}
    assert submit(coord, people, "tst-w3", lease(coord, people, "tst-w3", pins), bad)[0] == 422
    assert coord.projects()[0]["progress"] == {"failed": 1}
    assert lease(coord, people, "tst-w4", pins) is None
    [row] = coord.export(people["tst-owner"]["id"], "tst-det")
    assert (row["status"], row["attempts"], row["result"]) == ("failed", 3, None)
    assert [s["reason"] for s in row["submissions"]] == ["check:quote_span:quotes"] * 3


def test_max_attempts_comes_from_the_manifest(coord):
    people, pins = setup(coord, make_det_manifest(verify__max_attempts=2), units=1)
    bad = make_output(quotes=["tst-missing"])
    assert submit(coord, people, "tst-w1", lease(coord, people, "tst-w1", pins), bad)[0] == 422
    assert coord.projects()[0]["progress"] == {"open": 1}
    assert submit(coord, people, "tst-w2", lease(coord, people, "tst-w2", pins), bad)[0] == 422
    assert coord.projects()[0]["progress"] == {"failed": 1}


def test_deterministic_accept_gives_points_and_finishes_the_wu(coord, clock):
    people, pins = setup(coord, make_det_manifest(), units=1)
    a = lease(coord, people, "tst-w1", pins)
    out = make_output()
    assert coord.submit(people["tst-w1"]["id"], a["lease_id"], out, {"seconds": 4}) == (
        200, {"status": "accepted", "points": PTS})
    me = coord.me(people["tst-w1"]["id"])
    assert me == {"name": "tst-w1", "role": "worker", "status": "active", "points": PTS, "active_leases": []}
    assert coord.projects()[0]["progress"] == {"done": 1}
    [row] = coord.export(people["tst-owner"]["id"], "tst-det")
    assert row == {"wu": a["wu"]["id"], "ref": "tst-u1", "status": "done", "payload": {"text": TEXT}, "result": out,
                   "attempts": 0, "releases": {},
                   "submissions": [{"participant": "tst-w1", "family": "anthropic", "model": None, "status": "accepted",
                                    "reason": None, "points": PTS, "output": out}]}
    assert coord.leaderboard() == [{"name": "tst-w1", "points": PTS}]


# ---------- quorum ----------


def test_quorum_two_families_agree(coord):
    people, pins = setup(coord, units=1)
    a, b = lease(coord, people, "tst-w1", pins), lease(coord, people, "tst-w2", pins)
    first = make_output()
    assert submit(coord, people, "tst-w1", a, first) == PENDING
    assert coord.projects()[0]["progress"] == {"open": 1}
    near = make_output(value=1.5004, quotes=["  alpha at 1.5   units"])  # same key after normalize at 3 digits
    assert submit(coord, people, "tst-w2", b, near) == (200, {"status": "accepted", "points": PTS})
    row = export_rows(coord, people, "tst-quorum")[a["wu"]["id"]]
    assert row["status"] == "done" and row["result"] == first
    assert [s["status"] for s in row["submissions"]] == ["accepted", "accepted"]


def test_conflict_adds_a_replica_that_only_a_third_family_can_fill(coord):
    people, pins = setup(coord, units=1)
    a, b = lease(coord, people, "tst-w1", pins), lease(coord, people, "tst-w2", pins)
    assert submit(coord, people, "tst-w1", a, make_output(value=1.5)) == PENDING
    assert submit(coord, people, "tst-w2", b, make_output(value=2.5)) == PENDING
    assert coord.projects()[0]["progress"] == {"open": 1}
    assert lease(coord, people, "tst-w3", pins, family="anthropic") is None
    assert lease(coord, people, "tst-w3", pins, family="openai") is None
    c = lease(coord, people, "tst-w3", pins, family="google")
    assert c["wu"]["id"] == a["wu"]["id"]
    assert lease(coord, people, "tst-w4", pins) is None  # the third replica is the last slot
    assert submit(coord, people, "tst-w3", c, make_output(value=1.5)) == (200, {"status": "accepted", "points": PTS})
    row = export_rows(coord, people, "tst-quorum")[a["wu"]["id"]]
    assert row["status"] == "done" and row["result"]["value"] == 1.5
    assert [(s["participant"], s["status"], s["reason"], s["points"]) for s in row["submissions"]] == [
        ("tst-w1", "accepted", None, PTS), ("tst-w2", "rejected", "quorum_lost", 0), ("tst-w3", "accepted", None, PTS)]
    assert coord.leaderboard("tst-quorum") == [{"name": "tst-w1", "points": PTS}, {"name": "tst-w3", "points": PTS},
                                                {"name": "tst-w2", "points": 0}]


def test_three_way_conflict_is_disputed_with_no_points(coord):
    people, pins = setup(coord, units=1)
    a, b = lease(coord, people, "tst-w1", pins), lease(coord, people, "tst-w2", pins)
    submit(coord, people, "tst-w1", a, make_output(value=1.5))
    submit(coord, people, "tst-w2", b, make_output(value=2.5))
    c = lease(coord, people, "tst-w3", pins)
    disputed = (200, {"status": "rejected", "reason": "disputed", "points": 0})
    assert submit(coord, people, "tst-w3", c, make_output(value=3.5)) == disputed
    row = export_rows(coord, people, "tst-quorum")[a["wu"]["id"]]
    assert row["status"] == "disputed" and row["result"] is None
    assert {(s["status"], s["reason"], s["points"]) for s in row["submissions"]} == {("rejected", "disputed", 0)}
    assert lease(coord, people, "tst-w4", pins) is None
    assert sum(r["points"] for r in coord.leaderboard()) == 0


def test_forced_db_state_same_family_agreement_does_not_commit(coord, db_path):
    people, pins = setup(coord, units=1)
    a, b = lease(coord, people, "tst-w1", pins), lease(coord, people, "tst-w2", pins)
    with closing(sqlite3.connect(db_path)) as db, db:  # routing would never allow this: force it
        db.execute("UPDATE leases SET family = 'anthropic' WHERE id = ?", (b["lease_id"],))
    assert submit(coord, people, "tst-w1", a) == PENDING
    assert submit(coord, people, "tst-w2", b) == PENDING  # two anthropic votes are not a quorum
    c = lease(coord, people, "tst-w3", pins)
    assert c["wu"]["id"] == a["wu"]["id"]
    assert submit(coord, people, "tst-w3", c) == (200, {"status": "accepted", "points": PTS})


# ---------- canaries and quarantine ----------


def canary_coord(db_path, clock, chosen, quarantine_after=2, manifest=None, units=2, canaries=None):
    calls = []

    def pick(name, project_id, rate):
        calls.append((name, project_id, rate))
        return name in chosen

    coord = Coordinator(db_path, now=clock.now, want_canary=pick)
    people = add_people(coord)
    m = manifest or make_manifest(canary={"rate": 0.25, "quarantine_after": quarantine_after})
    coord.add_project(m, make_units(units), canaries or make_canaries(3, text=SECRET_TEXT))
    return coord, people, {m["id"]: coord.project(m["id"])["manifest_hash"]}, calls


def canary_counts(db_path):
    with closing(sqlite3.connect(db_path)) as db:  # read only: the counters are not in the API
        rows = db.execute("SELECT name, canary_pass, canary_fail FROM participants WHERE canary_pass + canary_fail > 0")
        return {n: (p, f) for n, p, f in rows}


def masked(lease):
    """A lease without the values that differ for every WU: what is left must be equal for a canary."""
    return {**lease, "lease_id": None, "wu": {**lease["wu"], "id": None, "prompt": None}}


def shape(x):
    if isinstance(x, dict):
        return {k: shape(v) for k, v in x.items()}
    return type(x).__name__


def numbered_canaries(n):
    """Canaries that carry SECRET_TEXT and differ by their text, so a test can tell one canary from another."""
    return [{"payload": {"text": f"{SECRET_TEXT} Sample {i}."}, "answer": {"kind": "alpha", "value": 1.5}}
            for i in range(n)]


def test_canary_is_chosen_only_when_want_canary_says_so(db_path, clock):
    coord, people, pins, calls = canary_coord(db_path, clock, chosen={"tst-w2"}, canaries=numbered_canaries(3))
    a = lease(coord, people, "tst-w1", pins)
    assert SECRET_TEXT not in a["wu"]["prompt"] and calls == [("tst-w1", "tst-quorum", 0.25)]
    b = lease(coord, people, "tst-w2", pins)
    assert SECRET_TEXT in b["wu"]["prompt"]
    submit(coord, people, "tst-w2", b)
    b2 = lease(coord, people, "tst-w2", pins)  # the next canary: never the same one twice
    assert SECRET_TEXT in b2["wu"]["prompt"] and b2["wu"]["prompt"] != b["wu"]["prompt"]
    coord.add_project(make_det_manifest(), make_units(1), [])
    lease(coord, people, "tst-w3", {"tst-det": coord.project("tst-det")["manifest_hash"]})
    assert len(calls) == 3  # a project with rate 0 never asks


def test_canary_lease_and_reply_look_like_a_normal_wu(db_path, clock):
    coord, people, pins, _ = canary_coord(db_path, clock, chosen={"tst-w2", "tst-w3"})
    normal, good, bad = lease(coord, people, "tst-w1", pins), lease(coord, people, "tst-w2", pins), lease(coord, people, "tst-w3", pins)
    assert SECRET_TEXT in good["wu"]["prompt"] and SECRET_TEXT in bad["wu"]["prompt"]
    assert shape(good) == shape(normal) == shape(bad)
    assert masked(good) == masked(normal) == masked(bad)
    assert re.fullmatch("[0-9a-f]{16}", good["wu"]["id"])
    assert all("canary" not in json.dumps(x).lower() for x in (normal, good, bad))
    assert submit(coord, people, "tst-w1", normal) == PENDING
    assert submit(coord, people, "tst-w2", good) == PENDING  # graded: pass
    assert submit(coord, people, "tst-w3", bad, make_output(kind="beta")) == PENDING  # graded: fail
    assert submit(coord, people, "tst-w2", good) == PENDING and submit(coord, people, "tst-w3", bad) == PENDING
    me2, me3 = coord.me(people["tst-w2"]["id"]), coord.me(people["tst-w3"]["id"])
    assert (me2["points"], me3["points"], me3["status"]) == (0, 0, "active")  # canaries never pay points
    assert set(me2) == {"name", "role", "status", "points", "active_leases"}
    assert canary_counts(db_path) == {"tst-w2": (1, 0), "tst-w3": (0, 1)}
    assert coord.projects()[0]["progress"] == {"open": 2}  # canaries are not counted


def test_deterministic_canary_reply_matches_a_normal_accept(db_path, clock):
    m = make_det_manifest(canary={"rate": 0.5, "quarantine_after": 2})
    coord, people, pins, _ = canary_coord(db_path, clock, chosen={"tst-w2", "tst-w3"}, manifest=m)
    accepted = (200, {"status": "accepted", "points": PTS})
    assert submit(coord, people, "tst-w1", lease(coord, people, "tst-w1", pins)) == accepted
    assert submit(coord, people, "tst-w2", lease(coord, people, "tst-w2", pins)) == accepted
    assert submit(coord, people, "tst-w3", lease(coord, people, "tst-w3", pins), make_output(value=2.0)) == accepted
    assert [coord.me(people[w]["id"])["points"] for w in ("tst-w1", "tst-w2", "tst-w3")] == [PTS, 0, 0]
    assert canary_counts(db_path) == {"tst-w2": (1, 0), "tst-w3": (0, 1)}  # graded on all answer fields


def test_a_422_on_a_canary_counts_as_a_canary_failure(db_path, clock):
    coord, people, pins, _ = canary_coord(db_path, clock, chosen={"tst-w1"})
    for _ in range(2):
        k = lease(coord, people, "tst-w1", pins)
        assert SECRET_TEXT in k["wu"]["prompt"]
        assert submit(coord, people, "tst-w1", k, make_output(quotes=["not in the text"])) == (
            422, {"status": "rejected", "reason": "check:quote_span:quotes", "points": 0})
    assert coord.me(people["tst-w1"]["id"])["status"] == "quarantined"


def test_same_family_workers_get_the_same_canary_and_may_submit_late(db_path, clock):
    # three units, so each canary stands in for its own unit W: a late canary goes on while W has a free slot
    # and no family clash, as a late real vote on W would
    coord, people, pins, _ = canary_coord(db_path, clock, chosen={"tst-w1", "tst-w2", "tst-w3"}, units=3,
                                          canaries=numbered_canaries(3))
    got = [lease(coord, people, w, pins, family="anthropic") for w in ("tst-w1", "tst-w2", "tst-w3")]
    assert len({g["wu"]["prompt"] for g in got}) == 1 and SECRET_TEXT in got[0]["wu"]["prompt"]  # the oldest canary
    clock.advance(TTL + 1)
    assert [submit(coord, people, w, g) for w, g in zip(("tst-w1", "tst-w2", "tst-w3"), got)] == [PENDING] * 3


def test_canary_failures_quarantine_and_free_the_slots(db_path, clock):
    chosen = set()
    coord, people, pins, _ = canary_coord(db_path, clock, chosen, units=2)
    coord.add_project(make_det_manifest(), make_units(1), [])
    det = {"tst-det": coord.project("tst-det")["manifest_hash"]}
    w1 = people["tst-w1"]["id"]
    x = lease(coord, people, "tst-w1", pins)
    assert submit(coord, people, "tst-w1", x) == PENDING
    y = lease(coord, people, "tst-w1", det)  # w1's live lease sits in the other project
    x2 = lease(coord, people, "tst-w2", pins)
    assert x2["wu"]["id"] == x["wu"]["id"]  # x is full; the other unit is the real work left for w1's canaries
    assert lease(coord, people, "tst-w3", det) is None  # y is full
    chosen.add("tst-w1")
    for _ in range(2):
        k = lease(coord, people, "tst-w1", pins)
        assert SECRET_TEXT in k["wu"]["prompt"]
        assert submit(coord, people, "tst-w1", k, make_output(kind="unclear", value=None)) == PENDING
    me = coord.me(w1)
    assert (me["status"], me["active_leases"], me["points"]) == ("quarantined", [], 0)
    assert api_error(lease, coord, people, "tst-w1", pins) == (403, "not_active")
    assert api_error(coord.submit, w1, y["lease_id"], make_output()) == (403, "not_active")
    assert api_error(coord.heartbeat, w1, y["lease_id"]) == (403, "not_active")
    assert api_error(coord.release, w1, y["lease_id"], "stop") == (403, "not_active")
    x4, y3 = lease(coord, people, "tst-w4", pins), lease(coord, people, "tst-w3", det)
    assert (x4["wu"]["id"], y3["wu"]["id"]) == (x["wu"]["id"], y["wu"]["id"])  # the vote and the lease are freed
    for pid in ("tst-quorum", "tst-det"):
        assert {r["status"] for r in export_rows(coord, people, pid).values()} == {"open"}  # none finished yet
    submit(coord, people, "tst-w2", x2)
    assert submit(coord, people, "tst-w4", x4) == (200, {"status": "accepted", "points": PTS})
    rows = export_rows(coord, people, "tst-quorum")
    assert [(s["participant"], s["status"], s["reason"]) for s in rows[x["wu"]["id"]]["submissions"]] == [
        ("tst-w1", "rejected", "quarantined"), ("tst-w2", "accepted", None), ("tst-w4", "accepted", None)]
    submit(coord, people, "tst-w3", y3)
    assert export_rows(coord, people, "tst-det")[y["wu"]["id"]]["releases"] == {"quarantined": 1}


def test_points_equal_accepted_submissions_times_stage_points(db_path, clock):
    chosen = set()
    coord, people, pins, _ = canary_coord(db_path, clock, chosen)
    det_pts = 5
    coord.add_project(make_det_manifest(stages__brain__points=det_pts), make_units(2), [])
    det_pins = {"tst-det": coord.project("tst-det")["manifest_hash"]}
    # unit 1: agree (w1, w2); unit 2: conflict, w3 breaks the tie for w1
    for value in (1.5, 2.5):
        submit(coord, people, "tst-w1", lease(coord, people, "tst-w1", pins))
        submit(coord, people, "tst-w2", lease(coord, people, "tst-w2", pins), make_output(value=value))
    chosen.add("tst-w4")
    submit(coord, people, "tst-w4", lease(coord, people, "tst-w4", pins))  # canary pass, in place of the tie-break
    chosen.clear()
    submit(coord, people, "tst-w3", lease(coord, people, "tst-w3", pins))
    assert submit(coord, people, "tst-w4", lease(coord, people, "tst-w4", det_pins), make_output(quotes=["x"]))[0] == 422
    for who in ("tst-w1", "tst-w2"):
        submit(coord, people, who, lease(coord, people, who, det_pins))
    rows = {p: coord.export(people["tst-owner"]["id"], p) for p in ("tst-quorum", "tst-det")}
    stage_pts = {"tst-quorum": PTS, "tst-det": det_pts}
    subs = [(p, s) for p, rs in rows.items() for r in rs for s in r["submissions"]]
    assert all(s["points"] == (stage_pts[p] if s["status"] == "accepted" else 0) for p, s in subs)
    from_export = sum(s["points"] for _, s in subs)
    assert from_export == 4 * PTS + 2 * det_pts  # 2 + 2 quorum winners, 2 deterministic accepts
    board = coord.leaderboard()
    assert sum(r["points"] for r in board) == from_export  # the canary pass pays nothing
    assert {r["name"]: r["points"] for r in board} == {
        w: coord.me(people[w]["id"])["points"] for w in ("tst-w1", "tst-w2", "tst-w3", "tst-w4")}
    assert sum(r["points"] for r in coord.leaderboard("tst-det")) == 2 * det_pts


def test_export_is_for_the_owner_or_an_admin_and_shows_open_wus(coord):
    people, _ = setup(coord, units=1, canaries=make_canaries(1))
    [row] = coord.export(people["tst-owner"]["id"], "tst-quorum")  # the canary is not exported
    assert (row["ref"], row["status"], row["result"], row["submissions"]) == ("tst-u1", "open", None, [])
    assert coord.export(people["tst-admin"]["id"], "tst-quorum") == [row]
    assert api_error(coord.export, people["tst-w1"]["id"], "tst-quorum") == (403, "forbidden")
    assert api_error(coord.export, people["tst-admin"]["id"], "tst-missing") == (404, "unknown_project")


def test_cli_admin_commands_and_project_dir(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    assert main(["add-participant", "tst-boss", "--role", "owner", "--db", db]) == 0
    token = capsys.readouterr().out.strip()
    proj = tmp_path / "proj"
    proj.mkdir()
    m = make_det_manifest(owner="tst-boss", units="units.jsonl", canaries="canaries.jsonl")
    (proj / "rally.json").write_text(json.dumps(m))
    (proj / "units.jsonl").write_text("\n".join(json.dumps(u) for u in make_units(2)) + "\n\n")
    (proj / "canaries.jsonl").write_text(json.dumps(make_canaries(1)[0]) + "\n")
    assert load_project_dir(proj) == (m, make_units(2), make_canaries(1))
    assert main(["add-project", str(proj), "--db", db]) == 0 and capsys.readouterr().out.strip() == "tst-det"
    coord = Coordinator(db)
    assert coord.auth(token)["role"] == "owner"
    assert main(["pause", "tst-det", "--db", db]) == 0 and coord.projects() == []
    assert main(["resume", "tst-det", "--db", db]) == 0 and len(coord.projects()) == 1
    assert main(["add-project", str(proj), "--db", db]) == 1 and "version" in capsys.readouterr().err
    assert main(["close", "tst-missing", "--db", db]) == 1 and "unknown_project" in capsys.readouterr().err
    assert main(["invite", "--db", db]) == 0
    worker = coord.register("tst-p1", capsys.readouterr().out.strip())
    got = coord.lease(worker["id"], {"tst-det": coord.project("tst-det")["manifest_hash"]}, [offer("anthropic")])
    coord.submit(worker["id"], got["lease_id"], make_output())
    assert main(["export", "tst-det", "--db", db]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sorted(r["status"] for r in rows) == ["done", "open"]  # export lists open WUs too
    assert [r["submissions"][0]["participant"] for r in rows if r["status"] == "done"] == ["tst-p1"]
    (proj / "units.jsonl").write_text("{broken\n")
    with pytest.raises(ManifestError, match="^units"):
        load_project_dir(proj)


def test_no_canary_without_real_work_for_this_participant(db_path, clock):
    m = make_det_manifest(canary={"rate": 0.5, "quarantine_after": 2})
    coord, people, pins, calls = canary_coord(db_path, clock, chosen={"tst-w1", "tst-w2"}, manifest=m, units=1)
    timeout = m["stages"]["brain"]["timeout_s"]
    assert coord.lease(people["tst-w1"]["id"], pins, [offer("anthropic")], max_ttl_s=timeout - 1) is None  # no offer fits
    k = lease(coord, people, "tst-w1", pins)
    assert SECRET_TEXT in k["wu"]["prompt"]  # in place of the one unit
    submit(coord, people, "tst-w1", k)  # the canary lease held the unit; its submit frees the unit for w3
    submit(coord, people, "tst-w3", lease(coord, people, "tst-w3", pins))  # the unit is done
    assert coord.projects()[0]["progress"] == {"done": 1}
    assert lease(coord, people, "tst-w2", pins) is None  # a lease now would prove a canary
    assert [c[0] for c in calls] == ["tst-w1", "tst-w3"]  # asked only when real work was found


def test_canaries_come_in_file_order_then_real_work(db_path, clock):
    texts = [f"Hidden sample {i} shows alpha at 1.5 units." for i in range(6)]
    canaries = [{"payload": {"text": t}, "answer": {"kind": "alpha", "value": 1.5}} for t in texts]
    coord, people, pins, _ = canary_coord(db_path, clock, chosen={"tst-w1"}, canaries=canaries)
    for t in texts:  # the oldest canary first; ties go by file order, never by the random WU id
        k = lease(coord, people, "tst-w1", pins)
        assert t in k["wu"]["prompt"]
        submit(coord, people, "tst-w1", k)
    assert TEXT in lease(coord, people, "tst-w1", pins)["wu"]["prompt"]  # every canary was leased


def next_lease_after(tmp_path, chosen, units, then):
    """tst-w1 leases, holds, releases or lets the lease expire, then leases real work: (masked lease, ref) or None."""
    clock, want = FakeClock(), {"tst-w1"} if chosen else set()
    m = make_det_manifest(canary={"rate": 0.5, "quarantine_after": 2})
    coord, people, pins, _ = canary_coord(tmp_path / f"{chosen}.db", clock, want, manifest=m, units=units)
    first = lease(coord, people, "tst-w1", pins)
    assert (SECRET_TEXT in first["wu"]["prompt"]) == chosen
    if then == "release":
        coord.release(people["tst-w1"]["id"], first["lease_id"], "error")
    elif then == "expire":
        clock.advance(TTL + 1)
    want.clear()  # a second canary would hide which unit is left
    nxt = lease(coord, people, "tst-w1", pins)
    return nxt and (masked(nxt), export_rows(coord, people, "tst-det")[nxt["wu"]["id"]]["ref"])


@pytest.mark.parametrize("then", ["hold", "release", "expire"])
@pytest.mark.parametrize("units", [1, 2])
def test_after_a_canary_lease_the_next_lease_is_the_same_as_after_a_real_one(tmp_path, units, then):
    canary, real = (next_lease_after(tmp_path, chosen, units, then) for chosen in (True, False))
    assert canary == real and (real is None) == (units == 1)  # None only when the last real unit was taken


def test_after_a_canary_submit_the_replaced_wu_comes_back(db_path, clock):
    chosen = {"tst-w1"}
    coord, people, pins, _ = canary_coord(db_path, clock, chosen, units=1,
                                          manifest=make_det_manifest(canary={"rate": 0.5, "quarantine_after": 2}))
    k = lease(coord, people, "tst-w1", pins)
    assert SECRET_TEXT in k["wu"]["prompt"]
    chosen.clear()
    assert lease(coord, people, "tst-w1", pins) is None  # the canary stands in for the one real unit
    submit(coord, people, "tst-w1", k)
    got = lease(coord, people, "tst-w1", pins)
    assert got is not None and SECRET_TEXT not in got["wu"]["prompt"]


def test_canary_comes_only_from_a_joined_project(db_path, clock):
    coord, people, _, _ = canary_coord(db_path, clock, chosen={"tst-w1"}, units=1)
    clock.advance(10)
    other_text = "The other hidden sample shows alpha at 1.5 units."
    coord.add_project(make_manifest(id="tst-other", canary={"rate": 0.25, "quarantine_after": 2}), make_units(1),
                      make_canaries(3, text=other_text))
    pins = {"tst-other": coord.project("tst-other")["manifest_hash"]}
    for _ in range(3):  # the older canaries of tst-quorum sort first, but w1 never joined tst-quorum
        k = lease(coord, people, "tst-w1", pins)
        assert other_text in k["wu"]["prompt"] and (k["wu"]["project"], k["wu"]["manifest_hash"]) == (
            "tst-other", pins["tst-other"])
        submit(coord, people, "tst-w1", k)


def test_canary_lease_names_the_offer_that_fits(db_path, clock):
    m = make_det_manifest(stages__brain__needs={"families": ["openai"]}, canary={"rate": 0.25, "quarantine_after": 2})
    coord, people, pins, _ = canary_coord(db_path, clock, chosen={"tst-w1"}, manifest=m)
    offers = [offer("anthropic", adapter="unfit"), offer("openai", adapter="fit")]
    k, n = (coord.lease(people[w]["id"], pins, offers) for w in ("tst-w1", "tst-w2"))
    assert SECRET_TEXT in k["wu"]["prompt"] and SECRET_TEXT not in n["wu"]["prompt"]
    assert k["adapter"] == n["adapter"] == "fit"


def test_quorum_canary_is_graded_on_the_quorum_fields_only(db_path, clock):
    answer = {"kind": "alpha", "value": 1.5, "quotes": ["a quote no output has"]}
    coord, people, pins, _ = canary_coord(db_path, clock, chosen={"tst-w1", "tst-w2"},
                                          canaries=make_canaries(1, answer=answer, text=SECRET_TEXT))
    submit(coord, people, "tst-w1", lease(coord, people, "tst-w1", pins))
    submit(coord, people, "tst-w2", lease(coord, people, "tst-w2", pins), make_output(value=1.6))
    assert canary_counts(db_path) == {"tst-w1": (1, 0), "tst-w2": (0, 1)}


CANARY_ON = {"rate": 0.5, "quarantine_after": 2}


def first_lease(tmp_path, canary, kind, units=1, voters=(), clock=None, **kw):
    """The voters lease and submit, then tst-w1 leases (a canary when canary is True); later leases are real."""
    chosen = set()
    m = (make_det_manifest if kind == "det" else make_manifest)(canary=CANARY_ON, **kw)
    coord, people, pins, _ = canary_coord(tmp_path / f"{kind}-{canary}.db", clock or FakeClock(), chosen, manifest=m,
                                          units=units)
    for w in voters:
        assert submit(coord, people, w, lease(coord, people, w, pins))[0] == 200
    if canary:
        chosen.add("tst-w1")
    a = lease(coord, people, "tst-w1", pins)
    assert (SECRET_TEXT in a["wu"]["prompt"]) == canary
    chosen.clear()
    return coord, people, pins, a


@pytest.mark.parametrize("kind,others", [
    ("det", [("tst-w2", "openai")]), ("det", [("tst-w2", "anthropic")]), ("quorum", [("tst-w2", "anthropic")]),
    ("quorum", [("tst-w2", "openai"), ("tst-w3", "google")])])
def test_a_canary_holds_the_slot_and_family_of_the_unit_it_replaced(tmp_path, kind, others):
    seen = {}
    for canary in (True, False):
        coord, people, pins, _ = first_lease(tmp_path, canary, kind)
        seen[canary] = [(g := lease(coord, people, w, pins, family=f)) and masked(g) for w, f in others]
    assert seen[True] == seen[False]


@pytest.mark.parametrize("kind,voters", [("det", ()), ("quorum", ("tst-w2",))])
def test_progress_under_honest_traffic_is_the_same_while_a_canary_is_held(tmp_path, kind, voters):
    seen = {}
    for canary in (True, False):
        coord, people, pins, _ = first_lease(tmp_path, canary, kind, voters=voters)
        before = coord.projects()[0]["progress"]
        if honest := lease(coord, people, "tst-w3", pins):
            submit(coord, people, "tst-w3", honest)
        seen[canary] = before, coord.projects()[0]["progress"]
    assert seen[True] == seen[False]


@pytest.mark.parametrize("kind", ["det", "quorum"])
def test_two_accounts_comparing_wu_ids_learn_nothing(tmp_path, kind):
    seen = {}
    for canary in (True, False):
        coord, people, pins, a = first_lease(tmp_path, canary, kind, units=4)
        seen[canary] = a["wu"]["id"] == lease(coord, people, "tst-w2", pins)["wu"]["id"]
    assert seen[True] == seen[False]


def test_two_canaries_at_once_show_two_ids_as_two_real_leases_do(tmp_path):
    seen = {}
    for canary in (True, False):
        coord, people, pins, _ = canary_coord(tmp_path / f"{canary}.db", FakeClock(),
                                              {"tst-w1", "tst-w2"} if canary else set(),
                                              manifest=make_det_manifest(canary=CANARY_ON), units=4)
        a, b = lease(coord, people, "tst-w1", pins), lease(coord, people, "tst-w2", pins)
        assert (SECRET_TEXT in a["wu"]["prompt"]) == (SECRET_TEXT in b["wu"]["prompt"]) == canary
        seen[canary] = a["wu"]["id"] == b["wu"]["id"]
    assert seen[True] == seen[False]


@pytest.mark.parametrize("kind,then,other,want", [
    ("quorum", "fail", "openai", (409, "wu_closed")),  # W fails while tst-w1's lease is live
    ("det", "done", "openai", (409, "lease_expired")),
    ("det", "hold", "openai", (409, "lease_expired")),  # no free slot
    ("quorum", "hold", "anthropic", (409, "lease_expired"))])  # a free slot, but the family clashes
def test_a_canary_submit_answers_as_a_submit_on_the_replaced_wu(tmp_path, kind, then, other, want):
    seen = {}
    for canary in (True, False):
        clock = FakeClock()
        coord, people, pins, a = first_lease(tmp_path, canary, kind, clock=clock, verify__max_attempts=1)
        if then != "fail":
            clock.advance(TTL + 1)
        b = lease(coord, people, "tst-w2", pins, family=other)  # the replaced WU W, or a's own WU
        if then != "hold":
            assert submit(coord, people, "tst-w2", b, make_output(kind="nope") if then == "fail" else None)[0] in (200, 422)
        seen[canary] = [api_error(submit, coord, people, "tst-w1", a)]
        if then == "hold":  # the 409 leaves the lease as it was: once W has room again, the late submit goes on
            coord.release(people["tst-w2"]["id"], b["lease_id"], "error")
            seen[canary].append(submit(coord, people, "tst-w1", a))
    assert seen[True] == seen[False] and seen[True][0] == want, seen


def test_lease_queries_use_their_indexes(coord, db_path):
    from rally.server import CANARY_PICK, CANDIDATES
    sql = CANDIDATES.format(ids=":p0", order="WHEN :p0 THEN 0")
    with closing(sqlite3.connect(db_path)) as db:
        plan = " ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + CANARY_PICK, ("tst-p", 1)))
        outer = [r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql, {"me": 1, "skip": "", "p0": "tst-p"})]
        partial = {r[1]: r[4] for r in db.execute("PRAGMA index_list(work_units)")}
    assert "INDEX wu_canary" in plan and partial["wu_canary"] == 1, (plan, partial)  # it holds canaries only
    assert any(r.startswith("SEARCH w USING INDEX wu_pick (status=?") for r in outer), outer  # not every unit


def test_quorum_waits_for_all_replicas(coord):
    people, pins = setup(coord, make_manifest(verify__quorum__replicas=3))
    got = {w: lease(coord, people, w, pins) for w in ("tst-w1", "tst-w2", "tst-w3")}
    assert [submit(coord, people, w, got[w]) for w in ("tst-w1", "tst-w2")] == [PENDING] * 2  # agree 2, replicas 3
    assert submit(coord, people, "tst-w3", got["tst-w3"]) == (200, {"status": "accepted", "points": PTS})
    (row,) = export_rows(coord, people, "tst-quorum").values()
    assert (row["status"], [s["status"] for s in row["submissions"]]) == ("done", ["accepted"] * 3)


def test_agree_counts_members_when_distinct_families_is_false(coord):
    people, pins = setup(coord, make_manifest(verify__quorum__distinct_families=False))
    a, b = lease(coord, people, "tst-w1", pins), lease(coord, people, "tst-w2", pins)
    assert submit(coord, people, "tst-w1", a) == PENDING
    assert submit(coord, people, "tst-w2", b, make_output(value=9.0)) == PENDING
    (row,) = export_rows(coord, people, "tst-quorum").values()
    assert row["status"] == "open"  # two single votes: no group reached agree = 2


def test_a_failed_wu_rejects_its_votes_and_refuses_live_and_late_submits(coord, clock):
    people, pins = setup(coord, make_manifest(verify__quorum__replicas=3, verify__max_attempts=1))
    assert submit(coord, people, "tst-w1", lease(coord, people, "tst-w1", pins)) == PENDING
    live = lease(coord, people, "tst-w2", pins)
    assert submit(coord, people, "tst-w3", lease(coord, people, "tst-w3", pins), make_output(quotes=["x"]))[0] == 422
    assert api_error(coord.submit, people["tst-w2"]["id"], live["lease_id"], make_output()) == (409, "wu_closed")
    clock.advance(TTL + 1)
    assert api_error(coord.submit, people["tst-w2"]["id"], live["lease_id"], make_output()) == (409, "lease_expired")
    (row,) = export_rows(coord, people, "tst-quorum").values()
    assert row["status"] == "failed"
    assert [(s["participant"], s["status"], s["reason"], s["points"]) for s in row["submissions"]] == [
        ("tst-w1", "rejected", "failed", 0), ("tst-w3", "rejected", "check:quote_span:quotes", 0)]


def test_a_version_bump_may_not_change_the_verify_mode(coord):
    people, pins = setup(coord)
    a = lease(coord, people, "tst-w1", pins)
    assert submit(coord, people, "tst-w1", a) == PENDING
    with pytest.raises(ManifestError, match="^verify.mode:"):
        coord.add_project(make_det_manifest(id="tst-quorum", version=2), [], [])
    view = coord.project("tst-quorum")
    assert (view["version"], view["manifest_hash"], view["verify"]["mode"]) == (1, pins["tst-quorum"], "quorum")
    b = lease(coord, people, "tst-w2", pins)  # the old pin still works: nothing changed
    assert submit(coord, people, "tst-w2", b) == (200, {"status": "accepted", "points": PTS})


def test_a_version_bump_may_not_change_the_quorum_rule(coord):
    _, pins = setup(coord)
    with pytest.raises(ManifestError, match="^verify.quorum:"):
        coord.add_project(make_manifest(version=2, verify__quorum__fields=["kind"]), [], [])
    view = coord.project("tst-quorum")
    assert (view["version"], view["manifest_hash"], view["verify"]["quorum"]["fields"]) == (
        1, pins["tst-quorum"], ["kind", "value"])
    # max_replicas left out fills in its default 3, the same value as before
    coord.add_project(make_manifest(version=2, stages__brain__prompt="New words.\n${text}",
                                    verify__quorum__max_replicas=DROP), [], [])
    view = coord.project("tst-quorum")
    assert (view["version"], view["stages"]["brain"]["prompt"]) == (2, "New words.\n${text}")


def test_a_bump_that_turns_canaries_on_needs_stored_canaries(coord):
    setup(coord, make_det_manifest())
    with pytest.raises(ManifestError, match="^canaries:"):  # a bump stores no canaries, so these would be lost
        coord.add_project(make_det_manifest(version=2, canary=CANARY_ON), [], make_canaries(2))
    assert coord.project("tst-det")["version"] == 1


def test_a_bump_keeps_the_stored_canaries(db_path, clock):
    coord, people, _, _ = canary_coord(db_path, clock, {"tst-w1"}, manifest=make_det_manifest(canary=CANARY_ON))
    coord.add_project(make_det_manifest(version=2, canary=CANARY_ON), [], [])
    pins = {"tst-det": coord.project("tst-det")["manifest_hash"]}
    assert SECRET_TEXT in lease(coord, people, "tst-w1", pins)["wu"]["prompt"]


def test_export_reads_one_snapshot(coord, db_path, clock):
    people, pins = setup(coord)
    a, b = lease(coord, people, "tst-w1", pins), lease(coord, people, "tst-w2", pins)
    assert submit(coord, people, "tst-w1", a) == PENDING
    writer, read, wrote = Coordinator(db_path, now=clock.now), coord._read, []

    def read_then_write(sql, args=()):
        rows = read(sql, args)
        if sql.startswith("SELECT * FROM work_units") and not wrote:  # its own connection: a real second writer
            wrote.append(writer.submit(people["tst-w2"]["id"], b["lease_id"], make_output()))
        return rows

    coord._read = read_then_write
    (row,) = coord.export(people["tst-owner"]["id"], "tst-quorum")
    assert wrote == [(200, {"status": "accepted", "points": PTS})]
    assert (row["status"], [(s["participant"], s["status"]) for s in row["submissions"]]) == (
        "open", [("tst-w1", "pending")])
