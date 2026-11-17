# Shared settings for scripts run on the Mac. Source, do not execute.
REMOTE_HOST="${DP_REMOTE_HOST:-llmserver}"
REMOTE_DIR="${DP_REMOTE_DIR:-/opt/drzavni-posao}"
if [[ -z "${BASH_SOURCE[0]:-}" ]]; then
  echo "common.sh must be sourced from a bash script, not from an interactive shell" >&2
  return 1 2>/dev/null || exit 1
fi
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Guard: rsync --delete must only ever sync this project's root.
if [[ ! -f "$REPO_DIR/compose.yaml" || ! -d "$REPO_DIR/dp" || ! -f "$REPO_DIR/PLAN.md" ]]; then
  echo "refusing to run: $REPO_DIR is not the drzavni-posao repo root" >&2
  exit 1
fi

# One exclude list for every rsync to the mini PC. Excluded paths are also protected
# from --delete, so the remote data/ and .env* stay. /data/ and /backups/ are anchored to
# the transfer root, so a nested data/ (e.g. tests/data/) is still synced. Order matters: the first matching
# rule wins, so .env.example is included before .env* is excluded.
RSYNC_EXCLUDES=(
  --include '.env.example' --exclude '.env*'
  --exclude '.git/' --exclude '/data/' --exclude '/backups/' --exclude '/exports/' --exclude '.venv/'
  --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.ruff_cache/'
  --exclude '.DS_Store' --exclude '*.sqlite*'
)

# Refuse to rsync --delete into a remote directory that is not ours: it must not exist
# yet, contain compose.yaml, or hold nothing but data/ and .env* files.
# DIR is absolute or relative to the remote home directory.
check_remote_target() {
  local dir="$1"
  local rc=0
  # shellcheck disable=SC2029
  ssh "$REMOTE_HOST" "bash -s -- $(printf '%q' "$dir")" <<'REMOTE' || rc=$?
set -eu
[ -d "$1" ] || exit 0
cd "$1"
[ -f compose.yaml ] && exit 0
for f in * .[!.]* ..?*; do
  [ -e "$f" ] || continue
  case "$f" in
    data | .env*) ;;
    *) exit 1 ;; # foreign content; never 255, which ssh reserves for its own errors
  esac
done
REMOTE
  if ((rc == 255)); then
    echo "cannot reach $REMOTE_HOST (ssh failed)" >&2
    exit 1
  elif ((rc != 0)); then
    echo "refusing to sync: $REMOTE_HOST:$dir has no compose.yaml and is not empty" >&2
    exit 1
  fi
}

# Quote arguments for the remote shell: each in single quotes. Not printf %q: the Mac's
# bash 3.2 breaks multibyte characters with it ("ovčija" arrived as "ov\xc4?ija").
shell_quote() {
  local a out="" sq="'" esc="'\\''"  # ' -> '\'' (close, escaped quote, reopen)
  for a in "$@"; do
    out+="'${a//$sq/$esc}' "
  done
  printf '%s' "$out"
}

remote() {
  # shellcheck disable=SC2029
  ssh "$REMOTE_HOST" "cd '$REMOTE_DIR' && $*"
}

remote_tty() {
  # shellcheck disable=SC2029
  ssh -t "$REMOTE_HOST" "cd '$REMOTE_DIR' && $*"
}
