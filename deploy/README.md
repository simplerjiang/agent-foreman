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
- LLM API keys: each user's key lives in **their own local process's `.env`** — never on the server. The
  server holds just **one site-wide LLM key** (its `.env`) for its own server-side LLM calls (on this box,
  the loopback gateway).
- VAPID private key / server master key: server `.env`.
- Nothing secret is committed; `ServerInfo.txt` / `key.txt` / `*.key` / `*.pem` are git-ignored.

---

## Live deployment facts (as of 2026-06-18)

| Thing | Value |
|-------|-------|
| Server | `165.232.161.99` (DigitalOcean Singapore, Ubuntu 24.04, 2 GB) |
| Foreman service | systemd `foreman.service`, user `foreman`, dir `/opt/foreman/app`, port **:8787** |
| Public URL | **https://foreman.kongsites.com** via a Cloudflare named tunnel (`cloudflared` service) |
| Domain | `kongsites.com` on Cloudflare |
| ⚠️ Co-tenant | The LLM gateway `cli-proxy-api` runs on **:80** (`cliproxy.service`) on the **same box**. Foreman never touches it. |

The public URL is served by Cloudflare's tunnel → `127.0.0.1:8787`, so **no inbound port besides the
tunnel needs to be open** for phone access. (8787 is also `ufw allow`ed for direct/LAN testing.)

## How to deploy (push) — and how to stay clear of the gateway

1. **Push to `main`.** That's the whole trigger — GitHub Actions does the rest (SSH as `foreman` →
   `remote_deploy.sh` → `git reset --hard origin/main` → `pip install -e .` → `sudo systemctl restart foreman`).
2. **Watch the run:** `gh run watch` (or repo → Actions). It's green when `systemctl is-active foreman`
   prints `active`.
3. **Verify the app is live:** `curl -fsS https://foreman.kongsites.com/health`.
4. **Confirm the gateway was untouched** (do this before/after any server-side change): the gateway is a
   *separate* service on a *separate* port owned by a *separate* user. A push only ever restarts
   `foreman.service` (the sudoers rule allows nothing else). If you ever SSH in manually, never
   `systemctl` anything but `foreman`, and never bind/free port 80.

> The PM Core itself runs on each person's **local machine**, not here — this server only hosts the
> server-side surface (today the PWA + `/health`; later the relay hub). Pushing to `main` redeploys the
> server side; local processes update themselves separately (`foreman update`, see DESIGN §11.1).

## Open security follow-ups (not yet done)

These were created when wiring up CI and are tracked here so they aren't forgotten:

- **Port 22 is world-open.** It was IP-restricted (DO Cloud Firewall) but opened so the GitHub-hosted
  runner could SSH in. Tighten it back to a deploy source, or move deploys to a self-hosted/pinned egress.
- **Root still allows password login.** Recommend key-only auth (`PasswordAuthentication no`) + `fail2ban`.
- **The public URL has no login gate.** Cloudflare Access (Zero-Trust email gate) was scoped but not
  enabled — anyone with the URL can reach the PWA. Add it before real multi-user data lives here.
- **Rotate the Cloudflare API token** that was used for tunnel setup (it was pasted in a chat).
