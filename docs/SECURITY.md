# Security model

Foreman exposes a **high-privilege control channel** (it can dispatch tasks that run code on your
machine and approve dangerous actions). Treat it accordingly.

## Threat model
- The phone surface is reachable from outside the PC → an attacker with the URL + token could
  dispatch tasks or approve destructive operations.
- The agents themselves may attempt dangerous actions (push, deploy, `rm -rf`).

## Controls (defense in depth)

1. **No public ports by default.** Backend binds `127.0.0.1`. External access only via an
   authenticated tunnel:
   - **Tailscale** (recommended): private mesh, `tailscale serve` gives HTTPS, never public.
   - **Cloudflare Tunnel**: public HTTPS hostname, no inbound ports, put Access in front.
   - **frp**: self-hosted; you own the relay.
2. **Single-user Bearer token.** Generated on first run, shown once on the PC. Phone stores it.
   Every REST/WS call must present it. Rotate via `foreman token --rotate`.
3. **Device pairing.** First connection from a new device shows a pairing code on the PC that you
   must confirm. Unpaired devices are rejected.
4. **Approval nonces.** Each approval request carries a one-time nonce to prevent replay.
5. **Workspace allowlist.** Agents may only write inside explicitly approved directories.
6. **Dangerous-command Gate.** `git push`, deploy, secret changes, destructive ops always require
   human approval — even if an attacker got past auth, they hit this wall.
7. **Secrets hygiene.** Your LLM API key and the VAPID **private** key live in `.env`
   (git-ignored, never stored in the DB, never sent to the phone).

## HTTPS requirement
Web Push **requires HTTPS** and a service worker on a secure origin. `localhost` counts as secure
for development; for the phone you need real HTTPS — use Tailscale/Cloudflare/frp as above.

## iOS caveat
iOS supports Web Push only on **iOS 16.4+** and only after the PWA is **added to the Home Screen**.
Document this in onboarding.

## What we deliberately do NOT do
- No telemetry / phone-home.
- No third-party cloud storage of session data (local SQLite only).
- No storing your LLM key in the browser.
