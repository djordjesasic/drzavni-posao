#!/usr/bin/env bash
# Run a dp CLI command in a one-off worker container, e.g. `scripts/dp.sh catalog-fetch`.
set -euo pipefail
source "$(dirname "$0")/common.sh"

# Arguments are quoted so the remote shell passes them through literally.
args=""
if (($#)); then args="$(shell_quote "$@")"; fi
remote "$COMPOSE --profile worker run --rm -T worker python -m dp.cli $args"
