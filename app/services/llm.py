"""LLM 客户端工厂：按模型名选择服务商。

独立成模块，避免长期记忆摘要（summarize.py）反向依赖 chat.py 造成循环导入。
"""

from openai import OpenAI

from app.core.configuration import settings


def get_chat_client(
    model: str, timeout: float | None = None, max_retries: int | None = None
) -> OpenAI:
    """按模型名选择服务商：qwen-* 走百炼，其余走 DeepSeek。

    timeout / max_retries 供记忆摘要这类“可选增强”调用使用，
    避免模型异常时拖住问答主链路。
    """
    options: dict = {}
    if timeout is not None:
        options["timeout"] = timeout
    if max_retries is not None:
        options["max_retries"] = max_retries

    if model.startswith("qwen"):
        return OpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            **options,
        )
    return OpenAI(
        api_key=settings.llm_api_key, base_url=settings.llm_base_url, **options
    )


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
