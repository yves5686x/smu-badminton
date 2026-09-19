"""
预约相关路由模块。

包含：立即预约、定时预约、可用性查询、本地预约记录。
"""

import logging
import time as _time

from fastapi import APIRouter, Response
from fastapi.concurrency import run_in_threadpool

from .availability import availability_service
from .booking_api import UpstreamAuthenticationError, UpstreamQueryError
from .booking_service import booking_service
from .schemas import (
    AvailabilityRequest,
    AvailabilityResponse,
    BookRequest,
    BookResponse,
    LocalBookingRequest,
    ScheduleRequest,
    ScheduleResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["booking"])


@router.post("/book", response_model=BookResponse)
async def api_book(req: BookRequest):
    return await run_in_threadpool(booking_service.submit, **req.model_dump(), wait=True)


@router.post("/book/schedule", response_model=ScheduleResponse)
async def api_book_schedule(req: ScheduleRequest):
    return await run_in_threadpool(booking_service.submit, **req.model_dump(), scheduled=True)


@router.post("/local_bookings")
async def api_save_local_booking(req: LocalBookingRequest):
    return await run_in_threadpool(booking_service.save_local, **req.model_dump())


@router.get("/local_bookings")
async def api_list_local_bookings(
    bookdate: str, response: Response, limit: int | None = None, offset: int = 0, fields: str | None = None
):
    """列出本地预约记录。

    过期记录由后台任务周期清理（server_fastapi._stale_local_bookings_cleanup），
    请求路径不再做全表扫描。
    """
    rows = await run_in_threadpool(booking_service.store.list_local, bookdate, limit, offset)
    if fields:
        allow = {"username", "bookdate", "resources_name", "kssj", "jssj", "created_at"}
        wanted = [f.strip() for f in fields.split(",") if f.strip() in allow]
        if wanted:
            rows = [{k: v for k, v in row.items() if k in wanted} for row in rows]
    response.headers["X-LBookings-Count"] = str(len(rows))
    return {"ok": True, "data": {"list": rows}}


@router.post("/availability", response_model=AvailabilityResponse)
async def api_availability(req: AvailabilityRequest, response: Response):
    if not req.token:
        return AvailabilityResponse(ok=False, error="token_required")
    started = _time.perf_counter()
    try:
        rows, cache_status = await availability_service.query(req.token, req.bookdate, force_refresh=req.force_refresh)
    except UpstreamAuthenticationError:
        return AvailabilityResponse(ok=False, error="login_failed")
    except UpstreamQueryError as exc:
        logger.warning("可用性查询失败: %s", exc)
        return AvailabilityResponse(ok=False, error="upstream_query_failed")
    response.headers["X-Avail-Cache"] = cache_status
    response.headers["X-Avail-TotalMs"] = f"{(_time.perf_counter() - started) * 1000:.2f}"
    response.headers["X-Avail-ListLen"] = str(len(rows))
    return AvailabilityResponse(ok=True, data={"list": rows})
