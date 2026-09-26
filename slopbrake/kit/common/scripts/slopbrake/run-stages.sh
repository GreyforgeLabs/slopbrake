# Stage runner sourced by scripts/check (rule L1, https://github.com/GreyforgeLabs/slopbrake/blob/main/docs/RULES.md). Not executable on its own.
# scripts/check defines stage_<name> functions and STAGES, then calls run_stages "$@".
#
#   scripts/check                 all stages
#   scripts/check --fast          all but the slow ones (SLOW_STAGES): pre-commit, Stop hook
#   scripts/check tests lint      only the named stages
#
# Every stage runs even after a failure, so one run shows everything that is red.

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
  if [ ${#selected[@]} -eq 0 ]; then
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
    else results+=("FAIL  $name ($((SECONDS - start))s)"); failed=1; fi
  done
  echo "── summary ──"
  printf '%s\n' "${results[@]}"
  return $failed
}
