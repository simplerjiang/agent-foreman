# P5 — Workflow 控制流（V2：独立 API/UI + WorkflowEngine 接线 + 轻量 step 派发）

> 日期：2026-06-24
> 对应设计书章节：§10（Workflow 控制流 V2）、§13(P5)、并引用 §8B.5/§8B.6（步边界压缩）、§9（硬执行门）
> 分支：`codex/work-mode-design`（基线 HEAD == `1801128`）

---

## 0. 目标与产出

**一句话「本阶段定义之完成」**：把全仓库当前**零实例化**的 `WorkflowEngine`（+ `WorkflowQAReviewer`）真正接进运行时，配上一套**独立的 `POST /api/workflows/{start,begin,submit,resume}` API + 最小 UI 启动入口**，并让每个 step 用**轻量 step-instruction 派发**（PM 只把「步目标 + L0 索引」转成一条 coding instruction，不每步跑完整 PM tool-loop），从而让用户能**显式启动**一个 workflow 秘方、按固定骨架逐步执行、在审批门停住、QA/check 不过不进下一步。

完成后系统多了的能力：

- 产品层面真的能「跑工作流」——此前 `WorkflowEngine` 的状态机、per-step material 解析、approval gate、QA park/advance 全部写好了但**没有任何调用方**（设计书 §17 已诚实声明「workflow 未接 API/UI 前不能从产品运行」）。本阶段补上这条产品入口。
- 每个 step 复用 P0–P2 同一套渐进式披露：step 的 skills/standards/qa 经 `_resolve_material` 出 L0 索引 + 文件注入（P2 的 `WorkspaceInjector`），**而非整段灌入**。
- step 边界成为天然压缩点（§8B.6）：每步结束把该步时间线压成 `scope=workflow` 的 `MemoryItem`，多步 workflow 不随步数线性膨胀。

**本阶段不做**：embedding 检索（P3）、把 step 派发升级成完整 PM 多轮 review（明确反模式，见 §10「避免 ×步数 成本」）、workflow 的可视化编排画布（只做最小启动 + 状态查看 UI）。

---

## 1. 前置依赖

| 依赖 step | 为什么 | 文档 |
|---|---|---|
| **P0** | step 的 skill/standard/qa 的 L0 索引依赖 `metadata.description` 与 `resolve_work_mode_context()` 漏斗；step material 注入要带描述索引 | [`10-P0-copy-and-L0-metadata.md`](10-P0-copy-and-L0-metadata.md) |
| **P1** | 轻量 step 派发把 step 目标转 coding instruction 时复用 PM 通道理念；`scope=workflow` 压缩记忆 writer 与 work_mode telemetry 事件在 P1/P1b 定稿 | [`20-P1-L1-retrieval-budget-telemetry.md`](20-P1-L1-retrieval-budget-telemetry.md) |
| **P2** | **关键**：`WorkflowEngine.begin_step → _inject` / `submit_step 完成 → _clear` 调的是一个注入器（`self.injector`）。这个 `WorkspaceInjector` 的 `.claude/skills` 原生写法、`task_id` 并发隔离、`.git/info/exclude`、inject↔clear 成对，全部由 **P2** 建立。P5 复用 P2 的注入器，不重造 | [`40-P2-coding-agent-channel.md`](40-P2-coding-agent-channel.md) |
| 横切 | `scope=workflow` 的 `MemoryItem` writer 与 scope 常量化由 **P1b-context** 先建好，P5 复用 | [`31-P1b-unified-context-compression.md`](31-P1b-unified-context-compression.md) |

进入本阶段时假定的代码状态：

- `WorkflowEngine`（`workflow_engine.py`）状态机、`_resolve_material`、approval gate、QA park/advance、`_inject`/`_clear` 接缝**已全部存在且经单测覆盖**（`tests/test_workflow_engine.py`），但**没有任何运行时调用方**。
- `WorkflowQAReviewer`（`qa_review.py`）已存在但同样**零实例化**。
- `Store` 的 workflow_runs / definition_links 方法齐全（`db.py:558-647`）。
- P2 已把一个可用的 `WorkspaceInjector`（带 task_id 隔离 + 原生 SKILL.md）实例化并接到 `DispatchService` 的普通任务路径上。**P5 把同一个 injector 注入 `WorkflowEngine`。**

> 横切提醒：P5 是设计书 `recommended_order` 的**最后一步**（P0 → P1 → P1b-trace → P1b-context → P2 → P3 → P4 → P5）。在 P2 的注入生命周期与 P1b 的 `scope=workflow` 压缩记忆落地前，不要开 P5。

---

## 2. 涉及文件与现状

> 行号基于 HEAD `1801128`，已逐处亲自核对。凡设计书行号与真实不符均就地标注。

| 文件 | 真实 file:line | 当前行为 / 现状（核实） |
|---|---|---|
| `client/core/workflow_engine.py` | 全文 1-553 | `WorkflowEngine` 类（`L160`）、状态机、`_resolve_material`（`L241-276`）、`begin_step`（`L324-343`）、`_inject`（`L345-355`）、`submit_step`（`L370-408`）、`_open_gate`（`L410-425`）、`resume_after_gate`（`L449-464`）、`_advance`（`L466-478`）、`_clear`（`L480-490`）、`start`（`L208-238`）、`step_view`（`L314-322`）。**全仓零实例化（无 `WorkflowEngine(` 命中）。** |
| `client/core/qa_review.py` | 全文 1-163 | `WorkflowQAReviewer`（`L44`）、`review_step`（`L68-117`）。接 `engine.submit_step(run_id, qa_passed=...)`（`L109`）。**全仓零实例化。** |
| `client/local_app.py` | `L158-171`（`DispatchService` 构造）、`L181`（`DefinitionService`）、`L187-190`（`create_app`） | 全仓**唯一**实例化 `DispatchService`、`DefinitionService` 的地方。**没有** `WorkflowEngine(` / `WorkflowQAReviewer(`。`create_app(...)` 未传任何 workflow 服务。 |
| `client/core/dispatch_service.py` | `__init__` `L79-100`；`create` `L103-230`；`_pm_launch` `L486-598`；`runner.wait` 在 `L484/545/598`；`_pm_launch` 三个 `return` 出口在 `L581`（done）/`L588`（run 上限）/`L595`（空 follow-up） | PM 通道。`_pm_launch` 内 while 循环（`L546-598`）多次 `runner.wait`。**完全不引用 `injector`/`WorkflowEngine`。** 轻量 step 派发要么挂在此处新方法、要么新建一个 dispatcher 方法。 |
| `client/store/db.py` | `add_workflow_run` `L600`、`get_workflow_run` `L609`、`get_workflow_runs` `L613`、`update_workflow_run` `L624`；`get_definition_links` `L565`、`add_definition_link` `L558`、`get_active_definition` `L468`、`get_definitions` `L442` | workflow_runs / definition_links 全部 CRUD 就绪，无需改库。 |
| `client/store/models.py` | `WorkflowRun` `L218-227`；`DefinitionLink` `L208-215`；`Definition` `L194-205`；`MemoryItem` 的 `scope` 见 P1b 笔记 | `WorkflowRun.step_status ∈ {pending,running,qa,passed,failed,blocked}`（`L225`）。无需迁移。 |
| `server/app.py` | `create_app` 签名 `L426-440`；`app.state.*` 注入 `L459-471`；`_DispatchBody` `L180-189`；`/api/tasks` `L1375-1402`；`/api/definitions` 系列 `L1440-1545` | API 层全部 client-side 服务 INJECTED 进来（app.py 保持 shared-only，§14）。**无任何 `/api/workflows*` 路由、无 workflow 请求体。** definitions 路由是要照抄的 503-fallback + 错误码映射模式。 |
| `server/web/app.js` | i18n zh `L56-57`、en `L136-137`（已有 `kindWorkflow` 等键）；`Composer`/`runDispatch` `L919-952`；definitions 编辑器 `L1038/1251-1252/1558` | 已有 workflow definition 的**编辑** UI（增删改 workflow 秘方），但**没有任何「启动一次 workflow run」的入口**，也没有 run 状态查看面板。承载页是 `index.html` 加载的 `app.js`（personal 入口）；team 入口 `app.html`/`admin-app.js` 与本簇无关。 |
| `client/core/injector.py` | `WorkspaceInjector.inject` `L105-135`、`clear`（P2 改造后）；`_resolve_material` 产的 material 形状由 `inject` 消费 | P2 交付。P5 把 P2 实例化好的 injector 传给 `WorkflowEngine`。 |

---

## 3. 开发任务（有序、可勾选）

> 建议作为**一个 PR**：engine 接线 + API + 轻量 step 派发 + 最小 UI 是一条产品链，拆开会出现「engine 接了但没 API」「API 有了但 UI 进不去」的悬空态。但**测试可先于 UI**落（API/engine 单测 + 集成测试不依赖前端）。

### 3.1 实例化 `WorkflowEngine` + `WorkflowQAReviewer`（local_app.py 接线）

- **改哪**：`local_app.py`，在 `DispatchService` 构造（`L158`）之后、`create_app`（`L187`）之前，新建两个服务实例。
- **加什么**：

```python
# local_app.py，紧跟 dispatcher = DispatchService(...) 之后
from .core.workflow_engine import WorkflowEngine
from .core.qa_review import WorkflowQAReviewer
from .core.injector import WorkspaceInjector   # P2 已引入；若 P2 已在上方构造好 injector，直接复用那一个

# P2 已经为普通任务路径造好一个 WorkspaceInjector（带 allowed_roots / task_id 隔离）。
# 复用同一个实例——绝不要再 new 一个语义不同的注入器（否则 task_id 隔离规则会分叉）。
workflow_engine = WorkflowEngine(
    store,
    cards=cards,          # 复用上方已构造的 CardService（approval gate 卡片）
    bus=bus,
    injector=injector,    # ← P2 的同一个 WorkspaceInjector
)
# QA 门复用现成 Reviewer（reviewer.py 基线即存在，与 P4 无关——P4 降级后不构造任何 reviewer/硬门）
from .core.reviewer import Reviewer   # Reviewer 基线即存在，直接 new 一个 Reviewer(_llm(), language=...)
workflow_qa = WorkflowQAReviewer(workflow_engine, reviewer, store=store, bus=bus)
```

- **为什么**：这是把「写好但没人调」的状态机接进进程的**唯一**接线点（`L158` 是全仓唯一实例化 client-side 服务的地方）。
- **接缝**：`WorkflowEngine.__init__(store, *, cards=None, bus=None, injector=None, clock=None)`（`workflow_engine.py:168-184`），关键字参数全部 optional，注入干净。`WorkflowQAReviewer.__init__(engine, reviewer, *, checkpoints=None, store=None, bus=None)`（`qa_review.py:52-66`）。
- **依赖坑**：`injector` 必须是 P2 已实例化的那个。若 P2 把 injector 仅作为 `DispatchService` 的局部闭包而未提为局部变量，P5 接线需先把它提成 `local_app` 作用域内的具名变量 `injector = WorkspaceInjector(...)`，再同时喂给 `DispatchService` 和 `WorkflowEngine`。

### 3.2 把两个服务注入 `create_app`（server 接线）

- **改哪**：`local_app.py:187-190` 的 `create_app(...)` 调用；`server/app.py:426-440` 的 `create_app` 签名 + `L467` 附近的 `app.state.*`。
- **加什么**：
  - `create_app` 新增两个 keyword-only 参数：`workflow_engine: Any = None, workflow_qa: Any = None`（默认 None → team-cache server 无此能力，路由返回 503，与 dispatcher/definitions 同模式）。
  - `app.state.workflow_engine = workflow_engine` / `app.state.workflow_qa = workflow_qa`（紧跟 `L467` 的 `app.state.dispatcher = dispatcher`）。
  - `local_app.py` 的 `create_app(...)` 调用补 `workflow_engine=workflow_engine, workflow_qa=workflow_qa`。
- **为什么**：app.py 保持 shared-only（§14），所有 client-side 能力都是 INJECTED；workflow run 涉及本地 store + 本地秘方，绝不能在 app.py 里 import client。

### 3.3 新增 `POST /api/workflows/{start,begin,submit,resume}` 路由（server/app.py）

- **改哪**：`server/app.py`，在 `/api/tasks`（`L1375`）附近新增一组路由。**独立于 `/api/tasks`**（设计书 §10 明确「不混进 `/api/tasks`」）。
- **加什么**（请求体 + 4 条路由，照抄 definitions 的 503-fallback + 错误码映射模式）：

```python
# 与 _DispatchBody 同处（app.py L180 附近）
class _WorkflowStartBody(BaseModel):
    session_id: str
    workflow: str            # active workflow definition 的 name
    version: int | None = None

class _WorkflowStepBody(BaseModel):
    run_id: str

class _WorkflowSubmitBody(BaseModel):
    run_id: str
    qa_passed: bool | None = None    # 由 QA 通道回填；普通推进留 None

class _WorkflowResumeBody(BaseModel):
    run_id: str
    approved: bool

# 路由（与 /api/tasks 同区，app.py L1375 之后）
_WF_ERR_STATUS = {
    "no_workflow": 404, "bad_workflow": 400, "no_run": 404,
    "run_finished": 409, "blocked_on_gate": 409, "not_blocked": 409,
    "no_store": 503,
}

def _wf_engine():
    eng = getattr(app.state, "workflow_engine", None)
    if eng is None:
        raise HTTPException(status_code=503, detail="no workflow engine")
    return eng

@app.post("/api/workflows/start")
async def workflow_start(body: _WorkflowStartBody) -> dict:
    res = await _wf_engine().start(body.session_id, body.workflow, version=body.version)
    if res.get("ok"):
        return res
    raise HTTPException(status_code=_WF_ERR_STATUS.get(res.get("error", ""), 400),
                        detail=res.get("error", "decline"))

@app.post("/api/workflows/begin")
async def workflow_begin(body: _WorkflowStepBody) -> dict:
    res = _wf_engine().begin_step(body.run_id)   # 注意 begin_step 是同步方法（非 async）
    if res.get("ok"):
        return res
    raise HTTPException(status_code=_WF_ERR_STATUS.get(res.get("error", ""), 400),
                        detail=res.get("error", "decline"))

@app.post("/api/workflows/submit")
async def workflow_submit(body: _WorkflowSubmitBody) -> dict:
    res = await _wf_engine().submit_step(body.run_id, qa_passed=body.qa_passed)
    ...

@app.post("/api/workflows/resume")
async def workflow_resume(body: _WorkflowResumeBody) -> dict:
    res = await _wf_engine().resume_after_gate(body.run_id, body.approved)
    ...

@app.get("/api/workflows/{run_id}")
async def workflow_view(run_id: str) -> dict:
    view = _wf_engine().step_view(run_id)        # 同步
    if view is None:
        raise HTTPException(status_code=404, detail="no run")
    return view
```

- **接缝/坑（已核实方法签名）**：
  - `start(session_id, workflow_name, *, version=None)` → **async**（`workflow_engine.py:208`），返回 `{ok, run_id, workflow, total_steps, step}` 或 `{ok:False, error∈{no_workflow,bad_workflow}}`。
  - `begin_step(run_id)` → **同步**（`workflow_engine.py:324`），返回 `{ok, step, injection}` 或 error∈{no_run,run_finished,bad_workflow}。**别 `await`。**
  - `submit_step(run_id, *, qa_passed=None)` → **async**（`workflow_engine.py:370`），error∈{no_run,run_finished,blocked_on_gate,bad_workflow}；返回 `{ok, status∈{advanced,done,qa,blocked,failed}, ...}`。
  - `resume_after_gate(run_id, approved)` → **async**（`workflow_engine.py:449`），error∈{no_run,not_blocked,bad_workflow}。
  - `step_view(run_id)` → **同步**（`workflow_engine.py:314`），None 表示 run/workflow 已不存在。
- **为什么独立 API**：workflow 是控制流，不是一次性 dispatch；`/api/tasks` 的语义（建 Root Session + fire-and-forget）和 workflow 的「逐步推进 + 阻塞门」完全不同，混在一起会让 `_DispatchBody` 长出一堆与普通任务无关的字段。

### 3.4 轻量 step-instruction 派发（dispatch_service.py 或新模块）

> 这是 §10 的核心约束：**不要每步都跑完整 PM tool loop**（`_pm_launch` 的 plan→launch→while review 循环），否则成本 ×步数。step 派发只做「把 step 目标 + L0 索引 → 一条 coding instruction → `runner.launch` → `runner.wait`」。

- **改哪**：在 `DispatchService` 新增一个方法 `launch_workflow_step(...)`，或新建 `client/core/workflow_dispatch.py` 持有 `runner` + `engine`。推荐放 `DispatchService`（它已持有 `runner`、`store`、`_emit_*`、`_sync_pm_language`），避免重复造事件发射器。
- **加什么**（骨架）：

```python
# dispatch_service.py，DispatchService 内新增（与 _direct_launch 同风格，但单步、无 review 循环）
async def launch_workflow_step(self, run_id: str, *, agent: str, model: str = "", effort: str = "") -> dict:
    """轻量 step 派发：begin_step（注入）→ 把 step 目标转 coding instruction → launch → wait。
    不跑 PM review 循环（§10：避免 ×步数）。返回后由调用方/UI 调 submit_step 推进。"""
    eng = self.workflow_engine
    begun = eng.begin_step(run_id)                       # 同步；内部已 _inject step material（P2 注入器）
    if not begun.get("ok"):
        return begun
    step = begun["step"]
    run = step["run"]
    session_id = run["session_id"]
    workspace = self._workspace_for_session(session_id)  # 复用现有 session.workspace 读取
    language = self._sync_pm_language()
    # step.instruction + L0 索引（step material 已含 skills/standards 的名字+描述）转一条 coding instruction。
    # 正文不进 instruction：靠 P2 注入器写进 workspace 文件，coding CLI 自行渐进式披露读取。
    instruction = _workflow_step_instruction(step, language=language)
    handle = await self.runner.launch(agent, instruction, Path(workspace), session_id, model=model, effort=effort)
    await self.runner.wait(handle)
    return {"ok": True, "run_id": run_id, "step_index": run["step_index"]}
```

- **接缝/坑**：
  - `begin_step` **内部已调 `_inject`**（`workflow_engine.py:342`）把 step material 写进 workspace（P2 注入器）。所以 step 派发**不要**再单独 inject，避免双写。
  - `runner.launch(agent, instruction, workspace: Path, session_id, model=, effort=)`（`runner.py:45-53`）。
  - `runner.wait(handle)` 返回 **None**、不带 outcome（`runner.py:118-123`）——step 是否「完成」由调用方/QA 通道判定，再调 `submit_step`。
  - **`_clear` 时机**：`_clear` 由 engine 在 `submit_step` 的「QA 失败 / gate 拒绝 / run 完成」三处自动调（`workflow_engine.py:404/461/473`）。**step 派发本身不 clear**——否则下一步注入会被提前删掉。这与 P2 普通任务「follow-up 循环外才 clear」的原则一致。
- **为什么**：每步跑完整 `_pm_launch`（plan + N 轮 review）会把成本放大到 ×步数 × max_rounds；workflow 的骨架是固定的，PM 在这里只需把「这一步做什么」转成 instruction，review 由 QA 门（§9/§3.5）而非 PM 多轮承担。

### 3.5 QA 门接线（不过不进下一步）

- **改哪**：QA 通道用 `WorkflowQAReviewer.review_step(run_id, ...)`（`qa_review.py:68`）。带 `qa` 的 step 在 `submit_step(qa_passed=None)` 时被 engine park 成 `qa` 状态（`workflow_engine.py:397-401`）；随后调 `workflow_qa.review_step(run_id)`，它内部解析 rubric → 跑 Reviewer → 映射 verdict → **回调 `engine.submit_step(run_id, qa_passed=...)`**（`qa_review.py:109`）。
- **加什么**：一条 `POST /api/workflows/qa`（body `{run_id, diff?, context?}`）路由 → `app.state.workflow_qa.review_step(...)`；或在 `launch_workflow_step` 后由编排逻辑自动触发 QA（取决于 UI 是否要人工确认 diff）。
- **接缝/坑**：
  - 只有 `step_status == QA` 的 run 能被 `review_step` 判定（`qa_review.py:88`，否则 `not_in_qa`）。所以流程是：派发 step → `submit_step(qa_passed=None)` 让 engine park 成 `qa` → `review_step`。
  - 只有 `approve` verdict 通过（`qa_review.py:31,106`，§6.7 从严默认），其余（request_changes/escalate/parse-fail）→ `qa_passed=False` → engine 置 `failed` 并 `_clear`（`workflow_engine.py:402-406`）。
  - rubric body 不可解析 → `no_qa_rubric`（`qa_review.py:96-99`），fail-closed，不静默推进。
- **为什么**：§9 V2「硬执行」——qa_rubric 在 workflow 里是「过了才走下一步」的真门，不是只影响 prompt 的软约束。

### 3.6 步边界压缩成 `scope=workflow` 记忆（§8B.6）

- **改哪**：在 `_advance` 成功推进/完成时（`workflow_engine.py:466-478`），把刚结束那一步的原始时间线压成一条 `scope="workflow"` 的 `MemoryItem`。
- **加什么**：复用 **P1b-context 已建好的 `scope=workflow` writer**（横切依赖；P1b 已把 scope 常量化、writer 路径建好）。engine 在步推进前调该 writer，把 `session.plan` / 该步新增 events 压成步产出结论。
- **接缝/坑（评审 note）**：
  - 当前唯一的 `MemoryItem` writer 硬编码 `scope="session"`（`dispatch_service.py:791/799`）。**P5 不要自己再硬编码 `scope="workflow"` 字符串**——`MemoryItem.scope` 是无 enum/CHECK 的 free-form str，typo 会静默不匹配（`get_memory_items` 精确串匹配）。必须复用 P1b 定义的 scope 常量。
  - run 状态（`step_index`/`step_status`）在 `workflow_runs` 表，**不进会话上下文**；每步作为「新鲜注入」（lane 4，§8B.6），不塞进会话历史滚雪球。跨步只传 `scope=workflow` 的压缩结论，不传前几步原始时间线。
- **为什么**：多步 workflow 否则会一路堆叠到爆窗（§8B.6「步边界 = 天然压缩点」）。

### 3.7 最小 UI：启动入口 + run 状态查看（app.js）

- **改哪**：`server/web/app.js`（personal 入口，`index.html` 加载的那个；不是 `admin-app.js`）。
- **加什么**：
  1. 在 workflow definition 行（已有编辑 UI，`L1251-1252`）加一个「启动」按钮 → `api("/api/workflows/start", {method:"POST", body:{session_id, workflow: row.name}})`。
  2. 一个最小 run 状态面板：调 `GET /api/workflows/{run_id}` 显示 `step.name` / `total_steps` / `step.run.step_status`；approval gate（`status==blocked`）显示「批准 / 拒绝」两按钮 → `/api/workflows/resume`。
  3. i18n 键成对加 zh（约 `L56-57` 段）/ en（约 `L136-137` 段）——已有 `kindWorkflow` 但**没有** workflow run 相关文案键（startWorkflow / stepOf / approveGate 等），新增需 zh/en 两份。
- **为什么**：设计书 §17「workflow 未接 API/UI 前不能从产品运行」——UI 启动入口是 V2 验收（§15）的一部分。
- **范围**：只做「能显式启动 + 看状态 + 过审批门」。可视化编排画布不在 P5。

---

## 4. 验收标准（摘自 §14/§15，仅本阶段相关，改写为可勾选）

V2（workflow）验收（§15）：

- [ ] **显式启动**：在 UI 选一个 active `workflow` definition → `POST /api/workflows/start` 建出一条 `workflow_runs` 行（`step_index=0, step_status=pending`），并 emit 一条 `workflow` 事件（phase `started`）。
- [ ] **状态可查**：`GET /api/workflows/{run_id}` 返回当前 step 的 material（name/instruction/skills/standards/qa/missing）+ run 状态。
- [ ] **step 注入对应 L0/L1**：`begin_step` 把该步 skills/standards 经 P2 注入器写进 workspace（L0 索引进托管块、L1 正文进 `.claude/skills`/`.foreman/skills`），**正文不整段灌进 instruction**（§10）。
- [ ] **QA/check 不过不进下一步**：带 `qa` 的 step，Reviewer 非 `approve` → run 置 `failed`、injector `_clear`、不 advance（§9 V2）。
- [ ] **gate 停住等确认**：`approval: true` 的 step 在 `submit_step` 时 `status=blocked` + 建审批卡片；`resume_after_gate(approved=False)` → `failed`，`True` → advance（§9 / §11.2）。
- [ ] **轻量派发**：单步派发只 `launch`+`wait` 一次，**不跑** PM plan→review 循环（用调用计数断言：一个 N 步 workflow 不触发 N×max_runs 次 review）。

上下文（§14 §8B.6，本阶段相关条）：

- [ ] 多步 workflow 跑 N 步后，会话上下文**不随步数线性膨胀**（步边界压缩生效；断言 `Session.plan` 或 `scope=workflow` MemoryItem 数量与膨胀曲线）。

向后兼容 / 诚实边界（§17）：

- [ ] workflow **不自动套**——只有显式 `start` 才触发（§10「显式选择才触发」）。
- [ ] active workflow 热更新不影响已启动的 run（run 用 `parse_workflow(defn.body)` 在 `_spec_for_run` 每次重解析当前 active body——**注意此处是已知行为**：热更新会改变正在跑的 run 的骨架，见 §6 风险）。

---

## 5. 测试

> 集成测试**必须打真实路径**（engine API + 轻量派发 + 注入器），不允许只测 `parse_workflow` 这类纯函数或只测 `build_plan_prompt`。

### 5.1 单元测试（扩充 `tests/test_workflow_engine.py` 已有套件 + 新增）

- `WorkflowEngine` 注入了 P2 注入器后：`begin_step` 真的把 step material 写进 tmp workspace（断言 `.claude/skills/foreman-*` / `.foreman/skills/*.md` 存在、`CLAUDE.md` 托管块含 L0 索引、正文不在托管块整段出现）；run 结束 `submit_step` → `_clear` 后这些文件消失。
- QA 门：带 `qa` 的 step → `submit_step(qa_passed=None)` park 成 `QA`；`WorkflowQAReviewer.review_step` 用 mock reviewer 返回 `request_changes` → run `failed` 且 injector 已 clear；返回 `approve` → advance。
- approval gate：`submit_step` 在 `approval:true` step → `blocked` + cards.build_card 被调；`resume_after_gate(False/True)`。
- 边界错误码：`start` no_workflow/bad_workflow；`begin_step` no_run/run_finished；`submit_step` blocked_on_gate；`resume_after_gate` not_blocked。

### 5.2 API 集成测试（FastAPI TestClient + 真实 client Store）

- 用 `create_app(cfg, store, bus, workflow_engine=<real engine over tmp sqlite>, workflow_qa=<real>)` 起 app；`POST /api/workflows/start` → 200 + run_id；`/begin` → step view；`/submit` 推进；`/resume` 过门。
- 503 fallback：`workflow_engine=None` 时所有 `/api/workflows/*` → 503（team-cache server 无能力）。
- 错误码映射：no_run → 404、blocked_on_gate → 409、not_blocked → 409。

### 5.3 轻量派发集成测试（真实 dispatch 路径，禁止只测 prompt 构造）

- 用带 fake `runner`（记录 `launch`/`wait` 调用，不 spawn 真 CLI）+ 真 `WorkflowEngine` + 真注入器的 `DispatchService.launch_workflow_step`：
  - 断言 step material 经注入器进了 workspace（真路径，不是只检查返回的 dict）。
  - 断言 instruction 含 step 目标 + L0 索引，**不含**任一 skill/standard 的逐字正文（渐进式披露）。
  - 断言一个 N 步 workflow 的整跑只触发 N 次 `runner.launch`、**0 次** `pm_agent.review`（轻量派发不跑 review 循环）。

### 5.4 步边界压缩测试（§8B.6）

- 跑一个 3-step workflow，断言每步 advance 后产出一条 `scope=<P1b workflow 常量>` 的 `MemoryItem`；断言跨步注入的 lane 4 是「新鲜步状态」而非累积时间线。

---

## 6. 风险与回滚

| 风险 | 说明（呼应评审） | 缓解 / 回滚 |
|---|---|---|
| **依赖 P2 注入器未就绪** | `WorkflowEngine._inject`/`_clear` 调的 `self.injector` 若为 None，step 注入静默跳过（`workflow_engine.py:346-348` 返回 None），workflow 能跑但 step 指引不进 workspace——等于「点亮但没接渐进式披露」。 | P5 接线前确认 P2 已把 `WorkspaceInjector` 实例化并提为 `local_app` 具名变量。集成测试 5.1 必须断言文件真的落地，捕获 injector=None 的退化。 |
| **并发隔离缺口被放大** | P2 若未做 task_id 隔离，同 workspace 两个并发 workflow run（或一个 run + 一个普通任务）会互相覆盖 `CLAUDE.md` 托管块，且 `_clear` 的 `rmtree(.foreman/skills)` 会误删对方 skill 文件。 | 必须等 P2 的 task_id 隔离落地后再开 P5。workflow run 自带 `run_id`，可作为隔离维度传给注入器（与 P2 的 task_id 同位）。 |
| **热更新改变正在跑的 run** | `_spec_for_run`（`workflow_engine.py:493-500`）每次都用**当前 active** 的 `defn.body` 重 `parse_workflow`。run 跑到一半时若有人改/换 active workflow 版本，后续步会按新骨架走——与 §17「热更新不影响已启动 CLI 进程」对 workflow run **不成立**。 | 文档化为已知边界。若要严格隔离，需在 `start` 时把 `version` 钉死并让 `_spec_for_run` 优先按 run 记录的版本解析（当前 `WorkflowRun` 只存 `workflow_id` 不存 version——`workflow_id` 指向具体 Definition 行即某个版本，但 `_workflow_row`/`_spec_for_run` 用 `get_definition(run.workflow_id)` 取的是**该行**，实际**是钉死的**：核实 `workflow_engine.py:494` 用 `run.workflow_id` 而非按 name 重取 active）。→ **结论：`_spec_for_run` 实际按 `workflow_id` 取固定行，热更新换 active 不影响已起 run；但若直接编辑该行 body 则会变。** 建议「改 body 即新版本」策略（definition_service 的 create 新版本而非原地改 body）来彻底规避。 |
| **轻量派发被误改成完整 PM loop** | 实现者可能图省事直接复用 `_pm_launch`，导致 ×步数成本爆炸。 | 测试 5.3 用调用计数硬断言 `pm_agent.review` 调用数为 0。代码 review 时盯死 `launch_workflow_step` 不进 while review 循环。 |
| **`begin_step` 是同步、`submit_step` 是 async** | 路由里 `await begin_step(...)` 会 `TypeError`（同步方法返回 dict 不可 await）。 | §3.3 已标注：begin_step / step_view 同步，start / submit_step / resume_after_gate async。测试覆盖每条路由。 |
| **QA 门 fail-closed 误伤** | rubric body 不可解析 → `no_qa_rubric`，run 卡在 `qa` 不推进。 | 这是设计（§6.7 从严默认）。UI 要把 `no_qa_rubric` / `missing` 显式提示用户「rubric 没解析到，补/改 definition」。 |

**回滚**：本阶段全部是新增（新路由、新服务实例化、新 UI 按钮、engine 注入器接线）。回滚 = `create_app` 不传 `workflow_engine`/`workflow_qa`（路由 503）+ 移除 UI 启动按钮 + local_app 不实例化两个服务。普通任务路径（`/api/tasks` / `_pm_launch`）完全不受影响，因为 P5 不改动它们。`metadata_json`/`scope_json`/`workflow_runs` 表本就存在，无迁移可回滚。

---

## 7. 与设计书 / 其它阶段的对应

**映射到设计书章节**：

- §10（Workflow 控制流 V2）：本阶段全部 4 条要点——独立 API、轻量 step 派发、step 复用渐进式披露、gate/QA 用现成 `begin_step/submit_step/resume_after_gate` + §9 硬执行。
- §13(P5)：「独立 API/UI + `WorkflowEngine` 接线 + 轻量 step 派发」。
- §8B.6（Workflow 与压缩）：步边界压缩成 `scope=workflow` 记忆、run 状态不进上下文。
- §9（软约束 vs 硬执行）：qa_rubric 在 workflow 里是「过了才走下一步」的硬门。
- §15 / §14 验收：V2 显式启动、状态可查、step 注入对应 L0/L1、QA/check 不过不进下一步、gate 停住等确认。

**上游依赖（本阶段消费它们的产出）**：

- [P0](10-P0-copy-and-L0-metadata.md)：`metadata.description` + L0 漏斗（step material 的索引）。
- [P1](20-P1-L1-retrieval-budget-telemetry.md)：渐进式披露通道 + work_mode telemetry 事件 schema。
- [P2](40-P2-coding-agent-channel.md)：`WorkspaceInjector`（task_id 隔离、原生 SKILL.md、inject↔clear 成对、`.git/info/exclude`）——P5 直接复用，是**硬前置**。
- [P1b-context](31-P1b-unified-context-compression.md)：`scope=workflow` MemoryItem writer + scope 常量化。
- ~~P4~~（**非硬前置**）：P5 的 workflow QA 门由 `WorkflowQAReviewer` 复用基线即存在的 `reviewer.Reviewer`（`qa_review.py`/`reviewer.py` 在基线就有，与 P4 无关）。D2 后 [P4](60-P4-hard-enforcement.md) 已降级为软约束、不构造任何 reviewer/硬门，故**不是 P5 的上游依赖**（依赖图中 P5 只依赖 P0/P1/P2）。

**下游依赖本阶段的步骤**：无（P5 是 `recommended_order` 的最后一步）。

**共享常量 / Schema / 术语**：见附录 [`90-conventions-and-glossary.md`](90-conventions-and-glossary.md)（workflow_runs 状态机词表、`scope=workflow` 常量、telemetry 事件字段、文件路径映射）。索引与排期见 [`00-OVERVIEW-AND-SEQUENCING.md`](00-OVERVIEW-AND-SEQUENCING.md)，评审更正见 [`01-REVIEW-FINDINGS.md`](01-REVIEW-FINDINGS.md)。
