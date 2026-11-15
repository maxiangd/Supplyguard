"""多轮追问解析：把「那它呢？」这类省略问题补全成可检索、可图谱展开的上下文。

为什么需要：
- 检索（Qdrant）和图谱（Neo4j）只拿到当前问题文本
- 追问里往往没有实体名，直接检索会命中不到上一轮的对象
- 本模块在进入工作流之前，判断当前问题是否依赖上一轮，
  若依赖则继承上一轮实体（给图谱）并改写检索词（给 Qdrant）

策略：规则优先，不额外调用 LLM。
- 当前问题能命中实体 → 视为独立新问题，不继承
- 问题很短或含指代词，且命中不到实体 → 继承上一轮
"""

from dataclasses import dataclass, field

from app.services.graph import match_entities

# 指代词：出现这些词通常意味着“接着上一轮说”
FOLLOWUP_MARKERS = (
    "它",
    "它们",
    "他",
    "她",
    "这个",
    "那个",
    "该",
    "上述",
    "其",
    "那么",
    "这两",
    "这种",
    "这次",
)

# 短问题（如「替代港口呢？」）默认按追问处理
SHORT_QUESTION_MAX_LENGTH = 12


@dataclass
class FollowupContext:
    """一次追问解析的结果。"""

    question: str
    """用户原始问题"""

    resolved_question: str
    """补全后的问题，供检索与图谱使用；= question 表示无需补全"""

    carried_entity_ids: list[str] = field(default_factory=list)
    """从上一轮继承的实体 ID，供图谱展开"""

    carried_entity_names: list[str] = field(default_factory=list)
    """从上一轮继承的实体名称，用于拼接检索词和提示词"""

    is_followup: bool = False
    """是否判定为追问"""


def is_followup_question(question: str, has_history: bool) -> bool:
    """判断问题形态是否像追问。

    没有历史时一律不是追问；有历史时，短问题或含指代词视为追问。
    最终是否真的继承，还要看当前问题能否命中实体（见 resolve_followup）。
    """
    if not has_history:
        return False

    text = question.strip()
    if len(text) <= SHORT_QUESTION_MAX_LENGTH:
        return True
    return any(marker in text for marker in FOLLOWUP_MARKERS)


def resolve_followup(question: str, role: str, history: list[dict]) -> FollowupContext:
    """解析当前问题是否依赖上一轮对话。

    参数：
        question: 用户当前问题
        role: 当前角色（实体匹配按角色可见性过滤）
        history: Redis 短期记忆（从旧到新）

    返回：
        FollowupContext。无需继承时 resolved_question == question。
    """
    context = FollowupContext(question=question, resolved_question=question)

    if not history or not is_followup_question(question, True):
        return context

    # 只有疑似追问时才查图谱，避免每个独立问题都多一次实体匹配
    try:
        matched = match_entities(question, role)
    except Exception:
        # Neo4j 不可用时无法判断，保守起见不继承，按普通问题处理
        return context

    if matched:
        # 当前问题自己就能命中实体，说明是新话题
        return context

    last = history[-1]
    entity_ids = [eid for eid in (last.get("entity_ids") or []) if eid]
    entity_names = [name for name in (last.get("entity_names") or []) if name]

    context.is_followup = True
    context.carried_entity_ids = entity_ids
    context.carried_entity_names = entity_names

    # 检索词优先用实体名补全；没有实体名时退回到上一轮问题文本
    if entity_names:
        prefix = "、".join(entity_names)
    else:
        prefix = (last.get("question") or "")[:100]

    if prefix:
        context.resolved_question = f"{prefix} {question}"

    return context
