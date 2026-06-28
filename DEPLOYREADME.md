# Foreman Deployment Guide

Languages: [English](#english) | [中文](#中文)

## English

First, the important bit: the exe is not something you deploy to a server. It is the local desktop app. Put it on the computer that has your projects, run it there, and configure it there.

You deploy the server only when you want phone access or relay:

- you want to open Foreman from your phone over HTTPS;
- you want your phone to send commands to the PM agent running on your PC;
- you want team-mode accounts and access keys;
- you want a PWA endpoint that stays online even when your laptop is asleep.

Keep real domains, IPs, tokens, SSH keys, model names, and API keys out of committed files. The examples below use placeholders on purpose.

### Mental Model

```text
PC with your projects             Your server                    Your phone
Foreman app  -- outbound WSS -->  Foreman serve -- HTTPS PWA -->  Browser/PWA
PM Core                           auth + relay                   approve / dispatch
Project workspaces                no user project checkout       timeline
```

The PC does the actual project work. The server is just the front door and relay.

### Linux Quick Install

Use this on a fresh Ubuntu/Debian box as `root`. Change `REPO_URL` first.

```bash
export REPO_URL="https://github.com/<owner>/<repo>.git"
export APP_DIR="/opt/foreman/app"
export SERVICE_USER="foreman"
export PORT="8787"

apt-get update
apt-get install -y git python3 python3-venv python3-pip curl

id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$(dirname "$APP_DIR")"

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" pull --ff-only
fi

cd "$APP_DIR"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[server]"

[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f .env ] || cp .env.example .env

python - <<'PY'
from pathlib import Path
import secrets

path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
token = secrets.token_urlsafe(32)
out = []
seen = False
generated = False
for line in lines:
    if line.startswith("FOREMAN_AUTH_TOKEN="):
        seen = True
        current = line.split("=", 1)[1].strip()
        if current:
            out.append(line)
        else:
            out.append("FOREMAN_AUTH_TOKEN=" + token)
            generated = True
    else:
        out.append(line)
if not seen:
    out.append("FOREMAN_AUTH_TOKEN=" + token)
    generated = True
path.write_text("\n".join(out) + "\n", encoding="utf-8")
print("FOREMAN_AUTH_TOKEN is set in .env. Save it somewhere private." if generated else "Existing FOREMAN_AUTH_TOKEN kept.")
PY

chown -R "$SERVICE_USER:$SERVICE_USER" "$(dirname "$APP_DIR")"

cat >/etc/systemd/system/foreman.service <<SERVICE
[Unit]
Description=Foreman server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/foreman serve --config $APP_DIR/config.yaml --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now foreman
curl -fsS "http://127.0.0.1:$PORT/health"
```

If that last `curl` returns JSON, the service is alive.

Now put it behind HTTPS. Caddy is the shortest route:

```caddyfile
foreman.example.com {
  reverse_proxy 127.0.0.1:8787
}
```

Nginx works too:

```nginx
server {
    listen 443 ssl http2;
    server_name foreman.example.com;

    ssl_certificate /etc/letsencrypt/live/foreman.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/foreman.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

### Windows Quick Install

Run PowerShell as Administrator. Change `$RepoUrl` and `$AppDir` first. This uses Windows Task Scheduler so you do not need NSSM or another service wrapper.

```powershell
$RepoUrl = "https://github.com/<owner>/<repo>.git"
$AppDir = "C:\Foreman\app"
$Port = 8787
$TaskName = "ForemanServer"

New-Item -ItemType Directory -Force -Path (Split-Path $AppDir) | Out-Null

if (!(Test-Path "$AppDir\.git")) {
    git clone $RepoUrl $AppDir
} else {
    git -C $AppDir pull --ff-only
}

py -3.11 -m venv "$AppDir\.venv"
& "$AppDir\.venv\Scripts\python.exe" -m pip install -U pip
& "$AppDir\.venv\Scripts\python.exe" -m pip install -e "$AppDir[server]"

if (!(Test-Path "$AppDir\config.yaml")) {
    Copy-Item "$AppDir\config.example.yaml" "$AppDir\config.yaml"
}
if (!(Test-Path "$AppDir\.env")) {
    Copy-Item "$AppDir\.env.example" "$AppDir\.env"
}

$EnvPath = "$AppDir\.env"
$Token = [Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).TrimEnd("=")
$Lines = Get-Content -Encoding UTF8 $EnvPath
$Found = $false
$Generated = $false
$Next = foreach ($Line in $Lines) {
    if ($Line.StartsWith("FOREMAN_AUTH_TOKEN=")) {
        $Found = $true
        $Current = $Line.Substring("FOREMAN_AUTH_TOKEN=".Length).Trim()
        if ($Current) {
            $Line
        } else {
            $Generated = $true
            "FOREMAN_AUTH_TOKEN=$Token"
        }
    } else {
        $Line
    }
}
if (-not $Found) {
    $Next += "FOREMAN_AUTH_TOKEN=$Token"
    $Generated = $true
}
Set-Content -Encoding UTF8 $EnvPath $Next

$Exe = "$AppDir\.venv\Scripts\foreman.exe"
$Args = "serve --config `"$AppDir\config.yaml`" --host 127.0.0.1 --port $Port"
$Action = New-ScheduledTaskAction -Execute $Exe -Argument $Args -WorkingDirectory $AppDir
$Trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -RunLevel Highest -User "SYSTEM" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 5
Invoke-RestMethod "http://127.0.0.1:$Port/health"
if ($Generated) {
    Write-Host "FOREMAN_AUTH_TOKEN is set in $EnvPath. Save it somewhere private."
} else {
    Write-Host "Existing FOREMAN_AUTH_TOKEN kept in $EnvPath."
}
```

For public access on Windows, still put Foreman behind HTTPS: a reverse proxy, a tunnel, or a gateway that forwards to `127.0.0.1:8787`.

### Config You Should Actually Check

On the server:

```yaml
server:
  host: 127.0.0.1
  port: 8787
  public_base_url: "https://foreman.example.com"
  mode: personal  # or team
```

In `.env`:

```dotenv
FOREMAN_AUTH_TOKEN=<long-random-token>
FOREMAN_VAPID_PRIVATE_KEY=<private-web-push-key-if-web-push-is-enabled>
```

For the common relay setup, the model/API config belongs on the local PC, not the server. Only put model/API keys on the server if you knowingly enable server-side features that need them.

### Team Mode

Set `server.mode: team`, start the service, then create the first admin on the server:

```bash
foreman create-admin admin --config /opt/foreman/app/config.yaml
```

After that, log into the PWA, create users or access keys, and connect each local Foreman app to the relay URL from its settings page.

### Updating

Linux:

```bash
cd /opt/foreman/app
sudo -u foreman git pull --ff-only
sudo -u foreman .venv/bin/python -m pip install -e ".[server]"
sudo systemctl restart foreman
curl -fsS http://127.0.0.1:8787/health
```

Windows:

```powershell
git -C C:\Foreman\app pull --ff-only
& C:\Foreman\app\.venv\Scripts\python.exe -m pip install -e "C:\Foreman\app[server]"
Restart-ScheduledTask -TaskName ForemanServer
Invoke-RestMethod http://127.0.0.1:8787/health
```

### Quick Checks

- Local health: `curl -fsS http://127.0.0.1:8787/health`
- Public health: `curl -fsS https://foreman.example.com/health`
- Linux logs: `journalctl -u foreman -f`
- Windows task: `Get-ScheduledTask -TaskName ForemanServer`
- Phone check: open the public URL, install the PWA, sign in or enter the auth token, and confirm the timeline loads.

### Production Habits

- Do not expose phone access without HTTPS.
- Keep `FOREMAN_AUTH_TOKEN` private; rotate it if it leaks.
- Restrict SSH/RDP to trusted networks or VPNs.
- Let the reverse proxy face the public internet; keep Foreman on `127.0.0.1:8787` when possible.
- Back up `config.yaml`, `.env`, and the server database if you use accounts/team mode.
- Do not commit generated keys, real domains, IPs, provider URLs, model names, or API keys.

## 中文

先说最重要的：exe 不需要部署到服务器。它是本地桌面端，应该放在有项目目录的那台电脑上运行，也在那台电脑上配置。

只有下面这些情况才需要部署服务端：

- 你想在手机上通过 HTTPS 打开 Foreman；
- 你想让手机把命令转给电脑上的 PM Agent；
- 你需要团队模式账号和 access key；
- 你希望 PWA 入口一直在线，即使笔记本睡眠也能打开。

真实域名、IP、token、SSH key、模型名、API key 都不要写进提交。下面全部用占位符，是故意的。

### 先理解拓扑

```text
有项目的电脑                     你的服务器                       你的手机
Foreman app  -- 出站 WSS -->    Foreman serve -- HTTPS PWA -->    浏览器/PWA
PM Core                         认证 + relay                    审批 / 派活
项目工作区                       不需要用户项目 checkout          时间线
```

真正改项目的是电脑。服务器只是入口和转发层。

### Linux 快速安装

在新的 Ubuntu/Debian 机器上用 `root` 执行。先改 `REPO_URL`。

```bash
export REPO_URL="https://github.com/<owner>/<repo>.git"
export APP_DIR="/opt/foreman/app"
export SERVICE_USER="foreman"
export PORT="8787"

apt-get update
apt-get install -y git python3 python3-venv python3-pip curl

id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$(dirname "$APP_DIR")"

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" pull --ff-only
fi

cd "$APP_DIR"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[server]"

[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f .env ] || cp .env.example .env

python - <<'PY'
from pathlib import Path
import secrets

path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
token = secrets.token_urlsafe(32)
out = []
seen = False
generated = False
for line in lines:
    if line.startswith("FOREMAN_AUTH_TOKEN="):
        seen = True
        current = line.split("=", 1)[1].strip()
        if current:
            out.append(line)
        else:
            out.append("FOREMAN_AUTH_TOKEN=" + token)
            generated = True
    else:
        out.append(line)
if not seen:
    out.append("FOREMAN_AUTH_TOKEN=" + token)
    generated = True
path.write_text("\n".join(out) + "\n", encoding="utf-8")
print("FOREMAN_AUTH_TOKEN is set in .env. Save it somewhere private." if generated else "Existing FOREMAN_AUTH_TOKEN kept.")
PY

chown -R "$SERVICE_USER:$SERVICE_USER" "$(dirname "$APP_DIR")"

cat >/etc/systemd/system/foreman.service <<SERVICE
[Unit]
Description=Foreman server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/foreman serve --config $APP_DIR/config.yaml --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now foreman
curl -fsS "http://127.0.0.1:$PORT/health"
```

最后一行 `curl` 如果返回 JSON，说明服务已经活了。

然后把它放到 HTTPS 后面。Caddy 最省事：

```caddyfile
foreman.example.com {
  reverse_proxy 127.0.0.1:8787
}
```

Nginx 也可以：

```nginx
server {
    listen 443 ssl http2;
    server_name foreman.example.com;

    ssl_certificate /etc/letsencrypt/live/foreman.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/foreman.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

### Windows 快速安装

用管理员身份打开 PowerShell。先改 `$RepoUrl` 和 `$AppDir`。这里用 Windows 计划任务，不需要 NSSM 或其他 service wrapper。

```powershell
$RepoUrl = "https://github.com/<owner>/<repo>.git"
$AppDir = "C:\Foreman\app"
$Port = 8787
$TaskName = "ForemanServer"

New-Item -ItemType Directory -Force -Path (Split-Path $AppDir) | Out-Null

if (!(Test-Path "$AppDir\.git")) {
    git clone $RepoUrl $AppDir
} else {
    git -C $AppDir pull --ff-only
}

py -3.11 -m venv "$AppDir\.venv"
& "$AppDir\.venv\Scripts\python.exe" -m pip install -U pip
& "$AppDir\.venv\Scripts\python.exe" -m pip install -e "$AppDir[server]"

if (!(Test-Path "$AppDir\config.yaml")) {
    Copy-Item "$AppDir\config.example.yaml" "$AppDir\config.yaml"
}
if (!(Test-Path "$AppDir\.env")) {
    Copy-Item "$AppDir\.env.example" "$AppDir\.env"
}

$EnvPath = "$AppDir\.env"
$Token = [Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).TrimEnd("=")
$Lines = Get-Content -Encoding UTF8 $EnvPath
$Found = $false
$Generated = $false
$Next = foreach ($Line in $Lines) {
    if ($Line.StartsWith("FOREMAN_AUTH_TOKEN=")) {
        $Found = $true
        $Current = $Line.Substring("FOREMAN_AUTH_TOKEN=".Length).Trim()
        if ($Current) {
            $Line
        } else {
            $Generated = $true
            "FOREMAN_AUTH_TOKEN=$Token"
        }
    } else {
        $Line
    }
}
if (-not $Found) {
    $Next += "FOREMAN_AUTH_TOKEN=$Token"
    $Generated = $true
}
Set-Content -Encoding UTF8 $EnvPath $Next

$Exe = "$AppDir\.venv\Scripts\foreman.exe"
$Args = "serve --config `"$AppDir\config.yaml`" --host 127.0.0.1 --port $Port"
$Action = New-ScheduledTaskAction -Execute $Exe -Argument $Args -WorkingDirectory $AppDir
$Trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -RunLevel Highest -User "SYSTEM" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 5
Invoke-RestMethod "http://127.0.0.1:$Port/health"
if ($Generated) {
    Write-Host "FOREMAN_AUTH_TOKEN is set in $EnvPath. Save it somewhere private."
} else {
    Write-Host "Existing FOREMAN_AUTH_TOKEN kept in $EnvPath."
}
```

Windows 上如果要公网访问，也请放到 HTTPS 后面：反向代理、隧道或网关都行，转发到 `127.0.0.1:8787`。

### 真正需要检查的配置

服务器上的 `config.yaml`：

```yaml
server:
  host: 127.0.0.1
  port: 8787
  public_base_url: "https://foreman.example.com"
  mode: personal  # 或 team
```

`.env`：

```dotenv
FOREMAN_AUTH_TOKEN=<long-random-token>
FOREMAN_VAPID_PRIVATE_KEY=<private-web-push-key-if-web-push-is-enabled>
```

常见 relay 部署里，模型/API 配置应该在本地电脑上，不在服务器上。只有你明确启用了服务端模型能力时，才需要把模型/API key 放到服务器。

### 团队模式

把 `server.mode` 设成 `team`，启动服务后在服务器上创建第一个管理员：

```bash
foreman create-admin admin --config /opt/foreman/app/config.yaml
```

然后登录 PWA，创建用户或 access key，再到每台本地 Foreman app 的设置页里填写 relay URL。

### 更新

Linux：

```bash
cd /opt/foreman/app
sudo -u foreman git pull --ff-only
sudo -u foreman .venv/bin/python -m pip install -e ".[server]"
sudo systemctl restart foreman
curl -fsS http://127.0.0.1:8787/health
```

Windows：

```powershell
git -C C:\Foreman\app pull --ff-only
& C:\Foreman\app\.venv\Scripts\python.exe -m pip install -e "C:\Foreman\app[server]"
Restart-ScheduledTask -TaskName ForemanServer
Invoke-RestMethod http://127.0.0.1:8787/health
```

### 快速检查

- 本机健康检查：`curl -fsS http://127.0.0.1:8787/health`
- 公网健康检查：`curl -fsS https://foreman.example.com/health`
- Linux 日志：`journalctl -u foreman -f`
- Windows 计划任务：`Get-ScheduledTask -TaskName ForemanServer`
- 手机检查：打开公网 URL，安装 PWA，登录或输入 auth token，确认时间线能加载。

### 生产习惯

- 没有 HTTPS 时，不要开放手机访问。
- `FOREMAN_AUTH_TOKEN` 要保密；泄露就轮换。
- SSH/RDP 只开放给可信网络或 VPN。
- 尽量让反向代理面对公网，Foreman 本体保持在 `127.0.0.1:8787`。
- 使用账号/团队模式时，备份 `config.yaml`、`.env` 和服务端数据库。
- 不要提交生成的 key、真实域名、IP、provider URL、模型名或 API key。
