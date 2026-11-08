# ADR-0001: Pull-Based Zero-Credential Local Execution

- **Status:** Accepted
- **Date:** 2026-11-08

## Context
Participants want to contribute unused quota from flat-rate AI subscriptions (`ChatGPT`, `Claude`, `OpenCode`, `Google AI Pro`) before their periodic usage windows reset. However, AI providers forbid credential sharing, cookie pooling, and reverse-proxying personal accounts into multi-user APIs. Centralizing credentials also creates a security risk for participants.

## Decision
Reset Rally uses a **Pull-Based Zero-Credential Local Execution** architecture:
1. The central coordinator (`rally-server`) never requests, stores, or proxies API keys, OAuth tokens, or session cookies.
2. Each participant runs a local open-source CLI (`rally-worker`) on their own machine.
3. `rally-worker` pulls structured **Work Units** from `rally-server` and executes them locally by invoking the participant's officially installed and logged-in CLI tools (`claude -p`, `codex exec`, `opencode run`, `agy -p`, `ollama run`) in sandboxed, non-interactive mode.
4. Only the resulting structured output (JSON, math expression, or Git patch) is returned to `rally-server`.

## Consequences
- **Positive:** 100% compliant with single-user local CLI usage; zero credential liability; works out of the box with existing local CLI logins.
- **Negative:** Tasks must be asynchronous and tolerant of participant disconnect (handled via time-bounded **Leases**).
