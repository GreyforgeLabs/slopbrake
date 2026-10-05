# Stage runner sourced by scripts/check (rule L1, https://github.com/GreyforgeLabs/slopbrake/blob/main/docs/RULES.md). Not executable on its own.
# scripts/check defines stage_<name> functions and STAGES, then calls run_stages "$@".
#
#   scripts/check                 all stages
#   scripts/check --fast          all but the slow ones (SLOW_STAGES): pre-commit, Stop hook
#   scripts/check tests lint      only the named stages
#
# Every stage runs even after a failure, so one run shows everything that is red.
# A stage with nothing to check prints "<stage>: skipped: <reason>" and returns 78 (a skip,
# not a failure). A full run that is all green on a clean tree records HEAD as
# refs/slopbrake/last-green/<branch, "/" as %2F>, the base later checks use on the default
# branch (never on a detached HEAD; SLOPBRAKE_NO_RECORD=1 opts out).

run_stages() {
  # Git exports these to hooks (`commit -a` points GIT_INDEX_FILE at a temporary index).
  # A test that runs git in another directory would inherit them and write into this
  # repo's commit. The gate runs from the repo root, so it doesn't need them.
  unset GIT_INDEX_FILE GIT_DIR GIT_WORK_TREE GIT_PREFIX GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  local fast=0 selected=() name
  for arg in "$@"; do
    case "$arg" in
      --fast) fast=1 ;;
      -h|--help) echo "usage: scripts/check [--fast] [stage ...]"; echo "stages: ${STAGES[*]}"; return 0 ;;
      *) selected+=("$arg") ;;
    esac
  done
  local full=0
  if [ ${#selected[@]} -eq 0 ]; then
    [ "$fast" -eq 0 ] && full=1
    for name in "${STAGES[@]}"; do
      if [ "$fast" -eq 1 ] && [[ " ${SLOW_STAGES[*]:-} " == *" $name "* ]]; then continue; fi
      selected+=("$name")
    done
  fi

  local results=() failed=0 start status
  for name in "${selected[@]}"; do
    if ! declare -F "stage_${name//-/_}" >/dev/null; then
      echo "scripts/check: unknown stage '$name' (stages: ${STAGES[*]})" >&2
      return 2
    fi
    echo "── $name ──"
    start=$SECONDS
    "stage_${name//-/_}"
    status=$?
    if [ $status -eq 0 ]; then results+=("pass  $name ($((SECONDS - start))s)")
    elif [ $status -eq 78 ]; then results+=("skip  $name ($((SECONDS - start))s)")
    else results+=("FAIL  $name ($((SECONDS - start))s)"); failed=1; fi
  done
  echo "── summary ──"
  printf '%s\n' "${results[@]}"
  [ $failed -eq 0 ] && [ $full -eq 1 ] && _record_last_green
  return $failed
}

_record_last_green() {
  local ref branch head
  [ "${SLOPBRAKE_NO_RECORD:-}" = 1 ] && return
  branch=$(git symbolic-ref -q --short HEAD) || return  # per branch: green elsewhere vouches for nothing here
  ref=refs/slopbrake/last-green/${branch//\//%2F}  # %2F: feature and feature/x never collide
  head=$(git rev-parse -q --verify HEAD) || return
  [ -z "$(git status --porcelain)" ] || return
  # A base that skips commits after the last green run did not check them: don't vouch for them.
  # (The empty tree skips nothing: every line was new.)
  if [ -n "${SLOPBRAKE_BASE:-}" ] && git rev-parse -q --verify "$ref" >/dev/null \
     && [ "$SLOPBRAKE_BASE" != "$(git hash-object -t tree /dev/null)" ] \
     && ! git merge-base --is-ancestor "$SLOPBRAKE_BASE" "$ref" 2>/dev/null; then return; fi
  if git update-ref "$ref" "$head"; then echo "last-green: recorded ${head:0:12}"
  else echo "last-green: WARNING: could not record $ref; the next Stop runs the full gate again" >&2; fi
}
