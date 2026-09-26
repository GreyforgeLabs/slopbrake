#!/bin/bash
# Stop hook (rule H6): with uncommitted code changes, the fast gate must be green before
# the agent stops. On red, exit 2 feeds the failure back so the agent keeps working.
# After 3 consecutive red stops in one session it lets the agent stop and report
# (loop limit, rule C2) instead of trapping it.

INPUT=$(cat)
SESSION=$(printf '%s' "$INPUT" | jq -r '.session_id // "unknown"')
ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
cd "$ROOT" 2>/dev/null || exit 0
[ -x scripts/check ] || exit 0

# Docs-only changes don't need the gate.
CHANGED=$(git status --porcelain --untracked-files=all | awk '{print $NF}' | grep -vE '\.(md|txt)$')
[ -z "$CHANGED" ] && exit 0

STATE_DIR="$(git rev-parse --git-dir)/slopbrake"
mkdir -p "$STATE_DIR"
COUNT_FILE="$STATE_DIR/stop-red-$SESSION"
LOG="$STATE_DIR/stop-check.log"

if scripts/check --fast >"$LOG" 2>&1; then
  rm -f "$COUNT_FILE"
  exit 0
fi

COUNT=$(( $(cat "$COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
if [ "$COUNT" -ge 3 ]; then
  rm -f "$COUNT_FILE"
  echo "require-green: scripts/check --fast is still red after 3 attempts; stopping is allowed, but report the failure to the operator (log: $LOG)." >&2
  exit 0
fi
echo "$COUNT" > "$COUNT_FILE"
{
  echo "require-green: uncommitted code changes and scripts/check --fast is red (attempt $COUNT/3). Fix it before stopping:"
  tail -n 60 "$LOG"
} >&2
exit 2
