"""
资源锁与可用性公共缓存（跨路由共享的运行时状态）。

- 资源锁：按 (resources_name, bookdate, kssj, jssj) 防止同一场地同时段的并发预约。
- 可用性公共缓存：场地时间槽数据所有用户共享，按 bookdate 键控，短 TTL。
"""
import asyncio
import logging
import time as _time

logger = logging.getLogger(__name__)

ResourceKey = tuple[str, str, str, str]

_LOCK_MAX_AGE_SEC = 300  # 锁最大存活时间（5分钟）

_locks: dict[ResourceKey, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()
_lock_timestamps: dict[ResourceKey, float] = {}


async def get_resource_lock(key: ResourceKey) -> asyncio.Lock:
    """获取或创建资源锁（只获取锁对象，不 acquire；调用方自行 acquire/release）。"""
    async with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
            _lock_timestamps[key] = _time.time()
        return lock


async def locks_cleanup():
    """定期清理过期的锁，防止内存泄漏（server_fastapi 以后台任务运行）。"""
    while True:
        await asyncio.sleep(120)  # 每2分钟清理一次
        now = _time.time()
        async with _locks_guard:
            expired_keys = [
                k for k, ts in _lock_timestamps.items()
                if now - ts > _LOCK_MAX_AGE_SEC and k in _locks and not _locks[k].locked()
            ]
            for k in expired_keys:
                _locks.pop(k, None)
                _lock_timestamps.pop(k, None)
            if expired_keys:
                logger.info(f"清理了 {len(expired_keys)} 个过期异步锁")


# ============= 可用性公共缓存 =============
# key = bookdate，所有用户共享场地时间槽数据；TTL 很短，仅用于削峰

AVAIL_CACHE_TTL_SEC = 60.0
# 公共缓存最多保留的日期数（正常可约日期只有 ~8 个；上限防御任意日期查询刷内存）
AVAIL_CACHE_MAX_ENTRIES = 32

_avail_public_cache: dict[str, dict[str, object]] = {}
_avail_public_lock = asyncio.Lock()


def avail_cache_get(bookdate: str) -> dict[str, object] | None:
    """读公共缓存条目。不校验 TTL（条目内含 _ts，调用方自行比较），未命中返回 None。"""
    return _avail_public_cache.get(bookdate)


async def avail_cache_put(bookdate: str, data) -> None:
    """写入公共缓存条目，淘汰最旧条目控制内存。

    _ts 用写入时刻（而非请求进入时刻），避免慢查询把缓存条目的有效期提前耗尽。
    """
    async with _avail_public_lock:
        if len(_avail_public_cache) >= AVAIL_CACHE_MAX_ENTRIES:
            oldest = min(_avail_public_cache.items(), key=lambda kv: kv[1].get("_ts", 0))[0]
            _avail_public_cache.pop(oldest, None)
        _avail_public_cache[bookdate] = {"data": data, "_ts": _time.time()}
