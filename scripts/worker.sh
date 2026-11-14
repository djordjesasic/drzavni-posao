#!/usr/bin/env bash
# Start, stop or show the status of the processing worker.
set -euo pipefail
source "$(dirname "$0")/common.sh"

case "${1:-status}" in
  start)  remote "docker compose --profile worker up -d worker" ;;
  stop)   remote "docker compose --profile worker stop worker" ;;
  status) remote "docker compose --profile worker ps worker" ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
