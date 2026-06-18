#!/usr/bin/env bash
# One-time server bootstrap for Foreman's server-side component. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/simplerjiang/agent-foreman/main/deploy/bootstrap.sh | bash -s -- "<CI_DEPLOY_PUBLIC_KEY>"
# Idempotent. Isolated: dedicated `foreman` user, /opt/foreman, port 8787.
# Does NOT touch port 80 / the LLM gateway / any other service.
set -euo pipefail

PUBKEY="${1:-}"
REPO="https://github.com/simplerjiang/agent-foreman.git"
APP=/opt/foreman/app
PORT=8787

[ -n "$PUBKEY" ] || { echo "usage: bootstrap.sh '<ci deploy public key>'"; exit 1; }
[ "$(id -u)" = "0" ] || { echo "run as root"; exit 1; }

echo "== 1. unprivileged foreman user =="
id foreman >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/foreman --shell /bin/bash foreman

echo "== 2. install CI deploy public key =="
install -d -m 700 -o foreman -g foreman /opt/foreman/.ssh
touch /opt/foreman/.ssh/authorized_keys
grep -qF "$PUBKEY" /opt/foreman/.ssh/authorized_keys || echo "$PUBKEY" >> /opt/foreman/.ssh/authorized_keys
chmod 600 /opt/foreman/.ssh/authorized_keys
chown -R foreman:foreman /opt/foreman/.ssh

echo "== 3. clone + venv + deps (as foreman) =="
sudo -u foreman bash -lc "
  set -euo pipefail
  cd /opt/foreman
  [ -d app/.git ] || git clone '$REPO' app
  cd app && git fetch origin && git reset --hard origin/main
  [ -d .venv ] || python3 -m venv .venv
  . .venv/bin/activate
  pip install --upgrade pip wheel >/dev/null
  pip install -e . >/tmp/foreman_pip.log 2>&1 || { tail -30 /tmp/foreman_pip.log; exit 1; }
"

echo "== 4. config.yaml + .env (server-side; LLM via loopback) =="
if [ ! -f "$APP/config.yaml" ]; then
  cat > "$APP/config.yaml" <<YAML
server:
  host: 0.0.0.0
  port: $PORT
  public_base_url: ""
llm:
  provider: openai
  base_url: http://127.0.0.1/v1   # LLM gateway runs on this box -> loopback (see DESIGN §8.3)
  model: gpt-4o                   # adjust to a model your gateway serves
store:
  db_path: /opt/foreman/app/foreman.db
push:
  enabled: false
YAML
  chown foreman:foreman "$APP/config.yaml"
fi
if [ ! -f "$APP/.env" ]; then
  TOKEN=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 43)
  echo "FOREMAN_AUTH_TOKEN=$TOKEN" > "$APP/.env"
  chown foreman:foreman "$APP/.env"
  chmod 600 "$APP/.env"
fi

echo "== 5. sudoers: foreman may manage ONLY foreman.service =="
cat > /etc/sudoers.d/foreman <<'EOF'
foreman ALL=(root) NOPASSWD: /usr/bin/systemctl restart foreman, /usr/bin/systemctl start foreman, /usr/bin/systemctl stop foreman, /usr/bin/systemctl status foreman, /usr/bin/systemctl is-active foreman
EOF
chmod 440 /etc/sudoers.d/foreman
visudo -cf /etc/sudoers.d/foreman

echo "== 6. systemd service =="
install -m 644 "$APP/deploy/foreman.service" /etc/systemd/system/foreman.service
systemctl daemon-reload
systemctl enable foreman >/dev/null 2>&1 || true
systemctl restart foreman

echo "== 7. open ufw 8787 (phone access) =="
command -v ufw >/dev/null && ufw allow ${PORT}/tcp >/dev/null 2>&1 || true

sleep 1
echo "== verify =="
systemctl is-active foreman || true
curl -fsS "http://127.0.0.1:$PORT/health" || echo "(health not ready yet)"
echo
echo "BOOTSTRAP_DONE"
