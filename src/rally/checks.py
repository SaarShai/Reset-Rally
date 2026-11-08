"""Pure checks: manifest validation, prompt rendering, output schema and checks, quorum and canary grading."""

import copy
import hashlib
import json
import math
import re
import string
import unicodedata

MAX_DEPTH = 32
TOP_KEYS = {"id", "version", "title", "owner", "track", "units", "canaries", "stages", "verify", "canary"}
STAGE_KEYS = {"mode", "prompt", "schema", "needs", "timeout_s", "lease_ttl_s", "points"}
QUORUM_KEYS = {"replicas", "agree", "distinct_families", "fields", "max_replicas", "digits"}
SCHEMA_KEYS = {"type", "properties", "required", "enum", "items", "minimum", "maximum", "minLength", "maxLength",
               "minItems", "maxItems"}
PY_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
CHECK_ARGS = {"regex": ("pattern",), "exact": ("value",), "numeric": ("min", "max"),
              "quote_span": ("source",), "hash": ("sha256",)}


class ManifestError(ValueError):
    """The message starts with the field path, e.g. "verify.quorum.agree: ..."."""


def _need(ok, path, why):
    if not ok:
        raise ManifestError(f"{path}: {why}")


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _keys(d, allowed, path):
    _need(isinstance(d, dict), path or "$", "must be an object")
    for k in d:
        _need(k in allowed, f"{path}.{k}" if path else k, "unknown key")


def _int(d, key, path, lo=None, hi=None, default=None):
    _need(key in d or default is not None, path, "required")
    v = d.setdefault(key, default)
    _need(type(v) is int and (lo is None or v >= lo) and (hi is None or v <= hi), path,
          f"must be an int in [{lo}, {hi or 'any'}]")
    return v


def _str(d, key, path):
    _need(isinstance(d.get(key), str) and d[key], path, "must be a non-empty string")


def _str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _schema(s, path, depth=0):
    _need(depth <= MAX_DEPTH, path, "too deep")
    _keys(s, SCHEMA_KEYS, path)
    if "type" in s:
        types = s["type"] if isinstance(s["type"], list) and s["type"] else [s["type"]]
        _need(all(isinstance(x, str) and (x in {*PY_TYPES, "number", "integer"}) for x in types),
              f"{path}.type", "must be a JSON type name or a list of them")
    _need(isinstance(s.get("properties", {}), dict), f"{path}.properties", "must be an object")
    for k, sub in s.get("properties", {}).items():
        _schema(sub, f"{path}.properties.{k}", depth + 1)
    if "items" in s:
        _schema(s["items"], f"{path}.items", depth + 1)
    _need(_str_list(s.get("required", [])), f"{path}.required", "must be a list of strings")
    _need(isinstance(s.get("enum", []), list), f"{path}.enum", "must be a list")
    for k in ("minimum", "maximum"):
        _need(_is_num(s.get(k, 0)), f"{path}.{k}", "must be a number")
    for k in ("minLength", "maxLength", "minItems", "maxItems"):
        _need(type(s.get(k, 0)) is int and s.get(k, 0) >= 0, f"{path}.{k}", "must be an int >= 0")


def _check_spec(c, path):
    _need(isinstance(c, dict) and isinstance(c.get("type"), str) and c["type"] in CHECK_ARGS, f"{path}.type",
          f"must be one of {sorted(CHECK_ARGS)}")
    t, args = c["type"], CHECK_ARGS[c["type"]]
    _keys(c, {"type", "field", *args}, path)
    _str(c, "field", f"{path}.field")
    if t == "numeric":
        _need(any(a in c for a in args), path, "numeric needs min and/or max")
        for a in args:
            _need(_is_num(c.get(a, 0)), f"{path}.{a}", "must be a number")
        return
    _need(args[0] in c, f"{path}.{args[0]}", "required")
    if t != "exact":
        _str(c, args[0], f"{path}.{args[0]}")
    if t == "regex":
        try:
            re.compile(c["pattern"])
        except re.error as e:
            _need(False, f"{path}.pattern", f"bad pattern: {e}")


def validate_manifest(m: dict) -> dict:
    """Return a new manifest with defaults filled; raise ManifestError naming the bad field."""
    _need(isinstance(m, dict), "$", "must be an object")
    m = copy.deepcopy(m)
    _keys(m, TOP_KEYS, "")
    _need(isinstance(m.get("id"), str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", m["id"]), "id",
          "must match ^[a-z0-9][a-z0-9-]{0,63}$")
    _int(m, "version", "version", lo=1)
    for k in ["title", "owner"] + [k for k in ("units", "canaries") if k in m]:  # units, canaries: file names
        _str(m, k, k)
    _need(m.get("track") == "brain", "track", "must be brain")
    _need(isinstance(m.get("stages"), dict) and set(m["stages"]) == {"brain"}, "stages",
          "must be exactly {brain: ...}")
    _keys(b := m["stages"]["brain"], STAGE_KEYS, "stages.brain")
    _need(b.get("mode") == "json", "stages.brain.mode", "must be json")
    _str(b, "prompt", "stages.brain.prompt")
    _schema(b.get("schema"), "stages.brain.schema")
    if "needs" in b:
        _keys(b["needs"], {"families"}, "stages.brain.needs")
        fams = b["needs"].get("families")
        _need(fams is None or (_str_list(fams) and fams), "stages.brain.needs.families",
              "must be a non-empty list of strings")
    t = _int(b, "timeout_s", "stages.brain.timeout_s", lo=1, default=900)
    _int(b, "lease_ttl_s", "stages.brain.lease_ttl_s", lo=t, default=1800)
    _int(b, "points", "stages.brain.points", lo=0, default=1)
    _keys(v := m.get("verify"), {"mode", "max_attempts", "checks", "quorum"}, "verify")
    _need(v.get("mode") in ("deterministic", "quorum"), "verify.mode", "must be deterministic or quorum")
    _int(v, "max_attempts", "verify.max_attempts", lo=1, default=3)
    _need(isinstance(v.setdefault("checks", []), list), "verify.checks", "must be a list")
    for i, c in enumerate(v["checks"]):
        _check_spec(c, f"verify.checks[{i}]")
    if v["mode"] == "quorum":
        _keys(q := v.get("quorum"), QUORUM_KEYS, "verify.quorum")
        r = _int(q, "replicas", "verify.quorum.replicas", lo=2)
        a = _int(q, "agree", "verify.quorum.agree", lo=2, hi=r)
        _need(r <= 2 * a - 1, "verify.quorum.replicas", "must be at most 2 x agree - 1, so only one group can win")
        _need(isinstance(q.setdefault("distinct_families", True), bool), "verify.quorum.distinct_families",
              "must be a bool")
        props, f = b["schema"].get("properties", {}), q.get("fields")
        _need(_str_list(f) and f and all(x in props for x in f), "verify.quorum.fields",
              "must be a non-empty list of stages.brain.schema.properties keys")
        mx = _int(q, "max_replicas", "verify.quorum.max_replicas", lo=r, hi=2 * a - 1, default=min(r + 1, 2 * a - 1))
        fams = b.get("needs", {}).get("families")
        _need(not (q["distinct_families"] and fams) or len(set(fams)) >= mx, "verify.quorum.max_replicas",
              "is more than the number of stages.brain.needs.families, so a tie-break could never find a family")
        _int(q, "digits", "verify.quorum.digits", lo=1, hi=15, default=3)
    else:
        _need("quorum" not in v, "verify.quorum", "only allowed when mode is quorum")
    _keys(c := m.setdefault("canary", {}), {"rate", "quarantine_after"}, "canary")
    _need(_is_num(rate := c.setdefault("rate", 0)) and 0 <= rate <= 1, "canary.rate", "must be a number in [0, 1]")
    _int(c, "quarantine_after", "canary.quarantine_after", lo=1, default=2)
    return m


def public_manifest(m: dict) -> dict:
    return {k: copy.deepcopy(v) for k, v in m.items() if k != "canaries"}


def manifest_hash(m: dict) -> str:
    body = json.dumps(public_manifest(m), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def render_prompt(stage: dict, payload: dict) -> str:
    values = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in payload.items()}
    text = string.Template(stage["prompt"]).safe_substitute(values)
    suffix = "\n\nReply with one JSON object only. It must match this JSON schema:\n"
    return text + suffix + json.dumps(stage["schema"], sort_keys=True)


def _is_type(v, t):
    if t in ("number", "integer"):  # ints first: math.isfinite overflows on big ints; json.loads accepts NaN
        return _is_num(v) and (isinstance(v, int) or (math.isfinite(v) and (t == "number" or v.is_integer())))
    return isinstance(t, str) and isinstance(v, PY_TYPES.get(t, ()))


def _same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)  # keeps True != 1


def check_schema(value, schema: dict, path: str = "$", _depth: int = 0) -> str | None:
    """None when value matches the schema subset, else "schema: <path>: <why>". Never raises on JSON values."""
    if _depth > MAX_DEPTH:
        return f"schema: {path}: too deep"
    v, s = value, schema if isinstance(schema, dict) else {}
    if "type" in s:
        types = s["type"] if isinstance(s["type"], list) else [s["type"]]
        if not any(_is_type(v, t) for t in types):
            return f"schema: {path}: expected {'|'.join(map(str, types))}"
    lo, hi = s.get("minimum"), s.get("maximum")
    if _is_num(v) and ((_is_num(lo) and v < lo) or (_is_num(hi) and v > hi)):
        return f"schema: {path}: outside [{lo}, {hi}]"
    if isinstance(v, (str, list)):
        unit = "Length" if isinstance(v, str) else "Items"
        lo, hi = s.get("min" + unit), s.get("max" + unit)
        if (_is_num(lo) and len(v) < lo) or (_is_num(hi) and len(v) > hi):
            return f"schema: {path}: {unit.lower()} {len(v)} outside [{lo}, {hi}]"
    kids = []
    if isinstance(v, list):
        kids = [(x, s.get("items"), f"{path}[{i}]") for i, x in enumerate(v)]
    if isinstance(v, dict):
        if missing := [k for k in (s["required"] if _str_list(s.get("required")) else []) if k not in v]:
            return f"schema: {path}.{missing[0]}: required"
        props = s.get("properties") if isinstance(s.get("properties"), dict) else {}
        kids = [(x, props.get(k), f"{path}.{k}") for k, x in v.items()]
    for x, sub, p in kids:
        if r := check_schema(x, sub, p, _depth + 1):
            return r
    # after the walk above, so the value is known to be shallow enough to serialize
    if isinstance(s.get("enum"), list) and not any(_same(v, e) for e in s["enum"]):
        return f"schema: {path}: not in enum"
    return None


def _check_ok(c, output, payload):
    if c["field"] not in output:
        return False
    t, v = c["type"], output[c["field"]]
    if t == "regex":
        return isinstance(v, str) and re.fullmatch(c["pattern"], v) is not None
    if t == "exact":
        return _same(normalize(v), normalize(c["value"]))
    if t == "numeric":
        return _is_num(v) and c.get("min", v) <= v <= c.get("max", v)
    if t == "quote_span":
        src, quotes = payload.get(c["source"]), [v] if isinstance(v, str) else v
        return isinstance(src, str) and _str_list(quotes) and all(
            (q := " ".join(x.split())) and q in " ".join(src.split()) for x in quotes)
    if t == "hash":
        return isinstance(v, str) and hashlib.sha256(v.encode()).hexdigest() == c["sha256"]


def run_checks(output: dict, payload: dict, checks: list[dict]) -> str | None:
    """None when every check passes, else "check:<type>:<field>" of the first failure."""
    bad = next((c for c in checks if not _check_ok(c, output, payload)), None)
    return bad and f"check:{bad['type']}:{bad['field']}"


def normalize(value, digits: int = 3):
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFC", value).split()).casefold()
    if isinstance(value, float):
        x = float(f"{value:.{digits}g}")
        return int(x) if x.is_integer() and abs(x) < 2**53 else x
    if isinstance(value, list):
        return sorted((normalize(v, digits) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, dict):
        return {k: normalize(value[k], digits) for k in sorted(value)}
    return value  # bool, None and int stay as they are


def quorum_key(output: dict, fields: list[str], digits: int) -> str:
    key = {f: normalize(output.get(f), digits) for f in fields}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


def grade_canary(output: dict, answer: dict, fields: list[str] | None, digits: int) -> bool:
    return all(_same(normalize(answer.get(f), digits), normalize(output.get(f), digits))
               for f in (list(answer) if fields is None else fields))
