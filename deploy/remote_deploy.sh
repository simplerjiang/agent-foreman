#!/usr/bin/env bash
# Runs ON the server as the `foreman` user (invoked by CI over SSH, or by hand).
# Pulls latest main, reinstalls deps, restarts the service. Local-only files
# (.env, config.yaml, foreman.db) are git-ignored and survive the reset.
set -euo pipefail

APP=/opt/foreman/app
cd "$APP"

echo "== fetch + reset to origin/main =="
git fetch --quiet origin
git reset --hard origin/main

echo "== install deps =="
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -e . --quiet

echo "== restart service =="
sudo systemctl restart foreman
sleep 1
sudo systemctl is-active foreman

echo "== deployed: $(git rev-parse --short HEAD) =="
