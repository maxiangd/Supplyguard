"""Redis 短期会话记忆：保存最近 N 轮对话，注入到后续问答的提示词中。

存储格式（每个用户 + 每个会话一个 Redis List）：
  Key:   supplyguard:session:{username}:{session_id}
  Value: JSON 列表，每项一轮对话（见 add_turn 的结构）
  TTL:   由 settings.memory_session_ttl_seconds 控制，每次写入刷新
  轮次上限: 由 settings.memory_max_turns 控制，用 LTRIM 裁剪

为什么要 session_id：
- 同一个账号可能同时开多个标签页或多次独立咨询
- 只按 username 存会让不同会话的上下文互相污染
- 老客户端不传 session_id 时落到 default 会话，行为与旧版兼容

为什么用 Redis List 而不是一个 JSON 字符串：
- RPUSH + LTRIM + EXPIRE 一次 pipeline 完成，避免“读-改-写”的并发覆盖
- 轮次很多时也不需要每次反序列化整个列表再写回
"""

import json
import re
import time

import redis

from app.core.configuration import settings

SESSION_KEY_PREFIX = "supplyguard:session"
DEFAULT_SESSION_ID = "default"

# session_id 来自前端，必须限制字符集，避免拼出任意 Redis key
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _redis() -> redis.Redis:
    """返回 Redis 连接（每次新建，自动从连接池复用）。"""
    return redis.from_url(settings.redis_url, decode_responses=True)


def normalize_session_id(session_id: str | None) -> str:
    """校验并归一化 session_id：非法或缺失时回落到 default。"""
    if session_id and _SESSION_ID_PATTERN.match(session_id):
        return session_id
    return DEFAULT_SESSION_ID


def _session_key(username: str, session_id: str) -> str:
    """用户某个会话的 Redis key。"""
    return f"{SESSION_KEY_PREFIX}:{username}:{normalize_session_id(session_id)}"


def _legacy_session_key(username: str) -> str:
    """旧版本不带 session_id 的 key，仅用于读取历史数据。"""
    return f"{SESSION_KEY_PREFIX}:{username}"


def get_session_history(username: str, session_id: str = DEFAULT_SESSION_ID) -> list[dict]:
    """读取某个会话最近的对话轮次，按时间从旧到新返回。"""
    try:
        r = _redis()
        raw_items = r.lrange(_session_key(username, session_id), 0, -1)
        history = []
        for item in raw_items:
            try:
                history.append(json.loads(item))
            except json.JSONDecodeError:
                continue

        # 兼容旧版：default 会话没有新格式数据时，尝试读旧的 JSON 字符串 key
        if not history and normalize_session_id(session_id) == DEFAULT_SESSION_ID:
            legacy_raw = r.get(_legacy_session_key(username))
            if legacy_raw:
                try:
                    legacy = json.loads(legacy_raw)
                    if isinstance(legacy, list):
                        history = legacy
                except json.JSONDecodeError:
                    history = []

        max_turns = settings.memory_max_turns
        return history[-max_turns:] if len(history) > max_turns else history
    except Exception:
        # Redis 不可用时静默降级：没有历史也不影响问答主链路
        return []


def add_turn(
    username: str,
    role: str,
    question: str,
    answer: str,
    session_id: str = DEFAULT_SESSION_ID,
    *,
    resolved_question: str | None = None,
    entities: list[dict] | None = None,
    risk_level: str | None = None,
    risk_reason: str | None = None,
    approval_id: str | None = None,
    refs: list[dict] | None = None,
) -> None:
    """在会话末尾追加一轮对话，自动裁剪轮次并刷新 TTL。

    entities 存的是本轮命中的图谱实体（id + name），供下一轮追问继承；
    risk_level / approval_id 用于在后续对话里提示“上一轮结论的风险状态”。
    """
    try:
        entity_list = entities or []
        turn = {
            "role": role,
            "question": question,
            "resolved_question": resolved_question or question,
            "answer": answer,
            "timestamp": time.time(),
            "entity_ids": [entity.get("id") for entity in entity_list if entity.get("id")],
            "entity_names": [
                entity.get("name") for entity in entity_list if entity.get("name")
            ],
            "risk_level": risk_level,
            "risk_reason": risk_reason,
            "approval_id": approval_id,
            "refs": refs or [],
        }

        key = _session_key(username, session_id)
        # RPUSH 追加到尾部（最新），LTRIM 只保留最近 N 条，EXPIRE 刷新有效期
        pipe = _redis().pipeline()
        pipe.rpush(key, json.dumps(turn, ensure_ascii=False))
        pipe.ltrim(key, -settings.memory_max_turns, -1)
        pipe.expire(key, settings.memory_session_ttl_seconds)
        pipe.execute()
    except Exception:
        # Redis 不可用时静默降级，不影响问答主链路
        pass


def get_latest_turn(username: str, session_id: str = DEFAULT_SESSION_ID) -> dict | None:
    """返回当前会话最近一轮对话，没有则返回 None。"""
    history = get_session_history(username, session_id)
    return history[-1] if history else None


def _session_summary(
    session_id: str, turns: int, last: dict, ttl: int | None = None
) -> dict:
    """把一轮对话压成会话列表项。"""
    return {
        "session_id": session_id,
        "turns": turns,
        "last_question": last.get("question", ""),
        "last_answer": (last.get("answer") or "")[:120],
        "last_timestamp": last.get("timestamp"),
        "entity_names": last.get("entity_names") or [],
        "risk_level": last.get("risk_level"),
        "ttl": ttl,
    }


def list_sessions(username: str) -> list[dict]:
    """列出该用户所有未过期会话，按最近活动倒序。

    记忆管理页用它展示「风险问答记录」，不再依赖前端传入的单个 session_id。
    只扫描 `...:{username}:*`，不会读到其他用户的 key。
    """
    try:
        redis_client = _redis()
        summaries: list[dict] = []

        for key in redis_client.scan_iter(match=f"{SESSION_KEY_PREFIX}:{username}:*"):
            session_id = key.rsplit(":", 1)[-1]
            items = redis_client.lrange(key, 0, -1)
            if not items:
                continue
            try:
                last = json.loads(items[-1])
            except json.JSONDecodeError:
                continue
            summaries.append(
                _session_summary(
                    session_id, len(items), last, redis_client.ttl(key)
                )
            )

        # 旧版不带 session_id 的 default 会话
        legacy_raw = redis_client.get(_legacy_session_key(username))
        if legacy_raw:
            try:
                legacy = json.loads(legacy_raw)
            except json.JSONDecodeError:
                legacy = []
            if isinstance(legacy, list) and legacy:
                summaries.append(
                    _session_summary(
                        DEFAULT_SESSION_ID,
                        len(legacy),
                        legacy[-1],
                        redis_client.ttl(_legacy_session_key(username)),
                    )
                )

        summaries.sort(
            key=lambda item: item.get("last_timestamp") or 0, reverse=True
        )
        return summaries
    except Exception:
        return []


def clear_session(username: str, session_id: str = DEFAULT_SESSION_ID) -> bool:
    """删除某个会话的历史。返回 True 表示删除成功。"""
    try:
        _redis().delete(_session_key(username, session_id))
        return True
    except Exception:
        return False


def build_memory_context(history: list[dict]) -> str:
    """把历史轮次格式化为提示词片段，供 build_context_node 注入。

    返回空字符串表示无历史（调用方直接跳过）。
    """
    if not history:
        return ""

    lines = ["对话历史（最近几轮问答，可用于理解当前问题的上下文）："]
    for index, turn in enumerate(history, start=1):
        entity_names = turn.get("entity_names") or []
        entity_hint = f"（涉及：{'、'.join(entity_names)}）" if entity_names else ""
        lines.append(f"[{index}] 用户：{turn.get('question', '')}{entity_hint}")

        # 回答截断到 300 字，避免提示词过长
        answer = turn.get("answer", "")
        if len(answer) > 300:
            answer = answer[:300] + "..."
        lines.append(f"    助手：{answer}")

        if turn.get("risk_level"):
            lines.append(
                f"    （上一轮风险等级：{turn['risk_level']}，"
                f"审批单：{turn.get('approval_id') or '无'}）"
            )

    lines.append("（以上为历史记录，以下为当前问题）")
    return "\n".join(lines)
