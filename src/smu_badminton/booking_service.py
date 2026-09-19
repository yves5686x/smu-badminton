"""预约用例：入口规则、占位、启动与取消。路由不参与执行和回滚。"""

import logging

from .booking_api import (
    UpstreamAuthenticationError,
    check_appointment_cancel_time,
    find_my_appointment_id,
    update_appointment_state,
)
from .booking_store import SLOT_FIELDS
from .cas_manager import BookingManager, booking_manager
from .core_utils import BookingError
from .token_profile import find_user_by_access_token, has_saved_account, refresh_token_for_user

logger = logging.getLogger(__name__)


class BookingService:
    def __init__(self, manager: BookingManager):
        self.manager = manager
        self.store = manager.store

    def submit(self, *, scheduled=False, wait=False, target_time_str=None, num_threads=2, run_async=False, **params):
        """所有预约入口共享同一套校验与占位；同步/后台仅改变返回方式。"""
        if not params.get("password") and not has_saved_account(params["username"]):
            return {"ok": False, "error": "no_saved_credentials"}
        local_id = None
        try:
            if scheduled:
                # 启动线程前拒绝无效目标时间，避免先占位再在线程中解析失败。
                from .http_utils import get_target_datetime_from_network

                get_target_datetime_from_network(target_time_str, params["bookdate"])
            local_id = self.store.reserve(**{k: params[k] for k in SLOT_FIELDS})
            if scheduled:
                job_id = self.manager.start_scheduled_booking(
                    **params,
                    target_time_str=target_time_str,
                    num_threads=num_threads,
                    local_booking_id=local_id,
                    rollback_local_on_fail=False,
                )
                return {"ok": True, "data": {"scheduled": True, "job_id": job_id}}
            result = self.manager.start_immediate_booking(**params, local_booking_id=local_id, wait=wait)
            return result if wait else {"ok": True, "data": {"job_id": result}}
        except BookingError as exc:
            self.store.release(local_id)
            return {"ok": False, "error": exc.code}
        except Exception:
            self.store.release(local_id)
            logger.exception("创建预约任务失败")
            return {"ok": False, "error": "start_failed"}

    def save_local(self, **slot):
        try:
            self.store.reserve(**slot)
            return {"ok": True}
        except BookingError as exc:
            return {"ok": False, "error": exc.code}

    def cancel(self, *, username, bookdate, kssj, jssj, resources_name, access_token=""):
        slot = dict(username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
        jobs = self.store.matching_jobs(**slot)
        # 固定本次操作对应的记录，网络请求完成后不能误删后来新建的占位。
        local_id = self.store.local_id(**slot)
        stopped = sum(self.manager.stop_job(j["job_id"]) for j in jobs if j["status"] in ("scheduled", "running"))
        result = {"stopped": stopped, "upstream_status": "pending", "message": ""}
        if any(self.manager.is_active(j["job_id"]) for j in jobs):
            result["message"] = "停止请求已发送，正在等待已发出的预约请求结束，请稍后确认结果"
            return result

        id_token = ""
        if access_token:
            token_user, id_token = find_user_by_access_token(access_token)
            # 避免调用方误带另一个账号的 token 时取消错账号的预约。
            if token_user and token_user != username:
                return {**result, "upstream_status": "failed", "message": "登录账号与预约账号不一致"}
        try:
            if not access_token:
                tokens = refresh_token_for_user(username)
                if tokens:
                    access_token, id_token = tokens.get("access_token", ""), tokens.get("id_token", "")
            if not access_token:
                return {**result, "upstream_status": "skipped", "message": "已停止排队；无可用凭据，无法确认学校侧预约"}
            try:
                appointment_id = find_my_appointment_id(access_token, bookdate, kssj, jssj, resources_name, id_token=id_token)
            except UpstreamAuthenticationError:
                tokens = refresh_token_for_user(username)
                if not tokens or not tokens.get("access_token"):
                    return {**result, "upstream_status": "failed", "message": "登录已失效且重新登录失败，请重新登录后取消"}
                access_token, id_token = tokens["access_token"], tokens.get("id_token", "")
                appointment_id = find_my_appointment_id(access_token, bookdate, kssj, jssj, resources_name, id_token=id_token)
            if not appointment_id:
                self.store.release(local_id)
                return {**result, "upstream_status": "none", "message": "学校侧没有该时段的有效预约"}
            allowed, message = check_appointment_cancel_time(access_token, appointment_id, id_token=id_token)
            if not allowed:
                return {**result, "upstream_status": "failed", "message": f"当前不允许取消: {message}"}
            ok, message = update_appointment_state(access_token, appointment_id, id_token=id_token)
            if not ok:
                return {**result, "upstream_status": "failed", "message": f"撤销失败: {message}"}
            self.store.release(local_id)
            return {**result, "upstream_status": "cancelled", "message": "学校侧预约已撤销"}
        except Exception:
            logger.exception("取消预约失败")
            return {**result, "upstream_status": "failed", "message": "学校侧查询或撤销失败，请稍后重试"}


booking_service = BookingService(booking_manager)
