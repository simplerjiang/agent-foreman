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
pip install -e ".[server]" --quiet   # server deployable only (no client/desktop deps)

# Ensure a usable (non-empty) FOREMAN_AUTH_TOKEN before restart. The startup guard fails closed on
# the public 0.0.0.0 bind without one (issue #1 P0); the deploy path runs THIS script (not
# bootstrap.sh), so seed it here too or a box whose .env lacks/blanks the token would stay down
# after the restart (codex acceptance finding). Idempotent: only acts when the value is missing/blank.
CUR_TOKEN=""
[ -f "$APP/.env" ] && CUR_TOKEN=$(grep -E '^FOREMAN_AUTH_TOKEN=' "$APP/.env" | tail -n1 | cut -d= -f2- | tr -d '[:space:]')
if [ -z "$CUR_TOKEN" ]; then
  echo "== seeding missing FOREMAN_AUTH_TOKEN =="
  NEW_TOKEN=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 43)
  [ -f "$APP/.env" ] && sed -i '/^FOREMAN_AUTH_TOKEN=/d' "$APP/.env"   # drop any blank entry first
  echo "FOREMAN_AUTH_TOKEN=$NEW_TOKEN" >> "$APP/.env"
  chmod 600 "$APP/.env"
fi

echo "== restart service =="
sudo systemctl restart foreman
sleep 1
sudo systemctl is-active foreman

echo "== deployed: $(git rev-parse --short HEAD) =="
