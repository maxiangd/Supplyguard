"""长期记忆：PostgreSQL 持久化的用户偏好、洞察、历史风险与笔记。

与 app/services/memory.py（Redis 短期会话）互补：
- 短期记忆自动追加/过期，用于多轮对话上下文
- 长期记忆由用户或系统显式保存，用于跨会话的知识积累

三类记忆在问答链路里的用途：
- preference：用户偏好，每次都注入提示词，调整回答侧重点
- risk：历史风险结论，按实体重合度挑选后注入，避免重复分析
- insight/note：人工沉淀的洞察与笔记（含问答页「记住这条」），按相关度注入
"""

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.configuration import settings
from app.db.tables import LongTermMemory, User
from app.services.summarize import summarize_answer

CATEGORY_PREFERENCE = "preference"
CATEGORY_RISK = "risk"
CATEGORY_INSIGHT = "insight"
CATEGORY_NOTE = "note"

# 同一组实体在 24 小时内的重复风险结论只保留一条
RISK_DEDUPE_WINDOW_SECONDS = 24 * 3600

RISK_LEVEL_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _parse_evidence(evidence: str | None) -> dict:
    """evidence 统一存 JSON 字符串，解析失败时返回空字典。"""
    if not evidence:
        return {}
    try:
        data = json.loads(evidence)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _dump_evidence(data: dict) -> str:
    """把结构化证据压缩成 JSON 字符串。"""
    return json.dumps(data, ensure_ascii=False)


def _evidence_entity_ids(memory: LongTermMemory) -> set[str]:
    """从 evidence 里取出实体 ID 集合，用于相关性排序与去重。"""
    evidence = _parse_evidence(memory.evidence)
    return {str(item) for item in (evidence.get("entity_ids") or [])}



def save_memory(
    db: Session,
    username: str,
    role: str,
    category: str,
    title: str,
    content: str,
    evidence: str | None = None,
) -> LongTermMemory:
    """创建一条长期记忆记录。"""
    memory = LongTermMemory(
        username=username,
        role=role,
        category=category,
        title=title,
        content=content,
        evidence=evidence,
    )
    db.add(memory)
    db.flush()
    return memory


def list_memories(
    db: Session,
    user: User,
    category: str | None = None,
    limit: int = 50,
) -> list[LongTermMemory]:
    """按角色查看长期记忆：admin 全见，其他角色只看自己的。"""
    statement = select(LongTermMemory).order_by(LongTermMemory.updated_at.desc())

    if user.role != "admin":
        statement = statement.where(LongTermMemory.username == user.username)

    if category:
        statement = statement.where(LongTermMemory.category == category)

    statement = statement.limit(limit)
    return list(db.scalars(statement))


def get_memory(db: Session, memory_id: str) -> LongTermMemory | None:
    """按 ID 取单条记忆。"""
    return db.get(LongTermMemory, memory_id)


def update_memory(
    db: Session,
    memory_id: str,
    user: User,
    title: str | None = None,
    content: str | None = None,
    category: str | None = None,
) -> LongTermMemory | None:
    """更新一条长期记忆。admin 可改任意，其他角色只能改自己的。"""
    memory = db.get(LongTermMemory, memory_id)
    if memory is None:
        return None
    if user.role != "admin" and memory.username != user.username:
        return None

    if title is not None:
        memory.title = title
    if content is not None:
        memory.content = content
    if category is not None:
        memory.category = category

    db.flush()
    return memory


def delete_memory(db: Session, memory_id: str, user: User) -> bool:
    """删除一条长期记忆。admin 可删任意，其他角色只能删自己的。"""
    memory = db.get(LongTermMemory, memory_id)
    if memory is None:
        return False
    if user.role != "admin" and memory.username != user.username:
        return False

    db.delete(memory)
    db.flush()
    return True


# ---------------------------------------------------------------------------
# 问答链路：读取长期记忆
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    """提示词里只放摘要，避免单条记忆挤占上下文。"""
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def load_memory_context(
    db: Session,
    user: User,
    question: str | None = None,
    entity_ids: list[str] | None = None,
    limit: int = 5,
) -> str:
    """挑选与当前问题相关的长期记忆，拼成提示词片段。

    安全边界：
    - 只读取 username 与 role 都匹配当前用户的记忆
    - admin 在问答里也只注入自己的记忆（管理页的“全见”不影响问答）

    排序规则：实体重合数 → 标题命中 → 风险等级 → 更新时间。
    """
    if not settings.memory_long_term_enabled:
        return ""

    try:
        base = select(LongTermMemory).where(
            LongTermMemory.username == user.username,
            LongTermMemory.role == user.role,
        )
        preferences = list(
            db.scalars(
                base.where(LongTermMemory.category == CATEGORY_PREFERENCE)
                .order_by(LongTermMemory.updated_at.desc())
                .limit(limit)
            )
        )
        candidates = list(
            db.scalars(
                base.where(
                    LongTermMemory.category.in_(
                        (CATEGORY_RISK, CATEGORY_INSIGHT, CATEGORY_NOTE)
                    )
                )
                .order_by(LongTermMemory.updated_at.desc())
                .limit(50)
            )
        )
    except Exception:
        # 数据库异常时不影响问答主链路
        return ""

    wanted = {str(item) for item in (entity_ids or [])}
    question_text = (question or "").strip()

    def rank(memory: LongTermMemory) -> tuple:
        evidence = _parse_evidence(memory.evidence)
        overlap = len(wanted & _evidence_entity_ids(memory))
        keyword = 1 if question_text and memory.title in question_text else 0
        level = RISK_LEVEL_RANK.get(
            str(evidence.get("risk_level", "")).upper(), 0
        )
        return (overlap, keyword, level, memory.updated_at)

    candidates.sort(key=rank, reverse=True)
    selected = candidates[:limit]

    sections: list[str] = []
    if preferences:
        lines = [f"- {m.title}：{_truncate(m.content, 200)}" for m in preferences]
        sections.append(
            "用户偏好（回答时优先考虑，但不得违背证据规则）：\n" + "\n".join(lines)
        )

    if selected:
        lines = []
        for memory in selected:
            evidence = _parse_evidence(memory.evidence)
            level = evidence.get("risk_level")
            # 自动沉淀的标题本身已带 [LEVEL]，避免重复加前缀
            tag = f"[{level}] " if level and not memory.title.startswith("[") else ""
            entity_names = evidence.get("entity_names") or []
            entity_hint = (
                f"（涉及：{'、'.join(entity_names)}）" if entity_names else ""
            )
            lines.append(
                f"- {tag}{memory.title}{entity_hint}：{_truncate(memory.content, 200)}"
            )
        sections.append(
            "历史记忆（可能与当前问题相关，仅作背景，不作为事实证据）：\n"
            + "\n".join(lines)
        )

    if not sections:
        return ""
    return "长期记忆：\n" + "\n\n".join(sections)


# ---------------------------------------------------------------------------
# 问答链路：写入长期记忆
# ---------------------------------------------------------------------------


def save_risk_memory(
    db: Session,
    user: User,
    question: str,
    answer: str,
    risk_level: str,
    risk_reason: str = "",
    entities: list[dict] | None = None,
    approval_id: str | None = None,
    refs: list[dict] | None = None,
) -> LongTermMemory | None:
    """把 MEDIUM/HIGH 风险结论自动沉淀为历史风险记忆。

    去重规则：同一用户、同一组实体、24 小时内只保留一条，重复时覆盖旧记录。
    实体为空时退化为“同一问题文本”去重，避免无关问题被错误合并。
    """
    if risk_level not in ("HIGH", "MEDIUM"):
        return None

    entity_list = entities or []
    entity_ids = [entity.get("id") for entity in entity_list if entity.get("id")]
    entity_names = [
        entity.get("name") for entity in entity_list if entity.get("name")
    ]

    evidence = {
        "source": "auto",
        "question": question,
        "entity_ids": entity_ids,
        "entity_names": entity_names,
        "risk_level": risk_level,
        "risk_reason": risk_reason,
        "approval_id": approval_id,
        "refs": refs or [],
    }
    # 标题用涉及的实体或问题，正文只留一句结论，避免和完整会话重复
    subject = "、".join(entity_names[:2]) or question[:40]
    title = f"[{risk_level}] {subject}"[:255]
    content = (
        summarize_answer(question, answer, risk_level, risk_reason)
        or question[:200]
    )

    now = datetime.now()
    wanted = set(entity_ids)
    try:
        recent = list(
            db.scalars(
                select(LongTermMemory)
                .where(
                    LongTermMemory.username == user.username,
                    LongTermMemory.category == CATEGORY_RISK,
                )
                .order_by(LongTermMemory.updated_at.desc())
                .limit(50)
            )
        )
    except Exception:
        recent = []

    for memory in recent:
        age = (
            (now - memory.created_at).total_seconds()
            if memory.created_at
            else RISK_DEDUPE_WINDOW_SECONDS + 1
        )
        if age > RISK_DEDUPE_WINDOW_SECONDS:
            continue

        existing = _parse_evidence(memory.evidence)
        existing_ids = {str(item) for item in (existing.get("entity_ids") or [])}
        same_entities = bool(wanted) and existing_ids == wanted
        same_question = not wanted and existing.get("question") == question
        if same_entities or same_question:
            memory.title = title
            memory.content = content
            memory.evidence = _dump_evidence(evidence)
            db.flush()
            return memory

    memory = LongTermMemory(
        username=user.username,
        role=user.role,
        category=CATEGORY_RISK,
        title=title,
        content=content,
        evidence=_dump_evidence(evidence),
    )
    db.add(memory)
    db.flush()
    return memory


def list_risk_memories(
    db: Session,
    user: User,
    entity_id: str | None = None,
    level: str | None = None,
    limit: int = 50,
) -> list[LongTermMemory]:
    """按实体或风险等级查询历史风险。

    可见性沿用 list_memories：admin 全见，其他角色只看自己的。
    过滤字段存在 evidence JSON 里，因此先取最近一段再在内存中筛选。
    """
    statement = (
        select(LongTermMemory)
        .where(LongTermMemory.category == CATEGORY_RISK)
        .order_by(LongTermMemory.created_at.desc())
    )
    if user.role != "admin":
        statement = statement.where(LongTermMemory.username == user.username)

    rows = list(db.scalars(statement.limit(200)))
    result: list[LongTermMemory] = []
    for memory in rows:
        evidence = _parse_evidence(memory.evidence)
        if entity_id:
            ids = {str(item) for item in (evidence.get("entity_ids") or [])}
            if entity_id not in ids:
                continue
        if level:
            if str(evidence.get("risk_level", "")).upper() != level.upper():
                continue
        result.append(memory)
        if len(result) >= limit:
            break
    return result


def compact_memories(db: Session, limit: int | None = None) -> int:
    """把历史遗留的长篇长期记忆压缩成摘要，返回更新条数。

    只处理 risk / note 两类；preference 是用户自己写的，保持原样。
    limit 默认取 APP_MEMORY_SUMMARY_MAX_CHARS。
    """
    limit = limit or settings.memory_summary_max_chars
    statement = select(LongTermMemory).where(
        LongTermMemory.category.in_((CATEGORY_RISK, CATEGORY_NOTE))
    )
    updated = 0
    for memory in db.scalars(statement):
        changed = False
        evidence = _parse_evidence(memory.evidence)
        level = str(evidence.get("risk_level") or "").upper()
        question = evidence.get("question") or memory.title

        # 已经足够短的不再重复调用模型
        if len(memory.content) > limit:
            summary = summarize_answer(
                question,
                memory.content,
                level or None,
                evidence.get("risk_reason"),
                limit=limit,
            )
            if summary and summary != memory.content:
                memory.content = summary
                changed = True

        if memory.category == CATEGORY_RISK:
            names = evidence.get("entity_names") or []
            subject = "、".join(names[:2]) or question[:40]
            title = f"[{level}] {subject}"[:255] if level else memory.title[:255]
            if title != memory.title:
                memory.title = title
                changed = True

        if changed:
            updated += 1

    db.flush()
    return updated


def save_chat_note(
    db: Session,
    user: User,
    question: str,
    answer: str,
    session_id: str | None = None,
    refs: list[dict] | None = None,
) -> LongTermMemory:
    """把「记住这条」保存为简短结论，而不是整段回答。"""
    summary = summarize_answer(question, answer) or question[:200]
    memory = LongTermMemory(
        username=user.username,
        role=user.role,
        category=CATEGORY_NOTE,
        title=(question.strip() or "对话结论")[:255],
        content=summary or question[:200],
        evidence=_dump_evidence(
            {"source": "chat", "session_id": session_id, "refs": refs or []}
        ),
    )
    db.add(memory)
    db.flush()
    return memory


if __name__ == "__main__":
    # 一次性压缩历史长期记忆：python -m app.services.long_term
    from app.db.database import SessionLocal

    with SessionLocal() as session:
        count = compact_memories(session)
        session.commit()
        print(f"已压缩 {count} 条长期记忆")
