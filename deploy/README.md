# Deployment (server-side component)

The server hosts the **team-mode** component (today: the PWA + `/health`; later: the relay hub +
admin console). The actual PM Core runs on each person's local machine — see
[DESIGN.zh-CN.md §8](../docs/DESIGN.zh-CN.md).

## Deployment mode: personal (default) vs team

`server.mode` (config.yaml) decides what `foreman serve` assembles:

- **`personal` (default)** — just `/health` + the PWA shell + the single-user REST/WS. On a public
  bind it **fails closed unless `FOREMAN_AUTH_TOKEN` is set** (issue #1 P0): no token + non-loopback
  host (or a `public_base_url`) → the process refuses to start. With a token, every operational
  endpoint (`/api/*`, `/hooks`, `/ws`) requires `Authorization: Bearer <token>` (the PWA prompts for
  it once and stores it). The public deployment runs this mode today.
- **`team`** — adds accounts, access keys, the relay, and the display cache; each operational
  endpoint is gated per-account. There is **no self-signup**, so bootstrap the first admin once:

  ```bash
  # on the server, with config.yaml setting server.mode: team and server.db_path
  foreman create-admin <username>          # prompts for the password
  ```

  Then that admin logs into the PWA and builds the rest of the team from the admin console
  (`/admin.html`). Live acceptance to run after enabling team mode: PWA login → mint an access key
  (`/keys.html`) → a local process completes the relay handshake → the PWA reads the display cache
  while that machine is offline.

Auto-deploy: push to `main` → GitHub Actions SSHes in as the unprivileged `foreman` user and runs
[`remote_deploy.sh`](remote_deploy.sh) (git reset to `origin/main` → `pip install -e .` → restart).

## Isolation guarantees
- Runs as a dedicated **unprivileged** `foreman` user, in `/opt/foreman/app`, in a venv.
- Listens on **:8787** only. **Does not touch any other service that may share the box.**
- `foreman` may `sudo systemctl` **only** the `foreman` service (narrow sudoers rule).

## One-time server setup (done via `deploy/bootstrap.sh`, run as root)
1. Create the `foreman` user; install the CI deploy **public** key into its `authorized_keys`.
2. Clone this repo to `/opt/foreman/app`; create a venv; `pip install -e .`.
3. Write `/opt/foreman/app/config.yaml` (if an LLM gateway runs on the same box, the server-side LLM
   can reach it over loopback, e.g. `http://127.0.0.1/v1`) and `.env` (auth token).
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

## Deployment shape

> This repo is **public** — the concrete host / IP / domain are **not committed**. Keep the real
> values in the git-ignored `ServerInfo.txt` and in the GitHub Actions secrets. The shape below is
> generic on purpose.

| Thing | Value |
|-------|-------|
| Server | a small Ubuntu VPS (≈1–2 GB), reachable over SSH for the deploy user |
| Foreman service | systemd `foreman.service`, user `foreman`, dir `/opt/foreman/app`, port **:8787** |
| Public URL | your HTTPS URL via a Cloudflare named tunnel (`cloudflared` service) |
| ⚠️ Co-tenant | If another service shares the box, Foreman never touches it — its sudoers rule only lets it manage `foreman.service`. |

The public URL is served by Cloudflare's tunnel → `127.0.0.1:8787`, so **no inbound port besides the
tunnel needs to be open** for phone access. (8787 may also be `ufw allow`ed for direct/LAN testing.)

## How to deploy (push) — and how to stay clear of co-tenant services

1. **Push to `main`.** That's the whole trigger — GitHub Actions does the rest (SSH as `foreman` →
   `remote_deploy.sh` → `git reset --hard origin/main` → `pip install -e .` → `sudo systemctl restart foreman`).
2. **Watch the run:** `gh run watch` (or repo → Actions). It's green when `systemctl is-active foreman`
   prints `active`.
3. **Verify the app is live:** `curl -fsS https://<your-domain>/health`.
4. **Confirm any co-tenant service was untouched** (before/after any server-side change): a push only
   ever restarts `foreman.service` (the sudoers rule allows nothing else). If you ever SSH in manually,
   never `systemctl` anything but `foreman`, and never bind/free a port another service owns.

> The PM Core itself runs on each person's **local machine**, not here — this server only hosts the
> server-side surface (today the PWA + `/health`; later the relay hub). Pushing to `main` redeploys the
> server side; local processes update themselves separately (`foreman update`, see DESIGN §11.1).

## HTTPS + hardening (issue #1 P2)

- **Force HTTP→HTTPS at Cloudflare** (SSL/TLS → Edge Certificates → *Always Use HTTPS*). That is the
  primary redirect; the app also honours `server.force_https: true` as a defense-in-depth fallback
  (it trusts `X-Forwarded-Proto`, so it won't loop behind Cloudflare's TLS termination).
- **HSTS:** turn on `server.hsts: true` only once HTTPS is stable end-to-end (Cloudflare can also
  emit HSTS). The app sends a conservative **CSP** by default (`server.csp`) plus
  `X-Content-Type-Options`, `X-Frame-Options`, and `Referrer-Policy` on every response.
- **/health** no longer leaks the DB path by default (`server.health_show_db: false`).

## Security hardening checklist

Recommended before real multi-user data lives on the box:

- **Restrict SSH (port 22)** to known sources (cloud firewall / `ufw`), or deploy from a pinned egress
  rather than a world-open GitHub-hosted runner.
- **Key-only SSH** — disable root password login (`PasswordAuthentication no`) and add `fail2ban`.
- **App-level access gate is now enforced** (issue #1 P0): operational endpoints require the bearer
  token in personal mode, or a per-account login in team mode. Cloudflare Access (Zero-Trust email
  gate) is still recommended as an additional outer layer before real multi-user data lives here.
- **Rotate any tokens** (e.g. the Cloudflare tunnel/API token) that may have been exposed during setup.
