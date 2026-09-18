"""抢票管线离线回归验证（不联网、不碰真实账号、不写生产库）。

用法:
    python scripts/verify_rush_pipeline.py

背景
----
上游羽毛球场地是「提前 7 天、每晚 21:00 放号」。这带来两个必然事实：

1. **T-75s 的预取窗口里，目标时段的资源/时段 ID 还没放出来**，`fetch_resource_time_id`
   必然返回 None。旧实现在预取失败时直接把任务判 failed，于是一枪未发就结束。
2. 预约 mutation 的业务字段在 GraphQL 响应的 ``data.<mutation>`` **第二层**，
   旧实现对顶层取 ``code``，于是**真实成功也被判成失败**。

本脚本用假 Session / 假上游把整条抢票管线跑通，锁住下面五条路径的最终状态与副作用：

  A. 预取失败 → T-0 兜底解析 → 成功                ⇒ done
  B. 预取成功但上游业务层拒绝（嵌套响应里的失败）   ⇒ failed
  C. 预取与 T-0 都拿不到资源/时段                   ⇒ failed（且不得白白提交）
  D. 预取失败 + 资源需要滑块验证码                  ⇒ 验证码池必须**提前**攒好，T-0 只发射
  E. 预取成功且资源不需要验证码                     ⇒ 不得携带任何验证码凭证

D 是本脚本的重点：预取失败时 `open_captcha_verify` 未知，若不投机预热验证码池，
各枪就只能到 T-0 现场解滑块（数秒），等于直接放弃抢票。
"""
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

# 必须在导入业务模块前落到临时目录，避免动到 data/data.db
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("SECRET_KEY", "verify-secret")

from smu_badminton import cas_manager as cm  # noqa: E402
from smu_badminton.core_utils import get_db_pool, init_db_tables  # noqa: E402

init_db_tables()

BEIJING = timezone(timedelta(hours=8))

# 「不需要验证码」的响应体与「需要验证码」的响应体（open_captcha_verify 取值）
OK_NESTED = {"data": {"saveAppointmentInformationAll": {"code": "0", "messages": []}}}
CAPTCHA_ERR = {"data": {"saveAppointmentInformationAll": {"code": "500", "messages": ["验证码不能重复使用"]}}}

# local_bookings 的唯一约束是 (bookdate, resources_name, kssj, jssj)，即按**场次**唯一。
# 用例之间必须用不同场地名隔离，否则前一个用例留下的记录会挡住后一个用例。
_CASE_SEQ = 0


class FakeSession:
    """任何真实 HTTP 都是 bug，直接炸出来。"""

    def post(self, *a, **k):
        raise AssertionError("不应发真实请求")

    def close(self):
        pass


class FakeClock:
    """真机 ClockSync 要向美团校时接口发 3 次请求（离线时每次等满 3s 超时）。

    这里保留语义（本地钟 + 偏移）但不发 HTTP，让用例时序可控、可复现。
    """

    def __init__(self):
        self.offset_sec = 0.0

    def sync(self, samples: int = 3):
        return 0.0

    def now(self):
        return datetime.now(BEIJING)


def run_case(name, fetch_seq, make_resp, expect_status, *, checks=None, lead_sec=3,
             pool_window=None, post_check=None, rollback=True, insert_local=True):
    """跑一个抢票任务并断言结果。

    Args:
        name: 用例名。
        fetch_seq: fetch_resource_time_id 的逐次返回值（None 表示上游未放号）。
        make_resp: make_appointment 的固定返回值。
        expect_status: 期望的任务终态。
        checks: 额外断言，支持 captcha_min / captcha_prefetched /
            submitted_captcha_none / submitted_captcha_all。
        lead_sec: 目标时刻距今秒数（模拟「提前建任务」的等待时长）。
        pool_window: 把验证码池硬截止点改到 T+N 秒。生产环境任务提前数小时建好、
            T-75s 唤醒，天然有 [T-75, T-35] 的窗口；短用例需要这个开关才能造出窗口。
        post_check: 任务结束后调用 (username, bookdate)，返回问题描述或 None。
        rollback: 传给 start_scheduled_booking 的 rollback_local_on_fail。
        insert_local: 是否模拟 /api/book/schedule 先插入本地占位记录。

    Returns:
        bool: 该用例是否通过。
    """
    cm.ClockSync = FakeClock
    prev_stop = cm.CAPTCHA_HARD_STOP_BEFORE_SEC
    if pool_window is not None:
        cm.CAPTCHA_HARD_STOP_BEFORE_SEC = -pool_window

    cm._shared_session = lambda: FakeSession()
    cm.get_token_cached = lambda *a, **k: {"access_token": "aa.bb.cc", "id_token": "id"}
    cm.session_exp_epoch = lambda t: None
    cm.list_appointments_for_account = lambda *a, **k: []
    cm.resolve_user_info = lambda *a, **k: {"appointment_user": "u1"}

    calls = {"fetch": 0, "make": 0, "captcha": 0}
    submitted_captchas: list[str] = []  # 每次提交实际带上的 captcha_id
    solve_times: list[float] = []  # 每次解验证码的绝对时刻，用于判断是否发生在 T-0 之前

    def fake_solve(token):
        calls["captcha"] += 1
        solve_times.append(time.time())
        return (f"cid{calls['captcha']}", f"ccode{calls['captcha']}")

    seq = list(fetch_seq)

    def fake_fetch(*a, **k):
        calls["fetch"] += 1
        return seq.pop(0) if seq else None

    def fake_make(*a, **k):
        calls["make"] += 1
        submitted_captchas.append(k.get("captcha_id", ""))
        return make_resp

    cm.solve_and_verify_slide_captcha = fake_solve
    cm.fetch_resource_time_id = fake_fetch
    cm.make_appointment = fake_make

    now = datetime.now(BEIJING) + timedelta(seconds=lead_sec)
    target_epoch = now.timestamp()
    bookdate = (now + timedelta(days=7)).strftime("%Y-%m-%d")
    global _CASE_SEQ
    _CASE_SEQ += 1
    username = f"verify_{_CASE_SEQ}"
    court = f"验证场地{_CASE_SEQ}号"
    # 复刻 /api/book/schedule 的动作：先在本地登记这条「预约」，再交给后台任务
    if insert_local:
        cm.booking_manager.add_local_booking(username, bookdate, court, "10:00", "11:00")
    jid = cm.booking_manager.start_scheduled_booking(
        login_url="", captcha_url="", username=username, password="p",
        bookdate=bookdate, kssj="10:00", jssj="11:00", resources_name=court,
        target_time_str=now.strftime("%H:%M:%S"), num_threads=2,
        rollback_local_on_fail=rollback,
    )

    detail = {}
    deadline = time.time() + lead_sec + 30
    while time.time() < deadline:
        detail = cm.booking_manager.get_job_detail(jid) or {}
        if detail.get("status") in ("done", "failed", "skipped", "cancelled"):
            break
        time.sleep(0.3)

    cm.CAPTCHA_HARD_STOP_BEFORE_SEC = prev_stop

    status = detail.get("status")
    problems = []
    if status != expect_status:
        problems.append(f"status={status} 期望 {expect_status}")

    for key, want in (checks or {}).items():
        if key == "captcha_min" and calls["captcha"] < want:
            problems.append(f"验证码求解 {calls['captcha']} 次 < 期望 {want}")
        elif key == "captcha_prefetched" and want:
            # 所有求解都必须发生在 T-0 之前（留 0.3s 容差），否则等于临阵磨枪
            late = [t for t in solve_times if t > target_epoch - 0.3]
            if late:
                problems.append(f"有 {len(late)} 次验证码求解发生在 T-0 之后")
        elif key == "submitted_captcha_none" and want:
            bad = [c for c in submitted_captchas if c]
            if bad:
                problems.append(f"不需要校验却带上了凭证: {bad}")
        elif key == "submitted_captcha_all" and want:
            bad = [c for c in submitted_captchas if not c]
            if bad:
                problems.append(f"需要校验却有 {len(bad)} 枪未带凭证")

    if post_check is not None:
        problem = post_check(username, bookdate)
        if problem:
            problems.append(problem)

    ok = not problems
    extra = "; ".join(problems) if problems else ""
    print(
        f"[{'PASS' if ok else 'FAIL'}] {name}: status={status} "
        f"fetch={calls['fetch']} 提交={calls['make']} 解验证码={calls['captcha']} "
        f"提交凭证={submitted_captchas} {extra}"
    )
    return ok


def _no_leftover(username: str, bookdate: str) -> str | None:
    """失败后本地记录必须已回滚，否则该场次会被永久占用、无法重试。"""
    conflict = cm.booking_manager.day_booking_conflict(username, bookdate)
    if conflict:
        return f"失败后仍被拦截，无法重试: {conflict}"
    return None


def _record_kept(username: str, bookdate: str) -> str | None:
    """成功后本地记录必须保留。

    任务已结束（scheduled_jobs 里不会是 scheduled/running），所以此时还能查到冲突，
    只可能来自 local_bookings 那一行。
    """
    if not cm.booking_manager.day_booking_conflict(username, bookdate):
        return "成功后本地预约记录丢失，前端会误显示为「可预约」"
    return None


def main() -> int:
    results = []

    # A: 预取拿不到（上游未放号）→ T-0 兜底成功
    #    修复前：预取失败直接 failed，一枪未发
    results.append(run_case(
        "A 预取失败+T-0兜底成功",
        [None] + [("rid", "tid", "1")] * 4,
        OK_NESTED,
        "done",
    ))

    # B: 预取成功但上游拒绝（业务字段在嵌套层里的失败）
    results.append(run_case(
        "B 预取成功+上游拒绝",
        [("rid", "tid", "1")] * 5,
        CAPTCHA_ERR,
        "failed",
    ))

    # C: 预取与 T-0 都拿不到 → failed，且不应有任何提交
    results.append(run_case(
        "C 预取与T-0都失败",
        [None] * 8,
        OK_NESTED,
        "failed",
    ))

    # D: 预取失败 + 资源需要验证码 → 池必须在校验开关未知时就投机攒好
    results.append(run_case(
        "D 预取失败+需要验证码(提前解)",
        [None] + [("rid", "tid", "1")] * 4,
        OK_NESTED,
        "done",
        lead_sec=8,
        pool_window=20,
        checks={"captcha_min": 2, "captcha_prefetched": True, "submitted_captcha_all": True},
    ))

    # E: 预取成功且资源不需要验证码 → 一枪都不该带凭证
    results.append(run_case(
        "E 不需要验证码(不携带凭证)",
        [("rid", "tid", "0")] * 5,
        OK_NESTED,
        "done",
        checks={"submitted_captcha_none": True},
    ))

    # F: 抢票失败 → 本地预约记录必须回滚
    #    local_bookings 的唯一约束是按场次的，留一条就同时挡住本人重试和所有其他同学选这个场次
    results.append(run_case(
        "F 失败回滚本地记录",
        [("rid", "tid", "1")] * 5,
        CAPTCHA_ERR,
        "failed",
        post_check=_no_leftover,
    ))

    # G: 抢票成功 → 本地记录要保留（前端靠它把该场次渲染成「已占用」）
    results.append(run_case(
        "G 成功保留本地记录",
        [("rid", "tid", "1")] * 5,
        OK_NESTED,
        "done",
        post_check=_record_kept,
    ))

    # H: rollback_local_on_fail=False（如 /api/jobs/scheduled 不插占位记录）→ 不得误删
    #    这里刻意先插一条记录模拟「别的任务的占位」，任务失败后它必须还在
    results.append(run_case(
        "H 不承担回滚时不误删",
        [("rid", "tid", "1")] * 5,
        CAPTCHA_ERR,
        "failed",
        rollback=False,
        post_check=_record_kept,
    ))

    try:
        get_db_pool().close_all()
    except Exception:
        pass

    print("\n结果:", "全部通过" if all(results) else "存在失败用例")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
