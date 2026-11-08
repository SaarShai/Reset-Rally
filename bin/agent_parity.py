#!/usr/bin/env python3
"""Keep this project's agent setup usable by both Claude and Codex (RULES.md#shared-agent-setup).

Default (the SessionStart hook in both apps): make the safe fixes (link a shared skill for Claude;
move a Claude-only project skill into .agents/skills) and print one line per fix or remaining gap.
Prints nothing when both apps match. --check: change nothing, exit 1 on any gap (bin/doctor.py).
"""
import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED, CLAUDE = ROOT / ".agents/skills", ROOT / ".claude/skills"


def skills(fix):
    out = []
    if not (SHARED.is_dir() and CLAUDE.is_dir()):  # a sparse worktree may leave one out
        return out
    for d in sorted(CLAUDE.iterdir()):
        if d.is_symlink() and not d.exists():
            out.append(f"skill {d.name}: .claude/skills link points nowhere")
        elif d.is_dir() and not d.is_symlink() and (d / "SKILL.md").is_file():
            if (SHARED / d.name).exists():
                out.append(f"skill {d.name}: two copies, in .claude/skills and .agents/skills")
            elif fix:
                shutil.move(d, SHARED / d.name)
                d.symlink_to(f"../../.agents/skills/{d.name}")
                out.append(f"fixed: moved Claude-only skill {d.name} to .agents/skills and linked it")
            else:
                out.append(f"skill {d.name}: Claude only; move it to .agents/skills")
    for d in sorted(p for p in SHARED.iterdir() if (p / "SKILL.md").is_file()):
        link = CLAUDE / d.name
        if not link.exists() and not link.is_symlink():
            if fix:
                try:
                    link.symlink_to(f"../../.agents/skills/{d.name}")
                except FileExistsError:  # another session started at the same moment
                    continue
                out.append(f"fixed: linked .claude/skills/{d.name} for Claude")
            else:
                out.append(f"skill {d.name}: Codex only; link .claude/skills/{d.name} -> ../../.agents/skills/{d.name}")
        elif link.resolve() != d.resolve():
            out.append(f"skill {d.name}: .claude/skills/{d.name} is not the .agents/skills copy")
        name = re.search(r"^name:\s*['\"]?([\w.-]+)", (d / "SKILL.md").read_text(errors="replace"), re.M)
        if not name or name[1] != d.name:
            out.append(f"skill {d.name}: SKILL.md name differs from its folder")
    return out


def load(path, parse):
    try:
        return parse(path.read_text())
    except FileNotFoundError:
        return {}


def hooks(path):
    return {f"{event} {h.get('command')}" for event, groups in load(path, json.loads).get("hooks", {}).items()
            for group in groups for h in group.get("hooks", [])}


def pair(kind, claude, codex):
    return ([f"{kind} {x}: Claude only" for x in sorted(claude - codex)]
            + [f"{kind} {x}: Codex only" for x in sorted(codex - claude)])


def problems(fix=False):
    out = skills(fix)
    if not re.search(r"^@AGENTS\.md\s*$", load(ROOT / "CLAUDE.md", str) or "", re.M):
        out.append("CLAUDE.md: the @AGENTS.md import line is missing")
    if load(ROOT / ".claude/settings.json", json.loads).get("autoMemoryEnabled") is not False:
        out.append("Claude auto-memory is on; set autoMemoryEnabled: false in .claude/settings.json")
    out += pair("hook", hooks(ROOT / ".claude/settings.json"), hooks(ROOT / ".codex/hooks.json"))
    out += pair("MCP server", set(load(ROOT / ".mcp.json", json.loads).get("mcpServers", {})),
                set(load(ROOT / ".codex/config.toml", tomllib.loads).get("mcp_servers", {})))
    out += pair("subagent", {p.stem for p in (ROOT / ".claude/agents").glob("*.md")},
                {p.stem for p in (ROOT / ".codex/agents").glob("*.toml")})
    return out


if __name__ == "__main__":
    check = "--check" in sys.argv
    lines = problems(fix=not check)
    if lines:
        print("\n".join(lines) + "\nClaude/Codex shared setup: RULES.md#shared-agent-setup")
    sys.exit(1 if check and lines else 0)
