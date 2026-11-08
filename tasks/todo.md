# Reset Rally — Task Tracker

## Completed Work

- [x] **1. Repository & Workspace Setup (`/Users/za/Documents/Reset Rally`)**
  - Initialized Git repository (`main`) and created public GitHub repo [`SaarShai/Reset-Rally`](https://github.com/SaarShai/Reset-Rally).
  - Configured canonical GitHub triage & wayfinder labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`, `wayfinder:*`).
- [x] **2. Plugin & Skill Installation**
  - Installed 33 non-global project skills from [`mattpocock/skills`](https://github.com/mattpocock/skills), [`everyinc/compound-engineering-plugin`](https://github.com/everyinc/compound-engineering-plugin), and [`dietrichgebert/ponytail`](https://github.com/dietrichgebert/ponytail) into `.agents/skills/` and linked into `.claude/skills/`.
  - Enabled `compound-engineering@compound-engineering-plugin` and `ponytail@ponytail` in `.claude/settings.json` and `opencode.json`.
  - Configured `AGENTS.md` and `CLAUDE.md` with Ponytail rules, ASD-STE100 writing rules, and subagent routing (since 2026-11-08: Opus 5.5 subagents on the Claude subscription only; `bin/run_subagent.sh` removed).
  - Verified parity with `python3 bin/agent_parity.py --check`.
- [x] **3. Architecture & Build Plan (`docs/specs/architecture-and-build-plan.md`)**
  - Defined domain-agnostic `Rally` specification (supporting math, physics, code/development, structured extraction).
  - Specified SQLite WAL schema, Lease & Cross-Model Quorum state machine, 3-Gate Verification, and Pre-Reset Quota Harvester.
  - Authored `GLOSSARY.md`, `ADR-0001`, and `ADR-0002`.
- [x] **4. Test Plan & 5-Participant Mock Rally Design (`docs/specs/test-plan-and-5-participant-rally.md`)**
  - Designed unit, integration, and 5-Participant End-to-End Mock Rally tests (`Alice`, `Bob`, `Carol`, `Dave`, `Eve`) across 3 domain-agnostic projects (Math/Physics, Code Optimization, Structured Corpus Synthesis).
- [x] **5. Core Engine & Full Test Suite Implementation (`src/rally/` & `tests/`)**
  - **Slice 1 (`src/rally/checks.py`, `tests/fixtures/rally/`, `tests/shims/`, `tests/unit/test_manifest.py`, `tests/unit/test_gates.py`):** Implemented manifest validation, Gate 1 deterministic checks (JSON schema, safe AST expression parser, verbatim `quote_span`, Python AST patch check), Gate 2 quorum normalizer, Gate 3 Canary grading & Trust Score updates, and all 3 Rally fixtures + 5 CLI shims (`claude`, `codex`, `agy`, `opencode`, `brawn`).
  - **Slice 2 (`src/rally/worker.py`, `tests/unit/test_worker.py`):** Implemented underlying Model Family resolution (`W-04`), CLI adapter table (`W-05`), rate-limit classification (`QuotaExhausted`, `AdapterBusy`, `AdapterError`), Pre-Reset Drain window scheduler, and zero-credential HTTP client.
  - **Slice 3 (`src/rally/server.py`, `tests/unit/test_leases.py`, `tests/unit/test_ledger.py`, `tests/integration/`, `tests/e2e/`):** Implemented SQLite WAL Coordinator (`BEGIN IMMEDIATE` transactions, lazy lease expiry, cross-family quorum + tie-breaker slots, Brain-to-Brawn relay WUs, coordinator holdout verification, Canary quarantine & rollback, and Rally Points ledger) plus the flagship **5-Participant Mock Rally** (`E-01..E-12`) and all 12 invariants (`I-01..I-12`).
  - **Verification:** `python3 -m pytest` -> **80 passed in 1.82s** (`< 2.0s` target).

## In Progress

- [x] **6. Independent audit of the fallback-built core (2026-11-08)**
  - Why: Gemini Flash fallback agents wrote `src/rally/` and `tests/` (Opus and Sonnet quota ran out). No second agent reviewed the code.
  - Done: five read-only Gemini 4 Argon reviewers (spec gaps, coordinator, security, worker, tests) found 68 issues. A second Argon run confirmed all 36 it checked (security, worker, tests); the spec-gap and coordinator checks did not run (Google quota out, resets in about 6 days).
  - Verified by the main session in the code: fixture values in `server.py` (`rally-b`, `1794191700`, `P-bob-2`), `exec` of participant code through `tests/fixtures`, unauthenticated admin creation, no lease-holder checks, `is_canary` in lease payloads, `rally-worker` crash.
  - Result: [`docs/audits/2026-11-08-core-audit.md`](../docs/audits/2026-11-08-core-audit.md). The 80 green tests do not show a working system. Rule added to `AGENTS.md` §2 ("Accepting a slice").
  - Auditors made no repo edits (`git status`: only this file, the report and `AGENTS.md`).
- [x] **7. Decide: rebuild or patch** — Saar, 2026-11-08: rebuild Phase 1-2 test-first in this Claude Code session.
- [x] **8. Rebuild Phase 1-2 core**
  - [x] Contract: `docs/specs/phase-1-2-build-contract.md` (interfaces, behavior, tests; no Trust Score, the spec's canary counts decide quarantine).
  - [x] Old `src/rally/{checks,server,worker}.py`, `tests/` and the tracked `egg-info` removed (still in git history).
  - [x] Slice A: `checks.py` + checks tests + generic-src guard (128 tests).
  - [x] Slices B1 (`server.py`, 71 tests) and B2 (`worker.py`, 84 tests).
  - [x] Slice C: end-to-end tests (Phase 1 100 WUs, five-participant rally), `examples/rct-outcomes/`, README. Suite: 285 passed. Hand smoke run (serve, init, join, run, export) works.
  - [x] Plan audit (Fable 5.1, high): go with changes; 12 of 15 points adopted into the contract (attempts per manifest, canary 422 counts, canaries pay 0, late-submit family rule, stored replay reply, no LIMIT, `tst` names, argv override, drift log, schema minItems/minLength, `lease_ttl_s >= timeout_s`, one winning group).
  - [x] Fix stage for the adopted points.
  - [x] Review: 4 Claude reviewers, a mutation tester and a Gemini Flash check; one skeptic per finding. 50 findings, 43 confirmed, 7 refuted. All confirmed findings fixed or recorded as contract §7 limits:
    - canaries come only in place of real work for this participant (no lease when the project has none: the lease no longer proves a canary);
    - a live lease on a closed WU gets 409 `wu_closed` (no double pay after a version bump); a `failed` WU rejects its pending votes;
    - `replicas <= 2 x agree - 1`; `max_replicas` default `min(replicas + 1, 2 x agree - 1)`; families must cover `max_replicas`; `NaN`/`Infinity` → 400; export in one snapshot;
    - worker: refused submits count as errors; no redirects; argv prompt cap 100 KB; error text falls back to stdout;
    - tests: canary values equal, deterministic and quorum-field grading, fall-back, quorum waits for all replicas, order tests that can fail, owner cannot create participants, codex without `-o`, 401 on lease, guard reads payload and answer text, e2e second openai account gets 204.
  - [x] Live smoke (`bin/live_smoke.sh`, Saar's OK): codex answered 5 units; **the file-read probe FAILED: codex read the sentinel file** with `--sandbox read-only`. Fix: `CODEX_TOOLS_OFF` (`--disable` shell_tool, unified_exec, code_mode_host and 7 more, `-c mcp_servers={}`). Re-run: probe PASS. Claude did not run: the `claude` CLI is signed out on this Mac (`claude auth status` → `loggedIn: false`; same failure in a clean env, so the desktop app's variables are not the cause). Saar runs `claude auth login`, then `bin/live_smoke.sh 5 "claude codex"`. Saar's own re-run (13:5x) gave the same result: codex 5 units and probe clean, claude signed out, probe INCONCLUSIVE.
  - Verify: `python3 -m pytest` → 349 passed in 6.8 s; mutation check: 22 of 22 mutants red, unmutated copy green; `bin/agent_parity.py --check` exit 0; sizes: checks 256, server 775, worker 449 lines (targets 260 / 800 / 600; the checks target rose from 250 for the three review rules).
- [ ] **9. Live adapter checks (2026-11-08, `bin/live_smoke.sh`)**
  - [x] claude (Opus/Sonnet via the subscription, after `claude auth login`) + codex, 5 units each: 3 of 5 units `done` by anthropic + openai agreement (2 points each); 2 conflicts wait for a third family (tie-break slot). Probe: codex returns "filesystem tools are unavailable" (no leak); claude answers with no JSON object (no leak; `--tools ""` leaves it no tool).
  - [x] agy (Gemini Flash), 2 units: both submitted. Probe: the model asks for `read_file`; headless agy refuses the permission and gives no output, so the job fails and nothing leaks. A hostile prompt can waste an agy job, but it gets no file text.
  - [ ] opencode (free DeepSeek): the probe job gives no JSON answer, so a leak is not ruled out (the worker does not store raw output). Paused: Saar, 2026-11-08: "no codex!!! only claude!!". No agent starts codex, agy or opencode (AGENTS.md §2); `bin/live_smoke.sh` runs claude only. The tool-off research was stopped; an opencode daemon (`opencode serve --service`) that the probe left running was stopped.
  - Note: editing `bin/live_smoke.sh` while it runs breaks the run (bash reads the file as it goes); the claude + codex results above come from that run's database, exported by hand.
- [x] **10. Review rounds 2-5 on the fix code (2026-11-08)**
  - Round 2 review: 3 Claude lenses and a skeptic per lens. 13 findings confirmed, 4 refuted (R2S-3, XF-1..3).
  - [x] Round 2 fixes: non-finite numbers (`1e999`) → 400; a late submit first expires dead leases; a bump cannot change `verify.mode`; ties go by insert order (`rowid`), not the random id; `join` escapes control characters; tests for POST redirects, the literal codex feature list, stdin prompts above 100 KB, two canary projects, `[unfit, fit]` offers, the export snapshot, duplicate families; `test_generic_src` scans the imported package; AGENTS.md mutation command quoted. Check: 366 passed, 16 of 17 mutants red.
  - [x] Round 3: the round-2 rule "one active lease per project" locked a participant out for the lease TTL after a 409 `wu_closed` or a crash, and a release-then-lease probe still told a canary. Replaced by the canary stand-in rule (`leases.instead_of`: the replaced WU counts as leased by this participant until the canary is submitted). A 409 on submit is no adapter error; the worker refuses non-finite output; a bump cannot change `verify.quorum`; `join` escapes families. Check: 381 passed, 44 of 45 mutants red; the canary hunter found TELL-1 (a held canary left its WU's slot and family free for other accounts) and TELL-2 (the canary's own id).
  - [x] Round 4: a canary lease holds the replaced WU's slot and family and shows its id; a bump that turns canaries on needs stored canaries; `join` survives `families: null`; `agree` test with `distinct_families` off; probe tests that can fail; `CANARY_PICK` index. Check: 395 passed; 43 old and 31 new canary probes find no tell before the answer; 72 of 75 mutants red (2 test gaps, 1 equivalent); the new index slowed `CANDIDATES` (CK4-1).
  - [x] Round 5: partial index `wu_canary`; a canary submit gets the same 409s as a real lease of its WU (TELL2-1); boundary tests (late same-family vote, exact `MAX_OUTPUT`). Check: 401 passed; 74 canary probes pass; plans on their indexes; 33 of 35 mutants red, the 2 survivors equivalent (dead `:skip` terms and a dead `_fit` guard, now removed).
  - [x] Verify (main session, after the dead-code removal): `python3 -m pytest` → 401 passed in 9.5 s; 12 mutants on the round 2-5 rules red, clean copy green, syntax copy red; 80 canary probes pass; `bin/live_smoke.sh 2 "fake-a fake-o"` → 2 units done, probe PASS; `bin/agent_parity.py --check` exit 0; sizes: checks 256, server 798, worker 456 (targets 260 / 800 / 600).
