# Foreman 使用指南（本地 / 线上 · 账号 · 安装）

> 这份文档说人话讲清楚三件事：**怎么装**、**本地和线上分别怎么用**、**账号是怎么回事**。
> 标了 💬 的地方是把术语翻成大白话。

---

## 0. 一分钟搞懂：两种用法

Foreman 同一套代码，有两种跑法：

| | 本地 · 个人模式 | 线上 · 团队模式 |
|---|---|---|
| 命令 | `foreman app`（带窗口）/ `foreman serve` | `foreman serve`（配置里 `mode: team`） |
| 跑在哪 | 你自己的 PC | 一台你自己的服务器（任意 Ubuntu VPS 即可） |
| 干什么 | 真正驱动 `claude` / `codex` 干活、监控、审阅、把审批推手机 | 当**总机**💬（中转站），让多个人的本地进程接进来，手机能远程看/批 |
| 账号 | **没有账号**，单人用 | **有账号**：管理员建人、成员用邀请码激活、再发接入密钥 |
| 数据 | 会话/diff/秘方都在本地库 | 服务器**只存**账号和密钥哈希，**不存**你的代码/diff/秘方/LLM key |

💬 **一句话**：干活的核心（PM Core）永远跑在**你自己的电脑**上；线上那台服务器只是个"总机+手机门户"，方便你人不在电脑前也能在手机上看进度、点批准。

---

## 1. 账号说明（重点）

### 1.1 本地个人模式 —— 没有账号

`foreman app` / 个人模式的 `foreman serve` **不需要登录、没有用户名密码**。它信任本机回环💬（只有本机能访问），远程访问靠隧道💬（Tailscale / Cloudflare Tunnel 之类把内网安全地暴露出去）。

- `.env` 里有个 `FOREMAN_AUTH_TOKEN`（给手机用的令牌），留空会在首次启动时自动生成并打印一次。
- ⚠️ 注意：`foreman token` 这个命令目前还**没实现**（路线图 P3），所以现在个人模式实际就是"靠网络/隧道保护"，不是靠登录。

### 1.2 线上团队模式 —— 有账号，但**没有默认账号**

线上服务器（team 模式）有完整账号体系，规则是 **不能自助注册**💬（没有"点我注册"，只能管理员给你开号）：

```
管理员建号 ──▶ 生成一次性邀请码 ──▶ 你在 /redeem.html 用邀请码设密码（激活）
       └──▶ 登录后在 /keys.html 自己生成「接入密钥」给本地进程用
```

**关键事实（容易踩坑）**：

- 代码里**没有内置"创建第一个管理员"的命令**，也**没有默认用户名/密码**。`create_account` 只被两处调用：①管理员控制台接口（但它本身要求你已经是管理员——先有鸡还是先有蛋）；②一个 demo 脚本 `scripts/accept_devices_panel.py`（建的是 alice/bob 测试号，跟线上无关）。
- 所以**第一个管理员必须手动建**（见下面 1.3）。
- 我**看不到服务器数据库**，没法告诉你线上现在到底有没有已建好的账号——查/建方法见 1.3。

### 1.3 怎么建第一个管理员（在服务器上跑一次）

SSH 上服务器（`/opt/foreman/app`，用部署那个 `foreman` 用户），在 venv 里跑：

```bash
cd /opt/foreman/app
. .venv/bin/activate
python - <<'PY'
from foreman.shared.config import load_config
from foreman.server.store import ServerStore
from foreman.server.auth_manager import AuthManager
cfg = load_config("config.yaml")              # 用线上同一份配置，拿到正确的 db 路径
store = ServerStore(cfg.server.db_path); store.init()
auth = AuthManager(store)
# 查现有账号（只看元数据，看不到密码）
print("现有账号：", auth.list_accounts())
# 没有 admin 就建一个（改成你要的用户名/强密码）
print(auth.create_account("admin", "换成你的强密码", role="admin", display_name="Admin"))
PY
```

- 直接写库即可，**不用重启服务**。
- 建好后：浏览器开 `https://<你的域名>/admin.html` → 用刚才的用户名密码登录 → 在控制台「建用户」：填密码=直接激活，留空密码=生成**一次性邀请码**发给对方，对方去 `/redeem.html` 设密码激活。
- ⚠️ 这是个真实的产品缺口——**没有 CLI 建管理员**。如果你愿意，我可以加一个 `foreman admin create-user` 命令（见文末）。

### 1.4 三种"凭据"分别是什么（别混）

| 凭据 | 谁用 | 在哪生成 | 干嘛 |
|---|---|---|---|
| **用户名 + 密码** | 人 | 管理员建 / 邀请码激活 | 登录 PWA（手机/网页） |
| **邀请码** | 新用户激活时 | 管理员控制台，**只显示一次** | 一次性，在 `/redeem.html` 设密码 |
| **接入密钥 access key** | 你的本地进程 | 登录后 `/keys.html`，**只显示一次** | 让本地 `foreman app` 拨进线上总机，一机一张、可单独吊销 |
| **LLM API key** | Foreman 的"大脑" | 你自己的 `.env` | 给 PM 审阅/简报调用你自己的大模型，**永不上服务器** |

---

## 2. 安装

### 2.1 前置

- **Python ≥ 3.11**、**git**。
- 想真正驱动 agent，本机还要装好 **`claude`（Claude Code）** 和/或 **`codex`（Codex CLI）**，能在命令行直接调用。
- 一个你自己的 **LLM API**（OpenAI 兼容 或 Anthropic 兼容），用于 PM 的审阅/简报。

### 2.2 本地（个人模式，最常用）

```bash
git clone https://github.com/simplerjiang/agent-foreman.git
cd agent-foreman
python -m venv .venv && . .venv/Scripts/activate   # Windows；Linux/mac 是 . .venv/bin/activate

pip install -e ".[client]"           # PC 应用全套（驱动 agent + 本地 UI + 监控 + 托盘窗口）
# 想跑测试： pip install -e ".[client,server,dev]"

cp .env.example .env                 # 填 FOREMAN_LLM_API_KEY
cp config.example.yaml config.yaml   # 填 workspaces 白名单、llm、（个人模式 mode 留 personal）
python scripts/gen_vapid.py          # 要手机推送的话，生成 VAPID 密钥对填进 .env / config

foreman app                          # 带原生窗口启动（关窗=离线）；默认 http://127.0.0.1:8788
# 或无界面后台： foreman serve        # 默认 http://127.0.0.1:8787
```

💬 `pip install -e` = 可编辑安装（改代码立即生效，不用重装）。`.[client]` = 装"客户端"那组依赖。

手机访问本地：配一个隧道（Tailscale / Cloudflare Tunnel），把隧道给的 HTTPS 地址填到 `config.yaml` 的 `server.public_base_url`。

### 2.3 线上（团队模式，服务器上）

服务器一次性安装见 [`deploy/README.md`](../deploy/README.md)（`deploy/bootstrap.sh` 以 root 跑：建 `foreman` 用户、克隆到 `/opt/foreman/app`、建 venv、`pip install -e ".[server]"`、写 `config.yaml`/`.env`、装 systemd、放行端口）。

团队模式的关键配置（`config.yaml`）：

```yaml
server:
  mode: team                 # ← 打开团队模式（默认是 personal）
  host: 127.0.0.1
  port: 8787
  db_path: foreman-server.db # 服务器库：只存账号/密钥哈希，绝无代码/秘方/LLM key
```

⚠️ 服务器的 `.[server]` 这组**不含**驱动 agent 的依赖——它只是总机+PWA，不在服务器上跑 `claude`/`codex`。

### 2.4 部署（已接好 CI，改完推一下就行）

```
改代码 → git push 到 main → GitHub Actions 自动 SSH 上服务器
       → git reset --hard origin/main → pip install -e ".[server]" → 重启 foreman 服务
```

- 看部署进度：`gh run watch`；验证存活：`curl https://<你的域名>/health`。
- 前端资源（CSS/JS）已做**自动版本化**：服务器把部署的 git SHA 注入 `?v=`，所以每次部署用户刷新就是新版，**不用手动清缓存**。

---

## 3. 线上现状（截至本文）

| 项 | 值 |
|---|---|
| 公网地址 | 你的 HTTPS 域名（Cloudflare 隧道 → 服务器 `:8787`；实际值放进 gitignore 的 `ServerInfo.txt`，不入库） |
| 模式 | **team（团队模式）** —— `/api/auth/login` 在、`/api/sessions` 返回 503（总机无本地库，正常） |
| 服务 | systemd `foreman.service`，用户 `foreman`，目录 `/opt/foreman/app` |
| 健康检查 | `/health` → `{"ok":true,"version":"0.1.0",...}` |

两个要注意的点：

1. **线上现在看不到"会话/决策卡"是正常的**：那些数据来自你**本地进程**（`foreman app` 用 access key 拨进总机后推上来的展示缓存）。没有本地进程接入时，登录后内容是空的。"在线实时代理"是后续要做的部分（见 TASKS T7.1）。
2. ⚠️ **公网地址目前没有登录门**（Cloudflare Access 还没开）——任何人拿到链接都能打开 PWA 页面。放真实多用户数据前，建议先加 Cloudflare Access 邮箱网关。（这是 `deploy/README.md` 里已记录的待办。）

---

## 4. CLI 命令速查

| 命令 | 作用 |
|---|---|
| `foreman app` | 启动 PC 应用：引擎 + 原生窗口 + 托盘（个人模式，开窗=在线） |
| `foreman serve` | 启动后端（个人或团队，长驻、阻塞） |
| `foreman dispatch "<任务>" --workspace <路径> [--agent claude-code\|codex] [--model <模型>]` | 建会话、把任务交给 agent 跑到完成 |
| `foreman seed-examples` | 把内置的起步「秘方」种进本地库（幂等） |
| `foreman version` | 看版本 |
| `foreman token --rotate` | ⚠️ 还没实现（路线图 P3） |

## 5. 关键接口速查（团队模式）

- 认证：`POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`、`POST /api/auth/redeem`（用邀请码激活，免登录）
- 自助：`GET/POST /api/keys`、`DELETE /api/keys/{id}`（管自己的接入密钥）、`GET /api/processes`（看自己的机器）
- 管理员：`GET/POST /api/admin/accounts`、`POST /api/admin/accounts/{id}/invite`（重发邀请）、`POST /api/admin/accounts/{id}/status`（停用/启用）、`GET /api/admin/health`（只看聚合计数，看不到他人内容）
- 页面：`/`（主控制台）、`/admin.html`（管理员）、`/keys.html`（接入密钥/我的机器）、`/redeem.html`（邀请码激活）

---

## 6. 常见问题

- **改了 UI 线上还是旧色？** 多半是 Cloudflare 边缘缓存。现已用 `?v=<git SHA>` 自动版本化解决；普通刷新即可。旧的无参数 `/app.css` 会自然过期。
- **线上登录后是空的？** 见 §3 第 1 点——需要本地 `foreman app` 用 access key 接进来才有数据。
- **本地 `foreman app` 没弹窗？** 没装 `pywebview`（在 `.[client]` 里）。它会退回无界面模式，浏览器开提示的本地地址即可。
- **想在 worktree 里测改动却跑到旧代码？** 可编辑安装是从主仓库 `src` 导入的，要 `set PYTHONPATH=<worktree>/src` 才会用 worktree 的代码。

---

## 7. 已知缺口 / 可选增强

- **没有建管理员的 CLI**（首个 admin 只能手动写库，见 §1.3）。建议加 `foreman admin create-user --admin`。
- **公网无登录门**（§3 第 2 点），建议上 Cloudflare Access。
- `foreman token` 未实现（P3）。

> 想让我补上 `foreman admin create-user` 命令、或顺手把第一个管理员在服务器上建好，说一声即可。
