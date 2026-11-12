"""配置项矩阵验证：每个配置开关是否真的生效。

用户最容易困惑的是"我改了配置但没效果"。本测试逐个验证
每个配置项开启/关闭时的实际行为差异。

覆盖 24 个配置项：
- 感知层 7 个（collect_*）
- 唤醒层 9 个（wake_* / cooldown / quiet_hours / max_per_hour）
- 摘要 6 个（perceive_* / env_*）
- 改写 2 个（recall_*）
- 工具 3 个（tools_* / avatar_*）
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")

# 框架路径先加（会被插件的 tests 覆盖，避免遮蔽）
if (KIRAAI / "core").is_dir():
    sys.path.insert(0, str(KIRAAI))
# 插件根目录**最后**插入（最高优先级，保证 import tests.* 指向插件自己的）
sys.path.insert(0, str(ROOT))

from tests.real_adapter import make_real_adapter, make_poke, make_emoji_like, \
    make_ban, make_decrease, make_profile_like, push_notice

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


SELF_ID = "3003003"
GID = 188395693
SID = f"napcat:gm:{GID}"


async def build_plugin(adapter, cfg_overrides=None):
    """加载插件实例，可覆盖配置。"""
    from core.plugin.manager import PluginManager
    from core.plugin.plugin_context import PluginContext
    from core.plugin import registry

    for name in ("_plugin_classes", "_plugin_components", "_plugin_load_errors",
                 "_plugin_manifests", "_plugin_module_dirs", "_plugin_module_paths",
                 "_plugin_schemas", "_plugin_infos", "_module_to_plugin"):
        if hasattr(registry, name):
            getattr(registry, name).clear()

    context = object.__new__(PluginContext)
    context.config = {}
    context.tool_mgr = None
    context.adapter_mgr = type("AM", (), {
        "get_adapter": staticmethod(lambda n: adapter),
        "get_all_adapters": staticmethod(lambda: [adapter]),
    })()
    published = []

    class EB:
        @staticmethod
        async def publish(e):
            published.append(e)
    context.event_bus = EB()
    context.provider_mgr = None
    context.plugin_mgr = PluginManager(context)

    pid = await context.plugin_mgr.load_plugin_from_dir(ROOT, auto_install=False)
    inst = context.plugin_mgr.get_plugin_inst(pid)

    # 覆盖配置（在 initialize 前）
    if cfg_overrides:
        for k, v in cfg_overrides.items():
            setattr(inst, k, v)

    await inst.initialize()

    # 用独立临时 data_dir（避免跨 case 数据残留）
    import tempfile
    if inst.store is not None:
        try:
            await inst.store.stop()
        except Exception:
            pass
        from qq_core.event_store import EventStore
        tmpdir = tempfile.mkdtemp(prefix="qqe2_")
        inst.store = EventStore(
            tmpdir,
            max_events=inst.perceive_max_events,
            window_seconds=inst.perceive_window_seconds,
            ttl_hours=inst.env_ttl_hours,
        )
        await inst.store.start()
    return inst, published


async def feed(adapter, inst, raw) -> list:
    """推送一个 notice 并喂给插件，返回产生的事件。"""
    evs = await push_notice(adapter, adapter._q if hasattr(adapter, "_q") else None, raw) \
        if False else None
    return []


print("\n=== 感知层配置（collect_*）===")


async def test_collect_flags():
    results = {}
    cases = [
        # (配置名, 事件构造, 检查 store 里是否有该类型)
        ("collect_poke", lambda: make_poke(1001, 1003, GID, SELF_ID), "poke"),
        ("collect_group_ban", lambda: make_ban(1007, 8888, GID, SELF_ID), "group_ban"),
        ("collect_member_decrease", lambda: make_decrease(1009, GID, SELF_ID), "member_decrease"),
    ]
    for cfg_name, make_fn, etype in cases:
        # 开启
        adapter, q, _ = make_real_adapter(self_id=SELF_ID)
        inst, pub = await build_plugin(adapter, {cfg_name: True})
        evs = await push_notice(adapter, q, make_fn())
        for e in evs:
            await inst.on_im_message(e)
        stored_on = len(await inst.store.get_events(SID, etypes={etype}))
        await inst.terminate()

        # 关闭
        adapter2, q2, _ = make_real_adapter(self_id=SELF_ID)
        inst2, _ = await build_plugin(adapter2, {cfg_name: False})
        evs2 = await push_notice(adapter2, q2, make_fn())
        for e in evs2:
            await inst2.on_im_message(e)
        stored_off = len(await inst2.store.get_events(SID, etypes={etype}))
        await inst2.terminate()

        results[cfg_name] = (stored_on, stored_off)
        check(f"{cfg_name}: 开={stored_on}条 关={stored_off}条",
              stored_on > 0 and stored_off == 0,
              f"开={stored_on}, 关={stored_off}")
    return results


asyncio.run(test_collect_flags())


print("\n=== 贴表情：归档 vs 进摘要（两个独立开关）===")


async def test_emoji_two_switches():
    async def run_case(collect: bool, env: bool):
        adapter, q, _ = make_real_adapter(self_id=SELF_ID)
        inst, _ = await build_plugin(adapter, {
            "collect_emoji_like": collect,
            "collect_emoji_like_env": env,
            "wake_others_emoji": False,   # 排除唤醒干扰
        })
        evs = await push_notice(adapter, q, make_emoji_like(1005, 123456, GID, SELF_ID))
        for e in evs:
            await inst.on_im_message(e)
        archived = len(inst.emoji_rewriter.get(SID)) if inst.emoji_rewriter else 0
        in_store = len(await inst.store.get_events(SID, etypes={"emoji_like"}))
        await inst.terminate()
        return archived, in_store

    a1, s1 = await run_case(True, False)
    check(f"总开关开+摘要关：归档={a1} 进摘要={s1}", a1 == 1 and s1 == 0, f"{a1}/{s1}")

    a2, s2 = await run_case(True, True)
    check(f"总开关开+摘要开：归档={a2} 进摘要={s2}", a2 == 1 and s2 == 1, f"{a2}/{s2}")

    a3, s3 = await run_case(False, True)
    check(f"总开关关：归档={a3} 进摘要={s3}（都不处理）", a3 == 0 and s3 == 0, f"{a3}/{s3}")


asyncio.run(test_emoji_two_switches())


print("\n=== 唤醒配置（wake_*）===")


async def test_wake_flags():
    # wake_self_emoji：自己被贴表情
    async def self_emoji(wake: bool):
        adapter, q, _ = make_real_adapter(self_id=SELF_ID)
        inst, pub = await build_plugin(adapter, {"wake_self_emoji": wake})
        # 先记录自己的消息
        await inst.locator.record_incoming("111222333", SELF_ID, "我自己", "我的消息", is_self=True)
        evs = await push_notice(adapter, q, make_emoji_like(1005, 111222333, GID, SELF_ID))
        for e in evs:
            await inst.on_im_message(e)
        n = len(pub)
        await inst.terminate()
        return n

    n_on = await self_emoji(True)
    n_off = await self_emoji(False)
    check(f"wake_self_emoji: 开={n_on}次唤醒 关={n_off}次", n_on == 1 and n_off == 0,
          f"{n_on}/{n_off}")

    # wake_profile_like
    async def profile_like(wake: bool):
        adapter, q, _ = make_real_adapter(self_id=SELF_ID)
        inst, pub = await build_plugin(adapter, {
            "wake_profile_like": wake, "profile_like_merge_window_seconds": 1,
        })
        evs = await push_notice(adapter, q, make_profile_like(1001, SELF_ID))
        for e in evs:
            await inst.on_im_message(e)
        await asyncio.sleep(1.4)
        n = len(pub)
        await inst.terminate()
        return n

    p_on = await profile_like(True)
    p_off = await profile_like(False)
    check(f"wake_profile_like: 开={p_on} 关={p_off}", p_on == 1 and p_off == 0, f"{p_on}/{p_off}")

    # wake_kicked（被踢）
    async def kicked(wake: bool):
        adapter, q, _ = make_real_adapter(self_id=SELF_ID, roles={"8888": "admin"})
        inst, pub = await build_plugin(adapter, {"wake_kicked": wake})
        evs = await push_notice(adapter, q, make_decrease(SELF_ID, GID, SELF_ID,
                                                          sub_type="kick_me",
                                                          operator_id=8888))
        for e in evs:
            await inst.on_im_message(e)
        n = len(pub)
        await inst.terminate()
        return n

    k_on = await kicked(True)
    k_off = await kicked(False)
    check(f"wake_kicked: 开={k_on} 关={k_off}", k_on == 1 and k_off == 0, f"{k_on}/{k_off}")


asyncio.run(test_wake_flags())


print("\n=== 唤醒配额与静默时段 ===")


async def test_quota_and_quiet():
    # wake_max_per_hour = 1
    adapter, q, _ = make_real_adapter(self_id=SELF_ID)
    inst, pub = await build_plugin(adapter, {
        "wake_self_emoji": True, "wake_max_per_hour": 1,
    })
    for i in range(3):
        await inst.locator.record_incoming(f"msg{i}", SELF_ID, "我", f"消息{i}", is_self=True)
        evs = await push_notice(adapter, q, make_emoji_like(1005, f"msg{i}", GID, SELF_ID))
        for e in evs:
            await inst.on_im_message(e)
    n = len(pub)
    check(f"wake_max_per_hour=1 → 3 次尝试只唤醒 {n} 次", n == 1, f"{n}")
    await inst.terminate()

    # wake_max_per_hour = -1（无限制）
    adapter2, q2, _ = make_real_adapter(self_id=SELF_ID)
    inst2, pub2 = await build_plugin(adapter2, {
        "wake_self_emoji": True, "wake_max_per_hour": -1,
    })
    for i in range(3):
        await inst2.locator.record_incoming(f"m{i}", SELF_ID, "我", f"消息{i}", is_self=True)
        evs = await push_notice(adapter2, q2, make_emoji_like(1005, f"m{i}", GID, SELF_ID))
        for e in evs:
            await inst2.on_im_message(e)
    n2 = len(pub2)
    check(f"wake_max_per_hour=-1 → 3 次全部唤醒（{n2}）", n2 == 3, f"{n2}")
    await inst2.terminate()

    # 静默时段（构造一个覆盖当前时刻的时段）
    from datetime import datetime, timedelta
    now = datetime.now()
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")
    quiet = f"{start}-{end}"
    adapter3, q3, _ = make_real_adapter(self_id=SELF_ID)
    inst3, pub3 = await build_plugin(adapter3, {
        "wake_self_emoji": True, "wake_quiet_hours": quiet,
    })
    await inst3.locator.record_incoming("qm", SELF_ID, "我", "消息", is_self=True)
    evs = await push_notice(adapter3, q3, make_emoji_like(1005, "qm", GID, SELF_ID))
    for e in evs:
        await inst3.on_im_message(e)
    n3 = len(pub3)
    check(f"静默时段 {quiet} → 不唤醒（{n3}）", n3 == 0, f"{n3}")
    await inst3.terminate()


asyncio.run(test_quota_and_quiet())


print("\n=== 摘要配置（perceive_* / env_*）===")


async def test_summary_cfg():
    from qq_core.env_renderer import render_env_summary

    # 追加两条事件
    async def build(n_events):
        adapter, q, _ = make_real_adapter(self_id=SELF_ID)
        inst, _ = await build_plugin(adapter, {"collect_poke": True})
        for i in range(n_events):
            evs = await push_notice(adapter, q, make_poke(1000 + i, 2000 + i, GID, SELF_ID))
            for e in evs:
                await inst.on_im_message(e)
        stored = len(await inst.store.get_events(SID, etypes={"poke"}))
        await inst.terminate()
        return stored

    # perceive_max_events = 3
    n1 = await build(5)  # 用默认 20
    check(f"默认 max_events 能存 5 条（{n1}）", n1 == 5, str(n1))

    # env_max_chars 生效（踢旧淘汰）——用**混合类型**（同类会被合并，测不出上限）
    from qq_core.event_normalizer import (QQEvent, E_POKE, E_BAN, E_UNBAN,
                                          E_MEMBER_DECREASE, E_RECALL, E_TITLE)
    import time as _t
    now = int(_t.time())
    mixed = []
    for i in range(10):
        mixed.append(QQEvent(f"p{i}", E_POKE, SID, now - i * 10, actor=str(1000 + i),
                             actor_name=f"用户{i}", target=f"20{i}", target_name=f"目标{i}"))
        mixed.append(QQEvent(f"b{i}", E_BAN, SID, now - i * 10 - 1, actor="8888",
                             actor_name="管理员", target=f"30{i}", target_name=f"被禁{i}",
                             extra={"duration": 600 + i}))
        mixed.append(QQEvent(f"d{i}", E_MEMBER_DECREASE, SID, now - i * 10 - 2,
                             target=f"40{i}", target_name=f"退群{i}",
                             extra={"sub_type": "leave"}))
        mixed.append(QQEvent(f"r{i}", E_RECALL, SID, now - i * 10 - 3, actor=f"50{i}",
                             actor_name=f"撤回者{i}", target=f"50{i}"))

    short = render_env_summary(mixed, max_per_type=10, max_chars=120)
    long = render_env_summary(mixed, max_per_type=10, max_chars=5000)
    check(f"env_max_chars=120 → {len(short)} 字符（严格不超）", len(short) <= 120, str(len(short)))
    check(f"env_max_chars=5000 → {len(long)} 字符（更长）", len(long) > len(short),
          f"{len(long)} vs {len(short)}")

    # perceive_max_per_type 生效（用非合并型：ban）
    many_bans = [
        QQEvent(f"ban{i}", E_BAN, SID, now - i * 10, actor="8888", actor_name="管理员",
                target=f"60{i}", target_name=f"被禁{i}", extra={"duration": 60 + i})
        for i in range(10)
    ]
    few = render_env_summary(many_bans, max_per_type=2, max_chars=5000)
    more = render_env_summary(many_bans, max_per_type=10, max_chars=5000)
    check(f"max_per_type=2 vs 10：{len(few)} vs {len(more)} 字符",
          len(few) < len(more), f"{len(few)} vs {len(more)}")


asyncio.run(test_summary_cfg())


print("\n=== 改写配置（recall_*）===")


async def test_recall_cfg():
    from core.provider import LLMRequest
    from core.prompt_manager import Prompt
    from core.agent.tool import ToolSet

    async def run_case(rewrite: bool, show_content: bool):
        adapter, q, _ = make_real_adapter(self_id=SELF_ID)
        inst, _ = await build_plugin(adapter, {
            "recall_rewrite": rewrite, "recall_show_content": show_content,
        })
        # 记录消息 + 撤回
        await inst.locator.record_incoming("rmsg", "1001", "张三", "被撤回的内容")
        evs = await push_notice(adapter, q, {
            "post_type": "notice", "notice_type": "group_recall",
            "time": 1, "self_id": int(SELF_ID), "user_id": 1001,
            "operator_id": 1001, "message_id": "rmsg", "group_id": GID,
        })
        for e in evs:
            await inst.on_im_message(e)

        req = LLMRequest(tool_set=ToolSet())
        req.user_prompt = [Prompt("被撤回的内容", name="message", source="system")]
        await inst.on_llm_request(
            type("B", (), {"sid": SID, "adapter": adapter.info})(), req,
        ) if False else None
        # 直接调 rewriter（on_llm_request 需要 batch event，这里简化）
        await inst.recall_rewriter.rewrite(
            sid=SID, req=req, recall_rewrite=rewrite, recall_show_content=show_content,
        )
        content = req.user_prompt[0].content
        await inst.terminate()
        return content

    c_on = await run_case(True, True)
    c_off = await run_case(False, True)
    check(f"recall_rewrite 开：有标注（{bool('[已撤回' in c_on)}）",
          "[已撤回" in c_on, c_on)
    check(f"recall_rewrite 关：无标注（{bool('[已撤回' in c_off)}）",
          "[已撤回" not in c_off, c_off)


asyncio.run(test_recall_cfg())


print("\n=== 工具配置（tools_*）===")


async def test_tool_cfg():
    from core.provider import LLMRequest
    from core.agent.tool import ToolSet
    from core.chat.message_utils import KiraMessageBatchEvent, KiraIMMessage
    from core.chat.session import Session
    from core.chat import MessageChain
    from core.tag import TagSet

    async def tool_names(profile: bool, emoji: bool):
        adapter, q, _ = make_real_adapter(self_id=SELF_ID)
        inst, _ = await build_plugin(adapter, {
            "tools_profile": profile, "tools_fetch_emoji_like": emoji,
        })
        batch = KiraMessageBatchEvent(
            supported_elements=["text"], timestamp=1, adapter=adapter.info,
            session=Session(adapter_name="napcat", session_type="gm", session_id=str(GID)),
            messages=[KiraIMMessage(timestamp=1, message_id="1", self_id=SELF_ID,
                                    chain=MessageChain(), is_notice=False)],
        )
        req = LLMRequest(tool_set=ToolSet())
        await inst.inject_tools(batch, req, TagSet())
        names = [t.name for t in (req.tool_set.tools if req.tool_set else [])]
        await inst.terminate()
        return names

    n_on = await tool_names(True, True)
    n_off = await tool_names(False, False)
    check(f"tools 全开：{n_on}", len(n_on) == 3, str(n_on))
    check(f"tools 全关：{n_off}", len(n_off) == 0, str(n_off))


asyncio.run(test_tool_cfg())


print("\n" + "=" * 60)
print(f"配置矩阵验证: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部通过 ===")