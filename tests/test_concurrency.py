"""20 real threads lease and submit over HTTP against one file database; slots never overfill."""

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import closing

from conftest import FAMILIES, make_manifest, make_output, make_units, offer
from rally.server import make_server

THREADS, UNITS = 20, 100
OVERFULL = """
SELECT w.id FROM work_units w
WHERE (SELECT count(*) FROM leases l WHERE l.wu_id = w.id AND l.status = 'active')
    + (SELECT count(*) FROM submissions s WHERE s.wu_id = w.id AND s.status IN ('pending', 'accepted')) > w.replicas
"""


def post(port, path, body, token):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:  # any HTTP error raises and is recorded
        text = r.read()
        return r.status, json.loads(text) if text else None


def test_twenty_threads_never_overfill_a_wu(coord, db_path):
    coord.add_participant("tst-owner", "owner")
    tokens = [(coord.add_participant(f"tst-p{i}"), FAMILIES[i % 4]) for i in range(THREADS)]
    m = make_manifest()
    coord.add_project(m, make_units(UNITS), [])
    pins = {m["id"]: coord.project(m["id"])["manifest_hash"]}
    srv = make_server(coord, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, args=(0.01,), daemon=True).start()
    port, errors, overfull, done = srv.server_address[1], [], [], threading.Event()
    start = threading.Barrier(THREADS)

    def work(token, family):
        try:
            start.wait()
            while True:
                code, got = post(port, "/v1/lease", {"projects": pins, "offers": [offer(family)]}, token)
                if code == 204:
                    return
                code, reply = post(port, f"/v1/leases/{got['lease_id']}/submit", {"output": make_output()}, token)
                assert code == 200 and reply["status"] in ("pending", "accepted"), reply
        except Exception as e:  # noqa: BLE001 - every client error fails the test below
            errors.append(repr(e))

    def watch():
        with closing(sqlite3.connect(db_path)) as db:
            while not done.is_set():
                overfull.extend(r[0] for r in db.execute(OVERFULL))

    watcher = threading.Thread(target=watch)
    watcher.start()
    workers = [threading.Thread(target=work, args=t) for t in tokens]
    for t in workers:
        t.start()
    for t in workers:
        t.join(60)
    done.set()
    watcher.join()
    srv.shutdown()
    srv.server_close()

    assert errors == [] and overfull == []
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute(OVERFULL).fetchall() == []
        families = db.execute("SELECT wu_id, count(DISTINCT family), count(*) FROM submissions GROUP BY wu_id").fetchall()
        statuses = dict(db.execute("SELECT status, count(*) FROM work_units GROUP BY status").fetchall())
        n_leases = db.execute("SELECT count(*) FROM leases").fetchone()[0]
        per_wu = db.execute("SELECT count(*), count(DISTINCT participant_id), count(DISTINCT family) FROM leases"
                            " GROUP BY wu_id").fetchall()
    assert {(n_fam, n) for _, n_fam, n in families} == {(2, 2)}  # two votes from two families on every WU
    # a thread stops only when no open WU fits it, and every lease ends in a valid submit, so all finish
    assert statuses == {"done": UNITS}
    # each participant offers only its own family, so the lease family is the participant's family
    assert n_leases == 2 * UNITS and len(per_wu) == UNITS and set(per_wu) == {(2, 2, 2)}
