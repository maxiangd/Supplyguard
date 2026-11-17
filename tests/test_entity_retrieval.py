"""图谱实体混合召回：关键词优先，向量兜底。

测试不连 Qdrant/Neo4j，用假客户端验证三件事：
1. 索引文本和 payload 正确，角色白名单随点写入。
2. 向量查询带角色过滤，分数和阈值正确传递。
3. 关键词命中事件时不被向量覆盖；短词命中时才允许补候选。
"""

from types import SimpleNamespace

import pytest

from app.services import entity_retrieval, graph

EVENT = {
    "id": "event-yichun-suspend",
    "name": "宜春枧下窝锂矿停产",
    "type": "event",
    "aliases": ["锂矿停产", "枧下窝停产"],
    "desc": "锂云母供给受限",
    "allowed_roles": ["admin", "procurement"],
}

REGION = {
    "id": "region-fujian-ningde",
    "name": "中国福建（宁德）",
    "type": "region",
    "aliases": ["福建", "宁德"],
    "desc": "总部所在地",
    "allowed_roles": ["admin", "procurement"],
}


@pytest.fixture
def embedding_key(monkeypatch):
    """索引和语义检索都要求配置密钥，测试里用假密钥放行。"""
    monkeypatch.setattr(entity_retrieval.settings, "embedding_api_key", "test-key")


class FakeQdrantClient:
    """记录调用参数的假客户端，不发网络请求。"""

    def __init__(self, hits=None):
        self.hits = hits or []
        self.upserted = None
        self.deleted = []
        self.query_filter = "unset"
        self.score_threshold = "unset"
        self.limit = None

    def collection_exists(self, collection_name):
        return False

    def create_collection(self, **kwargs):
        self.created = kwargs

    def upsert(self, **kwargs):
        self.upserted = kwargs

    def scroll(self, **kwargs):
        return [], None

    def delete(self, **kwargs):
        self.deleted.append(kwargs)

    def query_points(self, **kwargs):
        self.query_filter = kwargs.get("query_filter")
        self.score_threshold = kwargs.get("score_threshold")
        self.limit = kwargs.get("limit")
        return SimpleNamespace(points=self.hits)


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------


def test_entity_document_includes_aliases_and_desc():
    text = entity_retrieval.entity_document(EVENT)

    assert EVENT["name"] in text
    assert "锂矿停产" in text
    assert EVENT["desc"] in text


def test_sync_entity_index_writes_role_payload(monkeypatch, embedding_key):
    client = FakeQdrantClient()
    monkeypatch.setattr(
        entity_retrieval,
        "run_query",
        lambda *_: [{"node": EVENT}, {"node": REGION}],
    )
    monkeypatch.setattr(
        entity_retrieval,
        "embed_texts",
        lambda texts: [[0.1, 0.2] for _ in texts],
    )
    monkeypatch.setattr(entity_retrieval, "get_qdrant_client", lambda: client)

    count = entity_retrieval.sync_entity_index()

    assert count == 2
    payloads = [point.payload for point in client.upserted["points"]]
    assert payloads[0][entity_retrieval.ENTITY_ID_FIELD] == EVENT["id"]
    assert payloads[0][entity_retrieval.ROLE_PAYLOAD_FIELD] == EVENT["allowed_roles"]
    assert payloads[1][entity_retrieval.ENTITY_TYPE_FIELD] == "region"


# ---------------------------------------------------------------------------
# 向量检索
# ---------------------------------------------------------------------------


def test_search_entities_filters_by_role(monkeypatch, embedding_key):
    client = FakeQdrantClient(
        hits=[
            SimpleNamespace(
                payload={
                    entity_retrieval.ENTITY_ID_FIELD: EVENT["id"],
                    entity_retrieval.ROLE_PAYLOAD_FIELD: EVENT["allowed_roles"],
                },
                score=0.82,
            )
        ]
    )
    monkeypatch.setattr(entity_retrieval, "embed_texts", lambda texts: [[0.3, 0.4]])
    monkeypatch.setattr(entity_retrieval, "get_qdrant_client", lambda: client)

    hits = entity_retrieval.search_entities("枧下窝矿停了", "procurement")

    assert hits[0]["score"] == 0.82
    condition = client.query_filter.must[0]
    assert condition.key == entity_retrieval.ROLE_PAYLOAD_FIELD
    assert condition.match.any == ["procurement"]
    assert client.score_threshold == pytest.approx(
        entity_retrieval.settings.entity_vector_threshold
    )


def test_search_entities_admin_is_unfiltered(monkeypatch, embedding_key):
    client = FakeQdrantClient()
    monkeypatch.setattr(entity_retrieval, "embed_texts", lambda texts: [[0.3, 0.4]])
    monkeypatch.setattr(entity_retrieval, "get_qdrant_client", lambda: client)

    entity_retrieval.search_entities("枧下窝矿停了", "admin")

    assert client.query_filter is None


def test_search_without_embedding_key_returns_empty(monkeypatch):
    """没配密钥时不发远程请求，直接返回空让关键词链路继续。"""
    monkeypatch.setattr(entity_retrieval.settings, "embedding_api_key", "")
    monkeypatch.setattr(
        entity_retrieval, "embed_texts", lambda texts: (_ for _ in ()).throw(AssertionError("不应调用"))
    )

    assert entity_retrieval.search_entities("枧下窝矿停了", "procurement") == []


def test_sync_without_embedding_key_raises(monkeypatch):
    """索引同步失败要显式报错，由 seed 路由降级处理。"""
    monkeypatch.setattr(entity_retrieval.settings, "embedding_api_key", "")

    with pytest.raises(RuntimeError):
        entity_retrieval.sync_entity_index()


# ---------------------------------------------------------------------------
# 混合召回
# ---------------------------------------------------------------------------


def _patch_graph(monkeypatch, nodes, vector_hits):
    calls = []
    node_map = {node["id"]: node for node in nodes}

    def fake_search(question, role, limit=None):
        calls.append((question, role, limit))
        return vector_hits

    monkeypatch.setattr(graph, "_load_nodes", lambda role: list(nodes))
    monkeypatch.setattr(
        graph,
        "_nodes_by_ids",
        lambda entity_ids, role: {
            entity_id: node_map[entity_id]
            for entity_id in entity_ids
            if entity_id in node_map
        },
    )
    monkeypatch.setattr(graph, "search_entities", fake_search)
    return calls


def test_keyword_miss_falls_back_to_vector(monkeypatch):
    calls = _patch_graph(
        monkeypatch,
        [EVENT],
        [{entity_retrieval.ENTITY_ID_FIELD: EVENT["id"], "score": 0.8}],
    )

    entities = graph.match_entities("枧下窝矿停了会影响谁？", "procurement")

    assert [entity["id"] for entity in entities] == [EVENT["id"]]
    assert len(calls) == 1


def test_keyword_event_not_overridden_by_vector(monkeypatch):
    calls = _patch_graph(
        monkeypatch,
        [EVENT, REGION],
        [{entity_retrieval.ENTITY_ID_FIELD: REGION["id"], "score": 0.9}],
    )

    entities = graph.match_entities("宜春枧下窝锂矿停产影响谁？", "procurement")

    # 关键词命中事件，向量召回的地区被事件压制，不进入种子
    assert [entity["id"] for entity in entities] == [EVENT["id"]]
    assert len(calls) == 1


def test_vector_event_keeps_keyword_entities(monkeypatch):
    _patch_graph(
        monkeypatch,
        [REGION, EVENT],
        [
            {entity_retrieval.ENTITY_ID_FIELD: EVENT["id"], "score": 0.8},
            {entity_retrieval.ENTITY_ID_FIELD: REGION["id"], "score": 0.7},
        ],
    )

    entities = graph.match_entities("宁德时代最近情况如何？", "procurement")

    # 向量补出事件，但关键词命中的地区必须保留，否则整片地区路径会丢
    assert [entity["id"] for entity in entities] == [EVENT["id"], REGION["id"]]


def test_long_keyword_match_still_returns_keyword(monkeypatch):
    calls = _patch_graph(monkeypatch, [REGION], [])

    entities = graph.match_entities("中国福建（宁德）的情况如何？", "procurement")

    assert [entity["id"] for entity in entities] == [REGION["id"]]
    assert len(calls) == 1


def test_vector_failure_degrades_to_keyword_result(monkeypatch):
    monkeypatch.setattr(graph, "_load_nodes", lambda role: [])
    monkeypatch.setattr(
        graph, "search_entities", lambda question, role, limit=None: (_ for _ in ()).throw(RuntimeError("down"))
    )

    assert graph.match_entities("完全无关的问题", "procurement") == []


def test_rrf_fuse_deduplicates_and_boosts_overlap():
    fused = graph._rrf_fuse([EVENT, REGION], [EVENT], k=60)
    scores = {node["id"]: score for node, score in fused}

    assert len(fused) == 2
    assert scores[EVENT["id"]] > scores[REGION["id"]]


def test_rerank_paths_prefers_question_intent():
    def build(rule, name, severity="MEDIUM", hops=2):
        return {
            "rule": rule,
            "severity": severity,
            "hops": hops,
            "summary": name,
            "nodes": [{"id": name, "name": name, "type": "factory"}],
        }

    port_path = build("PORT", "厦门港")
    supply_path = build("SUPPLY", "富临精工")

    ranked = graph.rerank_paths([supply_path, port_path], "危险品舱位紧张对出货有什么影响？")

    assert ranked[0]["rule"] == "PORT"


def test_dedupe_paths_keeps_first_exact_duplicate():
    first = {
        "rule": "REGION",
        "severity": "HIGH",
        "hops": 2,
        "summary": "A → B",
        "nodes": [{"id": "A", "name": "A", "type": "event"}, {"id": "B", "name": "B", "type": "region"}],
    }
    duplicate = {**first, "summary": "重复"}

    assert graph.dedupe_paths([first, duplicate]) == [first]


def test_graph_evidence_limit_defaults_to_fifteen():
    from app.core.configuration import settings

    assert settings.graph_evidence_limit == 15
