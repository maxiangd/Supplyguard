"""单轮问答：LLM 调用与流式生成，配合 LangGraph 工作流编排。

职责分离：
- workflow.py 负责状态机编排（检索→图谱→复核→提示词拼装）
- 本模块负责 LLM 调用、流式生成，以及注入三个依赖 LLM/DB 的节点
- chat.py 路由层负责 SSE 事件序列化
"""

import json

from openai import OpenAI

from langgraph.types import Command

from app.core.configuration import settings
from app.db.tables import User
from app.services.approval import create_approval
from app.services.conversation import save_turn as save_conversation_turn
from app.services.followup import FollowupContext, resolve_followup
from app.services.long_term import load_memory_context, save_risk_memory
from app.services.memory import add_turn, build_memory_context, get_session_history
from app.services.workflow import (
    SupplyGuardState,
    await_decision,
    register_risk_and_approval_nodes,
    submit_decision,
)

# 拒答判定用这个字符串，只匹配整段回答以此开头的
REFUSAL_TEXT = "知识库中暂无相关资料"

RISK_ASSESSMENT_PROMPT = """你是供应链风险审核专家。根据以下信息，判断该 AI 回答涉及的风险等级。

证据资料：
{context}

图谱影响路径：
{graph_context}

用户问题：{question}

AI 回答：{answer}

请只输出一个 JSON 对象，不要包含其他内容：
{{"level": "HIGH 或 MEDIUM 或 LOW", "reason": "简述判断依据，不超过 100 字"}}

风险等级定义：
- HIGH：结论涉及供应中断、停产、召回、制裁、重大合规风险，可能影响多个基地或客户交付
- MEDIUM：价格/成本波动、单一供应商依赖、区域性物流延误、合规要求变化
- LOW：常规信息查询、历史回顾、不涉及实质性风险变化"""


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _is_refusal(answer: str) -> bool:
    """只有整段回答以拒答语开头才算拒答，避免部分回答里顺带提到而被误判。"""
    return answer.strip().lstrip("*# >-").startswith(REFUSAL_TEXT)


def _prepare_memory(
    db, user: User, question: str, role: str, session_id: str
) -> tuple[list[dict], FollowupContext, str]:
    """加载本轮问答需要的全部记忆。

    顺序很重要：
    1. 先读 Redis 短期记忆
    2. 用短期记忆判断当前问题是不是追问，得到补全后的检索词和继承实体
    3. 再用补全后的问题去匹配 PostgreSQL 长期记忆
    """
    history = get_session_history(user.username, session_id)
    followup = resolve_followup(question, role, history)
    long_term_context = load_memory_context(
        db, user, followup.resolved_question, followup.carried_entity_ids
    )
    return history, followup, long_term_context


def _refs_from_hits(hits: list[dict], limit: int = 5) -> list[dict]:
    """把命中的文档块压缩成引用列表，随对话轮次一起存进短期记忆。"""
    return [
        {
            "filename": hit.get("filename"),
            "chunk_index": hit.get("chunk_index"),
        }
        for hit in (hits or [])[:limit]
    ]


def _record_turn(
    db,
    user: User,
    session_id: str,
    question: str,
    answer: str,
    *,
    resolved_question: str | None = None,
    entities: list[dict] | None = None,
    risk_level: str | None = None,
    risk_reason: str | None = None,
    approval_id: str | None = None,
    refs: list[dict] | None = None,
) -> None:
    """同时写 Redis 短期记忆和 PostgreSQL 永久问答档案。

    Redis 失败已由 add_turn 内部静默降级；PostgreSQL 失败时回滚，
    不能因为档案写入失败而影响用户已经拿到的回答。
    """
    add_turn(
        user.username,
        user.role,
        question,
        answer,
        session_id=session_id,
        resolved_question=resolved_question,
        entities=entities,
        risk_level=risk_level,
        risk_reason=risk_reason,
        approval_id=approval_id,
        refs=refs,
    )
    try:
        save_conversation_turn(
            db,
            user,
            session_id=session_id,
            question=question,
            answer=answer,
            resolved_question=resolved_question,
            entities=entities,
            risk_level=risk_level,
            risk_reason=risk_reason,
            approval_id=approval_id,
            refs=refs,
        )
        db.commit()
    except Exception:
        db.rollback()


def _save_risk_if_needed(
    db,
    user: User,
    question: str,
    answer: str,
    risk_level: str,
    risk_reason: str,
    entities: list[dict],
    approval_id: str | None,
    refs: list[dict],
) -> None:
    """MEDIUM/HIGH 结论自动沉淀为长期记忆中的历史风险。"""
    if risk_level not in ("HIGH", "MEDIUM"):
        return
    save_risk_memory(
        db,
        user,
        question=question,
        answer=answer,
        risk_level=risk_level,
        risk_reason=risk_reason,
        entities=entities,
        approval_id=approval_id,
        refs=refs,
    )
    db.commit()


def get_chat_client(model: str) -> OpenAI:
    """按模型名选择服务商：qwen-* 走百炼，其余走 DeepSeek。"""
    if model.startswith("qwen"):
        return OpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
        )
    return OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)


def available_models() -> list[str]:
    """返回当前密钥下真正可用的模型。"""
    models = []
    for item in settings.llm_available_models.split(","):
        model = item.strip()
        if not model:
            continue
        is_qwen = model.startswith("qwen")
        if is_qwen and not settings.embedding_api_key:
            continue
        if not is_qwen and not settings.llm_api_key:
            continue
        models.append(model)
    return models


def default_model() -> str:
    """配置的默认模型不可用时，退回第一个可用模型。"""
    models = available_models()
    if settings.llm_model in models:
        return settings.llm_model
    return models[0] if models else settings.llm_model


# ---------------------------------------------------------------------------
# LangGraph 节点（依赖 LLM / DB，通过 register 注入到 workflow 图）
# ---------------------------------------------------------------------------


def _assess_risk_node(state: SupplyGuardState) -> dict:
    """调用 LLM 动态判断回答的风险等级。

    在工作流中断后由路由处理器调用——此时 answer 已填好。
    失败时降级为 LOW，不触发审批。
    """
    question = state["question"]
    answer = state.get("answer", "")
    hits = state.get("review_passed_hits", state.get("hits", []))
    paths = state.get("review_passed_paths", state.get("graph_paths", []))

    # 没有证据就不评估
    if not hits and not paths:
        return {"risk_level": "LOW", "risk_reason": ""}

    context = "\n\n".join(
        f"[{idx}] {hit['filename']}: {hit['text'][:300]}"
        for idx, hit in enumerate(hits[:3], start=1)
    )
    graph_context = (
        _build_graph_context(paths[:3]) if paths else "无图谱路径"
    )

    prompt = RISK_ASSESSMENT_PROMPT.format(
        context=context or "无文档证据",
        graph_context=graph_context,
        question=question,
        answer=answer[:1500],
    )

    try:
        model = state["model"]
        response = get_chat_client(model).chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content or "{}")
        level = result.get("level", "LOW")
        reason = result.get("reason", "")
        if level not in ("HIGH", "MEDIUM", "LOW"):
            level = "LOW"
        return {"risk_level": level, "risk_reason": reason}
    except Exception:
        return {"risk_level": "LOW", "risk_reason": ""}


def _approval_gate_node(state: SupplyGuardState) -> dict:
    """审批门：创建审批单，状态设为 pending。

    此节点只在 risk_level == HIGH 时由条件边路由进入。
    审批单创建后，路由处理器等待管理员决定，然后 resume。
    """
    # 此节点由 db_session_factory 提供 session
    return {"approval_status": "pending"}


def _finalize_node(state: SupplyGuardState) -> dict:
    """收尾节点：标记工作流完成。审计日志由 create_approval 和 decide_approval 写入。"""
    return {"approval_status": state.get("approval_status", "skipped")}


# ---------------------------------------------------------------------------
# 提示词构建
# ---------------------------------------------------------------------------


def _build_graph_context(paths: list[dict]) -> str:
    """把影响路径压成带编号的文本，供模型引用。"""
    return "\n".join(
        f"[G{index}] {path['summary']}（{path['rule_label']}，{path['hops']} 跳；"
        f"事件等级 {path['severity']}；原因：{path['reason']}）"
        for index, path in enumerate(paths, start=1)
    )


# ---------------------------------------------------------------------------
# 图实例（模块级单例，首次调用时编译）
# ---------------------------------------------------------------------------

_compiled_graph = None


def _get_graph():
    """惰性编译图，避免模块导入时循环依赖。"""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = register_risk_and_approval_nodes(
            assess_risk_fn=_assess_risk_node,
            approval_gate_fn=_approval_gate_node,
            finalize_fn=_finalize_node,
        )
    return _compiled_graph


# ---------------------------------------------------------------------------
# 非流式问答（保留兼容）
# ---------------------------------------------------------------------------


def answer_question(
    question: str,
    role: str,
    model: str,
    top_k: int,
    db,
    user: User,
    session_id: str = "default",
) -> dict:
    """检索→图谱→复核→生成→评估→审批，全流程同步执行。"""
    graph = _get_graph()

    # 加载短期记忆、解析追问、加载长期记忆
    history, followup, long_term_context = _prepare_memory(
        db, user, question, role, session_id
    )
    memory_context = build_memory_context(history)

    initial: SupplyGuardState = {
        "question": question,
        "role": role,
        "model": model,
        "top_k": top_k,
        "username": user.username,
        "session_id": session_id,
        "session_history": history,
        "memory_context": memory_context,
        "resolved_question": followup.resolved_question,
        "carried_entity_ids": followup.carried_entity_ids,
        "long_term_context": long_term_context,
    }
    config = {"configurable": {"thread_id": f"sync_{user.username}_{id(initial)}"}}

    # 第一阶段：检索 → 图谱 → 复核 → 拼提示词，中断在 assess_risk 前
    state = graph.invoke(initial, config)

    # 无证据直接拒答
    hits = state.get("review_passed_hits", state.get("hits", []))
    paths = state.get("review_passed_paths", state.get("graph_paths", []))
    if not hits and not paths:
        refusal = "知识库中暂无相关资料，无法回答该问题。"
        _record_turn(
            db,
            user,
            session_id,
            question,
            refusal,
            resolved_question=followup.resolved_question,
        )
        return {
            "answer": refusal,
            "reasoning": None,
            "model": model,
            "hits": [],
            "graph_paths": [],
        }

    # 生成回答
    messages = state.get("messages", [])
    parameters: dict = {"model": model, "messages": messages}
    if "reasoner" not in model:
        parameters["temperature"] = 0.2

    response = get_chat_client(model).chat.completions.create(**parameters)
    message = response.choices[0].message
    answer = message.content or ""
    reasoning = getattr(message, "reasoning_content", None)

    if _is_refusal(answer):
        _record_turn(
            db,
            user,
            session_id,
            question,
            answer,
            resolved_question=followup.resolved_question,
        )
        return {
            "answer": answer,
            "reasoning": reasoning,
            "model": model,
            "hits": [],
            "graph_paths": [],
        }

    # 第二阶段：填入回答 → 风险评估 → 审批门
    update = {"answer": answer, "reasoning": reasoning}
    final_state = graph.invoke(Command(update=update), config)

    risk_level = final_state.get("risk_level", "LOW")
    risk_reason = final_state.get("risk_reason", "")
    entities = final_state.get("entities", [])
    refs = _refs_from_hits(hits)

    result: dict = {
        "answer": answer,
        "reasoning": reasoning,
        "model": model,
        "hits": hits,
        "graph_paths": paths,
        "risk": {"level": risk_level, "reason": risk_reason},
    }

    # HIGH 风险建审批单
    approval_id = None
    if risk_level == "HIGH":
        approval = create_approval(
            db, question, answer, hits, paths, risk_level, risk_reason, user
        )
        db.commit()
        approval_id = approval.approval_id
        result["approval"] = {
            "approval_id": approval_id,
            "level": risk_level,
            "reason": risk_reason,
        }

    # MEDIUM/HIGH 自动沉淀为历史风险
    _save_risk_if_needed(
        db, user, question, answer, risk_level, risk_reason, entities, approval_id, refs
    )

    # 保存本轮对话：Redis 短期记忆 + PostgreSQL 永久档案
    _record_turn(
        db,
        user,
        session_id,
        question,
        answer,
        resolved_question=followup.resolved_question,
        entities=entities,
        risk_level=risk_level,
        risk_reason=risk_reason,
        approval_id=approval_id,
        refs=refs,
    )

    return result


# ---------------------------------------------------------------------------
# 流式问答（SSE）
# ---------------------------------------------------------------------------


def run_workflow_phase1(
    question: str,
    role: str,
    model: str,
    top_k: int,
    username: str,
    session_id: str = "default",
    session_history: list[dict] | None = None,
    resolved_question: str | None = None,
    carried_entity_ids: list[str] | None = None,
    long_term_context: str = "",
):
    """运行工作流第一阶段：检索 → 图谱 → 复核 → 拼提示词。

    在 assess_risk 节点前中断，返回 (thread_id, state, config)。
    路由处理器在此基础上做流式 LLM 生成。

    session_history / long_term_context 由调用方从 Redis 和 PostgreSQL 加载；
    resolved_question 与 carried_entity_ids 用于多轮追问。
    """
    graph = _get_graph()
    history = session_history or []
    memory_context = build_memory_context(history)
    initial: SupplyGuardState = {
        "question": question,
        "role": role,
        "model": model,
        "top_k": top_k,
        "username": username,
        "session_id": session_id,
        "session_history": history,
        "memory_context": memory_context,
        "resolved_question": resolved_question or question,
        "carried_entity_ids": carried_entity_ids or [],
        "long_term_context": long_term_context,
    }
    thread_id = f"stream_{username}_{id(initial)}"
    config = {"configurable": {"thread_id": thread_id}}
    state = graph.invoke(initial, config)
    return thread_id, state, config


def run_workflow_phase2(
    thread_id: str, config: dict, answer: str, reasoning: str | None
) -> dict:
    """工作流第二阶段：填入 LLM 回答 → 风险评估 → 条件路由 → 审批门/finalize。

    使用 Command(update=...) 在中途注入 LLM 回答，然后继续执行
    assess_risk → 条件边 → approval_gate（或 finalize）。
    返回更新后的 state（含 risk_level, risk_reason, approval_status）。
    """
    update = {"answer": answer, "reasoning": reasoning}
    return _get_graph().invoke(Command(update=update), config)


def run_workflow_resume(config: dict) -> dict:
    """从审批门中断恢复：approval_gate → finalize → END。

    使用 Command(resume=...) 告知 LangGraph 继续执行被中断的节点。
    """
    return _get_graph().invoke(Command(resume="approved"), config)


def create_approval_from_state(
    db, state: dict, user: User
) -> str:
    """从工作流状态创建审批单，返回 approval_id。"""
    approval = create_approval(
        db,
        state["question"],
        state.get("answer", ""),
        state.get("review_passed_hits", state.get("hits", [])),
        state.get("review_passed_paths", state.get("graph_paths", [])),
        state.get("risk_level", "HIGH"),
        state.get("risk_reason", ""),
        user,
    )
    db.commit()
    return approval.approval_id


# ---------------------------------------------------------------------------
# 流式 SSE 生成器（放在这里是因为它编排 graph 和 LLM 两边）
# ---------------------------------------------------------------------------


def stream_answer(
    question: str,
    role: str,
    model: str,
    top_k: int,
    db,
    user: User,
    session_id: str = "default",
):
    """边生成边产出 SSE 事件。

    流程：
    0. 加载短期记忆 + 解析追问 + 加载长期记忆
    1. 运行 Phase 1（检索→图谱→复核→拼提示词）→ yield hits, graph
    2. LLM 流式生成 → yield reasoning, delta
    3. 运行 Phase 2（评估风险→条件路由）→ yield risk, approval（如 HIGH）
    4. 如果是 HIGH，阻塞等待审批决定
    5. 沉淀历史风险、保存本轮对话到 Redis → yield done
    """
    username = user.username

    # 记忆准备：短期历史 → 追问解析 → 长期记忆
    history, followup, long_term_context = _prepare_memory(
        db, user, question, role, session_id
    )

    thread_id, state, config = run_workflow_phase1(
        question,
        role,
        model,
        top_k,
        username,
        session_id=session_id,
        session_history=history,
        resolved_question=followup.resolved_question,
        carried_entity_ids=followup.carried_entity_ids,
        long_term_context=long_term_context,
    )

    # 第一阶段结果
    hits = state.get("review_passed_hits", state.get("hits", []))
    paths = state.get("review_passed_paths", state.get("graph_paths", []))
    messages = state.get("messages", [])

    yield {"type": "hits", "hits": hits}
    yield {"type": "graph", "paths": paths}

    # 无证据直接拒答
    if not hits and not paths:
        refusal_text = "知识库中暂无相关资料，无法回答该问题。"
        yield {"type": "delta", "text": refusal_text}
        _record_turn(
            db,
            user,
            session_id,
            question,
            refusal_text,
            resolved_question=followup.resolved_question,
        )
        yield {"type": "done", "hits": [], "graph_paths": []}
        return

    # ---- LLM 流式生成 ----
    parameters: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if "reasoner" not in model:
        parameters["temperature"] = 0.2

    answer_parts = []
    for chunk in get_chat_client(model).chat.completions.create(**parameters):
        delta = chunk.choices[0].delta
        reasoning_chunk = getattr(delta, "reasoning_content", None)
        if reasoning_chunk:
            yield {"type": "reasoning", "text": reasoning_chunk}
        if delta.content:
            answer_parts.append(delta.content)
            yield {"type": "delta", "text": delta.content}

    answer = "".join(answer_parts)
    refused = _is_refusal(answer)

    if refused:
        _record_turn(
            db,
            user,
            session_id,
            question,
            answer,
            resolved_question=followup.resolved_question,
        )
        yield {
            "type": "done",
            "hits": [],
            "graph_paths": [],
        }
        return

    # ---- Phase 2：风险评估 + 审批门 ----
    reasoning_value = None
    final_state = run_workflow_phase2(thread_id, config, answer, reasoning_value)

    risk_level = final_state.get("risk_level", "LOW")
    risk_reason = final_state.get("risk_reason", "")
    entities = final_state.get("entities", [])
    refs = _refs_from_hits(hits)
    yield {"type": "risk", "level": risk_level, "reason": risk_reason}

    approval_id = None
    if risk_level == "HIGH":
        approval_id = create_approval_from_state(
            db,
            {
                "question": question,
                "answer": answer,
                "review_passed_hits": hits,
                "review_passed_paths": paths,
                "risk_level": risk_level,
                "risk_reason": risk_reason,
            },
            user,
        )
        yield {
            "type": "approval",
            "approval_id": approval_id,
            "level": risk_level,
            "reason": risk_reason,
        }

        # 阻塞等待管理员决定
        decision = await_decision(approval_id)
        if decision:
            run_workflow_resume(config)

    # MEDIUM/HIGH 自动沉淀为历史风险
    _save_risk_if_needed(
        db, user, question, answer, risk_level, risk_reason, entities, approval_id, refs
    )

    # 保存本轮对话：Redis 短期记忆 + PostgreSQL 永久档案
    _record_turn(
        db,
        user,
        session_id,
        question,
        answer,
        resolved_question=followup.resolved_question,
        entities=entities,
        risk_level=risk_level,
        risk_reason=risk_reason,
        approval_id=approval_id,
        refs=refs,
    )

    yield {
        "type": "done",
        "hits": hits,
        "graph_paths": paths,
        "approval_id": approval_id,
    }