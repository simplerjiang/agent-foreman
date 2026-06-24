# P0 — 文案修订 + L0 元数据骨架 + 选择漏斗

> 日期：2026-06-24 ｜ 对应设计书章节：§4 / §4.1 / §4.2 / §4.3 / §5 / §12 / §13(P0) ｜ 分支：`codex/work-mode-design`（实现分支基线 == `1801128`）

> 配套阅读：进入开发前先读 [00-OVERVIEW-AND-SEQUENCING.md](00-OVERVIEW-AND-SEQUENCING.md)（全局顺序）、[01-REVIEW-FINDINGS.md](01-REVIEW-FINDINGS.md)（评审更正）、[90-conventions-and-glossary.md](90-conventions-and-glossary.md)（常量 / Schema / telemetry / 路径映射 / 术语）。本文不重复这些背景，只给 P0 的可执行步骤。

---

## 0. 目标与产出

P0 是整条路线图的**基线起步**，原本只做「便宜且可逆」的事——不碰运行时注入、不碰 PM 工具循环、不碰上下文预算。

> **【已拍板 2026-06-24】本阶段范围因 owner 决定扩容**：D3（存量 definition 的 LLM 批量回填纳入 P0 一并交付，不再只作运维步骤）与 D4（composer 手选勾选 UI 归 P0、UI 先行）将原「三件事」扩为「五件事」。其中 D3 给 P0 引入 PM LLM 调用与人工抽检环节，**P0 因此变重**（如实标注，见下方第 4、5 件与 §3 任务 4/7）。

五件事：

1. **修订过度承诺文案**：把工作方式页副标题里的「自动注入 / auto-injected」改掉（中英文两处），因为新架构是「按需拉取」而非「自动全量灌入」（§17 诚实边界）。
2. **L0 元数据骨架**：让 `Definition.metadata_json` 真正承载结构化的 `description`（做什么 + 何时用）——UI 编辑器加 description/metadata 输入框，前端能填能发；存量与种子 definition 回填 description；最后把「`description` 必填」做成可控的写时校验（fail-closed，但放行 import 路径）。
3. **选择漏斗 `resolve_work_mode_context()`**：新建 `work_mode_context.py`，实现 §5 的三步漏斗（scope 硬过滤 → 词法排序 → top-K 截断），输出 **L0 索引**（`[{id,kind,name,description,est_tokens}]`，**绝不含 body**），并记录 `dropped`。这是 P1 工具 `work_mode_search/get` 背后的引擎，P0 先把纯函数与单测落地。
4. **【已拍板 2026-06-24｜D3】存量 definition 的 LLM 批量回填**：对存量 definition 跑 `summarize_to_description(body) -> ≤1024 字`，人工抽检后写回 `metadata_json.description`。这是 owner 拍板纳入 P0 的交付项（不再只作运维步骤），**带 PM LLM 调用与抽检环节，使 P0 变重**（见 §3 任务 4b）。
5. **【已拍板 2026-06-24｜D4】composer 手选 work_mode_ids 勾选 UI（UI 先行）**：composer 加 definition 多选控件 + 本地选择状态，`runDispatch` body 带 `work_mode_ids`；后端 `_DispatchBody` 同步新增可选 `work_mode_ids` 字段「**接受但暂不消费**」以免 400。**真正消费（resolver 用这些 id 做手选直通/过滤）仍在 P1**（见 §3 任务 7）。

**本阶段定义之完成**：UI 能为 definition 填写并保存结构化 `description`；存量/种子 definition 都已有 description（**含 D3 的 LLM 存量回填，人工抽检后无空 description**）；`resolve_work_mode_context()` 能对一批 definition 跑通「scope 过滤 + 词法排序 + top-K」并产出不含 body 的 L0 索引（有单测覆盖）；工作方式页副标题不再宣称「自动注入」；**composer 能勾选 definition 并把 `work_mode_ids` 发出（D4，UI 先行），后端接受该字段不报 400**。

**本阶段还不做**（属于后续阶段，避免越界）：

- `work_mode_search/get` 工具、L0 进 system message、`work_mode_ids` 透传 → **P1**（[20-P1-...](20-P1-L1-retrieval-budget-telemetry.md)）。
- **【已拍板 2026-06-24｜D4】`work_mode_ids` 的真正消费**（resolver 用这些 id 做手选直通/过滤）→ **P1**。P0 只做 UI 与「后端接受但暂不消费」的字段占位。
- **【已拍板 2026-06-24｜D2】check 硬执行门**（任务结束实跑 check 命令、QA/check 不过则强制 follow_up、workflow 不进下一步）→ **推迟到 V2，P4 当前不实现**；本阶段只做软约束（rubric/standard 进 review 影响 done/follow_up，已主要由 P1 的 review 通道承担）。
- token 感知预算、自动压缩、LLM trace → **P1b**。
- 文件注入（`.claude/skills` / `.foreman/skills`）→ **P2**。

---

## 1. 前置依赖

- **前置 step 文档**：无。P0 是基线起步。
- **假定的代码状态**：本分支 HEAD == 基线 `1801128`。后端 definition API 对 `metadata_json` 的支持**已完整就绪**（见 §2）；`Definition` 表已有 `scope_json` / `metadata_json` 两列，**无需迁移**（§12）。`resolve_work_mode_context()` / `work_mode_context.py` **尚不存在**（已确认全仓无此文件），P0 从零创建。

---

## 2. 涉及文件与现状

所有行号已对照基线源码逐处核实；与设计书有出入处就地标注。

| 文件 | file:line（真实） | 当前行为 / 现状 |
|---|---|---|
| `server/web/app.js` | `23`（zh）、`103`（en） | 工作方式页副标题 `rulesSubtitle`。zh：`"维护工作流、技能、代码规范和验收标准 —— agent 干活时会自动注入。"`；en：`"... — auto-injected when agents work."`。**设计书 §13(P0) 把两者都写成 `app.js:23`，实际 en 在 `app.js:103`**，必须同改两行。全文「自动注入/auto-inject」仅此两处。 |
| `server/web/app.js` | `1245-1261` | `DefinitionEditor` 组件。当前只有四输入：kind 下拉、name、`scope_json`、body(textarea) + activate 勾选。**无 description / metadata 输入框**。 |
| `server/web/app.js` | `1551-1562` | `saveDefinition()`。POST body 只发 `{kind,name,body,scope_json,activate}`；PATCH 只发 `{body,scope_json}`。**均不发 `metadata_json`**。 |
| `server/web/app.js` | `1049-1064` | Playbook 列表卡。`row.body` 前 160 字当展示描述（`1055`），非结构化 description。 |
| `server/web/app.js` | i18n：zh `16-96`、en `97-176` | I18N 写死对象，zh/en 各一份相隔约 80 行。本簇相关键 `rulesSubtitle`(23/103)、`defnKind/defnName/defnScope/defnBody/defnActivate`(61/141)。**现无 description / metadata 对应键**，需成对新增。 |
| `core/definition_service.py` | create `124-169`、update `179-213`、`_validate` `406-418` | **当前完全无 `description` 校验**。`create_definition` 无 `description` 参数；`_validate` 仅校验 kind∈KNOWN_KINDS / name≤200 / body≤200_000 / `scope_json` 是 JSON 对象 / `metadata_json` 是 JSON 对象——**只验 `metadata_json` 能 parse 成 dict，从不读内部**。 |
| `core/definition_service.py` | import `280-367`（`_json_object_or_default` `80-83`） | import 路径走**独立的宽松校验**：`metadata_json` 经 `_json_object_or_default` 兜底成 `"{}"`，**不读 description**。这是有意分歧，P0 的必填 gate **不得**套到 import 路径（否则旧 bundle 幂等重导入会挂）。 |
| `core/examples.py` | `84`（metadata 拼装）、`114-124`（seed row） | 种子 definition 的 metadata 是 `{"example": True, **(entry.get("metadata") or {})}`。 |
| `examples/definitions/manifest.yaml` | 全文 8 条 | **所有 8 条 example 均无 `metadata` 字段** → 种子出来的 `metadata_json` 都是 `{"example": true}`，**无 description**。直接打开「必填」会让种子重导入失败——必须先回填。 |
| `core/dispatch_service.py` | `_within_any` `60-73` | `_within_any(path: str, roots: list[str])`，str 入参，`Path.resolve(strict=False)` + `is_relative_to`，Windows 安全。**注意它在 `client.core`，resolver 将放 `client.core`，同包可直接复用。** |
| `core/injector.py` | `_within_any` `64-77` | 另一份同名副本，**`Path` 入参**（与 dispatch 的 str 入参不同）。P0 不碰，仅记差异。 |
| `tools/policy.py` | `PathGuard` `19`、`_is_relative_or_same` `50-54` | `tools` 包里的第三份路径包含判断（`Path` 入参）。resolver 放 `core` 则用 `dispatch_service._within_any`，无需 import `tools`。 |
| `store/db.py` | `get_definitions` `442-466`、`get_active_definition` `468-478` | `get_definitions(*, kind=None, name=None, active_only=False)`（keyword-only）；`get_active_definition(kind, name)`（**positional**）。两者均解密 body 后返回。`active_only=True` 保证每 (kind,name) 恰一条 live。 |
| `store/models.py` | `Definition` `194-206`（`scope_json:201`、`body:202`、`metadata_json:203`） | 表已有两列，无需改表。 |
| `store/migrations.py` | `CLIENT_MIGRATIONS` `32-34` | 仅一条迁移（`decisioncard.diff_stat`），**无 Definition 表迁移**——印证 §12「不改表」。P0 不加迁移。 |
| `server/app.py` | `_DefinitionCreateBody` `208-217`、`_DefinitionUpdateBody` `220-226`、create `1488-1503`、update `1505-1519`、`_DEFN_ERR_STATUS` `1434-1438` | **后端已就绪**：请求体已含 `metadata_json`（create 默认 `"{}"`、update 默认 `None`），路由已下传，错误码已含 `bad_metadata_json:400`。**P0 的 description 落地在 API 层近乎零成本，缺口纯在前端 + service 层校验。** |

> 路径映射：表中相对名 → 绝对路径见 [90-conventions-and-glossary.md](90-conventions-and-glossary.md) 的「文件路径映射」。本簇所有 UI 改动落在 `index.html` 加载的 `app.js`（personal 入口）；team 入口 `app.html` 加载的是 `admin-app.js`，**与本簇无关**。

---

## 3. 开发任务（有序、可勾选）

> **顺序硬约束**（评审 blocker，§6 复述）：任务 2（UI 能填能发 `metadata_json`）→ 任务 4（回填 examples/存量，**含 D3 的 LLM 存量回填**）→ 任务 5（必填 gate fail-closed）。次序颠倒会立刻打挂存量 definition、种子重导入、`import_bundle` 幂等。**【已拍板 2026-06-24｜D3】LLM 存量回填必须在「必填 gate」（任务 5）打开之前完成，否则会打挂存量 definition。** 任务 1（文案）、任务 6（resolver）、任务 7（composer 手选 UI，D4）可与之并行，但 resolver 的 L0 用到 `metadata.description`，逻辑上排在「输入框可填」之后。

### [ ] 任务 1 — 修订过度承诺文案（`app.js` 两处 + 无 i18n 新增）

**改哪 / 加什么**：`server/web/app.js:23`（zh `rulesSubtitle`）与 `app.js:103`（en `rulesSubtitle`），去掉「自动注入 / auto-injected」。

- zh `23` 现：`"维护工作流、技能、代码规范和验收标准 —— agent 干活时会自动注入。"`
  → 改为不承诺「自动注入」的措辞，例如：`"维护工作流、技能、代码规范和验收标准 —— PM 规划时按相关性选用，干活时按需取用。"`
- en `103` 现：`"... — auto-injected when agents work."`
  → 例如：`"... — selected by relevance and pulled in on demand."`

**为什么**：§17 明确「不再过度承诺」；新架构是渐进式披露/按需拉取，不是自动全量灌入。**只改 zh(23) 会留下英文版的 `auto-injected`**（评审 minor，已核实 en 在 103）。

**接缝**：纯文案，无逻辑。改后需**重启 server 进程** `?v=` token 才更新（`ASSET_VER` 在 import 时算一次，见记忆「前端资源版本化」）。

---

### [ ] 任务 2 — 编辑器加 description / metadata 输入框 + saveDefinition 发 `metadata_json`（纯前端）

**这是顺序约束的第一步，必须先于任务 4/5。**

**2a. i18n 成对新增键**（`app.js` zh 段约 `16-96`、en 段约 `97-176`，建议挨着 `defnBody`(61/141) 加）：

```js
// zh（约 61 行附近）
defnDescription: "描述（必填 · ≤1024 字，说明做什么 + 何时用）",
defnDescriptionHint: "L0 选择信号：PM 据此判断这条工作方式该不该用。空描述不进自动选择。",
// en（约 141 行附近，成对）
defnDescription: "Description (required · ≤1024 chars: what it does + when to use)",
defnDescriptionHint: "L0 selection signal: the PM decides relevance from this. Blank → excluded from auto-select.",
```

**2b. `DefinitionEditor`（`app.js:1245-1261`）加一个 description textarea**，复用既有 `.field` / `.input` / `.textarea` class 保持样式一致。draft 上读写 `draft.description`（编辑器内部状态字段），保存时拼进 `metadata_json`：

```jsx
<div className="field"><span className="field-label">${d.defnDescription}</span>
  <textarea className="textarea" rows="3" maxLength=${1024}
    value=${row.description || ""}
    onChange=${(e) => update({ description: e.target.value })}
    placeholder=${d.defnDescriptionHint}></textarea>
</div>
```

打开既有 definition 编辑时，要把 `metadata_json` 里的 `description` 反序列化回 `draft.description`（编辑器初始化处：`JSON.parse(row.metadata_json||"{}").description`）。

**2c. `saveDefinition`（`app.js:1551-1562`）发 `metadata_json`**。把 `draft.description` 合并进现有 metadata 后整体发送（保留 metadata 里既有的键如 `example`）：

```js
// 组装 metadata_json：保留原有键，写入 description + schema 版本
function buildMeta(draft) {
  let meta = {};
  try { meta = JSON.parse(draft.metadata_json || "{}"); } catch (e) { meta = {}; }
  meta.schema = "foreman.workmode.meta/1";   // §4.2
  const desc = (draft.description || "").trim();
  if (desc) meta.description = desc; else delete meta.description;
  return JSON.stringify(meta);
}
// create 分支
body: { kind, name, body, scope_json, metadata_json: buildMeta(draft), activate }
// PATCH 分支
body: { body, scope_json, metadata_json: buildMeta(draft) }
```

**为什么**：后端 `_DefinitionCreateBody/_DefinitionUpdateBody`（`app.py:208-226`）已含 `metadata_json`，`create/update_definition`（`app.py:1493/1510`）已下传，错误码 `bad_metadata_json` 已就绪——**这一步纯前端，零 API/表改动**。

**接缝**：`est_tokens` / `keywords` 等 §4.2 其它字段 P0 **可选**，最小可行只需 `description`。`est_tokens` 由保存时测 body 得出（§4.2），建议放 P1 与预算器一起，P0 不强求；若 P0 顺手填，在 `buildMeta` 里加 `meta.est_tokens = Math.ceil((draft.body||"").length / 4)`。

---

### [ ] 任务 3 — Playbook 列表卡优先展示 description（`app.js:1049-1064`）

**改哪 / 加什么**：`app.js:1055` 当前用 `row.body` 前 160 字做 `.desc` 预览。改为**优先展示 `metadata.description`**，无则回退 body 预览：

```jsx
<div className="desc"><${MD} text=${(() => {
  try { const m = JSON.parse(row.metadata_json || "{}"); if (m.description) return m.description; } catch (e) {}
  return (row.body || "").slice(0, 160);
})()} className="markdown-compact" /></div>
```

**为什么**：否则用户填了 description 却在列表看不到（评审 minor）。

---

### [ ] 任务 4 — 回填 examples 种子 + 存量 definition 的 description（**含 D3 的 LLM 批量回填**）

**必须先于任务 5。** 否则种子重导入 / 存量 definition 会被「必填」打挂（评审 major + blocker）。**【已拍板 2026-06-24｜D3】存量 LLM 批量回填已纳入 P0 交付（不再只作运维步骤），P0 因此引入 PM LLM 调用与人工抽检、整体变重。**

**4a. 回填 8 条种子 example 的 description**：在 `examples/definitions/manifest.yaml` 给每条加 `metadata.description`（loader `examples.py:84` 已 `{"example": True, **(entry.get("metadata") or {})}`，原样透传 manifest 的 `metadata`，无需改 loader 代码）：

```yaml
  - kind: skill
    name: write-tests
    file: skill/write-tests.md
    scope: { languages: [python] }
    metadata:
      description: "为改动写/补单元测试：覆盖正常路径与关键边界。何时用：实现或修复了带逻辑的代码后。"
```

8 条逐一补写（write-tests / implementation-notes / python-style / test-naming / covers-happy-path / meets-standard-no-shortcuts / lint-passes / add-feature），文案≤1024 字、说明「做什么 + 何时用」。

**4b. 存量 definition 一次性 LLM 批量回填**（§4.3 步骤 2 ｜ **【已拍板 2026-06-24｜D3】纳入 P0 一并交付，不再只作运维步骤**）：写一个本地脚本/CLI 子命令，对 `store.get_definitions()` 里 `metadata_json` 无非空 `description` 的行，用 PM 的 LLM 跑 `summarize_to_description(body) -> ≤1024 字`，**人工抽检后**经 `update_definition(..., metadata_json=...)` 写回 `metadata_json.description`。

- 本子任务由两部分组成：**① 确定性种子回填**（即 4a，对 `manifest`/`examples` 的 8 条种子，确定性、零 LLM 依赖）+ **② 存量 LLM 回填**（对存量 definition 调 PM LLM 跑 `summarize_to_description(body)->≤1024 字`、人工抽检后写回）。
- **这是 D3 引入的、带 PM LLM 调用与人工抽检的较重任务**（不再是可省略的最小版/运维步骤）：P0 要实际跑完存量回填并通过人工抽检，使「必填 gate」打开后无存量被打挂。
- **顺序**：②（LLM 存量回填）必须在任务 5（必填 gate）打开之前完成，否则会打挂存量 definition（与 §6 顺序硬约束一致）。
- 回填前的 definition：UI 标「缺描述，暂不参与自动选择」（§4.3 步骤 3，与任务 3 的列表卡呼应——`metadata.description` 为空时显示提示徽标，可后续补）。

**为什么**：种子/存量都无 description（已核实 manifest 全 8 条无 metadata），fail-closed 必填会让它们重导入/再保存即报 `bad_metadata_json` 或新加的 description 错误码。

---

### [ ] 任务 5 — `description` 必填校验（fail-closed，仅 create/update，放行 import）

**必须在任务 2、4 完成后才打开。**

**改哪 / 加什么**：`core/definition_service.py`。

- `_validate`（`406-418`）新增：解析 `meta`（已知能 parse 成 dict），要求非空 `description` 键：

```python
# definition_service.py _validate 内，bad_metadata_json 之后
try:
    meta_obj = json.loads(meta or "{}")
except (ValueError, TypeError):
    return "bad_metadata_json"
desc = meta_obj.get("description") if isinstance(meta_obj, dict) else None
if not (isinstance(desc, str) and desc.strip()):
    return "missing_description"
if len(desc) > 1024:            # §4.2 约束：≤1024 字
    return "description_too_long"
```

- `update_definition`（`179-213`）：仅当本次传入了 `metadata_json`（`is not None`）时才套这条校验（PATCH 可只改 body 不动 metadata，不应被迫带 description）。
- **新增错误码**：在 `server/app.py:1434-1438` 的 `_DEFN_ERR_STATUS` 加 `"missing_description": 400, "description_too_long": 400`，并在 i18n / `friendlyError` 给中英文提示。
- **import 路径不套**：`import_bundle`（`280-367`）继续走 `_json_object_or_default`，**不读 description**。保持 create 与 import 校验有意分歧（评审 blocker：否则旧 bundle 幂等重导入破）。
- **`seed_examples` 路径**：种子经 `store.add_definition` 直插、**不经 `_validate`**（`examples.py:114-126`），所以种子重导入不会被 gate 拦；但任务 4a 仍要回填，因为种子也是 L0 候选，无 description 会被 resolver 排除。

**为什么**：§4.3 步骤 1「空 description 的 definition 不进 L0 索引（fail-closed）」。但「不进自动选择」**优先用 resolver 排除实现**（任务 6），写时拒绝只拦**新建/编辑**，不影响存量与 import（§12 向后兼容）。

> 顺序自检：打开本任务前，确认 (a) UI 能填能发 description（任务 2），(b) 8 条种子已有 description（任务 4a），(c) import 路径未套此校验。三者齐了再合入，否则会有回归。

---

### [ ] 任务 6 — 新建 `work_mode_context.py`，实现 `resolve_work_mode_context()`（§5 漏斗）

**改哪 / 加什么**：新建 `src/foreman/client/core/work_mode_context.py`（放 `core`，与 `dispatch_service` 同包，可直接复用其 `_within_any`，**避免 `tools → core` 反向依赖**——评审 major）。

**输入/输出契约**（P0 交付纯函数 + 单测；P1 再把它接进工具）：

```python
# work_mode_context.py
from .dispatch_service import _within_any   # 同包复用，str 入参，Windows 安全（dispatch_service.py:60）

# §8 预算常量（完整表见 90-conventions-and-glossary.md；P0 只用到选择/索引相关三个）
WORKMODE_MAX_SELECTED = 8        # top-K 进 L0 索引
WORKMODE_INDEX_DESC_CHARS = 200  # L0 索引里 description 截断（比存储上限 1024 小，索引更省）
WORKMODE_INDEX_MAX_TOKENS = 1500 # 整个 L0 索引块硬上限（P0 仅记录/校验用，真正裁剪在 P1）

def resolve_work_mode_context(
    definitions: list,          # 已 active 的 Definition 行（调用方传 store.get_definitions(active_only=True)）
    *,
    goal: str,
    workspace: str | None = None,
    agent: str | None = None,
    selected_ids: list[str] | None = None,   # 手选 id：跳过排序/截断直通（§5）
    kind: str | None = None,
    limit: int = WORKMODE_MAX_SELECTED,
) -> dict:
    """三步漏斗 → L0 索引。返回 {"selected": [...], "dropped": [...]}.
    selected/dropped 每条 = {id, kind, name, description, est_tokens}，**绝不含 body**。"""
```

**三步漏斗**（§5）：

1. **硬过滤（scope）**：解析每条 `scope_json`；用 `_within_any(workspace, [roots])` 过 workspace 前缀（**禁止裸字符串前缀**，Windows 会错）；过 agent；过 path globs（`fnmatch`）。**手选 id（`selected_ids`）直通**，跳过步骤 2/3。无 `description`（任务 5 的 L0 信号）的 definition **不进**（fail-closed 排除，呼应 §4.3「无 description 不参与自动选择」）。
2. **相关性排序（lexical，V1）**：对剩余项，按 `metadata.keywords` / `name` / `metadata.description` 与 `goal` 的词法重叠打分；`metadata.priority` 做 tie-break。（V2 换 embedding 是 [50-P3-...](50-P3-tool-rag-upgrade.md)。）
3. **截断（top-K）**：保留 top-`limit`（默认 8）进 `selected`；其余进 `dropped`（**绝不静默丢弃**，§5/timeline 显示「另有 N 条未选中」）。

**L0 索引条目构造**：每条只取 `{id, kind, name, description(截断到 WORKMODE_INDEX_DESC_CHARS), est_tokens}`。**读 description 来自 `metadata_json` 解析**，不读 body。

**为什么**：这是 P1 工具 `work_mode_search/get` 背后的引擎（§6 handler 调 `resolver.index(...)`），也是 coding-agent 通道（P2）写托管块索引的来源。P0 先把可独立单测的纯函数落地。

**接缝（与下游）**：

- **resolver 不持 store**：`resolve_work_mode_context` 收**已查好的 definition 列表**，不在内部碰 store。P1 由 `local_app.py:166` 的 lambda 闭包捕获 `store`，构造一个 resolver 对象（`index()/body()`）调用本函数（评审 corrected_ref：`from_config` 不收 store，resolver 由闭包注入）。
- **path 复用**：用 `dispatch_service._within_any`（str 入参）。`injector._within_any` 是 `Path` 入参的不同副本，`tools/policy.PathGuard._is_relative_or_same` 是第三份——P0 不统一三者，仅在注释标明所选来源，避免分叉（评审 minor）。
- **body 截断不在此处**：`WORKMODE_BODY_MAX_CHARS=6000` 的单条正文截断是 P1 `work_mode_get` handler 内的事，与 resolver 无关。

---

### [ ] 任务 7 — composer 手选 `work_mode_ids` 勾选 UI（**【已拍板 2026-06-24｜D4】归 P0，UI 先行**）

**【已拍板 2026-06-24｜D4】** owner 拍板把 composer 手选勾选 UI 提前到 P0（UI 先行）；**真正消费（resolver 用这些 id 做手选直通/过滤）仍在 P1**。本任务可与文案（任务 1）、resolver（任务 6）并行。

**改哪 / 加什么**：

- **前端 composer（`app.js:917-957`）**：加 definition 多选控件（勾选列表）+ **本地选择状态**（如 `selectedWorkModeIds`），让用户在派发前手选若干 definition。
- **前端 `runDispatch`（`app.js:1498-1517`）**：dispatch 请求 body 带上 `work_mode_ids`（取自本地选择状态）。
- **后端 `_DispatchBody`（`app.py:180-189`）**：新增**可选** `work_mode_ids` 字段（默认 `None` / 空列表），**「接受但暂不消费」**——仅为避免前端带该字段时被 422/400 拒绝，路由层不读取、不传给 resolver。

**为什么**：D4 要 UI 先行，让用户尽早能勾选；但手选直通漏斗的逻辑（resolver 据 `selected_ids` 跳过排序/截断直通，见任务 6 的 `selected_ids` 形参）落在 P1。P0 把后端字段做成「接受但暂不消费」，可让**旧无该字段的请求仍 200、新带该字段的请求也不报 400**，为 P1 平滑接线。

**接缝（与下游）**：

- **真正消费在 P1**：P1 的 dispatch handler 才把 `work_mode_ids` 透传给 `resolve_work_mode_context(..., selected_ids=...)`（任务 6 已预留 `selected_ids` 形参做手选直通）。P0 **不得**在后端读取/转发此字段。
- 本任务只动 personal 入口的 `app.js`（与本簇其余 UI 改动一致）；team 入口 `admin-app.js` 与本簇无关。

---

## 4. 验收标准（摘自 §14/§15，仅 P0 相关）

- [ ] **文案**：工作方式页副标题 zh(`app.js:23`) 与 en(`app.js:103`) 均不再出现「自动注入 / auto-injected」；全文 grep 无残留。
- [ ] **L0 输出不含 body**：`resolve_work_mode_context()` 返回的 `selected`/`dropped` 每条**只含** `{id,kind,name,description,est_tokens}`，单测断言无 `body` 键（§14 单元）。
- [ ] **scope 命中/不命中**（含 Windows 路径用 `_within_any`）：workspace 前缀过滤正确，跨盘/非子目录不误命中（§14 单元）。
- [ ] **排序与 top-K**：词法重叠高的排前，`priority` tie-break；超过 `limit` 的进 `dropped`，`dropped` 非空时被记录（§5/§14）。
- [ ] **无 description 排除**：无非空 `metadata.description` 的 definition 不进 `selected`（§4.3 fail-closed；§14 向后兼容「无 description 不进自动选择、可手选」）。
- [ ] **手选直通**：`selected_ids` 中的 id 跳过排序/截断，全部进 `selected`（§5）。
- [ ] **UI 可填可见**：编辑器能填 description、保存后 `metadata_json` 含 `description`；列表卡优先展示 description（§13(P0)）。
- [ ] **必填 gate 与向后兼容**：新建/编辑空 description 报 `missing_description`；**存量 definition 仍可读、import 旧 bundle 仍幂等成功**（§12，评审 blocker）。
- [ ] **【已拍板 2026-06-24｜D3】存量 LLM 回填**：批量回填（含人工抽检）跑完后，`store.get_definitions()` 里**无 `metadata.description` 为空的行**（必填 gate 打开前完成，否则打挂存量）。
- [ ] **【已拍板 2026-06-24｜D4】composer 手选 `work_mode_ids`**：composer 勾选 definition 后 `runDispatch` 发出的 body 含 `work_mode_ids`；**旧的、不带该字段的 dispatch 请求仍返回 200**（后端「接受但暂不消费」，P0 不读不转发）。
- [ ] **不改表**：无新增迁移；`metadata_json`/`scope_json` 复用（§12）。

---

## 5. 测试

> 集成测试纪律（§14）：凡涉及 PM 路径的断言**必须打 tool-loop 真实路径**，**不允许只测 `build_plan_prompt`**。P0 的 resolver 本身是纯函数，主要靠单测；但任务 6 的输出契约会被 P1 的集成测试复用，P0 要把契约测死。

**新增单元测试**（建议 `tests/client/core/test_work_mode_context.py`）：

- [ ] `resolve_work_mode_context`：scope 命中 / 不命中（构造 Windows 风格路径 `E:\\a\\b` 与子目录，断言 `_within_any` 行为）。
- [ ] 排序与 top-K：构造 >8 条候选，断言 top-`limit` 进 `selected`、其余进 `dropped`；`priority` tie-break。
- [ ] **L0 输出不含 body**：断言每条 dict 的 keys 子集 ⊆ `{id,kind,name,description,est_tokens}`。
- [ ] description 截断到 `WORKMODE_INDEX_DESC_CHARS`（200），存储上限 1024 不被带进索引。
- [ ] 无 description 排除：metadata 无/空 description 的行不进 `selected`。
- [ ] 手选 `selected_ids` 直通，跳过排序与 top-K。

**修改/新增 service 测试**（`definition_service` 已有测试套）：

- [ ] create/update 空 description → `missing_description`；>1024 → `description_too_long`。
- [ ] **import_bundle 不套 description 校验**：导入无 description 的旧 bundle 仍 `ok=True` 且幂等（评审 blocker 回归）。
- [ ] `seed_examples` 重跑幂等，且回填后种子 `metadata_json` 含 description（任务 4a）。
- [ ] **【已拍板 2026-06-24｜D3】存量 LLM 回填后无空 description**：回填脚本跑完（含人工抽检写回）后，`get_definitions()` 里每条 `metadata.description` 均非空（任务 4b ②）。
- [ ] **【已拍板 2026-06-24｜D4】`_DispatchBody` 接受但暂不消费 `work_mode_ids`**：带 `work_mode_ids` 的 dispatch 请求返回 200 且字段不被读取/转发；**不带该字段的旧请求仍 200**（任务 7 后端字段占位）。

**前端冒烟**（手动或既有前端测试）：

- [ ] 编辑器填 description → 保存 → reload 编辑同一条，description 回显。
- [ ] 列表卡显示 description 而非 body 前 160 字。

---

## 6. 风险与回滚

**特有风险 / 坑**（呼应评审）：

1. **顺序倒置打挂存量（blocker）**：先加 create/update 必填 gate、后补回填 → 存量 definition 一保存就报错、种子重导入失败、`import_bundle` 幂等破。**铁律**：任务 2 → 4 → 5。`import_bundle` 与 `seed_examples` 都**不经** `_validate`，必须验证它们仍放行。
2. **只改一处文案（minor）**：忘了 en(`app.js:103`) 会留下英文 `auto-injected`。改后 grep 全文确认。
3. **路径复用选错来源（major）**：resolver 放 `core` 用 `dispatch_service._within_any`（str 入参）。若误 import `tools/policy` 会造 `tools→core` 反向依赖；若误用 `injector._within_any`（Path 入参）会传参类型错。三份副本语义需确认一致，P0 不强行合并但要标注来源。
4. **resolver 误持 store（corrected_ref）**：`resolve_work_mode_context` 收已查好的列表，不在内部碰 store；store 注入是 P1 的 `local_app.py:166` lambda 闭包，别在 P0 提前把 store 塞进 `PMToolRuntime.from_config`。
5. **i18n 漏改一种语言（minor）**：新增 `defnDescription` 等键必须 zh/en 成对，否则一种语言渲染 `undefined`。
6. **改 app.js 不重启 server 看不到（开发体验）**：`ASSET_VER` 在 import 时算一次，本地改 `app.js` 后需重启进程 `?v=` 才更新。

**副作用边界**：P0 不接线任何运行时注入、不改 PM 工具循环、不动上下文预算/压缩——所以即便 resolver 有 bug，也**不会影响线上派发**（P1 才把它接进工具）。这正是「先做便宜且可逆的层」。

**回滚**：

- 文案（任务 1）：还原 `app.js:23/103` 两行。
- UI 输入框（任务 2/3）：还原 `DefinitionEditor` / `saveDefinition` / 列表卡；后端无改动，无需回滚 API。
- 必填 gate（任务 5）：从 `_validate` 移除 description 检查 + 从 `_DEFN_ERR_STATUS` 移除两个错误码——**无数据迁移、无表变更，纯代码回退**。
- resolver（任务 6）：删 `work_mode_context.py`，无引用方（P0 不接线），零副作用。
- 回填（任务 4）：manifest 是种子数据，回退即还原 yaml；存量回填写进了 `metadata_json`，回退代码不影响已写入的 description（它们是合法 metadata，无害）。

---

## 7. 与设计书 / 其它阶段的对应

**映射到设计书章节**：

- §4 / §4.1（L0 存哪：复用 `metadata_json`，`scope_json` vs `metadata_json` 分工）→ 任务 2/6。
- §4.2（`foreman.workmode.meta/1` schema：`description` 必填 ≤1024、`keywords`、`est_tokens`、`priority`）→ 任务 2（`buildMeta` 写 schema 标签）、任务 6（读 description/keywords/priority）。完整 schema 见 [90-conventions-and-glossary.md](90-conventions-and-glossary.md)。
- §4.3（回填：新增强制 + 一次性回填 + UI 标记）→ 任务 4/5；**其中存量一次性回填【已拍板 2026-06-24｜D3】用 PM LLM 批量回填、纳入 P0 交付**（任务 4b ②）。
- §5（三步漏斗、手选 vs 自动）→ 任务 6；**手选 UI【已拍板 2026-06-24｜D4】UI 先行归 P0**（任务 7），手选直通的真正消费在 P1。
- §9 / P4（check 硬执行）→ **【已拍板 2026-06-24｜D2】硬执行门推迟到 V2、P4 当前不实现**；本阶段只做软约束（rubric/standard 进 review，主要由 P1 的 review 通道承担），P0 不实现硬执行。
- §12（不改表、向后兼容）→ 任务 5 的 import 放行、无迁移；任务 7 后端 `work_mode_ids`「接受但暂不消费」亦属向后兼容（旧请求仍 200）。
- §13(P0)（文案 + L0 骨架 + resolver + 【D4】composer 手选 UI 先行）→ 全部任务。
- §14/§15 的 P0 子集 → 第 4 节验收。

**下游依赖本阶段的步骤**：

- **[20-P1-L1-retrieval-budget-telemetry.md](20-P1-L1-retrieval-budget-telemetry.md)**：`work_mode_search/get` 工具背后即 P0 的 `resolve_work_mode_context()`；L0 用 `metadata.description`。P1 必须作为**原子 PR**（ToolSpec + handler + dispatch + `from_config` 增 resolver 参 + `local_app.py:166` lambda 增传 + `pm_agent.py` 的 L0 注入 system）。**【已拍板 2026-06-24｜D4】composer 手选 UI 已归 P0（任务 7，UI 先行），P1 负责的是 `work_mode_ids` 的真正消费**（dispatch handler 把它透传给 `resolve_work_mode_context(..., selected_ids=...)` 做手选直通/过滤）。
- **[40-P2-coding-agent-channel.md](40-P2-coding-agent-channel.md)**：写托管块 L0 索引复用 P0 的 resolver 输出。
- **[50-P3-tool-rag-upgrade.md](50-P3-tool-rag-upgrade.md)**：把任务 6 步骤 2 的词法排序换/补 embedding，依赖 resolver 接口稳定。
- **[60-P4-hard-enforcement.md](60-P4-hard-enforcement.md)**：`metadata.check` 字段（§4.2/§9）依赖本阶段 metadata schema 落地。**【已拍板 2026-06-24｜D2】check 硬执行门推迟到 V2，P4 当前不实现**；本阶段及 P4 只做软约束（rubric/standard 进 review，主要由 P1 的 review 通道承担）。

**共享常量/术语**：`WORKMODE_MAX_SELECTED` / `WORKMODE_INDEX_DESC_CHARS` / `WORKMODE_INDEX_MAX_TOKENS` / `WORKMODE_BODY_MAX_CHARS` / `WORKMODE_MAX_PULLS`、L0 schema、`work_mode` telemetry 字段、路径映射——**统一定义在 [90-conventions-and-glossary.md](90-conventions-and-glossary.md)**，各阶段引用而非各写一份。
