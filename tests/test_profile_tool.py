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


# ==================== 换头像 ====================

class _Msg:
    def __init__(self, chain):
        self.chain = chain


class _EvWithMsgs(_Ev):
    def __init__(self, self_id="", messages=None):
        super().__init__(self_id)
        self.messages = messages or []


def _self_tables(**extra):
    t = {
        "get_login_info": {"status": "ok", "data": {"user_id": 3003003, "nickname": "紫小贱"}},
        "get_stranger_info": {"status": "ok", "data": {
            "user_id": 3003003, "nickname": "紫小贱", "long_nick": "签名",
        }},
        "set_qq_avatar": {"status": "ok", "data": None},
    }
    t.update(extra)
    return t


def test_avatar_recent_uses_latest_image():
    """avatar='recent' → 用当前消息里最新的一张图（图片在 prompt 里只有描述，LLM 拿不到路径）。"""
    from core.chat.message_elements import Image

    bot = _Bot(_self_tables())
    tool = _tool(_Adapter(bot, config={}), self_id="3003003")
    ev = _EvWithMsgs("3003003", [
        _Msg([Image("https://example.com/old.jpg")]),
        _Msg([Image("https://example.com/new.jpg")]),
    ])
    out = asyncio.run(tool.execute(ev, avatar="recent"))
    assert "头像已更新" in out, out
    sent = _args(bot.calls, "set_qq_avatar")
    assert sent and sent[0]["file"] == "https://example.com/new.jpg", sent


def test_avatar_recent_without_image():
    bot = _Bot(_self_tables())
    tool = _tool(_Adapter(bot, config={}), self_id="3003003")
    out = asyncio.run(tool.execute(_EvWithMsgs("3003003", [_Msg([])]), avatar="recent"))
    assert "没有图片" in out, out
    assert _args(bot.calls, "set_qq_avatar") == []


def test_avatar_set_failure_reason_surfaced():
    """NapCat 用 throw 报错，原因要原样透给用户（而不是笼统说"不支持"）。"""
    bot = _Bot(_self_tables(), raises=RuntimeError("头像xxx设置失败,file字段可能格式不正确"),
               raise_for=("set_qq_avatar",))
    tool = _tool(_Adapter(bot, config={}), self_id="3003003")
    out = asyncio.run(tool.execute(_Ev("3003003"), avatar="https://example.com/x.jpg"))
    assert "file字段可能格式不正确" in out, out
    assert "可能不支持" not in out, out


class _Session:
    def __init__(self, session_type="gm", session_id="188395693"):
        self.session_type = session_type
        self.session_id = session_id


class _EvGroup(_Ev):
    def __init__(self, self_id="", group_id="188395693", messages=None):
        super().__init__(self_id)
        self.session = _Session("gm", group_id)
        self.messages = messages or []

    def is_group_message(self):
        return True


def test_group_query_shows_card_as_extra_line():
    """群聊里查资料：昵称 + **额外**群名片（两者并存，不是替代）。"""
    bot = _Bot({
        "get_stranger_info": {"status": "ok", "data": {
            "user_id": 769690776, "nickname": "周武", "sex": "male",
            "long_nick": "签名",
        }},
        "get_group_member_info": {"status": "ok", "data": {
            "user_id": 769690776, "card": "老王", "nickname": "周武", "role": "admin",
        }},
    })
    out = asyncio.run(_tool(_Adapter(bot, config={})).execute(
        _EvGroup("3003003"), user_id="769690776"))

    assert "昵称：周武" in out, out
    assert "群名片：老王" in out, out
    assert "群角色：管理员" in out, out
    assert out.index("昵称：") < out.index("群名片："), "群名片应在昵称之后单独一行"


def test_private_query_has_no_card_line():
    bot = _Bot({"get_stranger_info": {"status": "ok", "data": {
        "user_id": 1001, "nickname": "张三", "sex": 1,
    }}})
    out = asyncio.run(_tool(_Adapter(bot, config={})).execute(_Ev(), user_id="1001"))
    assert "群名片" not in out, out


def test_signature_uses_set_self_longnick():
    """修改签名：NapCat 专用 set_self_longnick（longNick），不再只靠 set_qq_profile。"""
    bot = _Bot({
        "set_self_longnick": {"status": "ok", "data": None},
        "get_login_info": {"status": "ok", "data": {"user_id": 3003003, "nickname": "紫小贱"}},
        "get_stranger_info": {"status": "ok", "data": {"user_id": 3003003, "nickname": "紫小贱"}},
    })
    out = asyncio.run(_tool(_Adapter(bot, config={}), self_id="3003003").execute(
        _Ev("3003003"), personal_note="新签名"))
    sent = _args(bot.calls, "set_self_longnick")
    assert sent and sent[0]["longNick"] == "新签名", bot.calls
    assert "个性签名已更新" in out, out
    assert _args(bot.calls, "set_qq_profile") == [], "不应再走 set_qq_profile 的签名路径"


def test_signature_fallback_to_set_qq_profile():
    """协议端不支持 set_self_longnick 时回退 OB11 personal_note。"""
    bot = _Bot({
        "set_qq_profile": {"status": "ok", "data": None},
        "get_login_info": {"status": "ok", "data": {"user_id": 3003003, "nickname": "紫小贱"}},
        "get_stranger_info": {"status": "ok", "data": {"user_id": 3003003, "nickname": "紫小贱"}},
    })
    out = asyncio.run(_tool(_Adapter(bot, config={}), self_id="3003003").execute(
        _Ev("3003003"), personal_note="新签名"))
    sent = _args(bot.calls, "set_qq_profile")
    assert sent and sent[0].get("personal_note") == "新签名", bot.calls
    assert "个性签名已更新" in out, out


def test_signature_failure_reported():
    """两个接口都失败 → 明确报错（不能静默当成功）。"""
    bot = _Bot({"set_qq_profile": {"status": "failed", "wording": "无权限"},
                "get_login_info": {"status": "ok", "data": {"user_id": 3003003, "nickname": "x"}},
                "get_stranger_info": {"status": "ok", "data": {"user_id": 3003003, "nickname": "x"}}})
    out = asyncio.run(_tool(_Adapter(bot, config={}), self_id="3003003").execute(
        _Ev("3003003"), personal_note="新签名"))
    assert "个性签名修改失败" in out, out


# ==================== 群名片修改（qq_group_profile）====================

def _group_tool(bot, self_id="3003003"):
    from tools.qq_group_profile import QQGroupProfileTool
    return QQGroupProfileTool(ctx=None, adapter=_Adapter(bot), self_id=self_id)


def test_group_card_update_own():
    """改自己的群名片 → set_group_card(group_id/user_id/card)。"""
    bot = _Bot({
        "set_group_card": {"status": "ok", "data": None},
        "get_group_member_info": {"status": "ok", "data": {
            "user_id": 3003003, "nickname": "紫小贱", "card": "新名片",
            "title": "", "role": "admin",
        }},
    })
    out = asyncio.run(_group_tool(bot).execute(_EvGroup("3003003"), card="新名片"))
    sent = _args(bot.calls, "set_group_card")
    assert sent, bot.calls
    assert sent[0] == {"group_id": "188395693", "user_id": "3003003", "card": "新名片"}, sent
    assert "群名片：新名片" in out, out


def test_group_card_clear_with_empty_string():
    """空字符串 = 清除群名片（注意不是"不修改"）。"""
    bot = _Bot({
        "set_group_card": {"status": "ok", "data": None},
        "get_group_member_info": {"status": "ok", "data": {
            "user_id": 3003003, "nickname": "紫小贱", "card": "", "title": "", "role": "member",
        }},
    })
    asyncio.run(_group_tool(bot).execute(_EvGroup("3003003"), card=""))
    sent = _args(bot.calls, "set_group_card")
    assert sent and sent[0]["card"] == "", sent


def test_group_card_query_other_readonly():
    """查他人群名片：只读，不发 set_group_card。"""
    bot = _Bot({"get_group_member_info": {"status": "ok", "data": {
        "user_id": 1001, "nickname": "张三", "card": "老张", "title": "活跃", "role": "member",
    }}})
    out = asyncio.run(_group_tool(bot).execute(_EvGroup("3003003"), user_id="1001"))
    assert "群名片：老张" in out and "昵称：张三" in out and "头衔：活跃" in out, out
    assert _args(bot.calls, "set_group_card") == []


def test_avatar_local_path_inlined_as_base64():
    """本地路径头像：读成 base64:// 再发（NapCat 在 Docker/别的主机也能用）。"""
    import os
    import tempfile

    f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    f.write(b"\x89PNG\r\n\x1a\n" + b"1" * 200)
    f.close()
    try:
        bot = _Bot(_self_tables())
        tool = _tool(_Adapter(bot, config={}), self_id="3003003")
        out = asyncio.run(tool.execute(_Ev("3003003"), avatar=f.name))
        sent = _args(bot.calls, "set_qq_avatar")
        assert sent, out
        assert sent[0]["file"].startswith("base64://"), sent[0]["file"][:40]
        assert "头像已更新" in out, out
    finally:
        os.unlink(f.name)


def test_avatar_missing_local_path_errors_early():
    """路径不存在 → 明确报错，不发 API（协议端的报错太模糊）。"""
    bot = _Bot(_self_tables())
    tool = _tool(_Adapter(bot, config={}), self_id="3003003")
    out = asyncio.run(tool.execute(_Ev("3003003"), avatar="/nope/not-here.png"))
    assert "本地文件不存在" in out, out
    assert _args(bot.calls, "set_qq_avatar") == []
