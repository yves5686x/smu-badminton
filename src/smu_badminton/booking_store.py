"""任务与本地占位的持久化边界。状态变更和占位释放在同一事务内完成。"""

import enum
import sqlite3
import time

from .core_utils import BookingError, get_db_pool, obfuscate_password


class JobState(enum.StrEnum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


TERMINAL_STATES = {JobState.DONE, JobState.FAILED, JobState.SKIPPED, JobState.CANCELLED}
VALID_TRANSITIONS = {
    JobState.SCHEDULED: {JobState.RUNNING, JobState.FAILED, JobState.SKIPPED, JobState.CANCELLED},
    JobState.RUNNING: {JobState.DONE, JobState.FAILED, JobState.SKIPPED, JobState.CANCELLED},
}
SLOT_FIELDS = ("username", "bookdate", "resources_name", "kssj", "jssj")
JOB_FIELDS = (
    "job_id",
    "username",
    "bookdate",
    "kssj",
    "jssj",
    "resources_name",
    "target_time_str",
    "num_threads",
    "status",
    "created_at",
    "local_booking_id",
)


class BookingStore:
    def __init__(self, pool=None):
        # 默认按调用取连接池，避免应用生命周期结束后仍引用已关闭的全局池。
        self._pool = pool

    @property
    def pool(self):
        return self._pool if self._pool is not None else get_db_pool()

    def create_job(self, job_id, *, password, local_booking_id=None, created_at=None, **params):
        fields = (
            "login_url",
            "captcha_url",
            "username",
            "bookdate",
            "kssj",
            "jssj",
            "resources_name",
            "target_time_str",
            "num_threads",
            "status",
        )
        with self.pool.get_connection() as conn:
            conn.execute(
                f"INSERT INTO scheduled_jobs (job_id,password,local_booking_id,created_at,{','.join(fields)}) "
                f"VALUES ({','.join('?' for _ in range(4 + len(fields)))})",
                (
                    job_id,
                    obfuscate_password(password),
                    local_booking_id,
                    time.time() if created_at is None else created_at,
                    *(params[k] for k in fields),
                ),
            )

    def transition(self, job_id, status, *, release_local=True):
        """原子转换；终态不可覆盖。只删除任务持有的那一条占位，而非按场次模糊删除。"""
        status = JobState(status)
        sources = [old.value for old, targets in VALID_TRANSITIONS.items() if status in targets]
        if not sources:
            return False
        with self.pool.get_connection() as conn:
            cur = conn.execute(
                f"UPDATE scheduled_jobs SET status=? WHERE job_id=? AND status IN ({','.join('?' for _ in sources)})",
                (status.value, job_id, *sources),
            )
            changed = cur.rowcount == 1
            if changed and release_local and status in TERMINAL_STATES and status != JobState.DONE:
                conn.execute(
                    "DELETE FROM local_bookings WHERE id=(SELECT local_booking_id FROM scheduled_jobs WHERE job_id=?)",
                    (job_id,),
                )
        return changed

    def status(self, job_id):
        row = self.detail(job_id)
        return row["status"] if row else None

    def detail(self, job_id):
        with self.pool.get_connection(auto_commit=False) as conn:
            row = conn.execute(f"SELECT {','.join(JOB_FIELDS)} FROM scheduled_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    @staticmethod
    def _job(row):
        job = dict(zip(JOB_FIELDS, row, strict=True))
        job["type"] = "scheduled" if job["target_time_str"] else "immediate"
        return job

    def list_jobs(self, username=None):
        sql = f"SELECT {','.join(JOB_FIELDS)} FROM scheduled_jobs"
        with self.pool.get_connection(auto_commit=False) as conn:
            rows = conn.execute(
                sql + (" WHERE username=?" if username else "") + " ORDER BY created_at DESC", (username,) if username else ()
            ).fetchall()
        return [self._job(row) for row in rows]

    def pending_jobs(self):
        fields = (
            "job_id",
            "login_url",
            "captcha_url",
            "username",
            "password",
            "bookdate",
            "kssj",
            "jssj",
            "resources_name",
            "target_time_str",
            "num_threads",
            "status",
            "local_booking_id",
        )
        with self.pool.get_connection(auto_commit=False) as conn:
            rows = conn.execute(
                f"SELECT {','.join(fields)} FROM scheduled_jobs WHERE status IN ('scheduled','running')"
            ).fetchall()
        return [dict(zip(fields, row, strict=True)) for row in rows]

    def matching_jobs(self, **slot):
        return [j for j in self.list_jobs(slot["username"]) if all(j[k] == slot[k] for k in SLOT_FIELDS)]

    @staticmethod
    def _day_conflict(conn, username, bookdate):
        if conn.execute(
            "SELECT 1 FROM scheduled_jobs WHERE username=? AND bookdate=? AND status IN ('scheduled','running') LIMIT 1",
            (username, bookdate),
        ).fetchone():
            return "您当天已有预约任务，每人每天只能预约一次"
        if conn.execute("SELECT 1 FROM local_bookings WHERE username=? AND bookdate=? LIMIT 1", (username, bookdate)).fetchone():
            return "您当天已有预约记录，每人每天只能预约一次"
        return None

    def day_conflict(self, username, bookdate):
        with self.pool.get_connection(auto_commit=False) as conn:
            return self._day_conflict(conn, username, bookdate)

    def reserve(self, *, check_day=True, **slot):
        """冲突检查和插入在一个写事务中完成，包括不同场地的同日并发请求。"""
        try:
            with self.pool.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if check_day:
                    conflict = self._day_conflict(conn, slot["username"], slot["bookdate"])
                    if conflict:
                        raise BookingError(conflict, conflict)
                cur = conn.execute(
                    f"INSERT INTO local_bookings ({','.join(SLOT_FIELDS)},created_at) VALUES (?,?,?,?,?,?)",
                    (*(slot[k] for k in SLOT_FIELDS), time.time()),
                )
                return cur.lastrowid
        except sqlite3.IntegrityError as e:
            raise BookingError("该场次已有预约", "resource_already_booked") from e

    def local_id(self, **slot):
        with self.pool.get_connection(auto_commit=False) as conn:
            row = conn.execute(
                "SELECT id FROM local_bookings WHERE " + " AND ".join(f"{k}=?" for k in SLOT_FIELDS),
                tuple(slot[k] for k in SLOT_FIELDS),
            ).fetchone()
        return row[0] if row else None

    def release(self, local_booking_id):
        if local_booking_id is not None:
            with self.pool.get_connection() as conn:
                conn.execute("DELETE FROM local_bookings WHERE id=?", (local_booking_id,))

    def list_local(self, bookdate, limit=None, offset=0):
        fields = (*SLOT_FIELDS, "created_at")
        sql = f"SELECT {','.join(fields)} FROM local_bookings WHERE bookdate=? ORDER BY created_at DESC"
        params = [bookdate]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self.pool.get_connection(auto_commit=False) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(zip(fields, row, strict=True)) for row in rows]

    def cleanup_local(self, now):
        from datetime import datetime

        with self.pool.get_connection() as conn:
            expired = []
            for row_id, bookdate, end_time in conn.execute("SELECT id, bookdate, jssj FROM local_bookings"):
                try:
                    end = datetime.strptime(f"{bookdate} {end_time}", "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
                    if end < now:
                        expired.append((row_id,))
                except ValueError:
                    continue
            conn.executemany("DELETE FROM local_bookings WHERE id=?", expired)
        return len(expired)

    def cleanup_history(self, cutoff):
        with self.pool.get_connection() as conn:
            return conn.execute(
                "DELETE FROM scheduled_jobs WHERE status IN ('done','failed','cancelled','skipped') AND created_at < ?", (cutoff,)
            ).rowcount


booking_store = BookingStore()
