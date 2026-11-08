"""Deterministic verifiers for Rally A and Rally B work units.

This module provides exact verifiers for mathematical formulas, leapfrog
numerical simulation, prime counting code optimization, and holdout splits.
"""

import json
from pathlib import Path
import re
import sys
import time
from typing import Any

from rally.checks import eval_safe_expr

INITIAL_STATES_A4 = [
    (1.0, 0.0),
    (0.0, 1.0),
    (0.5, 0.5),
    (1.0, 1.0),
    (-1.0, 0.5),
    (0.7, -0.7),
    (2.0, 0.0),
    (0.0, -1.5),
    (-1.0, -1.0),
    (0.3, 0.4),
]

PUBLIC_CASES_B = {
    10: 4,
    100: 25,
    1000: 168,
    5000: 669,
}

HOLDOUT_CASES_B = {
    0: 0,
    1: 0,
    2: 1,
    3: 2,
    7919: 1000,
    10007: 1230,
}


def verify_a1(expr: str) -> dict:
    """Verify sum of k^3 for n = 1..200 against closed form (n*(n+1)//2)**2."""
    for n in range(1, 201):
        expected = (n * (n + 1) // 2) ** 2
        try:
            actual = eval_safe_expr(expr, n)
        except Exception:
            return {"verdict": "fail", "reason": "eval error"}
        if actual != expected:
            return {"verdict": "fail", "n": n, "expected": expected, "actual": actual}
    return {"verdict": "pass"}


def verify_a2(expr: str) -> dict:
    """Verify sum of k^2 for n = 1..200 against closed form n*(n+1)*(2n+1)//6."""
    for n in range(1, 201):
        expected = n * (n + 1) * (2 * n + 1) // 6
        try:
            actual = eval_safe_expr(expr, n)
        except Exception:
            return {"verdict": "fail", "reason": "eval error"}
        if actual != expected:
            return {"verdict": "fail", "n": n, "expected": expected, "actual": actual}
    return {"verdict": "pass"}


def verify_a3(expr: str) -> dict:
    """Verify sum of (2k-1) for n = 1..200 against closed form n**2."""
    for n in range(1, 201):
        expected = n ** 2
        try:
            actual = eval_safe_expr(expr, n)
        except Exception:
            return {"verdict": "fail", "reason": "eval error"}
        if actual != expected:
            return {"verdict": "fail", "n": n, "expected": expected, "actual": actual}
    return {"verdict": "pass"}


def verify_ka(expr: str = "n**3") -> dict:
    """Verify sum of k^2 using wrong candidate n**3. Always returns fail."""
    return verify_a2(expr)


def verify_a4(dt: float) -> dict:
    """Verify leapfrog time step dt for harmonic oscillator (omega = 1.0).

    Simulates 1000 steps across 10 initial states. Passes if 0 < dt <= 0.5
    and max relative energy drift < 1e-2. Fails/diverges for dt = 2.5.
    """
    try:
        dt = float(dt)
    except Exception:
        return {"verdict": "fail", "reason": "invalid dt"}

    if dt <= 0 or dt > 0.5:
        return {"verdict": "fail", "reason": "dt out of bounds"}

    max_drift = 0.0
    for x0, v0 in INITIAL_STATES_A4:
        x, v = x0, v0
        e0 = 0.5 * (v**2 + x**2)
        if e0 == 0:
            continue
        v_half = v + 0.5 * dt * (-x)
        for _ in range(1000):
            x = x + dt * v_half
            a = -x
            v_next_half = v_half + dt * a
            v_full = v_half + 0.5 * dt * a
            e = 0.5 * (v_full**2 + x**2)
            drift = abs(e - e0) / e0
            if drift > max_drift:
                max_drift = drift
            if drift >= 1e-2 or drift != drift or abs(x) > 1e10:
                return {"verdict": "fail", "max_drift": max_drift}
            v_half = v_next_half

    return {"verdict": "pass", "max_drift": max_drift}


def count_settrace_lines(fn: Any, arg: int) -> int:
    """Count line events during fn(arg) execution using sys.settrace."""
    lines = 0
    fn_name = fn.__name__

    def tracer(frame, event, _arg):
        nonlocal lines
        if event == "line" and frame.f_code.co_name == fn_name:
            lines += 1
        return tracer

    old_trace = sys.gettrace()
    sys.settrace(tracer)
    try:
        fn(arg)
    finally:
        sys.settrace(old_trace)
    return lines


def run_with_timeout(fn: Any, arg: int, timeout_s: float = 2.0) -> int:
    """Run fn(arg) under sys.settrace and raise TimeoutError on timeout."""
    start = time.time()

    def tracer(frame, event, _arg):
        if time.time() - start > timeout_s:
            raise TimeoutError("timeout")
        return tracer

    old_trace = sys.gettrace()
    sys.settrace(tracer)
    try:
        return fn(arg)
    finally:
        sys.settrace(old_trace)


def verify_b(code: str, timeout_s: float = 2.0) -> dict:
    """Verify count_primes patch on public cases and measure settrace lines at n = 5000.

    Returns {"verdict": "pass", "lines": int} or {"verdict": "fail"}.
    Raises TimeoutError on infinite loop.
    """
    start = time.time()

    def tracer(frame, event, _arg):
        if time.time() - start > timeout_s:
            raise TimeoutError("timeout")
        return tracer

    old_trace = sys.gettrace()
    sys.settrace(tracer)
    try:
        loc: dict[str, Any] = {}
        exec(code, loc)
    finally:
        sys.settrace(old_trace)

    fn = loc.get("count_primes")
    if not fn or not callable(fn):
        return {"verdict": "fail", "reason": "missing count_primes function"}

    for inp, expected in PUBLIC_CASES_B.items():
        got = run_with_timeout(fn, inp, timeout_s=timeout_s)
        if got != expected:
            return {"verdict": "fail", "input": inp, "expected": expected, "actual": got}

    lines = count_settrace_lines(fn, 5000)
    return {"verdict": "pass", "lines": lines}


def run_holdout_b(code: str, holdout_cases: dict | None = None) -> bool:
    """Run hidden holdout verification for count_primes patch.

    Checks n in {0, 1, 2, 3, 7919, 10007}. Returns True on pass, False on fail.
    """
    cases = holdout_cases if holdout_cases is not None else HOLDOUT_CASES_B
    try:
        loc: dict[str, Any] = {}
        exec(code, loc)
        fn = loc.get("count_primes")
        if not fn or not callable(fn):
            return False
        for inp_str, expected in cases.items():
            inp = int(inp_str)
            if fn(inp) != expected:
                return False
        return True
    except Exception:
        return False


def run(prompt: str) -> tuple[int, str, str]:
    """Execute a verification task from Dave's brawn CLI.

    Returns (rc, stdout, stderr).
    """
    timeout_s = 2.0
    m_timeout = None
    parsed_json = None
    if prompt.strip().startswith("{"):
        try:
            parsed_json = json.loads(prompt.strip())
        except Exception:
            parsed_json = None

    item = ""
    candidate = ""
    if parsed_json:
        item = parsed_json.get("item") or parsed_json.get("id") or ""
        candidate = (
            parsed_json.get("code")
            or parsed_json.get("patch")
            or parsed_json.get("expr")
            or str(parsed_json.get("dt", ""))
        )
    else:
        m = re.search(r"^Item:\s*(\S+)", prompt, re.M)
        if m:
            item = m[1]
        m_timeout = re.search(r"timeout[_\s]*s?[:=]\s*([0-9.]+)", prompt)
    if m_timeout:
        timeout_s = float(m_timeout[1])

    if not candidate:
        if "def count_primes" in prompt:
            m_code = re.search(r"(def count_primes[\s\S]+?)(?=\n[A-Za-z0-9_-]+:|\Z)", prompt)
            candidate = m_code[1].strip() if m_code else prompt
        elif re.search(r"^(?:Candidate|Expr|Code|dt):\s*(.+)$", prompt, re.M):
            candidate = re.search(r"^(?:Candidate|Expr|Code|dt):\s*(.+)$", prompt, re.M)[1].strip()
        else:
            lines = [ln for ln in prompt.splitlines() if not re.match(r"^(?:Item|timeout[_\s]*s?):", ln)]
            candidate = "\n".join(lines).strip()

    try:
        if item in ("VA1", "A1"):
            res = verify_a1(candidate)
        elif item in ("VA2", "A2"):
            res = verify_a2(candidate)
        elif item in ("VA3", "A3"):
            res = verify_a3(candidate)
        elif item in ("VA4", "VA4a", "VA4b", "A4"):
            dt = float(candidate) if candidate else 0.0
            res = verify_a4(dt)
        elif item in ("KA", "wu-ka"):
            res = verify_ka(candidate if candidate else "n**3")
        elif item in ("VB1", "VB1a", "VB1b", "VB2", "B1", "B2"):
            res = verify_b(candidate, timeout_s=timeout_s)
        else:
            if "def count_primes" in candidate or "def count_primes" in prompt:
                code_to_run = candidate if "def count_primes" in candidate else prompt
                res = verify_b(code_to_run, timeout_s=timeout_s)
            else:
                try:
                    dt = float(candidate)
                    res = verify_a4(dt)
                except Exception:
                    res = verify_a1(candidate)
    except TimeoutError:
        return 124, json.dumps({"verdict": "timeout"}), "timeout"
    except Exception as exc:
        return 0, json.dumps({"verdict": "fail", "error": str(exc)}), ""

    return 0, json.dumps(res), ""
