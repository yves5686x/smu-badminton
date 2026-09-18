"""预约结果判定单元测试（纯本地，无网络）。

回归背景：上游 GraphQL 把业务字段放在 ``data.<mutation>`` 第二层，而预约结果的判定
历史上直接对顶层取 ``code``，导致**真实成功被判为失败**（定时抢票任务永远落到 failed）。
这组测试同时覆盖嵌套与扁平两种响应体，锁死判定行为。
"""
from smu_badminton.booking_api import (
    business_messages,
    is_business_success,
    unwrap_graphql_result,
)
from smu_badminton.cas_manager import _classify_upstream_response


def _nested(payload):
    """构造上游真实的嵌套响应体。"""
    return {"data": {"saveAppointmentInformationAll": payload}}


# ============= unwrap_graphql_result =============


def test_unwrap_nested_body():
    inner = {"code": "0", "messages": []}
    assert unwrap_graphql_result(_nested(inner)) == inner


def test_unwrap_flat_body_is_returned_as_is():
    flat = {"code": "0", "messages": []}
    assert unwrap_graphql_result(flat) == flat


def test_unwrap_ignores_trailing_data_fields():
    """data 层可能有 __typename 等非业务字段，不应被当成业务结果。"""
    body = {"data": {"__typename": "Mutation", "saveAppointmentInformationAll": {"code": "0"}}}
    assert unwrap_graphql_result(body) == {"code": "0"}


def test_unwrap_handles_non_dict():
    for bad in (None, "boom", [], 0):
        assert unwrap_graphql_result(bad) == {}


def test_unwrap_handles_null_data():
    """{"data": null, "errors": [...]} 不应崩溃，且判不出成功。"""
    body = {"data": None, "errors": [{"message": "ACCESS_TOKEN_INVALID"}]}
    assert is_business_success(body) is False


def test_unwrap_is_idempotent():
    """对已展开的业务对象再次展开必须原样返回。

    调用方会把展开结果继续传给 business_messages 等辅助函数；若二次下钻，
    上游业务层将来一旦新增 data 字段就会误取子节点。
    """
    inner = {"code": "0", "messages": ["ok"]}
    once = unwrap_graphql_result(_nested(inner))
    assert unwrap_graphql_result(once) is once


def test_unwrap_does_not_descend_into_business_data_field():
    """业务层自带 data 子字典时，不得误把它当成 GraphQL data 层。"""
    inner = {"code": "500", "messages": ["已约满"], "data": {"foo": "bar"}}
    assert unwrap_graphql_result(_nested(inner)) == inner
    assert business_messages(unwrap_graphql_result(_nested(inner))) == "已约满"


# ============= is_business_success =============


def test_success_in_nested_body():
    """核心回归点：嵌套响应里的成功必须被识别为成功。"""
    assert is_business_success(_nested({"code": "0", "messages": []})) is True
    assert is_business_success(_nested({"code": "success", "messages": []})) is True


def test_failure_in_nested_body():
    for code in ("500", "1", "", None):
        assert is_business_success(_nested({"code": code, "messages": ["已约满"]})) is False


def test_success_in_flat_body():
    assert is_business_success({"code": "0"}) is True


def test_non_dict_response():
    assert is_business_success(None) is False
    assert is_business_success("ok") is False


# ============= business_messages =============


def test_business_messages_joins_list():
    assert business_messages(_nested({"code": "1", "messages": ["已约满", "换个时段"]})) == "已约满; 换个时段"


def test_business_messages_empty():
    assert business_messages(_nested({"code": "0", "messages": []})) == ""
    assert business_messages(None) == ""


# ============= _classify_upstream_response =============


def test_classify_success():
    assert _classify_upstream_response(_nested({"code": "0", "messages": []})) == "success"


def test_classify_banned():
    resp = _nested({"code": "500", "messages": ["频繁调用接口，禁用3分钟"]})
    assert _classify_upstream_response(resp) == "banned"


def test_classify_captcha_error():
    resp = _nested({"code": "500", "messages": ["验证码不能重复使用"]})
    assert _classify_upstream_response(resp) == "captcha_error"


def test_classify_other():
    assert _classify_upstream_response(None) == "other"
    assert _classify_upstream_response(_nested({"code": "500", "messages": ["系统异常"]})) == "other"
