"""指标中间件：按路由模板记录请求数与耗时，避免路径参数撑爆标签。"""

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS

_SKIP_PATHS = {"/metrics"}


class MetricsMiddleware(BaseHTTPMiddleware):
    """记录每个请求的方法、路由模板、状态码与耗时。"""

    async def dispatch(self, request: Request, call_next):
        """执行下游请求并上报指标；路由匹配失败时归到 unmatched。"""
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self._observe(request, 500, start)
            raise
        self._observe(request, response.status_code, start)
        return response

    @staticmethod
    def _observe(request: Request, status_code: int, start: float) -> None:
        """写入请求计数与耗时，跳过 /metrics 自身。"""
        path = request.url.path
        if path in _SKIP_PATHS:
            return
        route = request.scope.get("route")
        route_path = getattr(route, "path", None) or path
        HTTP_REQUESTS.labels(request.method, route_path, str(status_code)).inc()
        HTTP_LATENCY.labels(request.method, route_path).observe(
            time.perf_counter() - start
        )
