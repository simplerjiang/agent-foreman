"""P3 — semantic (embedding) Tool-RAG upgrade (DESIGN §5/§13(P3)).

Default OFF → byte-identical to P0/P1 lexical. When on, the funnel's step-2 ranking re-ranks the
scope-passing candidates by embedding cosine, with a never-fatal fallback to lexical. Unit-tests the
embed plumbing + scorer + cache; integration asserts the real dispatch records the scorer telemetry.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from foreman.client.core.dispatch_service import DispatchService
from foreman.client.core.pm_agent import PMAgent
from foreman.client.core.work_mode_context import (
    EmbeddingScorer,
    LocalHashEmbedder,
    WorkModeResolver,
    _cosine,
)
from foreman.client.store import Store
from foreman.client.store.models import Definition
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg
from foreman.shared.events import EventBus, make_event
from foreman.shared.llm import LLMClient


# ── LLMClient.embed ───────────────────────────────────────────────────────────────────────────────
def _embed_client(*, provider="openai", key="sk-test"):
    cfg = Config()
    cfg.llm.provider = provider
    cfg.llm.base_url = "https://api.test"
    cfg.secrets.llm_api_key = key

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        n = len(body.get("input", []))
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in range(n)]})

    return LLMClient(cfg, transport=httpx.MockTransport(handler))


async def test_embed_batch_in_out():
    client = _embed_client()
    out = await client.embed(["a", "b"])
    assert len(out) == 2 and out[0] == [0.1, 0.2, 0.3]
    assert await client.embed([]) == []


async def test_embed_orders_by_index():
    """Vectors must be index-aligned to inputs even when the provider returns `data` out of order
    (regression: embed() previously trusted response array order)."""
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://api.test"
    cfg.secrets.llm_api_key = "sk-test"

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(json.loads(request.content.decode("utf-8"))["input"])
        # return REVERSED order, each tagged with its true index
        data = [{"index": i, "embedding": [float(i)]} for i in reversed(range(n))]
        return httpx.Response(200, json={"data": data})

    client = LLMClient(cfg, transport=httpx.MockTransport(handler))
    assert await client.embed(["a", "b", "c"]) == [[0.0], [1.0], [2.0]]


async def test_embed_anthropic_raises():
    from foreman.shared.llm.client import LLMConfigError
    client = _embed_client(provider="anthropic")
    with pytest.raises(LLMConfigError):
        await client.embed(["a"])


async def test_embed_missing_key_raises():
    from foreman.shared.llm.client import LLMConfigError
    client = _embed_client(key="")
    with pytest.raises(LLMConfigError):
        await client.embed(["a"])


# ── cosine + local embedder ───────────────────────────────────────────────────────────────────────
def test_cosine_edges():
    assert _cosine([1, 0], [1, 0]) == 1.0
    assert _cosine([1, 0], [0, 1]) == 0.0
    assert _cosine([], [1.0]) == 0.0
    assert _cosine([1, 2], [1, 2, 3]) == 0.0  # length mismatch → 0, no crash


async def test_local_hash_embedder_deterministic_and_normalized():
    e = LocalHashEmbedder(dim=64)
    a = (await e(["hello world"]))[0]
    b = (await e(["hello world"]))[0]
    assert a == b and len(a) == 64
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9  # L2-normalized
    assert (await e([""]))[0] == [0.0] * 64  # empty → zero vector, no crash


# ── EmbeddingScorer: ranking, cache, fallback ──────────────────────────────────────────────────────
class _FakeEmbedder:
    """Maps text→vector by a rule so cosine ranking is controllable; counts calls. Raises if armed."""

    def __init__(self, rule, *, boom=False):
        self.rule = rule
        self.calls = 0
        self.boom = boom

    async def __call__(self, texts):
        self.calls += 1
        if self.boom:
            raise RuntimeError("embed down")
        return [self.rule(t) for t in texts]


async def test_embedding_scorer_ranks_and_caches():
    # query aligns with 'A' text; 'B' is orthogonal.
    def rule(t):
        return [1.0, 0.0] if ("alpha" in t or "QUERY" in t) else [0.0, 1.0]

    emb = _FakeEmbedder(rule)
    scorer = EmbeddingScorer(emb)
    items = [{"id": uuid.uuid4().hex, "text": "alpha thing", "src_hash": "h1"},
             {"id": uuid.uuid4().hex, "text": "beta thing", "src_hash": "h2"}]
    scores, calls = await scorer.scores("QUERY", items)
    assert scores[items[0]["id"]] > scores[items[1]["id"]]  # A ranks above B
    assert calls == 2  # query + one batch of candidates
    # second call with the SAME src_hash → candidates cached, only the query is embedded.
    emb.calls = 0
    scores2, calls2 = await scorer.scores("QUERY", items)
    assert calls2 == 1  # only the query embed; candidates hit the cache
    # change a candidate's src_hash → it is re-embedded.
    items[0]["src_hash"] = "h1-new"
    emb.calls = 0
    await scorer.scores("QUERY", items)
    assert emb.calls == 2  # query + re-embed of the stale candidate


async def test_embedding_scorer_returns_none_on_failure():
    scorer = EmbeddingScorer(_FakeEmbedder(lambda t: [1.0], boom=True))
    scores, _ = await scorer.scores("q", [{"id": "x", "text": "t", "src_hash": "h"}])
    assert scores is None  # signals the resolver to fall back to lexical


# ── resolver: default-off == lexical; on → embedding re-rank + telemetry ───────────────────────────
def _store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.init()
    return s


def _seed(store, name, *, description, keywords=None):
    meta = {"description": description}
    if keywords:
        meta["keywords"] = keywords
    row = Definition(id=f"id-{name}", kind="skill", name=name, version=1, status="active",
                     is_active=True, scope_json="{}", body="B", metadata_json=json.dumps(meta))
    store.add_definition(row)
    store.set_definition_active(row.id)


async def test_aresolve_default_off_equals_lexical(tmp_path):
    store = _store(tmp_path)
    _seed(store, "a", description="alpha", keywords=["x"])
    _seed(store, "b", description="beta", keywords=["y"])
    r = WorkModeResolver(store, goal="x")  # no scorer → lexical
    sync = r.resolve()
    asyncv = await r.aresolve()
    assert [e["name"] for e in sync["selected"]] == [e["name"] for e in asyncv["selected"]]
    assert r.last_scorer == "lexical"


async def test_aresolve_embedding_reranks_beyond_lexical(tmp_path):
    store = _store(tmp_path)
    # 'migration' lexically matches the goal; 'alpha' does not.
    _seed(store, "migration", description="run migration", keywords=["migration"])
    _seed(store, "alpha", description="unrelated words", keywords=["zzz"])

    def rule(t):
        # query + the 'alpha' definition embed to the same direction; 'migration' is orthogonal.
        return [1.0, 0.0] if ("run migration" == t or "alpha" in t) else [0.0, 1.0]

    r = WorkModeResolver(store, goal="run migration", scorer=EmbeddingScorer(_FakeEmbedder(rule)))
    out = await r.aresolve(limit=1)
    assert out["selected"][0]["name"] == "alpha"  # embedding beats lexical
    assert r.last_scorer == "embedding" and r.embed_calls >= 1
    # lexical (off) would pick 'migration'
    lex = WorkModeResolver(store, goal="run migration").resolve(limit=1)
    assert lex["selected"][0]["name"] == "migration"


# ── integration: dispatch records the scorer in work_mode telemetry ────────────────────────────────
class _FakeHandle:
    session_id = "s"


class _FakeRunner:
    async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
        h = _FakeHandle()
        h.session_id = session_id
        self._store.add_event(make_event("stop", agent, session_id, payload={"result": "done"}))
        return h

    async def wait(self, handle):
        return None


def _final_pm():
    class FakeLLM:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None,
                           state_key=""):
            if "reviewing a coding CLI" in messages[0].content:
                return json.dumps({"done": True, "summary": "ok", "reason": "", "follow_up": ""})
            return json.dumps({"summary": "go", "agent": "codex", "model": "", "effort": "high",
                               "instruction": "do it", "todo": [], "ready": True})

    return PMAgent(FakeLLM(), tool_runtime_factory=None)


async def _dispatch_with_embedder(tmp_path, embedder):
    store = _store(tmp_path)
    _seed(store, "s1", description="a skill", keywords=["k"])
    cfg = Config()
    cfg.agents = {"codex": AgentCfg(command="codex", enabled=True)}
    cfg.workspaces = [WorkspaceCfg(path=str(tmp_path))]
    cfg.work_mode.semantic_search = "on"
    runner = _FakeRunner()
    runner._store = store
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=_final_pm(),
                          embedder=embedder)
    res = await svc.create("do work", workspace=str(tmp_path))
    await asyncio.gather(*list(svc._tasks))
    rows = store.get_events(res["session_id"])
    return [json.loads(e.payload_json) for e in rows if e.type == "work_mode"][0]


async def test_dispatch_records_embedding_scorer(tmp_path):
    wm = await _dispatch_with_embedder(tmp_path, LocalHashEmbedder(64))
    assert wm["scorer"] == "embedding" and wm["embed_calls"] >= 1


async def test_dispatch_falls_back_to_lexical_when_embedder_fails(tmp_path):
    async def boom(_texts):
        raise RuntimeError("no embeddings")

    wm = await _dispatch_with_embedder(tmp_path, boom)
    assert wm["scorer"] == "embedding_fallback_lexical"
