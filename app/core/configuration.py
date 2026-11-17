"""应用配置：环境变量与 .env 统一使用 APP_ 前缀。"""

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def _bypass_proxy_for_local_services() -> None:
    """本地 Qdrant 不走系统代理，外部 embedding 仍然走代理。

    Clash 之类的系统代理会把 localhost:6333 也转发出去并返回 502，
    这里把本机地址加入 NO_PROXY，避免向量库请求被劫持。
    """
    for key in ("NO_PROXY", "no_proxy"):
        values = {
            item.strip()
            for item in os.environ.get(key, "").split(",")
            if item.strip()
        }
        values.update({"127.0.0.1", "localhost"})
        os.environ[key] = ",".join(sorted(values))


_bypass_proxy_for_local_services()


class Settings(BaseSettings):
    """应用配置，字段对应 APP_ 前缀的环境变量。"""

    model_config = SettingsConfigDict(
        env_prefix="APP_", env_file=".env", extra="ignore"
    )

    app_name: str = "SupplyGuard"
    app_version: str = "0.1.0"
    env: str = "development"
    api_prefix: str = "/api"
    log_level: str = "INFO"

    # 认证
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    demo_password: str = "catl123"

    # 数据库
    database_url: str = (
        "postgresql+psycopg://supplyguard:123456@127.0.0.1:5432/supplyguard"
    )

    # 队列
    redis_url: str = "redis://localhost:6380/0"

    # 上传
    upload_directory: str = "data/uploads"
    max_upload_size_mb: int = 20

    # 向量库
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "supplyguard_chunks"
    # 图谱实体的语义索引：与关键词召回并行，再用 RRF 融合
    qdrant_entity_collection: str = "supplyguard_entities"
    # 两路各取 Top N 候选，再用 RRF 融合
    entity_keyword_limit: int = 20
    entity_vector_limit: int = 20
    entity_vector_threshold: float = 0.6
    entity_vector_min_query_chars: int = 2
    entity_rrf_k: int = 60

    # 图谱
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "change-me"
    neo4j_database: str = "neo4j"
    # 进入提示词的图谱路径条数
    graph_evidence_limit: int = 15

    # Embedding：阿里百炼（OpenAI 兼容模式）
    embedding_api_key: str = ""
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v3"
    embedding_dimension: int = 1024

    # LLM：DeepSeek（OpenAI 兼容模式）
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    # 前端可选的模型；qwen-* 复用百炼的 key 与 base_url
    llm_available_models: str = (
        "deepseek-chat,deepseek-reasoner,qwen-plus,qwen-max"
    )

    # RAG
    chunk_size: int = 800
    chunk_overlap: int = 120
    top_k: int = 4

    # SecureRAG
    secure_rag_enforce_roles: bool = True
    secure_rag_review_max_retries: int = 1
    secure_rag_max_drop_ratio: float = 0.5

    # Memory
    memory_session_ttl_seconds: int = 3600
    memory_max_turns: int = 10
    memory_long_term_enabled: bool = True
    # 长期记忆是否调用 LLM 生成摘要；密钥缺失或调用失败时自动回退到规则摘要
    memory_summarize_with_llm: bool = True
    memory_summary_max_chars: int = 120
    # 留空则使用默认模型
    memory_summary_model: str = ""
    # 超过最近 N 轮的旧对话滚动压缩成会话摘要
    memory_rolling_summary_enabled: bool = True
    memory_session_summary_max_chars: int = 300

    # Benchmark
    benchmark_max_cases: int = 200
    benchmark_min_hit_rate: float = 0.8
    benchmark_require_zero_leak: bool = True

    # 审批
    require_approval: bool = True
    output_policy_enabled: bool = True

    # CORS：逗号分隔，用 property 转成列表
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 CORS 来源转成列表。"""
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """返回全局唯一的配置实例。"""
    return Settings()


settings = get_settings()
