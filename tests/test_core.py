"""核心安全逻辑的回归测试。

覆盖点都是历史上真实踩过的坑：密码哈希格式、旧哈希兼容、
登录节流计数、多租户日志隔离。运行：pytest -q
"""
import asyncio
import hashlib

import web_app as m


# ---------------- 密码哈希 ----------------

def test_password_roundtrip():
    stored = m.hash_password("s3cret", "salt123")
    assert stored.startswith("pbkdf2_sha256$")
    ok, need_upgrade = m.verify_password("s3cret", "salt123", stored)
    assert ok is True
    assert need_upgrade is False


def test_password_rejects_wrong():
    stored = m.hash_password("s3cret", "salt123")
    ok, _ = m.verify_password("s3cret!", "salt123", stored)
    assert ok is False


def test_legacy_sha256_still_works_and_flags_upgrade():
    legacy = hashlib.sha256(("oldpass" + "abc").encode()).hexdigest()
    ok, need_upgrade = m.verify_password("oldpass", "abc", legacy)
    assert ok is True
    assert need_upgrade is True


def test_empty_hash_never_passes():
    assert m.verify_password("x", "y", "")[0] is False


# ---------------- 登录节流 ----------------

def test_throttle_locks_after_max_failures():
    key = "1.2.3.4|tester"
    m._clear_login_failure(key)
    for _ in range(m.LOGIN_MAX_FAILS):
        assert m._login_locked_for(key) == 0      # 未达阈值不锁定
        m._record_login_failure(key)
    assert m._login_locked_for(key) > 0           # 达到阈值后锁定
    m._clear_login_failure(key)
    assert m._login_locked_for(key) == 0


def test_throttle_key_differs_by_username():
    class _Req:
        headers = {}
        client = type("C", (), {"host": "1.2.3.4"})()

    assert m._throttle_key(_Req(), "Alice") != m._throttle_key(_Req(), "Bob")
    assert m._throttle_key(_Req(), "Alice") == m._throttle_key(_Req(), "alice")


# ---------------- 多租户日志隔离 ----------------

class _FakeWS:
    def __init__(self):
        self.got = []

    async def send_text(self, s):
        self.got.append(s)


def test_log_broadcast_is_isolated_per_user():
    async def scenario():
        b = m.LogBroadcaster()
        b.logger.handlers.clear()
        u1, u2, admin = _FakeWS(), _FakeWS(), _FakeWS()
        b.connections = {
            u1: {"user_id": 1, "is_admin": False},
            u2: {"user_id": 2, "is_admin": False},
            admin: {"user_id": 9, "is_admin": True},
        }
        await b.broadcast("[u1] 机密群消息", 1)
        await b.broadcast("[系统] 全体通知", None)
        return u1, u2, admin

    u1, u2, admin = asyncio.run(scenario())
    assert u1.got == ["[u1] 机密群消息"]
    assert u2.got == []                       # 绝不串线
    assert len(admin.got) == 2                # 管理员可见全部


# ---------------- 依赖瘦身回归 ----------------

def test_no_gui_legacy_imports():
    """requirements 里已移除 GUI 时代依赖，代码不应再引用它们"""
    import inspect
    src = inspect.getsource(m)
    for pkg in ("customtkinter", "plyer", "win10toast"):
        assert f"import {pkg}" not in src
