"""敏感信息检查：长期记忆落库前脱敏，避免密钥、手机号等被持久化。"""

import re

from loguru import logger

# 顺序有意义：身份证 18 位要排在大范围数字串之前
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("api_key", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,})\b")),
    ("password", re.compile(r"(?i)(?:password|passwd|密码|口令)\s*[:：=]\s*\S+")),
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
]

REDACTED = "[敏感信息]"


def redact_sensitive(text: str | None) -> tuple[str, list[str]]:
    """返回脱敏后的文本与命中的敏感类型列表。"""
    if not text:
        return text or "", []

    hits: list[str] = []
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            hits.append(name)
            text = pattern.sub(REDACTED, text)

    if hits:
        logger.bind(categories=hits).warning("长期记忆命中敏感信息，已脱敏")
    return text, hits
