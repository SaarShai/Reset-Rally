"""Gate checks, expression evaluation, quorum, and canary grading for Reset Rally.

This module provides deterministic verification functions and state transitions.
It uses only the Python standard library.
"""

import ast
import copy
import json
import os
from pathlib import Path
from typing import Any

from rally import (
    CANARY_FAIL,
    CANARY_PASS,
    GATE1_REJECT,
    MAX_OUTPUT_BYTES,
    QUARANTINE_BELOW,
    TIEBREAK_MIN_TRUST,
    TRUST_START,
)


class ManifestError(ValueError):
    """Raised when a rally manifest fails validation."""


def load_manifest(data_or_path: str | bytes | os.PathLike | dict) -> dict:
    """Load and validate a rally project manifest.

    Validates required fields: id, track, verification mode, quorum k, and canaries.
    Raises ManifestError with the field name on validation failure.
    """
    if isinstance(data_or_path, dict):
        manifest = copy.deepcopy(data_or_path)
    elif isinstance(data_or_path, (str, bytes, os.PathLike)):
        p = Path(data_or_path) if not isinstance(data_or_path, bytes) else None
        if p and p.is_file():
            manifest = json.loads(p.read_text(encoding="utf-8"))
        else:
            raw_str = data_or_path.decode("utf-8") if isinstance(data_or_path, bytes) else str(data_or_path)
            manifest = json.loads(raw_str)
    else:
        raise ManifestError("manifest: invalid type")

    if not manifest.get("id"):
        raise ManifestError("id: missing or empty")

    track = manifest.get("track")
    if track not in {"brain", "brawn", "relay"}:
        raise ManifestError(f"track: invalid value '{track}'")

    v_block = manifest.get("verification") or manifest.get("verify")
    if not v_block:
        raise ManifestError("verification: missing verification block")

    if isinstance(v_block, dict):
        mode = v_block.get("mode")
        k = v_block.get("quorum_k", v_block.get("k", v_block.get("quorum", {}).get("agree", v_block.get("quorum", {}).get("replicas"))))
    elif isinstance(v_block, str):
        mode = v_block
        k = manifest.get("quorum_k")
    else:
        raise ManifestError("verification: invalid verification format")

    if mode not in {"deterministic", "quorum", "holdout"}:
        raise ManifestError(f"mode: invalid mode '{mode}'")

    if mode == "quorum":
        if k is None or k < 2:
            raise ManifestError(f"quorum_k: quorum mode requires k >= 2, got {k}")

    canary = manifest.get("canary")
    if canary:
        canaries = manifest.get("canaries")
        if not canaries:
            raise ManifestError("canaries: canary enabled but canaries list is empty")

    return manifest


def public_manifest(manifest: dict) -> dict:
    """Return a deep copy of the manifest with holdout and canary secrets removed."""
    pub = copy.deepcopy(manifest)
    pub.pop("holdout", None)
    pub.pop("canaries", None)
    for block_key in ("verify", "verification"):
        if isinstance(pub.get(block_key), dict):
            pub[block_key].pop("holdout", None)
    return pub


def eval_safe_expr(expr: str, n: int) -> int | float:
    """Safely evaluate a mathematical expression for a given integer n.

    Uses an allowlist of AST nodes. Rejects expressions with exponents > 1000
    or values with magnitude > 10**100. Never calls builtins.eval.
    Raises ValueError("gate1:expr") on any violation.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("gate1:expr")

    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except Exception:
        raise ValueError("gate1:expr")

    def _eval(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("gate1:expr")
            if abs(node.value) > 10**100:
                raise ValueError("gate1:expr")
            return node.value
        if isinstance(node, ast.Name):
            if node.id != "n":
                raise ValueError("gate1:expr")
            return n
        if isinstance(node, ast.UnaryOp):
            operand = _eval(node.operand)
            if isinstance(node.op, ast.UAdd):
                val = +operand
            elif isinstance(node.op, ast.USub):
                val = -operand
            else:
                raise ValueError("gate1:expr")
            if abs(val) > 10**100:
                raise ValueError("gate1:expr")
            return val
        if isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Add):
                val = left + right
            elif isinstance(node.op, ast.Sub):
                val = left - right
            elif isinstance(node.op, ast.Mult):
                val = left * right
            elif isinstance(node.op, ast.Div):
                if right == 0:
                    raise ValueError("gate1:expr")
                val = left / right
            elif isinstance(node.op, ast.FloorDiv):
                if right == 0:
                    raise ValueError("gate1:expr")
                val = left // right
            elif isinstance(node.op, ast.Pow):
                if not isinstance(right, (int, float)) or abs(right) > 1000:
                    raise ValueError("gate1:expr")
                if abs(left) > 1 and right > 333:
                    raise ValueError("gate1:expr")
                try:
                    val = left ** right
                except Exception:
                    raise ValueError("gate1:expr")
            else:
                raise ValueError("gate1:expr")

            if isinstance(val, complex) or abs(val) > 10**100:
                raise ValueError("gate1:expr")
            return val

        raise ValueError("gate1:expr")

    return _eval(tree.body)


def validate_schema(obj: Any, schema: dict) -> None:
    """Validate obj against a JSON schema subset.

    Checks type, required, properties, minimum, maximum, and enum.
    Raises ValueError("gate1:schema") on mismatch.
    """
    if not isinstance(schema, dict):
        return

    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(obj, dict):
            raise ValueError("gate1:schema")
    elif expected_type == "string":
        if not isinstance(obj, str):
            raise ValueError("gate1:schema")
    elif expected_type == "integer":
        if isinstance(obj, bool) or not isinstance(obj, int):
            raise ValueError("gate1:schema")
    elif expected_type == "number":
        if isinstance(obj, bool) or not isinstance(obj, (int, float)):
            raise ValueError("gate1:schema")
    elif expected_type == "boolean":
        if not isinstance(obj, bool):
            raise ValueError("gate1:schema")
    elif expected_type == "array":
        if not isinstance(obj, list):
            raise ValueError("gate1:schema")

    if "enum" in schema:
        if obj not in schema["enum"]:
            raise ValueError("gate1:schema")

    if "minimum" in schema:
        if not isinstance(obj, (int, float)) or obj < schema["minimum"]:
            raise ValueError("gate1:schema")

    if "maximum" in schema:
        if not isinstance(obj, (int, float)) or obj > schema["maximum"]:
            raise ValueError("gate1:schema")

    if isinstance(obj, dict):
        for req in schema.get("required", []):
            if req not in obj:
                raise ValueError("gate1:schema")
        props = schema.get("properties", {})
        for prop, prop_schema in props.items():
            if prop in obj:
                validate_schema(obj[prop], prop_schema)

    if isinstance(obj, list) and "items" in schema:
        for item in obj:
            validate_schema(item, schema["items"])


def check_quote_span(quote: str, source: str, number_val: float | int | None = None) -> None:
    """Verify that quote appears in source after whitespace collapse.

    If number_val is given, verifies its string representation appears in quote.
    Raises ValueError("gate1:quote_span") on failure.
    """
    if not isinstance(quote, str) or not isinstance(source, str):
        raise ValueError("gate1:quote_span")

    q_norm = " ".join(quote.split())
    s_norm = " ".join(source.split())
    if not q_norm or q_norm not in s_norm:
        raise ValueError("gate1:quote_span")

    if number_val is not None:
        candidates = [str(number_val)]
        if isinstance(number_val, (int, float)) and number_val == int(number_val):
            candidates.append(str(int(number_val)))
        if not any(c in q_norm for c in candidates):
            raise ValueError("gate1:quote_span")


def check_patch_code(code: str, required_fn: str = "count_primes") -> None:
    """Parse python code and verify a FunctionDef with required_fn exists.

    Raises ValueError("gate1:patch") on syntax error or missing function.
    """
    if not isinstance(code, str):
        raise ValueError("gate1:patch")
    try:
        tree = ast.parse(code)
    except Exception:
        raise ValueError("gate1:patch")

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == required_fn:
            return
    raise ValueError("gate1:patch")


def check_gate1(
    raw_output: str | bytes | dict,
    *,
    manifest: dict,
    unit: dict,
    expected_family: str | None = None,
    reported_model: str | None = None,
    family_resolver: Any = None,
) -> dict:
    """Run Gate 1 deterministic validation on worker output.

    Checks payload size limit, model family consistency, JSON syntax,
    JSON schema compliance, and domain-specific rules (expr, patch, quote_span).
    Returns the parsed output dictionary.
    """
    max_bytes = manifest.get("max_output_bytes", MAX_OUTPUT_BYTES)
    if isinstance(raw_output, bytes):
        size = len(raw_output)
    elif isinstance(raw_output, str):
        size = len(raw_output.encode("utf-8"))
    elif isinstance(raw_output, dict):
        size = len(json.dumps(raw_output).encode("utf-8"))
    else:
        size = 0

    if size > max_bytes:
        raise ValueError("gate1:size")

    if expected_family and reported_model and family_resolver:
        if family_resolver(reported_model) != expected_family:
            raise ValueError("gate1:family")

    kind = manifest.get("gate1_kind") or unit.get("gate1_kind")
    if isinstance(raw_output, dict):
        parsed = raw_output
    else:
        raw_str = raw_output.decode("utf-8") if isinstance(raw_output, bytes) else str(raw_output)
        if kind == "patch":
            try:
                parsed = json.loads(raw_str)
            except Exception:
                parsed = {"code": raw_str}
        else:
            try:
                parsed = json.loads(raw_str)
            except Exception:
                raise ValueError("gate1:json")

    if not isinstance(parsed, dict):
        raise ValueError("gate1:json")

    schema = manifest.get("schema") or unit.get("schema")
    if schema:
        validate_schema(parsed, schema)

    if kind == "expr":
        expr = parsed.get("expr") or parsed.get("closed_form") or parsed.get("answer")
        if not expr or not isinstance(expr, str):
            raise ValueError("gate1:expr")
        eval_safe_expr(expr, n=1)
    elif kind in ("number", "Number"):
        val = parsed.get("supply_v", parsed.get("number", parsed.get("dt")))
        if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError("gate1:number")
    elif kind == "quote_span":
        quote = parsed.get("quote")
        source = (
            unit.get("payload", {}).get("text")
            or unit.get("source")
            or unit.get("text", "")
        )
        num = parsed.get("supply_v", parsed.get("number"))
        check_quote_span(quote, source, num)
    elif kind == "patch":
        code = parsed.get("code") or parsed.get("patch")
        check_patch_code(code, "count_primes")

    return parsed


def quorum_key(output: dict, fields: list[str], tol: float = 0.01) -> tuple:
    """Normalize output fields into a canonical quorum comparison key.

    Strips and casefolds strings. Rounds numbers to nearest multiple of tol.
    """
    key_parts = []
    for f in fields:
        v = output.get(f)
        if isinstance(v, str):
            key_parts.append(v.strip().casefold())
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            rounded = round(float(v) / tol) * tol
            key_parts.append(f"{rounded:.2f}")
        else:
            key_parts.append(v)
    return tuple(key_parts)


def evaluate_quorum(
    votes: list[dict],
    fields: list[str],
    k: int = 2,
    tol: float = 0.01,
) -> dict:
    """Evaluate cross-model agreement for a set of replica votes.

    Votes must have: participant, family, output.
    Enforces at most one vote per participant and distinct model families for consensus.
    Returns status: committed, disputed, failed, or pending.
    """
    seen_participants = set()
    unique_votes = []
    for v in votes:
        p = v.get("participant")
        if p not in seen_participants:
            seen_participants.add(p)
            unique_votes.append(v)

    grouped: dict[tuple, list[dict]] = {}
    for v in unique_votes:
        key = quorum_key(v["output"], fields, tol)
        grouped.setdefault(key, []).append(v)

    for key, key_votes in grouped.items():
        distinct_families = {v["family"] for v in key_votes}
        if len(distinct_families) >= k:
            winners = [v["participant"] for v in key_votes]
            minorities = [v["participant"] for v in unique_votes if v not in key_votes]
            canonical_output = key_votes[0]["output"]
            return {
                "status": "committed",
                "result": canonical_output,
                "winners": winners,
                "minorities": minorities,
            }

    if len(unique_votes) >= 3:
        return {"status": "failed", "reason": "no_consensus"}

    all_families = {v["family"] for v in unique_votes}
    if len(all_families) >= 2:
        return {"status": "disputed", "exclude_families": all_families}

    return {"status": "pending"}


def grade_canary(output: dict, truth: dict, fields: list[str], tol: float = 0.01) -> bool:
    """Grade worker output against hidden ground truth using normalizer keys."""
    return quorum_key(output, fields, tol) == quorum_key(truth, fields, tol)


def update_trust(trust: float, event: str) -> tuple[float, bool]:
    """Compute new trust score and quarantine status following an event.

    Events: canary_pass (+0.1), canary_fail (-0.5), gate1_reject (-0.1).
    Clamps trust to [0.0, 1.0]. Sets is_quarantined if new_trust < QUARANTINE_BELOW.
    """
    deltas = {
        "canary_pass": CANARY_PASS,
        "canary_fail": CANARY_FAIL,
        "gate1_reject": GATE1_REJECT,
    }
    delta = deltas.get(event, 0.0)
    new_trust = round(max(0.0, min(1.0, trust + delta)), 4)
    is_quarantined = (event == "canary_fail" and new_trust < QUARANTINE_BELOW) or (new_trust < QUARANTINE_BELOW)
    return new_trust, is_quarantined
