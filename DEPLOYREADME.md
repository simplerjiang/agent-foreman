# Foreman Deployment Guide

Languages: [English](#english) | [中文](#中文)

## English

This guide is for deploying the server-side Foreman component. The Windows exe is mainly a portable local desktop client: download it, run it on the computer that owns the project workspace, and configure it there. There is no special exe deployment step unless you are distributing the desktop client to users.

Deploy the server when you need one of these:

- phone access through HTTPS;
- a cloud relay so your phone can control the PM agent running on your PC;
- team-mode accounts and access keys;
- a long-running public PWA endpoint.

Do not put real domains, server IPs, tokens, SSH keys, model names, or API keys into committed files. Use placeholders in docs and keep real values in `.env`, `config.yaml` on the target machine, or your secret manager.

### Deployment Topology

```text
Local PC                         Server                         Phone
Foreman app  -- outbound WSS --> Foreman serve -- HTTPS PWA --> Browser/PWA
PM Core                           auth + relay                  approve / dispatch
Project workspaces                no user project checkout      timeline
```

The local PC is where development work happens. The server should be treated as a relay and web surface, not as the machine that edits your projects.

### Linux Quick Install

Run this on a fresh Ubuntu/Debian server as `root`. Replace `REPO_URL` before running.

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

After the service is healthy, put it behind HTTPS. Caddy example:

```caddyfile
foreman.example.com {
  reverse_proxy 127.0.0.1:8787
}
```

Nginx example:

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

Run PowerShell as Administrator. Replace `$RepoUrl` and `$AppDir` before running. This uses Windows Task Scheduler so no third-party service wrapper is required.

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

For HTTPS on Windows, place Foreman behind a reverse proxy or tunnel that terminates TLS and forwards to `127.0.0.1:8787`. Keep the public URL generic in committed docs, for example `https://foreman.example.com`.

### Required Configuration

Set these on the deployed server:

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

Only add model/API keys on the server if you intentionally run server-side features that require them. For the common relay setup, the local PC owns PM execution and model configuration.

### Team Mode

For team mode, set `server.mode: team`, start the service, then create the first admin on the server:

```bash
foreman create-admin admin --config /opt/foreman/app/config.yaml
```

Log into the PWA, create users or access keys, then connect each local Foreman app to the relay URL and its access key from the app settings.

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

### Verification

- Local health: `curl -fsS http://127.0.0.1:8787/health`
- Public health: `curl -fsS https://foreman.example.com/health`
- Linux logs: `journalctl -u foreman -f`
- Windows status: `Get-ScheduledTask -TaskName ForemanServer`
- Phone: open the public URL, install the PWA, sign in or enter the auth token, then confirm timeline and approval cards load.

### Production Checklist

- Use HTTPS before exposing phone access.
- Keep `FOREMAN_AUTH_TOKEN` private and rotate it if shared accidentally.
- Lock down SSH/RDP to known networks or VPNs.
- Keep firewall rules narrow. The reverse proxy should be public; Foreman can stay on `127.0.0.1:8787`.
- Back up `config.yaml`, `.env`, and the server database if you use accounts/team mode.
- Never commit generated keys, real domains, IPs, provider URLs, model names, or API keys.

## 中文

本文档用于部署 Foreman 的服务端组件。Windows exe 主要是本地便携桌面客户端：下载后放在拥有项目工作区的电脑上运行并配置即可。除非你要分发桌面客户端，否则 exe 本身没有特殊的“部署”步骤。

当你需要下面能力时，才需要部署服务端：

- 通过 HTTPS 在手机上访问；
- 通过云端 relay 控制运行在电脑上的 PM Agent；
- 使用团队模式账号和 access key；
- 提供长期运行的公开 PWA 入口。

不要把真实域名、服务器 IP、token、SSH key、模型名或 API key 写进提交的文件。文档里使用占位符，真实值放在目标机器的 `.env`、`config.yaml` 或你的 secret manager 里。

### 部署拓扑

```text
本地电脑                         服务器                         手机
Foreman app  -- 出站 WSS --> Foreman serve -- HTTPS PWA --> 浏览器/PWA
PM Core                         认证 + relay                  审批 / 派活
项目工作区                       不需要用户项目 checkout        时间线
```

开发工作发生在本地电脑。服务端应该被当作 relay 和 Web 入口，而不是实际改项目代码的机器。

### Linux 快速安装

在新的 Ubuntu/Debian 服务器上以 `root` 执行。执行前替换 `REPO_URL`。

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

服务健康后，把它放到 HTTPS 后面。Caddy 示例：

```caddyfile
foreman.example.com {
  reverse_proxy 127.0.0.1:8787
}
```

Nginx 示例：

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

以管理员身份运行 PowerShell。执行前替换 `$RepoUrl` 和 `$AppDir`。这里使用 Windows 计划任务，不依赖第三方 service wrapper。

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

Windows 上的 HTTPS 建议交给反向代理或隧道：公网 HTTPS 入口终止 TLS，再转发到 `127.0.0.1:8787`。提交到 git 的文档里只写通用占位符，例如 `https://foreman.example.com`。

### 必要配置

部署服务器上的 `config.yaml`：

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

只有当你明确要在服务端运行需要模型调用的功能时，才把模型/API key 放到服务器上。常见 relay 部署里，PM 执行和模型配置属于本地电脑。

### 团队模式

团队模式下，把 `server.mode` 设成 `team`，启动服务后在服务器上创建第一个管理员：

```bash
foreman create-admin admin --config /opt/foreman/app/config.yaml
```

登录 PWA 后创建用户或 access key，再在每台本地 Foreman app 的设置里填写 relay URL 和对应 access key。

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

### 验证

- 本机健康检查：`curl -fsS http://127.0.0.1:8787/health`
- 公网健康检查：`curl -fsS https://foreman.example.com/health`
- Linux 日志：`journalctl -u foreman -f`
- Windows 状态：`Get-ScheduledTask -TaskName ForemanServer`
- 手机：打开公网 URL，安装 PWA，登录或输入 auth token，确认时间线和审批卡可以加载。

### 生产检查清单

- 手机访问必须先上 HTTPS。
- `FOREMAN_AUTH_TOKEN` 要保密；如果误发，立即轮换。
- SSH/RDP 只对可信网络或 VPN 开放。
- 防火墙规则保持最窄：公网入口给反向代理，Foreman 本体可以只监听 `127.0.0.1:8787`。
- 使用账号/团队模式时，备份 `config.yaml`、`.env` 和服务端数据库。
- 不要提交生成的 key、真实域名、IP、provider URL、模型名或 API key。
