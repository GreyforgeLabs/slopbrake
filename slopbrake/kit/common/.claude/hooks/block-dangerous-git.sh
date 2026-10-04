#!/bin/bash
# PreToolUse guard (rule H5), adapted from Matt Pocock's git-guardrails-claude-code.
# Blocks destructive git commands in agent sessions. Plain `git push` of a branch stays
# allowed; force pushes do not. Exit 2 = block, with the reason on stderr.

COMMAND=$(jq -r '.tool_input.command // empty')
[ -z "$COMMAND" ] && exit 0

# git's global options (-C dir, -c key=val, --no-pager, --git-dir=x ...) may sit before the subcommand.
# Shell token separators may contain repeated spaces or tabs.
GIT='git([[:blank:]]+-C[[:blank:]]+[^[:blank:]]+|[[:blank:]]+-c[[:blank:]]+[^[:blank:]]+|[[:blank:]]+--[a-z-]+(=[^[:blank:]]+)?)*'

DANGEROUS_PATTERNS=(
  "$GIT"'[[:blank:]]+push([[:blank:]]+[^;&|]*)?[[:blank:]]+(--force|--force-with-lease|-f)([[:blank:]]|$|=)'
  "$GIT"'[[:blank:]]+push([[:blank:]]+[^;&|]*)?[[:blank:]]+\+[^[:blank:]]+'
  'reset[[:blank:]]+--hard'
  "$GIT"'[[:blank:]]+clean([[:blank:]]+[^;&|]*)?[[:blank:]]+(-[a-zA-Z]*f|--force)'
  "$GIT"'[[:blank:]]+branch([[:blank:]]+[^;&|]*)?[[:blank:]]+(-D|--delete[[:blank:]]+--force|-d[[:blank:]]+--force)([[:blank:]]|$)'
  "$GIT"'[[:blank:]]+checkout([[:blank:]]+--)?[[:blank:]]+\.([[:blank:]]|$|;|&)'
  "$GIT"'[[:blank:]]+checkout[[:blank:]]+[^;&|]*[[:blank:]]+--[[:blank:]]+\.([[:blank:]]|$|;|&)'
  "$GIT"'[[:blank:]]+restore([[:blank:]]+--worktree|[[:blank:]]+-W|[[:blank:]]+--source(=|[[:blank:]]+)[^[:blank:]]+|[[:blank:]]+-s[[:blank:]]+[^[:blank:]]+)*[[:blank:]]+\.([[:blank:]]|$|;|&)'
)

for pattern in "${DANGEROUS_PATTERNS[@]}"; do
  if printf '%s' "$COMMAND" | grep -qE -- "$pattern"; then
    echo "BLOCKED: '$COMMAND' matches the destructive-git guard ('$pattern'). The operator has not granted this in agent sessions; ask them to run it." >&2
    exit 2
  fi
done
exit 0
