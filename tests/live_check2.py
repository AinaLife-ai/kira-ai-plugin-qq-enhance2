"""实机验证 Part 2：唤醒 trigger + prompt 组装（真实框架链路）。

验证：
1. 唤醒事件 trigger 后的 process_strategy 是否正确
2. 事件是否真的进入 event_queue（会被框架的 receive stage 处理）
3. 环境摘要注入后 prompt 的真实结构（persist=False 不进历史）
4. prompt 缓存不变量（连续两轮 system 前缀一致）
"""

import asyncio
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
KIRAAI_ROOT = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
sys.path.insert(0, str(KIRAAI_ROOT))
sys.path.insert(0, str(PLUGIN_ROOT))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


SELF_ID = "3003003"
GROUP_ID = "188395693"


def clean_plugin_data():
    """清理测试残留数据（避免多轮测试累积干扰）。"""
    import shutil
    from pathlib import Path as P
    d = KIRAAI_ROOT / "data" / "plugin_data" / "kira-ai-plugin-qq-enhance2"
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)


def build_adapter():
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.context import AdapterContext
    from core.adapter.src.qq.qq import QQAdapter

    info = AdapterInfo(
        enabled=True, adapter_id="napcat", name="napcat", platform="QQ",
        config={"self_id": SELF_ID, "permission_mode": "deny_list",
                "group_deny_list": [], "user_deny_list": [],
                "ws_uri": "ws://127.0.0.1:0", "ws_token": "", "bot_pid": SELF_ID},
    )
    q = asyncio.Queue()
    adapter = QQAdapter(AdapterContext(info=info, event_queue=q))

    async def fake_send_action(action, params, timeout=10.0):
        if action == "get_group_info":
            return {"status": "ok", "data": {"group_name": "绿岛酒吧"}}
        if action == "get_stranger_info":
            return {"status": "ok", "data": {"nickname": f"用户{params.get('user_id')}",
                                             "sex": 1, "sign": ""}}
        return {"status": "ok", "data": {}}

    adapter.bot.send_action = fake_send_action
    adapter.bot.get_msg = lambda mid: fake_send_action("get_msg", {})
    adapter.bot.login_success_event.set()
    adapter.bot.self_id = SELF_ID
    return adapter, q


async def build_plugin(adapter):
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

    pid = await context.plugin_mgr.load_plugin_from_dir(PLUGIN_ROOT, auto_install=False)
    inst = context.plugin_mgr.get_plugin_inst(pid)
    await inst.initialize()
    return inst, published


print("\n=== 1. 真实唤醒 trigger ===")


async def test_wake():
    clean_plugin_data()
    adapter, q = build_adapter()
    inst, published = await build_plugin(adapter)

    # 构造"自己消息被贴表情"事件（真实格式）
    now = int(time.time())
    # 先让 locator 记录一条自己的消息
    await inst.locator.record_incoming(
        "777888999", SELF_ID, "我自己", "我发的消息内容", is_self=True,
    )

    raw = {
        "post_type": "notice", "notice_type": "group_msg_emoji_like",
        "time": now, "self_id": int(SELF_ID), "user_id": 1001,
        "group_id": int(GROUP_ID), "message_id": 777888999,
        "likes": [{"emoji_id": "4", "count": 1}], "is_add": True,
    }
    await adapter.im._on_notice_message(raw)
    while not q.empty():
        ev = q.get_nowait()
        await inst.on_im_message(ev)

    print(f"      发布唤醒事件: {len(published)} 条")
    for e in published:
        txt = ""
        try:
            txt = " ".join(str(el) for el in e.message.chain.message_list)
        except Exception:
            pass
        print(f"      - strategy={e.process_strategy}, chain_len={len(e.message.chain)}, text={txt[:60]}")

    check("自己被贴表情触发了唤醒", len(published) == 1, str(len(published)))
    if published:
        e = published[0]
        check("唤醒事件 process_strategy=trigger",
              e.process_strategy == "trigger", e.process_strategy)
        check("唤醒事件 chain 非空", len(e.message.chain) > 0, str(len(e.message.chain)))
        check("唤醒事件 is_mentioned=True", e.is_mentioned is True, str(e.is_mentioned))
        check("唤醒事件 supported_elements 非空",
              bool(getattr(e, "supported_elements", None)),
              str(getattr(e, "supported_elements", None)))
        check("唤醒事件 sid 正确", e.session.sid == f"napcat:gm:{GROUP_ID}", e.session.sid)

    await inst.terminate()


asyncio.run(test_wake())


print("\n=== 2. 真实 llm_request 环境摘要注入 ===")


async def test_env_inject():
    clean_plugin_data()
    adapter, q = build_adapter()
    inst, published = await build_plugin(adapter)

    # 先塞几个事件到 store（模拟已收集）
    from qq_core.event_normalizer import normalize_notice
    now = int(time.time())
    for raw in [
        {"notice_type": "notify", "sub_type": "poke", "user_id": 1001, "target_id": 1003,
         "group_id": int(GROUP_ID), "time": now - 100},
        {"notice_type": "group_ban", "sub_type": "ban", "user_id": 1007, "operator_id": 8888,
         "duration": 600, "group_id": int(GROUP_ID), "time": now - 50},
    ]:
        ev = normalize_notice(raw, sid=f"napcat:gm:{GROUP_ID}", self_id=SELF_ID)
        if ev:
            ev.actor_name = f"用户{ev.actor}"
            ev.target_name = f"用户{ev.target}"
            await inst.store.add(ev)

    # 构造真实 LLMRequest
    from core.provider import LLMRequest
    from core.agent.tool import ToolSet
    from core.prompt_manager import Prompt
    from core.chat.message_utils import KiraMessageBatchEvent, KiraIMMessage
    from core.chat.session import Session
    from core.chat import MessageChain

    batch = KiraMessageBatchEvent(
        supported_elements=["text"],
        timestamp=int(time.time()),
        adapter=adapter.info,
        session=Session(adapter_name="napcat", session_type="gm", session_id=GROUP_ID),
        messages=[KiraIMMessage(
            timestamp=int(time.time()), message_id="1", self_id=SELF_ID,
            chain=MessageChain([]), is_notice=False,
        )],
    )

    req = LLMRequest(tool_set=ToolSet())
    req.user_prompt.append(Prompt("真实用户消息", name="message", source="system"))

    await inst.on_llm_request(batch, req)

    # 检查注入
    env_prompts = [p for p in req.user_prompt if getattr(p, "name", "") == "qq_social_env"]
    print(f"      注入的环境块: {len(env_prompts)} 个")
    if env_prompts:
        p = env_prompts[0]
        print(f"      内容: {p.content}")
        print(f"      persist={p.persist}, 位置={req.user_prompt.index(p)}")
        check("环境块 persist=False", p.persist is False, str(p.persist))
        check("环境块在 user_prompt 最前", req.user_prompt.index(p) == 0,
              str(req.user_prompt.index(p)))
        check("环境块含群动态", "群动态" in p.content)
    else:
        check("环境块已注入", False, "未找到")

    # 验证 assemble 后进历史的内容不含环境块
    req.assemble_prompt()
    persisted = "".join(m.content or "" for m in req.messages if m.role == "user")
    print(f"      assemble 后 user 消息（含临时块）: {persisted[:120]}")
    persist_only = "".join(
        p.to_string() for p in req.user_prompt if getattr(p, "persist", True)
    )
    check("持久化内容不含环境块", "群动态" not in persist_only, persist_only[:100])
    check("持久化内容含真实消息", "真实用户消息" in persist_only)

    await inst.terminate()


asyncio.run(test_env_inject())


print("\n=== 3. prompt 缓存不变量（连续两轮）===")


async def test_cache_invariant():
    from core.provider import LLMRequest
    from core.agent.tool import ToolSet
    from core.prompt_manager import Prompt

    # 模拟两轮：system prompt 相同，环境块内容不同
    def build_round(env_text: str):
        req = LLMRequest(tool_set=ToolSet())
        req.system_prompt = [Prompt("你是一个QQ群聊机器人。", name="base", source="system")]
        req.user_prompt = [
            Prompt(env_text, name="qq_social_env", persist=False),
            Prompt("用户消息", name="message", source="system"),
        ]
        req.assemble_prompt(dynamic_position="latest_user")
        system_part = next((m.content for m in req.messages if m.role == "system"), "")
        return system_part

    s1 = build_round("[群动态·刚刚] A 戳了戳 B")
    s2 = build_round("[群动态·刚刚] A 戳了戳 B；C 禁言 D 600秒")
    print(f"      第1轮 system: {s1!r}")
    print(f"      第2轮 system: {s2!r}")
    check("两轮 system prompt 完全一致（缓存前缀不变）", s1 == s2, f"{s1!r} vs {s2!r}")


asyncio.run(test_cache_invariant())


print("\n" + "=" * 60)
print(f"实机验证 Part2: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部通过 ===")