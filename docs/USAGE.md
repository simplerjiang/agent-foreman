# Foreman — Usage (local / server · accounts · install)

> Three things, plainly: **how to install**, **how to use it locally vs. on a server**, and **how
> accounts work**. (中文版：[USAGE.zh-CN.md](USAGE.zh-CN.md))

---

## 0. The 1-minute model

One codebase, two ways to run:

| | Local · personal mode | Server · team mode |
|---|---|---|
| Command | `foreman app` (windowed) / `foreman serve` | `foreman serve` (with `mode: team` in config) |
| Runs on | your own PC | a server you control |
| Does what | actually drives `claude` / `codex`, monitors, reviews, pushes approvals to your phone | acts as a **relay/switchboard** so several people's local processes can dial in and be reached from a phone |
| Accounts | **none** — single user | **yes** — admin builds users, members redeem an invite, then mint access keys |
| Data | sessions / diffs / recipes all live in the local DB | the server stores **only** account & key *hashes* — never your code, diffs, recipes, or LLM keys |

**In one line:** the part that does the work (the PM Core) always runs on **your own machine**; the
server is just a switchboard + phone gateway so you can watch progress and approve remotely.

---

## 1. Accounts

### 1.1 Local personal mode — no accounts

`foreman app` / personal-mode `foreman serve` has **no login, no username/password**. It trusts the
local loopback; remote access is the job of a tunnel (Tailscale / Cloudflare Tunnel / etc.).

- `.env` has a `FOREMAN_AUTH_TOKEN` (a phone token); leave it blank to auto-generate one on first run.
- ⚠️ The `foreman token` command is **not implemented yet** (roadmap P3), so personal mode today is
  protected by your network/tunnel, not by a login.

### 1.2 Server team mode — accounts, but **no default account**

Team mode has a full account system with **no self-signup** (only an admin can create accounts):

```
admin builds a user ─▶ one-time invite code ─▶ user sets a password at /redeem.html (activates)
        └─▶ once logged in, the user mints "access keys" at /keys.html for their local processes
```

**Gotcha:** there is **no built-in command to create the first admin**, and **no default
credentials**. `create_account` is only called by ① the admin console API (which itself requires an
existing admin — chicken-and-egg) and ② a demo script. So the **first admin must be created by hand**
(see 1.3).

### 1.3 Create the first admin (run once, on the server)

SSH into the box (`/opt/foreman/app`, as the deploy user), inside the venv:

```bash
cd /opt/foreman/app
. .venv/bin/activate
python - <<'PY'
from foreman.shared.config import load_config
from foreman.server.store import ServerStore
from foreman.server.auth_manager import AuthManager
cfg = load_config("config.yaml")                 # use the same config to get the right db path
store = ServerStore(cfg.server.db_path); store.init()
auth = AuthManager(store)
print("existing accounts:", auth.list_accounts())   # metadata only — never password hashes
print(auth.create_account("admin", "a-strong-password", role="admin", display_name="Admin"))
PY
```

- Writes the DB directly — **no restart needed**.
- Then open `https://<your-domain>/admin.html`, log in, and build users (password = active now;
  blank password = a one-time invite code they redeem at `/redeem.html`).

### 1.4 The three kinds of credential (don't mix them up)

| Credential | Who uses it | Created where | Purpose |
|---|---|---|---|
| **username + password** | a person | admin builds / invite redeem | log into the PWA |
| **invite code** | a new user activating | admin console, **shown once** | one-time, set a password at `/redeem.html` |
| **access key** | your local process | `/keys.html` after login, **shown once** | lets local `foreman app` dial into the server relay; one per machine, revocable |
| **LLM API key** | Foreman's "brain" | your own `.env` | your model for review/briefings — **never leaves your machine** |

---

## 2. Install

### 2.1 Prerequisites

- **Python ≥ 3.11**, **git**.
- To actually drive agents, install **`claude` (Claude Code)** and/or **`codex` (Codex CLI)** on the
  machine so they're callable from the shell.
- Your own **LLM API** (OpenAI-compatible or Anthropic-compatible) for the PM's review/briefings.

### 2.2 Local (personal mode — the common case)

```bash
git clone https://github.com/simplerjiang/agent-foreman.git
cd agent-foreman
python -m venv .venv && . .venv/bin/activate        # Windows: . .venv/Scripts/activate

pip install -e ".[client]"          # full PC app (drive agents + local UI + monitor + tray window)
# for tests: pip install -e ".[client,server,dev]"

cp .env.example .env                # set FOREMAN_LLM_API_KEY
cp config.example.yaml config.yaml  # set workspaces allowlist, llm; keep mode: personal
python scripts/gen_vapid.py         # for phone push: generate a VAPID keypair into .env / config

foreman app                         # native window (closing it = offline); default http://127.0.0.1:8788
# or headless: foreman serve         # default http://127.0.0.1:8787
```

For phone access to a local instance, run a tunnel (Tailscale / Cloudflare Tunnel) and put the HTTPS
URL it gives you into `config.yaml` → `server.public_base_url`.

### 2.3 Server (team mode)

One-time server setup: see [`../deploy/README.md`](../deploy/README.md) (`deploy/bootstrap.sh`, as
root: create the `foreman` user, clone to `/opt/foreman/app`, venv, `pip install -e ".[server]"`,
write `config.yaml`/`.env`, install the systemd unit, open the port).

Key team-mode config (`config.yaml`):

```yaml
server:
  mode: team                 # turn on team mode (default is personal)
  host: 127.0.0.1
  port: 8787
  db_path: foreman-server.db # server DB: only account/key hashes — never code/recipes/LLM keys
```

⚠️ The `.[server]` extra does **not** include the agent-driving deps — the server is only the relay +
PWA; it never runs `claude`/`codex`.

### 2.4 Deploy (CI is already wired)

```
edit → git push to main → GitHub Actions SSHes into the server
     → git reset --hard origin/main → pip install -e ".[server]" → restart the foreman service
```

- Progress: `gh run watch`. Liveness: `curl https://<your-domain>/health`.
- Front-end assets (CSS/JS) are **auto-versioned**: the server stamps the deployed git SHA into `?v=`,
  so each deploy is fresh on reload — **no manual cache busting**.

---

## 3. CLI reference

| Command | What it does |
|---|---|
| `foreman app` | Start the PC app: engine + native window + tray (personal mode) |
| `foreman serve` | Start the backend (personal or team; long-running) |
| `foreman dispatch "<task>" --workspace <path> [--agent claude-code\|codex]` | Create a session and run a task to completion |
| `foreman seed-examples` | Seed the built-in starter recipes into the local DB (idempotent) |
| `foreman version` | Print version |
| `foreman token --rotate` | ⚠️ not implemented yet (roadmap P3) |

## 4. Key endpoints (team mode)

- Auth: `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`, `POST /api/auth/redeem` (redeem an invite, no auth)
- Self-service: `GET/POST /api/keys`, `DELETE /api/keys/{id}`, `GET /api/processes` (your own machines)
- Admin: `GET/POST /api/admin/accounts`, `POST /api/admin/accounts/{id}/invite`, `POST /api/admin/accounts/{id}/status`, `GET /api/admin/health` (aggregate counts only — never anyone's content)
- Pages: `/` (dashboard), `/admin.html`, `/keys.html`, `/redeem.html`

---

## 5. FAQ

- **Changed the UI but the server still shows old colors?** Usually Cloudflare edge cache. Now solved
  via `?v=<git SHA>` auto-versioning; a normal reload is enough.
- **Logged into the server but it's empty?** Expected — the dashboard data comes from your local
  `foreman app` once it dials in with an access key. Live proxy-while-online is a deferred rollout.
- **`foreman app` didn't open a window?** `pywebview` (in `.[client]`) isn't installed; it falls back
  to headless — open the printed local URL in a browser.
- **Editing in a worktree but running old code?** The editable install imports from the main repo's
  `src`; set `PYTHONPATH=<worktree>/src` to exercise the worktree's code.

## 6. Known gaps

- **No CLI to create an admin** (the first admin is created by hand — see 1.3). A `foreman admin
  create-user --admin` command would close this.
- **The public URL has no built-in login gate** — front it with Cloudflare Access (or similar) before
  real multi-user data lives there.
- `foreman token` is not implemented (P3).
