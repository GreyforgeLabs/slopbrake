#!/bin/bash
# PreToolUse guard (rule H5), descended from Matt Pocock's git-guardrails-claude-code.
# Blocks destructive git commands in agent sessions: force pushes and remote deletes, reset --hard,
# clean -f, branch -D, whole-tree checkout/restore, stash drop/clear, --no-verify and core.hooksPath
# overrides. Plain branch pushes stay allowed. The parsing lives in git_guard.py (stdlib Python, no jq),
# which reads the hook JSON on stdin. Exit 2 = block, with the reason on stderr.
# Without python3 the guard can't judge anything, so it fails closed.
if ! command -v python3 >/dev/null 2>&1; then
  echo "BLOCKED: the destructive-git guard (H5) needs python3, which is not on PATH; install python3 or ask the operator to run this command." >&2
  exit 2
fi
exec python3 "$(dirname "$0")/git_guard.py" --scope always
