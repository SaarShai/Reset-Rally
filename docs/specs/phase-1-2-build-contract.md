# Phase 1–2 build contract

Status: binding for the 2026-11-08 rebuild. Owner: Saar.

This contract fixes the interfaces, the behavior and the tests for Phases 1 and 2 of [architecture-and-build-plan.md](architecture-and-build-plan.md) (the "spec"). Where this contract is silent, the spec decides. Where the spec and [test-plan-and-5-participant-rally.md](test-plan-and-5-participant-rally.md) disagree, the spec decides: there is **no Trust Score**; canaries move `canary_pass` / `canary_fail`, and `canary.quarantine_after` decides quarantine.

## 0. Scope

In: track `brain`, stage `brain`, mode `json`, verify modes `deterministic` and `quorum`, canaries, quarantine, adapters `claude`, `codex`, `agy`, `opencode`, `fake`, worker commands `init`, `doctor`, `join`, `leave`, `run`, `status`, server CLI `serve`, `add-participant`, `invite`, `add-project`, `pause`, `resume`, `close`, `export`.

Out (later phases; reject cleanly, do not stub): tracks `brawn` and `relay`, stages `brawn` and `verify`, modes `patch` and `text`, verify mode `holdout`, harvest windows and `state.json` (Phase 3), heartbeat thread (Phase 3), Docker exec, YAML manifests, rate limits per token, backup. `add-project` raises `ManifestError` for any out-of-scope value, naming the field.

Rules for all code:
- Python ≥ 3.11, stdlib only. Size targets: `checks.py` ≤ 260 lines (250 before the review added three quorum rules), `server.py` ≤ 800, `worker.py` ≤ 600.
- **Generic code.** `src/` never names a fixture project, participant, WU, time or expected result, and imports only `rally.*` and the stdlib. Every value comes from the manifest, the database, the config or the request. `tests/test_generic_src.py` enforces this.
- **Test names carry `tst`.** Every test-only identifier (project ids, unit refs, participant names) in `tests/` contains the token `tst` (for example `tst-quorum`, `tst-w1`). `tests/test_generic_src.py` fails on any `tst` token in `src/`.
- Tests use pytest, plain functions, no network beyond `127.0.0.1`, no real AI CLI, no real sleep longer than 0.2 s. Each test checks behavior through the public API (Coordinator methods, HTTP, worker functions), never by writing SQL rows to fake a state, except where a test name says `forced_db_state`.

## 1. Files

| File | Owner slice |
|---|---|
| `src/rally/__init__.py` | docstring only |
| `src/rally/checks.py`, `tests/conftest.py`, `tests/test_checks.py`, `tests/test_generic_src.py` | A |
| `src/rally/server.py`, `tests/test_coordinator.py`, `tests/test_http.py`, `tests/test_concurrency.py` | B1 |
| `src/rally/worker.py`, `tests/test_worker.py` | B2 |
| `tests/test_e2e.py`, `tests/fixtures/`, `examples/rct-outcomes/`, `README.md` run section | C |

## 2. `checks.py` (pure functions)

```python
class ManifestError(ValueError): ...           # message starts with the field path, e.g. "verify.quorum.agree: ..."

def validate_manifest(m: dict) -> dict          # returns a new dict with defaults filled; raises ManifestError
def public_manifest(m: dict) -> dict            # copy without "canaries"
def manifest_hash(m: dict) -> str               # "sha256:" + sha256(json.dumps(public_manifest(m), sort_keys=True, separators=(",", ":")))
def render_prompt(stage: dict, payload: dict) -> str
def check_schema(value, schema: dict, path: str = "$") -> str | None   # None = ok, else "schema: <path>: <why>"
def run_checks(output: dict, payload: dict, checks: list[dict]) -> str | None  # None = ok, else "check:<type>:<field>"
def normalize(value, digits: int = 3)
def quorum_key(output: dict, fields: list[str], digits: int) -> str  # sha256 hex
def grade_canary(output: dict, answer: dict, fields: list[str] | None, digits: int) -> bool
```

`validate_manifest` rules (each failure names its field):
- `id`: matches `^[a-z0-9][a-z0-9-]{0,63}$`. `version`: int ≥ 1. `title`, `owner`: non-empty strings.
- `track`: `brain`. `stages`: exactly `{"brain": ...}`.
- `stages.brain`: `mode` = `json`; `prompt` non-empty string; `schema` a dict that uses only the keywords `type, properties, required, enum, items, minimum, maximum, minLength, maxLength, minItems, maxItems` (checked recursively); `needs.families` optional list of strings; `timeout_s` int > 0 (default 900); `lease_ttl_s` int ≥ `timeout_s` (default 1800); `points` int ≥ 0 (default 1).
- `units`, `canaries`: optional file names (non-empty strings) that `load_project_dir` reads.
- `verify.mode`: `deterministic` or `quorum`. `verify.max_attempts`: int ≥ 1 (default 3). `verify.checks`: list (default `[]`) of `{type, field, ...}` with `type` in `regex` (`pattern`), `exact` (`value`), `numeric` (`min` and/or `max`), `quote_span` (`source`), `hash` (`sha256`); missing argument → error.
- `verify.quorum` (required when mode is `quorum`): `replicas` int ≥ 2; `agree` int, 2 ≤ agree ≤ replicas; `distinct_families` bool (default true); `fields` non-empty list of keys of `stages.brain.schema.properties`; `max_replicas` int ≥ replicas and < 2 × agree, so only one group can win (default `min(replicas + 1, 2 × agree − 1)`); with `distinct_families` and `stages.brain.needs.families` set, `len(set(families)) ≥ max_replicas`; `replicas ≤ 2 × agree − 1`; `needs.families`, when given, is a non-empty list, or a tie-break could never find a family (error names `verify.quorum.max_replicas`); `digits` int 1..15 (default 3).
- `canary`: `rate` number in [0, 1] (default 0); `quarantine_after` int ≥ 1 (default 2).
- Any other top-level key, or `relay`, `holdout`, `brawn`, `patch`, `text` values → error.

`render_prompt`: `string.Template(stage["prompt"]).safe_substitute(payload)`, where dict and list values become `json.dumps(v)`. Then append `"\n\nReply with one JSON object only. It must match this JSON schema:\n" + json.dumps(schema, sort_keys=True)`.

`check_schema`: types `object, array, string, number, integer, boolean, null`; `type` may be a list. `bool` is never `integer` or `number`. A float with no fraction (`120.0`) is an `integer`, as in JSON Schema. Unknown properties are allowed. It must not raise on any JSON value: guard depth (reject deeper than 32 levels with `"schema: <path>: too deep"`).

`run_checks`: `regex` → value is a string and `re.fullmatch(pattern, value)`; `exact` → `normalize(value) == normalize(arg)`; `numeric` → a number (not bool) inside `[min, max]`; `quote_span` → every string in the field (a string or a list of strings) is non-empty and, after `" ".join(s.split())` on both sides, a substring of `payload[source]`; `hash` → `sha256(value.encode()).hexdigest() == sha256`. A missing field fails its check.

`normalize`: strings → NFC, `" ".join(s.split())`, casefold. `bool` and `None` unchanged. `int` unchanged. `float` → `float(f"{x:.{digits}g}")`, then to `int` when the result is integral and `abs < 2**53` (so `12.0 == 12` and `3.30 == 3.3`). Lists → normalized items sorted by `json.dumps(item, sort_keys=True)`. Dicts → normalized values. `quorum_key` hashes `json.dumps({f: normalize(output.get(f), digits) for f in fields}, sort_keys=True)`. `grade_canary` compares `normalize` of `answer[f]` and `output.get(f)` for each `f` in `fields`, or for every key of `answer` when `fields` is None.

## 3. Coordinator (`server.py`)

Schema: spec §2 DDL, minus the relay-only columns (`projects.wave`, `work_units.parent_id`, `source_sub_id`, `wave`, `score`; `submissions.score`, `holdout_score`), keep `CREATE UNIQUE INDEX lease_once ON leases (wu_id, participant_id)`, plus:
- `work_units.ref TEXT` — the owner's unit id from `units.jsonl` (`"id"` key). Never sent to workers. Shown in export.
- `CREATE TABLE invites (code_hash TEXT PRIMARY KEY, used_by INTEGER REFERENCES participants(id), created_at INTEGER NOT NULL)`.

Connections: one per thread (`threading.local`), `isolation_level=None`, WAL, `busy_timeout=5000`, `foreign_keys=ON`. Every write runs in `BEGIN IMMEDIATE … COMMIT` (ROLLBACK on error). No background threads.

```python
class ApiError(Exception):                     # .code: int (HTTP), .error: str, .reason: str | None
class Coordinator:
    def __init__(self, db_path, now=time.time, want_canary=None)
        # want_canary(participant_name, project_id, rate) -> bool; default: random.random() < rate
    def add_participant(self, name, role="worker") -> str          # token; ApiError(409,"name_taken") if the name exists
    def create_invite(self) -> str                                   # one-use code
    def register(self, name, invite) -> dict                         # {"id", "token"}; role worker; ApiError 403 bad/used invite, 409 name taken
    def auth(self, token) -> dict | None                             # {"id","name","role","status"} by sha256(token)
    def add_project(self, manifest, units, canaries) -> str          # validate_manifest; owner must exist with role owner|admin
    def set_project_status(self, project_id, status)                 # active | paused | closed
    def lease(self, participant_id, projects, offers, max_ttl_s=None) -> dict | None
    def heartbeat(self, participant_id, lease_id) -> dict            # {"expires_at"}
    def release(self, participant_id, lease_id, reason) -> dict      # {"ok": True}
    def submit(self, participant_id, lease_id, output, usage=None) -> tuple[int, dict]
    def me(self, participant_id) -> dict      # {"name","role","status","points","active_leases":[ids]}; no canary counters
    def projects(self) -> list[dict]          # active: id, title, track, version, manifest_hash, progress {status: count} (canaries excluded)
    def project(self, project_id) -> dict     # public_manifest + "manifest_hash"; ApiError 404
    def leaderboard(self, project_id=None) -> list[dict]   # [{"name","points"}] by points desc, then name
    def export(self, participant_id, project_id) -> list[dict]   # owner of the project or admin, else ApiError 403
def load_project_dir(path) -> tuple[dict, list[dict], list[dict]]  # reads rally.json; units/canaries paths are relative to it
```

Tokens: `secrets.token_urlsafe(32)`; store `sha256` hex only. Invite codes the same way.

Units: each line `{"id"?, "payload": {...}, "priority"?}` → one WU (`stage='brain'`, random 16-hex `id`, `replicas` = quorum replicas or 1). Canary lines `{"payload", "answer"}` → WU with `is_canary=1`, `answer` JSON. `add_project` on an existing id: allowed only with a higher `version`; it replaces `manifest`, `manifest_hash`, `version` and adds no units. A `canary.rate > 0` with no canaries → `ManifestError("canaries: ...")`.

**Lease** (one transaction; spec §4.1 with these exact rules):
1. Expire: `status='expired'` for active leases with `expires_at < now`.
2. Participant not `active` → `ApiError(403, "not_active")`. Role must be `worker` or `owner`, else 403 `role`.
3. Keep only joined projects that are `active` and whose `manifest_hash` equals the pinned hash. None left → return None.
4. (Canaries, applied to the WU that step 5 picks.) If that WU's project has `canary.rate > 0` and `want_canary(name, project_id, rate)` is true, grant instead, with the same offer, the oldest canary of that project that this participant never leased; when none is left, grant the step 5 WU. `want_canary` is called once per lease call that finds a normal WU, never otherwise. A canary only takes the place of real work, so a lease never proves a canary. Canaries ignore the slot rule and the distinct-family rule, and stay `open`.
5. Run the spec §4.1 candidate query (status `open`, `is_canary = 0`, never leased by me, free slot, `ORDER BY priority DESC, has-submissions DESC, created_at, id`, no `LIMIT`: iterate the rows and stop at the first WU that a matching offer fits), with joined projects in request order first (`ORDER BY` project position before the rest).
6. Offer match, in offer order: `offer.kind == "llm"`, `"json" in offer.modes`, family in `needs.families` when set, `stage.timeout_s <= max_ttl_s` when given; for quorum with `distinct_families`, the family is not among the families of the WU's active leases and its `pending`/`accepted` submissions.
7. Insert the lease (`adapter`, `family`, `model` from the offer) with `expires_at = now + min(lease_ttl_s, max_ttl_s + 60)`. Update `participants.last_seen` and `capabilities`.

Response: `{"lease_id", "expires_at", "heartbeat_s": min(300, ttl // 3), "adapter", "wu": {"id","project","manifest_hash","stage","mode","prompt","schema","timeout_s","workspace": None,"image": None,"command": None,"files": {}}}`. The response is identical in shape for canaries. The word `canary` never appears in it.

**Ownership.** `heartbeat`, `release` and `submit` raise `ApiError(404, "unknown_lease")` when the lease does not exist or belongs to another participant. A non-active participant gets 403 on all three.

**Heartbeat**: lease `active` and `expires_at >= now` → `expires_at = now + lease_ttl_s`. Otherwise mark it `expired` if it was active, then `ApiError(409, "lease_not_active")`.

**Release**: lease `active` → `released` with `reason` (≤ 64 chars). Already not active → `ApiError(409, "lease_not_active")`.

**Submit** (one transaction):
1. Lease `submitted` → return the first reply again, unchanged (idempotent). Store it as `submissions.reply` (JSON of code and body).
2. Lease `active` and `expires_at >= now` → go on if the WU is `open` (else `ApiError(409, "wu_closed")`; canaries are always `open`). Lease `active` with `expires_at < now`, or `expired`: go on only if the WU is `open`, has a free slot (slot rule without this lease) and, for quorum with `distinct_families`, the lease family is not among the families of the WU's active leases and `pending`/`accepted` submissions; else `ApiError(409, "lease_expired")`. A late submit on a canary always goes on. `released` → 409 `lease_not_active`.
3. `len(json.dumps(output)) > 262144` → reason `size`. `output` not a dict → reason `schema: $: not an object`. Then `check_schema`, then `run_checks`. On failure: insert the submission `rejected` with the reason, lease `submitted`, `attempts += 1`; at `attempts >= verify.max_attempts` the WU → `failed` and its `pending` submissions → `rejected` reason `failed`. Return `(422, {"status": "rejected", "reason": r, "points": 0})`. On a canary the attempts count is skipped, and the reject counts as a canary failure (`canary_fail += 1`, quarantine check).
4. Insert the submission `pending`, `quorum_key` set in quorum mode; lease → `submitted`.
5. Canary: `grade_canary` (quorum fields if quorum mode, else all answer keys). Pass → submission `accepted`, `points` = 0, `canary_pass += 1`. Canaries never pay points. Fail → submission `rejected` with reason `canary`, `canary_fail += 1`; at `canary_fail >= quarantine_after` → **quarantine** (below). In both cases return the reply that a normal WU of this project gives to a first valid submission: quorum → `(200, {"status": "pending", "points": 0})`; deterministic → `(200, {"status": "accepted", "points": <stage points>})`. Pass or fail, the reply is the same.
6. Deterministic: submission `accepted`, `points` = stage points, WU `done`, `result` = output, `done_at`. Return `(200, {"status": "accepted", "points": p})`.
7. Quorum: when `pending` + `accepted` submissions reach `replicas`, group them by `quorum_key`. A group with ≥ `agree` members from ≥ `agree` distinct families (or members, when `distinct_families` is false) wins: its members → `accepted` with stage points, the others → `rejected` reason `quorum_lost`, WU `done` with `result` = the first winner's output. No winner and `replicas < max_replicas` → `replicas += 1` (WU stays `open`). No winner at `max_replicas` → all → `rejected` reason `disputed`, WU `disputed`. Return the caller's own submission status: `accepted` with its points, `rejected` (HTTP 200, reason `quorum_lost` or `disputed`), or `pending`.

**Quarantine** (inside the same transaction): participant → `quarantined`; their `pending` submissions → `rejected` reason `quarantined` (this frees the slots); their active leases → `released` reason `quarantined`. Accepted results stay (spec §4.2; known limit, see §7).

Points live only in `submissions.points` (no ledger table). `me.points` and `leaderboard` are `SUM(points)` over the participant's submissions (per project for `leaderboard(project_id)`).

**Export** rows (every non-canary WU, any status; all reads in one read transaction), ordered by `created_at, id`: `{"wu": id, "ref", "status", "payload", "result", "attempts", "releases": {reason: count}, "submissions": [{"participant", "family", "model", "status", "reason", "points", "output"}]}`.

### HTTP (`ThreadingHTTPServer`, `daemon_threads = True`)

Routes exactly as spec §4: `GET /healthz`, `POST /v1/participants`, `GET /v1/me`, `GET /v1/projects`, `GET /v1/projects/{id}`, `POST /v1/lease`, `POST /v1/leases/{id}/heartbeat`, `POST /v1/leases/{id}/release`, `POST /v1/leases/{id}/submit`, `GET /v1/leaderboard?project=`, `GET /v1/projects/{id}/export` (JSONL body, `application/x-ndjson`).

- Auth: `Authorization: Bearer <token>` on all but `/healthz`; `POST /v1/participants` accepts either an admin token (`{name, role}`) or `{name, invite}` without a token. Bad token → 401.
- Body: `Content-Length` required for POST (411 when missing); above 1 MiB → 413 without reading it; not JSON, `NaN`/`Infinity`, not an object, or nested too deep → 400. Handler socket timeout 30 s.
- `ApiError` → its code with `{"error", "reason"?}`. Unknown route → 404. Unexpected exception → 500 `{"error": "internal"}` (log the traceback to stderr, never to the client).
- Lease: request `{"projects": {id: hash}, "offers": [...], "max_ttl_s"?}`; None → 204 with no body.
- Submit: request `{"output", "usage"?}`; reply `(code, body)` from `Coordinator.submit`.

CLI (`rally-server`): `serve --db PATH [--host 127.0.0.1] [--port 8787]`, `add-participant NAME [--role worker|owner|admin] --db PATH` (prints the token once), `invite --db PATH`, `add-project DIR --db PATH`, `pause|resume|close PROJECT --db PATH`, `export PROJECT --db PATH` (JSONL to stdout, admin view).

## 4. Worker (`worker.py`)

Config: `$RALLY_HOME/worker.json` (default `~/.rally/worker.json`), written with mode 600: `{"server", "token", "projects": {id: hash}, "adapters": {name: {...}}}`. An adapter entry: `{"type"?: name, "family"?, "model"?, "max_tasks"?, "argv"?, ...}`. `argv` (a list of strings) replaces the table argv; in it `{prompt}`, `{model}`, `{tmp}` and `{timeout_s}` are replaced; the prompt still goes on stdin for stdin types. `type` defaults to the entry name; type `fake` takes `{"answers": {substring: output-or-text} | "path.json", "limit_after"?: N}`.

```python
class ApiError(Exception): code, body
class Client:                                    # urllib; JSON in/out; Bearer token; timeout 30 s
    def __init__(self, server, token=None)
    def get(self, path) -> dict | list; def post(self, path, body) -> dict | None   # None on 204
    # 204 → None; other non-2xx (3xx too: no redirect is followed) → ApiError(code, parsed body)
def family_of(acfg: dict, name: str) -> str
def offers(cfg, names=None) -> list[dict]       # [{"adapter","kind":"llm","family","model","modes":["json"]}]
def build_argv(atype, acfg, wu, tmpdir) -> tuple[list[str], str | None]   # (argv, stdin text)
def run_adapter(name, acfg, wu, runner=subprocess.run, fake_state=None) -> tuple[str, str, dict]
    # status "ok" | "rate_limited" | "error"; usage {"seconds": float} (+ "error": short text)
def extract_json(text) -> dict | None
class Worker:
    def __init__(self, cfg, client, names=None, max_tasks=None, runner=subprocess.run, sleep=time.sleep, log=print, now=time.time)
    def step(self) -> bool                       # one lease → job → submit/release cycle; False = stop
    def run(self, once=False)
```

Adapter table (json mode, cwd = a fresh empty temp dir, env = the participant's env unchanged; the worker adds and logs nothing):

| type | argv | prompt | text | rate-limit regex (case-insensitive) |
|---|---|---|---|---|
| `claude` | `claude -p --model M --tools "" --strict-mcp-config --no-session-persistence --output-format text` | stdin | stdout | `usage limit\|limit reached\|rate.?limit\|5-hour limit\|weekly limit` |
| `codex` | `codex exec --sandbox read-only --skip-git-repo-check --ephemeral -C TMP -o TMP/last.txt` + `--disable F` for each tool feature in `CODEX_TOOLS_OFF` (`shell_tool`, `unified_exec`, `code_mode_host`, `apps`, `plugins`, `browser_use`, `computer_use`, `multi_agent`, `view_image`, `image_generation`) + `-c mcp_servers={}` `[-m M] -` | stdin | `TMP/last.txt` | `usage limit\|rate.?limit\|429\|try again in` |
| `agy` | `agy --print=PROMPT --model M --mode plan --sandbox --print-timeout {timeout_s}s` | argv (`--print=` form) | stdout | `quota reached\|resource_exhausted\|rate.?limit\|429` |
| `opencode` | `opencode run --agent plan -m M PROMPT` | argv; a prompt that starts with `-` gets a leading space | stdout | `usage limit\|rate.?limit\|429\|quota` |
| `fake` | none (in process) | — | the matched answer | after `limit_after` calls → `rate_limited` |

`--model`/`-m` is left out when the adapter has no `model`. Prompts above 200 KB → `error` without running. `runner(argv, input=stdin, cwd=tmp, capture_output=True, text=True, timeout=wu["timeout_s"])`; `TimeoutExpired` or `OSError` → `error`. For argv-prompt types (`agy`, `opencode`) the cap is 100 KB (Linux limits one argument to 128 KiB).

Classification: rc 0 and `extract_json(text)` finds an object → `ok`. Else, if the regex matches stdout + stderr → `rate_limited`. Else → `error`.

`family_of`: `acfg["family"]` when set; else from the model id (`claude`→anthropic, `gemini`→google, `gpt`/`o3`/`o4`→openai, `deepseek`→deepseek, `qwen`→qwen, `llama`→llama); else the type default (`claude`→anthropic, `codex`→openai). `agy` or `opencode` with neither → `ValueError` at load.

`extract_json`: the last top-level JSON object in the text: scan left to right; at each `{` try `json.JSONDecoder().raw_decode`; on a dict, remember it and jump past it; otherwise go to the next `{`.

`Worker.step`:
1. Usable adapters: not disabled, not `blocked_until > now`, `done < max_tasks` (adapter or global). None → return False.
2. `POST /v1/lease` with `cfg["projects"]` and the usable offers. None (204) → sleep with back-off (30 s doubling to 300 s; reset after a lease), return True. 403 → log and return False.
3. `wu.manifest_hash` differs from the pinned hash → release `manifest_changed`, log "run `rally-worker join <project>` again", return True.
4. `run_adapter`. `ok` → `check_schema(out, wu["schema"])`; pass → submit; fail → release `error`. `rate_limited` → release `rate_limited`, `blocked_until = now + 3600`. `error` → release `error`, `consecutive_errors += 1`; 3 in a row disables that adapter for this run.
5. Any other exception during a job → release `error` and count it as an error. A refused submit (any 4xx, such as a 422 from a server check) counts as an error too, so an adapter whose output always fails the server checks gets disabled. The `Client` follows no HTTP redirect (the token must never go to another URL). A 409 on submit or release → drop the lease, no retry. `KeyboardInterrupt` (also raised on `SIGTERM`) during a job → release `stop`, re-raise. `run()` catches it and exits 0.

Drift: at the start of `run`, and after 10 replies of 204 in a row, `GET /v1/projects`; for each pinned project that is missing or whose hash differs, log "run `rally-worker join <project>` again".

CLI (`rally-worker`): `init --server URL (--name NAME --invite CODE | --token TOKEN)`, `doctor` (for each of claude, codex, agy, opencode on `PATH`: run only `--version`, timeout 10 s; print an adapters block), `join PROJECT [--yes]` (print title, track, stage mode, families, prompt template, verify mode; ask y/n unless `--yes`; pin the hash), `leave PROJECT`, `run [--once] [--max-tasks N] [--adapter NAME ...]`, `status` (`GET /v1/me`).

## 5. Tests (all must fail before the code exists, and pass after)

`conftest.py`: `FakeClock` (`now()`, `advance(s)`), `make_manifest(**overrides)` (a valid quorum manifest built in the test, not loaded from fixtures), `coord(tmp_path, clock)` fixture, `add_people(coord)` helper.

| File | Must cover |
|---|---|
| `test_checks.py` | every `validate_manifest` rule above has one failing case naming its field; schema types incl. bool≠int, lists of types, enum, nested items, maxLength, maxItems, too deep; each check type pass and fail, missing field; `normalize` cases (NFC, casefold, whitespace, 3.30→3.3, 12.0==12, int unchanged, list order, dict order); `quorum_key` equal for `"zx-3"`/`"ZX-3"`; `grade_canary` with and without fields; `render_prompt` substitution, JSON for dicts, unknown `${x}` left as is, schema suffix; `manifest_hash` stable under key order and different after a change; `public_manifest` drops canaries. |
| `test_coordinator.py` | slot rule and `lease_once`; distinct-family routing (two `anthropic` workers never fill one quorum WU); offer matching (family, mode, `max_ttl_s`); hash pin mismatch → None; project order; expiry boundary (`expires_at == now` still active); late submit accepted while a slot is free, 409 when taken; release frees the slot at once; heartbeat extends, 409 after expiry; ownership 404 on submit/release/heartbeat; idempotent submit; 422 reasons (`size`, `schema`, each check) and `failed` at 3 attempts; a `failed` WU rejects its pending votes (`failed`) and refuses a live submit (409 `wu_closed`) and a late one (409 `lease_expired`); a version bump never lets a live lease pay twice; quorum waits for all `replicas` votes when `replicas > agree`; the has-submissions order and request order beat a later WU and a higher priority in a later project; deterministic accept and points; quorum agree; conflict → `replicas` 3 → tie-breaker must be a third family → `done`, minority `quorum_lost` with 0 points; three-way → `disputed`, 0 points; `forced_db_state` same-family agreement does not commit; canary: chosen only when `want_canary` says so, graded silently, submit reply for a canary == reply for a first quorum submit, lease payload key set, types and all non-content values equal for canary and normal WU, no `canary` text; no canary when this participant has no normal work in the project; a canary needs a matching offer; quorum canaries graded on quorum fields only; deterministic canaries graded on all answer fields; pass gives 0 points; `quarantine_after` failures → quarantined, pending submissions rejected and slots re-open, active leases released, lease/submit → 403; ledger: points sum equals accepted non-canary submissions × stage points after a mixed run; family starvation: more than 50 open quorum WUs that already hold a vote of family X, and one free WU after them → a second family-X worker still gets the free WU; two same-family workers both get the same canary; a late submit of a same-family vote is refused; a 422 on a canary counts as a canary failure; `max_attempts` from the manifest; a replayed submit returns the stored first reply even after the WU is done. |
| `test_http.py` | real server on port 0: healthz; invite register; admin creates owner; no-token register without invite → 401; name taken → 409; bad token → 401; projects list and public project view (no canaries); lease → submit → `me` points; 204 when no work; 411, 413 (body above 1 MiB), 400 (bad JSON, `NaN`/`Infinity`, deep nesting); 404 route; a worker or owner token cannot create participants (403 `role`); export: owner 200 JSONL, other worker 403; leaderboard; `PRAGMA journal_mode` = `wal`. |
| `test_concurrency.py` | 20 threads with distinct participants of 4 families lease from one quorum project of 100 WUs (`replicas` 2) until 204; no WU ever has more active leases + live submissions than `replicas`; no client sees an error; at the end `leases` holds exactly 200 rows, every WU has exactly 2 leases from 2 participants of 2 families. |
| `test_worker.py` | `extract_json` (prose, code fence, nested object, braces inside strings, two objects → last, none → None); `build_argv` for each type (exact argv, stdin vs argv, `--print=` form, leading-dash guard, no `-m` without a model); `run_adapter` through a real tiny shim script (subprocess) that records argv and stdin: prompt arrives, rc 0 JSON → ok, each rate-limit sample (`Claude usage limit reached…`, `You've hit your usage limit…`, `Individual quota reached`, `Go usage limit exceeded`) → rate_limited, rc 3 → error, timeout → error; codex reads `-o` file; sentinel env (`ANTHROPIC_API_KEY=sk-SENTINEL`) reaches the subprocess but no request to the server contains `SENTINEL`; `family_of`; `Worker.step` against a stub client: 204 back-off, manifest drift → release, schema fail → release `error` and no submit, rate limit → release and blocked, 3 errors → disabled, 409 → no retry, three refused submits (422) → disabled, 401 on lease raises, `max_tasks`; the `Client` follows no redirect; codex with no `-o` file → error; argv prompts above 100 KB → error; any exception in a job → release `error`; `argv` override with placeholders; drift log after 10 replies of 204 and at start; config file mode 600; `doctor` never passes anything but `--version`. |
| `test_e2e.py` | **Phase 1:** real HTTP server + two `Worker`s with `fake` adapters (families anthropic and openai) on a deterministic project of 100 WUs → all `done`; export has 100 rows; ledger = 100 × points. **Phase 2 five-participant rally** (below). |
| `test_generic_src.py` | `src/` has no `import` of `tests`/`fixtures`, no `tst` token, and no fixture values: participant names, project ids, unit refs, payload and answer strings, part names in them (such as `ZX-3`), and the `conftest` text, quote and start time. |

**Five-participant rally** (`tests/fixtures/rally-c/`: `rally.json` quorum, `replicas` 2, `agree` 2, `distinct_families`, `fields [component, supply_v]`, `max_replicas` 3, `quote_span quote←text`, `canary.rate 0.2`, `quarantine_after` 2, families `[anthropic, openai, google, deepseek]`; units C1–C4 and canaries K1–K3 from the test plan §5 Rally C, plus C5 so that Eve still has real work when her canaries come, plus `tests/fixtures/rally-d/` deterministic sums with families `[qwen]`). `want_canary` is a scripted table in the test. Real HTTP server, `FakeClock`, real `Worker` objects with `fake` adapters for Alice (anthropic, `limit_after` 4), Bob (openai, C3 = 5.0), Carol (google, online later), Dave (qwen, rally-d only); Eve is a raw `Client` that sends a stale submit, malformed and hallucinated outputs, and lazy canary answers. A second openai account (Dave with a borrowed openai offer) gets 204 while C3, C4 and C5 each hold Bob's openai vote. Expected end state is written by hand in the test: C1, C2, C4, C5 `done`; C3 `done` = 3.3 through Carol's tie-break with Bob `quorum_lost`; Eve `quarantined` after two canary failures, 0 points, her pending vote rejected and its slot re-filled; Alice stops on the rate limit and holds no lease; each participant's points (Alice 6, Bob 8, Carol 6, Dave 3, Eve 0) and the total 23. Every number in the expected state comes from the fixture table and the stage points, worked out in a comment, never copied from a run.

## 6. Done means

`python3 -m pytest -q` green; `tests/test_generic_src.py` green; size targets met; `rally-server serve` + `rally-worker init/join/run --once` work by hand against `examples/rct-outcomes/` with a `fake` adapter; a reviewer on a different model than the authors finds no test-shaped code.

## 7. Known limits (write them as `shortcut:` comments where the code takes them)

- Quarantine does not undo results already accepted with the quarantined participant's vote (spec §4.2).
- The worker reports its own family (spec §8.2).
- After the fact, points can show which WUs were canaries (a canary never pays). The worker cannot tell before it answers.
- Colluding accounts can compare prompts: a canary prompt can reach more accounts than `replicas`. A large, private, rotated canary pool is the control (spec §8.4).
- The HTTP socket timeout applies per read, and the server has no thread limit; put a reverse proxy in front (Phase 6).
- Heartbeat does not respect `max_ttl_s` (Phase 3).
- A `failed` WU keeps its active leases until they expire; their submits get 409.
- A version bump that changes `quorum.fields`, `digits` or `schema` leaves old `quorum_key` values in pending votes.
- A network error on submit loses that job's output; `state.json` and retries come in Phase 3.
- File access: the live probe (2026-11-08, codex-cli 0.159.2) showed that `codex --sandbox read-only` alone lets the model read any file and submit it. With `CODEX_TOOLS_OFF` the same probe gets no file text. A new codex version can add tools: run `bin/live_smoke.sh 1 codex` after each upgrade. `claude --tools ""` removes file access (live probe still open: the Claude login on the test Mac had expired). `agy --mode plan` and opencode's `plan` agent are not probed yet and can still read files. Join only projects from owners you trust (ADR-0001).
