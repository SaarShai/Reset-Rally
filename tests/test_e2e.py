"""End to end over real HTTP: Phase 1 (two fake workers, 100 deterministic WUs) and the Phase 2 five-participant rally."""

import contextlib
import json
import re
import sqlite3
import threading
import urllib.request
from contextlib import closing
from pathlib import Path

import pytest

from conftest import FakeClock, add_people, make_det_manifest, make_output, make_units
from rally.server import Coordinator, load_project_dir, make_server
from rally.worker import ApiError, Client, Worker

FIXTURES = Path(__file__).parent / "fixtures"
LEASE_KEYS = {"lease_id", "expires_at", "heartbeat_s", "adapter", "wu"}
WU_KEYS = {"id", "project", "manifest_hash", "stage", "mode", "prompt", "schema", "timeout_s", "workspace", "image",
           "command", "files"}
# a WU over its replicas: active leases (expired ones too, until the next lease call expires them) + live votes
OVER_SQL = """SELECT w.ref FROM work_units w WHERE w.is_canary = 0 AND
  (SELECT count(*) FROM leases l WHERE l.wu_id = w.id AND l.status = 'active')
  + (SELECT count(*) FROM submissions s WHERE s.wu_id = w.id AND s.status IN ('pending','accepted')) > w.replicas"""


@contextlib.contextmanager
def running(coord):
    srv = make_server(coord, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, args=(0.01,), daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


class Recorder(Client):
    """The worker's own Client, plus a log of each POST and its reply, so the test sees what the server served."""

    def __init__(self, server, token, who, log):
        super().__init__(server, token)
        self.who, self.log = who, log

    def post(self, path, body):
        entry = {"who": self.who, "path": path, "body": body}
        self.log.append(entry)
        try:
            entry["reply"] = super().post(path, body)
        except ApiError as e:
            entry["error"] = (e.code, e.body)
            raise
        return entry["reply"]


def export(base, token, project):
    req = urllib.request.Request(f"{base}/v1/projects/{project}/export", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.headers["Content-Type"] == "application/x-ndjson"
        return [json.loads(line) for line in r.read().decode().splitlines()]


def quiet(*_):
    pass


# ---------- Phase 1 ----------


def test_phase1_two_fake_workers_finish_100_deterministic_wus(tmp_path):
    clock = FakeClock()
    coord = Coordinator(tmp_path / "rally.db", now=clock.now)
    people = add_people(coord, workers=2)
    manifest = make_det_manifest()
    pid = coord.add_project(manifest, make_units(100), [])
    with running(coord) as base:
        pins = {pid: Client(base, people["tst-w1"]["token"]).get(f"/v1/projects/{pid}")["manifest_hash"]}
        workers = []
        for name, family in (("tst-w1", "anthropic"), ("tst-w2", "openai")):
            cfg = {"server": base, "token": people[name]["token"], "projects": pins,
                   "adapters": {"fk": {"type": "fake", "family": family, "answers": {"": make_output()}}}}
            naps = []
            workers.append((Worker(cfg, Client(base, cfg["token"]), sleep=naps.append, log=quiet), naps))
        for _ in range(200):  # one step per worker per round; stop when both got 204 in the same round
            idle = []
            for w, naps in workers:
                before = len(naps)
                assert w.step()
                idle.append(len(naps) > before)
            if all(idle):
                break
        rows = export(base, people["tst-owner"]["token"], pid)
        me = [Client(base, people[n]["token"]).get("/v1/me") for n in ("tst-w1", "tst-w2")]
        board = Client(base, people["tst-w1"]["token"]).get(f"/v1/leaderboard?project={pid}")
    assert all(idle)
    assert len(rows) == 100 and {r["status"] for r in rows} == {"done"}
    assert sorted(r["ref"] for r in rows) == sorted(u["id"] for u in make_units(100))
    assert all([s["status"] for s in r["submissions"]] == ["accepted"] for r in rows)
    points = manifest["stages"]["brain"]["points"]
    assert sum(m["points"] for m in me) == sum(b["points"] for b in board) == 100 * points
    assert all(m["points"] > 0 and m["active_leases"] == [] for m in me)


# ---------- Phase 2: the five-participant rally ----------

# Honest answers, read by hand off each note in tests/fixtures/rally-c (units C1-C5, canaries K1-K3).
TRUE = {
    "RX-100": ("RX-100", 5.5, "takes a 5.5 V supply"),              # C1
    "LM-20": ("LM-20", 12, "runs from a 12 V supply"),               # C2
    "ZX-3": ("ZX-3", 3.3, "The ZX-3 sensor runs at 3.3 V"),          # C3
    "QT-9": ("QT-9", 9, "uses a 9 V supply"),                        # C4
    "HD-4": ("HD-4", 5, "needs a 5 V supply"),                       # C5 (real work left for Eve's canaries)
    "KC-7": ("KC-7 core", 1.8, "Its core supply is 1.8 V"),          # K1 (trap: the 12 V adapter)
    "PB-2": ("PB-2", 24, "needs a 24 V supply"),                     # K2
    "MV-5": ("MV-5 logic", 2.5, "Its logic supply is 2.5 V"),        # K3 (trap: the 9 V battery)
}
CANARY_PARTS = {"KC-7", "PB-2", "MV-5"}
BOB = {**TRUE, "ZX-3": ("ZX-3", 5.0, "Its I/O pins tolerate 5.0 V")}  # seeded: Bob reads the I/O limit on C3
SUMS = {"A: 17\nB: 25\n": 42, "A: 300\nB: 456\n": 756, "A: 999\nB: 1\n": 1000}  # rally-d units, added by hand
# (participant, n-th lease call that reaches the canary step) -> a canary is wanted
WANT_CANARY = {("tst-alice", 1), ("tst-bob", 2), ("tst-carol", 1), ("tst-eve", 5), ("tst-eve", 6)}


def answers(table):
    return {part: {"component": c, "supply_v": v, "quote": q} for part, (c, v, q) in table.items()}


def note(lease):
    return re.search(r"NOTE:\n(.*)\n", lease["wu"]["prompt"])[1]


def part(lease):
    return note(lease).split()[1]


def lazy(lease):
    """Eve's lazy answer: no reading; the part name, her usual 12 V and the first sentence as the quote."""
    return {"component": part(lease), "supply_v": 12, "quote": note(lease).split(". ")[0].rstrip(".")}


def test_five_participant_rally(tmp_path):
    clock, calls = FakeClock(), {}

    def want_canary(name, project_id, rate):
        calls[name] = calls.get(name, 0) + 1
        return (name, calls[name]) in WANT_CANARY

    db = tmp_path / "rally.db"
    coord = Coordinator(db, now=clock.now, want_canary=want_canary)
    proj_c, proj_d = load_project_dir(FIXTURES / "rally-c"), load_project_dir(FIXTURES / "rally-d")
    coord.add_participant(proj_c[0]["owner"], "owner")
    admin = coord.add_participant("tst-auditor", "admin")
    tokens = {n: coord.add_participant(n) for n in ("tst-alice", "tst-bob", "tst-carol", "tst-dave", "tst-eve")}
    pc, pd = coord.add_project(*proj_c), coord.add_project(*proj_d)
    stage_c = proj_c[0]["stages"]["brain"]
    assert (stage_c["points"], proj_d[0]["stages"]["brain"]["points"]) == (2, 1)  # the points the sums below use
    log, naps = [], {n: [] for n in tokens}

    def check():
        with closing(sqlite3.connect(db)) as c:
            assert c.execute(OVER_SQL).fetchall() == []
        for e in log:
            if e["path"] == "/v1/lease" and e.get("reply"):
                assert "canary" not in json.dumps(e["reply"]).lower()
                assert set(e["reply"]) == LEASE_KEYS and set(e["reply"]["wu"]) == WU_KEYS

    def last(who, suffix):
        return next(e for e in reversed(log) if e["who"] == who and e["path"].endswith(suffix))

    def step(w):
        ok = w.step()
        check()
        return ok

    with running(coord) as base:
        pins = {p: Client(base, tokens["tst-eve"]).get(f"/v1/projects/{p}")["manifest_hash"] for p in (pc, pd)}

        def worker(name, family, projects, table, **extra):
            cfg = {"server": base, "token": tokens[name], "projects": {p: pins[p] for p in projects},
                   "adapters": {name: {"type": "fake", "family": family, "answers": table, **extra}}}
            return Worker(cfg, Recorder(base, tokens[name], name, log), sleep=naps[name].append, log=quiet)

        alice = worker("tst-alice", "anthropic", [pc], answers(TRUE), limit_after=4)
        bob = worker("tst-bob", "openai", [pc], answers(BOB))
        carol = worker("tst-carol", "google", [pc], answers(TRUE))
        dave = worker("tst-dave", "qwen", [pd], {k: {"sum": v} for k, v in SUMS.items()})
        eve = Recorder(base, tokens["tst-eve"], "tst-eve", log)
        eve_offer = [{"adapter": "opencode", "kind": "llm", "family": "deepseek", "model": "deepseek-v4",
                      "modes": ["json"]}]

        def eve_lease():
            got = eve.post("/v1/lease", {"projects": {pc: pins[pc]}, "offers": eve_offer})
            check()
            return got

        def eve_submit(lease, output):
            try:
                got = 200, eve.post(f"/v1/leases/{lease['lease_id']}/submit", {"output": output})
            except ApiError as e:
                if e.code != 422:
                    raise
                got = 422, e.body
            check()
            return got

        def submitted(who):
            return last(who, "/submit")["reply"]

        # P0: Eve leases C1 (highest priority) and drops.
        e1 = eve_lease()
        assert part(e1) == "RX-100"

        # P1: Alice and Bob drain; Dave sums; Eve comes back faulty.
        step(alice)  # canary (Alice lease 1): graded in silence
        assert part(last("tst-alice", "/lease")["reply"]) in CANARY_PARTS
        assert submitted("tst-alice") == {"status": "pending", "points": 0}
        step(bob)  # C1, the slot next to Eve's live lease
        assert part(last("tst-bob", "/lease")["reply"]) == "RX-100"
        step(dave)
        step(alice)  # C1 is full (Eve's lease + Bob's vote) but open: Alice gets C2
        assert part(last("tst-alice", "/lease")["reply"]) == "LM-20"
        clock.advance(stage_c["lease_ttl_s"] + 1)  # Eve's lease runs out
        step(bob)  # canary (Bob lease 2); this lease call expires Eve's lease
        assert part(last("tst-bob", "/lease")["reply"]) in CANARY_PARTS
        assert submitted("tst-bob") == {"status": "pending", "points": 0}
        step(alice)  # C1 again: Eve's slot came back; anthropic + openai agree
        assert part(last("tst-alice", "/lease")["reply"]) == "RX-100"
        assert submitted("tst-alice") == {"status": "accepted", "points": 2}
        with pytest.raises(ApiError) as stale:
            eve_submit(e1, answers(TRUE)["RX-100"])
        assert (stale.value.code, stale.value.body["error"]) == (409, "lease_expired")
        e2 = eve_lease()  # C2 has one free slot
        assert part(e2) == "LM-20"
        assert eve_submit(e2, '{"component": "LM-20", "supply_v": 12,') == (422, {
            "status": "rejected", "reason": "schema: $: not an object", "points": 0})
        step(bob)  # C2 agrees with Alice
        assert part(last("tst-bob", "/lease")["reply"]) == "LM-20"
        assert submitted("tst-bob") == {"status": "accepted", "points": 2}
        step(dave)
        e3 = eve_lease()
        assert part(e3) == "ZX-3"
        assert eve_submit(e3, {"component": "ZX-3", "supply_v": 3.3, "quote": "The ZX-3 runs at 3.3 V"}) == (422, {
            "status": "rejected", "reason": "check:quote_span:quote", "points": 0})
        step(alice)  # C3 = 3.3 (Alice's 4th fake call, her last before the limit)
        step(bob)  # C3 = 5.0: conflict, a third slot opens
        assert part(last("tst-bob", "/lease")["reply"]) == "ZX-3"
        assert submitted("tst-bob") == {"status": "pending", "points": 0}
        e4 = eve_lease()  # C3 is no longer hers to take; C4 is
        assert part(e4) == "QT-9"
        assert eve_submit(e4, answers(TRUE)["QT-9"]) == (200, {"status": "pending", "points": 0})
        e5 = eve_lease()  # a canary in place of C5, the real work Eve may still take
        assert part(e5) in CANARY_PARTS
        assert eve_submit(e5, lazy(e5)) == (200, {"status": "pending", "points": 0})  # same reply as a real vote
        e6 = eve_lease()
        assert part(e6) in CANARY_PARTS - {part(e5)}
        assert eve_submit(e6, lazy(e6)) == (200, {"status": "pending", "points": 0})  # second failure: quarantined
        with pytest.raises(ApiError) as banned:
            eve_lease()
        assert (banned.value.code, banned.value.body["error"]) == (403, "not_active")
        assert step(alice)  # C4 (Eve's vote is gone), 5th fake call -> rate limit -> release at once
        assert part(last("tst-alice", "/lease")["reply"]) == "QT-9"
        assert last("tst-alice", "/release")["body"] == {"reason": "rate_limited"}
        step(bob)  # C4, the slot Alice gave back
        assert part(last("tst-bob", "/lease")["reply"]) == "QT-9"
        step(dave)
        assert step(alice) is False  # no usable adapter: Alice stops without a lease call
        step(bob)
        assert part(last("tst-bob", "/lease")["reply"]) == "HD-4"
        assert step(bob) and naps["tst-bob"] == [30]  # nothing left that Bob may take: 204, back off
        # A second openai account gets no slot: C3, C4 and C5 each hold Bob's openai vote.
        dave_openai = [{"adapter": "codex", "kind": "llm", "family": "openai", "model": None, "modes": ["json"]}]
        assert Recorder(base, tokens["tst-dave"], "tst-dave", log).post(
            "/v1/lease", {"projects": {pc: pins[pc]}, "offers": dave_openai}) is None
        check()

        # P2: Carol comes online and breaks the C3 tie.
        step(carol)  # canary (Carol lease 1)
        assert part(last("tst-carol", "/lease")["reply"]) in CANARY_PARTS
        step(carol)
        assert part(last("tst-carol", "/lease")["reply"]) == "ZX-3"
        assert submitted("tst-carol") == {"status": "accepted", "points": 2}
        step(carol)
        assert part(last("tst-carol", "/lease")["reply"]) == "QT-9"
        assert submitted("tst-carol") == {"status": "accepted", "points": 2}
        step(carol)
        assert part(last("tst-carol", "/lease")["reply"]) == "HD-4"
        assert submitted("tst-carol") == {"status": "accepted", "points": 2}
        for w, who in ((carol, "tst-carol"), (dave, "tst-dave"), (bob, "tst-bob")):  # quiet: everyone gets 204
            assert step(w) and naps[who]

        rows_c, rows_d = export(base, admin, pc), export(base, admin, pd)
        me = {n: Client(base, t).get("/v1/me") for n, t in tokens.items()}
        board = Client(base, admin).get("/v1/leaderboard")
        progress = {p["id"]: p["progress"] for p in Client(base, admin).get("/v1/projects")}

    # ---- Expected end state, worked out by hand from the fixture table and the stage points ----
    # rally-c pays 2 per accepted vote and 0 for a canary; rally-d pays 1 per accepted sum.
    # Alice: canary 0 + C1 2 + C2 2 + C3 2 (wins with Carol) = 6; C4 released on the rate limit = 0.
    # Bob:   C1 2 + canary 0 + C2 2 + C3 0 (5.0 loses) + C4 2 + C5 2 = 8.
    # Carol: canary 0 + C3 2 (tie-break) + C4 2 + C5 2 = 6.
    # Dave:  three sums x 1 = 3.
    # Eve:   409 stale, 422 malformed, 422 quote, C4 vote rejected by quarantine, two canary fails = 0.
    # Total 6 + 8 + 6 + 3 + 0 = 23 = 10 accepted C votes x 2 + 3 sums x 1.
    assert {n: m["points"] for n, m in me.items()} == {"tst-alice": 6, "tst-bob": 8, "tst-carol": 6, "tst-dave": 3, "tst-eve": 0}
    assert sum(b["points"] for b in board) == 23
    assert [b["name"] for b in board] == ["tst-bob", "tst-alice", "tst-carol", "tst-dave", "tst-eve"]
    assert {n: (m["status"], m["active_leases"]) for n, m in me.items()} == {
        "tst-alice": ("active", []), "tst-bob": ("active", []), "tst-carol": ("active", []), "tst-dave": ("active", []),
        "tst-eve": ("quarantined", [])}
    assert sum(1 for e in log if e["who"] == "tst-alice" and e["path"] == "/v1/lease") == 5
    assert progress == {pc: {"done": 5}, pd: {"done": 3}}  # canaries never count, never finish

    by_part = {r["payload"]["text"].split()[1]: r for r in rows_c}
    assert {r["ref"] for r in rows_c} == {u["id"] for u in proj_c[1]}
    assert {p: (r["status"], r["result"]["supply_v"]) for p, r in by_part.items()} == {
        "RX-100": ("done", 5.5), "LM-20": ("done", 12), "ZX-3": ("done", 3.3), "QT-9": ("done", 9), "HD-4": ("done", 5)}
    assert {p: [(s["participant"], s["status"], s["reason"], s["points"]) for s in r["submissions"]]
            for p, r in by_part.items()} == {
        "RX-100": [("tst-bob", "accepted", None, 2), ("tst-alice", "accepted", None, 2)],
        "LM-20": [("tst-alice", "accepted", None, 2), ("tst-eve", "rejected", "schema: $: not an object", 0),
                  ("tst-bob", "accepted", None, 2)],
        "ZX-3": [("tst-eve", "rejected", "check:quote_span:quote", 0), ("tst-alice", "accepted", None, 2),
                 ("tst-bob", "rejected", "quorum_lost", 0), ("tst-carol", "accepted", None, 2)],
        "QT-9": [("tst-eve", "rejected", "quarantined", 0), ("tst-bob", "accepted", None, 2),
                 ("tst-carol", "accepted", None, 2)],
        "HD-4": [("tst-bob", "accepted", None, 2), ("tst-carol", "accepted", None, 2)]}
    zx3 = by_part["ZX-3"]
    assert {s["family"] for s in zx3["submissions"] if s["status"] == "accepted"} == {"anthropic", "google"}
    assert zx3["result"]["quote"] == TRUE["ZX-3"][2]  # the first winner's output: Alice's
    assert {p: (r["attempts"], r["releases"]) for p, r in by_part.items()} == {
        "RX-100": (0, {}), "LM-20": (1, {}), "ZX-3": (1, {}), "QT-9": (0, {"rate_limited": 1}), "HD-4": (0, {})}
    assert sorted((r["payload"]["a"] + r["payload"]["b"], r["result"]["sum"]) for r in rows_d) == [
        (42, 42), (756, 756), (1000, 1000)]

    with closing(sqlite3.connect(db)) as c:  # counters the API does not show
        canary = dict(c.execute("SELECT name, canary_pass || '/' || canary_fail FROM participants"
                                " WHERE name IN ('tst-alice','tst-bob','tst-carol','tst-dave','tst-eve')").fetchall())
        expired = c.execute("SELECT p.name FROM leases l JOIN participants p ON p.id = l.participant_id"
                            " WHERE l.status = 'expired'").fetchall()
        live = c.execute("SELECT count(*) FROM work_units WHERE is_canary = 0 AND status = 'open'").fetchone()[0]
    assert canary == {"tst-alice": "1/0", "tst-bob": "1/0", "tst-carol": "1/0", "tst-dave": "0/0", "tst-eve": "0/2"}
    assert expired == [("tst-eve",)] and live == 0
