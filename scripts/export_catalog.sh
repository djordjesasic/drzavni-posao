#!/usr/bin/env bash
# Export the episodes table on the mini PC to CSV and copy it to exports/epizode.csv.
# Columns: broj, sezona, naslov, datum, trajanje, vrsta, youtube; ordered by episode number.
set -euo pipefail
source "$(dirname "$0")/common.sh"

remote_file="exports/epizode.csv" # relative to data/ on the mini PC (/data in the container)
local_file="$REPO_DIR/exports/epizode.csv"

remote "$COMPOSE --profile worker run --rm -T worker \
  python -m dp.cli export-episodes /data/$remote_file"
mkdir -p "$(dirname "$local_file")"
scp -q "$REMOTE_HOST:$REMOTE_DIR/data/$remote_file" "$local_file"
echo "$local_file"
