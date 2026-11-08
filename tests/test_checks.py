import hashlib
import json

import pytest

from conftest import DROP, QUOTE, TEXT, make_det_manifest, make_manifest, make_output
from rally.checks import (
    ManifestError,
    check_schema,
    grade_canary,
    manifest_hash,
    normalize,
    public_manifest,
    quorum_key,
    render_prompt,
    run_checks,
    validate_manifest,
)

# ---------- validate_manifest ----------


def test_valid_manifests_pass_and_defaults_fill():
    m = make_manifest(stages__brain__timeout_s=DROP, stages__brain__lease_ttl_s=DROP, stages__brain__points=DROP,
                      verify__quorum__distinct_families=DROP, verify__quorum__max_replicas=DROP,
                      verify__quorum__digits=DROP, canary=DROP)
    out = validate_manifest(m)
    b, q = out["stages"]["brain"], out["verify"]["quorum"]
    assert (b["timeout_s"], b["lease_ttl_s"], b["points"]) == (900, 1800, 1)
    assert out["verify"]["max_attempts"] == 3
    assert (q["distinct_families"], q["max_replicas"], q["digits"]) == (True, 3, 3)
    assert out["canary"] == {"rate": 0, "quarantine_after": 2}
    assert "canary" not in m  # returns a new dict, input untouched
    d = make_det_manifest()
    del d["verify"]["checks"]
    assert validate_manifest(d)["verify"]["checks"] == []


def test_valid_manifest_accepts_unit_file_keys_and_needs():
    m = make_manifest(units="units.jsonl", canaries="canaries.jsonl",
                      stages__brain__needs={"families": ["anthropic", "openai", "google"]})
    out = validate_manifest(m)
    assert out["stages"]["brain"]["needs"]["families"] == ["anthropic", "openai", "google"]
    assert (out["units"], out["canaries"]) == ("units.jsonl", "canaries.jsonl")


@pytest.mark.parametrize("overrides", [
    {"stages__brain__lease_ttl_s": 60},  # equal to timeout_s
    {"verify__max_attempts": 1},
    {"verify__quorum__replicas": 3, "verify__quorum__agree": 3, "verify__quorum__max_replicas": DROP},  # default 4 < 6
    {"verify__quorum__replicas": 3, "verify__quorum__max_replicas": 3},  # agree 2: at most 3
    {"verify__quorum__replicas": 3, "verify__quorum__max_replicas": DROP},  # default min(4, 3)
    {"stages__brain__needs": {"families": ["anthropic"]}, "verify__quorum__distinct_families": False},
    {"stages__brain__schema__properties__quotes__minItems": 1,
     "stages__brain__schema__properties__quotes__items__minLength": 0},
])
def test_valid_manifest_edge_values(overrides):
    validate_manifest(make_manifest(**overrides))


BAD = [
    ("id", {"id": "Tst_Bad_Id"}),
    ("id", {"id": "-tst"}),
    ("id", {"id": "tst-" + "a" * 61}),  # 65 chars: one over the limit
    ("id", {"id": DROP}),
    ("version", {"version": 0}),
    ("version", {"version": True}),
    ("version", {"version": "1"}),
    ("title", {"title": ""}),
    ("owner", {"owner": DROP}),
    ("units", {"units": 3}),
    ("canaries", {"canaries": ""}),
    ("track", {"track": "brawn"}),
    ("track", {"track": "relay"}),
    ("stages", {"stages__brawn": {"mode": "json"}}),
    ("stages", {"stages": {}}),
    ("stages.brain.mode", {"stages__brain__mode": "patch"}),
    ("stages.brain.mode", {"stages__brain__mode": "text"}),
    ("stages.brain.prompt", {"stages__brain__prompt": ""}),
    ("stages.brain.schema", {"stages__brain__schema": "object"}),
    ("stages.brain.schema.properties.kind.format", {"stages__brain__schema__properties__kind__format": "x"}),
    ("stages.brain.schema.properties.quotes.items.pattern", {"stages__brain__schema__properties__quotes__items__pattern": "x"}),
    ("stages.brain.schema.type", {"stages__brain__schema__type": "thing"}),
    ("stages.brain.schema.required", {"stages__brain__schema__required": "kind"}),
    ("stages.brain.schema.properties.quotes.maxItems", {"stages__brain__schema__properties__quotes__maxItems": -1}),
    ("stages.brain.schema.properties.quotes.minItems", {"stages__brain__schema__properties__quotes__minItems": -1}),
    ("stages.brain.schema.properties.quotes.items.minLength",
     {"stages__brain__schema__properties__quotes__items__minLength": 1.5}),
    ("stages.brain.workspace", {"stages__brain__workspace": {"repo": "x"}}),
    ("stages.brain.needs.families", {"stages__brain__needs": {"families": "anthropic"}}),
    ("stages.brain.needs.families", {"stages__brain__needs": {"families": [1]}}),
    ("stages.brain.timeout_s", {"stages__brain__timeout_s": 0}),
    ("stages.brain.lease_ttl_s", {"stages__brain__lease_ttl_s": -5}),
    ("stages.brain.lease_ttl_s", {"stages__brain__lease_ttl_s": 59}),  # below timeout_s 60
    ("stages.brain.lease_ttl_s", {"stages__brain__timeout_s": 2000, "stages__brain__lease_ttl_s": DROP}),
    ("stages.brain.points", {"stages__brain__points": -1}),
    ("stages.brain.points", {"stages__brain__points": 1.5}),
    ("verify", {"verify": DROP}),
    ("verify.mode", {"verify__mode": "holdout"}),
    ("verify.holdout", {"verify__holdout": {"command": ["x"]}}),
    ("verify.checks", {"verify__checks": {"type": "regex"}}),
    ("verify.max_attempts", {"verify__max_attempts": 0}),
    ("verify.max_attempts", {"verify__max_attempts": True}),
    ("verify.max_attempts", {"verify__max_attempts": "3"}),
    ("verify.checks[0].type", {"verify__checks": [{"type": "eval", "field": "kind"}]}),
    ("verify.checks[0].type", {"verify__checks": [{"type": ["regex"], "field": "kind"}]}),
    ("verify.checks[0].field", {"verify__checks": [{"type": "exact", "value": 1}]}),
    ("verify.checks[0].pattern", {"verify__checks": [{"type": "regex", "field": "kind"}]}),
    ("verify.checks[0].pattern", {"verify__checks": [{"type": "regex", "field": "kind", "pattern": "("}]}),
    ("verify.checks[0].value", {"verify__checks": [{"type": "exact", "field": "kind"}]}),
    ("verify.checks[0]", {"verify__checks": [{"type": "numeric", "field": "value"}]}),
    ("verify.checks[0].max", {"verify__checks": [{"type": "numeric", "field": "value", "max": "9"}]}),
    ("verify.checks[0].source", {"verify__checks": [{"type": "quote_span", "field": "quotes"}]}),
    ("verify.checks[0].sha256", {"verify__checks": [{"type": "hash", "field": "kind"}]}),
    ("verify.quorum", {"verify__quorum": DROP}),
    ("verify.quorum.replicas", {"verify__quorum__replicas": 1}),
    ("verify.quorum.agree", {"verify__quorum__agree": 1}),
    ("verify.quorum.agree", {"verify__quorum__agree": 3}),
    ("verify.quorum.distinct_families", {"verify__quorum__distinct_families": "yes"}),
    ("verify.quorum.fields", {"verify__quorum__fields": []}),
    ("verify.quorum.fields", {"verify__quorum__fields": ["nope"]}),
    ("verify.quorum.max_replicas", {"verify__quorum__max_replicas": 1}),
    ("verify.quorum.max_replicas", {"verify__quorum__max_replicas": 4}),  # agree 2: two groups of 2 could win
    ("verify.quorum.replicas", {"verify__quorum__replicas": 4, "verify__quorum__max_replicas": DROP}),  # 2 groups of 2
    ("verify.quorum.max_replicas", {"stages__brain__needs": {"families": ["anthropic", "openai"]}}),  # 3 > 2 families
    ("stages.brain.needs.families", {"stages__brain__needs": {"families": []}}),
    ("verify.quorum.digits", {"verify__quorum__digits": 0}),
    ("verify.quorum.digits", {"verify__quorum__digits": 16}),
    ("canary.rate", {"canary__rate": 1.5}),
    ("canary.rate", {"canary__rate": -0.1}),
    ("canary.rate", {"canary__rate": True}),
    ("canary.quarantine_after", {"canary__quarantine_after": 0}),
    ("relay", {"relay": {"waves": 3}}),
    ("holdout", {"holdout": {}}),
    ("extra", {"extra": 1}),
]


@pytest.mark.parametrize("field,overrides", BAD, ids=[f"{f}-{i}" for i, (f, _) in enumerate(BAD)])
def test_validate_manifest_names_the_bad_field(field, overrides):
    with pytest.raises(ManifestError) as e:
        validate_manifest(make_manifest(**overrides))
    assert str(e.value).startswith(field + ":"), str(e.value)


def test_quorum_block_rejected_in_deterministic_mode():
    m = make_det_manifest(verify__quorum={"replicas": 2, "agree": 2, "fields": ["kind"]})
    with pytest.raises(ManifestError, match=r"^verify\.quorum:"):
        validate_manifest(m)


def test_validate_manifest_rejects_non_object_and_too_deep_schema():
    with pytest.raises(ManifestError):
        validate_manifest([])
    deep = {"type": "object"}
    for _ in range(40):
        deep = {"type": "array", "items": deep}
    with pytest.raises(ManifestError, match=r"^stages\.brain\.schema.*too deep"):
        validate_manifest(make_manifest(stages__brain__schema=deep))


# ---------- public_manifest / manifest_hash ----------


def test_public_manifest_drops_canaries():
    m = make_manifest(canaries="canaries.jsonl")
    pub = public_manifest(m)
    assert "canaries" not in pub and pub["id"] == m["id"]
    assert "canaries" in m


def test_manifest_hash_stable_under_key_order_and_changes_on_edit():
    m = make_manifest()
    reordered = json.loads(json.dumps(m, sort_keys=True))
    reordered = dict(reversed(list(reordered.items())))
    h = manifest_hash(m)
    assert h.startswith("sha256:") and len(h) == 7 + 64
    assert manifest_hash(reordered) == h
    assert manifest_hash(make_manifest(canaries="other.jsonl")) == h  # canaries are not public
    assert manifest_hash(make_manifest(stages__brain__prompt="Other ${text}")) != h
    body = json.dumps(public_manifest(m), sort_keys=True, separators=(",", ":"))
    assert h == "sha256:" + hashlib.sha256(body.encode()).hexdigest()


# ---------- render_prompt ----------


def test_render_prompt_substitutes_and_appends_schema():
    stage = {"prompt": "T=${text} D=${data} L=${items} N=${n} U=${unknown} $$", "schema": {"type": "object", "b": 1, "a": 2}}
    out = render_prompt(stage, {"text": "hi", "data": {"k": 1}, "items": [1, "x"], "n": 3})
    head, tail = out.split("\n\nReply with one JSON object only. It must match this JSON schema:\n")
    assert head == 'T=hi D={"k": 1} L=[1, "x"] N=3 U=${unknown} $'
    assert tail == json.dumps(stage["schema"], sort_keys=True)


# ---------- check_schema ----------

S = make_manifest()["stages"]["brain"]["schema"]


def test_check_schema_accepts_valid_output():
    assert check_schema(make_output(), S) is None
    assert check_schema(make_output(value=None, quotes=[]), S) is None
    assert check_schema({**make_output(), "extra": {"x": [1]}}, S) is None  # unknown properties allowed


@pytest.mark.parametrize("value,schema,path", [
    (True, {"type": "integer"}, "$"),
    (True, {"type": "number"}, "$"),
    (1.5, {"type": "integer"}, "$"),
    ("1", {"type": "number"}, "$"),
    (None, {"type": "string"}, "$"),
    (1, {"type": "boolean"}, "$"),
    ([], {"type": "object"}, "$"),
    ({}, {"type": "array"}, "$"),
    (0, {"type": "null"}, "$"),
    ("x", {"type": ["integer", "null"]}, "$"),
    ("gamma", {"enum": ["alpha", "beta"]}, "$"),
    (True, {"enum": [1]}, "$"),
    ("abcd", {"type": "string", "maxLength": 3}, "$"),
    ([1, 2, 3], {"type": "array", "maxItems": 2}, "$"),
    ("ab", {"type": "string", "minLength": 3}, "$"),
    ([1], {"type": "array", "minItems": 2}, "$"),
    ({"q": []}, {"properties": {"q": {"minItems": 1}}}, "$.q"),
    (120.5, {"type": "integer"}, "$"),
    (False, {"type": "integer"}, "$"),
    (5, {"type": "integer", "minimum": 6}, "$"),
    (5.5, {"type": "number", "maximum": 5}, "$"),
    (float("nan"), {"type": "number"}, "$"),
    (float("inf"), {"type": "number"}, "$"),
    ({"a": [1, "x"]}, {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "integer"}}}}, "$.a[1]"),
    ({"b": 1}, {"type": "object", "required": ["a"]}, "$.a"),
])
def test_check_schema_rejects(value, schema, path):
    r = check_schema(value, schema)
    assert r is not None and r.startswith(f"schema: {path}:"), r


@pytest.mark.parametrize("value,schema", [
    (3, {"type": "integer"}),
    (3, {"type": "number"}),
    (2.5, {"type": "number", "minimum": 2.5, "maximum": 2.5}),
    (None, {"type": ["integer", "null"]}),
    (False, {"type": "boolean"}),
    (1, {"enum": [1, 2]}),
    ("abc", {"type": "string", "maxLength": 3}),
    ("abc", {"type": "string", "minLength": 3}),
    ([1, 2], {"type": "array", "minItems": 2, "maxItems": 2}),
    (120.0, {"type": "integer"}),  # no fraction: an integer, as in JSON Schema
    (120.0, {"type": "integer", "minimum": 120}),
    pytest.param(10**400, {"type": "number"}, id="big-int-number"),  # json.loads makes big ints; must not raise
    pytest.param(10**400, {"type": "integer"}, id="big-int-integer"),
    ([[1], [2]], {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}),
])
def test_check_schema_accepts(value, schema):
    assert check_schema(value, schema) is None


def test_check_schema_rejects_too_deep_without_raising():
    v = []
    for _ in range(1000):
        v = [v]
    r = check_schema(v, {})
    assert r is not None and r.startswith("schema: $") and r.endswith("too deep")
    ok = []
    for _ in range(10):
        ok = [ok]
    assert check_schema(ok, {}) is None


def test_check_schema_uses_given_path():
    assert check_schema(1, {"type": "string"}, "$.out").startswith("schema: $.out:")


# ---------- run_checks ----------

P = {"text": "Line one.\n  The   dose was 5 mg.  ", "n": 3}


@pytest.mark.parametrize("check,good,bad", [
    ({"type": "regex", "field": "f", "pattern": r"[A-Z]{2}-\d"}, "ZX-3", "ZX-3a"),
    ({"type": "regex", "field": "f", "pattern": r"\d+"}, "12", 12),
    ({"type": "exact", "field": "f", "value": "Yes"}, "  yes ", "no"),
    ({"type": "exact", "field": "f", "value": 3.3}, 3.30, 3.4),
    ({"type": "exact", "field": "f", "value": 1}, 1, True),
    ({"type": "numeric", "field": "f", "min": 0, "max": 10}, 10, 10.5),
    ({"type": "numeric", "field": "f", "min": 0}, 0, -1),
    ({"type": "numeric", "field": "f", "max": 1}, 1, True),
    ({"type": "numeric", "field": "f", "max": 1}, 0.5, "0.5"),
    ({"type": "quote_span", "field": "f", "source": "text"}, "The dose was 5", "dose was 6 mg"),
    ({"type": "quote_span", "field": "f", "source": "text"}, "The dose\nwas 5 mg.", "   "),
    ({"type": "quote_span", "field": "f", "source": "text"}, ["Line one.", "dose was 5"], ["Line one.", "nope"]),
    ({"type": "quote_span", "field": "f", "source": "text"}, ["Line one."], ["Line one.", 5]),
    ({"type": "quote_span", "field": "f", "source": "missing"}, None, "Line one."),
    ({"type": "hash", "field": "f", "sha256": hashlib.sha256(b"abc").hexdigest()}, "abc", "abd"),
])
def test_run_checks_pass_and_fail(check, good, bad):
    if good is not None:
        assert run_checks({"f": good}, P, [check]) is None
    assert run_checks({"f": bad}, P, [check]) == f"check:{check['type']}:f"


def test_run_checks_quote_span_is_case_sensitive():
    c = {"type": "quote_span", "field": "f", "source": "text"}
    assert run_checks({"f": "the dose was 5"}, P, [c]) == "check:quote_span:f"


@pytest.mark.parametrize("check", [
    {"type": "regex", "field": "f", "pattern": ".*"},
    {"type": "exact", "field": "f", "value": None},
    {"type": "numeric", "field": "f", "min": 0},
    {"type": "quote_span", "field": "f", "source": "text"},
    {"type": "hash", "field": "f", "sha256": "0" * 64},
])
def test_run_checks_missing_field_fails(check):
    assert run_checks({"g": 1}, P, [check]) == f"check:{check['type']}:f"


def test_run_checks_returns_first_failure_and_empty_list_passes():
    checks = [{"type": "numeric", "field": "a", "min": 0}, {"type": "numeric", "field": "b", "min": 0}]
    assert run_checks({"a": 1, "b": -1}, P, checks) == "check:numeric:b"
    assert run_checks({}, P, []) is None
    assert run_checks(make_output(), {"text": TEXT}, make_manifest()["verify"]["checks"]) is None
    assert QUOTE in TEXT


# ---------- normalize / quorum_key / grade_canary ----------


def test_normalize_cases():
    assert normalize("Café") == normalize("Café") == "café"  # NFC
    assert normalize("  ZX-3 \n  Beta ") == "zx-3 beta"
    assert normalize("STRASSE") == normalize("straße")  # casefold, not lower
    assert normalize(3.30) == 3.3 and normalize(3.304) == 3.3
    assert normalize(12.0) == 12 and isinstance(normalize(12.0), int)
    assert normalize(123456) == 123456  # int unchanged
    assert normalize(123456.0) == 123000
    assert normalize(1.23456, 5) == 1.2346
    assert normalize(True) is True and normalize(None) is None
    assert normalize([3, "B", 1.0]) == normalize([1, "b", 3])
    assert normalize({"b": 2.0, "a": " X "}) == {"a": "x", "b": 2}
    assert list(normalize({"b": 1, "a": 2})) == ["a", "b"]


def test_quorum_key():
    k = quorum_key({"code": "zx-3", "v": 3.3, "noise": 1}, ["code", "v"], 3)
    assert k == quorum_key({"code": " ZX-3", "v": 3.30, "noise": 2}, ["code", "v"], 3)
    assert len(k) == 64 and int(k, 16) >= 0
    assert k != quorum_key({"code": "zx-3", "v": 3.4}, ["code", "v"], 3)
    assert quorum_key({"v": 1.234}, ["v"], 2) == quorum_key({"v": 1.2}, ["v"], 2)
    assert quorum_key({}, ["v"], 3) == quorum_key({"v": None}, ["v"], 3)


def test_grade_canary():
    answer = {"kind": "Alpha", "value": 1.5}
    assert grade_canary({"kind": "alpha", "value": 1.50, "quotes": ["x"]}, answer, None, 3)
    assert not grade_canary({"kind": "alpha", "value": 1.6}, answer, None, 3)
    assert not grade_canary({"kind": "alpha"}, answer, None, 3)
    assert grade_canary({"kind": "alpha", "value": 9}, answer, ["kind"], 3)
    assert not grade_canary({"kind": "beta", "value": 1.5}, answer, ["kind"], 3)
    assert not grade_canary({"v": True}, {"v": 1}, None, 3)
