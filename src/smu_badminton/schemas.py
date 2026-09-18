"""
Pydantic 请求/响应模型。

错误响应约定：{"ok": false, "error": <机器码>, "message": <可选人话>}；
成功响应：{"ok": true, "data": ...}。
"""

from pydantic import BaseModel, Field


class BookRequest(BaseModel):
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
    target_time_str: str = Field(..., description="目标开抢时间，格式 HH:MM:SS")
    num_threads: int = Field(5, ge=1, le=5, description="并发线程数")  # 限制 1-5
    run_async: bool = Field(False, description="是否后台异步执行（立即返回）")


class ScheduleResponse(BookResponse):
    pass


class AvailabilityRequest(BaseModel):
    token: str = Field(..., description="访问令牌")
    bookdate: str = Field(..., pattern=r"\d{4}-\d{2}-\d{2}", description="预约日期 YYYY-MM-DD")


class AvailabilityResponse(BookResponse):
    pass


class JobImmediateRequest(BookRequest):
    pass


class JobScheduledRequest(ScheduleRequest):
    pass


class JobsListResponse(BaseModel):
    ok: bool
    data: dict | None = None


class LocalBookingRequest(BaseModel):
    username: str = Field(..., description="用户名")
    bookdate: str = Field(..., pattern=r"\d{4}-\d{2}-\d{2}", description="预约日期")
    resources_name: str = Field(..., description="资源名称")
    kssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="开始时间")
    jssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="结束时间")


class StopByParamsRequest(BaseModel):
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
