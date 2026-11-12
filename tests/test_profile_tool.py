"""qq_profile 工具回归（第 16 轮）：self_id 兜底 / 个性签名 / 头像。

用户实机日志暴露的两个问题：
1. `qq_profile args: {}` → `请求 get_stranger_info 超时`
   根因：self_id 解析为空 → 发给协议端 `user_id=""` → 挂满 10s 才超时。
2. 查到了昵称/性别，但 `个性签名：（未提供）` `头像：（该协议端未提供）`
   根因（实测 NapCat 文档 + 实机返回）：
     · 个性签名的字段名是 **long_nick**，代码只读 `sign` / `personal_note`
     · get_stranger_info **根本不返回头像字段** → 必须用腾讯头像 CDN
       `https://q1.qlogo.cn/g?b=qq&nk={QQ}&s=640`
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tools.qq_profile import QQProfileTool  # noqa: E402


class _Ev:
    """最小事件替身（工具只读 self_id）。"""

    def __init__(self, self_id=""):
        self.self_id = self_id


class _Bot:
    def __init__(self, table=None, self_id="", raises=None, raise_for=()):
        self.table = table or {}
        self.self_id = self_id
        self.calls = []
        self.raises = raises
        self.raise_for = set(raise_for)

    async def send_action(self, action, params=None, timeout=10.0):
        params = dict(params or {})
        self.calls.append((action, params))
        if action in self.raise_for:
            raise self.raises
        r = self.table.get(action)
        if r is None:
            return {"status": "failed", "retcode": 1404, "data": None}
        if callable(r):
            r = r(params)
            if asyncio.iscoroutine(r):
                r = await r
        return r


class _Adapter:
    def __init__(self, bot, config=None):
        self.bot = bot
        self.config = config or {}


def _tool(adapter, self_id="", ctx=None):
    return QQProfileTool(ctx=ctx, adapter=adapter, self_id=self_id,
                         avatar_desc_prompt="", avatar_desc_timeout=1.0)


def _args(calls, action):
    return [p for a, p in calls if a == action]


def test_self_id_from_event_never_empty():
    """self_id 注册表为空时，用事件自带 self_id —— 绝不发 user_id=""。"""
    bot = _Bot({
        "get_login_info": {"status": "ok", "data": {"user_id": 3003003, "nickname": "紫小贱"}},
        "get_stranger_info": {"status": "ok", "data": {
            "user_id": 3003003, "nickname": "紫小贱", "sex": "unknown", "long_nick": "签名",
        }},
    })
    tool = _tool(_Adapter(bot, config={}), self_id="")
    out = asyncio.run(tool.execute(_Ev("3003003")))

    assert "紫小贱" in out
    sent = _args(bot.calls, "get_stranger_info")
    assert sent and sent[0]["user_id"] == "3003003", sent
    assert all(p.get("user_id") not in ("", None) for p in sent), sent


def test_no_self_id_returns_error_without_api_call():
    """完全拿不到 self_id → 直接报错，一次 API 都不发（这就是超时的根因）。"""
    bot = _Bot({})
    tool = _tool(_Adapter(bot, config={}), self_id="")
    out = asyncio.run(tool.execute(_Ev("")))

    assert "无法确定机器人自己的 QQ 号" in out
    assert bot.calls == [], bot.calls


def test_napcat_long_nick_and_cdn_avatar():
    """NapCat 真实返回：昵称/性别有，签名在 long_nick，头像字段完全没有。"""
    bot = _Bot({
        "get_stranger_info": {"status": "ok", "data": {
            "user_id": 769690776, "nickname": "周武", "sex": "male",
            "age": 20, "qid": "", "long_nick": "今天也要开心",
            "remark": "", "level": 3, "login_days": 100,
        }},
    })
    tool = _tool(_Adapter(bot, config={}))
    out = asyncio.run(tool.execute(_Ev("3003003"), user_id="769690776"))

    assert "周武" in out
    assert "今天也要开心" in out, out
    assert "男" in out
    assert "q1.qlogo.cn/g?b=qq&nk=769690776&s=640" in out, out
    assert "（该协议端未提供）" not in out.split("头像")[0], out


def test_signature_field_variants():
    """兼容 longNick / longnick / sign / personal_note 各种命名。"""
    for key in ("longNick", "longnick", "sign", "personal_note", "personalNote"):
        bot = _Bot({"get_stranger_info": {"status": "ok", "data": {
            "user_id": 1001, "nickname": "张三", "sex": 1, key: "签名X",
        }}})
        out = asyncio.run(_tool(_Adapter(bot, config={})).execute(_Ev(), user_id="1001"))
        assert "签名X" in out, (key, out)


def test_avatar_api_field_takes_priority():
    bot = _Bot({"get_stranger_info": {"status": "ok", "data": {
        "user_id": 1001, "nickname": "张三", "sex": 1,
        "headUrl": "https://example.com/a.jpg",
    }}})
    out = asyncio.run(_tool(_Adapter(bot, config={})).execute(_Ev(), user_id="1001"))
    assert "https://example.com/a.jpg" in out
    assert "q1.qlogo.cn" not in out


def test_avatar_cdn_preferred_over_api():
    """数字 QQ → 直接用腾讯 CDN（零 API 调用），不再等 get_qq_avatar 超时。"""
    bot = _Bot({
        "get_stranger_info": {"status": "ok", "data": {"user_id": 1001, "nickname": "张三"}},
        "get_qq_avatar": {"status": "ok", "data": {"url": "https://example.com/from-api.jpg"}},
    })
    out = asyncio.run(_tool(_Adapter(bot, config={})).execute(_Ev(), user_id="1001"))
    assert "https://q1.qlogo.cn/g?b=qq&nk=1001&s=640" in out, out
    assert all(a != "get_qq_avatar" for a, _ in bot.calls), bot.calls


def test_get_qq_avatar_used_for_non_numeric_id():
    """非纯数字 ID（CDN 拼不出来）→ 才走 get_qq_avatar。"""
    bot = _Bot({
        "get_stranger_info": {"status": "ok", "data": {"user_id": "u_abc", "nickname": "张三"}},
        "get_qq_avatar": {"status": "ok", "data": {"url": "https://example.com/from-api.jpg"}},
    })
    out = asyncio.run(_tool(_Adapter(bot, config={})).execute(_Ev(), user_id="u_abc"))
    assert "https://example.com/from-api.jpg" in out, out


def test_timeout_self_still_shows_login_info():
    """get_stranger_info 超时：查自己时用 get_login_info 兜底，不整条失败。"""
    bot = _Bot(
        {"get_login_info": {"status": "ok", "data": {"user_id": 3003003, "nickname": "紫小贱"}}},
        raises=asyncio.TimeoutError(),
        raise_for=("get_stranger_info",),
    )
    tool = _tool(_Adapter(bot, config={"bot_pid": "3003003"}))
    out = asyncio.run(tool.execute(_Ev("3003003")))

    assert "紫小贱" in out, out
    assert "超时" in out, out


def test_timeout_third_party_reports_clearly():
    bot = _Bot(
        {},
        raises=asyncio.TimeoutError(),
        raise_for=("get_stranger_info",),
    )
    out = asyncio.run(_tool(_Adapter(bot, config={})).execute(_Ev(), user_id="1001"))
    assert "超时" in out or "没有响应" in out, out


def test_group_tool_self_id_fallback():
    """qq_group_profile 同样不能把空 user_id 发给协议端。"""
    from tools.qq_group_profile import QQGroupProfileTool

    class _Ev2(_Ev):
        class session:
            session_id = "188395693"

        def is_group_message(self):
            return True

    bot = _Bot({"get_group_member_info": {"status": "ok", "data": {
        "user_id": 3003003, "nickname": "紫小贱", "card": "", "title": "", "role": "member",
    }}})
    tool = QQGroupProfileTool(ctx=None, adapter=_Adapter(bot), self_id="")
    out = asyncio.run(tool.execute(_Ev2("3003003")))

    assert "紫小贱" in out
    sent = _args(bot.calls, "get_group_member_info")
    assert sent and sent[0]["user_id"] == "3003003", sent


def test_group_tool_no_self_id_no_call():
    from tools.qq_group_profile import QQGroupProfileTool

    class _Ev2(_Ev):
        class session:
            session_id = "188395693"

        def is_group_message(self):
            return True

    bot = _Bot({})
    tool = QQGroupProfileTool(ctx=None, adapter=_Adapter(bot), self_id="")
    out = asyncio.run(tool.execute(_Ev2("")))
    assert "无法确定机器人自己的 QQ 号" in out
    assert bot.calls == []
