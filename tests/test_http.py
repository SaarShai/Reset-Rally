"""HTTP API against a real ThreadingHTTPServer on 127.0.0.1, port 0, with a urllib client."""

import http.client
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import closing

import pytest

from conftest import TEXT, add_people, make_canaries, make_det_manifest, make_output, make_units, offer
from rally.server import make_server

SECRET_TEXT = "The hidden sample shows alpha at 1.5 units."


@pytest.fixture
def api(coord):
    srv = make_server(coord, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, args=(0.01,), daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def call(port, method, path, body=None, token=None, raw=None):
    """Return (status, parsed JSON | text | None, content type)."""
    data = raw if raw is not None else None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code, text, ctype = r.status, r.read().decode(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        code, text, ctype = e.code, e.read().decode(), e.headers.get("Content-Type", "")
    assert "Traceback" not in text
    return code, json.loads(text) if text and ctype == "application/json" else text or None, ctype


def raw_post(port, path, headers):
    """A POST that sends only headers, so the test sees what the server does before any body arrives."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", path)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()
    r = conn.getresponse()
    out = r.status, json.loads(r.read())
    conn.close()
    return out


@pytest.fixture
def world(coord, api):
    people = add_people(coord)
    coord.add_project(make_det_manifest(), make_units(2), make_canaries(1, text=SECRET_TEXT))  # rate 0: never leased
    pins = {"tst-det": coord.project("tst-det")["manifest_hash"]}
    return people, pins


def test_healthz_needs_no_token(api):
    assert call(api, "GET", "/healthz")[:2] == (200, {"ok": True})


def test_register_with_an_invite(coord, api):
    invite = coord.create_invite()
    code, body, _ = call(api, "POST", "/v1/participants", {"name": "tst-newcomer", "invite": invite})
    assert code == 200 and set(body) == {"id", "token"}
    code, me, _ = call(api, "GET", "/v1/me", token=body["token"])
    assert (code, me["name"], me["role"], me["points"]) == (200, "tst-newcomer", "worker", 0)
    assert call(api, "POST", "/v1/participants", {"name": "second", "invite": invite})[:2] == (
        403, {"error": "bad_invite"})


def test_admin_creates_an_owner_and_others_cannot(coord, api):
    people = add_people(coord)
    code, body, _ = call(api, "POST", "/v1/participants", {"name": "tst-boss", "role": "owner"}, people["tst-admin"]["token"])
    assert code == 200
    assert call(api, "GET", "/v1/me", token=body["token"])[1]["role"] == "owner"
    for who in ("tst-w1", "tst-owner"):  # only an admin makes accounts
        assert call(api, "POST", "/v1/participants", {"name": "tst-x", "role": "admin"}, people[who]["token"])[:2] == (
            403, {"error": "role"})
    assert call(api, "POST", "/v1/participants", {"name": "tst-boss", "role": "worker"}, people["tst-admin"]["token"])[:2] == (
        409, {"error": "name_taken"})
    assert call(api, "POST", "/v1/participants", {"name": "tst-y", "role": "king"}, people["tst-admin"]["token"])[0] == 400


def test_register_without_token_or_invite_is_401(api):
    assert call(api, "POST", "/v1/participants", {"name": "tst-nobody"})[:2] == (401, {"error": "unauthorized"})


def test_name_taken_is_409(coord, api):
    coord.add_participant("tst-taken")
    assert call(api, "POST", "/v1/participants", {"name": "tst-taken", "invite": coord.create_invite()})[:2] == (
        409, {"error": "name_taken"})


def test_bad_or_missing_token_is_401(api):
    assert call(api, "GET", "/v1/me", token="wrong")[:2] == (401, {"error": "unauthorized"})
    assert call(api, "GET", "/v1/me")[:2] == (401, {"error": "unauthorized"})
    assert call(api, "POST", "/v1/lease", {"projects": {}, "offers": []}, token="wrong")[0] == 401


def test_projects_list_and_public_view_hide_canaries(coord, api, world):
    people, pins = world
    t = people["tst-w1"]["token"]
    code, listed, _ = call(api, "GET", "/v1/projects", token=t)
    assert code == 200 and [(p["id"], p["manifest_hash"], p["progress"]) for p in listed] == [
        ("tst-det", pins["tst-det"], {"open": 2})]
    code, view, _ = call(api, "GET", "/v1/projects/tst-det", token=t)
    assert code == 200 and view["manifest_hash"] == pins["tst-det"] and view["title"] == "Demo deterministic project"
    assert "canaries" not in view and SECRET_TEXT not in json.dumps(view)
    assert call(api, "GET", "/v1/projects/missing", token=t)[:2] == (404, {"error": "unknown_project"})


def test_lease_submit_and_me_points(api, world):
    people, pins = world
    t = people["tst-w1"]["token"]
    code, got, _ = call(api, "POST", "/v1/lease", {"projects": pins, "offers": [offer("anthropic")], "max_ttl_s": 900}, t)
    assert code == 200 and got["wu"]["project"] == "tst-det" and TEXT in got["wu"]["prompt"]
    lid = got["lease_id"]
    hb = call(api, "POST", f"/v1/leases/{lid}/heartbeat", {}, t)
    assert hb[0] == 200 and set(hb[1]) == {"expires_at"}
    assert call(api, "POST", f"/v1/leases/{lid}/submit", {"output": make_output(quotes=["nope"])}, t)[:2] == (
        422, {"status": "rejected", "reason": "check:quote_span:quotes", "points": 0})
    other = call(api, "POST", "/v1/lease", {"projects": pins, "offers": [offer("anthropic")]}, people["tst-w2"]["token"])[1]
    assert call(api, "POST", f"/v1/leases/{other['lease_id']}/submit", {"output": make_output(), "usage": {"seconds": 3}},
                people["tst-w2"]["token"])[:2] == (200, {"status": "accepted", "points": 2})
    me = call(api, "GET", "/v1/me", token=people["tst-w2"]["token"])[1]
    assert me["points"] == 2 and me["active_leases"] == []
    assert call(api, "POST", f"/v1/leases/{lid}/submit", {"output": make_output()}, people["tst-w2"]["token"])[:2] == (
        404, {"error": "unknown_lease"})


def test_release_route_and_409(api, world):
    people, pins = world
    t = people["tst-w1"]["token"]
    lid = call(api, "POST", "/v1/lease", {"projects": pins, "offers": [offer("anthropic")]}, t)[1]["lease_id"]
    assert call(api, "POST", f"/v1/leases/{lid}/release", {"reason": "stop"}, t)[:2] == (200, {"ok": True})
    assert call(api, "POST", f"/v1/leases/{lid}/release", {"reason": "stop"}, t)[:2] == (
        409, {"error": "lease_not_active"})
    assert call(api, "POST", f"/v1/leases/{lid}/submit", {"no_output": 1}, t)[0] == 400


def test_no_work_is_204_with_no_body(api, world):
    people, _ = world
    code, body, _ = call(api, "POST", "/v1/lease", {"projects": {}, "offers": [offer("anthropic")]},
                         people["tst-w1"]["token"])
    assert (code, body) == (204, None)


def test_post_without_content_length_is_411(api, world):
    people, _ = world
    assert raw_post(api, "/v1/lease", {"Authorization": f"Bearer {people['tst-w1']['token']}"}) == (
        411, {"error": "length_required"})


def test_body_above_one_mib_is_413_before_reading(api, world):
    people, _ = world
    headers = {"Authorization": f"Bearer {people['tst-w1']['token']}", "Content-Length": str(2**20 + 1)}
    assert raw_post(api, "/v1/lease", headers) == (413, {"error": "too_large"})  # no body sent: a read would hang


def test_one_mib_body_is_read(api, world):
    people, _ = world
    body = json.dumps({"projects": {}, "offers": [], "pad": ""}).encode()
    body = body[:-2] + b" " * (2**20 - len(body)) + b'"}'
    assert len(body) == 2**20
    assert call(api, "POST", "/v1/lease", token=people["tst-w1"]["token"], raw=body)[0] == 204


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b"\xff\xfe", b"[" * 100_000 + b"]" * 100_000,
                                 b'{"a":' * 50_000 + b"1" + b"}" * 50_000, b'{"x": NaN}', b'{"x": -Infinity}'])
def test_bad_json_bodies_are_400(api, world, raw):
    people, _ = world
    code, body, _ = call(api, "POST", "/v1/lease", token=people["tst-w1"]["token"], raw=raw)
    assert code == 400 and body["error"] == "bad_json"


def test_bad_lease_fields_are_400(api, world):
    people, _ = world
    code, body, _ = call(api, "POST", "/v1/lease", {"projects": [], "offers": []}, people["tst-w1"]["token"])
    assert code == 400 and body["error"] == "bad_request"


def test_unknown_routes_are_404(api, world):
    people, _ = world
    t = people["tst-w1"]["token"]
    for method, path in [("GET", "/v1/nothing"), ("GET", "/nope"), ("POST", "/v1/me"), ("GET", "/v1/lease"),
                         ("POST", "/v1/leases/abc/explode")]:
        assert call(api, method, path, {} if method == "POST" else None, t)[:2] == (404, {"error": "not_found"})


def test_unexpected_error_is_500_without_a_traceback(coord, api, world, monkeypatch, capsys):
    people, _ = world

    def boom(*_):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(coord, "me", boom)
    code, body, _ = call(api, "GET", "/v1/me", token=people["tst-w1"]["token"])
    assert (code, body) == (500, {"error": "internal"})
    assert "secret internal detail" in capsys.readouterr().err  # logged on the server side only


def test_export_is_jsonl_for_the_owner_and_403_for_a_worker(api, world):
    people, pins = world
    t = people["tst-w1"]["token"]
    for _ in range(2):
        lid = call(api, "POST", "/v1/lease", {"projects": pins, "offers": [offer("anthropic")]}, t)[1]["lease_id"]
        call(api, "POST", f"/v1/leases/{lid}/submit", {"output": make_output()}, t)
    code, text, ctype = call(api, "GET", "/v1/projects/tst-det/export", token=people["tst-owner"]["token"])
    rows = [json.loads(line) for line in text.splitlines()]
    assert (code, ctype, len(rows)) == (200, "application/x-ndjson", 2)
    assert sorted(r["ref"] for r in rows) == ["tst-u1", "tst-u2"] and {r["status"] for r in rows} == {"done"}
    assert call(api, "GET", "/v1/projects/tst-det/export", token=t)[:2] == (403, {"error": "forbidden"})


def test_leaderboard_route(api, world):
    people, pins = world
    t = people["tst-w1"]["token"]
    lid = call(api, "POST", "/v1/lease", {"projects": pins, "offers": [offer("anthropic")]}, t)[1]["lease_id"]
    call(api, "POST", f"/v1/leases/{lid}/submit", {"output": make_output()}, t)
    assert call(api, "GET", "/v1/leaderboard", token=t)[:2] == (200, [{"name": "tst-w1", "points": 2}])
    assert call(api, "GET", "/v1/leaderboard?project=tst-det", token=t)[1] == [{"name": "tst-w1", "points": 2}]
    assert call(api, "GET", "/v1/leaderboard?project=other", token=t)[1] == []


def test_database_runs_in_wal_mode(api, db_path):
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
