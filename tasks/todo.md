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
