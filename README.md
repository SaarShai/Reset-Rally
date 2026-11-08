# Reset Rally

**Pool expiring AI subscription quotas and idle local hardware for shared engineering, math, physics, and research goals.**

**Status (2026-11-08):** Phases 1 and 2 of the [build plan](docs/specs/architecture-and-build-plan.md) are built: the `brain` track, JSON mode, deterministic checks, cross-family quorum with a tie-breaker, canaries and quarantine, and the `claude`, `codex`, `agy`, `opencode` and `fake` adapters. The `brawn` and `relay` tracks, holdout checks, the Pre-Reset Drain scheduler, `ollama` and YAML manifests are design only.

**Live check (2026-11-08, [`bin/live_smoke.sh`](bin/live_smoke.sh)):** real `codex` answered 6 units of the example project. A file-read probe showed that `codex --sandbox read-only` alone lets the model read a local file and submit it; the worker now turns codex's tools off, and the same probe gets no file text. The `claude` run did not start (the CLI login on the test Mac had expired). `agy` and `opencode` are not probed yet and can still read files: join only projects from owners you trust.

Millions of subscribers leave unused tokens on flat-rate AI plans (`ChatGPT`, `Claude`, `OpenCode`, `Google AI Pro`) when their 5-hour, daily, weekly, or monthly limits reset. At the same time, millions of personal GPUs and Apple Silicon Macs sit idle overnight.

**Reset Rally** coordinates both resources into a domain-agnostic distributed research and engineering grid—without ever collecting, proxying, or sharing user credentials.

---

## Core Architecture

1. **Pull-Based Zero-Credential Execution ([ADR-0001](docs/adr/0001-pull-based-zero-credential-execution.md)):**
   Participants run `rally-worker` locally. It invokes their existing authenticated CLIs (`claude`, `codex`, `opencode`, `agy`) in non-interactive mode. Planned: `ollama`, and runs during idle hours or right before a quota reset (**Pre-Reset Drain**).
2. **Decoupled `Brain` + `Brawn` + `Relay` Tracks (only `brain` is built) ([ADR-0002](docs/adr/0002-decoupled-brain-brawn-relay-and-cross-model-quorum.md)):**
   - **`brain` Track:** Pure reasoning, coding, math derivation, or structured extraction using expiring subscription quotas (runs on any laptop, zero GPU required).
   - **`brawn` Track:** Sandboxed local compute (running test suites, benchmarks, numerical physics simulations, or local models).
   - **`relay` Track:** Connects `brain` and `brawn` asynchronously—`brain` participants propose code patches or candidate solutions, and `brawn` participants benchmark and score them for the next wave.
3. **3-Gate Verification Pipeline:**
   - **Gate 1 (Deterministic):** Strict JSON schema validation and server-side checks (`regex`, `exact`, `numeric`, verbatim `quote_span`, `hash`). Compiler and unit-test runs are planned for the `brawn` track.
   - **Gate 2 (Orthogonal Cross-Model Quorum):** Blind replication across distinct **Model Families** (e.g., `anthropic` + `openai`), with automatic tie-breaker dispatch to a third family (`google` / `opencode`) on conflict.
   - **Gate 3 (Holdout & Canaries):** Secret ground-truth **Canary Work Units** at a rate the project sets. Two failed canaries (by default) quarantine a participant. Coordinator-only holdout test sets are planned.

---

## Run a local rally

You need Python 3.11 or later. There are no other dependencies. These steps use the synthetic example in [`examples/rct-outcomes/`](examples/rct-outcomes/) and a `fake` adapter, so they spend no AI quota.

1. Make the coordinator database, the owner and the project:

   ```sh
   export PYTHONPATH=src
   python3 -m rally.server add-participant saar --role owner --db rally.db   # prints the owner token once
   python3 -m rally.server add-project examples/rct-outcomes --db rally.db
   python3 -m rally.server invite --db rally.db                              # prints a one-use invite code
   python3 -m rally.server serve --db rally.db --port 8787
   ```

2. In a second terminal, register a participant and join the project:

   ```sh
   export PYTHONPATH=src RALLY_HOME=/tmp/rally-w1
   python3 -m rally.worker init --server http://127.0.0.1:8787 --name w1 --invite <CODE>
   ```

   Add an adapter to `$RALLY_HOME/worker.json`. For a test run, use a `fake` adapter that gives one answer to every prompt:

   ```json
   "adapters": {"fk": {"type": "fake", "family": "anthropic",
     "answers": {"": {"n_randomized": null, "design": "unclear", "primary_met": "unclear", "p_value": null, "quotes": []}}}}
   ```

   For real CLIs, copy the `adapters` block of [`worker.example.json`](examples/rct-outcomes/worker.example.json). `rally-worker doctor` finds the CLIs on your `PATH` and runs only `--version`.

   ```sh
   python3 -m rally.worker join rct-outcomes        # shows the manifest and asks y/n; --yes skips the question
   python3 -m rally.worker run --once               # one lease, one job, one submit
   python3 -m rally.worker status
   ```

3. The project uses a quorum of two families. Repeat step 2 with a second `RALLY_HOME`, a second invite and `"family": "openai"`. The second worker gets the same Work Unit, the two answers agree, and the unit is done.

4. Export the results (JSONL, one line per finished Work Unit):

   ```sh
   python3 -m rally.server export rct-outcomes --db rally.db
   ```

Tests: `python3 -m pytest -q`. They use only `127.0.0.1` and fake adapters.

**Before you join a project:** `codex --sandbox read-only`, `agy --mode plan` and `opencode --agent plan` still let the model read files. Only `claude --tools ""` removes file access. Join only projects from owners you trust ([ADR-0001](docs/adr/0001-pull-based-zero-credential-execution.md)).

---

## Documentation & Specifications

- **[Domain Glossary](GLOSSARY.md):** Ubiquitous language (`Rally`, `Participant`, `Work Unit`, `Track`, `Lease`, `Model Family`, `Verification Gate`, `Canary`, `Pre-Reset Drain`).
- **[Architecture & Build Plan](docs/specs/architecture-and-build-plan.md):** Complete specification of the SQLite/Python coordinator, the project manifest (`rally.json` now; `rally.yaml` is planned) across domains (code, math/physics, corpus extraction), worker CLI adapters, and vertical build slices.
- **[Phase 1–2 Build Contract](docs/specs/phase-1-2-build-contract.md):** The interfaces, behavior and tests of the current build.
- **[Test Plan & 5-Participant Mock Rally](docs/specs/test-plan-and-5-participant-rally.md):** End-to-end test design simulating 5 heterogeneous participants (`Alice/Claude`, `Bob/Codex`, `Carol/Gemini-OpenCode`, `Dave/Brawn`, `Eve/Faulty-Spoofed`) rallying across multi-domain projects.
- **[Agent Instructions & Skills](AGENTS.md):** Ponytail (`lazy senior dev mode`), Matt Pocock engineering skills, Compound Engineering workflows, and Agent Roster routing (`agy/claude-opus-5-5-high` with Gemini fallback).
