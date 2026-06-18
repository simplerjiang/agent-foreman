# Deployment (server-side component)

The server hosts the **team-mode** component (today: the PWA + `/health`; later: the relay hub +
admin console). The actual PM Core runs on each person's local machine — see
[DESIGN.zh-CN.md §8](../docs/DESIGN.zh-CN.md).

Auto-deploy: push to `main` → GitHub Actions SSHes in as the unprivileged `foreman` user and runs
[`remote_deploy.sh`](remote_deploy.sh) (git reset to `origin/main` → `pip install -e .` → restart).

## Isolation guarantees
- Runs as a dedicated **unprivileged** `foreman` user, in `/opt/foreman/app`, in a venv.
- Listens on **:8787** only. **Does not touch port 80 / the LLM gateway / any other service.**
- `foreman` may `sudo systemctl` **only** the `foreman` service (narrow sudoers rule).

## One-time server setup (done via `deploy/bootstrap.sh`, run as root)
1. Create the `foreman` user; install the CI deploy **public** key into its `authorized_keys`.
2. Clone this repo to `/opt/foreman/app`; create a venv; `pip install -e .`.
3. Write `/opt/foreman/app/config.yaml` (server-side LLM uses `http://127.0.0.1/v1` — the gateway is
   on this same box, so it's an internal/loopback call) and `.env` (auth token).
4. Install `foreman.service`; `ufw allow 8787/tcp`; enable + start.

## GitHub Actions secrets (set these in the repo — never committed)
| Secret | Value |
|--------|-------|
| `DEPLOY_HOST` | the server IP |
| `DEPLOY_USER` | `foreman` |
| `DEPLOY_SSH_KEY` | the **private** half of the CI deploy keypair (public half is on the server) |

Set them at: **GitHub repo → Settings → Secrets and variables → Actions → New repository secret.**

## Secrets that never leave their home
- LLM API keys: per-account, set in the PWA, stored **encrypted** server-side (or in a local `.env`).
- VAPID private key / server master key: server `.env`.
- Nothing secret is committed; `ServerInfo.txt` / `key.txt` / `*.key` / `*.pem` are git-ignored.
