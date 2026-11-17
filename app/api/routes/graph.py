"""图谱路由：角色子图、多跳影响分析与演示数据导入。"""

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger

from app.api.dependencies import get_current_user, require_roles
from app.db.graph_database import graph_is_ready
from app.db.tables import User
from app.models.api_models import (
    GraphImpactRequest,
    GraphImpactResponse,
    GraphOverviewResponse,
    GraphSeedResponse,
)
from app.services import graph
from app.services.entity_retrieval import sync_entity_index
from app.services.graph_seed import seed_graph

router = APIRouter(prefix="/graph", tags=["graph"])


def _require_neo4j() -> None:
    """Neo4j 未启动时返回 503，而不是把连接异常抛成 500。"""
    if not graph_is_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="图谱服务不可用，请检查 Neo4j 是否启动",
        )


@router.get("/overview", response_model=GraphOverviewResponse)
def graph_overview(
    current_user: User = Depends(get_current_user),
) -> GraphOverviewResponse:
    """返回当前角色可见的供应链子图。"""
    _require_neo4j()
    return GraphOverviewResponse(**graph.overview(current_user.role))


@router.post("/impact", response_model=GraphImpactResponse)
def graph_impact(
    payload: GraphImpactRequest,
    current_user: User = Depends(get_current_user),
) -> GraphImpactResponse:
    """按问题或实体 ID 做多跳影响分析。"""
    _require_neo4j()
    if not payload.question and not payload.entity_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="question 与 entity_id 至少提供一个",
        )
    return GraphImpactResponse(
        **graph.impact(
            payload.question, payload.entity_id, current_user.role, payload.max_hops
        )
    )


@router.post("/seed", response_model=GraphSeedResponse)
def graph_seed_import(
    _current_user: User = Depends(require_roles("admin")),
) -> GraphSeedResponse:
    """管理员导入或刷新演示图谱，可重复执行。"""
    _require_neo4j()
    summary = seed_graph()

    # 图谱是实体索引的唯一数据源；索引失败不回滚图谱，只记录降级
    try:
        summary["entities_indexed"] = sync_entity_index()
    except Exception as exc:
        logger.warning("实体向量索引同步失败，语义兜底暂不可用：{}", exc)
        summary["entities_indexed"] = 0

    return GraphSeedResponse(**summary)
