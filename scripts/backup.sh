#!/usr/bin/env bash
# Copy a consistent snapshot of the SQLite database to the Mac (./backups/).
set -euo pipefail
source "$(dirname "$0")/common.sh"

stamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$REPO_DIR/backups"
remote "$COMPOSE --profile worker run --rm -T worker python -m dp.cli backup /data/backup.sqlite"
scp "$REMOTE_HOST:$REMOTE_DIR/data/backup.sqlite" "$REPO_DIR/backups/dp-$stamp.sqlite"
remote "rm -f data/backup.sqlite"
echo "backups/dp-$stamp.sqlite"
