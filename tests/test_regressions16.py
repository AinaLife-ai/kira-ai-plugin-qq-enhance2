"""第 18 轮回归：适配器实时绑定 + 头像识别失败原因 + 连接错误文案。

对应 2026-11-25 实机：
1. 明明开了「启用资料工具」，工具列表里却没有 qq_profile
   （插件启动时没有/还没有适配器 → initialize 早退 → 工具永不注册）
2. 同一台机器连接正常，但工具报 `NapCat WebSocket 未连接`
   （适配器重启/重建后，插件缓存的是已关闭的旧实例）
3. 头像只有 URL、没有 VLM 描述，而且完全看不出原因
   （get_default_vlm() 抛错被吞 → 返回空串）
"""

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from qq_core.adapter_bind import resolve_adapter, get_live_adapter  # noqa: E402
from qq_core.avatar_view import clear_l1_cache, describe_avatar_ex  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


class _Adapter:
    def __init__(self, name, self_id="", platform="QQ", alive=True):
        self.name = name
        self.platform = platform
        self.config = {"bot_pid": self_id}
        self.info = types.SimpleNamespace(name=name, platform=platform)
        self.bot = types.SimpleNamespace(self_id=self_id, alive=alive)


class _Mgr:
    def __init__(self, adapters):
        self._adapters = adapters

    def get_adapter(self, name):
        return self._adapters.get(name)


class _Ctx:
    def __init__(self, adapters):
        self.adapter_mgr = _Mgr(adapters)


def test_resolve_prefers_live():
    old = _Adapter("qq", "111")
    new = _Adapter("qq", "111")
    ctx = _Ctx({"qq": new})
    cached = {"qq": (old, "111")}
    adapter, self_id = resolve_adapter(
        ctx, "qq", cached, old, "111",
        lambda a: a.config.get("bot_pid", ""),
    )
    check("实时实例优先（不返回僵尸实例）", adapter is new, "got old" if adapter is old else "")
    check("self_id 从活实例提取", self_id == "111", self_id)
    check("缓存被就地刷新", cached["qq"][0] is new, "cache not refreshed")


def test_resolve_falls_back_to_cache():
    old = _Adapter("qq", "222")
    ctx = _Ctx({})
    adapter, self_id = resolve_adapter(
        ctx, "qq", {"qq": (old, "222")}, None, "",
        lambda a: a.config.get("bot_pid", ""),
    )
    check("没有实时实例时用缓存", adapter is old and self_id == "222", (adapter, self_id))


def test_resolve_falls_back_to_default():
    default = _Adapter("qq2", "333")
    adapter, self_id = resolve_adapter({}, "unknown", {}, default, "333", None)
    check("都没有时用默认实例", adapter is default and self_id == "333", (adapter, self_id))


def test_resolve_keeps_cached_self_id_when_live_has_none():
    live = _Adapter("qq", "")
    cached = {"qq": (_Adapter("qq", "444"), "444")}
    _, self_id = resolve_adapter(_Ctx({"qq": live}), "qq", cached, None, "", lambda a: a.config.get("bot_pid", ""))
    check("活实例没 self_id 时回退缓存值", self_id == "444", self_id)


def test_get_live_adapter_handles_broken_manager():
    class Boom:
        def get_adapter(self, name):
            raise RuntimeError("boom")

    check("adapter_mgr 抛异常 → None", get_live_adapter(types.SimpleNamespace(adapter_mgr=Boom()), "qq") is None)
    check("没有 adapter_mgr → None", get_live_adapter(object(), "qq") is None)


# ---------------- 头像识别原因 ----------------

class _ProviderMgrNone:
    def get_default_vlm(self):
        raise ValueError("default_vlm not set")


class _ProviderMgrOk:
    def get_default_vlm(self):
        return object()


class _Ctx2:
    image_desc_cache = None

    def __init__(self, pm):
        self.provider_mgr = pm
        self.config = {"locale": {"lang": "zh"}}


def test_avatar_reason_no_vlm():
    clear_l1_cache()
    desc, reason = asyncio.run(describe_avatar_ex(
        "https://q1.qlogo.cn/g?b=qq&nk=1&s=640", ctx=_Ctx2(_ProviderMgrNone()), ttl=60,
    ))
    check("没配默认 VLM → 描述为空", desc == "", desc)
    check("没配默认 VLM → 原因可读", "未配置默认视觉模型" in reason, reason)


def test_avatar_reason_vlm_error():
    import qq_core.avatar_view as av
    clear_l1_cache()
    orig = av._desc_img

    async def boom(**kw):
        raise RuntimeError("provider 502")

    av._desc_img = boom
    try:
        desc, reason = asyncio.run(describe_avatar_ex(
            "https://q1.qlogo.cn/g?b=qq&nk=2&s=640", vlm=object(), ttl=0,
        ))
    finally:
        av._desc_img = orig
        clear_l1_cache()
    check("VLM 异常 → 描述为空", desc == "", desc)
    check("VLM 异常 → 原因含异常信息", "识别失败" in reason and "provider 502" in reason, reason)


def test_avatar_reason_success_empty():
    import qq_core.avatar_view as av
    clear_l1_cache()
    orig = av._desc_img

    async def ok(**kw):
        return "一只蓝猫"

    av._desc_img = ok
    try:
        desc, reason = asyncio.run(describe_avatar_ex(
            "https://q1.qlogo.cn/g?b=qq&nk=3&s=640", vlm=object(), ttl=0,
        ))
    finally:
        av._desc_img = orig
        clear_l1_cache()
    check("成功 → 返回描述", desc == "一只蓝猫", desc)
    check("成功 → 无失败原因", reason == "", reason)


# ---------------- 工具输出 ----------------

class _Bot:
    def __init__(self, table=None, raises=None):
        self.table = table or {}
        self.raises = raises
        self.calls = []

    async def send_action(self, action, params=None, timeout=10.0):
        self.calls.append(action)
        if self.raises and action == "get_stranger_info":
            raise self.raises
        return self.table.get(action, {"status": "failed", "data": None})


class _Adapter2:
    def __init__(self, bot):
        self.bot = bot
        self.config = {"bot_pid": "100"}


class _Ev:
    self_id = "100"

    @staticmethod
    def is_group_message():
        return False


def _tool(bot, **kw):
    from tools.qq_profile import QQProfileTool

    class Ctx:
        class provider_mgr:
            @staticmethod
            def get_default_vlm():
                return object()

        config = {"locale": {"lang": "zh"}}
        image_desc_cache = None

    return QQProfileTool(ctx=Ctx(), adapter=_Adapter2(bot), self_id="100", **kw)


def test_tool_shows_avatar_reason():
    bot = _Bot({
        "get_stranger_info": {"status": "ok", "data": {
            "user_id": 769690776, "nickname": "周武", "sex": "male",
        }},
    })
    tool = _tool(bot)

    async def fake(url):
        tool._avatar_desc_reason = "未配置默认视觉模型（models.default_vlm 为空）"
        return ""

    tool._describe_avatar = fake
    out = asyncio.run(tool.execute(_Ev(), user_id="769690776", look_avatar=True))
    check("头像失败时输出原因", "（未识别：未配置默认视觉模型" in out, out[-120:])


def test_tool_connection_error_message():
    bot = _Bot(raises=ConnectionError("NapCat WebSocket 未连接，无法执行 get_stranger_info"))
    tool = _tool(bot)
    out = asyncio.run(tool.execute(_Ev(), user_id="769690776"))
    check("连接错误不再谎称“用户不存在”", "用户 769690776 不存在" not in out, out)
    check("连接错误给出明确原因", "协议端连接不可用" in out, out)
    check("提示重启适配器", "适配器" in out, out)



def test_tool_always_shows_avatar_url():
    """识别成功时也要给 URL（bot 可能想引用/发送原图），描述另起一行。"""
    bot = _Bot({
        "get_stranger_info": {"status": "ok", "data": {
            "user_id": 769690776, "nickname": "周武", "sex": "male",
        }},
    })
    tool = _tool(bot)

    async def fake(url):
        return "一只蓝猫"

    tool._describe_avatar = fake
    out = asyncio.run(tool.execute(_Ev(), user_id="769690776", look_avatar=True))
    check("识别成功时仍输出头像 URL", "头像：https://q1.qlogo.cn/g?b=qq&nk=769690776&s=640" in out, out[-160:])
    check("描述单独成行", "头像内容：一只蓝猫" in out, out[-160:])
    check("不再把描述当成头像值", "头像：一只蓝猫" not in out, out[-160:])


def test_tool_url_only_when_recognition_off():
    bot = _Bot({
        "get_stranger_info": {"status": "ok", "data": {
            "user_id": 769690776, "nickname": "周武", "sex": "male",
        }},
    })
    tool = _tool(bot)
    out = asyncio.run(tool.execute(_Ev(), user_id="769690776", look_avatar=False))
    check("关闭识别时给 URL", "头像：https://q1.qlogo.cn/g?b=qq&nk=769690776&s=640" in out, out[-120:])
    check("关闭识别时没有描述行", "头像内容：" not in out, out[-120:])


def _main():
    test_resolve_prefers_live()
    test_resolve_falls_back_to_cache()
    test_resolve_falls_back_to_default()
    test_resolve_keeps_cached_self_id_when_live_has_none()
    test_get_live_adapter_handles_broken_manager()
    test_avatar_reason_no_vlm()
    test_avatar_reason_vlm_error()
    test_avatar_reason_success_empty()
    test_tool_shows_avatar_reason()
    test_tool_connection_error_message()
    test_tool_always_shows_avatar_url()
    test_tool_url_only_when_recognition_off()
    print("\n" + "=" * 60)
    print(f"回归18: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for name, detail in FAIL:
            print(f"  FAIL {name}: {detail}")
        sys.exit(1)
    print("=== 全部通过 ===")


if __name__ == "__main__":
    _main()
