#!/usr/bin/env bash
# Run pytest + ruff on the mini PC in an isolated copy of the working tree.
# Does not touch /opt/drzavni-posao or the running api; each NAME gets its own
# directory (~/dp-test/NAME) and image tag (drzavni-posao-test:NAME).
# usage: bash scripts/test.sh NAME [pytest args...]
set -euo pipefail
source "$(dirname "$0")/common.sh"

NAME="${1:-default}"
shift || true
if [[ ! "$NAME" =~ ^[a-z0-9-]+$ ]]; then
  echo "NAME must match [a-z0-9-]+" >&2
  exit 2
fi
DEST="dp-test/$NAME" # relative to the remote home directory

check_remote_target "$DEST"
ssh "$REMOTE_HOST" "mkdir -p ~/$DEST"
rsync -az --delete "${RSYNC_EXCLUDES[@]}" "$REPO_DIR/" "$REMOTE_HOST:$DEST/"

# Pytest args are %q-quoted for the remote shell and reach `sh -c` as positional
# parameters ("$@"), so they are never re-parsed inside the container.
args=""
if (($#)); then args="$(printf '%q ' "$@")"; fi
# shellcheck disable=SC2029
ssh "$REMOTE_HOST" "cd ~/$DEST && docker build -q -f docker/Dockerfile -t drzavni-posao-test:$NAME . >/dev/null \
  && docker run --rm -e RUFF_CACHE_DIR=/tmp/ruff -e DP_CONFIG=/app/config.toml drzavni-posao-test:$NAME \
     sh -c 'pytest -q \"\$@\" && ruff check dp tests && ruff format --check dp tests' sh $args"
