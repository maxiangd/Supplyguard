"""问答档案：PostgreSQL 永久保存每一轮风险问答。

与 Redis 短期会话的分工：
- Redis：最近 N 轮，供 LLM 理解追问，TTL 到期自动清理
- 本模块：完整问答档案，供记忆管理页回溯，不参与提示词注入

所有查询都按 username 过滤，用户只能看到自己的问答记录。
"""

import json

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.configuration import settings
from app.db.tables import ConversationSummary, ConversationTurn, User
from app.services.summarize import summarize_conversation


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


# ---------------------------------------------------------------------------
# 滚动摘要：超过最近 N 轮的旧对话压缩成一段摘要
# ---------------------------------------------------------------------------


def get_summary(
    db: Session, user: User, session_id: str
) -> ConversationSummary | None:
    """取某个会话已有的滚动摘要。"""
    return db.scalar(
        select(ConversationSummary).where(
            ConversationSummary.username == user.username,
            ConversationSummary.session_id == session_id,
        )
    )


def _upsert_summary(
    db: Session,
    user: User,
    session_id: str,
    summary: str,
    covered_seq: int,
) -> ConversationSummary:
    """写入或更新滚动摘要。"""
    row = get_summary(db, user, session_id)
    if row is None:
        row = ConversationSummary(
            username=user.username,
            session_id=session_id,
            summary=summary,
            covered_seq=covered_seq,
        )
        db.add(row)
    else:
        row.summary = summary
        row.covered_seq = covered_seq
    db.flush()
    return row


def load_rolling_summary(
    db: Session, user: User, session_id: str, keep_turns: int | None = None
) -> str:
    """返回「最近 N 轮之前」的会话摘要，并按需增量更新。

    调用时机是每次问答准备上下文时：
    - 总轮次不超过 N：直接返回已有摘要（可能为空）
    - 有新的溢出轮次：只总结 covered_seq 之后的部分，合并进旧摘要
    - LLM 不可用时由 summarize_conversation 内部降级为规则摘要
    """
    if not settings.memory_rolling_summary_enabled:
        return ""

    keep = keep_turns or settings.memory_max_turns
    conditions = (
        ConversationTurn.username == user.username,
        ConversationTurn.session_id == session_id,
    )

    try:
        total = (
            db.scalar(
                select(func.count()).select_from(ConversationTurn).where(*conditions)
            )
            or 0
        )
        existing = get_summary(db, user, session_id)
        if total <= keep:
            return existing.summary if existing else ""

        # 最近 keep 轮里最小的 seq，比它更早的都属于溢出部分
        recent_seqs = list(
            db.scalars(
                select(ConversationTurn.seq)
                .where(*conditions)
                .order_by(ConversationTurn.seq.desc())
                .limit(keep)
            )
        )
        if not recent_seqs:
            return existing.summary if existing else ""

        cutoff = min(recent_seqs)
        covered = existing.covered_seq if existing else 0

        overflow = list(
            db.scalars(
                select(ConversationTurn)
                .where(
                    *conditions,
                    ConversationTurn.seq < cutoff,
                    ConversationTurn.seq > covered,
                )
                .order_by(ConversationTurn.seq.asc())
            )
        )
        if not overflow:
            return existing.summary if existing else ""

        summary = summarize_conversation(
            existing.summary if existing else "",
            [_turn_to_dict(turn) for turn in overflow],
            limit=settings.memory_session_summary_max_chars,
        )
        if not summary:
            return existing.summary if existing else ""

        _upsert_summary(db, user, session_id, summary, overflow[-1].seq)
        return summary
    except Exception:
        # 摘要失败不能影响主链路，能拿到旧摘要就返回旧摘要
        try:
            existing = get_summary(db, user, session_id)
            return existing.summary if existing else ""
        except Exception:
            return ""


def backfill_from_redis(
    db: Session, username: str | None = None
) -> int:
    """把 Redis 中尚未过期的短期会话补录到永久档案。

    用于上线本表之前已经产生的问答；重复执行不会重复写入：
    以 username + session_id + question + answer 判重。
    """
    from app.services import memory as memory_service

    try:
        client = memory_service._redis()
        keys = list(
            client.scan_iter(match=f"{memory_service.SESSION_KEY_PREFIX}:*")
        )
    except Exception:
        return 0

    created = 0
    for key in keys:
        parts = key.split(":")
        # 格式：supplyguard:session:{username}:{session_id}
        if len(parts) < 4:
            continue
        owner, session_id = parts[2], parts[3]
        if username and owner != username:
            continue

        for item in client.lrange(key, 0, -1):
            try:
                turn = json.loads(item)
            except (json.JSONDecodeError, TypeError):
                continue
            question = turn.get("question", "")
            answer = turn.get("answer", "")

            exists = db.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.username == owner,
                    ConversationTurn.session_id == session_id,
                    ConversationTurn.question == question,
                    ConversationTurn.answer == answer,
                )
            )
            if exists:
                continue

            entity_ids = turn.get("entity_ids") or []
            entity_names = turn.get("entity_names") or []
            entities = [
                {"id": entity_id, "name": entity_names[index] if index < len(entity_names) else entity_id, "type": None}
                for index, entity_id in enumerate(entity_ids)
            ]

            db.add(
                ConversationTurn(
                    username=owner,
                    role=turn.get("role", ""),
                    session_id=session_id,
                    question=question,
                    resolved_question=turn.get("resolved_question") or question,
                    answer=answer,
                    risk_level=turn.get("risk_level"),
                    risk_reason=turn.get("risk_reason"),
                    approval_id=turn.get("approval_id"),
                    entities=_dump(entities),
                    refs=_dump(turn.get("refs")),
                )
            )
            created += 1

    db.flush()
    return created


if __name__ == "__main__":
    # 一次性迁移：python -m app.services.conversation
    from app.db.database import SessionLocal

    with SessionLocal() as session:
        count = backfill_from_redis(session)
        session.commit()
        print(f"已从 Redis 补录 {count} 轮问答到 conversation_turns")

