# Reset Rally — Task Tracker

## Completed Work

- [x] **1. Repository & Workspace Setup (`/Users/za/Documents/Reset Rally`)**
  - Initialized Git repository (`main`) and created public GitHub repo [`SaarShai/Reset-Rally`](https://github.com/SaarShai/Reset-Rally).
  - Configured canonical GitHub triage & wayfinder labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`, `wayfinder:*`).
- [x] **2. Plugin & Skill Installation**
  - Installed 33 non-global project skills from [`mattpocock/skills`](https://github.com/mattpocock/skills), [`everyinc/compound-engineering-plugin`](https://github.com/everyinc/compound-engineering-plugin), and [`dietrichgebert/ponytail`](https://github.com/dietrichgebert/ponytail) into `.agents/skills/` and linked into `.claude/skills/`.
  - Enabled `compound-engineering@compound-engineering-plugin` and `ponytail@ponytail` in `.claude/settings.json` and `opencode.json`.
  - Configured `AGENTS.md` and `CLAUDE.md` with Ponytail rules, ASD-STE100 writing rules, and subagent routing (`agy/claude-opus-5-5-high` -> Gemini fallback via `bin/run_subagent.sh`).
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
  - [x] Live smoke (`bin/live_smoke.sh`, Saar's OK): codex answered 5 units; **the file-read probe FAILED: codex read the sentinel file** with `--sandbox read-only`. Fix: `CODEX_TOOLS_OFF` (`--disable` shell_tool, unified_exec, code_mode_host and 7 more, `-c mcp_servers={}`). Re-run: probe PASS. Claude did not run: its CLI login on this Mac has expired (`Failed to authenticate: OAuth session expired`), so Saar must sign in again; then run `bin/live_smoke.sh 5 "claude codex"`.
  - Verify: `python3 -m pytest` → 349 passed in 6.8 s; mutation check: 22 of 22 mutants red, unmutated copy green; `bin/agent_parity.py --check` exit 0; sizes: checks 256, server 775, worker 449 lines (targets 260 / 800 / 600; the checks target rose from 250 for the three review rules).
- [ ] **9. Live probes still open:** `claude` (after a fresh login), `agy --mode plan`, opencode `plan` agent. A probe that prints the sentinel blocks that adapter type until its tools are off.
