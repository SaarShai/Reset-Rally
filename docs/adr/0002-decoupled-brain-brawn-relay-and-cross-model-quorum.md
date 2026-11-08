# ADR-0002: Decoupled Brain/Brawn Relay and Orthogonal Cross-Model Quorum

- **Status:** Accepted
- **Date:** 2026-11-08

## Context
1. Most people with expiring subscription quotas (`claude`, `codex`, `opencode`, `agy`) use laptops without heavy GPUs, while hardware contributors with idle GPUs may not hold frontier LLM subscriptions. Coupling LLM reasoning and heavy compute on the same node (as in single-machine `autoresearch`) excludes 90%+ of potential contributors.
2. Frontier LLMs are stochastic and can hallucinate. Validating an LLM output with the same model family risks correlated errors.

## Decision
1. **Decouple Tracks (`brain`, `brawn`, `relay`):**
   - `brain` Work Units require only an LLM CLI adapter (zero GPU).
   - `brawn` Work Units require only a local sandboxed compute runner (zero LLM subscription).
   - `relay` Rallies connect both via the coordinator: `brain` nodes propose code diffs, proofs, or simulation candidates; `brawn` nodes benchmark or simulate them and write scores back to the project state for the next `brain` wave.
2. **Orthogonal Cross-Model Quorum (`Gate 2`):**
   - When a Work Unit uses `quorum` verification, the coordinator assigns replicas to participants with **different Model Families** (e.g., Replica 1 to `anthropic`, Replica 2 to `openai` or `google`).
   - If the structured outputs match, the Work Unit is verified. If they conflict, the coordinator spawns a targeted tie-breaker Work Unit for a third model family.

## Consequences
- **Positive:** Laptop users can drain 100% of expiring subscription tokens rapidly without waiting on 5-minute GPU runs; model heterogeneity becomes a quality guarantee rather than a bug.
- **Negative:** Requires the coordinator to track `model_family` on each Lease andSubmission.
