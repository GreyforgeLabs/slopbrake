#!/bin/bash
# PreToolUse guard (rule H5), adapted from Matt Pocock's git-guardrails-claude-code.
# Blocks destructive git commands in agent sessions. Plain `git push` of a branch stays
# allowed; force pushes do not. Exit 2 = block, with the reason on stderr.

COMMAND=$(jq -r '.tool_input.command // empty')
[ -z "$COMMAND" ] && exit 0

DANGEROUS_PATTERNS=(
  'git( -C [^ ]+)* push( [^;&|]*)? (--force|--force-with-lease|-f)( |$|=)'
  'git( -C [^ ]+)* push( [^;&|]*)? \+[^ ]+'
  'reset --hard'
  'git( -C [^ ]+)* clean( [^;&|]*)? -[a-zA-Z]*f'
  'git( -C [^ ]+)* branch( [^;&|]*)? (-D|--delete --force|-d --force)( |$)'
  'git( -C [^ ]+)* checkout( --)? \.( |$|;|&)'
  'git( -C [^ ]+)* restore( --worktree| -W| --source[= ][^ ]+| -s [^ ]+)* \.( |$|;|&)'
)

for pattern in "${DANGEROUS_PATTERNS[@]}"; do
  if printf '%s' "$COMMAND" | grep -qE -- "$pattern"; then
    echo "BLOCKED: '$COMMAND' matches the destructive-git guard ('$pattern'). The operator has not granted this in agent sessions; ask them to run it." >&2
    exit 2
  fi
done
exit 0
