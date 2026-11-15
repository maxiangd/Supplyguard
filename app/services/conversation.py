"""问答档案：PostgreSQL 永久保存每一轮风险问答。

与 Redis 短期会话的分工：
- Redis：最近 N 轮，供 LLM 理解追问，TTL 到期自动清理
- 本模块：完整问答档案，供记忆管理页回溯，不参与提示词注入

所有查询都按 username 过滤，用户只能看到自己的问答记录。
"""

import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.tables import ConversationTurn, User


def _dump(items) -> str:
    """列表统一存 JSON 字符串。"""
    return json.dumps(items or [], ensure_ascii=False)


def _parse(raw: str | None) -> list:
    """解析 JSON 字符串，失败时返回空列表。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _entity_brief(entities: list[dict] | None) -> list[dict]:
    """只保留实体的展示字段，避免把大段描述写进档案。"""
    return [
        {
            "id": entity.get("id"),
            "name": entity.get("name"),
            "type": entity.get("type"),
        }
        for entity in (entities or [])
        if entity.get("id")
    ]


def save_turn(
    db: Session,
    user: User,
    *,
    session_id: str,
    question: str,
    answer: str,
    resolved_question: str | None = None,
    entities: list[dict] | None = None,
    risk_level: str | None = None,
    risk_reason: str | None = None,
    approval_id: str | None = None,
    refs: list[dict] | None = None,
) -> ConversationTurn:
    """保存一轮问答，调用方负责 commit。"""
    turn = ConversationTurn(
        username=user.username,
        role=user.role,
        session_id=session_id or "default",
        question=question,
        resolved_question=resolved_question or question,
        answer=answer,
        risk_level=risk_level,
        risk_reason=risk_reason,
        approval_id=approval_id,
        entities=_dump(_entity_brief(entities)),
        refs=_dump(refs),
    )
    db.add(turn)
    db.flush()
    return turn


def _turn_to_dict(turn: ConversationTurn) -> dict:
    """转成前端会话详情使用的结构，与 Redis 短期轮次字段保持一致。"""
    entities = _parse(turn.entities)
    return {
        "turn_id": turn.turn_id,
        "session_id": turn.session_id,
        "role": turn.role,
        "question": turn.question,
        "resolved_question": turn.resolved_question or turn.question,
        "answer": turn.answer,
        "risk_level": turn.risk_level,
        "risk_reason": turn.risk_reason,
        "approval_id": turn.approval_id,
        "entity_ids": [item.get("id") for item in entities if item.get("id")],
        "entity_names": [item.get("name") for item in entities if item.get("name")],
        "refs": _parse(turn.refs),
        "timestamp": turn.created_at.timestamp() if turn.created_at else None,
    }


def list_turns(
    db: Session, user: User, session_id: str, limit: int = 200
) -> list[dict]:
    """按时间正序返回某个会话的全部问答。"""
    statement = (
        select(ConversationTurn)
        .where(
            ConversationTurn.username == user.username,
            ConversationTurn.session_id == session_id,
        )
        .order_by(ConversationTurn.seq.asc())
        .limit(limit)
    )
    return [_turn_to_dict(turn) for turn in db.scalars(statement)]


def list_sessions(db: Session, user: User, limit: int = 50) -> list[dict]:
    """按 session_id 聚合会话摘要，最近活动的排在前面。

    兼容 SQLite/PostgreSQL，先在 Python 里聚合最近 500 轮，
    演示规模下足够；数据量增大后可改为 SQL GROUP BY。
    """
    statement = (
        select(ConversationTurn)
        .where(ConversationTurn.username == user.username)
        .order_by(ConversationTurn.seq.desc())
        .limit(500)
    )

    summaries: dict[str, dict] = {}
    for turn in db.scalars(statement):
        if turn.session_id in summaries:
            summaries[turn.session_id]["turns"] += 1
            continue
        summaries[turn.session_id] = {
            "session_id": turn.session_id,
            "turns": 1,
            "last_question": turn.question,
            "last_answer": (turn.answer or "")[:120],
            "last_timestamp": (
                turn.created_at.timestamp() if turn.created_at else None
            ),
            "entity_names": [
                item.get("name")
                for item in _parse(turn.entities)
                if item.get("name")
            ],
            "risk_level": turn.risk_level,
        }

    return list(summaries.values())[:limit]


def delete_session(db: Session, user: User, session_id: str) -> int:
    """删除某个会话的全部问答，返回删除条数。"""
    result = db.execute(
        delete(ConversationTurn).where(
            ConversationTurn.username == user.username,
            ConversationTurn.session_id == session_id,
        )
    )
    db.flush()
    return result.rowcount or 0
