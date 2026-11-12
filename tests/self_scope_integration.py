"""群动态边界集成验证（独立脚本，需真实框架环境）。

为什么单独一个脚本而不放 pytest：
    pytest 的 conftest 在"没有真实框架"时会装 stub 模块，
    stub 里没有 core.plugin.manager → 无法真实加载插件。
    仓库既有约定：纯单元进 pytest，需要真实框架的走独立脚本
    （config_matrix.py / audit_deep.py 等同款）。

运行：
    cd <插件根目录> && python3 tests/self_scope_integration.py

验证目标（用户实机反馈）：
    别人戳 bot 出现在群动态里 —— 不应该。群动态只记"其他用户之间"的事件。
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai_latest")
if not (KIRAAI / "core").is_dir():
    KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
sys.path.insert(0, str(KIRAAI))
sys.path.insert(0, str(ROOT))

# 框架的日志 handler 使用相对路径 data/log.log（相对 cwd）→ 切到框架根目录
os.chdir(KIRAAI)

from tests.real_adapter import (  # noqa: E402
    make_real_adapter, make_poke, make_ban, make_decrease, make_recall,
    push_notice,
)

SELF = "3991867505"
GID = 188395693
SID = f"napcat:gm:{GID}"


class _FakeBuffer:
    """最小 SessionBuffer 替身（只有 .buffer 列表）。"""

    def __init__(self):
        self.buffer = []


_FAKE_BUFFER = _FakeBuffer()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


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
    tmp = tempfile.mkdtemp(prefix="qqe2_scope_")
    context.get_plugin_data_dir = lambda *a, **k: tmp
    # 假会话缓冲区：用于验证「被贴消息是否还在 buffer」分支
    context.get_buffer = lambda sid: _FAKE_BUFFER
    return context


async def build_plugin(adapter, published=None, overrides=None):
    from core.plugin.manager import PluginManager
    from core.plugin import registry

    for name in ("_plugin_classes", "_plugin_components", "_plugin_load_errors",
                 "_plugin_manifests", "_plugin_module_dirs", "_plugin_module_paths",
                 "_plugin_schemas", "_plugin_infos", "_module_to_plugin"):
        if hasattr(registry, name):
            getattr(registry, name).clear()

    published = published if published is not None else []
    context = _ctx(adapter, published)
    context.plugin_mgr = PluginManager(context)
    pid = await context.plugin_mgr.load_plugin_from_dir(ROOT, auto_install=False)
    inst = context.plugin_mgr.get_plugin_inst(pid)
    for k, v in (overrides or {}).items():
        setattr(inst, k, v)
    await inst.initialize()

    # 独立临时 store（避免历史落盘污染）
    from qq_core.event_store import EventStore
    try:
        await inst.store.stop()
    except Exception:
        pass
    tmp = tempfile.mkdtemp(prefix="qqe2_store_")
    inst.store = EventStore(
        tmp, max_events=inst.perceive_max_events,
        window_seconds=inst.perceive_window_seconds,
        ttl_hours=inst.env_ttl_hours,
    )
    await inst.store.start()
    return inst, published, context


async def feed(adapter, inst, raw):
    for ev in await push_notice(adapter, adapter._qq_queue if hasattr(adapter, "_qq_queue") else _QUEUE[0], raw):
        await inst.on_im_message(ev)


_QUEUE = [None]


def fresh_adapter():
    adapter, q, calls = make_real_adapter(self_id=SELF)
    _QUEUE[0] = q
    return adapter, calls


async def stored_types(inst):
    evs = await inst.store.get_events(SID)
    return [e.etype for e in evs]


async def main():
    print("\n=== 1. 别人戳 bot → 不进群动态 ===")
    adapter, calls = fresh_adapter()
    inst, _, _ = await build_plugin(adapter)
    try:
        await feed(adapter, inst, make_poke(769690776, int(SELF), GID, SELF))
        check("store 为空", await stored_types(inst) == [], await stored_types(inst))
        check("没有调用 get_stranger_info（不需要查 bot 自己）",
              all(a != "get_stranger_info" or str(p.get("user_id")) != SELF
                  for a, p in calls), calls)
    finally:
        await inst.terminate()

    print("\n=== 2. 其他用户戳其他用户 → 进群动态 ===")
    adapter, _ = fresh_adapter()
    inst, _, _ = await build_plugin(adapter)
    try:
        await feed(adapter, inst, make_poke(1001, 1002, GID, SELF))
        check("store 有 1 条 poke", await stored_types(inst) == ["poke"])
    finally:
        await inst.terminate()

    print("\n=== 3. bot 禁言别人 → 不进群动态 ===")
    adapter, _ = fresh_adapter()
    inst, _, _ = await build_plugin(adapter)
    try:
        await feed(adapter, inst, make_ban(1001, int(SELF), GID, SELF))
        check("store 为空", await stored_types(inst) == [], await stored_types(inst))
    finally:
        await inst.terminate()

    print("\n=== 4. 别人禁言别人 → 进群动态 ===")
    adapter, _ = fresh_adapter()
    inst, _, _ = await build_plugin(adapter)
    try:
        await feed(adapter, inst, make_ban(1001, 8888, GID, SELF))
        check("store 有 group_ban", await stored_types(inst) == ["group_ban"])
    finally:
        await inst.terminate()

    print("\n=== 5. bot 自己的群名片/管理员变动 → 不进群动态 ===")
    adapter, _ = fresh_adapter()
    inst, _, _ = await build_plugin(adapter)
    try:
        await feed(adapter, inst, {
            "post_type": "notice", "notice_type": "group_card",
            "time": int(time.time()), "self_id": int(SELF),
            "user_id": int(SELF), "card_new": "新", "card_old": "旧",
            "group_id": GID,
        })
        await feed(adapter, inst, {
            "post_type": "notice", "notice_type": "group_admin", "sub_type": "set",
            "time": int(time.time()), "self_id": int(SELF),
            "user_id": int(SELF), "group_id": GID,
        })
        check("store 为空", await stored_types(inst) == [], await stored_types(inst))
    finally:
        await inst.terminate()

    print("\n=== 6. bot 自己退群 → 不进群动态 ===")
    adapter, _ = fresh_adapter()
    inst, _, _ = await build_plugin(adapter)
    try:
        await feed(adapter, inst, make_decrease(int(SELF), GID, SELF, sub_type="leave"))
        check("store 为空", await stored_types(inst) == [], await stored_types(inst))
    finally:
        await inst.terminate()

    print("\n=== 7. bot 被踢 → 唤醒（不是群动态）===")
    adapter, _ = fresh_adapter()
    inst, published, _ = await build_plugin(adapter)
    try:
        await feed(adapter, inst, make_decrease(int(SELF), GID, SELF,
                                                sub_type="kick_me", operator_id=8888))
        check("store 为空", await stored_types(inst) == [], await stored_types(inst))
        texts = [m.text for e in published for m in e.message.chain
                 if getattr(m, "text", None)]
        check("触发被踢唤醒", any("移出群聊" in t for t in texts), texts)
    finally:
        await inst.terminate()

    print("\n=== 8. bot 被授予头衔 → 唤醒（此前是死开关）===")
    adapter, _ = fresh_adapter()
    inst, published, _ = await build_plugin(adapter)
    try:
        await feed(adapter, inst, {
            "post_type": "notice", "notice_type": "notify", "sub_type": "title",
            "time": int(time.time()), "self_id": int(SELF),
            "user_id": int(SELF), "title": "活跃分子",
            "group_id": GID, "operator_id": 8888,
        })
        check("store 为空", await stored_types(inst) == [], await stored_types(inst))
        texts = [m.text for e in published for m in e.message.chain
                 if getattr(m, "text", None)]
        check("触发头衔唤醒", any("授予头衔" in t for t in texts), texts)
    finally:
        await inst.terminate()

    print("\n=== 9. bot 撤回消息 → 不进群动态 ===")
    adapter, _ = fresh_adapter()
    inst, _, _ = await build_plugin(adapter)
    try:
        await feed(adapter, inst, make_recall(1001, int(SELF), 123456, GID, SELF))
        check("store 为空", await stored_types(inst) == [], await stored_types(inst))
    finally:
        await inst.terminate()

    print("\n=== 10. 历史残留：store 里已有涉及 bot 的事件 → 渲染时过滤 ===")
    from core.provider import LLMRequest
    from core.agent.tool import ToolSet
    from core.chat.message_utils import KiraMessageBatchEvent
    from core.chat import KiraIMMessage, MessageChain
    from core.chat.session import Session
    from qq_core.event_normalizer import QQEvent, E_POKE

    adapter, _ = fresh_adapter()
    inst, _, _ = await build_plugin(adapter)
    try:
        now = int(time.time())
        await inst.store.add(QQEvent(eid="1", etype=E_POKE, sid=SID, ts=now,
                                     actor="769690776", actor_name="周武",
                                     target=SELF, target_name="紫小贱"))
        await inst.store.add(QQEvent(eid="2", etype=E_POKE, sid=SID, ts=now,
                                     actor="1001", actor_name="张三",
                                     target="1002", target_name="李四"))
        req = LLMRequest(tool_set=ToolSet())
        batch = KiraMessageBatchEvent(
            supported_elements=["text"], timestamp=now, adapter=adapter.info,
            session=Session(adapter_name="napcat", session_type="gm", session_id=str(GID)),
            messages=[KiraIMMessage(timestamp=now, message_id="1", self_id=SELF,
                                    chain=MessageChain(), is_notice=False)],
        )
        await inst.on_llm_request(batch, req)
        env = [p.content for p in req.user_prompt if getattr(p, "name", "") == "qq_social_env"]
        check("有群动态", bool(env))
        text = env[0] if env else ""
        print(f"       群动态: {text}")
        check("不含 bot 昵称紫小贱", "紫小贱" not in text, text)
        check("含纯他人事件", "张三" in text and "李四" in text, text)
    finally:
        await inst.terminate()

    print("\n=== 11. 贴表情但消息作者查不到 → 不进群动态（无法确认是不是 bot 自己的）===")
    from tests.real_adapter import make_emoji_like
    adapter, _ = fresh_adapter()
    # get_msg 返回失败 → locator 查不到作者
    adapter.bot.get_msg = lambda mid: asyncio.sleep(0, result={"status": "failed", "data": None})
    inst, pub, _ = await build_plugin(adapter, overrides={"collect_emoji_like_env": True})
    try:
        await feed(adapter, inst, make_emoji_like(1001, 555666, GID, SELF))
        check("store 为空（作者未知）", await stored_types(inst) == [], await stored_types(inst))
        check("没有误唤醒", pub == [], str(len(pub)))
    finally:
        await inst.terminate()

    print("\n=== 12. 贴表情且作者是他人 → 允许进群动态（开启配置时）===")
    adapter, _ = fresh_adapter()
    inst, _pub, _ = await build_plugin(adapter, overrides={"collect_emoji_like_env": True})
    try:
        await feed(adapter, inst, make_emoji_like(1001, 555667, GID, SELF))
        check("store 有 emoji_like", await stored_types(inst) == ["emoji_like"],
              await stored_types(inst))
    finally:
        await inst.terminate()

    print("\n=== 13. resolve_nickname 开关 ===")
    from qq_core.env_renderer import render_env_summary

    adapter, _ = fresh_adapter()
    inst, _pub, _ = await build_plugin(adapter, overrides={"resolve_nickname": False})
    try:
        await feed(adapter, inst, make_poke(1001, 1002, GID, SELF))
        evs = await inst.store.get_events(SID)
        check("关闭后不补昵称", bool(evs) and not evs[0].target_name,
              evs[0].target_name if evs else "no event")
        text = render_env_summary(evs) if evs else ""
        check("渲染只有 QQ 号（无昵称括号）", "(李四)" not in text and "用户1002" in text, text)
    finally:
        await inst.terminate()

    adapter, _ = fresh_adapter()
    inst, _pub, _ = await build_plugin(adapter, overrides={"resolve_nickname": True})
    try:
        await feed(adapter, inst, make_poke(1001, 1002, GID, SELF))
        evs = await inst.store.get_events(SID)
        check("开启后补全昵称", bool(evs) and bool(evs[0].target_name),
              evs[0].target_name if evs else "no event")
    finally:
        await inst.terminate()

    print("\n=== 14. 他人贴表情：消息在缓冲区 → 省略事实复述 ===")
    import types
    from tests.real_adapter import make_emoji_like

    class _Ev:
        def __init__(self, mid):
            self.message = types.SimpleNamespace(message_id=mid)

    _FAKE_BUFFER.buffer = [_Ev("900777")]
    adapter, _ = fresh_adapter()
    inst, pub, _ = await build_plugin(
        adapter, overrides={"wake_others_emoji": True,
                            "wake_emoji_merge_seconds": 0})
    try:
        await feed(adapter, inst, make_emoji_like(1001, 900777, GID, SELF))
        text = "".join(getattr(x, "text", "") for e in pub for x in e.message.chain) if pub else ""
        print(f"       唤醒正文: {text!r}")
        check("有唤醒", bool(pub))
        check("不复述事实（无 [System）", "[System" not in text, text)
        check("带互动提示", "[互动提示]" in text, text)
    finally:
        await inst.terminate()

    print("\n=== 15. 他人贴表情：消息不在缓冲区 → 保留事实 + 提示 ===")
    _FAKE_BUFFER.buffer = []
    adapter, _ = fresh_adapter()
    inst, pub, _ = await build_plugin(
        adapter, overrides={"wake_others_emoji": True,
                            "wake_emoji_merge_seconds": 0})
    try:
        await feed(adapter, inst, make_emoji_like(1001, 900778, GID, SELF))
        text = "".join(getattr(x, "text", "") for e in pub for x in e.message.chain) if pub else ""
        print(f"       唤醒正文: {text!r}")
        check("有事实", "[System" in text and "贴了" in text, text)
        check("有提示", "[互动提示]" in text, text)
    finally:
        await inst.terminate()

    print("\n=== 16. 提示为空 + 在缓冲区 → 回退事实（否则空通知会被丢弃）===")
    _FAKE_BUFFER.buffer = [_Ev("900779")]
    adapter, _ = fresh_adapter()
    inst, pub, _ = await build_plugin(
        adapter, overrides={"wake_others_emoji": True, "wake_hint": "",
                            "wake_emoji_merge_seconds": 0})
    try:
        await feed(adapter, inst, make_emoji_like(1001, 900779, GID, SELF))
        text = "".join(getattr(x, "text", "") for e in pub for x in e.message.chain) if pub else ""
        print(f"       唤醒正文: {text!r}")
        check("回退为事实", "[System" in text, text)
        check("无提示", "[互动提示]" not in text, text)
    finally:
        await inst.terminate()
    _FAKE_BUFFER.buffer = []

    print("\n=== 17. 资料卡点赞：默认落最近互动会话，开关可强制私聊 ===")
    import warnings
    from tests.real_adapter import make_profile_like, push_notice as _push

    async def _like_sid(overrides):
        a, q, _ = make_real_adapter(self_id=SELF)
        inst, pub, _ = await build_plugin(
            a, overrides={"profile_like_merge_window_seconds": 1, **overrides})
        try:
            inst._remember_session("napcat", "769690776", SID)  # 最近在群里活跃
            for e in await _push(a, q, make_profile_like(769690776, SELF)):
                await inst.on_im_message(e)
            await asyncio.sleep(1.4)
            return [e.session.sid for e in pub]
        finally:
            await inst.terminate()

    sids_default = await _like_sid({"profile_like_dm_only": False})
    print(f"       默认: {sids_default}")
    check("默认落到最近互动会话（群）", any(x == SID for x in sids_default), sids_default)
    check("默认不落 None", all("None" not in x for x in sids_default), sids_default)

    sids_dm = await _like_sid({"profile_like_dm_only": True})
    print(f"       强制私聊: {sids_dm}")
    check("开关开启 → 私聊", any(x == "napcat:dm:769690776" for x in sids_dm), sids_dm)
    check("强制私聊也不落 None", all("None" not in x for x in sids_dm), sids_dm)

    # 没有任何历史互动 + 默认 → 回退私聊
    a2, q2, _ = make_real_adapter(self_id=SELF)
    inst2, pub2, _ = await build_plugin(
        a2, overrides={"profile_like_merge_window_seconds": 1,
                       "profile_like_dm_only": False})
    try:
        for e in await _push(a2, q2, make_profile_like(769690776, SELF)):
            await inst2.on_im_message(e)
        await asyncio.sleep(1.4)
        sids2 = [e.session.sid for e in pub2]
        print(f"       无历史: {sids2}")
        check("无历史 → 回退私聊", any(x == "napcat:dm:769690776" for x in sids2), sids2)
    finally:
        await inst2.terminate()

    print("\n=== 18. 唤醒事件元素类型：不再触发 message_types 弃用告警 ===")
    adapter, _ = fresh_adapter()
    inst, _pub, _ = await build_plugin(adapter)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            els = inst.wake_scheduler._get_supported_elements(adapter, inst.ctx)
        dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        print(f"       元素: {els}")
        check("无 DeprecationWarning", not dep, [str(x.message) for x in dep])
        check("拿到完整元素列表（含 img/at）",
              "img" in els and "at" in els, els)
    finally:
        await inst.terminate()

    print("\n=== 19. 自己被贴表情：独立冷却 wake_self_emoji_cooldown（默认 10）===")
    from tests.real_adapter import make_emoji_like

    async def _self_emoji_wakes(cooldown):
        a, _calls = fresh_adapter()
        inst, pub, _ = await build_plugin(
            a, overrides={"wake_self_emoji": True,
                          "wake_self_emoji_cooldown": cooldown,
                          "wake_emoji_merge_seconds": 0})
        try:
            await inst.locator.record_incoming(
                "900901", SELF, "紫小贱", "我的消息", is_self=True)
            for _ in range(2):
                for e in await push_notice(a, _QUEUE[0],
                                           make_emoji_like(1001, 900901, GID, SELF)):
                    await inst.on_im_message(e)
            return len(pub)
        finally:
            await inst.terminate()

    n0 = await _self_emoji_wakes(0)
    n60 = await _self_emoji_wakes(60)
    n10 = await _self_emoji_wakes(10)
    print(f"       cooldown=0 → {n0} 次；=60 → {n60} 次；=10 → {n10} 次")
    check("0 = 每条都唤醒", n0 == 2, n0)
    check("60 秒内第二次被冷却", n60 == 1, n60)
    check("默认 10 秒也冷却", n10 == 1, n10)

    print("\n=== 20. 自己被贴表情：同一条消息短时间合并 ===")

    async def _merge_texts(merge_seconds, same_message):
        a, _calls = fresh_adapter()
        inst, pub, _ = await build_plugin(
            a, overrides={"wake_self_emoji": True,
                          "wake_self_emoji_cooldown": 0,
                          "wake_emoji_merge_seconds": merge_seconds})
        try:
            await inst.locator.record_incoming("900902", SELF, "紫小贱", "我的消息", is_self=True)
            await inst.locator.record_incoming("900903", SELF, "紫小贱", "另一条消息", is_self=True)
            targets = ("900902", "900902") if same_message else ("900902", "900903")
            for i, mid in enumerate(targets):
                eid = "4" if i == 0 else "66"
                for e in await push_notice(a, _QUEUE[0],
                                           make_emoji_like(1001, int(mid), GID, SELF, emoji_id=eid)):
                    await inst.on_im_message(e)
            await asyncio.sleep(max(merge_seconds, 0) + 0.6)
            return ["".join(getattr(x, "text", "") for x in e.message.chain) for e in pub]
        finally:
            await inst.terminate()

    merged = await _merge_texts(1, True)
    print(f"       同一条消息 1s 窗口: {merged}")
    check("同一条消息两次贴表情 → 1 条唤醒", len(merged) == 1, merged)
    check("合并后包含两个表情 ID",
          bool(merged) and "ID: 4" in merged[0] and "ID: 66" in merged[0], merged)

    split = await _merge_texts(1, False)
    print(f"       不同消息 1s 窗口: {len(split)} 条")
    check("不同消息不合并 → 2 条唤醒", len(split) == 2, len(split))

    no_merge = await _merge_texts(0, True)
    print(f"       合并=0: {len(no_merge)} 条")
    check("0 = 不合并", len(no_merge) == 2, len(no_merge))

    print("\n=== 21. 他人消息被贴表情：共用同一个合并窗口 ===")

    async def _others_merge(merge_seconds, same_message):
        a, _calls = fresh_adapter()
        inst, pub, _ = await build_plugin(
            a, overrides={"wake_others_emoji": True,
                          "wake_others_emoji_cooldown": 0,
                          "wake_emoji_merge_seconds": merge_seconds})
        try:
            await inst.locator.record_incoming("900911", "3967118487", "C7", "别人的消息")
            await inst.locator.record_incoming("900912", "3967118487", "C7", "另一条消息")
            targets = ("900911", "900911") if same_message else ("900911", "900912")
            for i, mid in enumerate(targets):
                eid = "4" if i == 0 else "66"
                for e in await push_notice(a, _QUEUE[0],
                                           make_emoji_like(1001 + i, int(mid), GID, SELF, emoji_id=eid)):
                    await inst.on_im_message(e)
            await asyncio.sleep(max(merge_seconds, 0) + 0.6)
            return ["".join(getattr(x, "text", "") for x in e.message.chain) for e in pub]
        finally:
            await inst.terminate()

    om = await _others_merge(1, True)
    print(f"       他人同消息 1s 窗口: {om}")
    check("他人同消息两次贴 → 1 条唤醒", len(om) == 1, om)
    check("合并后包含两位贴表情的人",
          bool(om) and "用户1001" in om[0] and "用户1002" in om[0], om)
    check("合并后包含两个表情 ID",
          bool(om) and "ID: 4" in om[0] and "ID: 66" in om[0], om)

    om2 = await _others_merge(1, False)
    print(f"       他人不同消息 1s 窗口: {len(om2)} 条")
    check("他人不同消息不合并 → 2 条", len(om2) == 2, len(om2))

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for n, d in FAIL:
            print(f"  ✗ {n}: {d}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
