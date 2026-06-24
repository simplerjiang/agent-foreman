# P3 — Tool-RAG 升级（词法 → embedding 语义检索）

> 日期：2026-06-24
> 对应设计书章节：§5（选择与相关性 / Tool-RAG）、§13(P3)
> 分支：codex/work-mode-design（本分支 HEAD == 基线 `1801128`，下文行号以此为准）
> 设计书全文：[`WORK_MODE_EFFECTIVE_INTEGRATION_DESIGN.md`](../WORK_MODE_EFFECTIVE_INTEGRATION_DESIGN.md)
> 索引与排期：[`00-OVERVIEW-AND-SEQUENCING.md`](./00-OVERVIEW-AND-SEQUENCING.md)；审阅结论：[`01-REVIEW-FINDINGS.md`](./01-REVIEW-FINDINGS.md)；常量/Schema/术语见附录 [`90-conventions-and-glossary.md`](./90-conventions-and-glossary.md)

---

## 0. 目标与产出

**一句话「本阶段定义之完成」**：`resolve_work_mode_context()`（P0 建的三步漏斗）里**第二步「相关性排序」从纯词法换/补为 embedding 语义检索**——候选 definition 多了之后，PM 仍能按「任务 goal 的语义」而非仅「关键词字面重叠」挑出最相关的 top-K，且**不改变漏斗的输入/输出契约**（scope 硬过滤、top-K 截断、`dropped` 记录、L0 索引不含 body 全部不变），对 P1 的 `work_mode_search/get` handler 与 telemetry **完全透明**。

**本阶段交付什么**：
1. 一个可插拔的 **relevance scorer 抽象**：`LexicalScorer`（P0 已有的词法实现，保底）与新增 `EmbeddingScorer`（语义）。漏斗按配置/可用性二选一，embedding 不可用时**自动回退**到词法，永不致命。
2. definition 的 **embedding 物化与缓存**：保存/激活 definition 时（或首次检索时懒计算）算出其语义向量并缓存，检索时只算一次 query 向量再做余弦相似度，避免每次派发把所有 body 重新发给 embedding API。
3. `LLMClient` 新增一个 **`embed()` 能力**（OpenAI 兼容 `/embeddings` + 可选本地哈希/词袋 fallback），与现有 `complete/tool_complete` 同一 provider 配置。
4. 配置开关 + 度量字段，让「语义检索是否启用、命中如何」线上可观测。

**为什么**：词法排序在 definition 数量小时够用（RAG-MCP 论文里词法已拿到大部分收益，见设计书 §5、§参考资料），但当 active definition 上规模、或 goal 措辞与 keywords 不字面重叠（同义/跨语言）时，词法会漏选。P3 是设计书明确标注的**「N 大时」纯增量升级**——不是重写，是给漏斗第二步换更聪明的打分器。

**本阶段完成后系统多了什么能力**：同样的 goal，能召回「语义相关但关键词没写中」的 definition；且这套语义检索是 best-effort 叠加，词法路径永远保底，不引入新的致命依赖。

---

## 1. 前置依赖

**必须先完成的 step**：
- [`10-P0-copy-and-L0-metadata.md`](./10-P0-copy-and-L0-metadata.md)（**强依赖**）——P3 改的就是 P0 建的 `resolve_work_mode_context()` 第二步。**没有 P0 的 L0 元数据骨架（`metadata.description`/`keywords`）与漏斗，P3 无处可插。**
- [`20-P1-L1-retrieval-budget-telemetry.md`](./20-P1-L1-retrieval-budget-telemetry.md)（**接口依赖**）——P3 升级的 scorer 被 P1 的 `WorkModeResolver.index()`（`work_mode_search` 背后）消费。P3 必须保持 `index()` 的入参/出参契约不变，只换其内部排序实现。

**可并行**：P3 与 [`30-P1b-llm-debug-trace.md`](./30-P1b-llm-debug-trace.md) / [`31-P1b-unified-context-compression.md`](./31-P1b-unified-context-compression.md) / [`40-P2-coding-agent-channel.md`](./40-P2-coding-agent-channel.md) **无代码耦合，可并行推进**；设计书排期把 P3 列为「可最后做」的纯增量项（见 [`00-OVERVIEW`](./00-OVERVIEW-AND-SEQUENCING.md) 的 `recommended_order`）。

**进入本阶段时假定的代码状态**（P0/P1 已落地后）：
- `resolve_work_mode_context()` 已存在（P0 建议落在新模块 `client/core/work_mode_context.py` 或 `client/core/work_mode/`），实现三步漏斗：①scope 硬过滤 → ②**词法相关性排序** → ③top-K 截断 + `dropped`。
- `metadata_json` 已能产出结构化 L0（`description` 必填、`keywords`/`est_tokens`），见 §4.2。
- `WorkModeResolver`（P1）持有 `store`，暴露 `index(query, kind, limit) -> [{id,kind,name,description,est_tokens}]` 与 `body(name, kind) -> str|None`，由 `local_app.py:166` 的 lambda 闭包注入 `PMToolRuntime`。
- `WORKMODE_*` 预算常量已集中在 `work_mode_context.py`（见附录 [`90`](./90-conventions-and-glossary.md)）。

> ⚠️ 若 P0/P1 把漏斗的实际模块名/函数名落得与本文假设不同，**以 P0/P1 文档的真实落点为准**；本文所有「在第二步插 scorer」的描述按那个真实落点对齐即可。P3 不改漏斗的第一/三步。

---

## 2. 涉及文件与现状

> 行号为本分支 HEAD(`1801128`) 实测。P0/P1 落地后，`work_mode_context.py`/`WorkModeResolver` 等为**新文件**（HEAD 尚不存在，下表标注「P0/P1 新建」）。P3 在它们之上做增量。

| 文件 | 真实 file:line（HEAD） | 当前行为 / P3 关注点 |
|---|---|---|
| `client/core/work_mode_context.py`（**P0 新建**） | — | 漏斗 `resolve_work_mode_context()` + 词法 scorer + `WORKMODE_*` 常量。**P3 在此加 scorer 抽象 + `EmbeddingScorer`。** |
| `client/core/work_mode/` resolver（**P1 新建**） | — | `WorkModeResolver.index/body`。**P3 不改其签名**，只让 `index` 内部排序换 scorer。 |
| `shared/llm/client.py` | 1-698（无 embedding 方法） | `LLMClient` 只有 `complete`(169)/`tool_complete`(191)/`list_models`(213)。**全文无 `/embeddings`、无 `embed()`**。P3 **新增** `embed()`。provider 解析走 `_resolve()`(117)、key 走 `_api_key()`(132)，可直接复用。 |
| `shared/config.py` | `LLMCfg` 87-99；`Config` 226-243 | `LLMCfg` 无 embedding 字段；`Config` 无 work-mode 段。**P3 新增** embedding 相关配置（建议独立 `WorkModeCfg`/`EmbeddingCfg` 段，勿塞进 `pm_tools`）。`Config(226-243)` 是 config.yaml 纯 `BaseModel`，**无 env 绑定**——新段加进 `Config` 即随 yaml 生效，无需 env glue（与 P1b-trace 的 debug 段不同，那个要 env）。 |
| `client/store/models.py` | `Definition` 194-206 | `Definition` 有 `metadata_json:203`（明文存储）、`body:202`（**仅 body 加密**）。**无 embedding 列**。P3 若要持久化向量，需决策：复用 `metadata_json`（明文）vs 新增列（要迁移）——见任务 3.3。 |
| `client/store/db.py` | `add_definition` 411-428；`get_definitions` 442-466；`get_active_definition` 468-478；`update_definition` 526 | body 透明加解密（`maybe_encrypt`/`maybe_decrypt`）；`metadata_json` 明文。`get_definitions(active_only=True)` 是漏斗候选来源。**P3 的向量缓存若落库，加在这里的 add/update 路径。** |
| `client/store/migrations.py` | `CLIENT_MIGRATIONS` 32-34（仅 v1）；`add_column` 在 `shared/migrations.py:62` | 客户端迁移 ledger 表 `schemaversion`。**仅一条迁移**，无 Definition 改表。P3 若加 embedding 列，**这里加一条幂等 `add_column` 迁移**。 |
| `pyproject.toml` | `dependencies` 17-24；`client` extra 28-39 | **无 numpy/faiss/sqlite-vec/sentence-transformers** 等向量依赖。core deps 仅 pydantic/httpx/typer/rich。**P3 默认不引入重依赖**：余弦相似度用纯 Python，embedding 走已有的 httpx provider。 |
| `client/tools/runtime.py` | `_truncate` 629-632 | 已有截断 helper（阈值 `cfg.max_chars=12000`，与 work-mode 的 6000 不同，不能直接套）——与 P3 无直接关系，列此仅提示 P1 的 body 截断在 handler 内。 |

**关键现状结论（P3 必读）**：
1. **embedding 是从零新增**：仓库 `src/` 内除前端 vendor JS 外，无任何 embedding/向量/余弦代码（grep 实测）。`LLMClient` 没有 `/embeddings` 通道。
2. **无向量依赖**：不引入 numpy/faiss。余弦相似度对几百条 definition 用纯 Python（`math.sqrt` + zip）足够，向量维度 ~1536，逐个点积是毫秒级。
3. **provider 通道现成**：embedding 复用 `LLMClient` 的 `_resolve()`/`_api_key()`/`self._client`(httpx)，OpenAI 兼容 `/embeddings` 即可；anthropic provider **无原生 embedding**，需 fallback（见任务 3.2）。

---

## 3. 开发任务（有序、可勾选）

> 总原则：**P3 是叠加，不是替换。词法 scorer 永远在，作为 embedding 不可用时的保底。** 任何一步失败都不能让 `resolve_work_mode_context()` 抛错——漏斗对 PM 必须始终能返回候选。

### 3.1 抽象出 relevance scorer 接口（在 P0 的漏斗模块内）

- [ ] **改 `client/core/work_mode_context.py`（P0 新建）**：把第二步「相关性排序」抽成一个 scorer 接口，P0 的词法实现成为其默认实现。

```python
# work_mode_context.py（P3 新增的抽象；保留 P0 的词法逻辑作为 LexicalScorer）
from typing import Protocol

class RelevanceScorer(Protocol):
    def score(self, query: str, candidates: list["WorkModeMeta"]) -> list[tuple["WorkModeMeta", float]]:
        """对已过 scope 的候选按与 query 的相关性打分，返回 (meta, score) 列表（未排序也可，调用方排序）。"""
        ...

class LexicalScorer:
    """P0 既有逻辑：keywords/name/description 与 goal 的词法重叠打分。永远可用、零外部依赖。"""
    def score(self, query, candidates):
        # …P0 已实现的词法重叠 + priority tie-break，原样搬进来…
        ...
```

- **接缝**：P0 的漏斗第二步原本内联了词法打分；P3 把它提到 `LexicalScorer.score()`，漏斗改为持有一个 `RelevanceScorer` 实例（默认 `LexicalScorer`）。第一步（scope `_within_any`）与第三步（top-K + `dropped`）**一行不动**。
- **为什么**：让 embedding 成为可替换实现，且词法保底零成本。

> 关于 `_within_any` 复用：scope 硬过滤复用路径包含判断。审阅结论提醒——`dispatch_service.py:60` 的 `_within_any` 在 `client.core`，若 resolver 实现在 `client.tools` 会造成反向依赖。P3 不碰 scope 步，沿用 P0 已定的归属即可；**不要**为 P3 新引一份路径判断。

### 3.2 给 `LLMClient` 加 `embed()`（`shared/llm/client.py`）

- [ ] **新增 `async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]`**：OpenAI 兼容 `/embeddings`，批量入、批量出。

```python
# shared/llm/client.py —— 紧邻 list_model_infos(222) 之后即可
async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
    """Return one embedding vector per input text via the configured provider's /embeddings.

    Reuses _resolve()/_api_key()/self._client like complete(). Only the OpenAI-compatible
    shape is supported natively; anthropic (no embeddings endpoint) raises so the caller
    can fall back to a local scorer. Empty input -> []."""
    if not texts:
        return []
    provider, base_url, default_model = self._resolve(model)
    if provider == "anthropic":
        raise LLMConfigError("anthropic provider has no /embeddings; use a local fallback")
    emb_model = (model or "").strip() or self._embedding_model()  # see config task 3.4
    r = await self._client.post(
        f"{base_url}/embeddings",
        headers={"Authorization": f"Bearer {self._api_key()}"},
        json={"model": emb_model, "input": texts},
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return [list(item.get("embedding") or []) for item in data]
```

- **接缝**：
  - 复用 `_resolve()`(client.py:117) 拿 provider/base_url、`_api_key()`(132) 拿 key、`self._client`(112, httpx) 发请求——与 `complete`/`list_model_infos` 同一套，**无需新建 client**。
  - `LLMConfigError`(client.py:48) 已存在，anthropic/缺 key 复用它。
  - `embed()` **不进** `tool_complete` 的 ws 分支——embedding 与对话 transport 无关，始终走 http POST。注意：若 `transport=ws`，base_url 仍是 http(s) 形式（ws_url 只在对话路径里转 scheme），`/embeddings` POST 正常。
- **fallback（无 `/embeddings` 或 anthropic 或调用失败）**：提供一个**纯本地、零网络**的 `LocalHashEmbedder`（确定性词袋/特征哈希到固定维度，L2 归一化）。它召回质量不如真 embedding，但保证 `EmbeddingScorer` 在任何 provider 下都能跑、且离线可测试。**不要**因为没有真 embedding 就静默退回词法——本地 embedder 仍能给出比纯字面更宽的语义近邻；只有当本地 embedder 也被关闭时才退回 `LexicalScorer`。

> 测试性：`LLMClient.__init__` 已支持注入 `transport=httpx.MockTransport`（client.py:95,111-112），所以 `embed()` 的单测可用 MockTransport 喂假向量，**不花 token、不联网**。

### 3.3 definition 向量的物化与缓存（存哪 / 怎么算一次）

embedding 必须缓存——否则每次派发把所有候选 body 重新发给 API，既慢又贵。两种落点，**推荐 A**：

- [ ] **方案 A（推荐，无迁移）——存进 `metadata_json`**：保存/激活 definition 时算一次向量，写进 `metadata_json` 的一个保留键，例如：
  ```json
  {"schema":"foreman.workmode.meta/1","description":"…","keywords":[...],"est_tokens":1234,
   "embedding":{"model":"text-embedding-3-small","dim":1536,"vec_b64":"<base64 of float32 array>","src_hash":"<sha256 of description+keywords+body-prefix>"}}
  ```
  - **优点**：`metadata_json` 已是明文列（db.py 不加密它），无需改表/迁移；随 export bundle 一起走（definition_service 的 import/export）。
  - **`src_hash`**：记录算向量时的源文本指纹，**热更新失效检测**——definition body/description 一改，hash 变，下次检索发现 hash 不匹配就重算。
  - **写入路径**：在 `DefinitionService.create/update`（definition_service.py，P0 已会写 `metadata_json`）或 `activate` 后**异步/惰性**算向量回填。**不要**在 create 的同步路径里阻塞等 embedding API——保存必须快且不能因 embedding API 挂掉而失败。建议「保存即标记 stale → 首次检索时若 stale 则现场算并回写」。
  - **隐私注意**（审阅结论提醒）：`metadata_json` 是**明文**（仅 body 加密）。向量本身不可逆回原文，风险可接受，但要在 §11 隐私框架里记一笔「L0 含向量，与 description/keywords 同级明文」。

- [ ] **方案 B（要迁移）——新增 `Definition.embedding_json` 列**：
  - `store/models.py:194-206` 加 `embedding_json: str = ""`；
  - `store/migrations.py:32-34` 加一条幂等迁移：`add_column(conn, "definition", "embedding_json", "TEXT NOT NULL DEFAULT ''")`（`add_column` 在 `shared/migrations.py:62`，幂等）；
  - 优点：与选择信号分离、不撑大 `metadata_json`；缺点：要迁移、要在 add/update/export 各处带上。
  - **仅当**向量体积大到污染 `metadata_json` 可读性时才选 B。

- [ ] **内存索引**：检索时不必每次反序列化全部向量。`WorkModeResolver` 持有一个进程内缓存 `{definition_id: (src_hash, vec)}`，首次用到时从 `metadata_json`/列解码并 cache；definition 热更新（id 不变、版本/hash 变）时按 hash 失效。**几百条向量全内存，无需 sqlite-vec/faiss。**

### 3.4 实现 `EmbeddingScorer`（语义打分）

- [ ] **新增 `EmbeddingScorer`（在 `work_mode_context.py`）**：

```python
class EmbeddingScorer:
    def __init__(self, embedder, vector_cache, *, fallback: RelevanceScorer):
        self._embedder = embedder        # callable: (list[str]) -> list[vec]，包 LLMClient.embed + 本地 fallback
        self._cache = vector_cache       # {id: (src_hash, vec)}，见 3.3 内存索引
        self._fallback = fallback        # LexicalScorer，最终兜底

    async def score(self, query, candidates):
        try:
            qv = (await self._embedder([query]))[0]
        except Exception:                # embedding 通道任何失败 -> 退词法，绝不抛
            return self._fallback.score(query, candidates)
        out = []
        for meta in candidates:
            vec = self._cache.get_or_compute(meta)   # stale 时现场算并回写
            if vec is None:
                # 该条没向量（旧 definition 未回填）-> 用词法分占位，别丢
                out.append((meta, self._fallback.score_one(query, meta)))
            else:
                out.append((meta, _cosine(qv, vec)))
        return out
```

```python
def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
```

- **要点**：
  - `score` 在漏斗里是 `async`（要 await query 向量）。P0 的漏斗第二步可能是同步的——P3 需把 `resolve_work_mode_context()` 第二步改为 `await scorer.score(...)`（漏斗本就在 PM 的 async 派发路径里，`WorkModeResolver.index()` 由 `work_mode_search` handler 调，handler 在 `runtime.call()` 的 async 链内，见 runtime.py:235）。**确认 P1 的 `index()` 是 async**；若是同步，P3 需把它改 async 并同步改其唯一调用点（`_work_mode_search` handler）。
  - **`priority` tie-break 保留**：embedding 分相近时仍用 `metadata.priority` 打破平局——与 P0 词法一致，§5 第二步语义不变。
  - **永不抛**：query embedding 失败 → 整体退词法；单条无向量 → 该条用词法分占位。漏斗对 PM 始终返回结果。

### 3.5 配置开关 + 接线（`shared/config.py` + resolver 构造）

- [ ] **`shared/config.py` 新增段**（独立，不塞 `pm_tools`）：

```python
class WorkModeCfg(BaseModel):
    # 语义检索开关：off=纯词法(P0 行为)；auto=有 embedding 通道就用、否则退词法；on=强制(失败仍退词法)
    semantic_search: str = "off"        # off | auto | on
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536           # 本地 fallback embedder 的维度 / 校验真 embedding 维度一致
    embedding_min_score: float = 0.0    # 余弦下限；低于此视为不相关（可选，默认不裁）
# Config(226-243) 里加：
#     work_mode: WorkModeCfg = WorkModeCfg()
```

- **默认 `semantic_search="off"`**：P3 上线后**默认仍是 P0 的词法行为**，必须显式开启才走 embedding。保证 P3 是「零行为变更地合入、按需开启」。
- [ ] **resolver 构造选 scorer**：`WorkModeResolver`（P1）按 `cfg.work_mode.semantic_search` 决定持有 `LexicalScorer` 还是 `EmbeddingScorer(fallback=LexicalScorer())`。embedder = `LLMClient.embed` 包一层 + 本地 fallback。
  - **接线点**：embedder 需要 `LLMClient` 实例。P1 的 resolver 由 `local_app.py:166` 的 lambda 闭包注入；P3 在同一处把 LLMClient（local_app 里已构造的 PM brain client）也闭包进 resolver 即可。**不要**让 `PMToolRuntime`/`ToolRuntimeConfig` 承载 LLMClient——保持 resolver 自带依赖（与 P1 的「resolver 持 store」同款）。

### 3.6 度量（telemetry，与 §16 对齐）

- [ ] **扩 `work_mode` 事件**（P1 已落该事件，schema 见附录 [`90`](./90-conventions-and-glossary.md) 与设计书 §8/§16）：在现有 `{selected,dropped,index_tokens,pulls,body_tokens,kinds}` 基础上**加可选字段**：
  - `scorer`: `"lexical" | "embedding" | "embedding_fallback_lexical"`（实际用了哪种，便于线上看 fallback 频率）。
  - `embed_calls`: 本次派发触发的 embedding API 调用数（query + 回填）。
- **为什么**：让「语义检索是否真在用、回退多不多、值不值这点 embedding 调用」线上可观测，呼应 §16「选得准不准」。**字段是新增、可选**——P1 的事件消费方不读它也不报错（向后兼容）。

---

## 4. 验收标准

> 仅摘与 P3 相关的条目（设计书 §14/§15）。注意 §14/§15 的 V/交付通道验收主要属 P0/P1/P2；P3 只需证明**「语义升级是无损叠加」**。

- [ ] **契约不变**：开启 `semantic_search` 前后，`resolve_work_mode_context()` 的输出形状一致——L0 索引**不含 body**（§14 单元）、`dropped` 仍记录未选中项（§5 步骤3）、top-K 截断仍生效（`WORKMODE_MAX_SELECTED`）。
- [ ] **scope 硬过滤不动**：embedding 只影响排序，**不放宽 scope**；scope 不命中的 definition 永远不进候选（含 Windows 路径用 `_within_any`，§14 单元）。
- [ ] **语义召回**：构造一条 goal，其措辞与某 definition 的 `keywords` **不字面重叠但语义相关**；`semantic_search="on"` 时该 definition 进 top-K，`"off"`（纯词法）时不进——证明语义检索带来真实增益。
- [ ] **回退保底**：embedding provider 不可用 / 返回错误时，漏斗**不抛**、仍返回词法排序结果；`work_mode` 事件 `scorer="embedding_fallback_lexical"`。
- [ ] **缓存生效**：同一 definition 多次检索**不重复**调 embedding API（命中向量缓存）；definition body/description 改动后（`src_hash` 变）下次检索重算一次。
- [ ] **默认无变更**：不改任何配置时（`semantic_search="off"`），P3 合入后行为与 P0/P1 **逐位一致**（同输入 → 同 selected/dropped/排序）。
- [ ] **不出 server**：embedding 调用在**本地进程**内（用本地 PM brain 的 LLMClient），definition body/向量**不经 server**（守 §8.3/§14「definition 是本地秘方」）。
- [ ] **度量可查**：`work_mode` 事件含 `scorer`/`embed_calls`，可据此算「语义检索启用率 / 回退率」（§16）。

---

## 5. 测试

> 集成测试必须打 **tool-loop 真实路径**（带 `tool_runtime_factory` 的 `PMAgent`），不允许只测 `build_plan_prompt`（§14 硬性要求）。P3 的语义升级要在 `work_mode_search` → resolver → scorer 的真实链路上验证。

**单元（新增）**：
- [ ] `LLMClient.embed`：用 `httpx.MockTransport` 喂假 `/embeddings` 响应，断言批量入批量出、维度正确、空输入返 `[]`、anthropic provider 抛 `LLMConfigError`、缺 key 抛 `LLMConfigError`（复用 `_api_key()`）。
- [ ] `_cosine`：正交向量→0、同向→1、空向量→0、不等长/含 0 范数→0（不崩）。
- [ ] `LocalHashEmbedder`：确定性（同输入同输出）、固定维度、L2 归一化、离线零网络。
- [ ] `EmbeddingScorer.score`：
  - 正常：按余弦排序，语义近的排前；
  - query embedding 抛异常 → 整体退 `LexicalScorer`（断言 fallback 被调）；
  - 单条候选无向量 → 该条用词法分占位、不被丢弃；
  - `priority` tie-break 在分数相近时仍生效。
- [ ] 向量缓存：`src_hash` 不变→命中缓存不重算；body 改动→hash 变→重算一次。
- [ ] 漏斗不变量：`semantic_search="off"` 时输出与 P0 词法逐位一致（同一组 fixture 跑两遍断言相等）。

**集成（打 tool-loop 路径）**：
- [ ] 用线上路径（`PMAgent` + `tool_runtime_factory` → `PMToolRuntime` → resolver 注入了 `EmbeddingScorer`，embedder 用 MockTransport 假向量）：
  - 准备多条 active definition，其中一条与 goal **语义相关但关键词不重叠**；
  - PM 调 `work_mode_search` → 断言该 definition 出现在返回的 L0 索引里（`"on"`），且**返回项仍只含元数据、不含 body**；
  - 切到 `"off"` 重跑同 goal → 该条不在 top-K（证明差异来自语义）。
- [ ] 回退路径集成：embedder MockTransport 返回 500 → `work_mode_search` 仍返回（词法）结果，`work_mode` 事件 `scorer="embedding_fallback_lexical"`。

**回归**：
- [ ] 已有 P0/P1 的 `resolve_work_mode_context` / `work_mode_search/get` 单测、集成测在 `semantic_search="off"` 下**全绿不变**（P3 默认关，回归零变更）。

---

## 6. 风险与回滚

**本阶段特有风险与坑**：
1. **embedding API 成本/延迟**：query 每次派发算一次（可接受），但**定义向量回填若放同步保存路径会阻塞且能让保存因 API 挂掉而失败**。→ 必须惰性/异步回填，保存路径绝不等 embedding（任务 3.3）。
2. **provider 无 embedding**：anthropic provider 与许多 OpenAI 兼容代理**没有 `/embeddings`** 或省略它。→ `embed()` 对 anthropic 直接抛、调用失败统一退本地 fallback embedder 或词法，**永不致命**（任务 3.2/3.4）。
3. **维度不一致**：换 embedding_model 后旧缓存向量维度与新 query 向量不同 → `_cosine` 会给错值。→ 缓存里存 `model`/`dim`，检索时维度不匹配视为 stale 重算（任务 3.3 的 `embedding` 子对象带 `model`/`dim`）。
4. **热更新失效**：definition body/description 改了但向量没重算 → 排序用旧语义。→ `src_hash` 比对，不匹配重算（任务 3.3）。
5. **隐私**：向量存 `metadata_json` 明文（仅 body 加密）。向量不可逆，但仍属「比 body 多暴露的选择信号」——在 §11 隐私框架记一笔（审阅结论 extra_observation）。
6. **async 传染**：scorer 变 `async` 会把 `resolve_work_mode_context()` 第二步与 `WorkModeResolver.index()` 都拉成 async。→ 确认 P1 的 `index()` 已是 async（在 `runtime.call()` 的 async 链内调用，runtime.py:235），若不是需同步改其唯一调用点 `_work_mode_search`。

**如何回滚**：
- **配置级**：`work_mode.semantic_search="off"` 即整体退回 P0 词法路径——**无需改代码、无需重启逻辑变更**，这是 P3 最大的安全网（默认就是 off）。
- **数据级**：方案 A（向量存 `metadata_json`）回滚只需停读 `embedding` 键，旧字段不影响 L0 其它消费方（它们不读 `embedding`）。方案 B 的新列留空即无影响（迁移幂等、不删数据）。
- **依赖级**：P3 默认不新增第三方依赖（纯 Python 余弦 + 已有 httpx），无依赖回滚负担。

**呼应评审 findings**：
- 审阅结论将 P3 列为「纯增量，依赖 P0/P1 的 resolver 接口稳定，可最后做」——本文据此把 P3 设计成**配置开关后的无损叠加**，不触碰 scope/top-K/telemetry 既有契约。
- 不复用 `dispatch_service._within_any` 跨包引入反向依赖（P3 不碰 scope 步，沿用 P0 归属）。

---

## 7. 与设计书 / 其它阶段的对应

**映射到设计书章节**：
- §5「选择与相关性（Tool-RAG）」第 2 步括注「（V2 可换 embedding，但词法已能拿到 RAG-MCP 论文里大部分收益）」——**P3 即落实这句 V2**。
- §13(P3)「词法排序换/补 embedding；`work_mode_search` 支持语义检索」——本文逐条覆盖。
- §参考资料 RAG-MCP / Toolshed（Tool-RAG：~50% token、3.2× 准确率）——语义检索的收益依据。

**上游（P3 依赖）**：
- [`10-P0`](./10-P0-copy-and-L0-metadata.md)：提供漏斗 + L0 元数据（`description`/`keywords`）+ 词法 scorer（P3 的保底）。
- [`20-P1`](./20-P1-L1-retrieval-budget-telemetry.md)：提供 `WorkModeResolver.index/body` 契约 + `work_mode` telemetry 事件（P3 扩字段）。

**下游（依赖 P3 或与之相关）**：
- **无强下游**。P3 是末端纯增量，[`40-P2`](./40-P2-coding-agent-channel.md) / [`60-P4`](./60-P4-hard-enforcement.md) / [`70-P5`](./70-P5-workflow-control-flow.md) 都依赖 P0/P1 的 resolver 接口，而 P3 **不改该接口的签名**，故对它们透明——它们拿到的是「排序更准的同一份 L0 索引」。

**共享常量/Schema**：`WORKMODE_*` 预算常量、`foreman.workmode.meta/1` schema、`work_mode` 事件 schema 统一见附录 [`90-conventions-and-glossary.md`](./90-conventions-and-glossary.md)；P3 新增的 `embedding` 子对象 schema 与 `WorkModeCfg` 字段建议一并登记到附录，避免各阶段各写一份。
