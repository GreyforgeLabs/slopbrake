#!/bin/bash
# Stop hook (rule H6): the gate must be green before the agent stops. Commits no full green
# run has covered (see unverified) need the full gate, which records the new green on success
# when the tree is clean; otherwise uncommitted code changes need the fast gate. The full
# gate samples at most SLOPBRAKE_STOP_MUTANTS (40) mutants, and all runs together share one
# SLOPBRAKE_STOP_TIMEOUT (540) second budget, inside the 600 s hook timeout; running out of
# it is a red attempt, never a hang.
# Gated: the project, plus the same directory in the worktree the agent is working in
# (payload cwd) when it belongs to the same repository. On red, exit 2 feeds the failure
# back so the agent keeps working. After 3 consecutive red stops in one session it lets
# the agent stop and report (loop limit, rule C2) instead of trapping it.

INPUT=$(cat)
PARSE='(.session_id // .sessionId // "unknown"), (.cwd // ""), (.reason // "")'  # grok sends camelCase keys
if command -v jq >/dev/null 2>&1; then
  PARSED=$(printf '%s' "$INPUT" | jq -r "$PARSE" 2>/dev/null)
else
  PARSED=$(printf '%s' "$INPUT" | python3 -c 'import json, sys
d = json.load(sys.stdin)
print(d.get("session_id") or d.get("sessionId") or "unknown"); print(d.get("cwd") or ""); print(d.get("reason") or "")' 2>/dev/null)
fi
{ read -r SESSION; read -r CWD; read -r REASON; } <<<"$PARSED"
case "$REASON" in channel_closed|shutdown) exit 0 ;; esac  # grok's observe-only Stop at session end
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

# unverified BRANCH: HEAD has commits that no full green run has covered. With a ratchet
# (refs/slopbrake/last-green/<branch, "/" as %2F>): commits after it, or any commit once it is no
# longer an ancestor (amend, rebase, reset). Without one: on the default branch itself (a local
# base: no remote has its commits) every commit; elsewhere, commits since the default branch.
# BRANCH is empty on a detached HEAD: never a ratchet, just the default-branch check.
unverified() {
  local head last base
  head=$(git rev-parse -q --verify HEAD) || return 1
  if [ -n "$1" ] && last=$(git rev-parse -q --verify "refs/slopbrake/last-green/${1//\//%2F}"); then
    git merge-base --is-ancestor "$last" HEAD || return 0
    [ "$last" != "$head" ]
    return
  fi
  for base in origin/HEAD main master origin/main origin/master; do
    git rev-parse -q --verify "$base^{commit}" >/dev/null || continue
    [ -n "$1" ] && [ "$(git rev-parse --symbolic-full-name "$base")" = "refs/heads/$1" ] && return 0
    [ "$(git merge-base "$base" HEAD 2>/dev/null)" != "$head" ]
    return
  done
  [ -n "$1" ]  # no base ref at all: this branch is the only history and nothing vouches for it
}

# gate DIR: runs what DIR needs; on red prints why and the log tail, returns 1.
# Unverified commits need the full gate even when the tree is also dirty (it then checks both
# but cannot record); dirty code alone needs the fast gate.
gate() {
  cd "$1" 2>/dev/null && [ -x scripts/check ] || return 0
  local log args=() why branch status dirty= last
  log="$(git rev-parse --absolute-git-dir)/slopbrake/stop-check.log"
  # Docs-only changes don't need the gate; neither does a nested worktree ("dir/": another checkout).
  if git -c core.quotePath=false status --porcelain --untracked-files=all | awk '{print $NF}' \
     | grep -vE '/$' | grep -qvE '\.(md|txt)$'; then
    dirty=" (and uncommitted code changes)"
  fi
  # The user-level gate may run this in a clean copy (a detached worktree) and name the real branch.
  branch=${SLOPBRAKE_STOP_BRANCH:-$(git symbolic-ref -q --short HEAD)}
  if [ -n "${SLOPBRAKE_STOP_BRANCH:-}" ] && last=$(git rev-parse -q --verify "refs/slopbrake/last-green/${branch//\//%2F}"); then
    export SLOPBRAKE_BASE=$last  # measure the copy from the real branch's ratchet
  fi
  if unverified "$branch"; then
    why="commits on ${branch:-a detached HEAD} that no full green run has covered$dirty, and the full scripts/check is red"
    export MUTATION_MAX="${SLOPBRAKE_STOP_MUTANTS:-40}"
  elif [ -n "$dirty" ]; then
    args=(--fast) why="uncommitted code changes and scripts/check --fast is red"
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
