"""Prometheus 指标：HTTP 请求、LLM 调用与复核拦截。"""

from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter(
    "supplyguard_http_requests_total",
    "HTTP 请求数",
    ["method", "path", "status"],
)
HTTP_LATENCY = Histogram(
    "supplyguard_http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    ["method", "path"],
)
LLM_CALLS = Counter(
    "supplyguard_llm_calls_total",
    "LLM 调用次数",
    ["model"],
)
LLM_ERRORS = Counter(
    "supplyguard_llm_errors_total",
    "LLM 调用失败次数",
    ["model"],
)
RBAC_DROPS = Counter(
    "supplyguard_rbac_drops_total",
    "复核阶段被丢弃的越权证据数",
    ["stage"],
)
