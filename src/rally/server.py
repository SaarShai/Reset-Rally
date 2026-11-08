"""Coordinator: SQLite schema, leases, quorum, canaries, quarantine, HTTP API and the rally-server CLI."""

import argparse
import contextlib
import hashlib
import json
import math
import random
import secrets
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rally.checks import (ManifestError, check_schema, grade_canary, manifest_hash, public_manifest, quorum_key,
                          render_prompt, run_checks, validate_manifest)

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  token_hash TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL DEFAULT 'worker' CHECK (role IN ('worker','owner','admin')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','quarantined','banned')),
  canary_pass INTEGER NOT NULL DEFAULT 0,
  canary_fail INTEGER NOT NULL DEFAULT 0,
  capabilities TEXT,
  created_at INTEGER NOT NULL,
  last_seen INTEGER
);
CREATE TABLE IF NOT EXISTS invites (
  code_hash TEXT PRIMARY KEY,
  used_by INTEGER REFERENCES participants(id),
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  version INTEGER NOT NULL,
  manifest TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  owner_id INTEGER NOT NULL REFERENCES participants(id),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','closed')),
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS work_units (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  ref TEXT,
  stage TEXT NOT NULL CHECK (stage IN ('brain','brawn','verify')),
  payload TEXT NOT NULL,
  is_canary INTEGER NOT NULL DEFAULT 0,
  answer TEXT,
  replicas INTEGER NOT NULL DEFAULT 1,
  attempts INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','verifying','done','failed','disputed')),
  result TEXT,
  created_at INTEGER NOT NULL,
  done_at INTEGER
);
CREATE INDEX IF NOT EXISTS wu_pick ON work_units (status, project_id, stage, priority);
CREATE INDEX IF NOT EXISTS wu_canary ON work_units (project_id, created_at) WHERE is_canary = 1;
CREATE TABLE IF NOT EXISTS leases (
  id TEXT PRIMARY KEY,
  wu_id TEXT NOT NULL REFERENCES work_units(id),
  participant_id INTEGER NOT NULL REFERENCES participants(id),
  adapter TEXT NOT NULL,
  family TEXT NOT NULL,
  model TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','submitted','released','expired')),
  reason TEXT,
  instead_of TEXT,
  issued_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS lease_live ON leases (status, expires_at);
CREATE INDEX IF NOT EXISTS lease_wu ON leases (wu_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS lease_once ON leases (wu_id, participant_id);
CREATE INDEX IF NOT EXISTS lease_instead ON leases (instead_of, participant_id) WHERE instead_of IS NOT NULL;
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY,
  wu_id TEXT NOT NULL REFERENCES work_units(id),
  lease_id TEXT NOT NULL UNIQUE REFERENCES leases(id),
  participant_id INTEGER NOT NULL REFERENCES participants(id),
  family TEXT NOT NULL,
  model TEXT,
  output TEXT NOT NULL,
  quorum_key TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
  reason TEXT,
  points INTEGER NOT NULL DEFAULT 0,
  usage TEXT,
  reply TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sub_wu ON submissions (wu_id, status);
CREATE INDEX IF NOT EXISTS sub_part ON submissions (participant_id);
"""

ROLES = ("worker", "owner", "admin")
MAX_OUTPUT = 262144
MAX_BODY = 1 << 20
VOTE_REASONS = {"quorum_lost", "disputed", "quarantined", "canary"}  # rejections after a valid submit (HTTP 200)
SLOTS_USED = """(SELECT count(*) FROM leases l WHERE l.wu_id = w.id AND l.status = 'active')
  + (SELECT count(*) FROM submissions s WHERE s.wu_id = w.id AND s.status IN ('pending','accepted'))
  + (SELECT count(*) FROM leases l WHERE l.instead_of = w.id AND l.status = 'active')"""
FAMILIES_USED = """SELECT family FROM leases WHERE wu_id = :id AND status = 'active'
  UNION SELECT family FROM submissions WHERE wu_id = :id AND status IN ('pending','accepted')
  UNION SELECT family FROM leases WHERE instead_of = :id AND status = 'active'"""
CANDIDATES = f"""SELECT w.* FROM work_units w JOIN projects p ON p.id = w.project_id
WHERE w.status = 'open' AND w.is_canary = 0 AND p.status = 'active' AND w.project_id IN ({{ids}})
  AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.wu_id = w.id AND l.participant_id = :me)
  AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.instead_of = w.id AND l.participant_id = :me
    AND l.status != 'submitted')
  AND {SLOTS_USED} < w.replicas
ORDER BY CASE w.project_id {{order}} END, w.priority DESC,
  EXISTS (SELECT 1 FROM submissions s WHERE s.wu_id = w.id) DESC, w.created_at, w.rowid"""

CANARY_PICK = """SELECT * FROM work_units w WHERE project_id = ? AND is_canary = 1
  AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.wu_id = w.id AND l.participant_id = ?) ORDER BY created_at, rowid"""
EXPIRE = "UPDATE leases SET status = 'expired' WHERE status = 'active' AND expires_at < ?"


class ApiError(Exception):
    def __init__(self, code, error, reason=None):
        super().__init__(f"{code} {error}" + (f": {reason}" if reason else ""))
        self.code, self.error, self.reason = code, error, reason


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _bad(reason):
    return ApiError(400, "bad_request", reason)


def _short_str(v, n=64):
    return isinstance(v, str) and 0 < len(v) <= n


def _too_deep(value, limit=64):
    stack = [(value, 0)]  # a loop, not recursion: json.loads in Python 3.14 parses far deeper than the stack allows
    while stack:
        v, d = stack.pop()
        if d > limit:
            return True
        if isinstance(v, (dict, list)):
            stack.extend((x, d + 1) for x in (v.values() if isinstance(v, dict) else v))
    return False


def _no_constant(name):
    raise ValueError(f"{name} is not JSON")  # NaN and Infinity would make the export invalid JSON


def _finite(text):
    f = float(text)
    return f if math.isfinite(f) else _no_constant(text)  # 1e999 parses to inf


class Coordinator:
    def __init__(self, db_path, now=time.time, want_canary=None):
        self.db_path, self._now = str(db_path), now
        self.want_canary = want_canary or (lambda name, project_id, rate: random.random() < rate)
        self._local = threading.local()
        self._db().executescript(f"BEGIN IMMEDIATE;{SCHEMA}COMMIT;")

    def now(self):
        return int(self._now())

    def _db(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, isolation_level=None, timeout=5)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout = 5000")
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA foreign_keys = ON")
            self._local.conn = c
        return c

    def close(self):
        """Close this thread's connection (the HTTP handler calls it after each request)."""
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    @contextlib.contextmanager
    def _tx(self, mode="IMMEDIATE"):
        c = self._db()
        c.execute(f"BEGIN {mode}")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    def _read(self, sql, args=()):
        return self._db().execute(sql, args).fetchall()

    # ---------- participants ----------

    def _insert_participant(self, c, name, role):
        if not _short_str(name):
            raise _bad("name must be a string of 1 to 64 characters")
        if role not in ROLES:
            raise _bad(f"role must be one of {', '.join(ROLES)}")
        token = secrets.token_urlsafe(32)
        try:
            cur = c.execute("INSERT INTO participants (name, token_hash, role, created_at) VALUES (?, ?, ?, ?)",
                            (name, _sha(token), role, self.now()))
        except sqlite3.IntegrityError:
            raise ApiError(409, "name_taken") from None
        return cur.lastrowid, token

    def add_participant(self, name, role="worker"):
        with self._tx() as c:
            return self._insert_participant(c, name, role)[1]

    def create_invite(self):
        code = secrets.token_urlsafe(32)
        with self._tx() as c:
            c.execute("INSERT INTO invites (code_hash, created_at) VALUES (?, ?)", (_sha(code), self.now()))
        return code

    def register(self, name, invite):
        with self._tx() as c:
            h = _sha(invite) if isinstance(invite, str) else ""
            if not c.execute("SELECT 1 FROM invites WHERE code_hash = ? AND used_by IS NULL", (h,)).fetchone():
                raise ApiError(403, "bad_invite")
            pid, token = self._insert_participant(c, name, "worker")
            c.execute("UPDATE invites SET used_by = ? WHERE code_hash = ?", (pid, h))
        return {"id": pid, "token": token}

    def auth(self, token):
        if not isinstance(token, str):
            return None
        r = self._read("SELECT id, name, role, status FROM participants WHERE token_hash = ?", (_sha(token),))
        return dict(r[0]) if r else None

    # ---------- projects ----------

    def add_project(self, manifest, units, canaries):
        m = validate_manifest(manifest)
        q = m["verify"].get("quorum")
        rows = [self._unit_row(u, f"units[{i}]") for i, u in enumerate(units)]
        rows += [self._canary_row(k, f"canaries[{i}]") for i, k in enumerate(canaries)]
        now, h = self.now(), manifest_hash(m)
        with self._tx() as c:
            owner = c.execute("SELECT id FROM participants WHERE name = ? AND role IN ('owner','admin')",
                              (m["owner"],)).fetchone()
            if owner is None:
                raise ManifestError(f"owner: no participant {m['owner']!r} with role owner or admin")
            old = c.execute("SELECT version, manifest FROM projects WHERE id = ?", (m["id"],)).fetchone()
            stored = c.execute("SELECT 1 FROM work_units WHERE project_id = ? AND is_canary = 1", (m["id"],)).fetchone()
            if m["canary"]["rate"] > 0 and not (stored if old else canaries):  # a bump stores no canaries
                raise ManifestError("canaries: canary.rate > 0 needs at least one canary")
            if old:
                if m["version"] <= old["version"]:
                    raise ManifestError(f"version: must be higher than {old['version']}")
                was = json.loads(old["manifest"])["verify"]
                for key in ("mode", "quorum"):  # a vote cast under the old rule must count the same under the new one
                    if was.get(key) != m["verify"].get(key):
                        raise ManifestError(f"verify.{key}: a new version may not change it from {was.get(key)!r}")
                c.execute("UPDATE projects SET manifest = ?, manifest_hash = ?, version = ? WHERE id = ?",
                          (json.dumps(m), h, m["version"], m["id"]))
                return m["id"]
            c.execute("INSERT INTO projects (id, version, manifest, manifest_hash, owner_id, created_at)"
                      " VALUES (?, ?, ?, ?, ?, ?)", (m["id"], m["version"], json.dumps(m), h, owner["id"], now))
            c.executemany("INSERT INTO work_units (id, project_id, ref, stage, payload, is_canary, answer, replicas,"
                          " priority, created_at) VALUES (?, ?, ?, 'brain', ?, ?, ?, ?, ?, ?)",
                          [(secrets.token_hex(8), m["id"], ref, payload, is_canary, answer,
                            q["replicas"] if q else 1, prio, now) for ref, payload, is_canary, answer, prio in rows])
        return m["id"]

    @staticmethod
    def _unit_row(u, path):
        if not isinstance(u, dict) or not isinstance(u.get("payload"), dict):
            raise ManifestError(f"{path}.payload: must be an object")
        if "id" in u and not isinstance(u["id"], str):
            raise ManifestError(f"{path}.id: must be a string")
        prio = u.get("priority", 0)
        if type(prio) is not int:
            raise ManifestError(f"{path}.priority: must be an int")
        return u.get("id"), json.dumps(u["payload"]), 0, None, prio

    @staticmethod
    def _canary_row(k, path):
        for key in ("payload", "answer"):
            if not isinstance(k, dict) or not isinstance(k.get(key), dict):
                raise ManifestError(f"{path}.{key}: must be an object")
        return None, json.dumps(k["payload"]), 1, json.dumps(k["answer"]), 0

    def set_project_status(self, project_id, status):
        if status not in ("active", "paused", "closed"):
            raise _bad("status must be active, paused or closed")
        with self._tx() as c:
            if c.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id)).rowcount == 0:
                raise ApiError(404, "unknown_project")

    def _manifest(self, c, project_id):
        return json.loads(c.execute("SELECT manifest FROM projects WHERE id = ?", (project_id,)).fetchone()[0])

    def projects(self):
        out = []
        for p in self._read("SELECT id, manifest, manifest_hash, version FROM projects WHERE status = 'active'"
                            " ORDER BY id"):
            m = json.loads(p["manifest"])
            progress = dict(self._read("SELECT status, count(*) FROM work_units WHERE project_id = ? AND"
                                       " is_canary = 0 GROUP BY status ORDER BY status", (p["id"],)))
            out.append({"id": p["id"], "title": m["title"], "track": m["track"], "version": p["version"],
                        "manifest_hash": p["manifest_hash"], "progress": progress})
        return out

    def project(self, project_id):
        r = self._read("SELECT manifest, manifest_hash FROM projects WHERE id = ?", (project_id,))
        if not r:
            raise ApiError(404, "unknown_project")
        return {**public_manifest(json.loads(r[0]["manifest"])), "manifest_hash": r[0]["manifest_hash"]}

    # ---------- leases ----------

    def _active_participant(self, c, participant_id):
        p = c.execute("SELECT * FROM participants WHERE id = ?", (participant_id,)).fetchone()
        if p is None or p["status"] != "active":
            raise ApiError(403, "not_active")
        return p

    @staticmethod
    def _check_lease_request(projects, offers, max_ttl_s):
        if not isinstance(projects, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                     for k, v in projects.items()):
            raise _bad("projects must map project ids to manifest hashes")
        if not isinstance(offers, list) or not all(isinstance(o, dict) for o in offers):
            raise _bad("offers must be a list of objects")
        for o in offers:
            if not (_short_str(o.get("adapter")) and _short_str(o.get("family"))
                    and (o.get("model") is None or _short_str(o["model"], 200))
                    and isinstance(o.get("modes", []), list)):
                raise _bad("each offer needs adapter and family strings, an optional model string, a modes list")
        if max_ttl_s is not None and (type(max_ttl_s) is not int or max_ttl_s <= 0):
            raise _bad("max_ttl_s must be a positive int")

    @staticmethod
    def _fit(c, m, wu, offers, max_ttl_s):
        """The first offer that may work this WU, or None."""
        st, q = m["stages"]["brain"], m["verify"].get("quorum")
        fams = st.get("needs", {}).get("families")
        used = set()
        if q and q["distinct_families"]:
            used = {r[0] for r in c.execute(FAMILIES_USED, {"id": wu["id"]})}
        for o in offers:
            if (o.get("kind") == "llm" and "json" in o.get("modes", []) and (fams is None or o["family"] in fams)
                    and (max_ttl_s is None or st["timeout_s"] <= max_ttl_s) and o["family"] not in used):
                return o
        return None

    def lease(self, participant_id, projects, offers, max_ttl_s=None):
        self._check_lease_request(projects, offers, max_ttl_s)
        with self._tx() as c:
            now = self.now()
            c.execute(EXPIRE, (now,))
            p = self._active_participant(c, participant_id)
            if p["role"] not in ("worker", "owner"):
                raise ApiError(403, "role")
            kept = {}
            for pid, h in projects.items():
                r = c.execute("SELECT manifest FROM projects WHERE id = ? AND status = 'active' AND manifest_hash = ?",
                              (pid, h)).fetchone()
                if r:
                    kept[pid] = json.loads(r[0])
            if not kept:
                return None
            names = [f":p{i}" for i in range(len(kept))]
            sql = CANDIDATES.format(ids=", ".join(names), order=" ".join(f"WHEN {n} THEN {i}" for i, n in enumerate(names)))
            args = {"me": participant_id, **{n[1:]: pid for n, pid in zip(names, kept)}}
            # no LIMIT 50 (spec 4.1): SQLite scores every candidate before the first row anyway, and a cut-off
            # list can starve a family whose fitting WUs sort later
            for wu in c.execute(sql, args):
                m = kept[wu["project_id"]]
                if o := self._fit(c, m, wu, offers, max_ttl_s):
                    # a canary only takes the place of real work, so a lease never proves a canary
                    rate, real = m["canary"]["rate"], wu
                    if rate > 0 and self.want_canary(p["name"], wu["project_id"], rate):
                        wu = c.execute(CANARY_PICK, (wu["project_id"], participant_id)).fetchone() or wu
                    return self._grant(c, p, m, wu, o, offers, max_ttl_s, now, real)
            return None

    def _grant(self, c, p, m, wu, o, offers, max_ttl_s, now, real):
        st = m["stages"]["brain"]
        ttl = st["lease_ttl_s"] if max_ttl_s is None else min(st["lease_ttl_s"], max_ttl_s + 60)
        lid = secrets.token_hex(8)
        c.execute("INSERT INTO leases (id, wu_id, participant_id, adapter, family, model, instead_of, issued_at,"
                  " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (lid, wu["id"], p["id"], o["adapter"], o["family"], o.get("model"),
                   None if wu is real else real["id"], now, now + ttl))
        c.execute("UPDATE participants SET last_seen = ?, capabilities = ? WHERE id = ?",
                  (now, json.dumps(offers), p["id"]))
        h = c.execute("SELECT manifest_hash FROM projects WHERE id = ?", (wu["project_id"],)).fetchone()[0]
        return {"lease_id": lid, "expires_at": now + ttl, "heartbeat_s": min(300, ttl // 3), "adapter": o["adapter"],
                "wu": {"id": real["id"], "project": wu["project_id"], "manifest_hash": h, "stage": "brain",
                       "mode": st["mode"], "prompt": render_prompt(st, json.loads(wu["payload"])),
                       "schema": st["schema"], "timeout_s": st["timeout_s"], "workspace": None, "image": None,
                       "command": None, "files": {}}}

    def _own_lease(self, c, participant_id, lease_id):
        self._active_participant(c, participant_id)
        lease = c.execute("SELECT * FROM leases WHERE id = ? AND participant_id = ?",
                          (lease_id, participant_id)).fetchone()
        if lease is None:
            raise ApiError(404, "unknown_lease")
        wu = c.execute("SELECT * FROM work_units WHERE id = ?", (lease["wu_id"],)).fetchone()
        return lease, wu, self._manifest(c, wu["project_id"])

    def heartbeat(self, participant_id, lease_id):
        with self._tx() as c:
            lease, _, m = self._own_lease(c, participant_id, lease_id)
            now = self.now()
            if lease["status"] == "active" and lease["expires_at"] >= now:
                exp = now + m["stages"]["brain"]["lease_ttl_s"]
                c.execute("UPDATE leases SET expires_at = ? WHERE id = ?", (exp, lease_id))
                return {"expires_at": exp}
            c.execute("UPDATE leases SET status = 'expired' WHERE id = ? AND status = 'active'", (lease_id,))
        raise ApiError(409, "lease_not_active")  # outside the transaction, so the expiry above is kept

    def release(self, participant_id, lease_id, reason):
        with self._tx() as c:
            lease, _, _ = self._own_lease(c, participant_id, lease_id)
            if not _short_str(reason):
                raise _bad("reason must be a string of 1 to 64 characters")
            if lease["status"] != "active":
                raise ApiError(409, "lease_not_active")
            c.execute("UPDATE leases SET status = 'released', reason = ? WHERE id = ?", (reason, lease_id))
        return {"ok": True}

    # ---------- submit ----------

    def submit(self, participant_id, lease_id, output, usage=None):
        with self._tx() as c:
            lease, wu, m = self._own_lease(c, participant_id, lease_id)
            if lease["status"] == "submitted":
                return tuple(json.loads(c.execute("SELECT reply FROM submissions WHERE lease_id = ?",
                                                  (lease_id,)).fetchone()[0]))
            if lease["status"] == "released":
                raise ApiError(409, "lease_not_active")
            now = self.now()
            st, q = m["stages"]["brain"], m["verify"].get("quorum")
            late = lease["status"] == "expired" or lease["expires_at"] < now
            # a canary lease gets the 409s that its replaced WU W would give, so the reply never tells it apart
            w = c.execute("SELECT * FROM work_units WHERE id = ?", (lease["instead_of"],)).fetchone() if wu["is_canary"] else wu
            if w["status"] != "open":
                raise ApiError(409, "lease_expired" if late else "wu_closed")
            if late:
                c.execute(EXPIRE, (now,))  # as in lease(): a dead lease never holds a slot
                args = {"id": w["id"]}  # EXPIRE just made this lease inactive, so it holds no slot
                free = c.execute(f"SELECT {SLOTS_USED} < w.replicas FROM work_units w WHERE w.id = :id", args).fetchone()[0]
                clash = q and q["distinct_families"] and lease["family"] in {r[0] for r in c.execute(FAMILIES_USED, args)}
                if not free or clash:
                    raise ApiError(409, "lease_expired")
            body = json.dumps(output)
            if len(body) > MAX_OUTPUT:
                reason = "size"
            elif not isinstance(output, dict):
                reason = "schema: $: not an object"
            else:
                reason = (check_schema(output, st["schema"])
                          or run_checks(output, json.loads(wu["payload"]), m["verify"]["checks"]))
            key = quorum_key(output, q["fields"], q["digits"]) if q and not reason else None
            cur = c.execute("INSERT INTO submissions (wu_id, lease_id, participant_id, family, model, output,"
                            " quorum_key, status, reason, usage, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (wu["id"], lease_id, participant_id, lease["family"], lease["model"], body, key,
                             "rejected" if reason else "pending", reason,
                             json.dumps(usage) if isinstance(usage, dict) else None, now))
            c.execute("UPDATE leases SET status = 'submitted' WHERE id = ?", (lease_id,))
            if reason:
                if wu["is_canary"]:
                    self._canary_fail(c, m, participant_id)
                else:
                    if c.execute("UPDATE work_units SET attempts = attempts + 1, status = CASE WHEN attempts + 1 >= ?"
                                 " THEN 'failed' ELSE status END WHERE id = ? RETURNING status",
                                 (m["verify"]["max_attempts"], wu["id"])).fetchone()[0] == "failed":
                        c.execute("UPDATE submissions SET status = 'rejected', reason = 'failed' WHERE wu_id = ? AND"
                                  " status = 'pending'", (wu["id"],))
            elif wu["is_canary"]:
                self._grade_canary(c, cur.lastrowid, output, wu, m, participant_id)
            elif q:
                self._quorum(c, wu["id"], m, now)
            else:
                c.execute("UPDATE submissions SET status = 'accepted', points = ? WHERE id = ?",
                          (st["points"], cur.lastrowid))
                c.execute("UPDATE work_units SET status = 'done', result = ?, done_at = ? WHERE id = ?",
                          (body, now, wu["id"]))
            sub = c.execute("SELECT * FROM submissions WHERE id = ?", (cur.lastrowid,)).fetchone()
            code, reply = self._reply(sub, wu, m)
            c.execute("UPDATE submissions SET reply = ? WHERE id = ?", (json.dumps([code, reply]), cur.lastrowid))
            return code, reply

    @staticmethod
    def _reply(sub, wu, m):
        if sub["status"] == "rejected" and sub["reason"] not in VOTE_REASONS:
            return 422, {"status": "rejected", "reason": sub["reason"], "points": 0}
        if wu["is_canary"]:  # the reply a normal WU of this project gives to a first valid submission
            if m["verify"]["mode"] == "quorum":
                return 200, {"status": "pending", "points": 0}
            return 200, {"status": "accepted", "points": m["stages"]["brain"]["points"]}
        if sub["status"] == "rejected":
            return 200, {"status": "rejected", "reason": sub["reason"], "points": 0}
        return 200, {"status": sub["status"], "points": sub["points"]}

    def _grade_canary(self, c, sub_id, output, wu, m, participant_id):
        q = m["verify"].get("quorum")
        ok = grade_canary(output, json.loads(wu["answer"]), q and q["fields"], q["digits"] if q else 3)
        if ok:  # points stay 0: canaries never pay
            c.execute("UPDATE submissions SET status = 'accepted' WHERE id = ?", (sub_id,))
            c.execute("UPDATE participants SET canary_pass = canary_pass + 1 WHERE id = ?", (participant_id,))
            return
        c.execute("UPDATE submissions SET status = 'rejected', reason = 'canary' WHERE id = ?", (sub_id,))
        self._canary_fail(c, m, participant_id)

    @staticmethod
    def _canary_fail(c, m, participant_id):
        fails = c.execute("UPDATE participants SET canary_fail = canary_fail + 1 WHERE id = ? RETURNING canary_fail",
                          (participant_id,)).fetchone()[0]
        if fails >= m["canary"]["quarantine_after"]:
            # shortcut: results already accepted with this participant's votes stay (spec 4.2); re-verify them later
            c.execute("UPDATE participants SET status = 'quarantined' WHERE id = ?", (participant_id,))
            c.execute("UPDATE submissions SET status = 'rejected', reason = 'quarantined'"
                      " WHERE participant_id = ? AND status = 'pending'", (participant_id,))
            c.execute("UPDATE leases SET status = 'released', reason = 'quarantined'"
                      " WHERE participant_id = ? AND status = 'active'", (participant_id,))

    @staticmethod
    def _quorum(c, wu_id, m, now):
        q, pts = m["verify"]["quorum"], m["stages"]["brain"]["points"]
        wu = c.execute("SELECT replicas FROM work_units WHERE id = ?", (wu_id,)).fetchone()
        subs = c.execute("SELECT id, family, quorum_key, output FROM submissions WHERE wu_id = ? AND"
                         " status IN ('pending','accepted') ORDER BY id", (wu_id,)).fetchall()
        if len(subs) < wu["replicas"]:
            return
        groups = {}
        for s in subs:
            groups.setdefault(s["quorum_key"], []).append(s)
        wins = [g for g in groups.values() if len(g) >= q["agree"]
                and (not q["distinct_families"] or len({s["family"] for s in g}) >= q["agree"])]
        if wins:
            win = max(wins, key=len)
            ids = {s["id"] for s in win}
            for s in subs:
                c.execute("UPDATE submissions SET status = ?, points = ?, reason = ? WHERE id = ?",
                          ("accepted", pts, None, s["id"]) if s["id"] in ids else ("rejected", 0, "quorum_lost", s["id"]))
            c.execute("UPDATE work_units SET status = 'done', result = ?, done_at = ? WHERE id = ?",
                      (win[0]["output"], now, wu_id))
        elif wu["replicas"] < q["max_replicas"]:
            c.execute("UPDATE work_units SET replicas = replicas + 1 WHERE id = ?", (wu_id,))
        else:
            c.execute("UPDATE submissions SET status = 'rejected', reason = 'disputed' WHERE wu_id = ? AND"
                      " status IN ('pending','accepted')", (wu_id,))
            c.execute("UPDATE work_units SET status = 'disputed', done_at = ? WHERE id = ?", (now, wu_id))

    # ---------- reads ----------

    def me(self, participant_id):
        p = self._read("SELECT name, role, status FROM participants WHERE id = ?", (participant_id,))[0]
        points = self._read("SELECT COALESCE(SUM(points), 0) FROM submissions WHERE participant_id = ?",
                            (participant_id,))[0][0]
        leases = self._read("SELECT id FROM leases WHERE participant_id = ? AND status = 'active' AND expires_at >= ?"
                            " ORDER BY issued_at, id", (participant_id, self.now()))
        return {**dict(p), "points": points, "active_leases": [r[0] for r in leases]}

    def leaderboard(self, project_id=None):
        where, args = ("WHERE w.project_id = ?", (project_id,)) if project_id else ("", ())
        rows = self._read("SELECT p.name, SUM(s.points) AS points FROM submissions s JOIN participants p ON"
                          f" p.id = s.participant_id JOIN work_units w ON w.id = s.wu_id {where}"
                          " GROUP BY p.id ORDER BY points DESC, p.name", args)
        return [dict(r) for r in rows]

    def export(self, participant_id, project_id):
        """participant_id None is the admin view of the server CLI."""
        proj = self._read("SELECT owner_id FROM projects WHERE id = ?", (project_id,))
        if not proj:
            raise ApiError(404, "unknown_project")
        if participant_id is not None:
            role = self._read("SELECT role FROM participants WHERE id = ?", (participant_id,))
            if participant_id != proj[0]["owner_id"] and not (role and role[0]["role"] == "admin"):
                raise ApiError(403, "forbidden")
        with self._tx("DEFERRED"):  # one snapshot, so a row never mixes two states; WAL keeps writers going
            return self._export_rows(project_id)

    def _export_rows(self, project_id):
        out = []
        for w in self._read("SELECT * FROM work_units WHERE project_id = ? AND is_canary = 0 ORDER BY created_at, id",
                            (project_id,)):
            releases = dict(self._read("SELECT reason, count(*) FROM leases WHERE wu_id = ? AND status = 'released'"
                                       " GROUP BY reason ORDER BY reason", (w["id"],)))
            subs = self._read("SELECT p.name, s.family, s.model, s.status, s.reason, s.points, s.output FROM"
                              " submissions s JOIN participants p ON p.id = s.participant_id WHERE s.wu_id = ?"
                              " ORDER BY s.id", (w["id"],))
            out.append({"wu": w["id"], "ref": w["ref"], "status": w["status"], "payload": json.loads(w["payload"]),
                        "result": w["result"] and json.loads(w["result"]), "attempts": w["attempts"],
                        "releases": releases,
                        "submissions": [{"participant": s["name"], "family": s["family"], "model": s["model"],
                                         "status": s["status"], "reason": s["reason"], "points": s["points"],
                                         "output": json.loads(s["output"])} for s in subs]})
        return out


def load_project_dir(path):
    """Read DIR/rally.json and the units and canaries JSONL files it names (paths relative to DIR)."""
    base = Path(path)
    try:
        manifest = json.loads((base / "rally.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ManifestError(f"rally.json: {e}") from None

    def jsonl(key):
        name = manifest.get(key) if isinstance(manifest, dict) else None
        if not name:
            return []
        try:
            lines = (base / name).read_text(encoding="utf-8").splitlines()
            return [json.loads(line) for line in lines if line.strip()]
        except (OSError, ValueError) as e:
            raise ManifestError(f"{key}: {e}") from None

    return manifest, jsonl("units"), jsonl("canaries")


# ---------- HTTP ----------


class Handler(BaseHTTPRequestHandler):
    timeout = 30
    server_version = "rally-server"

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        coord = self.server.coord
        try:
            code, body = self._route(coord, method)
        except ApiError as e:
            code, body = e.code, {"error": e.error, **({"reason": e.reason} if e.reason else {})}
        except Exception:  # noqa: BLE001 - the client gets no detail, the log gets all of it
            traceback.print_exc(file=sys.stderr)
            code, body = 500, {"error": "internal"}
        finally:
            coord.close()
        self.send_response(code)
        if code == 204:
            self.end_headers()
            return
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_header("Content-Type", "application/x-ndjson" if isinstance(body, str) else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = self.headers.get("Content-Length")
        if n is None:
            raise ApiError(411, "length_required")
        if not (n.isascii() and n.isdigit()):
            raise _bad("bad Content-Length")
        if int(n) > MAX_BODY:
            raise ApiError(413, "too_large")
        try:
            body = json.loads(self.rfile.read(int(n)), parse_constant=_no_constant, parse_float=_finite)
        except (ValueError, RecursionError):
            raise ApiError(400, "bad_json") from None
        if _too_deep(body):
            raise ApiError(400, "bad_json", "nested too deep")
        if not isinstance(body, dict):
            raise ApiError(400, "bad_json", "body must be a JSON object")
        return body

    def _who(self, coord):
        auth = self.headers.get("Authorization", "")
        me = coord.auth(auth[7:]) if auth.startswith("Bearer ") else None
        if me is None:
            raise ApiError(401, "unauthorized")
        return me

    def _route(self, coord, method):
        url = urllib.parse.urlsplit(self.path)
        if (method, url.path) == ("GET", "/healthz"):
            return 200, {"ok": True}
        if (method, url.path) == ("POST", "/v1/participants"):
            return self._register(coord)
        if not url.path.startswith("/v1/"):
            raise ApiError(404, "not_found")
        me = self._who(coord)
        match method, url.path[4:].split("/"):
            case "GET", ["me"]:
                return 200, coord.me(me["id"])
            case "GET", ["projects"]:
                return 200, coord.projects()
            case "GET", ["projects", pid]:
                return 200, coord.project(pid)
            case "GET", ["projects", pid, "export"]:
                return 200, "".join(json.dumps(r) + "\n" for r in coord.export(me["id"], pid))
            case "GET", ["leaderboard"]:
                return 200, coord.leaderboard(urllib.parse.parse_qs(url.query).get("project", [None])[0])
            case "POST", ["lease"]:
                b = self._body()
                got = coord.lease(me["id"], b.get("projects"), b.get("offers"), b.get("max_ttl_s"))
                return (204, None) if got is None else (200, got)
            case "POST", ["leases", lid, "heartbeat"]:
                self._body()
                return 200, coord.heartbeat(me["id"], lid)
            case "POST", ["leases", lid, "release"]:
                return 200, coord.release(me["id"], lid, self._body().get("reason"))
            case "POST", ["leases", lid, "submit"]:
                b = self._body()
                if "output" not in b:
                    raise _bad("body needs output")
                return coord.submit(me["id"], lid, b["output"], b.get("usage"))
        raise ApiError(404, "not_found")

    def _register(self, coord):
        if self.headers.get("Authorization"):
            if self._who(coord)["role"] != "admin":
                raise ApiError(403, "role")
            b = self._body()
            token = coord.add_participant(b.get("name"), b.get("role", "worker"))
            return 200, {"id": coord.auth(token)["id"], "token": token}
        b = self._body()
        if "invite" not in b:
            raise ApiError(401, "unauthorized")
        return 200, coord.register(b.get("name"), b["invite"])


class RallyServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128  # many workers connect at once after a quota reset


def make_server(coord, host="127.0.0.1", port=8787):
    srv = RallyServer((host, port), Handler)
    srv.coord = coord
    return srv


# ---------- CLI ----------


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rally-server")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s = sub.add_parser("add-participant")
    s.add_argument("name")
    s.add_argument("--role", choices=ROLES, default="worker")
    sub.add_parser("invite")
    sub.add_parser("add-project").add_argument("dir")
    for name in ("pause", "resume", "close", "export"):
        sub.add_parser(name).add_argument("project")
    for p in sub.choices.values():
        p.add_argument("--db", required=True)
    a = ap.parse_args(argv)
    coord = Coordinator(a.db)
    try:
        if a.cmd == "serve":
            srv = make_server(coord, a.host, a.port)
            print(f"rally-server on http://{a.host}:{srv.server_address[1]}", file=sys.stderr)
            with contextlib.suppress(KeyboardInterrupt):
                srv.serve_forever()
        elif a.cmd == "add-participant":
            print(coord.add_participant(a.name, a.role))
        elif a.cmd == "invite":
            print(coord.create_invite())
        elif a.cmd == "add-project":
            print(coord.add_project(*load_project_dir(a.dir)))
        elif a.cmd == "export":
            for row in coord.export(None, a.project):
                print(json.dumps(row))
        else:
            coord.set_project_status(a.project, {"pause": "paused", "resume": "active", "close": "closed"}[a.cmd])
    except (ApiError, ManifestError) as e:
        print(f"rally-server: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
