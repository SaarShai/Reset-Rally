# Reset Rally — Task Tracker

## Active Plan

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
  - Specified both Fast Deterministic CI Mode (`< 2 s`, `$0` tokens via `shim.py` + `FakeClock`) and Live Multi-Agent Smoke Mode (via `agent-roster/run.sh`).
- [x] **5. Commit & Push to `SaarShai/Reset-Rally`**
  - Verify all files, run `bin/agent_parity.py --check`, commit, and push to `origin/main`.

## Next Up (Phase 1 Implementation)

- [ ] **Phase 1A: `src/rally/checks.py` & `tests/unit/test_gates.py`** (pure deterministic Gate 1 checks, normalizer, safe AST expression parser, quote-span verifier).
- [ ] **Phase 1B: `src/rally/server.py` & `tests/unit/test_leases.py`** (SQLite WAL coordinator, lease transactions, lazy expiry, quorum & canary grading).
- [ ] **Phase 1C: `src/rally/worker.py` & `tests/unit/test_worker.py`** (CLI adapters, Pre-Reset Drain harvester, rate-limit release).
- [ ] **Phase 1D: `tests/e2e/test_five_participant_rally.py`** (5-Participant Mock Rally harness & 12 invariants).
