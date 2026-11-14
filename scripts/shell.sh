#!/usr/bin/env bash
# Open a shell in a one-off container of the given service (default: worker).
set -euo pipefail
source "$(dirname "$0")/common.sh"

service="${1:-worker}"
case "$service" in
  api | worker) ;;
  *) echo "usage: $0 [api|worker]" >&2; exit 2 ;;
esac
remote_tty "docker compose --profile worker run --rm $service bash"
