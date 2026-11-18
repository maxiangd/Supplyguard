"""图谱实体向量索引：关键词召回不到时，用语义相似度补充候选。

索引只保存实体的展示文本，不复制图结构。命中后仍按实体 ID 回 Neo4j
取节点，可见性由查询过滤和调用方二次校验共同保证。
"""

import uuid
from typing import Any

from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from app.core.configuration import settings
from app.db.graph_database import run_query
from app.services.embeddings import embed_texts
from app.services.retrieval import get_qdrant_client

ENTITY_ID_FIELD = "entity_id"
ENTITY_TYPE_FIELD = "entity_type"
ROLE_PAYLOAD_FIELD = "allowed_roles"

TYPE_LABELS = {
    "event": "风险事件",
    "supplier": "供应商",
    "factory": "工厂",
    "port": "港口",
    "route": "航线",
    "region": "地区",
}


def entity_document(node: dict) -> str:
    """把节点拼成用于向量化的文本，别名越全召回越稳。"""
    parts = [node.get("name", "")]
    aliases = node.get("aliases") or []
    if aliases:
        parts.append(f"别名：{'、'.join(aliases)}")
    entity_type = node.get("type")
    if entity_type:
        parts.append(f"类型：{TYPE_LABELS.get(entity_type, entity_type)}")
    if node.get("desc"):
        parts.append(node["desc"])
    return "；".join(part for part in parts if part)


def ensure_collection() -> None:
    """实体索引集合不存在时按配置维度创建。"""
    client = get_qdrant_client()
    if not client.collection_exists(settings.qdrant_entity_collection):
        client.create_collection(
            collection_name=settings.qdrant_entity_collection,
            vectors_config=VectorParams(
                size=settings.embedding_dimension, distance=Distance.COSINE
            ),
        )


def _point_id(entity_id: str) -> str:
    """把业务 ID 转成稳定的 UUID，重复导入时覆盖同一个点。"""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, entity_id))


def sync_entity_index() -> int:
    """全量重建实体索引，返回写入的实体数。

    以 Neo4j 为唯一数据源，Qdrant 只做派生索引；失败时抛异常，
    由调用方决定是否降级，避免索引坏了还假装同步成功。
    """
    if not settings.embedding_api_key:
        raise RuntimeError("未配置 APP_EMBEDDING_API_KEY，无法建立实体索引")

    records = run_query("MATCH (n:Entity) RETURN n AS node ORDER BY n.id")
    nodes = [record["node"] for record in records]
    if not nodes:
        return 0

    ensure_collection()
    vectors = embed_texts([entity_document(node) for node in nodes])
    points = [
        PointStruct(
            id=_point_id(node["id"]),
            vector=vector,
            payload={
                ENTITY_ID_FIELD: node["id"],
                ENTITY_TYPE_FIELD: node.get("type"),
                ROLE_PAYLOAD_FIELD: node.get("allowed_roles") or [],
                "name": node.get("name", ""),
            },
        )
        for node, vector in zip(nodes, vectors)
    ]

    client = get_qdrant_client()
    client.upsert(
        collection_name=settings.qdrant_entity_collection,
        points=points,
        wait=True,
    )

    # 图里已删除的节点不能继续被召回，按当前实体 ID 清理残留点
    current_ids = {node["id"] for node in nodes}
    existing, _ = client.scroll(
        collection_name=settings.qdrant_entity_collection,
        limit=10000,
        with_payload=True,
    )
    stale = [
        point.id
        for point in existing
        if (point.payload or {}).get(ENTITY_ID_FIELD) not in current_ids
    ]
    if stale:
        client.delete(
            collection_name=settings.qdrant_entity_collection,
            points_selector=PointIdsList(points=stale),
            wait=True,
        )

    return len(points)


def search_entities(
    query: str, role: str, limit: int | None = None
) -> list[dict[str, Any]]:
    """按语义相似度召回候选实体，非管理员只查角色可见的点。

    返回 payload 加相似度分数，实体是否真的可见仍由调用方回图校验。
    """
    if not query.strip() or not settings.embedding_api_key:
        return []

    vector = embed_texts([query])[0]

    query_filter = None
    if role != "admin":
        query_filter = Filter(
            must=[
                FieldCondition(
                    key=ROLE_PAYLOAD_FIELD, match=MatchAny(any=[role])
                )
            ]
        )

    response = get_qdrant_client().query_points(
        collection_name=settings.qdrant_entity_collection,
        query=vector,
        query_filter=query_filter,
        limit=limit or settings.entity_vector_limit,
        score_threshold=settings.entity_vector_threshold,
        with_payload=True,
    )

    hits = []
    for point in response.points:
        payload = dict(point.payload or {})
        payload["score"] = point.score
        hits.append(payload)
    return hits


if __name__ == "__main__":
    print(f"indexed {sync_entity_index()} entities")
