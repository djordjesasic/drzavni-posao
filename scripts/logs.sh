#!/usr/bin/env bash
# Follow logs of one service (api, worker) or all services.
set -euo pipefail
source "$(dirname "$0")/common.sh"

service="${1:-}"
case "$service" in
  "" | api | worker) ;;
  *) echo "usage: $0 [api|worker]" >&2; exit 2 ;;
esac
remote_tty "$COMPOSE --profile worker logs -f --tail=200 $service"
