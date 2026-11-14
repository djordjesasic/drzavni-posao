#!/usr/bin/env bash
# Resolve uv.lock on the mini PC (no uv on the Mac) and copy it back into the repo.
# Works in a throwaway remote temp dir: the container pip-installs from the internet,
# so it must not see the deployed tree (data/, .env). pyproject.toml has no readme or
# other local file that `uv lock` needs (hatchling is only used for builds).
set -euo pipefail
source "$(dirname "$0")/common.sh"

tmp="$(ssh "$REMOTE_HOST" 'mktemp -d')"
if [[ -z "$tmp" || "$tmp" != /* ]]; then
  echo "could not create a remote temp dir" >&2
  exit 1
fi
# Expanded now on purpose: the path is fixed by this point.
# shellcheck disable=SC2064
trap "ssh '$REMOTE_HOST' \"rm -rf '$tmp'\" || echo \"warning: remove $REMOTE_HOST:$tmp manually\" >&2" EXIT

files=("$REPO_DIR/pyproject.toml")
if [[ -f "$REPO_DIR/uv.lock" ]]; then files+=("$REPO_DIR/uv.lock"); fi
scp -q "${files[@]}" "$REMOTE_HOST:$tmp/"

# \$(id -u) is escaped so it expands on the mini PC, not on the Mac. The container runs
# as root, so it chowns the whole /src to the remote user to let the trap delete it.
# shellcheck disable=SC2029
ssh "$REMOTE_HOST" "docker run --rm -e HOST_UID=\$(id -u) -e HOST_GID=\$(id -g) \
  -v '$tmp':/src -w /src python:3.12-slim \
  sh -c 'pip install -q uv==0.13.7 && uv --version && uv lock && chown -R \$HOST_UID:\$HOST_GID /src'"
scp -q "$REMOTE_HOST:$tmp/uv.lock" "$REPO_DIR/uv.lock"
