#!/bin/bash
# PreToolUse guard (rule H5), descended from Matt Pocock's git-guardrails-claude-code.
# Blocks destructive git commands in agent sessions: force pushes and remote deletes, reset --hard,
# clean -f, branch -D, whole-tree checkout/restore/rm, stash drop/clear, tag -d, worktree remove --force,
# filter-branch, --no-verify and core.hooksPath overrides, and branch -f/checkout -B/switch -C/update-ref/forced
# fetches that would orphan commits no other ref reaches. Plain branch pushes stay allowed. The parsing lives in git_guard.py (stdlib Python, no jq),
# which reads the hook JSON on stdin. Exit 2 = block, with the reason on stderr.
# Without python3 the guard can't judge anything, so it fails closed.
if ! command -v python3 >/dev/null 2>&1; then
  echo "BLOCKED: the destructive-git guard (H5) needs python3, which is not on PATH; install python3 or ask the operator to run this command." >&2
  exit 2
fi
# git_guard.py is imported, not run as a script, so Python reuses its compiled bytecode instead of compiling
# 900 lines on every Bash call. Its bytecode goes under the user's cache directory, never into the repo; the
# standard library keeps its own. -I keeps the session's cwd and PYTHON* variables out of the import path.
exec python3 -I -c 'import __future__, json, re, shlex, sys
sys.pycache_prefix = sys.argv.pop(1)
sys.path.insert(0, sys.argv.pop(1))
import git_guard
sys.exit(git_guard.main(sys.argv[1:]))' "${XDG_CACHE_HOME:-$HOME/.cache}/slopbrake/pycache" "$(dirname "$0")" --scope always
