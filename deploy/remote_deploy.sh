#!/usr/bin/env bash
# Runs ON the server as the `foreman` user (invoked by CI over SSH, or by hand).
# Pulls the deployed commit, reinstalls deps, restarts the service. Local-only files
# (.env, config.yaml, foreman.db) are git-ignored and survive the reset.
set -euo pipefail

APP=/opt/foreman/app
cd "$APP"

# Deploy the EXACT commit the CI test gate passed (issue #1 / codex finding): the workflow passes
# DEPLOY_SHA so a newer push that landed after the gate can't be deployed untested. Falls back to
# origin/main for a manual run.
DEPLOY_SHA="${DEPLOY_SHA:-origin/main}"

echo "== fetch + reset to ${DEPLOY_SHA} =="
git fetch --quiet origin
git reset --hard "${DEPLOY_SHA}"

echo "== install deps =="
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -e ".[server]" --quiet   # server deployable only (no client/desktop deps)

# Ensure a usable (non-empty) FOREMAN_AUTH_TOKEN before restart. The startup guard fails closed on
# the public 0.0.0.0 bind without one (issue #1 P0); this is the script the deploy path runs, so
# seed it here too or a box whose .env lacks/blanks the token would stay down after the restart
# (codex finding). The `|| true` keeps the no-match grep from aborting under `set -euo pipefail`.
CUR_TOKEN=$(grep -E '^FOREMAN_AUTH_TOKEN=' "$APP/.env" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '[:space:]' | sed -e 's/^["'\'']//' -e 's/["'\'']$//' || true)
if [ -z "${CUR_TOKEN:-}" ]; then
  echo "== seeding missing FOREMAN_AUTH_TOKEN =="
  NEW_TOKEN=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 43)
  [ -f "$APP/.env" ] && sed -i '/^FOREMAN_AUTH_TOKEN=/d' "$APP/.env"   # drop any blank entry first
  # ensure a trailing newline so we don't concatenate onto an existing secret line (codex finding)
  [ -s "$APP/.env" ] && [ -n "$(tail -c1 "$APP/.env")" ] && printf '\n' >> "$APP/.env"
  echo "FOREMAN_AUTH_TOKEN=$NEW_TOKEN" >> "$APP/.env"
  chmod 600 "$APP/.env"
fi

echo "== restart service =="
sudo systemctl restart foreman
sleep 1
sudo systemctl is-active foreman

echo "== deployed: $(git rev-parse --short HEAD) =="
