"""Token 缓存和刷新单元测试。"""
import base64
import json

from smu_badminton.token_profile import (
    cache_token_for_user,
    clear_token_cache,
    find_user_by_access_token,
    get_cached_token,
    session_exp_epoch,
)


def _jwt_with_exp(exp: float) -> str:
    """构造只含 exp 的假 JWT（decode_jwt_payload 不校验签名，够用）。"""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode("utf-8")).rstrip(b"=")
    return f"eyJhbGciOiJIUzI1NiJ9.{payload.decode('ascii')}.sig"


def test_cache_and_get_token():
    """测试缓存和获取 token。"""
    username = "test_token_user_001"
    tokens = {
        "access_token": "test_access_token_001",
        "id_token": "test_id_token_001"
    }

    # 缓存
    cache_token_for_user(username, tokens)

    # 获取
    cached = get_cached_token(username)
    assert cached is not None
    assert cached["access_token"] == "test_access_token_001"
    assert cached["id_token"] == "test_id_token_001"

    # 清理
    clear_token_cache(username)


def test_get_cached_token_miss():
    """测试获取不存在的 token 缓存。"""
    cached = get_cached_token("nonexistent_token_user")
    assert cached is None


def test_find_user_by_access_token():
    """测试通过 access_token 查找用户。"""
    username = "test_token_user_002"
    tokens = {
        "access_token": "test_access_token_002",
        "id_token": "test_id_token_002"
    }

    cache_token_for_user(username, tokens)

    # 查找
    found_username, id_token = find_user_by_access_token("test_access_token_002")
    assert found_username == username
    assert id_token == "test_id_token_002"

    # 清理
    clear_token_cache(username)


def test_find_user_by_access_token_miss():
    """测试通过不存在的 access_token 查找。"""
    found_username, id_token = find_user_by_access_token("nonexistent_token")
    assert found_username == ""
    assert id_token == ""


def test_clear_token_cache_single():
    """测试清理单个用户的 token 缓存。"""
    username = "test_token_user_003"
    tokens = {"access_token": "token_003", "id_token": "id_003"}

    cache_token_for_user(username, tokens)
    clear_token_cache(username)

    cached = get_cached_token(username)
    assert cached is None


def test_clear_token_cache_all():
    """测试清理所有 token 缓存。"""
    cache_token_for_user("user1", {"access_token": "t1", "id_token": "i1"})
    cache_token_for_user("user2", {"access_token": "t2", "id_token": "i2"})

    clear_token_cache()

    assert get_cached_token("user1") is None
    assert get_cached_token("user2") is None


# ============= session_exp_epoch：access_token 读不出 exp 时回退 id_token =============


def test_session_exp_prefers_access_token():
    """access_token 本身是 JWT 时优先用它，不受 id_token 影响。"""
    access_exp, id_exp = 1_800_000_000.0, 1_900_000_000.0
    tokens = {"access_token": _jwt_with_exp(access_exp), "id_token": _jwt_with_exp(id_exp)}
    assert session_exp_epoch(tokens) == access_exp


def test_session_exp_falls_back_to_id_token():
    """真机实测形态：access_token 是 32 字符 opaque token（读不出 exp）→ 回退 id_token。

    这是回归用例。此前 cas_manager 只读 access_token，``exp_epoch is None`` 会让
    T-0 前的 token 预检被静默跳过，等于那段"杜绝 T-0 触发重新登录"的逻辑从未生效。
    """
    opaque = "2f83032d827f" + "c" * 20  # 32 字符、无 "."，decode_jwt_payload 返回 None
    assert len(opaque) == 32

    id_exp = 1_900_000_000.0
    tokens = {"access_token": opaque, "id_token": _jwt_with_exp(id_exp)}
    assert session_exp_epoch(tokens) == id_exp


def test_session_exp_none_when_both_unreadable():
    """两个 token 都读不出 exp 时返回 None（调用方需容忍）。"""
    assert session_exp_epoch({"access_token": "opaque-token", "id_token": "not-a-jwt"}) is None


def test_session_exp_handles_missing_tokens():
    """空值不应抛异常。"""
    assert session_exp_epoch(None) is None
    assert session_exp_epoch({}) is None
    assert session_exp_epoch({"access_token": "opaque-token"}) is None
