"""可用性查询：按日期共享资源缓存，个人预约始终单独查询。

资源与容量对不同身份相同；缓存不包含 bookedByMe。任务自己维护缓存和在途状态，
单个 HTTP 调用方取消不会中断其他调用方正在等待的公共查询。
"""

import asyncio
import time

from fastapi.concurrency import run_in_threadpool

from .booking_api import (
    build_my_bookings_map,
    create_session,
    fetch_all_time_slots,
    get_thread_session,
    list_appointments_for_account,
    list_resources_by_account,
    merge_bookings,
)
from .token_profile import find_user_by_access_token


class AvailabilityService:
    def __init__(self, ttl=60.0, max_entries=32):
        self.ttl = ttl
        self.max_entries = max_entries
        self._cache = {}
        self._inflight = {}

    @staticmethod
    def _load_slots(token, bookdate, id_token):
        with create_session() as session:
            resources = list_resources_by_account(token, bookdate, id_token=id_token, session=session)
            return fetch_all_time_slots(token, bookdate, resources, id_token, session)

    async def _load_and_cache(self, token, bookdate, id_token):
        try:
            slots = await run_in_threadpool(self._load_slots, token, bookdate, id_token)
            if len(self._cache) >= self.max_entries:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest)
            self._cache[bookdate] = (time.monotonic(), slots)
            return slots
        finally:
            self._inflight.pop(bookdate, None)

    @staticmethod
    def _my_bookings(token, bookdate, id_token):
        return list_appointments_for_account(token, bookdate, id_token=id_token, session=get_thread_session())

    async def query(self, token, bookdate, *, force_refresh=False):
        _, id_token = find_user_by_access_token(token)
        entry = self._cache.get(bookdate)
        if not force_refresh and entry and time.monotonic() - entry[0] < self.ttl:
            slots, cache_status = entry[1], "HIT-PUBLIC"
        else:
            task = self._inflight.get(bookdate)
            if task is None:
                task = asyncio.create_task(self._load_and_cache(token, bookdate, id_token))
                self._inflight[bookdate] = task
                # 最后一个调用方取消后仍消费任务异常，避免无人接收的异常警告。
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            slots, cache_status = await asyncio.shield(task), "MISS"
        mine = await run_in_threadpool(self._my_bookings, token, bookdate, id_token)
        return merge_bookings(slots, build_my_bookings_map(mine)), cache_status


availability_service = AvailabilityService()
