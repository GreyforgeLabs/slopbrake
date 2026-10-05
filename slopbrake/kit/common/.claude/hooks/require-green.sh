#!/bin/bash
# Stop hook (rule H6): the gate must be green before the agent stops. Uncommitted code
# changes need the fast gate; a clean tree with commits since this branch's last green run
# (refs/slopbrake/last-green/<branch>) needs the full gate, which records the new green on
# success. The full gate samples at most SLOPBRAKE_STOP_MUTANTS (40) mutants, and all runs
# together share one SLOPBRAKE_STOP_TIMEOUT (540) second budget, inside the 600 s hook
# timeout; running out of it is a red attempt, never a hang.
# Gated: the project, plus the same directory in the worktree the agent is working in
# (payload cwd) when it belongs to the same repository. On red, exit 2 feeds the failure
# back so the agent keeps working. After 3 consecutive red stops in one session it lets
# the agent stop and report (loop limit, rule C2) instead of trapping it.

INPUT=$(cat)
PARSE='(.session_id // "unknown"), (.cwd // "")'
if command -v jq >/dev/null 2>&1; then
  PARSED=$(printf '%s' "$INPUT" | jq -r "$PARSE" 2>/dev/null)
else
  PARSED=$(printf '%s' "$INPUT" | python3 -c 'import json, sys
d = json.load(sys.stdin)
print(d.get("session_id") or "unknown"); print(d.get("cwd") or "")' 2>/dev/null)
fi
{ read -r SESSION; read -r CWD; } <<<"$PARSED"
SESSION=$(printf '%s' "${SESSION:-unknown}" | tr -c 'A-Za-z0-9_.-' '_')

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
cd "$ROOT" 2>/dev/null && git rev-parse --git-dir >/dev/null 2>&1 || exit 0
ROOT=$PWD
REPOS=("$ROOT")
if [ -n "$CWD" ] && WT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null) \
   && [ "$WT" != "$(git rev-parse --show-toplevel)" ] \
   && [ "$(git -C "$WT" rev-parse --path-format=absolute --git-common-dir)" = "$(git rev-parse --path-format=absolute --git-common-dir)" ]; then
  REPOS+=("$WT/$(git rev-parse --show-prefix)")  # an install in a monorepo subdirectory sits there in the worktree too
fi

LIMIT=${SLOPBRAKE_STOP_TIMEOUT:-540}
DEADLINE=$((SECONDS + LIMIT))  # one budget for every repo: the hook itself is killed at 600 s
STATE_DIR="$(git rev-parse --absolute-git-dir)/slopbrake"
mkdir -p "$STATE_DIR"
COUNT_FILE="$STATE_DIR/stop-red-$SESSION"

# gate DIR: runs what DIR needs; on red prints why and the log tail, returns 1.
gate() {
  cd "$1" 2>/dev/null && [ -x scripts/check ] || return 0
  local log args=() why last branch status
  log="$(git rev-parse --absolute-git-dir)/slopbrake/stop-check.log"
  # Docs-only changes don't need the gate.
  if git -c core.quotePath=false status --porcelain --untracked-files=all | awk '{print $NF}' | grep -qvE '\.(md|txt)$'; then
    args=(--fast) why="uncommitted code changes and scripts/check --fast is red"
  elif branch=$(git symbolic-ref -q --short HEAD) && last=$(git rev-parse -q --verify "refs/slopbrake/last-green/$branch") \
       && [ "$last" != "$(git rev-parse HEAD)" ] && git merge-base --is-ancestor "$last" HEAD; then
    why="commits since the last green run on $branch (${last:0:12}) and the full scripts/check is red"
    export MUTATION_MAX="${SLOPBRAKE_STOP_MUTANTS:-40}"
  else
    return 0
  fi
  mkdir -p "$(dirname "$log")"
  local left=$((DEADLINE - SECONDS)) cap=() run="scripts/check${args[*]:+ ${args[*]}}"
  if command -v timeout >/dev/null; then
    if [ $left -le 0 ]; then
      echo "require-green: $1: $run timed out: the ${LIMIT}s Stop budget was used up before it could run"
      return 1
    fi
    cap=(timeout -k 5 "$left")
  fi
  "${cap[@]}" scripts/check "${args[@]}" >"$log" 2>&1 </dev/null
  status=$?
  [ $status -eq 0 ] && return 0
  [ $status -eq 124 ] && [ ${#cap[@]} -gt 0 ] && why="$run timed out (the Stop budget is ${LIMIT}s for all repos)"
  echo "require-green: $1: $why:"
  tail -n 60 "$log"
  return 1
}

REPORT=
for repo in "${REPOS[@]}"; do
  out=$(gate "$repo") || REPORT+="$out"$'\n'
done
if [ -z "$REPORT" ]; then
  rm -f "$COUNT_FILE"
  exit 0
fi

COUNT=$(( $(cat "$COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
if [ "$COUNT" -ge 3 ]; then
  rm -f "$COUNT_FILE"
  echo "require-green: the gate is still red after 3 attempts; stopping is allowed, but report the failure to the operator (logs: .git/slopbrake/stop-check.log)." >&2
  exit 0
fi
echo "$COUNT" > "$COUNT_FILE"
{
  echo "Fix it before stopping (attempt $COUNT/3):"
  printf '%s' "$REPORT"
} >&2
exit 2
