"""回答摘要：把长篇回答压缩成可注入提示词的一两句话。

长期记忆只保存结论，不保存完整回答：
- 完整问答在 conversation_turns 里，用于回溯
- 长期记忆只保留结论，避免和会话内容重复、也避免挤占提示词
"""

import re

# 引用编号：[1]、[G2]
CITATION_PATTERN = re.compile(r"\[G?\d+\]")
# 句末标点，用于切句
SENTENCE_PATTERN = re.compile(r"(?<=[。！？!?；;])")


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
