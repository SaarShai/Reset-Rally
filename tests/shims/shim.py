#!/usr/bin/env python3
"""Deterministic CLI shim for claude, codex, agy, opencode and brawn.

This shim replays predefined responses for participants during testing.
It supports in-process and subprocess execution modes.
"""

import json
import os
from pathlib import Path
import re
import sys

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "rally"


def respond(cli: str, persona: str, prompt: str, state_dir: str | Path) -> tuple[int, str, str]:
    """Compute deterministic shim response for given CLI, persona, and prompt."""
    answers_file = FIX / "answers" / f"{persona}.json"
    if not answers_file.exists():
        return 3, "", f"shim: answers file for persona '{persona}' does not exist"

    table = json.loads(answers_file.read_text(encoding="utf-8"))
    st_dir = Path(state_dir)
    st_dir.mkdir(parents=True, exist_ok=True)

    counter_path = st_dir / f"{persona}.count"
    n = int(counter_path.read_text(encoding="utf-8")) + 1 if counter_path.exists() else 1
    counter_path.write_text(str(n), encoding="utf-8")

    if n > table.get("limit_after", 10**9):
        return table.get("limit_rc", 1), "", table.get("limit_stderr", "rate limit exceeded")

    if cli == "brawn":
        fixtures_dir = str(FIX.parent.parent)
        if fixtures_dir not in sys.path:
            sys.path.insert(0, fixtures_dir)
        src_dir = str(FIX.parent.parent.parent / "src")
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        from fixtures.rally import verifiers

        return verifiers.run(prompt)

    m = re.search(r"^Item:\s*(\S+)", prompt, re.M)
    item = m[1] if m else prompt.strip()

    item_counter_path = st_dir / f"{persona}_{item}.count"
    attempt = (
        int(item_counter_path.read_text(encoding="utf-8")) + 1
        if item_counter_path.exists()
        else 1
    )
    item_counter_path.write_text(str(attempt), encoding="utf-8")

    answers = table.get("answers", {})
    ans = None
    if attempt > 1:
        ans = answers.get(f"{item}#{attempt}")
    if ans is None:
        ans = answers.get(item)
    if ans is None:
        return 3, "", f"shim: {persona} has no answer for {item}"

    raw_stdout = ans.get("stdout", "")
    out = raw_stdout if isinstance(raw_stdout, str) else json.dumps(raw_stdout)
    return ans.get("rc", 0), out, ans.get("stderr", "")


if __name__ == "__main__":
    cli = Path(sys.argv[0]).name
    persona = os.environ.get("SHIM_PERSONA", "alice")
    state_dir = os.environ.get("SHIM_STATE", "/tmp")

    st_path = Path(state_dir)
    st_path.mkdir(parents=True, exist_ok=True)
    argv_path = st_path / f"{persona}.argv"
    with open(argv_path, "a", encoding="utf-8") as f:
        f.write(" ".join(sys.argv) + "\n")

    prompt = sys.stdin.read() if sys.argv[-1] == "-" else sys.argv[-1]
    rc, out, err = respond(cli, persona, prompt, state_dir)
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(rc)
