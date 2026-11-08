# Glossary

Ubiquitous domain language for **Reset Rally**. Use these exact terms in specs, code, tests, and issues.

| Term | Definition | Avoid |
| :--- | :--- | :--- |
| **Rally** | A domain-agnostic project (math, physics, code optimization, corpus extraction, benchmark evaluation) that decomposes a goal into distributable **Work Units**. | Campaign, Job, Study |
| **Participant** | A volunteer node that contributes expiring AI subscription quota, idle local hardware, or both. | Worker, Miner, Slave, Donor |
| **Work Unit (WU)** | The smallest self-contained, atomic unit of work dispatched to a **Participant** under a time-bounded **Lease**. | Task, Chunk, Ticket, Joblet |
| **Track** | The resource execution lane of a **Work Unit**: `brain` (subscription LLM reasoning/coding only), `brawn` (local CPU/GPU execution/simulation only), or `relay` (`brain` output feeds `brawn` evaluation, which feeds the next `brain` wave). | Tier, Mode, Queue |
| **Lease** | A time-bounded exclusive lock (`lease_id`, `expires_at`) granting one **Participant** the right to execute a specific **Work Unit** replica. | Lock, Reservation, Claim |
| **Adapter** | A thin local wrapper around an official CLI (`claude`, `codex`, `opencode`, `agy`, `ollama`) or local sandboxed command runner. Never touches user credentials. | Proxy, Wrapper, Driver |
| **Model Family** | The independent training lineage of an LLM (`anthropic`, `openai`, `google`, `deepseek`, `qwen`, `meta`). Used to enforce orthogonal cross-model verification. | Provider, Vendor |
| **Verification Gate** | One of the three validation stages a **Submission** passes before acceptance: `Gate 1 (Deterministic)` (schema, compiler, unit tests, exact quote span), `Gate 2 (Quorum)` (blind agreement across distinct **Model Families**), and `Gate 3 (Holdout / Canary)` (hidden test split and secret ground-truth checks). | Validator, Checker, Filter |
| **Canary** | A secret **Work Unit** with a known ground-truth answer injected into the queue (~5% rate) to measure and update a **Participant's** **Trust Score**. | Honeypot, Golden Task, Trap |
| **Pre-Reset Drain** | A scheduled window before a subscription's periodic quota reset (e.g., 5-hour rolling, daily, weekly, or monthly) during which `rally-worker` pulls **Work Units** until reaching its task cap or a CLI rate-limit signal. | Quota Harvest, Burn-down |
| **Rally Points** | Non-monetary reciprocity credits earned by a **Participant** for verified **Submissions**, redeemable to run their own **Rallies**. | Kudos, Tokens, Coins |
