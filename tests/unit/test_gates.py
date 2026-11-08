"""Unit tests for Gates 1, 2, 3, trust scores, holdout, and brawn verifiers.

Covers G1-01..G1-07, G2-01..G2-06, G3-01..G3-06, H-01, H-03, and H-04.
"""

import builtins
import json
from pathlib import Path
import time
import pytest

from rally import (
    MAX_OUTPUT_BYTES,
    QUARANTINE_BELOW,
    TIEBREAK_MIN_TRUST,
    TRUST_START,
)
from rally.checks import (
    check_gate1,
    check_patch_code,
    check_quote_span,
    eval_safe_expr,
    evaluate_quorum,
    grade_canary,
    load_manifest,
    public_manifest,
    quorum_key,
    update_trust,
)
from fixtures.rally import verifiers

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rally"


# --- Gate 1 Tests ---


def test_g1_01_truncated_json():
    """Verify truncated JSON raises gate1:json (G1-01)."""
    manifest = {"gate1_kind": "quote_span"}
    unit = {"payload": {"text": "source text"}}
    with pytest.raises(ValueError, match="gate1:json"):
        check_gate1('{"component": "QT-9", "supply_v": 9,', manifest=manifest, unit=unit)


def test_g1_02_schema_validation():
    """Verify schema mismatch raises gate1:schema (G1-02)."""
    manifest = {
        "schema": {
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "supply_v": {"type": "number"},
                "quote": {"type": "string"},
            },
            "required": ["component", "supply_v", "quote"],
        }
    }
    unit = {}
    # Missing required 'quote'
    with pytest.raises(ValueError, match="gate1:schema"):
        check_gate1({"component": "QT-9", "supply_v": 9.0}, manifest=manifest, unit=unit)

    # supply_v is string instead of number
    with pytest.raises(ValueError, match="gate1:schema"):
        check_gate1(
            {"component": "QT-9", "supply_v": "9.0", "quote": "9 V"},
            manifest=manifest,
            unit=unit,
        )


def test_g1_03_quote_span():
    """Verify quote span checks source substring and number inclusion (G1-03)."""
    source = "The ZX-3 sensor runs at 3.3 V. Its I/O pins tolerate 5.0 V."

    # Quote not in source -> raises gate1:quote_span
    with pytest.raises(ValueError, match="gate1:quote_span"):
        check_quote_span("nonexistent sensor quote", source, 3.3)

    # Quote with extra whitespace -> passes after collapse
    check_quote_span("  runs   at   3.3  V.  ", source, 3.3)

    # Quote in source but missing the number -> raises gate1:quote_span
    with pytest.raises(ValueError, match="gate1:quote_span"):
        check_quote_span("Its I/O pins tolerate", source, 3.3)


def test_g1_04_eval_safe_expr(monkeypatch):
    """Verify safe AST evaluation rejects attacks and never calls builtins.eval (G1-04)."""
    def forbidden_eval(*args, **kwargs):
        raise AssertionError("builtins.eval was called")

    monkeypatch.setattr(builtins, "eval", forbidden_eval)

    # Valid closed form
    assert eval_safe_expr("(n*(n+1)//2)**2", 4) == 100

    # Code injection
    with pytest.raises(ValueError, match="gate1:expr"):
        eval_safe_expr("__import__('os').system('x')", 1)

    # Attribute access
    with pytest.raises(ValueError, match="gate1:expr"):
        eval_safe_expr("n.__class__", 1)

    # DoS exponentiation
    with pytest.raises(ValueError, match="gate1:expr"):
        eval_safe_expr("2**10**10", 1)


def test_g1_05_check_patch_code():
    """Verify patch syntax errors and missing required function raise gate1:patch (G1-05)."""
    # Syntax error
    with pytest.raises(ValueError, match="gate1:patch"):
        check_patch_code("def count_primes(n: invalid syntax")

    # Missing count_primes function
    with pytest.raises(ValueError, match="gate1:patch"):
        check_patch_code("def helper(n):\n    return n\n")

    # Valid patch
    check_patch_code("def count_primes(n):\n    return 0\n")


def test_g1_06_output_size_cap():
    """Verify output exceeding max_output_bytes raises gate1:size (G1-06)."""
    manifest = {"max_output_bytes": 100}
    unit = {}
    large_payload = {"data": "x" * 200}
    with pytest.raises(ValueError, match="gate1:size"):
        check_gate1(large_payload, manifest=manifest, unit=unit)


def test_g1_07_family_verification():
    """Verify family mismatch raises gate1:family (G1-07)."""
    def resolver(model_id: str) -> str:
        if "codex" in model_id:
            return "openai"
        if "deepseek" in model_id:
            return "deepseek"
        return "unknown"

    manifest = {}
    unit = {}
    with pytest.raises(ValueError, match="gate1:family"):
        check_gate1(
            {"data": 1},
            manifest=manifest,
            unit=unit,
            expected_family="deepseek",
            reported_model="codex/gpt-6-astra",
            family_resolver=resolver,
        )


# --- Gate 2 Tests ---


def test_g2_01_quorum_agreement():
    """Verify agreement across distinct families commits the result (G2-01)."""
    fields = ["component", "supply_v"]
    votes = [
        {"participant": "alice", "family": "anthropic", "output": {"component": "ZX-3", "supply_v": 3.3}},
        {"participant": "bob", "family": "openai", "output": {"component": "zx-3", "supply_v": 3.30}},
    ]
    res = evaluate_quorum(votes, fields, k=2)
    assert res["status"] == "committed"
    assert res["winners"] == ["alice", "bob"]
    assert res["minorities"] == []
    assert res["result"]["supply_v"] == 3.3


def test_g2_02_quorum_dispute():
    """Verify conflict between 2 distinct families produces disputed status (G2-02)."""
    fields = ["component", "supply_v"]
    votes = [
        {"participant": "alice", "family": "anthropic", "output": {"component": "ZX-3", "supply_v": 3.3}},
        {"participant": "bob", "family": "openai", "output": {"component": "ZX-3", "supply_v": 5.0}},
    ]
    res = evaluate_quorum(votes, fields, k=2)
    assert res["status"] == "disputed"
    assert res["exclude_families"] == {"anthropic", "openai"}


def test_g2_03_tiebreak_resolution():
    """Verify tie-breaker resolves dispute and assigns minority status (G2-03)."""
    fields = ["component", "supply_v"]
    votes = [
        {"participant": "alice", "family": "anthropic", "output": {"component": "ZX-3", "supply_v": 3.3}},
        {"participant": "bob", "family": "openai", "output": {"component": "ZX-3", "supply_v": 5.0}},
        {"participant": "carol", "family": "google", "output": {"component": "ZX-3", "supply_v": 3.3}},
    ]
    res = evaluate_quorum(votes, fields, k=2)
    assert res["status"] == "committed"
    assert set(res["winners"]) == {"alice", "carol"}
    assert res["minorities"] == ["bob"]
    assert res["result"]["supply_v"] == 3.3


def test_g2_04_tiebreak_no_consensus():
    """Verify tie-breaker disagreeing with both previous votes fails with no_consensus (G2-04)."""
    fields = ["component", "supply_v"]
    votes = [
        {"participant": "alice", "family": "anthropic", "output": {"component": "ZX-3", "supply_v": 3.3}},
        {"participant": "bob", "family": "openai", "output": {"component": "ZX-3", "supply_v": 5.0}},
        {"participant": "carol", "family": "google", "output": {"component": "ZX-3", "supply_v": 12.0}},
    ]
    res = evaluate_quorum(votes, fields, k=2)
    assert res["status"] == "failed"
    assert res["reason"] == "no_consensus"


def test_g2_05_same_family_cannot_commit():
    """Verify two votes from the same family cannot commit a quorum (G2-05)."""
    fields = ["component", "supply_v"]
    votes = [
        {"participant": "alice", "family": "anthropic", "output": {"component": "ZX-3", "supply_v": 3.3}},
        {"participant": "alice2", "family": "anthropic", "output": {"component": "ZX-3", "supply_v": 3.3}},
    ]
    res = evaluate_quorum(votes, fields, k=2)
    assert res["status"] != "committed"


def test_g2_06_casefold_normalization():
    """Verify quorum_key normalizes case and numbers (G2-06)."""
    fields = ["component", "supply_v"]
    k1 = quorum_key({"component": "zx-3", "supply_v": 3.3}, fields)
    k2 = quorum_key({"component": "ZX-3", "supply_v": 3.30}, fields)
    assert k1 == k2


# --- Gate 3 & Trust Score Tests ---


def test_g3_01_canary_pass():
    """Verify Canary pass increases trust by 0.1 without quarantine (G3-01)."""
    new_trust, quarantined = update_trust(TRUST_START, "canary_pass")
    assert new_trust == 0.6
    assert not quarantined


def test_g3_02_canary_fail():
    """Verify Canary fail drops trust by 0.5 and quarantines when below 0.2 (G3-02)."""
    new_trust, quarantined = update_trust(0.3, "canary_fail")
    assert new_trust == 0.0
    assert quarantined


def test_g3_03_gate1_rejects_no_immediate_quarantine():
    """Verify two Gate 1 rejects from 0.5 drop trust to 0.3 without quarantine (G3-03)."""
    trust, q1 = update_trust(0.5, "gate1_reject")
    assert trust == 0.4
    assert not q1
    trust, q2 = update_trust(trust, "gate1_reject")
    assert trust == 0.3
    assert not q2


def test_g3_04_tiebreak_eligibility_threshold():
    """Verify tie-breaker eligibility obeys TIEBREAK_MIN_TRUST (G3-04)."""
    assert 0.5 < TIEBREAK_MIN_TRUST
    assert 0.6 >= TIEBREAK_MIN_TRUST


def test_g3_05_canary_grader_catch_rate():
    """Verify canary grading catches 4 lazy strategies (100%) and passes truth (0% false fail) (G3-05)."""
    fields = ["component", "supply_v"]
    truth = {"component": "KC-7 core", "supply_v": 1.8, "quote": "core supply is 1.8 V."}

    lazy_answers = [
        {"component": "constant", "supply_v": 0.0},
        {"component": "KC-7", "supply_v": 12.0},  # First number in text
        {"component": "", "supply_v": 0.0},
        {"component": "random_val", "supply_v": 99.9},
    ]

    for lazy in lazy_answers:
        assert not grade_canary(lazy, truth, fields), f"Failed to catch lazy answer: {lazy}"

    assert grade_canary(truth, truth, fields)


def test_g3_06_canary_wu_looks_normal():
    """Verify served Canary WU has same key set and types as normal WU (G3-06, I-07)."""
    manifest_c = load_manifest(FIXTURES_DIR / "c_corpus.json")
    normal_wu = manifest_c["units"][0]
    canary_wu = manifest_c["canaries"][0]

    # Normal WU keys vs Canary WU keys
    assert set(normal_wu.keys()) == {"id", "payload", "prompt"}
    assert set(normal_wu["payload"].keys()) == {"text"}
    assert "canary" not in normal_wu
    assert "is_canary" not in normal_wu

    assert set(canary_wu["payload"].keys()) == {"text"}
    assert isinstance(canary_wu["id"], str)
    assert isinstance(canary_wu["prompt"], str)


# --- Holdout & Brawn Tests ---


def test_h01_bob1_fails_holdout():
    """Verify P-bob-1 passes public cases but fails holdout split with IndexError (H-01)."""
    bob1_code = """
def count_primes(n):
    s = [True] * (n + 1)
    s[0] = False
    s[1] = False
    for i in range(2, int(n**0.5) + 1):
        if s[i]:
            s[i*i:n+1:i] = [False] * len(range(i*i, n+1, i))
    return sum(s)
"""
    # Public cases pass
    res_pub = verifiers.verify_b(bob1_code)
    assert res_pub["verdict"] == "pass"

    # Holdout cases fail (IndexError at n = 0)
    assert not verifiers.run_holdout_b(bob1_code)


def test_h03_deterministic_settrace_metric():
    """Verify deterministic settrace line count ordering across 3 runs: bob2 < alice < naive (H-03)."""
    naive_code = """
def count_primes(n):
    cnt = 0
    for i in range(2, n + 1):
        prime = True
        for d in range(2, int(i**0.5) + 1):
            if i % d == 0:
                prime = False
                break
        if prime:
            cnt += 1
    return cnt
"""
    alice_code = """
def count_primes(n):
    if n < 2:
        return 0
    s = [True] * (n + 1)
    s[0] = s[1] = False
    for i in range(2, int(n**0.5) + 1):
        if s[i]:
            s[i*i:n+1:i] = [False] * len(range(i*i, n+1, i))
    return sum(s)
"""
    bob2_code = """
def count_primes(n):
    if n < 2:
        return 0
    if n < 3:
        return 1
    m = (n - 1) // 2
    s = [True] * (m + 1)
    for i in range(1, int((n**0.5 - 1) / 2) + 1):
        if s[i]:
            p = 2 * i + 1
            s[2*i*(i+1):m+1:p] = [False] * len(range(2*i*(i+1), m+1, p))
    return 1 + sum(s[1:])
"""

    naive_counts = [verifiers.verify_b(naive_code)["lines"] for _ in range(3)]
    alice_counts = [verifiers.verify_b(alice_code)["lines"] for _ in range(3)]
    bob2_counts = [verifiers.verify_b(bob2_code)["lines"] for _ in range(3)]

    # Determinism across runs
    assert len(set(naive_counts)) == 1
    assert len(set(alice_counts)) == 1
    assert len(set(bob2_counts)) == 1

    # Ordering
    assert bob2_counts[0] < alice_counts[0] < naive_counts[0]


def test_h04_brawn_timeout_enforcement():
    """Verify brawn verifier terminates infinite loop and returns timeout verdict in < 1s (H-04)."""
    bad_code = """
def count_primes(n):
    while True:
        pass
"""
    t0 = time.time()
    rc, out, err = verifiers.run(
        f"Item: VB1\nCandidate: {bad_code}\ntimeout_s: 0.2"
    )
    elapsed = time.time() - t0

    assert elapsed < 1.0
    assert rc == 124
    parsed = json.loads(out)
    assert parsed["verdict"] == "timeout"
    assert "timeout" in err
