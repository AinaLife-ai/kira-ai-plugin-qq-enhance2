"""逐字节展示：**现在**唤醒时 bot 到底收到什么。

两条来源要分开看：
  · 戳一戳（别人戳 bot）—— **框架**自己构造，不是本插件
    （core/adapter/src/qq/im.py:458  `[Poke 用户{QQ}({昵称}){动作}你{后缀}]`）
  · 其余唤醒（贴表情/点赞/头衔/被踢）—— 本插件的 wake_scheduler 构造

本脚本用真实框架类 + 真实插件实例，把每条唤醒走完整发布流程后，
再用框架同款 `_format_user_message` 格式渲染成 LLM 实际看到的那一行。

运行：
    cd /path/to/kiraai && python3 <此文件>
"""

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai_latest")
if not (KIRAAI / "core").is_dir():
    KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
sys.path.insert(0, str(KIRAAI))
sys.path.insert(0, str(ROOT))
os.chdir(KIRAAI)

SELF = "3991867505"
SELF_NICK = "紫小贱"
GID = 188395693
SID = f"napcat:gm:{GID}"


def fmt_msg(msg) -> str:
    """逐字复刻框架 core/plugin/builtin_plugins/kira-ai/main.py:_format_user_message。

    关键：**notice 走另一个分支**（没有 message_id / group_name / user_nickname）：
        [time] Notice [group_id: X, user_id: Y] | 文本
    poke / 禁言 / 入群 都是这个形态 —— 唤醒事件也必须一致。
    """
    date_str = datetime.fromtimestamp(msg.timestamp).strftime("%b %d %Y %H:%M %a")
    body = "".join(getattr(ele, "repr", "") for ele in (msg.chain or []))
    if msg.group is not None:
        if msg.is_notice:
            return (f"[{date_str}] Notice [group_id: {msg.group.group_id}, "
                    f"user_id: {msg.sender.user_id}] | {body}")
        return (f"[{date_str}] [message_id: {msg.message_id!s}] "
                f"[group_name: {msg.group.group_name} group_id: {msg.group.group_id} "
                f"user_nickname: {msg.sender.nickname}, user_id: {msg.sender.user_id}] | {body}")
    if msg.is_notice:
        return f"[{date_str}] Notice [user_id: {msg.sender.user_id}] | {body}"
    return (f"[{date_str}] [message_id: {msg.message_id!s}] "
            f"[user_nickname: {msg.sender.nickname}, user_id: {msg.sender.user_id}] | {body}")


def _ctx(adapter, published):
    from core.plugin.plugin_context import PluginContext

    class EB:
        @staticmethod
        async def publish(e):
            published.append(e)

    context = object.__new__(PluginContext)
    context.config = {}
    context.tool_mgr = None
    context.adapter_mgr = type("AM", (), {
        "get_adapter": staticmethod(lambda n: adapter),
        "get_adapters": staticmethod(lambda: {adapter.info.name: adapter}),
    })()
    context.event_bus = EB()
    context.provider_mgr = None
    tmp = tempfile.mkdtemp(prefix="qqe2_wake_")
    context.get_plugin_data_dir = lambda *a, **k: tmp
    return context


async def build_plugin(adapter, published):
    from core.plugin.manager import PluginManager
    from core.plugin import registry

    for name in ("_plugin_classes", "_plugin_components", "_plugin_load_errors",
                 "_plugin_manifests", "_plugin_module_dirs", "_plugin_module_paths",
                 "_plugin_schemas", "_plugin_infos", "_module_to_plugin"):
        if hasattr(registry, name):
            getattr(registry, name).clear()

    context = _ctx(adapter, published)
    context.plugin_mgr = PluginManager(context)
    pid = await context.plugin_mgr.load_plugin_from_dir(ROOT, auto_install=False)
    inst = context.plugin_mgr.get_plugin_inst(pid)
    await inst.initialize()

    from qq_core.event_store import EventStore
    try:
        await inst.store.stop()
    except Exception:
        pass
    inst.store = EventStore(tempfile.mkdtemp(prefix="qqe2_store_"),
                            max_events=inst.perceive_max_events,
                            window_seconds=inst.perceive_window_seconds,
                            ttl_hours=inst.env_ttl_hours)
    await inst.store.start()
    return inst


def show(title, src, ev):
    print(f"\n── {title} " + "─" * max(1, 66 - len(title)))
    print(f"   来源 : {src}")
    print(f"   sid  : {ev.session.sid}")
    print(f"   sender: {ev.message.sender.nickname} ({ev.message.sender.user_id})"
          f" | message_id={ev.message.message_id}"
          f" | is_notice={ev.message.is_notice}"
          f" | is_mentioned={ev.message.is_mentioned}")
    print("   LLM 实际看到：")
    print(f"   {fmt_msg(ev.message)}")


async def main():
    from tests.real_adapter import make_real_adapter
    from core.adapter.src.qq.im import QQIMCapability
    from qq_core.event_normalizer import QQEvent, E_TITLE_SELF, E_KICKED_SELF, E_EMOJI_LIKE

    published = []
    adapter, _q, _calls = make_real_adapter(
        self_id=SELF,
        nicknames={"1001": "张三", "2002": "李四", "8888": "管理员C7"},
    )
    adapter.bot.self_nickname = SELF_NICK
    inst = await build_plugin(adapter, published)
    sched = inst.wake_scheduler
    try:
        print("=" * 78)
        print("唤醒通知实际长相（逐字符，不截断）")
        print("=" * 78)

        # ---------- 1. 戳一戳（框架构造，不是插件） ----------
        raw_poke = {
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "time": int(time.time()), "self_id": int(SELF),
            "user_id": 769690776, "target_id": int(SELF), "group_id": GID,
            "raw_info": [{}, {}, {"txt": "戳了戳"}, {}, {"txt": ""}],
        }
        motion, obj = QQIMCapability._extract_poke_texts(raw_poke)
        # 框架原文（core/adapter/src/qq/im.py:458）
        poke_text = f"[Poke 用户769690776(周武){motion}你{obj}]"
        print("\n── 1. 别人戳 bot " + "─" * 55)
        print("   来源 : 框架 QQIMCapability._on_notice_message（不是本插件）")
        print("   is_notice=True, is_mentioned=True（框架置位 → 触发对话）")
        _now = datetime.now().strftime("%b %d %Y %H:%M %a")
        print("   LLM 实际看到：")
        print(f"   [{_now}] Notice [group_id: {GID}, user_id: 769690776] | {poke_text}")

        # ---------- 2. 自己消息被贴表情 ----------
        ev = QQEvent(eid="e1", etype=E_EMOJI_LIKE, sid=SID, ts=int(time.time()),
                     actor="1001", actor_name="张三", target=SELF,
                     target_name=SELF_NICK,
                     extra={"emoji_ids": ["4"], "count": 2,
                            "message_id": "900001"})
        await sched.trigger_emoji_like_self(ev, SELF, SELF_NICK, "画上去的还能换色号")
        show("2. 别人给你的消息贴表情", "插件 wake_scheduler.trigger_emoji_like_self",
             published[-1])

        # ---------- 3. 别人的消息被贴表情（wake_others_emoji 开启时才走） ----------
        ev2 = QQEvent(eid="e2", etype=E_EMOJI_LIKE, sid=SID, ts=int(time.time()),
                      actor="1001", actor_name="张三", target="2002",
                      target_name="李四",
                      extra={"emoji_ids": ["4"], "count": 2,
                             "message_id": "900002"})
        await sched.trigger_others_emoji_like(ev2, "2002", "李四", "哈哈哈哈哈")
        show("3. 别人的消息被贴表情", "插件 wake_scheduler.trigger_others_emoji_like", published[-1])

        # ---------- 4. 资料卡被赞（合并后） ----------
        await sched._trigger_like_summary(SID, "1001", 3, 8.0, "张三")
        show("4. 资料卡被赞 ×3", "插件 wake_scheduler._trigger_like_summary", published[-1])

        # ---------- 5. 头衔 ----------
        ev5 = QQEvent(eid="e5", etype=E_TITLE_SELF, sid=SID, ts=int(time.time()),
                      actor="8888", actor_name="管理员C7", target=SELF,
                      extra={"title": "活跃分子"})
        await sched.trigger_title_self(ev5)
        show("5. 你被授予头衔", "插件 wake_scheduler.trigger_title_self", published[-1])

        # ---------- 6. 被踢 ----------
        ev6 = QQEvent(eid="e6", etype=E_KICKED_SELF, sid=SID, ts=int(time.time()),
                      actor="8888", actor_name="管理员C7", target=SELF)
        await sched.trigger_kicked_self(ev6)
        show("6. 你被移出群聊", "插件 wake_scheduler.trigger_kicked_self", published[-1])

        print("\n" + "=" * 78)
        print("说明")
        print("  · 唤醒事件按 **notice** 构造（is_notice=True）：sender = 真正的触发者，")
        print("    走框架 Notice 分支，与戳一戳/禁言/入群完全同一种写法。")
        print("    is_mentioned=True → 直接进入对话。")
        print("  · 戳一戳由框架负责，插件的群动态不再重复记录它。")
        print("=" * 78)
    finally:
        await inst.terminate()


if __name__ == "__main__":
    asyncio.run(main())
