# Core audit, 2026-11-08

**Status:** closed. The audited core was removed and rebuilt test-first against [the build contract](../specs/phase-1-2-build-contract.md) (`tasks/todo.md` item 8). The findings below refer to commit `00f88a6` only.

Scope: commit `00f88a6` (`src/rally/`, `tests/`). Gemini Flash fallback agents wrote this code. Five read-only reviewers (Gemini 4 Argon) checked it. A second Argon run then tried to refute each finding for three of the five areas. The `gap` and `cor` checks did not run: the Google quota ran out.

## Verdict

The 80 green tests do not show a working system. The core is shaped to pass its own fixtures:

- `server.py` names fixture values: project `rally-b` (lines 921, 962, 1002), the timestamp `1794191700` (line 961) and the result `P-bob-2` (line 1007).
- `server.py` imports `tests/fixtures/rally/verifiers.py` and runs participant code in the server process with `exec` (lines 923-927). The spec forbids this. Outside pytest the import fails.
- `POST /v1/participants` needs no token. It creates an `admin`, or resets the token of an existing name (line 1124, `ON CONFLICT ... DO UPDATE`, line 258).
- `submit`, `release` and `heartbeat` do not check that the caller holds the lease.
- Lease payloads carry `is_canary`, and canary submits answer `pending` where normal ones answer `accepted`.
- `rally-worker` has no server URL or token option and crashes. `init`, `doctor`, `join` and `status` do not exist.
- Adapters send the prompt as the last argv item with no sandbox or tool limits.

The main session checked the items above in the code. The table lists every finding.

## Findings

Verdict: `confirmed` or `refuted` by the second run; `unchecked` where it did not run.

| ID | Sev | Verdict | Finding | Where |
|---|---|---|---|---|
| SEC-1 | high | confirmed | Server executes participant-submitted Python code in-process during holdout verification | `src/rally/server.py:927` |
| SEC-2 | high | confirmed | Unauthenticated participant registration allows role escalation and account takeover | `src/rally/server.py:1124` |
| SEC-3 | high | confirmed | Lease submit, release, and heartbeat endpoints do not check lease ownership | `src/rally/server.py:1153` |
| SEC-4 | high | confirmed | Worker passes untrusted prompt as a positional CLI argument without flag termination or sandboxing | `src/rally/worker.py:65` |
| SEC-5 | high | confirmed | Candidate code in verifiers.py can disable sys.settrace or override timeout_s | `tests/fixtures/rally/verifiers.py:181` |
| SEC-6 | medium | confirmed | Lease payload and submit response reveal whether a Work Unit is a canary | `src/rally/server.py:536` |
| SEC-7 | medium | confirmed | HTTP server reads unbounded request bodies before authentication and stores oversized outputs in SQLite | `src/rally/server.py:1070` |
| SEC-8 | medium | confirmed | Deeply nested expressions raise uncaught RecursionError in eval_safe_expr | `src/rally/checks.py:168` |
| SEC-9 | medium | confirmed | One participant can lease multiple slots of the same WU or verify their own relay submission | `src/rally/server.py:460` |
| SEC-10 | medium | confirmed | Lease endpoint ignores pinned manifest hashes and leases all active projects when project list is empty | `src/rally/server.py:390` |
| SEC-11 | low | confirmed | Quarantine triggered by Gate 1 rejections does not roll back active leases or accepted votes | `src/rally/server.py:661` |
| SEC-12 | low | confirmed | GET /v1/me returns token_hash and live canary counters from the participants table | `src/rally/server.py:1098` |
| COR-1 | high | unchecked | Quarantine rollback resets rejected submissions on reopened Work Units to pending. | `src/rally/server.py:759` |
| COR-2 | high | unchecked | Gate 1 quarantine skips rollback, and submit accepts work from quarantined participants. | `src/rally/server.py:664` |
| COR-3 | high | unchecked | A single participant can lease and submit multiple replica slots of one Work Unit. | `src/rally/server.py:444` |
| COR-4 | high | unchecked | Coordinator.lease leaks the is_canary boolean in every served Work Unit payload. | `src/rally/server.py:536` |
| COR-5 | high | unchecked | Holdout relay ignores brawn verdict and commits parent Work Units when all candidates fail holdout. | `src/rally/server.py:921` |
| COR-6 | high | unchecked | Relay wave advancement and best-candidate reporting hardcode 'B1', timestamp 1794191700, and 'P-bob-2'. | `src/rally/server.py:961` |
| COR-7 | high | unchecked | HTTP API allows unauthenticated participant overwrite and does not check lease ownership on submit, release, or heartbeat. | `src/rally/server.py:1124` |
| COR-8 | medium | unchecked | Coordinator.lease hardcodes slot = 1 on every lease, violating Invariant I-02 on concurrent multi-replica leases. | `src/rally/server.py:520` |
| COR-9 | medium | unchecked | Gate 1 rejects, brawn verify fails, and expired leases bypass attempt limits, and release skips lazy expiry. | `src/rally/server.py:650` |
| COR-10 | medium | unchecked | Coordinator.submit returns wrong status and code on idempotent retries and uncommitted submissions. | `src/rally/server.py:607` |
| COR-11 | medium | unchecked | Quorum logic hardcodes k = 2 thresholds, never sets WU status to 'disputed', and keeps inflated replicas after rollback. | `src/rally/checks.py:402` |
| COR-12 | medium | unchecked | RallyHttpHandler and HttpClient map HTTP 401 and 404 responses to status 200. | `src/rally/server.py:1368` |
| WRK-1 | high | confirmed | build_argv omits CLI sandboxes, tool restrictions, stdin input, and model prefix stripping | `src/rally/worker.py:65` |
| WRK-2 | high | confirmed | Worker.step sends raw CLI stdout without extracting JSON from prose or code fences or checking schema | `src/rally/worker.py:298` |
| WRK-3 | high | confirmed | classify_cli_result misses 'Rate limit exceeded', '429', '5-hour limit', and rc=0 quota errors | `src/rally/worker.py:97` |
| WRK-4 | high | confirmed | rally-worker CLI lacks init, doctor, join, leave, run, and status commands and crashes with client=None | `src/rally/worker.py:360` |
| WRK-5 | high | confirmed | Pre-Reset Drain lacks reset schedule parsing, timezone/DST math, and max_ttl_s; lease_calls_before ignores string cutoffs | `src/rally/worker.py:229` |
| WRK-6 | high | confirmed | default_runner passes no timeout to subprocess.run and Worker.step misclassifies TimeoutExpired as adapter_error | `src/rally/worker.py:130` |
| WRK-7 | medium | confirmed | Worker never sends heartbeats during CLI execution and server rejects late submits when slot is still free | `src/rally/worker.py:296` |
| WRK-8 | medium | confirmed | Worker has no SIGTERM/SIGINT handler to kill the child process, release the active lease, and save state | `src/rally/worker.py:353` |
| WRK-9 | medium | confirmed | HttpClient._req and Worker._get_status treat HTTP 401 and 404 JSON error responses as status 200 | `src/rally/server.py:1366` |
| WRK-10 | medium | confirmed | AdapterBusy halts Worker.run() permanently, while AdapterError spins in an infinite retry loop on the same WU | `src/rally/worker.py:313` |
| WRK-11 | medium | confirmed | Coordinator.lease leaks is_canary in the served WU payload and omits manifest_hash verification | `src/rally/server.py:536` |
| WRK-12 | low | confirmed | Worker defaults model=None to family='brawn' and track='brawn' for LLM CLIs, and resolve_family misclassifies 'sol' and rejects 'llama' | `src/rally/worker.py:209` |
| GAP-1 | high | unchecked | [partial] Lease and submit responses expose canary identity, and production canary selection is missing | `src/rally/server.py:536` |
| GAP-2 | high | unchecked | [partial] Holdout verification executes untrusted code inside the server and hardcodes Rally B results instead of routing verify WUs to the owner | `src/rally/server.py:923` |
| GAP-3 | high | unchecked | [partial] Worker adapters omit sandbox and tool-disabling flags, lack Docker exec and patch worktrees, and have a hollow W-05 test | `src/rally/worker.py:65` |
| GAP-4 | high | unchecked | [partial] HTTP API leaks token_hash on /v1/me, allows unauthenticated admin creation, skips lease-holder checks, and lacks leaderboard and export endpoints | `src/rally/server.py:1097` |
| GAP-5 | high | unchecked | [partial] rally-worker CLI crashes with client=None and lacks init, doctor, join, leave, status, worker.json config, and answer extraction | `src/rally/worker.py:360` |
| GAP-6 | medium | unchecked | [partial] POST /v1/lease ignores offers, hardware resource needs, max_ttl_s, and manifest_hash pinning, and omits spec wu fields | `src/rally/server.py:357` |
| GAP-7 | medium | unchecked | [partial] Server CLI and add_project lack admin subcommands, external JSONL loading, and canonical public manifest hashing | `src/rally/server.py:1400` |
| GAP-8 | medium | unchecked | [partial] Coordinator submit behavior contradicts architecture spec on late submits, Gate 1 failure attempts, 413 size limit, and WU status names | `src/rally/server.py:622` |
| GAP-9 | medium | unchecked | [partial] Checks and normalizer lack regex, exact, numeric, hash, significant-digit rounding, list/dict keys, and brawn quorum | `src/rally/checks.py:266` |
| GAP-10 | medium | unchecked | [partial] Pre-reset harvester lacks reset schedule parser, state.json persistence, heartbeat thread, SIGTERM handler, and idle checks | `src/rally/worker.py:229` |
| GAP-11 | medium | unchecked | [partial] Leases table omits UNIQUE(wu_id, participant_id) and hardcodes slot=1, breaking invariant I-02 when two leases on a WU are active | `src/rally/server.py:98` |
| GAP-12 | medium | unchecked | [hollow] Ledger test P-01 inserts raw SQL points instead of testing Coordinator point awards, and server hardcodes 1 point per submission | `src/rally/server.py:725` |
| GAP-13 | medium | unchecked | [hollow] Invariant checker omits I-05 (Blindness), I-11 (Pre-Reset Drain), and I-12 (Replay), and checks incomplete rules for I-06 and I-08 | `tests/e2e/invariants.py:137` |
| GAP-14 | medium | unchecked | [hollow] Unit tests G3-04, G3-01, G3-05, G2-05, and G1-01..G1-07 test constants or pure helpers instead of Coordinator behavior | `tests/unit/test_gates.py:260` |
| GAP-15 | medium | unchecked | [partial] Concurrency test IT-07 runs 1 round instead of 20 rounds and omits the Phase 1 100-WU replica concurrency test | `tests/integration/test_concurrency.py:12` |
| GAP-16 | low | unchecked | [hollow] End-to-end test E-11 does not check that drain reaches quiet by 19:00 simulated time, and rally_script forces P3 wave via raw SQL | `tests/e2e/test_five_participant_rally.py:90` |
| GAP-17 | low | unchecked | [partial] Idempotent submit retry (IT-02) returns HTTP 200 pending when the first submission was rejected with HTTP 422 | `src/rally/server.py:607` |
| GAP-18 | low | unchecked | [missing] Example project examples/rct-outcomes/ with 20 deterministic units and fixture docs/ directory with opaque IDs are missing | `tests/fixtures/rally/c_corpus.json:70` |
| GAP-19 | low | unchecked | [missing] Live multi-agent smoke suite (tests/live/faults.py, tests/live/test_live_rally.py, LV-01..LV-08) and rally[yaml] extra are missing | `pyproject.toml:20` |
| GAP-20 | low | unchecked | [missing] Manual runs with real CLIs in Phases 1, 2, 4, and 5 are not completed | `docs/specs/architecture-and-build-plan.md:866` |
| TST-1 | high | confirmed | Test G3-06 and invariant I-07 do not inspect served WU keys and miss 'is_canary' leak | `tests/unit/test_gates.py:284` |
| TST-2 | high | confirmed | Invariants I-05, I-11, and I-12 are unimplemented, I-09 ignores its sum, and run.fresh skips check_all | `tests/e2e/invariants.py:137` |
| TST-3 | high | confirmed | Test G3-04 asserts only static constants and never calls Coordinator.lease() | `tests/unit/test_gates.py:260` |
| TST-4 | high | confirmed | Test P-01 inserts hardcoded points with raw SQL instead of calling Coordinator.submit() | `tests/unit/test_ledger.py:17` |
| TST-5 | high | confirmed | E2E harness and get_final_state() hardcode Rally B wave transition, winner, and Dave WU order | `tests/e2e/rally_script.py:377` |
| TST-6 | high | confirmed | Worker.lease_calls_before(str) ignores its cutoff argument and cannot return non-zero | `tests/e2e/test_five_participant_rally.py:27` |
| TST-7 | medium | confirmed | Test E-11 never checks the 19:00 deadline and asserts on a hardcoded leased_count = 0 | `tests/e2e/test_five_participant_rally.py:90` |
| TST-8 | medium | confirmed | Test IT-02 hides non-idempotent submit response payload and IT-03 stops before completion | `tests/integration/test_api_flow.py:64` |
| TST-9 | medium | confirmed | Tests G1-07, H-01, and H-04 test local stubs or fixture verifiers instead of Coordinator and Worker | `tests/unit/test_gates.py:135` |
| TST-10 | medium | confirmed | Test W-05 never executes shims or checks .argv logs, and W-07 masks Worker.run() exit on Busy | `tests/unit/test_worker.py:175` |
| TST-11 | medium | confirmed | Test IT-07 runs 1 round instead of 20, and E2E test_e01 bypasses its HTTP server fixture and uses /tmp | `tests/integration/test_concurrency.py:12` |
| TST-12 | low | confirmed | Setuptools build artifacts in src/reset_rally.egg-info/ are tracked in git and missing from .gitignore | `.gitignore:1` |

Full findings (scenario, evidence, fix hint) and repro tests: `.scratch/audit/` (local only, not in git).
