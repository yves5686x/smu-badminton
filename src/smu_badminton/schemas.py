"""
Pydantic 请求/响应模型。

错误响应约定：{"ok": false, "error": <机器码>, "message": <可选人话>}；
成功响应：{"ok": true, "data": ...}。
"""

from datetime import date, time

from pydantic import BaseModel, Field, field_validator, model_validator


class DatedRequest(BaseModel):
    """日期在 HTTP 边界校验，输出仍为上游接口需要的字符串。"""

    @field_validator("bookdate", check_fields=False)
    @classmethod
    def valid_date(cls, value):
        date.fromisoformat(value)
        return value


class BookingSlot(DatedRequest):
    """场次的共同边界校验。"""

    @field_validator("kssj", "jssj", "target_time_str", check_fields=False)
    @classmethod
    def valid_time(cls, value):
        time.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def ordered_times(self):
        if self.kssj >= self.jssj:
            raise ValueError("结束时间必须晚于开始时间")
        return self


class BookRequest(BookingSlot):
    login_url: str = Field("", description="CAS 登录URL（可省略，服务端使用配置默认值）")
    captcha_url: str = Field("", description="验证码URL（可省略，服务端使用配置默认值）")
    username: str = Field(..., description="学号/用户名")
    password: str = Field("", description="密码（可省略：省略时服务端使用已保存的凭据，登录后自动保存）")
    bookdate: str = Field(..., pattern=r"\d{4}-\d{2}-\d{2}", description="预约日期 YYYY-MM-DD")
    kssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="开始时间 HH:MM")
    jssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="结束时间 HH:MM")
    resources_name: str = Field(..., description="资源名称，如 羽毛球13号场地")


class BookResponse(BaseModel):
    ok: bool
    data: dict | None = None
    error: str | None = None


class ScheduleRequest(BookRequest):
    target_time_str: str = Field(..., pattern=r"^\d{2}:\d{2}:\d{2}$", description="目标开抢时间，格式 HH:MM:SS")
    num_threads: int = Field(2, ge=1, le=5, description="并发线程数")  # 限制 1-5
    run_async: bool = Field(False, description="是否后台异步执行（立即返回）")


class ScheduleResponse(BookResponse):
    pass


class AvailabilityRequest(DatedRequest):
    force_refresh: bool = False
    token: str = Field(..., description="访问令牌")
    bookdate: str = Field(..., pattern=r"\d{4}-\d{2}-\d{2}", description="预约日期 YYYY-MM-DD")


class AvailabilityResponse(BookResponse):
    pass


class JobImmediateRequest(BookRequest):
    pass


class JobScheduledRequest(ScheduleRequest):
    pass


class JobsListResponse(BookResponse):
    message: str | None = None


class LocalBookingRequest(BookingSlot):
    username: str = Field(..., description="用户名")
    bookdate: str = Field(..., pattern=r"\d{4}-\d{2}-\d{2}", description="预约日期")
    resources_name: str = Field(..., description="资源名称")
    kssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="开始时间")
    jssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="结束时间")


class StopByParamsRequest(BookingSlot):
    username: str = Field(..., description="用户名")
    bookdate: str = Field(..., pattern=r"\d{4}-\d{2}-\d{2}", description="预约日期")
    kssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="开始时间")
    jssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="结束时间")
    resources_name: str = Field(..., description="资源名称")
    current_username: str = Field(..., description="当前操作用户名，用于权限验证")
    access_token: str = Field("", description="调用方 access_token（用于撤销学校侧预约，可选）")


class RefreshRequest(BaseModel):
    username: str = Field(..., description="用户名（凭服务端保存的账号静默重登换取新 token）")


class StopJobRequest(BaseModel):
    current_username: str = Field(..., description="当前操作用户名，用于权限验证")


class UpdateConfigRequest(BaseModel):
    login_url: str = Field(..., description="新的登录入口 URL；传空串恢复默认（WF 首页）")
    current_username: str = Field(..., description="当前操作用户名，用于权限验证")


class CaptchaRequest(BaseModel):
    login_url: str = Field("", description="CAS 登录URL（可选）")
    captcha_url: str = Field("", description="验证码URL（可选）")


class CaptchaResponse(BaseModel):
    ok: bool
    data: dict | None = None
    error: str | None = None


class LoginRequest(BaseModel):
    login_url: str = Field("", description="CAS 登录URL（可选）")
    captcha_url: str = Field("", description="验证码URL（可选）")
    username: str = Field(..., description="学号/用户名")
    password: str = Field(..., description="密码")
    captcha_code: str | None = Field(None, description="手动输入的验证码（可选）")
    session_id: str | None = Field(None, description="验证码会话ID（用于复用session）")


class LoginResponse(BaseModel):
    ok: bool
    data: dict | None = None
    error: str | None = None
    error_type: str | None = Field(None, description="错误类型: captcha_error, password_error, network_error, unknown_error")
    need_manual_captcha: bool = Field(False, description="是否需要手动输入验证码")


class LogoutRequest(BaseModel):
    username: str = Field(..., description="用户名")
