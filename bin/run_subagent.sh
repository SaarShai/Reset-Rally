#!/bin/bash
# Wrapper around ~/.agents/skills/agent-roster/run.sh for Reset Rally subagents.
# Primary: agy/claude-opus-5-5-high (Opus 5.5 on saar.shai@gmail.com Google AI Pro plan).
# Fallback when quota is reached: agy/claude-sonnet-5-5-high -> agy/gemini-3.8-flash-high -> gemini-4-argon:high.
set -u
[ $# -ge 3 ] || { echo "usage: run_subagent.sh <workdir> <outdir> <name> [preferred_agent]" >&2; exit 2; }
WD=$1; OUT=$2; N=$3; PREF=${4:-agy/claude-opus-5-5-high}
ROSTER="$HOME/.agents/skills/agent-roster/run.sh"

CANDIDATES=("$PREF" "agy/claude-sonnet-5-5-high" "agy/gemini-3.8-flash-high" "gemini-4-argon:high")

for AGENT in "${CANDIDATES[@]}"; do
  "$ROSTER" "$AGENT" "$WD" "$OUT" "$N"
  rc=$?
  if [ "$rc" -eq 0 ] && [ -s "$OUT/$N.md" ] && ! grep -qi "ANSWER FILE MISSING" "$OUT/$N.md"; then
    exit 0
  fi
  if grep -qiE "Individual quota reached|quota|rate limit|capacity" "$OUT/$N-log.txt" 2>/dev/null; then
    echo "run_subagent.sh: $AGENT hit quota/limit; trying next Gemini/Antigravity agent..." >&2
    continue
  fi
done
exit "${rc:-1}"
