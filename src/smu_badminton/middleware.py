"""
纯 ASGI 中间件：指标收集与限流。

不用 BaseHTTPMiddleware：它会给每个请求额外生成一层任务包装，
且对流式响应/后台任务的兼容性有坑。
"""
import asyncio
import logging
import time as _time

from fastapi.responses import JSONResponse

from .config import (
    RATE_LIMIT_JOBS_MAX,
    RATE_LIMIT_JOBS_WINDOW,
    RATE_LIMIT_MAX,
    RATE_LIMIT_WINDOW,
    TRUSTED_PROXIES,
)

logger = logging.getLogger(__name__)


# ============= 指标收集 =============

_metrics: dict[str, dict[str, float]] = {}
_metrics_lock = asyncio.Lock()


class MetricsMiddleware:
    """指标中间件。只统计业务接口，path 数量设上限，避免任意路径扫描把内存字典撑爆。"""

    _MAX_PATHS = 512

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        tracked = path.startswith("/api/") or path == "/health"
        start = _time.perf_counter()
        try:
            await self.app(scope, receive, send)
        finally:
            if tracked:
                duration = (_time.perf_counter() - start) * 1000.0
                async with _metrics_lock:
                    if path in _metrics or len(_metrics) < self._MAX_PATHS:
                        m = _metrics.setdefault(path, {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0})
                        m["count"] += 1.0
                        m["total_ms"] += duration
                        if duration > m["max_ms"]:
                            m["max_ms"] = duration


async def snapshot_metrics() -> dict[str, dict[str, float]]:
    """读取指标快照（/api/metrics 端点用，避免路由直接摸内部字典）。"""
    async with _metrics_lock:
        return {
            k: {
                "count": int(v["count"]),
                "avg_ms": (v["total_ms"] / v["count"]) if v["count"] else 0.0,
                "max_ms": v["max_ms"]
            }
            for k, v in _metrics.items()
        }


# ============= 限流中间件 =============

_rate_limits: dict[str, dict[str, tuple[float, float]]] = {}
_rate_lock = asyncio.Lock()


class RateLimitMiddleware:
    """纯 ASGI 限流中间件（固定窗口，按 IP + 路径计数）。"""

    _MAX_TRACKED_IPS = 4096

    def __init__(self, app):
        self.app = app
        self.default_max = RATE_LIMIT_MAX
        self.default_window = RATE_LIMIT_WINDOW
        self.jobs_max = RATE_LIMIT_JOBS_MAX
        self.jobs_window = RATE_LIMIT_JOBS_WINDOW

        # 受保护接口集合
        self.protected_paths = {
            "/api/book",
            "/api/book/schedule",
            "/api/availability",
            "/api/jobs",
        }

    @staticmethod
    def _client_ip_from_scope(scope) -> str:
        """获取客户端真实 IP。

        只有在配置了可信代理时才信任 X-Forwarded-For / X-Real-IP。
        """
        # 获取直接连接的 IP
        client = scope.get("client")
        direct_ip = client[0] if client else "unknown"

        # 如果没有配置可信代理，直接使用直接连接 IP
        if not TRUSTED_PROXIES:
            return direct_ip

        # 检查直接连接是否来自可信代理
        if direct_ip not in TRUSTED_PROXIES:
            return direct_ip  # 不信任此代理，使用直接 IP

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }

        # 信任此代理，解析 X-Forwarded-For
        xff = headers.get("x-forwarded-for")
        if xff:
            # 取最后一个非可信代理的 IP（最接近客户端）
            ips = [ip.strip() for ip in xff.split(",")]
            for ip in reversed(ips):
                if ip and ip not in TRUSTED_PROXIES:
                    return ip

        xreal = headers.get("x-real-ip")
        if xreal:
            return xreal.strip()

        return direct_ip

    @staticmethod
    def _prune_stale(now: float) -> None:
        """清理全部计数窗口都已过期的 IP 条目。调用者必须持有 _rate_lock。"""
        stale = [
            ip for ip, paths in _rate_limits.items()
            if all(now - ts > RATE_LIMIT_JOBS_WINDOW for _, ts in paths.values())
        ]
        for ip in stale:
            _rate_limits.pop(ip, None)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in self.protected_paths or path.startswith("/api/jobs"):
            # 针对 /api/jobs 使用更宽松的限流窗口
            if path.startswith("/api/jobs"):
                max_per_window = self.jobs_max
                window = self.jobs_window
            else:
                max_per_window = self.default_max
                window = self.default_window

            ip = self._client_ip_from_scope(scope)
            now = _time.time()
            limited = False
            count = 0.0
            async with _rate_lock:
                # 防 IP 维度内存无限增长：超上限时先清理彻底过期的条目
                if len(_rate_limits) > self._MAX_TRACKED_IPS:
                    self._prune_stale(now)
                user_map = _rate_limits.setdefault(ip, {})
                count, start_ts = user_map.get(path, (0.0, now))
                if now - start_ts > window:
                    count, start_ts = 0.0, now
                count += 1.0
                user_map[path] = (count, start_ts)
                limited = count > max_per_window

            if limited:
                logger.warning(
                    "429 请求过于频繁 - ip=%s path=%s count=%s window=%s max=%s",
                    ip, path, count, window, max_per_window,
                )
                # 返回更友好的 JSON 提示（仍然 429）
                resp = JSONResponse(status_code=429, content={
                    "ok": False,
                    "error": "请求过于频繁",
                    "hint": "请求频率超限，请降低轮询频率。",
                    "limit": max_per_window,
                    "window_sec": window,
                    "path": path,
                    "ip": ip,
                })
                await resp(scope, receive, send)
                return

        await self.app(scope, receive, send)
