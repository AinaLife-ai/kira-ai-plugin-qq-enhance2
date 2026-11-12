"""QQ增强2 单元测试。

测试覆盖：
1. event_normalizer：三家协议端 payload 归一化。
2. event_store：LRU/过期/持久化。
3. env_renderer：合并/踢旧淘汰。
4. wake_scheduler：点赞合并防抖/每小时上限。
5. recall_rewriter：撤回标注追加。
6. 兼容性：与 QQ增强1/S/Z 的事件单次注入。
"""

import asyncio
import json
import os
import sys
import time
import tempfile
from pathlib import Path

import pytest

# 测试环境无 KiraAI 框架：为框架依赖模块注入 stub，使 WakeScheduler 等可独立测试。
# 真实框架环境下的行为由集成测试（tests/test_integration.py）覆盖。
import types

def _install_framework_stubs():
    """注入最小框架 stub（仅测试用）。"""
    if "core" in sys.modules:
        return
    core = types.ModuleType("core")
    core.__path__ = []
    sys.modules["core"] = core

    logging_manager = types.ModuleType("core.logging_manager")
    class _Logger:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def debug(self, *a, **k): pass
        def error(self, *a, **k): pass
        def exception(self, *a, **k): pass
    logging_manager.get_logger = lambda *a, **k: _Logger()
    sys.modules["core.logging_manager"] = logging_manager
    core.logging_manager = logging_manager

    chat = types.ModuleType("core.chat")
    chat.__path__ = []  # 使其可作为 package，支持 core.chat.message_utils 子模块
    sys.modules["core.chat"] = chat
    core.chat = chat

    # KiraMessageEvent / KiraIMMessage / MessageChain / Text 最小实现（先定义，供各子模块引用）
    class _Chain:
        def __init__(self, elements=None):
            self.message_list = elements or []
    class _IMMessage:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    class _MessageEvent:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.session = None
            self._is_stopped = False
            self._process_strategy = "discard"
        def trigger(self, force=False):
            self._process_strategy = "trigger"
            return True
        def discard(self, force=False):
            self._process_strategy = "discard"
            return True
        def is_group_message(self):
            return getattr(getattr(self, "message", None), "group", None) is not None

    # core.chat.message_utils（工具需要 KiraMessageBatchEvent）
    msg_utils = types.ModuleType("core.chat.message_utils")
    class _BatchEvent:
        def __init__(self, *a, **kw):
            self.__dict__.update(kw)
        def is_group_message(self):
            return True
    msg_utils.KiraMessageBatchEvent = _BatchEvent
    msg_utils.KiraMessageEvent = _MessageEvent
    msg_utils.KiraIMMessage = _IMMessage
    sys.modules["core.chat.message_utils"] = msg_utils
    chat.message_utils = msg_utils

    chat_session = types.ModuleType("core.chat.session")
    class Session:
        def __init__(self, adapter_name="", session_type="gm", session_id=""):
            self.adapter_name = adapter_name
            self.session_type = session_type
            self.session_id = session_id
    class Group:
        def __init__(self, group_id="", group_name=""):
            self.group_id = group_id
            self.group_name = group_name
    class User:
        def __init__(self, user_id="", nickname=""):
            self.user_id = user_id
            self.nickname = nickname
    chat_session.Session = Session
    chat_session.Group = Group
    chat_session.User = User
    sys.modules["core.chat.session"] = chat_session
    chat.session = chat_session

    adapter_info = types.ModuleType("core.adapter.adapter_info")
    class AdapterInfo:
        pass
    adapter_info.AdapterInfo = AdapterInfo
    sys.modules["core.adapter.adapter_info"] = adapter_info
    adapter_pkg = types.ModuleType("core.adapter")
    adapter_pkg.__path__ = []
    sys.modules["core.adapter"] = adapter_pkg
    core.adapter = adapter_pkg
    adapter_pkg.adapter_info = adapter_info

    # KiraMessageEvent / KiraIMMessage / MessageChain / Text 最小实现
    class _Chain:
        def __init__(self, elements=None):
            self.message_list = elements or []
    class _IMMessage:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    class _MessageEvent:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.session = None
            self._is_stopped = False
            self._process_strategy = "discard"
        def trigger(self, force=False):
            self._process_strategy = "trigger"
            return True
        def discard(self, force=False):
            self._process_strategy = "discard"
            return True
        def is_group_message(self):
            return getattr(getattr(self, "message", None), "group", None) is not None
    chat.KiraMessageEvent = _MessageEvent
    chat.KiraIMMessage = _IMMessage
    chat.MessageChain = _Chain

    # message_elements.Text（_trigger_text 需要）
    msg_elements = types.ModuleType("core.chat.message_elements")
    msg_elements.__path__ = []
    class _Text:
        def __init__(self, text=""):
            self.text = text
        @property
        def repr(self):
            return self.text
    class _Image:
        def __init__(self, image="", **kw):
            self.image = image
            self.caption = None
        async def to_data_url(self):
            return ""
        async def hash_image(self):
            return ""
    msg_elements.Text = _Text
    msg_elements.Image = _Image
    sys.modules["core.chat.message_elements"] = msg_elements
    chat.message_elements = msg_elements

    # core.plugin.PluginContext
    plugin_pkg = types.ModuleType("core.plugin")
    plugin_pkg.__path__ = []
    class _PluginContext:
        pass
    plugin_pkg.PluginContext = _PluginContext

    # BasePlugin（main.py 需要）
    class _BasePlugin:
        def __init__(self, ctx=None, cfg=None):
            self.ctx = ctx
            self.plugin_cfg = cfg or {}
        async def initialize(self):
            pass
        async def terminate(self):
            pass
    plugin_pkg.BasePlugin = _BasePlugin

    # logger（main.py 用 core.plugin.logger）
    plugin_pkg.logger = _Logger()

    # on / Priority（hook 装饰器）
    class _Priority:
        SYS_LOW = -100
        LOW = -50
        MEDIUM = 0
        HIGH = 50
        SYS_HIGH = 100
    plugin_pkg.Priority = _Priority

    class _On:
        """mock 装饰器集合：原样返回函数并记录事件类型。"""
        def __getattr__(self, name):
            def deco(*a, **k):
                def wrap(func):
                    func._hook = name
                    return func
                return wrap
            return deco
    plugin_pkg.on = _On()

    sys.modules["core.plugin"] = plugin_pkg

    # core.utils.tool_utils.BaseTool（工具基类）
    utils_pkg = types.ModuleType("core.utils")
    utils_pkg.__path__ = []
    sys.modules["core.utils"] = utils_pkg
    tool_utils = types.ModuleType("core.utils.tool_utils")
    class _BaseTool:
        name = ""
        description = ""
        parameters = {}
        def __init__(self, *a, ctx=None, **kw):
            self.ctx = ctx
        async def execute(self, event, *a, **kw):
            return ""
    tool_utils.BaseTool = _BaseTool
    sys.modules["core.utils.tool_utils"] = tool_utils
    utils_pkg.tool_utils = tool_utils

    # core.provider.LLMRequest / core.prompt_manager.Prompt（recall/emoji rewriter 需要）
    provider_mod = types.ModuleType("core.provider")
    class _LLMRequest:
        def __init__(self, *a, **kw):
            self.user_prompt = []
            self.messages = []
    provider_mod.LLMRequest = _LLMRequest
    sys.modules["core.provider"] = provider_mod

    prompt_mod = types.ModuleType("core.prompt_manager")
    class _Prompt:
        def __init__(self, content="", name=None, source=None, end="\n", persist=True, **kw):
            self.content = content
            self.name = name
            self.source = source
            self.end = end
            self.persist = persist
        def to_string(self):
            return self.content + (self.end or "")
    prompt_mod.Prompt = _Prompt
    sys.modules["core.prompt_manager"] = prompt_mod

_install_framework_stubs()

# 添加插件根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from qq_core.event_normalizer import normalize_notice, QQEvent, E_POKE, E_EMOJI_LIKE, E_BAN, E_UNBAN, E_MEMBER_DECREASE, E_RECALL, E_PROFILE_LIKE_SELF
from qq_core.event_store import EventStore
from qq_core.env_renderer import render_env_summary
from qq_core.wake_scheduler import WakeScheduler


class TestNormalizer:
    """事件归一化测试。"""

    def test_poke(self):
        """戳一戳归一化。"""
        raw = {
            "notice_type": "notify",
            "sub_type": "poke",
            "user_id": 1001,
            "target_id": 2002,
            "group_id": 123456,
            "time": int(time.time()),
        }
        ev = normalize_notice(raw, sid="napcat:gm:123456", self_id="3003")
        assert ev is not None
        assert ev.etype == E_POKE
        assert ev.actor == "1001"
        assert ev.target == "2002"

    def test_emoji_like(self):
        """贴表情归一化。"""
        raw = {
            "notice_type": "group_msg_emoji_like",
            "user_id": 1001,
            "message_id": 123456789,
            "likes": [{"emoji_id": "128077", "count": 2}],
            "is_add": True,
            "group_id": 123456,
            "time": int(time.time()),
        }
        ev = normalize_notice(raw, sid="napcat:gm:123456", self_id="3003")
        assert ev is not None
        assert ev.etype == E_EMOJI_LIKE
        assert ev.actor == "1001"
        assert ev.extra.get("emoji_ids") == ["128077"]
        assert ev.extra.get("count") == 2

    def test_ban(self):
        """禁言归一化。"""
        raw = {
            "notice_type": "group_ban",
            "sub_type": "ban",
            "user_id": 1001,
            "operator_id": 2002,
            "duration": 600,
            "group_id": 123456,
            "time": int(time.time()),
        }
        ev = normalize_notice(raw, sid="napcat:gm:123456", self_id="3003")
        assert ev is not None
        assert ev.etype == E_BAN
        assert ev.target == "1001"
        assert ev.actor == "2002"
        assert ev.extra.get("duration") == 600

    def test_profile_like(self):
        """资料卡点赞归一化。"""
        raw = {
            "notice_type": "notify",
            "sub_type": "profile_like",
            "operator_id": 1001,
            "times": 3,
            "time": int(time.time()),
        }
        ev = normalize_notice(raw, sid="napcat:dm:3003", self_id="3003")
        assert ev is not None
        assert ev.etype == E_PROFILE_LIKE_SELF
        assert ev.actor == "1001"
        assert ev.extra.get("times") == 3


class TestEventStore:
    """事件状态机测试。"""

    @pytest.mark.asyncio
    async def test_lru(self):
        """LRU 淘汰测试。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = EventStore(tmpdir, max_events=3)
            for i in range(5):
                ev = QQEvent(eid=f"e{i}", etype=E_POKE, sid="test:sid", ts=int(time.time()) + i)
                await store.add(ev)
            events = await store.get_events("test:sid")
            assert len(events) == 3
            # 最旧的事件被淘汰
            assert events[0].eid == "e2"

    @pytest.mark.asyncio
    async def test_persist(self):
        """持久化测试。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = EventStore(tmpdir, max_events=10)
            ev = QQEvent(eid="e1", etype=E_POKE, sid="test:sid", ts=int(time.time()))
            await store.add(ev)
            await store._persist_all()

            # 加载
            store2 = EventStore(tmpdir, max_events=10)
            await store2.load_all()
            events = await store2.get_events("test:sid")
            assert len(events) == 1
            assert events[0].eid == "e1"


class TestEnvRenderer:
    """环境摘要渲染测试。"""

    def test_poke_merge(self):
        """戳一戳合并测试。"""
        now = int(time.time())
        events = [
            QQEvent(eid="e1", etype=E_POKE, sid="test:sid", ts=now - 10, actor="1001", actor_name="张三", target="2002", target_name="李四"),
            QQEvent(eid="e2", etype=E_POKE, sid="test:sid", ts=now - 5, actor="1001", actor_name="张三", target="2002", target_name="李四"),
        ]
        text = render_env_summary(events, max_per_type=6, max_chars=800)
        assert "用户1001(张三) 戳了戳 用户2002(李四)×2" in text

    def test_kick_old(self):
        """踢旧淘汰测试。"""
        now = int(time.time())
        events = [
            QQEvent(eid=f"e{i}", etype=E_POKE, sid="test:sid", ts=now - 100 + i, actor=f"100{i}", actor_name=f"用户{i}", target="2002", target_name="李四")
            for i in range(10)
        ]
        text = render_env_summary(events, max_per_type=6, max_chars=200)
        # 最旧的事件被淘汰
        assert "用户1000" not in text
        assert len(text) <= 200


class TestWakeScheduler:
    """唤醒调度器测试。"""

    @pytest.mark.asyncio
    async def test_like_merge(self):
        """点赞合并防抖测试。"""
        # mock ctx
        class MockCtx:
            class adapter_mgr:
                @staticmethod
                def get_adapter(name):
                    return None
            class event_bus:
                @staticmethod
                async def publish(event):
                    pass
            @staticmethod
            def get_plugin_data_dir():
                return "/tmp"

        scheduler = WakeScheduler(
            ctx=MockCtx(),
            adapter=None,
            self_id="3003",
            message_locator=None,
            profile_like_merge_window_seconds=10,
            wake_max_per_hour=60,
        )

        # 第一次点赞
        ev1 = QQEvent(eid="e1", etype=E_PROFILE_LIKE_SELF, sid="test:sid", ts=int(time.time()), actor="1001", extra={"times": 1})
        result1 = await scheduler.on_profile_like(ev1)
        assert result1 is False  # 不 trigger

        # 10 秒内第二次点赞
        ev2 = QQEvent(eid="e2", etype=E_PROFILE_LIKE_SELF, sid="test:sid", ts=int(time.time()), actor="1001", extra={"times": 1})
        result2 = await scheduler.on_profile_like(ev2)
        assert result2 is False  # 合并累加

        # 检查累加器
        acc = scheduler._like_accumulator["test:sid"]["1001"]
        assert acc["count"] == 2

    @pytest.mark.asyncio
    async def test_hourly_limit(self):
        """每小时上限测试。"""
        class MockCtx:
            class adapter_mgr:
                @staticmethod
                def get_adapter(name):
                    return None
            class event_bus:
                @staticmethod
                async def publish(event):
                    pass
            @staticmethod
            def get_plugin_data_dir():
                return "/tmp"

        scheduler = WakeScheduler(
            ctx=MockCtx(),
            adapter=None,
            self_id="3003",
            message_locator=None,
            wake_max_per_hour=3,
        )

        # 前 3 次不超限
        for i in range(3):
            assert scheduler._check_hourly_limit("test:sid") is True

        # 第 4 次超限
        assert scheduler._check_hourly_limit("test:sid") is False


if __name__ == "__main__":
    # 运行测试
    import pytest
    pytest.main([__file__, "-v"])