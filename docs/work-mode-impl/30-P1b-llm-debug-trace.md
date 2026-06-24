# P1b 子步（优先做）— LLM 请求/响应调试追踪落盘

> 日期：2026-06-24　|　对应设计书章节：§8C（核心）、§11（安全边界）、旁及 §8C.3↔§16 telemetry id 共用
> 分支：`codex/work-mode-design`　|　本文件：`docs/work-mode-impl/30-P1b-llm-debug-trace.md`
> 阶段标识：**P1b-trace**　|　依赖：**P1**（见 [§1 前置依赖](#1-前置依赖)）

> 跨阶段共享的常量表 / telemetry 字段 / 路径映射 / 术语，统一放在 [`90-conventions-and-glossary.md`](./90-conventions-and-glossary.md)；总体顺序见 [`00-OVERVIEW-AND-SEQUENCING.md`](./00-OVERVIEW-AND-SEQUENCING.md)；评审结论见 [`01-REVIEW-FINDINGS.md`](./01-REVIEW-FINDINGS.md)。本文件不重复背景，只讲本阶段怎么做。

---

## 0. 目标与产出

调优 work-mode 上下文（§8B）这件事，前提是**能看见每次喂给大模型的真实 payload**——完整 system + 全部 message + 工具 schema，以及完整返回（文本 / tool_calls）。在此之前一切都是拍脑袋。所以 P1b 内部 **trace 先于 context（§8B）**：先把"问/答原文"落盘可重放，后面每一层压缩/预算调优才有据可查。

**本阶段交付：**

1. 给 `LLMClient` 注入一个**可选** `tracer`（`None` = 关，零开销），在两个公开方法 `complete()` / `tool_complete()` 进/出处记录请求/响应/计时。
2. 用 `contextvars.ContextVar` 在调用边界 set `{session_id, task_id, phase}`，让 tracer 不改一堆函数签名就能给每条记录打上关联 id。
3. 每次调用产一条 **JSONL**，落到 `.foreman/debug/llm-trace-<session_id>.jsonl`，含完整 request/response + metrics + ids。
4. 开关：config `debug.llm_trace` 或 env `FOREMAN_DEBUG_LLM_TRACE`，**默认关**。
5. 安全：本地 only、不进 git、key 不入、大小轮转、UI 明示"完整对话明文落盘"。

**本阶段定义之完成（一句话）：** debug 开关打开后，跑一次普通任务，能在 `.foreman/debug/` 拿到一份逐条 JSONL，按 `seq` 单调、每条含 `phase`/`session_id`/`task_id` 且能与 §16 `work_mode` telemetry 事件按 ids 对上，文件里搜不到任何 api key；debug 关时零落盘、`LLMClient` 行为与今天逐字节一致。

---

## 1. 前置依赖

| 依赖 | 说明 | 链接 |
|---|---|---|
| **P1** | trace 的价值在于"看 PM tool-loop 的真实 payload"——P1 把 tool-loop（`work_mode_search/get`）跑通后才有有意义的 payload 可看；trace 的 `seq`/ids 须与 **P1 落地的 `work_mode` telemetry 事件**共用同一套 id 来源（§8C.3↔§16）。 | [`20-P1-L1-retrieval-budget-telemetry.md`](./20-P1-L1-retrieval-budget-telemetry.md) |
| **底层 LLMClient 基线** | 已具备：`complete()`(`client.py:169`) 与 `tool_complete()`(`client.py:191`) 是全部 PM 大脑路径的两个 choke point，已是 provider 无关、已有可选注入参数（`transport`/`ws_connect`/`settings_resolver`）的 DI 风格。**无须 P0/P1 代码改动即可下手 client 部分**，但 telemetry id 约定要与 P1 对齐定稿。 | 见 [§2](#2-涉及文件与现状) |

**进入本阶段时假定的代码状态：**
- 仓库中**零** `contextvars` / `ContextVar` / `tracer`（已 grep 确认，全新接缝，无冲突）。
- `Config` 模型**没有** `debug` 段，没有 `DebugCfg`（全新结构，见 [任务 1](#任务-1新增-debugcfg-config-段--envconfig-glue先做)）。
- `complete()` / `tool_complete()` 都**不收** `session_id` / `task_id` / `phase`，所以关联只能靠 contextvar，不能 thread 参数（否则要改 8 个调用方签名）。

> ⚠️ 顺序硬约束：**先做 [任务 1](#任务-1新增-debugcfg-config-段--envconfig-glue先做)（config 管线）→ 再做 tracer + contextvar + JSONL sink → 最后接 8 个 set-point + UI 文案**。config 段不是"沿用现有 pydantic settings 就自动生效"的——见任务 1 的坑。

---

## 2. 涉及文件与现状

> file:line 均为本分支 HEAD（== 基线 `1801128`）亲自核对结果。设计书与实际有出入处就地标注。

| 文件 | 真实 file:line | 当前行为 / 为什么相关 |
|---|---|---|
| `shared/llm/client.py` | `90-115` `__init__` | DI 风格：已接受 keyword-only 可选 `transport` / `ws_connect` / `settings_resolver`（默认 `None`）。**加 `tracer=None` 完全照此模式**，blast radius 极小。 |
| 同上 | `169-189` `complete()` | `async def complete(self, messages, *, json_mode=False, model='', on_stream=None, state_key='')`。182 行 `_resolve()` 出 provider/base_url/model，183 ws / 187 anthropic / 189 openai 分支。**流式路径也返回最终累积文本**（openai 在 335 行 `''.join(buf)`，ws 在 488），所以 tracer 记返回值即可拿到最终文本，无需 hook 每个 chunk。 |
| 同上 | `191-211` `tool_complete()` | `-> LLMToolResponse`（`text` + `tool_calls`，dataclass 在 42-45）。**非流式**（无 `on_stream`），返回即完整。ws 路径（206-208）**内部再调 `self.complete()`** → 重入坑（见 [任务 3](#任务-3给-llmclient-加可选-tracer防重入)）。 |
| 同上 | `117-130` `_resolve()` / `145-155` `_transport_mode()` | tracer 要记的 `provider`/`model`/`transport` 在这里**按请求**算（settings_resolver 可运行时覆盖）。**必须记 `_resolve()` 输出，不是 `cfg.llm.*`**——否则 UI 改过 provider 的请求会被记错（设计书 §8C.2 的 record 没点出这点）。 |
| 同上 | `132-143` `_api_key()`；headers 在 `259/287/317/349/373/451` | key 只在私有 transport 方法里拼进 HTTP 头（`Authorization`/`x-api-key`），**从不在 `messages`/`tools` 参数里**。所以 message 层 tracer 天然不含 key（§8C.5 的兜底 redactor 是 nice-to-have，不是正确性必需）。 |
| `shared/config.py` | `16-19` `Secrets(BaseSettings)`；`226-243` `Config(BaseModel)` | **只有** `Secrets` 是 env 驱动（`env_prefix='FOREMAN_'`）。`Config` 及其全部结构段是 config.yaml 加载的纯 `BaseModel`，**无 env 绑定，无 `debug` 字段**。⚠️ 设计书 §8C.4 称"沿用现有 FOREMAN_ pydantic settings"——只对 `Secrets` 成立；`debug` 段 + env→config glue 全新（[任务 1](#任务-1新增-debugcfg-config-段--envconfig-glue先做)）。 |
| 同上 | `253-265` `load_config()` | 读 yaml → `Config(**data)` → 末尾从 .env 灌 `Secrets`。env→config glue 要插这里。 |
| `client/local_app.py` | `130-131` `_llm()` 工厂 | `return LLMClient(cfg, settings_resolver=_llm_settings)`。**唯一构造点**，但 `_llm()` 被调 4 次（134 auditor / 139 operator / 164 pm_agent / 175 briefing），各产一个独立 client。tracer 要做成**单例**，构造一次后传进每个 client（[任务 4](#任务-4在-local_app-接线-tracer单例)）。 |
| `client/core/dispatch_service.py` | `486-495` `_pm_launch(session_id, task_id, …)` | 普通 PM 派发主入口，**session_id+task_id 都在作用域内**。计划在 527 行调 `pm_agent.plan`，565 行调 `pm_agent.review`（在 546 起的 while 循环里）。**这是设 plan + review-N contextvar 的天然边界**。 |
| 同上 | `232-249` `compact(session_id)` | 独立 API 方法（**不在 `_pm_launch` 循环内**）。只有 `session_id`，无 task_id。249 行调 `pm_agent.compact`。set-point 在此。 |
| `client/core/pm_agent.py` | `478-489` 构造并跑 `PMToolLoop`（生产 tool-loop 路径） | tool-loop 经 `build_plan_prompt`(459) + `build_tool_prompt_context`(471-477) 拼 prompt，再 `loop.run([system,user])`。**实际 LLM 调用在 loop 内**（见下）。 |
| 同上 | `528` plan-fallback / `593` review / `607` compact | 三处 `await self.llm.complete(...)`。设计书 §8C.1 称"PM plan/review/compact 三 phase"，对应这三行 + tool-loop。 |
| `client/tools/loop.py` | `152-168` `_complete()`；`154` `tool_complete` / `167` `complete` | tool-loop **每轮**(`run` 52 行 `for round_no`) 调 `_complete`。有 native-tool 走 `tool_complete`、ws/无 tool 走 `complete(json_mode=True)` 两条路。**`phase=tool-round-N` 的 contextvar 要设在这里**（每轮），不是外层 loop，否则每轮归属不清。 |
| `client/core/operator.py` | `212` | `await self.llm.complete(...)`。Decision-loop 的 operator，**作用域内无 session/task**（参数只有 goal/agent_output…），故其 trace 关联 id 默认 null——是预期行为，不是 bug。 |
| `client/core/auditor.py` | `305` | `await self.llm.complete(...)`。同上，关联 id 可能为 null。 |
| `client/core/briefing.py` | `236` | `await self.llm.complete(...)`。简报路径。 |
| `client/core/supervisor.py` | `459` | `out = await self.llm.complete(...)`。watchdog/judge 路径。 |
| `client/core/reviewer.py` | `275` | `raw = await self.llm.complete(...)`。设计书 §8C.1 未单列，但同样过 choke point，会被自动记到（review-类 phase）。 |
| `server/logbuffer.py` | `20-26` `RingBufferHandler`；`70-97` 单例 | **对照物**：进程内存环（`deque(maxlen=500)`），只存 logging 文本 `{ts,level,logger,msg}`，**从不存 payload/secret**，重启即丢，且**活在 server 进程**。LLM trace 与它**无任何复用**：不同进程（trace 在本地 client）、不同 sink（磁盘 JSONL）、不同敏感度（§8C.6）。 |

**8 个 set-point 全核对清单**（缺任一，其 trace 的 `phase`/`session_id` 为 null，无法与 telemetry 对账）：

| # | phase 值 | 设 contextvar 的位置（建议） | LLMClient 落点 |
|---|---|---|---|
| 1 | `plan` | `dispatch_service._pm_launch` 调 `pm_agent.plan` 前（≈527） | tool-loop 路径 `loop.py:154/167`；fallback `pm_agent.py:528` |
| 2 | `review-N` | `_pm_launch` while 循环内调 `pm_agent.review` 前（≈565），N=`run_count` | `pm_agent.py:593` |
| 3 | `compact` | `dispatch_service.compact` 调 `pm_agent.compact` 前（≈249） | `pm_agent.py:607` |
| 4 | `tool-round-N` | `loop.py:_complete`（152）内，N=`round_no` | `loop.py:154`(native) / `167`(ws/fallback) |
| 5 | `operator` | `operator.py` 调用边界（无 session/task → 仅 phase） | `operator.py:212` |
| 6 | `auditor` | `auditor.py` 调用边界 | `auditor.py:305` |
| 7 | `briefing` | `briefing.py` 调用边界 | `briefing.py:236` |
| 8 | `supervisor` | `supervisor.py` 调用边界 | `supervisor.py:459` |

> `reviewer.py:275` 会被自动记录（同一 choke point）；它的 phase 取决于上层 set 的值（通常 review 链路）。enumerate 时 5-8 因调用方手里没有 session/task，可只 set `phase`，关联 id 走默认 null。

---

## 3. 开发任务（有序、可勾选）

### 任务 1：新增 `DebugCfg` config 段 + env→config glue（先做）

**改哪：** `shared/config.py`
**为什么：** 设计书 §8C.4 假设开关"沿用现有 FOREMAN_ pydantic settings"，但只有 `Secrets(BaseSettings)` 是 env 驱动；结构化 `Config` 段是纯 `BaseModel`，env 不会自动生效。必须自己接。

- [ ] 1.1 加 `DebugCfg(BaseModel)`（仿 `PMToolsCfg` 风格，config.py:179-188）：

```python
class DebugCfg(BaseModel):
    # LLM 请求/响应明文落盘开关。默认 False。开启会把"完整对话明文"写到 log_dir（§8C.5）。
    llm_trace: bool = False
    # trace 落盘根目录（进程本地，永不上传/提交）。相对路径相对 config 所在目录解析。
    log_dir: str = ".foreman/debug"
    # 【默认 2026-06-24】单 trace 文件字节上限 = 50 MB，超过即轮转为 .1/.2…（§8C.5 大小可控）。可 config 覆盖。
    llm_trace_max_bytes: int = 50 * 1024 * 1024  # 50 MB
    # 【默认 2026-06-24】保留的轮转文件数（含当前）= 最近 20 个，超出删最旧。可 config 覆盖。
    llm_trace_keep: int = 20
    # 【默认 2026-06-24】轮转文件保留天数 = 14 天，更旧的删除。与 llm_trace_keep（20 个）取"先到先汰"——
    # 即文件数超 20 或文件超 14 天，先满足哪个就先汰哪个。可 config 覆盖。
    llm_trace_keep_days: int = 14
```

- [ ] 1.2 在 `Config`（226-243）加字段：`debug: DebugCfg = DebugCfg()`。
- [ ] 1.3 **env→config glue**（`load_config`，253-265 末尾）。`FOREMAN_DEBUG_LLM_TRACE` 不会自动绑定到 `Config.debug`（只有 `Secrets` 吃 env_prefix）。显式读：

```python
import os
# ...在 load_config 末尾、return cfg 之前：
_env_trace = os.environ.get("FOREMAN_DEBUG_LLM_TRACE")
if _env_trace is not None:
    cfg.debug.llm_trace = _env_trace.strip().lower() in {"1", "true", "yes", "on"}
```

> 决策：env 只覆盖**开关**这一个布尔，足够"临时开一次"。`log_dir`/轮转参数仍走 config.yaml。env 优先于 yaml（"我现在就要开"语义）。

**接缝：** `DebugCfg` 是新段，不动任何现有段；`load_config` 已有"末尾灌 Secrets"的尾段，glue 紧随其后。

---

### 任务 2：新建 `LLMTracer`（JSONL sink + 轮转 + redactor）

**改哪：** 新文件，建议 `shared/llm/trace.py`（与 `client.py` 同包，client 直接 import）。
**为什么：** tracer 是独立可注入的协作者；逻辑（关联读取 / 序列化 / 落盘 / 轮转 / redact）不该塞进 `client.py`。

- [ ] 2.1 定义关联 contextvar（**全仓唯一来源**，与 §16 telemetry 共用同一变量，避免两套对不上）：

```python
import contextvars
# {session_id, task_id, phase, seq_base?} —— 设计书 §8C.3
_TRACE_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("foreman_llm_trace_ctx", default={})

def set_trace_context(*, session_id: str = "", task_id: str = "", phase: str = ""):
    """在调用边界 set，返回 token 供 reset（确保不串到无关协程）。"""
    return _TRACE_CTX.set({"session_id": session_id, "task_id": task_id, "phase": phase})

def reset_trace_context(token) -> None:
    _TRACE_CTX.reset(token)

def current_trace_context() -> dict:
    return _TRACE_CTX.get()
```

> ⚠️ **必须用 token + reset**（或 `contextvars.copy_context()`），不要裸 `set` 不还原——并发任务/复用事件循环时会串味。建议把 set/reset 包成 `@contextmanager` 或 `async with`，调用方一行用。

- [ ] 2.2 `LLMTracer` 类，构造期决定文件 sink，提供一个 `record(...)` 入口：

```python
class LLMTracer:
    def __init__(self, *, log_dir: Path, max_bytes: int, keep: int, keep_days: int):
        self._dir = Path(log_dir); self._dir.mkdir(parents=True, exist_ok=True)
        # 【默认 2026-06-24】max_bytes=50 MB / keep=20 个 / keep_days=14 天，先到先汰；均可 config 覆盖。
        self._max_bytes = max_bytes; self._keep = keep; self._keep_days = keep_days
        self._seq = itertools.count(1)   # 进程内单调 seq（§8C.2 / §14"seq 单调"）

    def record(self, *, kind: str, provider: str, model: str, transport: str,
               json_mode: bool, messages, tools, response_text: str,
               tool_calls, latency_ms: float, error: str | None) -> None:
        ctx = current_trace_context()
        rec = {
            "ts": _utc_iso(), "seq": next(self._seq),
            "session_id": ctx.get("session_id", ""), "task_id": ctx.get("task_id", ""),
            "phase": ctx.get("phase", ""),
            "provider": provider, "model": model, "transport": transport,
            "json_mode": json_mode,
            "request":  {"messages": [{"role": m.role, "content": m.content} for m in messages],
                         "tools": tools or []},
            "response": {"text": response_text, "tool_calls": tool_calls or []},
            "metrics":  _metrics(messages, response_text, latency_ms),
            "error": error,
        }
        self._write(self._path_for(ctx.get("session_id", "")), _redact(rec))
```

  记录字段映射到设计书 §8C.2 的 record 形状（见 [`90-conventions`](./90-conventions-and-glossary.md) 的 trace schema）。注意：
  - `request.messages` 直接展开 `Message`(role+content)；不另起序列化模型。
  - `request.tools` 就是传给 `tool_complete` 的 raw `list[dict]`（provider-native schema）；`complete()` 无 tools → 空 list。
  - `response.tool_calls` 来自 `LLMToolResponse.tool_calls`（`complete()` 无 → 空 list）。
  - `metrics`：`req_chars`/`resp_chars` 实算；`approx_*_tokens` 用 `chars/4` 近似即可（与预算器同口径，见 §8B/附录）；`latency_ms` 在 client 内计时。

- [ ] 2.3 文件名 `llm-trace-<session_id>.jsonl`；`session_id` 为空（operator/auditor 等无会话路径）落 `llm-trace-_no-session.jsonl`。**append-only**。
- [ ] 2.4 **轮转/保留**（【默认 2026-06-24】单文件 ≤50 MB、保留最近 20 个文件或 14 天，先到先汰；均可 config 覆盖）：`_write` 前检查当前文件 size，超 `max_bytes`（默认 50 MB）即把 `name.jsonl`→`name.jsonl.1`、`.1`→`.2`…；保留按"先到先汰"——文件数超 `keep`（默认 20）删最旧、文件 mtime 超 `keep_days`（默认 14 天）也删（仓库无现成 rotation helper，从零写）。
- [ ] 2.5 **redactor**（兜底，§8C.5）：序列化后正则擦 `sk-[A-Za-z0-9]{8,}`、`Bearer\s+\S+`、`x-api-key`-style token。即使 settings_resolver 的 `api_key` 覆盖路径（`client.py:139-140`）也擦得到——但因 key 从不进 `messages`/`tools`，正常路径本就不会出现，这是双保险。
- [ ] 2.6 tracer 内**绝不抛进调用方**：`record`/`_write` 整个 try/except 吞掉（仿 `logbuffer.emit` 的"日志器永不上抛"约定，logbuffer.py:45）。trace 坏了不能拖垮真实 LLM 调用。

**接缝：** 不依赖 `client.py` 之外任何东西；`trace.py` 只 import stdlib + `Message`/`LLMToolCall` 类型。

---

### 任务 3：给 `LLMClient` 加可选 `tracer`（防重入）

**改哪：** `shared/llm/client.py`
**为什么：** 两个 choke point 是 trace 的唯一挂载边界，一次覆盖全 provider。

- [ ] 3.1 `__init__`（90-98）加 `tracer=None`（keyword-only，照 `transport`/`ws_connect` 模式），存 `self._tracer = tracer`。
- [ ] 3.2 包 `complete()`（169）：方法体外加 try/计时/记录。骨架：

```python
async def complete(self, messages, *, json_mode=False, model="", on_stream=None, state_key=""):
    provider, base_url, model_eff = self._resolve(model)
    transport = self._transport_mode()
    if self._tracer is None:
        return await self._complete_impl(messages, json_mode, model, on_stream, state_key,
                                         provider, base_url, model_eff, transport)
    t0 = time.perf_counter(); err = None; text = ""
    try:
        text = await self._complete_impl(...); return text
    except Exception as e:        # 记下错误也要落 trace
        err = repr(e); raise
    finally:
        self._tracer.record(kind="complete", provider=provider, model=model_eff,
                            transport=transport, json_mode=json_mode, messages=messages,
                            tools=None, response_text=text, tool_calls=None,
                            latency_ms=(time.perf_counter()-t0)*1000, error=err)
```

  （把现有 182-189 的 dispatch 抽进 `_complete_impl`，或更简单：在现有方法首尾包，不抽函数——只要保证"记 `_resolve()`/`_transport_mode()` 的结果，不是 cfg.llm"。）

- [ ] 3.3 **防重入（关键坑，§8C.1 / 评审 blocker 簇）**：ws 路径 `tool_complete()`（206-208）**内部再调 `self.complete()`**。若两个公开方法都朴素包 tracer，一次 ws `tool_complete` 会 emit **两条**（外层 tool_complete + 内层 complete），使 `seq`/token 在 ws 路径虚高。
  - 方案（推荐）：tracer 内部用一个 contextvar 标志 `_in_trace`，进入任一公开方法时若已 `True` 就跳过记录（只记最外层）。
  - 或：`tool_complete` ws 分支调内层时用 `self._complete_impl(...)`（绕过被包的公开 `complete`），即内层不经 tracer。
  - [ ] 选其一并在测试里断言"一次 ws tool_complete 只产一条 trace"。
- [ ] 3.4 包 `tool_complete()`（191）：同 3.2，`kind="tool_complete"`，记 `tools` = 入参 tools、`tool_calls` = 返回的 `LLMToolResponse.tool_calls`。**非流式**，返回即完整文本（`.text`）。

**接缝：** 不改 `complete()`/`tool_complete()` 的签名与返回类型 → 8 个调用方零改动。tracer=None 时除一个 `if` 外**零开销、行为逐字节不变**（§14"debug 关时零落盘、零开销"）。

---

### 任务 4：在 `local_app` 接线 tracer（单例）

**改哪：** `client/local_app.py`（≈130）
**为什么：** `_llm()`（130-131）被调 4 次产 4 个 client，tracer 必须是**同一个**（seq 才全局单调、文件才统一）。

- [ ] 4.1 构造**一次** tracer（仅当开关开），传进每个 client：

```python
from foreman.shared.llm.trace import LLMTracer
_tracer = None
if cfg.debug.llm_trace:
    _log_dir = Path(cfg.config_path or ".").parent / cfg.debug.log_dir \
        if not Path(cfg.debug.log_dir).is_absolute() else Path(cfg.debug.log_dir)
    _tracer = LLMTracer(log_dir=_log_dir, max_bytes=cfg.debug.llm_trace_max_bytes,
                        keep=cfg.debug.llm_trace_keep, keep_days=cfg.debug.llm_trace_keep_days)

def _llm() -> LLMClient:
    return LLMClient(cfg, settings_resolver=_llm_settings, tracer=_tracer)
```

> `log_dir` 相对路径相对 **config 所在目录**解析（与 client store db 同根），不是当前 cwd——避免 trace 散落到随机目录。`.foreman/debug/` 是**进程本地状态目录**，与 `injector.py` 写进**目标 workspace** 的 `.foreman/skills` 不是同一个 `.foreman`（P2 的注入物在被改的仓库里，trace 在 Foreman 自己的工作目录里）。

- [ ] 4.2 **git 排除（§8C.5 / §11）**：Foreman 仓库 `.gitignore` 当前**没有** `.foreman/`（已核对）。若 trace 落在 Foreman 进程目录且该目录是 git 仓库，需保证不被提交。两条路：
  - 在 Foreman 仓库 `.gitignore` 追加 `.foreman/`（最简单）；**且/或**
  - `LLMTracer` 构造时若发现 `log_dir` 在某 git 仓库内，自动写 `.git/info/exclude`（与 P2 的 `.git/info/exclude` 注入同思路，但 P2 是针对目标 workspace）。
  - [ ] 选定并在 §14"写在 `.foreman/debug/` 且被 git 排除"测试里断言。

---

### 任务 5：8 个 set-point 接 contextvar

**为什么：** 没有 set-point，每条 trace 的 ids/phase 全 null，无法对账（§8C.3 / 评审 major）。用 [§2 的清单](#2-涉及文件与现状)逐个接，**别只接 plan/review 两个**。

- [ ] 5.1 `dispatch_service._pm_launch`（≈527 plan / ≈565 review）：
  - plan 前 `with set_trace_context(session_id=session_id, task_id=task_id, phase="plan"):` 包住 `pm_agent.plan`。
  - review 前同理 `phase=f"review-{run_count}"`。
  - 建议用 `@contextmanager` 包成 `_trace_phase(...)` 局部 helper，少写 token/reset。
- [ ] 5.2 `dispatch_service.compact`（≈249）：`phase="compact"`，`session_id=session_id`，task_id 空。
- [ ] 5.3 `loop.py:_complete`（152）：**每轮**进入时 set `phase=f"tool-round-{round_no}"`（`round_no` 从 `run` 的 `for` 传进 `_complete`，或在 `run` 里包住每次 `_complete` 调用）。session/task 沿用上层 plan 已 set 的 ctx（contextvar 会被内层继承，只覆盖 phase 即可——注意继承时保留外层 session_id/task_id，别清掉）。
  > 实现细节：`set_trace_context` 若清掉 session/task 会丢关联。tool-round 的 set 应**只改 phase、保留已有 session/task**——提供一个 `set_phase_only(phase)` 变体，或 set 时从 `current_trace_context()` 取出 session/task 一并带上。
- [ ] 5.4 `operator.py:212` / `auditor.py:305` / `briefing.py:236` / `supervisor.py:459`：在各自调用边界 set `phase`（`operator`/`auditor`/`briefing`/`supervisor`）。这些路径手里**没有** session/task，关联 id 留空——预期行为。
- [ ] 5.5 **telemetry id 共用定稿（与 P1 对齐）**：trace 的 `session_id`/`task_id`/`seq` 与 §16 `work_mode` 事件用**同一 contextvar 来源**。在 [`90-conventions`](./90-conventions-and-glossary.md) 把"trace ids 字段 ↔ telemetry 事件字段"的对应表定稿一处，P1 与本阶段都引它，避免各定义一套对不上。

---

### 任务 6：UI debug 开关 + 明示文案（§8C.4 / §11）

**改哪：** 设置页（personal 入口 `server/web/app.js` + i18n；具体控件归属随 P0/P1 的设置页改造，本阶段只确保"开关存在且有警示文案"）。
**为什么：** §11 / §8C.5 要求 UI 明示"会把完整对话内容明文落盘"。

- [ ] 6.1 设置页加 `debug.llm_trace` 开关（写入 config_kv / 触发重建 tracer，或提示重启生效——取最简）。
- [ ] 6.2 开关旁**显式警示**（zh/en 成对加 i18n 键）：「开启后会把与大模型的完整对话（含源码与解密后的工作方式）明文写入本机 `.foreman/debug/`，仅本地保存、不上传、不进 git。」
- [ ] 6.3 default off，关掉后新派发不再落盘。

> 若设置页重建 tracer 复杂，可接受"改 config 后下次启动生效"作为 P1b-trace 的初版，UI 文案与开关本体必须有。

---

## 4. 验收标准

> 仅摘 §14/§15 与本阶段相关条目，改写成可勾选验收点。

- [ ] **AC-1（落盘内容）** debug 开时，每次 `complete` / `tool_complete` 产**一条** JSONL，含完整 `request`（system+全部 message+tools schema）、完整 `response`（text 或 tool_calls）、`phase`、`session_id`/`task_id`、`metrics`、`error`。（§14"调试追踪"①）
- [ ] **AC-2（零开销）** debug 关时 **零落盘、零开销**：`tracer=None`，`complete`/`tool_complete` 行为与基线逐字节一致（除一个 `if None` 分支）。（§14①）
- [ ] **AC-3（无 key）** trace 文件**不含** api key。**注入伪造头/伪造 `api_key` 覆盖**后断言文件里搜不到该 key、搜不到 `Bearer `/`sk-`。（§14"trace 文件不含 api key"）
- [ ] **AC-4（位置 + git 排除）** trace 写在 `debug.log_dir`（默认 `.foreman/debug/`）下 `llm-trace-<session_id>.jsonl`，且该路径被 git 排除（`git status` 不显示、`git check-ignore` 命中）。（§14"写在 `.foreman/debug/` 且被 git 排除"）
- [ ] **AC-5（seq 单调 + 可对账）** 同一会话多次调用 `seq` **进程内单调递增**；按 `session_id`/`task_id` 能与 §16 `work_mode` telemetry 事件**对上**。（§14"`seq` 单调、可与 telemetry 按 ids 对上"）
- [ ] **AC-6（ws 不双记）** 一次 ws `tool_complete` 只产**一条** trace（内层 `complete` 不另记）。（评审 blocker：ws 重入）
- [ ] **AC-7（流式记最终文本）** openai/ws 流式 `complete` 的 `response.text` 是**最终累积文本**（不是逐 chunk 片段）。
- [ ] **AC-8（phase 正确）** tool-loop 多轮跑出的 trace，`phase` 为 `tool-round-1`…`tool-round-N`（按轮递增），plan/review-N/compact 各 phase 正确；operator/auditor/briefing/supervisor 的 trace `phase` 正确、关联 id 允许为空。
- [ ] **AC-9（轮转/保留）** 单文件超 `llm_trace_max_bytes`（【默认 2026-06-24】50 MB）时轮转；保留按"先到先汰"——文件数不超 `llm_trace_keep`（【默认 2026-06-24】20 个）、且不保留超 `llm_trace_keep_days`（【默认 2026-06-24】14 天）的旧文件，超限删最旧。（数值均可 config 覆盖）
- [ ] **AC-10（provider 真实值）** trace 的 `provider`/`model`/`transport` 记的是 `_resolve()`/`_transport_mode()` 的**按请求结果**（settings 页覆盖后跟随覆盖值），不是构造期 `cfg.llm`。

---

## 5. 测试

> 集成测试必须打 **tool-loop 真实路径**（带 `tool_runtime_factory` 的 `PMAgent` → `PMToolLoop.run`），**不允许只测 `build_plan_prompt`**（§14 硬要求）。

### 单元

- [ ] **U-1 tracer off = no-op**：构造 `LLMClient(cfg, tracer=None)`，mock transport 跑 `complete`/`tool_complete`，断言无文件、无副作用、返回值与无 tracer 一致。
- [ ] **U-2 一调一条**：`tracer` 注入 fake sink（收集 record 到 list），跑一次 `complete`，断言恰好一条、字段齐全（含 `seq=1`、`phase`/ids 来自 contextvar）。
- [ ] **U-3 ws 防重入**：mock `ws_connect` 走 ws 分支，跑一次 `tool_complete`，断言 sink 只收**一条**（kind=`tool_complete`），内层 `complete` 未另记。
- [ ] **U-4 key 不泄露**：把 `settings_resolver` 设成返回 `api_key="sk-FAKE123456789"`，跑请求后 dump record 文本，断言不含 `sk-FAKE…`、不含 `Bearer`；再人为往 `Message.content` 塞一个 `sk-xxx` 验 redactor 也擦掉（兜底）。
- [ ] **U-5 provider/model 按请求**：settings_resolver 覆盖 `provider/model`，断言 record 里是覆盖值而非 `cfg.llm.*`。
- [ ] **U-6 流式最终文本**：mock openai stream 多 chunk，断言 `response.text == ''.join(chunks)`。
- [ ] **U-7 contextvar 隔离**：两个并发协程各 `set_trace_context` 不同 session_id，断言各自 record 带对的 id（验 token/reset 没串）。
- [ ] **U-8 轮转/保留**：`max_bytes` 设很小，写多条，断言产生 `.1`/`.2`、`keep`（默认 20）生效删最旧；再把若干轮转文件 mtime 改到 `keep_days`（默认 14 天）之前，断言被按"先到先汰"清掉。
- [ ] **U-9 seq 单调**：连续多次调用，断言 `seq` 1,2,3… 单调。
- [ ] **U-10 config glue**：`monkeypatch.setenv("FOREMAN_DEBUG_LLM_TRACE","1")` 后 `load_config()`，断言 `cfg.debug.llm_trace is True`；env 缺省时跟 yaml。

### 集成（打 tool-loop 真实路径）

- [ ] **I-1 端到端 tool-loop trace**：用带 `tool_runtime_factory` 的 `PMAgent`（线上路径，mock LLMClient 返回先 `work_mode_get` tool_call 再 `final_plan`），经 `dispatch_service._pm_launch`（set plan/tool-round/review contextvar）跑完一次派发；断言：
  - 产出 `.jsonl` 含 `phase=plan`、`phase=tool-round-1`（及更多轮）、`phase=review-1`；
  - 每条带正确 `session_id`/`task_id`；
  - tool-round 的 record `request.tools` 含 `work_mode_get` schema（验证记的是**实际发给 LLM 的入参**，不是 `build_plan_prompt` 字符串）。
- [ ] **I-2 trace ↔ telemetry 对账**：同一次派发，断言 trace 的 `session_id`/`task_id` 与 P1 落地的 `work_mode` telemetry 事件 ids **能 join 上**（AC-5）。
- [ ] **I-3 compact phase**：调 `dispatch_service.compact(session_id)`，断言产出含 `phase=compact` 的一条。

---

## 6. 风险与回滚

| 风险 / 坑 | 触发 | 缓解 | 回滚 |
|---|---|---|---|
| **ws 双记**（评审 blocker 簇） | tracer 朴素包两个公开方法，ws `tool_complete` 内调 `complete` → 两条 | 任务 3.3 防重入（`_in_trace` 标志或绕过公开 `complete`）；U-3 断言 | tracer=None 即整体关闭 |
| **关联 id 全 null** | 漏接 8 个 set-point 之一 | 任务 5 按清单逐个接 + I-1 断言各 phase 出现 | 缺的 set-point 仅影响该路径关联，trace 仍落盘（degrade 不崩） |
| **contextvar 串味** | 裸 `set` 不 reset，复用事件循环/并发任务 | token+reset 或 `@contextmanager`；U-7 并发断言 | — |
| **key 泄露** | 误把头/key 记进 payload | message 层天然无 key（已核 client.py headers 在私有方法）+ 兜底 redactor；AC-3/U-4 | 关 debug |
| **磁盘爆**（数十 MB/长会话，§8C.5） | 长任务无轮转 | `max_bytes`+`keep`+`keep_days` 轮转/保留（任务 2.4；【默认 2026-06-24】50 MB / 20 个 / 14 天先到先汰）；AC-9/U-8 | 删 `.foreman/debug/`；关 debug |
| **trace 异常拖垮真实调用** | sink 写盘失败上抛 | tracer 全程 try/except 吞（任务 2.6，仿 logbuffer.emit） | — |
| **误提交敏感 trace** | `.foreman/` 不在 `.gitignore`（已核对） | 任务 4.2 加 `.gitignore`/`.git/info/exclude`；AC-4 | 立即 `git rm --cached` + 加 ignore |
| **config env 不生效**（§8C.4 坑） | 以为 `FOREMAN_DEBUG_LLM_TRACE` 自动绑定 | 任务 1.3 显式 glue；U-10 | — |
| **记成 cfg.llm 而非 _resolve**（设计书未点出） | 用 settings 页覆盖 provider 的请求被记错 | 任务 3.2/AC-10 记 `_resolve()` 输出 | — |

**整体回滚极简**：tracer 是可选注入（`None`=关）+ 新增独立文件 + 新增 config 段，不改任何现有签名/返回类型。回滚 = `cfg.debug.llm_trace=false`（运行时关）或 revert 本阶段 commit（client `__init__` 去掉 `tracer` 参数即恢复基线行为）。

---

## 7. 与设计书 / 其它阶段的对应

**映射到设计书章节：**
- §8C.0 目标 → [§0](#0-目标与产出)
- §8C.1 choke point（`complete`/`tool_complete`，client.py:169/191）+ 流式记最终文本 + 可选 tracer → [任务 2/3](#任务-2新建-llmtracerjsonl-sink--轮转--redactor)，AC-1/6/7
- §8C.2 JSONL record 形状 → [任务 2.2](#任务-2新建-llmtracerjsonl-sink--轮转--redactor) + [`90-conventions` trace schema](./90-conventions-and-glossary.md)
- §8C.3 contextvar 关联 + 与 §16 telemetry 共用 ids → [任务 5](#任务-58-个-set-point-接-contextvar)，AC-5/8，I-2
- §8C.4 开关与默认（config/env，默认关，`.foreman/debug/`，按 session 一文件） → [任务 1/4/6](#任务-1新增-debugcfg-config-段--envconfig-glue先做)，AC-2/4
- §8C.5 安全（本地 only / 不进 git / key 不入 / 大小轮转 / UI 明示） → [任务 2.4-2.5/4.2/6](#任务-2新建-llmtracerjsonl-sink--轮转--redactor)，AC-3/4/9
- §8C.6 与 logbuffer/event store 的区别 → [§2 logbuffer 行](#2-涉及文件与现状)（已确认不可复用）
- §11 untrusted/敏感数据边界 → [任务 6 文案](#任务-6ui-debug-开关--明示文案-8c4--11)，AC-3/4

**下游依赖本阶段的步骤：**
- [`31-P1b-unified-context-compression.md`](./31-P1b-unified-context-compression.md)（P1b-context，§8B）——设计书明言 trace "优先做——后续每一层调优都靠它看真实 payload"。context 压缩/预算调优的 A/B 验证、"哪段上下文产了坏计划"的定位，都靠本阶段的 trace。
- 凡后续对 system/上下文形状有改动的阶段（P2 文件注入、P4 硬执行 review 形状变化），都可用本阶段 trace 复盘真实 payload。

**与 P1 的对齐点（必须定稿一处）：** trace 的 ids/seq ↔ §16 `work_mode` telemetry 事件字段，统一定义在 [`90-conventions-and-glossary.md`](./90-conventions-and-glossary.md)，P1 与本阶段共同引用。
