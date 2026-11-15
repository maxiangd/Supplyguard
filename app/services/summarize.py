"""回答摘要：把长篇回答压缩成可注入提示词的一两句话。

长期记忆只保存结论，不保存完整回答：
- 完整问答在 conversation_turns 里，用于回溯
- 长期记忆只保留结论，避免和会话内容重复、也避免挤占提示词
"""

import re

from app.core.configuration import settings
from app.services.llm import available_models, default_model, get_chat_client

# 引用编号：[1]、[G2]
CITATION_PATTERN = re.compile(r"\[G?\d+\]")
# 句末标点，用于切句
SENTENCE_PATTERN = re.compile(r"(?<=[。！？!?；;])")

SUMMARY_PROMPT = """你是供应链风险分析助手。请把下面这轮问答压缩成一句中文结论。

要求：
- 只保留核心结论和关键动作，不要背景铺垫
- 不超过 {max_chars} 个字
- 不要使用 Markdown、分点或引用编号
- 不要编造回答中没有的信息
- 只输出这一句结论，不要任何前缀

用户问题：{question}
风险等级：{risk_level}
风险依据：{risk_reason}
AI 回答：{answer}"""


def _clean(text: str) -> str:
    """去掉引用编号、Markdown 标记和多余空白。"""
    cleaned = CITATION_PATTERN.sub("", text or "")
    cleaned = cleaned.replace("**", "").replace("##", "").replace("###", "")
    cleaned = re.sub(r"^[#>*\-\s]+", "", cleaned, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", cleaned).strip()


def summarize_text(text: str, limit: int = 160) -> str:
    """抽取开头 1–2 句作为摘要，超长时截断。

    优先保证第一句完整（通常是结论），再在长度允许时补第二句。
    """
    cleaned = _clean(text)
    if not cleaned:
        return ""

    sentences = [part.strip() for part in SENTENCE_PATTERN.split(cleaned) if part.strip()]
    summary = ""
    for sentence in sentences:
        if not summary:
            summary = sentence
        elif len(summary) + len(sentence) <= limit:
            summary += sentence
        else:
            break
        if len(summary) >= limit * 0.6:
            break

    if len(summary) > limit:
        summary = summary[:limit].rstrip("，、；,; ") + "…"
    return summary


def _normalize_llm_output(text: str, limit: int) -> str:
    """清理模型输出：去引用/Markdown、去首尾引号、超长截断。"""
    cleaned = _clean(text).strip("“”\"'")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip("，、；,; ") + "…"
    return cleaned


def summarize_with_llm(
    question: str,
    answer: str,
    risk_level: str | None = None,
    risk_reason: str | None = None,
    limit: int | None = None,
) -> str | None:
    """调用 LLM 生成一句结论；不可用或失败时返回 None。"""
    limit = limit or settings.memory_summary_max_chars

    models = available_models()
    if not models:
        return None

    model = settings.memory_summary_model or default_model()
    if model not in models:
        model = default_model()

    prompt = SUMMARY_PROMPT.format(
        max_chars=limit,
        question=(question or "")[:1000],
        risk_level=risk_level or "未评估",
        risk_reason=(risk_reason or "无")[:500],
        answer=(answer or "")[:4000],
    )

    try:
        client = get_chat_client(model, timeout=10, max_retries=0)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=200,
        )
        content = response.choices[0].message.content or ""
        return _normalize_llm_output(content, limit) or None
    except Exception:
        # 模型超时、限流、密钥失效等一律降级到规则摘要
        return None


def summarize_answer(
    question: str,
    answer: str,
    risk_level: str | None = None,
    risk_reason: str | None = None,
    limit: int | None = None,
) -> str:
    """长期记忆统一入口：优先 LLM 摘要，失败时回退到规则摘要。"""
    limit = limit or settings.memory_summary_max_chars
    if settings.memory_summarize_with_llm:
        summary = summarize_with_llm(
            question, answer, risk_level, risk_reason, limit
        )
        if summary:
            return summary
    return summarize_text(answer, limit=limit)


CONVERSATION_PROMPT = """你是供应链风险分析助手。请把下面的历史对话合并成一段会话摘要。

已有摘要：
{existing}

新增对话：
{turns}

要求：
- 保留关键结论、涉及对象、风险等级和待办事项
- 合并旧摘要与新内容，去掉重复
- 不超过 {max_chars} 个字
- 不要 Markdown、分点或引用编号
- 只输出摘要正文"""


def _format_turns(turns: list[dict], answer_limit: int = 300) -> str:
    """把多轮问答压成摘要提示词里的对话文本。"""
    lines = []
    for turn in turns:
        question = _clean(turn.get("question", ""))[:120]
        answer = _clean(turn.get("answer", ""))[:answer_limit]
        lines.append(f"用户：{question}\n助手：{answer}")
    return "\n\n".join(lines)


def summarize_conversation(
    existing: str, turns: list[dict], limit: int | None = None
) -> str:
    """把溢出最近 N 轮的旧对话增量合并成一段会话摘要。"""
    limit = limit or settings.memory_session_summary_max_chars
    existing = _clean(existing)
    if not turns:
        return existing

    if settings.memory_summarize_with_llm:
        models = available_models()
        if models:
            model = settings.memory_summary_model or default_model()
            if model not in models:
                model = default_model()
            prompt = CONVERSATION_PROMPT.format(
                existing=existing or "无",
                turns=_format_turns(turns),
                max_chars=limit,
            )
            try:
                client = get_chat_client(model, timeout=10, max_retries=0)
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=400,
                )
                summary = _normalize_llm_output(
                    response.choices[0].message.content or "", limit
                )
                if summary:
                    return summary
            except Exception:
                pass

    # 规则降级：旧摘要 + 每轮结论，超长截断
    parts = [existing] if existing else []
    for turn in turns:
        parts.append(
            summarize_text(turn.get("answer", ""), limit=80)
            or _clean(turn.get("question", ""))[:80]
        )
    merged = " ".join(part for part in parts if part)
    if len(merged) > limit:
        merged = merged[:limit].rstrip("，、；,; ") + "…"
    return merged

