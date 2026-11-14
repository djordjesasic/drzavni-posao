#!/usr/bin/env bash
# Sync code to the mini PC and (re)start the api service there.
# Images are built on the mini PC (x86), never on the Mac (ARM).
set -euo pipefail
source "$(dirname "$0")/common.sh"

check_remote_target "$REMOTE_DIR"
rsync -az --delete "${RSYNC_EXCLUDES[@]}" "$REPO_DIR/" "$REMOTE_HOST:$REMOTE_DIR/"

# Runs on the mini PC from $REMOTE_DIR. The worker is stopped only around the migration:
# the build comes first because it can take long and does not touch the running
# containers; up -d then recreates the worker on the new image. Detection uses the
# compose labels, so one-off `run --rm` containers (oneoff=True) and stopped containers
# are never mistaken for the running service. stdin is /dev/null for docker because it
# would otherwise swallow the rest of this script, which is fed on stdin.
# A failure after the stop leaves the worker stopped on purpose (no restart on a
# failed migration) and says so on stderr.
ssh "$REMOTE_HOST" "cd '$REMOTE_DIR' && bash -s" <<'REMOTE'
set -eu
worker_was_running=""
done_ok=""
cleanup() {
  if [ -n "$worker_was_running" ] && [ -z "$done_ok" ]; then
    echo "deploy failed: the worker was stopped and is LEFT STOPPED; fix the problem, then run 'scripts/worker.sh start'" >&2
  fi
}
trap cleanup EXIT

# Apply migrations before the api starts; it answers 503 on an old schema.
docker compose build </dev/null
if [ -n "$(docker ps -q \
  --filter label=com.docker.compose.project=drzavni-posao \
  --filter label=com.docker.compose.service=worker \
  --filter label=com.docker.compose.oneoff=False \
  --filter status=running </dev/null)" ]; then
  worker_was_running=1
  docker compose --profile worker stop worker </dev/null
fi
docker compose --profile worker run --rm -T worker python -m dp.cli init </dev/null
mkdir -p data/models/tei
docker compose up -d api embed </dev/null
if [ -n "$worker_was_running" ]; then
  docker compose --profile worker up -d worker </dev/null
fi
done_ok=1
docker compose --profile worker ps </dev/null
REMOTE
