# Test Plan and 5-Participant Mock Rally

Status: design, 2026-11-08. For Phases 1-2, [phase-1-2-build-contract.md](phase-1-2-build-contract.md) replaces this plan: its tests are binding, and the Trust Score below is gone (canary failure counts decide quarantine). Rallies A and B (relay, brawn, holdout) and the Pre-Reset Drain tests wait for Phases 3-5.

Companion: [architecture-and-build-plan.md](architecture-and-build-plan.md). If a name in this plan differs from the architecture spec, the architecture spec is correct. Rename here, do not change the tests' intent.

Vocabulary: [GLOSSARY.md](../../GLOSSARY.md). This plan uses **Rally**, **Participant**, **Work Unit (WU)**, **Lease**, **Adapter**, **Model Family**, **Canary**, **Pre-Reset Drain** and **Rally Points** as the glossary defines them.

---

## 1. Goal

The tests prove six claims:

1. **Coordination.** No WU replica is done twice. No WU is lost when a Participant drops.
2. **Scheduling.** Leases expire on time. The Coordinator gives each WU to an eligible Participant.
3. **Cross-model verification.** Gate 2 commits a result only when two different Model Families agree blindly. A third family breaks ties.
4. **Fault tolerance.** Malformed output, spoofed output and dropped Participants do not corrupt committed results or Rally Points.
5. **Pre-Reset Drain.** `rally-worker` starts in its window, stops at its cap or at a rate-limit signal, and gives back the Lease it holds.
6. **Zero credentials.** No credential value ever reaches the Coordinator.

## 2. Terms this plan fixes

The task brief mixes two axes. This plan keeps them apart:

| Axis | Values | Set by |
| --- | --- | --- |
| **Track** | `brain`, `brawn`, `relay` | Rally manifest, per WU |
| **Verification mode** | `deterministic`, `quorum`, `holdout` | Rally manifest |
| **Canary** | on or off, at a rate | Rally manifest (overlay on any mode) |

Gates (from the glossary):

- **Gate 1 (Deterministic):** JSON parse, schema, exact quote span, safe-expression parse, patch parse, size limit, family match.
- **Gate 2 (Quorum):** blind agreement across two Model Families. A conflict opens a tie-breaker slot for a third family.
- **Gate 3 (Holdout / Canary):** hidden test split that only the Coordinator runs, and secret ground-truth Canaries that move the Trust Score.

**Model Family comes from the model, not from the CLI.** `agy` runs Gemini, Claude and GPT-OSS models. `opencode` runs many families. The Adapter resolves the family from the model id (test W-04).

## 3. Test seams the code must provide

Five seams. Each one is also the plain shape of the production code, so no test-only branches exist.

| # | Seam | Production default | Test use |
| --- | --- | --- | --- |
| S1 | Coordinator takes a `now()` callable | `time.time` | `FakeClock` moves time with no sleep |
| S2 | Coordinator takes a `canary_picker(participant, rally, lease_no)` callable | random, at the manifest rate (about 5%) | a fixed schedule makes Canary delivery deterministic |
| S3 | `rally-worker` exposes `step() -> bool`; its loop is `while step(): sleep(poll)` | the loop | the harness calls `step()` once per tick |
| S4 | Adapter takes a `run(argv, stdin, env) -> (rc, stdout, stderr)` callable | `subprocess.run` | in-process shim mode |
| S5 | Lease expiry is lazy: every lease, heartbeat and submit query treats `expires_at <= now()` as expired | same | no reaper thread, so time is fully deterministic |

Served payloads are logged on the client side (a thin wrapper on the test HTTP client). The server needs no logging seam.

## 4. Layout, commands and budget

```
tests/
  conftest.py                  # FakeClock, db_path, server, client, shims fixtures
  shims/
    shim.py                    # respond() + __main__; one file for all five CLIs
    bin/claude -> ../shim.py   # committed symlinks: claude codex agy opencode brawn
  fixtures/rally/
    a_math_physics.json        # Rally A manifest
    b_code_opt.json            # Rally B manifest
    c_corpus.json              # Rally C manifest
    docs/                      # Rally C source texts (opaque ids)
    holdout/b_holdout.json     # Coordinator-only; never served
    verifiers.py               # real deterministic verifiers for A and B
    answers/{alice,bob,carol,dave,eve}.json   # shim answer tables
    expected_final.json        # flagship end state
  unit/
    test_manifest.py
    test_leases.py
    test_gates.py              # Gate 1, Gate 2, Gate 3 (holdout + canary), Trust Score
    test_worker.py             # Pre-Reset Drain, Adapters, rate-limit mapping
    test_ledger.py
  integration/
    test_api_flow.py
    test_relay.py
    test_concurrency.py
    test_quarantine.py
  e2e/
    rally_script.py            # the phase table as data + run() harness
    invariants.py              # I-01..I-12, each a few SQL lines
    test_five_participant_rally.py
  live/
    faults.py                  # fault injection on top of real model output
    test_live_rally.py
```

`pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q -m 'not live'"
markers = ["live: real models through agent-roster run.sh (opt-in, uses plan quota)"]
```

Commands:

| Mode | Command | Cost | Wall time |
| --- | --- | --- | --- |
| Fast CI | `pytest` | 0 tokens, $0 | < 2 s on Apple Silicon |
| Live smoke | `RALLY_LIVE=1 pytest -m live -s tests/live` | plan quota only, $0 cash | < 30 min |

Fast-mode time budget (measured target):

| Part | Budget | Why |
| --- | --- | --- |
| unit/ | 0.4 s | in-process; one brawn timeout test uses a 0.2 s timeout |
| integration/ | 0.3 s | real HTTP on `127.0.0.1:0`, temp SQLite in WAL mode |
| e2e in-process, seeds 0, 1, 2 | 0.3 s | shim `respond()` called in-process (S4) |
| e2e subprocess, seed 0 | 0.9 s | about 35 real shim processes at about 25 ms each |

If CI hardware misses 2 s, mark only the subprocess e2e test `slow` and run it before merge. Do not cut other tests.

## 5. Fixtures: three Rallies

Fixture item ids are opaque (for example `doc-3c9e`) so that a Canary looks like a normal WU. This plan uses the labels C1, K1 and so on only for reading.

Default Rally Points: 1 point for each accepted contribution. Canaries give 0 points.

### Rally A: Math / Physics (`track: relay`, `verification: deterministic`)

A `brain` WU asks for a closed form or a parameter. Gate 1 parses the answer with a safe-expression parser (AST allowlist: integers, `n`, `+ - * / // **`, parentheses). It never calls `eval` on model output. A passing answer opens a `brawn` verify WU (VA*). The brain WU commits only when its verify WU passes.

| WU | Question | Correct answer | Brawn verifier (`verifiers.py`) |
| --- | --- | --- | --- |
| A1 | closed form of sum of k³, k = 1..n | `(n*(n+1)//2)**2` | exact integer match for n = 1..200 |
| A2 | closed form of sum of k² | `n*(n+1)*(2*n+1)//6` | same |
| A3 | closed form of sum of (2k − 1) | `n**2` | same |
| A4 | a leapfrog time step `dt` for the harmonic oscillator (ω = 1) | any `dt` in [0.01, 0.2] | 10 initial states × 1000 steps; max relative energy drift < 1e-2 |
| KA | brawn Canary: verify the wrong form `n**3` for sum of k² | verdict `fail` | same as A2 |

Checked numbers (Python 3.14, this Mac): drift at dt = 0.1 is 0.0025 (pass). Drift at dt = 0.2 is 0.0099 (pass). dt = 2.5 diverges to infinity (fail).

### Rally B: Code / Development (`track: relay`, `verification: holdout`)

Goal: make `count_primes(n)` faster. Baseline is naive trial division. Two waves.

- Wave 1 (B1): 2 proposal slots, different Participants.
- Wave 2 (B2): 1 slot. The prompt contains the public leaderboard: scores, and holdout pass or fail only.
- Dave runs the public tests (n ∈ {10, 100, 1000, 5000}) and the metric. The Coordinator runs the holdout split (n ∈ {0, 1, 2, 3, 7919, 10007}) itself. No holdout value is ever served.
- **Deterministic metric:** count of `line` events under `sys.settrace` for n = 5000. This count is stable from run to run. Tests compare order only, not exact counts, because counts change between Python versions.

| Patch | Author | Public tests | settrace lines (n = 5000) | Holdout |
| --- | --- | --- | --- | --- |
| baseline (naive) | fixture | pass | 93 254 | pass |
| P-alice (sieve with `n < 2` guard) | Alice, B1 | pass | 142 | pass |
| P-bob-1 (sieve, no `n < 2` guard) | Bob, B1 | pass | about 142 | **fail** (`IndexError` at n = 0) |
| P-bob-2 (odd-only sieve) | Bob, B2 | pass | 109 | pass |

Expected best at the end: P-bob-2. P-bob-1 is `disqualified`.

### Rally C: Structured Corpus Synthesis (`track: brain`, `verification: quorum`, Canary on)

Each WU gives one short technical text and asks for:

```json
{"component": "string", "supply_v": 3.3, "quote": "exact text from the source"}
```

Gate 1 rule for `quote`: after whitespace collapse, `quote` is a substring of the source, and it contains the `supply_v` number. Gate 2 compares `component` with casefold and `supply_v` with tolerance 0.01.

| WU | Source text (summary) | Truth | Seeded behavior |
| --- | --- | --- | --- |
| C1 | "The RX-100 regulator takes a 5.5 V supply." | 5.5 | all agree |
| C2 | "The LM-20 amplifier runs from a 12 V supply." | 12 | all agree |
| C3 | "The ZX-3 sensor runs at 3.3 V. Its I/O pins tolerate 5.0 V." | 3.3 | **Alice 3.3, Bob 5.0 → conflict; Carol breaks the tie with 3.3** |
| C4 | "The QT-9 module uses a 9 V supply." | 9 | Bob and Carol agree |
| K1 | Canary: "The KC-7 board takes a 12 V adapter. Its core supply is 1.8 V." (component: KC-7 core) | 1.8 | Eve's lazy answer: 12, with a real quote → passes Gate 1, fails the Canary |
| K2 | Canary: "The PB-2 probe needs a 24 V supply." | 24 | — |

Quorum size k = 2. Tie-breaker slot: family not in the conflicting pair, and Trust Score ≥ `TIEBREAK_MIN_TRUST`.

### Trust constants (fixture values)

| Constant | Value |
| --- | --- |
| `TRUST_START` | 0.5 |
| `CANARY_PASS` | +0.1 |
| `CANARY_FAIL` | −0.5 |
| `GATE1_REJECT` | −0.1 |
| `QUARANTINE_BELOW` | 0.2 |
| `TIEBREAK_MIN_TRUST` | 0.6 |
| Trust Score range | clamped to [0.0, 1.0] |
| `LEASE_TTL` | 15 min |
| `MAX_ATTEMPTS_PER_PARTICIPANT` (same WU slot) | 2 |

Tests read these names from the manifest or config. They do not repeat the numbers.

## 6. The five Participants

One shim per Participant. All five CLIs are used.

| Participant | Shim CLI | Model id (fixture) | Model Family | Track | Behavior |
| --- | --- | --- | --- | --- | --- |
| **Alice** (Claude Pro Pre-Reset) | `claude` | `claude/claude-sonnet-5-5` | `anthropic` | brain | Pre-Reset Drain window Sunday 18:00–20:00, buffer 10 min. Call 5 returns the rate-limit signal (`limit_after: 4`). |
| **Bob** (ChatGPT/Codex Weekly Drain) | `codex` | `codex/gpt-6-astra` | `openai` | brain | Quorum votes, patches, the A4 retry. Seeded: C3 = 5.0, P-bob-1 fails holdout. |
| **Carol** (Gemini Idle) | `agy` | `agy/gemini-3.8-flash-low` | `google` | brain | Online only in phase P2b. Tie-breaker for C3. Takes the expired A1 Lease. Seeded: A4 first answer dt = 2.5 (fails verify). |
| **Dave** (Local Brawn) | `brawn` | none (local) | none | brawn | Runs the real `verifiers.py` on each VA*/VB* WU. Honest. |
| **Eve** (Faulty / Spoofed) | `opencode` | `opencode/deepseek-v4.1-flash-free` | `deepseek` | brain | (a) Leases A1, then drops. (b) Malformed JSON, then a hallucinated quote, on C4. (c) Lazy answer on Canary K1. |

Carol's "Gemini / OpenCode" profile is covered by test W-04: `opencode-go/gemini-4-argon-contributor` resolves to `google`, so Carol on either CLI is the same family. Two Carol Adapters cannot fill two slots of one quorum (test L-07).

Canary schedule (seam S2) in the flagship:

| Participant | Lease number (per Rally) | Canary |
| --- | --- | --- |
| Bob | C lease #2 | K1 |
| Carol | C lease #1 | K2 |
| Dave | A lease #1 | KA |
| Eve | C lease #3 | K1 |
| Alice | none (her cap is too small) | — |

### Shim rate-limit and error samples

The shim returns these for the rate-limit path. `agy` and `opencode` strings come from the agent-roster notes. The `claude` and `codex` strings are assumptions. The live mode records the real strings (section 11) so that these fixtures stay true.

| CLI | rc | stderr | Adapter maps to |
| --- | --- | --- | --- |
| `claude` | 1 | `Claude usage limit reached. Your limit will reset at 8pm.` | `QuotaExhausted` → release, stop |
| `codex` | 1 | `You've hit your usage limit. Try again in 3 hours.` | `QuotaExhausted` |
| `agy` | 1 | `Individual quota reached` | `QuotaExhausted` |
| `opencode` | 1 | `Go usage limit exceeded` | `QuotaExhausted` |
| `codex` | 1 | `model is at capacity` | `Busy` → release, back off, continue |
| `brawn` | 124 | `timeout` | verify result `timeout` |
| any | 3 | anything else | `AdapterError` → release with reason, continue |

## 7. Shims

One file. Five symlinks. The same `respond()` runs in subprocess mode and in-process mode (seam S4), so both modes must give the same event log (test E-12).

```python
#!/usr/bin/env python3
"""Deterministic CLI shim for claude, codex, agy, opencode and brawn."""
import json, os, re, sys
from pathlib import Path

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "rally"

def respond(cli, persona, prompt, state_dir):
    table = json.loads((FIX / "answers" / f"{persona}.json").read_text())
    counter = Path(state_dir) / f"{persona}.count"
    n = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(n))
    if n > table.get("limit_after", 10**9):
        return table["limit_rc"], "", table["limit_stderr"]
    if cli == "brawn":
        from fixtures.rally import verifiers  # real deterministic verifier
        return verifiers.run(prompt)
    item = re.search(r"^Item: (\S+)", prompt, re.M)[1]
    ans = table["answers"].get(item)
    if ans is None:  # fail loud: a missing answer is a fixture bug, not a model error
        return 3, "", f"shim: {persona} has no answer for {item}"
    out = ans["stdout"] if isinstance(ans["stdout"], str) else json.dumps(ans["stdout"])
    return ans.get("rc", 0), out, ans.get("stderr", "")

if __name__ == "__main__":
    prompt = sys.stdin.read() if sys.argv[-1] == "-" else sys.argv[-1]
    rc, out, err = respond(Path(sys.argv[0]).name, os.environ["SHIM_PERSONA"], prompt, os.environ["SHIM_STATE"])
    sys.stdout.write(out); sys.stderr.write(err); sys.exit(rc)
```

The shim also appends its argv to `$SHIM_STATE/<persona>.argv` so test W-05 can check the exact CLI call. Example answer table entry (`eve.json`):

```json
{"answers": {
  "doc-c4": {"stdout": "{\"component\": \"QT-9\", \"supply_v\": 9,"},
  "doc-c4#2": {"stdout": {"component": "QT-9", "supply_v": 9, "quote": "The QT-9 runs at 9 V"}},
  "doc-k1": {"stdout": {"component": "KC-7 core", "supply_v": 12, "quote": "takes a 12 V adapter"}}
}}
```

`#2` selects the answer for the Participant's second attempt on the same item. The shim counts attempts per item in `$SHIM_STATE`.

## 8. The flagship: 5-Participant Mock Rally

Rallies A, B and C run at the same time on one Coordinator. The harness drives time.

- Start: Sunday 2026-11-08 17:30 America/Los_Angeles (`FakeClock`).
- Tick: 5 simulated minutes. After each tick, the harness calls `check_all()` (section 9).
- In each tick, the harness calls `step()` on each online Participant in an order shuffled by `random.Random(seed)`.
- **Every outcome below must hold for every seed.** The phases and the routing rule make this true: the Coordinator gives the lowest open eligible WU slot in the Participant's project order, and a Participant never fills two slots of one WU.
- Eve's drop is modeled at the protocol level: in P1 the harness takes a Lease for Eve with the raw client and then stops calling her. That is exactly what the Coordinator sees when a laptop lid closes.

### Phase table

| Phase | Sim time | Online (project order) | Events | Must be true at phase end |
| --- | --- | --- | --- | --- |
| **P0** Warm-up | 17:30–17:55 | all 5 registered; Alice outside her window | Dave polls; nothing to do | Alice made **0** lease calls. No WU leased. |
| **P1** Pre-reset surge | 18:00–18:10 | Alice [B, C], Bob [B, C], Dave [A, B]; Eve leases A1 at 18:00 and drops | Alice: B1, C1, C2, C3 (4 done). Lease 5 = C4 → `claude` rate-limit → she releases C4 and stops. Bob: B1, C1, K1, C2, C3, C4 (and B2 if it opens). Dave: KA, VB1a, VB1b. Coordinator runs holdout on both B1 patches. | C1, C2 `committed`. C3 `disputed`, tie-breaker slot open (family ∉ {anthropic, openai}). C4 has 1 vote (Bob) and 1 open slot. P-bob-1 `disqualified`. Alice state `stopped:quota`, holds 0 Leases, C4 slot became open at once (no TTL wait). A1 leased by Eve. |
| **P2a** Eve returns | 18:15 | Eve [C, A], Dave | A1 Lease expires at 18:15 (`expires_at <= now`). Eve submits stale A1 → **409**. Eve C lease #1 = C4 → malformed JSON → **422 `gate1:json`** (trust 0.4). C lease #2 = C4 → hallucinated quote → **422 `gate1:quote_span`** (trust 0.3). C lease #3 = K1 → answer 12 → Canary fail (trust 0.0) → quarantined. Next lease → **403**. | A1 `open`, attempt 1 expired. Eve `quarantined`. C4 slot open. Eve holds 0 Leases and 0 Rally Points. |
| **P2b** Carol online | 18:20–18:30 | Carol [C, A], Dave [A, B] | Carol: K2 pass (trust 0.6) → C3 tie-breaker = 3.3 → C4 = 9 → A1, A2, A3 correct → A4 dt = 2.5. Dave: VA1, VA2, VA3 pass; VA4a fails (diverges). | C3 `committed` = 3.3 (Alice + Carol; Bob in minority). C4 `committed` (Bob + Carol). A1–A3 `committed`. A4 `open` with feedback; Carol excluded from A4. |
| **P3** Bob returns | 18:35–18:50 | Bob [A, B], Dave [A, B] | Bob: A4 with feedback → dt = 0.1. B2 (if not done in P1) → P-bob-2. Dave: VA4b pass, VB2 pass. Coordinator holdout on P-bob-2: pass. | A4 `committed`. B2 `committed`; best = P-bob-2. |
| **P4** Drain | 18:55 → quiet | all online | ticks until no WU is `open` or `leased`; cap at 20:00 | Reached quiet by 19:00. End state below. |

### Expected end state (`expected_final.json`)

WU states:

- Brain WUs (10): A1, A2, A3, A4, B1, B2, C1, C2, C3, C4: all `committed`.
- Brawn WUs (8): VA1, VA2, VA3, VA4a, VA4b, VB1a, VB1b, VB2: all `committed` (VA4a commits the verdict `fail`).
- `open` = 0, `leased` = 0, `failed` = 0.
- Canaries K1, K2, KA: never `committed` as results. They appear only as Canary grades.

Committed values: C1 5.5, C2 12, C3 3.3, C4 9. A1–A3 forms match the table. A4 dt = 0.1. Rally B best = P-bob-2.

Counters:

| Counter | Value |
| --- | --- |
| Leases expired | 1 (Eve, A1) |
| Leases released | 1 (Alice, C4) |
| Gate 1 rejects | 2 (Eve) |
| 409 stale submits | 1 (Eve) |
| 403 quarantined lease calls | ≥ 1 (Eve) |
| Holdout disqualifications | 1 (P-bob-1) |
| Tie-breakers opened / resolved | 1 / 1 |
| Spoofed Canary answers / caught | 1 / 1 (100%) |
| Honest Canary answers / false fails | 3 / 0 |

Trust Scores and Rally Points:

| Participant | Trust | Status | Accepted contributions | Rally Points |
| --- | --- | --- | --- | --- |
| Alice | 0.5 | `stopped:quota` | B1, C1, C2, C3 | 4 |
| Bob | 0.6 | active | C1, C2, C4, A4, B2 (not B1: holdout fail; not C3: minority) | 5 |
| Carol | 0.6 | active | C3 (tie-break), C4, A1, A2, A3 (not A4: verify fail) | 5 |
| Dave | 0.6 | active | VA1, VA2, VA3, VA4a, VA4b, VB1a, VB1b, VB2 | 8 |
| Eve | 0.0 | `quarantined` | none | 0 |
| **Total** | | | | **22** |

## 9. Invariants

`invariants.py` checks I-01 to I-10 after every tick, and I-11 and I-12 at the end. Each check is a few lines of SQL or a scan of the client-side payload log. A failure message names the tick, the phase and the WU.

SQL uses the architecture brief's tables: `projects`, `work_units`, `leases`, `submissions`, `participants`.

| ID | Invariant | Check |
| --- | --- | --- |
| I-01 | **Zero duplicate work committed.** One committed result per WU. No Participant is credited twice on one WU. | `SELECT wu_id FROM submissions WHERE verdict='accepted' GROUP BY wu_id, participant_id HAVING count(*) > 1` is empty; each `committed` WU has exactly one result row. |
| I-02 | **One live Lease per slot.** | `SELECT wu_id, slot FROM leases WHERE state='active' AND expires_at > :now GROUP BY 1, 2 HAVING count(*) > 1` is empty. |
| I-03 | **Zero lost WUs.** Every WU is `open`, `leased` (live Lease), `disputed`, `committed` or `failed` with a reason. No WU is `leased` with an expired Lease after the next lease call. At the end: `open` = `leased` = 0. | state scan |
| I-04 | **Cross-family enforcement.** Each quorum commit has ≥ 2 distinct families among agreeing votes. A tie-breaker's family is not in the conflicting pair. One Participant never votes twice on one WU. | join `submissions` → `participants.family` |
| I-05 | **Blindness.** No lease payload contains another Participant's answer for the same WU. | for each served payload, search for sibling `supply_v`, `quote` and patch text |
| I-06 | **Holdout isolation.** No holdout input or expected output appears in any served payload or feedback. | search payload log for each `b_holdout.json` value as a token |
| I-07 | **Canary looks normal.** A served Canary has the same key set and value types as a normal WU of its Rally. The text `canary` and `is_canary` never appear. | payload log |
| I-08 | **Quarantine is total.** A quarantined Participant holds no live Lease, gets 403 on lease, and no committed result depends only on its vote. | SQL + response log |
| I-09 | **Ledger conservation.** Sum of Rally Points = number of accepted contributions × manifest points. Rejected, expired, minority, disqualified and Canary submissions earn 0. | SQL |
| I-10 | **Zero credentials.** The worker env holds `ANTHROPIC_API_KEY=sk-SENTINEL-7f3a`, `OPENAI_API_KEY=sk-SENTINEL-7f3a` and `GEMINI_API_KEY=SENTINEL-7f3a`. No request body, header or URL to the Coordinator contains `SENTINEL`. | client request log |
| I-11 | **Pre-Reset Drain window.** Alice makes 0 lease calls before 18:00 and 0 after her stop. She stops with 0 held Leases. | request log |
| I-12 | **Replay.** The same seed gives the same event-log SHA-256. In-process mode and subprocess mode give the same hash for seed 0. | hash compare |

## 10. Test case matrix

Format: ID, then given/when, then the exact assertion. "Rally X" means the fixture manifest.

### Unit: manifest (`unit/test_manifest.py`)

| ID | Given / when | Then |
| --- | --- | --- |
| M-01 | load the three fixture manifests | all load; tracks are relay, relay, brain; modes are deterministic, holdout, quorum |
| M-02 | unknown track, unknown mode, quorum with k < 2, Canary on with no Canary items | each raises `ManifestError` naming the field |
| M-03 | public view of Rally B | has no `holdout` key and no holdout values |

### Unit: Leases (`unit/test_leases.py`)

| ID | Given / when | Then |
| --- | --- | --- |
| L-01 | two Participants lease on Rally A | they get different WUs; the lowest open id goes first |
| L-02 | Lease taken at t; `now` = t + TTL − 1 s, then t + TTL | active, then expired (boundary is `<=`) |
| L-03 | heartbeat on a live Lease; heartbeat after expiry | `expires_at` moves to now + TTL; then 409 |
| L-04 | submit after expiry | 409 `lease_expired`; no `submissions` row; no points |
| L-05 | release | WU slot is `open` at once; attempt not counted as a failure |
| L-06 | a slot expires `MAX_ATTEMPTS` times in a row | WU is `failed` with reason `max_attempts`; it is visible, not lost |
| L-07 | Alice holds C1 slot 1; Alice asks again; a second `anthropic` Participant asks | Alice gets a different WU; the second `anthropic` gets no C1 slot |
| L-08 | Carol's A4 proposal failed verify; Carol asks for A | Carol never gets A4 again |
| L-09 | quarantined Participant asks for a Lease | 403 |
| L-10 | Participant subscribed to [B, C] | gets B work before C work |

### Unit: Gates and Trust (`unit/test_gates.py`)

| ID | Given / when | Then |
| --- | --- | --- |
| G1-01 | truncated JSON | 422 `gate1:json`; slot reopened |
| G1-02 | missing `quote`, or `supply_v` is a string | 422 `gate1:schema` |
| G1-03 | quote not in source; quote with extra spaces; quote without the number | 422 `gate1:quote_span`; pass; 422 `gate1:quote_span` |
| G1-04 | Rally A answer `__import__('os').system('x')`, `n.__class__`, `2**10**10` | 422 `gate1:expr` for each; no call to `eval` happens (patch `builtins.eval` to raise) |
| G1-05 | Rally B patch with a syntax error; patch with no `count_primes` | 422 `gate1:patch` |
| G1-06 | output above the size limit | 422 `gate1:size` |
| G1-07 | Eve (registered `deepseek`) submits with reported model `codex/gpt-6-astra` | 422 `gate1:family` |
| G2-01 | `anthropic` 3.3 and `openai` 3.30 | `committed`, value 3.3 |
| G2-02 | `anthropic` 3.3 and `openai` 5.0 | `disputed`; one tie-breaker slot with `exclude_families = {anthropic, openai}` |
| G2-03 | tie-breaker `google` 3.3 | `committed` 3.3; minority vote earns 0 points |
| G2-04 | tie-breaker `google` 12 (agrees with neither) | `failed: no_consensus`; nobody earns points |
| G2-05 | forced DB state with two `anthropic` votes that agree | no commit (the commit check repeats the family rule) |
| G2-06 | `component` "zx-3" and "ZX-3" | equal (casefold) |
| G3-01 | Canary pass | trust +0.1; 0 points; no commit row |
| G3-02 | Canary fail from trust 0.3 | trust 0.0 (clamped); `quarantined` |
| G3-03 | two Gate 1 rejects from 0.5 | trust 0.3; **not** quarantined |
| G3-04 | tie-breaker slot; `google` Participant at trust 0.5, then 0.6 | not eligible, then eligible |
| G3-05 | grader property: 4 lazy strategies (constant, first number in text, empty fields, random valid JSON) × every Canary; and the truth | every lazy answer graded fail (100% catch); every truth graded pass (0 false fails) |
| G3-06 | serve a Canary and a normal WU of Rally C | same key set and types (I-07) |
| H-01 | P-bob-1: best public score, fails holdout | `disqualified`; not on the leaderboard; 0 points |
| H-02 | B2 prompt after wave 1 | has scores and holdout pass/fail; has no holdout values (I-06) |
| H-03 | settrace metric on naive, P-alice, P-bob-2, three runs each | same count in each run; P-bob-2 < P-alice < naive |
| H-04 | brawn verify of a patch with `while True: pass`, timeout 0.2 s | verdict `timeout` in < 1 s; Dave's worker continues |

### Unit: worker and Adapters (`unit/test_worker.py`)

| ID | Given / when | Then |
| --- | --- | --- |
| W-01 | Pre-Reset Drain window 18:00–20:00; clock 17:59 | `step()` makes 0 lease calls |
| W-02 | clock 19:45, buffer 10 min, estimated WU time 10 min | no new lease (19:45 + 10 > 19:50) |
| W-03 | `max_tasks: 4` and no rate limit | stops after 4 submits with `stopped:budget` |
| W-04 | family resolution for `claude/*`, `codex/*`, `agy/gemini-*`, `agy/claude-*`, `agy/gpt-oss-*`, `opencode-go/gemini-*`, `opencode/deepseek-*` | anthropic, openai, google, **anthropic**, openai, google, deepseek |
| W-05 | each Adapter runs one prompt through its shim | argv matches: `claude -p …`, `codex exec …`, `agy -p …`, `opencode run -m <model> …`, `brawn <verifier> -` |
| W-06 | each rate-limit sample from the section 6 table, for each CLI | `QuotaExhausted`: Lease released, state `stopped:quota`, no more lease calls |
| W-07 | `model is at capacity` | `Busy`: Lease released, back-off, worker continues |
| W-08 | rc 3, unknown stderr | `AdapterError`: Lease released with reason; worker continues |
| W-09 | submit returns 409 | worker drops the Lease and asks for new work; no retry loop (≤ 1 submit per Lease) |
| W-10 | sentinel keys in env (I-10) | no request to the Coordinator contains `SENTINEL`; the CLI subprocess still gets the env (its own login needs it) |

### Unit: ledger (`unit/test_ledger.py`)

| ID | Given / when | Then |
| --- | --- | --- |
| P-01 | mix of accepted, rejected, minority, disqualified, Canary | points = accepted × manifest points (I-09) |
| P-02 | points are written only at commit | an `open` or `disputed` WU has given 0 points |

### Integration (`integration/`)

| ID | File | Given / when | Then |
| --- | --- | --- | --- |
| IT-01 | `test_api_flow.py` | real HTTP server, temp SQLite in WAL mode: register → lease → submit → commit | 200s; `PRAGMA journal_mode` = `wal`; one result row |
| IT-02 | `test_api_flow.py` | the same submit POST sent twice (client retry) | second reply = first reply; one `submissions` row (idempotent on `lease_id`) |
| IT-03 | `test_api_flow.py` | close the server mid-run, open a new server on the same DB file | live Leases still live; no WU duplicated; the run completes |
| IT-04 | `test_relay.py` | brain proposal passes Gate 1 | one brawn verify WU is created, linked by `parent_id` |
| IT-05 | `test_relay.py` | verify fails | brain WU reopens with feedback text; proposer excluded (L-08) |
| IT-06 | `test_relay.py` | verify passes | brain WU commits; proposer and verifier each earn points |
| IT-07 | `test_concurrency.py` | 20 real threads, 20 rounds, 10 open WUs, one DB file | each round: exactly 10 distinct Leases, 10 empty replies, no `database is locked` error to the client |
| IT-08 | `test_quarantine.py` | Eve votes 9 on C4 with Bob 9 → commit; then Eve fails a Canary | C4 goes back to `open` for a new slot; Eve's point and Bob's point for C4 reverse until a new commit; I-08 holds |

### End to end (`e2e/test_five_participant_rally.py`)

| ID | Assertion |
| --- | --- |
| E-01 | seeds 0, 1, 2: the run ends with `check_all()` clean on every tick and end state == `expected_final.json` |
| E-02 | Alice: 0 lease calls in P0; 4 submits; 1 release; `stopped:quota`; 0 held Leases (I-11) |
| E-03 | Bob's and Alice's C3 votes differ; the tie-breaker Lease goes to Carol (`google`, trust 0.6); C3 = 3.3 |
| E-04 | Eve's A1 Lease expires at 18:15; Eve's stale submit gets 409; Carol commits A1 |
| E-05 | Eve gets 422 `gate1:json`, then 422 `gate1:quote_span`, is not quarantined after these, then is quarantined right after K1 |
| E-06 | Canary catch rate = 1/1; honest Canary false-fail rate = 0/3 |
| E-07 | Dave verifies every relay proposal; VA4a = `fail`; A4 commits with Bob's dt = 0.1 |
| E-08 | P-bob-1 disqualified by holdout; best = P-bob-2; no holdout value in the payload log |
| E-09 | Rally Points per Participant match section 8; total 22 |
| E-10 | no `SENTINEL` in the request log |
| E-11 | quiet reached by 19:00 sim time |
| E-12 | subprocess mode, seed 0: same event-log hash as in-process seed 0 (I-12) |

## 11. Pytest skeleton

`tests/conftest.py`:

```python
import datetime as dt, os, threading
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest

SHIMS = Path(__file__).parent / "shims"
START = dt.datetime(2026, 11, 8, 17, 30, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()  # a Sunday

class FakeClock:
    def __init__(self, t=START): self.t = t
    def __call__(self): return self.t
    def advance(self, seconds): self.t += seconds

@pytest.fixture
def clock(): return FakeClock()

@pytest.fixture
def server(tmp_path, clock):
    from rally.server import make_server  # name per architecture spec
    srv = make_server(db=tmp_path / "rally.db", host="127.0.0.1", port=0, now=clock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()

@pytest.fixture
def shims(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{SHIMS / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SHIM_STATE", str(tmp_path))
    return tmp_path
```

`tests/e2e/rally_script.py` holds the phase table as data, so the test reads like section 8:

```python
PHASES = [
    # name, start, end, online {participant: [rallies]}, raw actions
    ("P0",  "17:30", "17:55", {"alice": ["B", "C"], "dave": ["A", "B"]}, []),
    ("P1",  "18:00", "18:10", {"alice": ["B", "C"], "bob": ["B", "C"], "dave": ["A", "B"]},
            [("raw_lease", "eve", ["A"])]),
    ("P2a", "18:15", "18:15", {"eve": ["C", "A"], "dave": ["A", "B"]},
            [("raw_submit_stale", "eve")]),
    ("P2b", "18:20", "18:30", {"carol": ["C", "A"], "dave": ["A", "B"]}, []),
    ("P3",  "18:35", "18:50", {"bob": ["A", "B"], "dave": ["A", "B"]}, []),
    ("P4",  "18:55", "20:00", "all", []),  # stops early when quiet
]
CANARY_SCHEDULE = {("bob", "C"): {2: "K1"}, ("carol", "C"): {1: "K2"},
                   ("dave", "A"): {1: "KA"}, ("eve", "C"): {3: "K1"}}
```

`tests/e2e/test_five_participant_rally.py`:

```python
import json, pytest
from pathlib import Path
from .rally_script import run
from .invariants import check_all

EXPECTED = json.loads((Path(__file__).parents[1] / "fixtures/rally/expected_final.json").read_text())

@pytest.mark.parametrize("seed", [0, 1, 2])
def test_rally_end_state_and_invariants(seed, server, clock, shims):
    result = run(server, clock, seed=seed, mode="inproc", after_tick=check_all)
    assert result.final_state() == EXPECTED

@pytest.fixture(scope="module")
def rally(tmp_path_factory):  # one seed-0 run shared by the named checks below
    return run.fresh(tmp_path_factory.mktemp("rally"), seed=0, mode="inproc")

def test_alice_drains_then_stops_on_quota(rally):
    a = rally.participant("alice")
    assert a.lease_calls_before("18:00") == 0
    assert (a.submits, a.releases, a.state, a.held_leases) == (4, 1, "stopped:quota", 0)

def test_tiebreak_goes_to_third_family(rally):
    c3 = rally.wu("C3")
    assert {v.family for v in c3.votes} == {"anthropic", "openai", "google"}
    assert c3.tiebreaker.participant == "carol" and c3.value == 3.3

def test_eve_dropout_loses_no_work(rally):
    a1 = rally.wu("A1")
    assert a1.leases[0].participant == "eve" and a1.leases[0].state == "expired"
    assert rally.response("eve", "submit_stale").status == 409
    assert a1.committed_by == "carol"

def test_eve_gate1_then_canary_quarantine(rally):
    codes = [r.code for r in rally.responses("eve") if r.status in (403, 409, 422)]
    assert codes[:4] == ["lease_expired", "gate1:json", "gate1:quote_span", "quarantined"]

def test_canary_catch_rate(rally):
    assert rally.canary_stats() == {"spoofed": 1, "caught": 1, "honest": 3, "false_fail": 0}

def test_rally_points(rally):
    assert rally.points() == {"alice": 4, "bob": 5, "carol": 5, "dave": 8, "eve": 0}

def test_holdout_never_served(rally):
    assert not rally.payload_log_contains_any(rally.holdout_values())

def test_no_credential_reaches_coordinator(rally):
    assert "SENTINEL" not in rally.request_log_text()

def test_subprocess_mode_replays_inproc(tmp_path, rally):
    sub = run.fresh(tmp_path, seed=0, mode="subprocess")
    assert sub.event_log_sha256() == rally.event_log_sha256()
```

`invariants.check_all(db, log, now)` raises `AssertionError("I-04 tick 14 (P2b) WU C3: ...")` on the first broken invariant.

## 12. Live multi-agent smoke mode

Purpose: prove that the same flow works with real models, real CLIs, real processes and real concurrency. Exact outcomes vary, so this mode asserts the invariants and the structural outcomes, not exact values.

### Participants

| Participant | `run.sh` agent | Plan | Model Family | Fault injection (`live/faults.py`) |
| --- | --- | --- | --- | --- |
| Alice | `haiku:low` | Claude subscription | anthropic | after 2 WUs, return the `claude` rate-limit result (tests the real stop path) |
| Bob | `astra:low` | ChatGPT plan | openai | on C3 only, replace `supply_v` with 5.0 (forces the dispute) |
| Carol | `agy/gemini-3.8-flash-low` | Google AI Pro | google | none |
| Dave | local brawn, no model | — | — | none |
| Eve | `deepseek-free` | OpenCode Zen, free | deepseek | truncate JSON; then change the quote; then on any Canary return the first number in the text |

The live Adapter (`roster`) writes the WU prompt to `<outdir>/<name>-brief.md`, runs `~/.agents/skills/agent-roster/run.sh <agent> <workdir> <outdir> <name>` in the foreground, waits for `<name>.done`, and reads `<name>.md`. It takes the first fenced `json` block, or the whole file. Gate 1 handles any other output. Exit 2 or 3 from `run.sh` fails the test with the log path.

### Safety rules (the test enforces them)

- The test refuses any agent name that is pay-per-use: `claude-api/*`, `opencode/*` without `-free`, `*luna*`. It never sets `AGENT_PAID_OK`.
- The test never reads, prints or copies a credential file or key. I-10 runs here too, with sentinel values.
- The Coordinator binds to `127.0.0.1` only.

### Mini-rally

- Rally A: A1 only (relay to Dave).
- Rally B: B1 with 1 slot (Bob), holdout by the Coordinator.
- Rally C: C1, C3 and Canary K1. Canary schedule: Eve's first C lease and Carol's first C lease.
- About 10 model calls in total.
- `LEASE_TTL` = 60 s for Rally A only, so that the dropout test waits 1 minute, not 15.

### Flow

1. Start the Coordinator on a free port with a temp DB.
2. Start the five `rally-worker` processes at the same time. Each process runs its real loop (no harness ticks).
3. When Eve holds a Lease on A1, send `SIGKILL` to her process. Start a new Eve process 5 s later (she continues on C).
4. Wait until quiet, or 30 minutes.

### Assertions

| ID | Assertion |
| --- | --- |
| LV-01 | I-01 to I-11 hold at the end (I-12 does not apply: real models are not deterministic) |
| LV-02 | every WU is `committed` or `failed` with a reason; 0 `open`, 0 `leased` |
| LV-03 | Eve's killed A1 Lease expired and a different Participant committed A1 |
| LV-04 | C3 went to `disputed` and a `google` tie-breaker resolved it, or it ended as `failed: no_consensus` with the reason logged |
| LV-05 | at least one Rally C WU committed with 2 distinct families |
| LV-06 | Eve has ≥ 2 Gate 1 rejects and is `quarantined`; Carol passed her Canary |
| LV-07 | Alice ended `stopped:quota` with 0 held Leases after exactly 2 submits |
| LV-08 | the run took < 30 min |

### Outputs

`.scratch/live-rally/<timestamp>/` holds: `events.jsonl`, `summary.json` (states, counters, Trust Scores, Rally Points), every brief and answer, and `cli-errors.jsonl` (each real non-zero rc with its stderr). Use `cli-errors.jsonl` to update the shim rate-limit samples in section 6.

## 13. Requirement to test trace

| Requirement | Tests |
| --- | --- |
| Alice: pre-reset window, stop after N on rate limit | W-01, W-02, W-03, W-06, E-02, I-11, LV-07 |
| Bob: cross-model votes, code patches | G2-01..G2-03, H-01, E-03, E-08 |
| Carol: tie-breaker, lease recovery, Gemini/OpenCode family | G3-04, W-04, L-07, E-03, E-04, LV-04 |
| Dave: brawn verify of relay output | IT-04..IT-06, H-03, H-04, E-07 |
| Eve (a) drop mid-task | L-02..L-04, W-09, E-04, LV-03 |
| Eve (b) malformed JSON, hallucinated quote | G1-01..G1-03, G3-03, E-05, LV-06 |
| Eve (c) Canary spoof → quarantine | G3-01, G3-02, G3-05, IT-08, E-05, E-06, LV-06 |
| Zero duplicate work | I-01, I-02, IT-02, IT-07 |
| Zero lost WUs on dropout | I-03, L-06, E-04, LV-02 |
| 100% Canary catch rate | G3-05, E-06 |
| Cross-family enforcement | I-04, L-07, G2-05, W-04 |
| Holdout isolation | M-03, H-02, I-06, E-08 |
| Zero credentials | I-10, W-10, E-10 |

## 14. Not covered, and known risks

- **Sandboxing.** Dave and the Coordinator run model-written code (Rally B patches). H-04 tests a timeout only. It does not prove isolation of the file system or network. The build needs a real sandbox decision.
- **Family spoofing.** A Participant can lie about its model. G1-07 catches only a mismatch with its own registration. Canaries are the real defense. A careful attacker who answers Canaries honestly and lies elsewhere is not detected by this suite.
- **Real rate-limit strings.** The `claude` and `codex` samples are assumptions until the live mode records real ones.
- **Scale.** IT-07 uses 20 threads on one SQLite file. This plan does not test hundreds of Participants or network partitions.
