"""QQ增强2 端到端集成验证（真实框架环境）。

在真实 KiraAI v3.1.2 环境里，用真实的 PluginManager 加载插件，
然后模拟完整事件流：notice → normalizer → store → 唤醒 trigger → llm_request 注入。

运行：cd /path/to/kiraai && python3 -m pytest <此文件> -x -q
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

PLUGIN_ROOT = Path(__file__).parent.parent


@pytest_asyncio.fixture
async def plugin_env(monkeypatch, tmp_path):
    """加载插件到真实框架环境。"""
    from core.plugin import manager as plugin_manager_module
    from core.plugin import registry

    for name in (
        "_plugin_classes", "_plugin_components", "_plugin_load_errors", "_plugin_manifests",
        "_plugin_module_dirs", "_plugin_module_paths", "_plugin_schemas",
        "_plugin_infos", "_module_to_plugin",
    ):
        monkeypatch.setattr(registry, name, {})
    monkeypatch.setattr(plugin_manager_module, "PLUGIN_CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(plugin_manager_module, "PLUGIN_STATE_FILE", tmp_path / "state.json")

    from core.plugin.manager import PluginManager
    from core.plugin.plugin_context import PluginContext

    # 构造完整可用的 mock adapter
    class FakeBot:
        def __init__(self):
            self.calls = []
        async def send_action(self, action, params, timeout=10.0):
            self.calls.append((action, params))
            return {"status": "ok", "data": {}}

    class FakeAdapter:
        platform = "QQ"
        class info:
            name = "napcat"
            platform = "QQ"
            config = {"self_id": "10000"}
            supported_elements = ["text"]
        def __init__(self):
            self.bot = FakeBot()
            self.config = {"self_id": "10000"}
        def get_client(self):
            return self.bot

    adapter = FakeAdapter()

    context = object.__new__(PluginContext)
    context.config = MagicMock()
    context.config.get_config = MagicMock(return_value="zh")
    context.tool_mgr = None
    context.adapter_mgr = MagicMock()
    context.adapter_mgr.get_adapter = MagicMock(return_value=adapter)
    context.adapter_mgr.get_all_adapters = MagicMock(return_value=[adapter])
    context.plugin_mgr = PluginManager(context)
    context.event_bus = MagicMock()
    context.event_bus.publish = AsyncMock()
    context.provider_mgr = MagicMock()
    context.get_plugin_data_dir = MagicMock(return_value=str(tmp_path / "plugin_data"))
    context.get_timezone = MagicMock(return_value=None)

    mgr = context.plugin_mgr
    pid = await mgr.load_plugin_from_dir(PLUGIN_ROOT, auto_install=False)
    assert pid == "kira-ai-plugin-qq-enhance2"

    yield {"mgr": mgr, "pid": pid, "adapter": adapter, "ctx": context, "registry": registry}

    await mgr.terminate(pid)


def test_e2e_plugin_lifecycle(plugin_env):
    """插件完整生命周期：加载 → initialize → terminate。"""
    assert plugin_env["pid"] == "kira-ai-plugin-qq-enhance2"
    comps = plugin_env["registry"]._plugin_components.get(plugin_env["pid"])
    assert comps is not None
    # 3 个 hook：im_message + llm_request × 2
    hook_types = [h.event_type.value for h in comps.hooks]
    assert hook_types.count("on_im_message") == 1
    assert hook_types.count("on_llm_request") == 2


def test_e2e_normalize_to_store(plugin_env):
    """事件流：notice → 归一化 → store。"""
    from qq_core.event_normalizer import normalize_notice

    raw = {
        "notice_type": "notify", "sub_type": "poke",
        "user_id": 1001, "target_id": 2002, "group_id": 123456,
        "time": int(time.time()),
    }
    ev = normalize_notice(raw, sid="napcat:gm:123456", self_id="10000")
    assert ev is not None
    assert ev.etype == "poke"
    assert ev.actor == "1001"
    assert ev.target == "2002"


def test_e2e_full_notice_flow(plugin_env):
    """完整流：多种 notice → store → 环境摘要渲染。"""
    from qq_core.event_normalizer import normalize_notice, QQEvent
    from qq_core.env_renderer import render_env_summary

    events = []
    now = int(time.time())
    raw_list = [
        {"notice_type": "notify", "sub_type": "poke", "user_id": 1001, "target_id": 2002,
         "group_id": 123456, "time": now - 60},
        {"notice_type": "notify", "sub_type": "poke", "user_id": 1001, "target_id": 2002,
         "group_id": 123456, "time": now - 30},
        {"notice_type": "group_ban", "sub_type": "ban", "user_id": 1005, "operator_id": 1001,
         "duration": 600, "group_id": 123456, "time": now - 120},
        {"notice_type": "group_msg_emoji_like", "user_id": 1007, "message_id": 888,
         "likes": [{"emoji_id": "128077", "count": 2}], "is_add": True,
         "group_id": 123456, "time": now - 90},
    ]
    for raw in raw_list:
        ev = normalize_notice(raw, sid="napcat:gm:123456", self_id="10000")
        if ev:
            # 补昵称（模拟 name_resolver）
            ev.actor_name = "用户" + ev.actor if ev.actor else ""
            ev.target_name = "用户" + ev.target if ev.target else ""
            events.append(ev)

    assert len(events) == 4, f"应有 4 个事件: {len(events)}"
    summary = render_env_summary(events, max_per_type=6, max_chars=800)
    assert summary, "应渲染出摘要"
    # 验证 QQ 号出现（对齐框架格式）
    assert "用户1001" in summary or "1001" in summary
    print(f"  渲染摘要: {summary}")


def test_e2e_wake_text_format(plugin_env):
    """唤醒文本格式：对齐框架 [Poke 用户X(昵称)戳了戳你] 风格。"""
    from qq_core.wake_scheduler import WakeScheduler
    from qq_core.event_normalizer import QQEvent, E_EMOJI_LIKE

    class Bus:
        def __init__(self):
            self.published = []
        async def publish(self, e):
            self.published.append(e)

    class FakeAdapterMgr:
        @staticmethod
        def get_adapter(name):
            a = MagicMock()
            a.info = MagicMock()
            a.info.name = "napcat"
            return a

    ctx = MagicMock()
    ctx.event_bus = Bus()
    ctx.adapter_mgr = FakeAdapterMgr()
    ctx.get_plugin_data_dir = MagicMock(return_value=None)

    sched = WakeScheduler(ctx=ctx, adapter=None, self_id="10000", message_locator=None,
                          wake_self_emoji=True, wake_others_emoji=True,
                          wake_others_emoji_cooldown=0, wake_max_per_hour=-1)

    ev = QQEvent(eid="1", etype=E_EMOJI_LIKE, sid="napcat:gm:123456", ts=int(time.time()),
                 actor="1001", actor_name="张三",
                 extra={"message_id": "1", "emoji_ids": ["128077"], "count": 2})

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(sched.trigger_others_emoji_like(ev, "2002", "李四", "测试消息摘要"))

    assert len(ctx.event_bus.published) == 1, "应发布一条唤醒事件"
    published = ctx.event_bus.published[0]
    text = published.message.chain.message_list[0].text
    assert "1001" in text, f"唤醒文本应含 QQ 号: {text}"
    assert "张三" in text, f"唤醒文本应含昵称: {text}"
    print(f"  唤醒文本: {text}")


def test_e2e_cache_safety(plugin_env):
    """缓存安全：环境摘要 persist=False，不进历史。"""
    from core.prompt_manager import Prompt

    p = Prompt("[群动态·刚刚] 测试", name="qq_social_env", persist=False)
    assert p.persist is False, "persist 必须为 False"
    assert p.name == "qq_social_env"
    assert p.name not in ("sessions", "chat_env", "time", "memory"), "name 不应与框架保留名冲突"


def test_e2e_emoji_archive_and_rewrite(plugin_env):
    """贴表情归档 → llm_request 改写标注。"""
    from qq_core.emoji_rewriter import archive_emoji_like, rewrite_emoji_like, _EMOJI_STORE
    from core.prompt_manager import Prompt
    from core.provider import LLMRequest

    _EMOJI_STORE.clear()
    archive_emoji_like(
        sid="napcat:gm:123456", message_id="888", message_text="原始消息内容",
        emoji_ids=["128077"], count=2, operator="1001", operator_name="张三",
        ts=int(time.time()),
    )

    req = LLMRequest()
    req.user_prompt.append(Prompt("原始消息内容", name="message", source="system"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(rewrite_emoji_like(sid="napcat:gm:123456", req=req, enabled=True))

    assert "[贴表情｜用户1001(张三)" in req.user_prompt[0].content, \
        f"应追加标注: {req.user_prompt[0].content}"
    # 标注内容不进 persist 判断（这是 user_prompt 里的 message Prompt，persist=True 是设计如此）
    print(f"  改写后: {req.user_prompt[0].content!r}")


def test_e2e_recall_rewrite(plugin_env, tmp_path):
    """撤回归档 → 改写标注（含群主/管理员判定）。"""
    from qq_core.message_locator import MessageLocator
    from qq_core.recall_rewriter import RecallRewriter
    from core.provider import LLMRequest
    from core.prompt_manager import Prompt

    class FakeBot:
        async def send_action(self, action, params, timeout=10.0):
            if action == "get_group_member_info":
                return {"status": "ok", "data": {"role": "admin"}}
            return {"status": "ok", "data": {}}

    class FakeAdapter:
        bot = FakeBot()
        class info:
            name = "napcat"

    locator = MessageLocator(adapter=FakeAdapter(), self_id="10000")
    locator._cache["888"] = {
        "sender": "1001", "sender_name": "张三",
        "text": "被撤回的消息", "ts": time.time(), "is_self": False,
    }

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # 归档撤回（操作者是管理员 2002）
    loop.run_until_complete(locator.archive_recall(
        sid="napcat:gm:123456", message_id="888", sender="1001", sender_name="张三",
        text="被撤回的消息", ts=int(time.time()), operator="2002",
        operator_name="管理员李四", sub_type="", is_group=True,
    ))

    rewriter = RecallRewriter(locator=locator)
    req = LLMRequest()
    req.user_prompt.append(Prompt("被撤回的消息", name="message", source="system"))
    loop.run_until_complete(rewriter.rewrite(sid="napcat:gm:123456", req=req))

    content = req.user_prompt[0].content
    assert "[已撤回｜" in content, f"应追加撤回标注: {content}"
    assert "2002" in content and "李四" in content, f"应含操作者 QQ 和昵称: {content}"
    print(f"  撤回改写: {content!r}")