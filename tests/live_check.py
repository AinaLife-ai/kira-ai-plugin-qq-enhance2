"""实机验证：真实 QQAdapter 解析 + 插件处理（全链路）。

不同于之前的 mock 测试，这里用：
- 真实 KiraAI v3.1.2 的 QQAdapter / QQIMCapability
- 真实的事件队列与 publish 链路
- 真实格式的 OneBot notice payload（从三家协议端源码抄的真实字段）
- 真实插件实例（PluginManager 加载）

只把协议端的 WebSocket 替换为 mock send_action（返回真实响应格式），
验证「适配器解析 → 事件发布 → 插件收集 → 摘要渲染」整条链路。

运行：cd /path/to/kiraai && python3 <此文件>
"""

import asyncio
import inspect
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


def build_adapter():
    """构造真实 QQAdapter（mock 底层 send_action）。"""
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.context import AdapterContext
    from core.adapter.src.qq.qq import QQAdapter

    info = AdapterInfo(
        enabled=True,
        adapter_id="napcat",
        name="napcat",
        platform="QQ",
        description="",
        config={
            "self_id": SELF_ID,
            "permission_mode": "deny_list",   # 允许所有
            "group_deny_list": [],
            "user_deny_list": [],
            "ws_uri": "ws://127.0.0.1:0",
            "ws_token": "",
            "bot_pid": SELF_ID,
        },
    )
    q = asyncio.Queue()
    ctx = AdapterContext(info=info, event_queue=q)
    adapter = QQAdapter(ctx)

    # 替换底层 send_action（模拟协议端响应，返回真实格式）
    async def fake_send_action(action, params, timeout=10.0):
        if action == "get_group_info":
            return {"status": "ok", "data": {"group_id": int(params["group_id"]),
                                             "group_name": "绿岛酒吧"}}
        if action == "get_stranger_info":
            uid = params.get("user_id")
            names = {"1001": "指尖落黑白", "1003": "Shana", "1005": "紫小贱-ai版",
                     "1007": "天然卷samurai", "2002": "李四", "8888": "管理员C7"}
            return {"status": "ok", "data": {"user_id": int(uid),
                                             "nickname": names.get(str(uid), f"用户{uid}"),
                                             "sex": 1, "sign": "签名"}}
        if action == "get_group_member_info":
            uid = str(params.get("user_id"))
            return {"status": "ok", "data": {
                "user_id": int(uid),
                "nickname": f"用户{uid}",
                "card": "",
                "title": "",
                "role": "admin" if uid == "8888" else "member",
            }}
        if action == "get_msg":
            return {"status": "ok", "data": {
                "message_id": params.get("message_id"),
                "user_id": 1001,
                "time": int(time.time()),
                "sender": {"nickname": "指尖落黑白"},
                "message": [{"type": "text", "data": {"text": "被撤回的消息内容"}}],
            }}
        return {"status": "ok", "data": {}}

    adapter.bot.send_action = fake_send_action
    adapter.bot.get_msg = lambda mid: fake_send_action("get_msg", {"message_id": mid})
    # login 事件已就绪（不需要真实 WS）
    adapter.bot.login_success_event.set()
    adapter.bot.self_id = SELF_ID
    return adapter, q


print("\n=== 1. 真实 QQAdapter 构造 ===")
adapter, queue = build_adapter()
check("QQAdapter 实例化成功", adapter is not None)
check("适配器 platform=QQ", adapter.info.platform == "QQ")
check("有 IMCapability", hasattr(adapter, "im"))


print("\n=== 2. 真实 notice 解析（QQIMCapability._on_notice_message）===")

# 真实格式：戳一戳（NapCat 有 raw_info）
poke_msg = {
    "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
    "time": int(time.time()), "self_id": int(SELF_ID), "user_id": 1001,
    "target_id": 1003, "group_id": int(GROUP_ID),
    "raw_info": [
        {"type": "qq", "uid": "u_1001"},
        {"type": "img", "src": ""},
        {"txt": "戳了戳", "type": "nor"},
        {"type": "qq", "uid": "u_1003"},
        {"txt": "", "type": "nor"},
    ],
}


async def test_poke():
    await adapter.im._on_notice_message(poke_msg)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


evs = asyncio.run(test_poke())
check("戳一戳产生事件", len(evs) == 1, str(len(evs)))
if evs:
    e = evs[0]
    check("事件 is_notice=True", e.is_notice is True)
    check("事件带 session", e.session is not None)
    check("session sid 正确", e.session.sid == f"napcat:gm:{GROUP_ID}", str(e.session.sid))
    check("raw_message 完整保留", isinstance(e.message.raw_message, dict))
    print(f"      session.sid={e.session.sid}, is_mentioned={e.is_mentioned}")


print("\n=== 3. 插件处理真实 notice（on_im_message）===")


async def test_plugin_flow():
    # 用 PluginManager 真实加载
    from core.plugin.manager import PluginManager
    from core.plugin.plugin_context import PluginContext
    from core.plugin import registry

    # 清空 registry（隔离）
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
        "get_adapters": staticmethod(lambda: {adapter.info.name: adapter}),
    })()
    context.event_bus = type("EB", (), {
        "publish": staticmethod(lambda e: asyncio.sleep(0)),
    })()
    context.provider_mgr = None
    context.plugin_mgr = PluginManager(context)

    pid = await context.plugin_mgr.load_plugin_from_dir(PLUGIN_ROOT, auto_install=False)
    if not pid:
        return None, "插件加载失败"

    inst = context.plugin_mgr.get_plugin_inst(pid)
    if inst is None:
        return None, "插件实例为空"

    await inst.initialize()
    check("插件 initialize 成功", True)
    check("检测到适配器", inst.adapter is not None)
    check(f"绑定正确的 self_id", inst.self_id == SELF_ID, inst.self_id)

    # 推送 5 个真实格式事件
    now = int(time.time())
    payloads = [
        # 戳一戳 x2（同一对）
        {"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
         "time": now - 300, "self_id": int(SELF_ID), "user_id": 1001,
         "target_id": 1003, "group_id": int(GROUP_ID)},
        {"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
         "time": now - 200, "self_id": int(SELF_ID), "user_id": 1001,
         "target_id": 1003, "group_id": int(GROUP_ID)},
        # 贴表情（他人）
        {"post_type": "notice", "notice_type": "group_msg_emoji_like",
         "time": now - 150, "self_id": int(SELF_ID), "user_id": 1005,
         "group_id": int(GROUP_ID), "message_id": 123456789,
         "likes": [{"emoji_id": "4", "count": 1}], "is_add": True},
        # 禁言（他人）
        {"post_type": "notice", "notice_type": "group_ban", "sub_type": "ban",
         "time": now - 100, "self_id": int(SELF_ID), "user_id": 1007,
         "operator_id": 8888, "duration": 600, "group_id": int(GROUP_ID)},
        # 退群
        {"post_type": "notice", "notice_type": "group_decrease", "sub_type": "leave",
         "time": now - 50, "self_id": int(SELF_ID), "user_id": 1009,
         "group_id": int(GROUP_ID)},
    ]

    handled = 0
    for raw in payloads:
        await adapter.im._on_notice_message(raw)
        # 从队列取事件，喂给插件 hook
        while not queue.empty():
            ev = queue.get_nowait()
            try:
                await inst.on_im_message(ev)
                handled += 1
            except Exception as e:
                return None, f"hook 异常: {type(e).__name__}: {e}"

    # 检查 store
    evs = await inst.store.get_events(f"napcat:gm:{GROUP_ID}")
    from qq_core.env_renderer import render_env_summary
    summary = render_env_summary(evs, max_per_type=6, max_chars=800)

    await inst.terminate()
    return {"handled": handled, "stored": len(evs), "summary": summary}, None


result, err = asyncio.run(test_plugin_flow())
if err:
    check("插件全链路", False, err)
else:
    print(f"      处理 {result['handled']} 个事件，store 有 {result['stored']} 条")
    print(f"      摘要: {result['summary']}")
    check("所有事件被处理", result["handled"] >= 4, str(result["handled"]))
    check("事件进入 store", result["stored"] >= 3, str(result["stored"]))
    check("摘要含群动态前缀", "群动态" in result["summary"])
    check("摘要含戳一戳", "戳了戳" in result["summary"])
    check("摘要含禁言", "禁言" in result["summary"])
    check("戳一戳被合并", result["summary"].count("戳了戳") == 1,
          result["summary"])


print("\n=== 4. 真实撤回链路 ===")


async def test_recall_flow():
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
        "get_adapters": staticmethod(lambda: {adapter.info.name: adapter}),
    })()
    context.event_bus = type("EB", (), {
        "publish": staticmethod(lambda e: asyncio.sleep(0)),
    })()
    context.provider_mgr = None
    context.plugin_mgr = PluginManager(context)

    pid = await context.plugin_mgr.load_plugin_from_dir(PLUGIN_ROOT, auto_install=False)
    inst = context.plugin_mgr.get_plugin_inst(pid)
    await inst.initialize()

    # 先记录一条普通消息（模拟收到）
    from core.chat.message_utils import KiraMessageEvent, KiraIMMessage
    from core.chat.message_elements import Text
    from core.chat import MessageChain
    from core.chat.session import Group, User

    normal = KiraMessageEvent(
        adapter=adapter.info,
        supported_elements=["text"],
        message=KiraIMMessage(
            timestamp=int(time.time()) - 60,
            message_id="555000111",
            self_id=SELF_ID,
            group=Group(group_id=GROUP_ID, group_name="绿岛酒吧"),
            sender=User(user_id="1001", nickname="指尖落黑白"),
            chain=MessageChain([Text("这是要被撤回的消息内容")]),
            is_notice=False,
        ),
        timestamp=int(time.time()) - 60,
    )
    await inst.on_im_message(normal)
    rec = await inst.locator.lookup("555000111", f"napcat:gm:{GROUP_ID}")
    check("普通消息被 locator 记录", rec is not None, str(rec))

    # 推送撤回事件
    recall_raw = {
        "post_type": "notice", "notice_type": "group_recall",
        "time": int(time.time()), "self_id": int(SELF_ID),
        "user_id": 1001, "operator_id": 1001,
        "message_id": 555000111, "group_id": int(GROUP_ID),
    }
    await adapter.im._on_notice_message(recall_raw)
    while not queue.empty():
        ev = queue.get_nowait()
        await inst.on_im_message(ev)

    recalls = await inst.locator.get_recalls(f"napcat:gm:{GROUP_ID}")
    check("撤回归档成功", len(recalls) == 1, str(len(recalls)))
    ann = recalls[0]["annotation"] if recalls else ""
    print(f"      撤回标注: {ann!r}")
    check("撤回标注含操作者", "1001" in ann, ann)

    # 模拟 llm_request 改写
    from core.provider import LLMRequest
    from core.prompt_manager import Prompt
    req = LLMRequest()
    req.user_prompt = [Prompt("这是要被撤回的消息内容", name="message", source="system")]
    await inst.recall_rewriter.rewrite(sid=f"napcat:gm:{GROUP_ID}", req=req)
    content = req.user_prompt[0].content
    print(f"      改写后: {content!r}")
    check("撤回标注追加到原文", "↑该条消息" in content and "撤回" in content, content)

    await inst.terminate()
    return True


try:
    asyncio.run(test_recall_flow())
except Exception as e:
    import traceback
    traceback.print_exc()
    check("真实撤回链路", False, f"{type(e).__name__}: {e}")


print("\n=== 5. 真实工具注入（llm_request）===")


async def test_tool_inject():
    from core.plugin.manager import PluginManager
    from core.plugin.plugin_context import PluginContext
    from core.plugin import registry
    from core.provider import LLMRequest

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
        "get_adapters": staticmethod(lambda: {adapter.info.name: adapter}),
    })()
    context.event_bus = type("EB", (), {
        "publish": staticmethod(lambda e: asyncio.sleep(0)),
    })()
    context.provider_mgr = None
    context.plugin_mgr = PluginManager(context)

    pid = await context.plugin_mgr.load_plugin_from_dir(PLUGIN_ROOT, auto_install=False)
    inst = context.plugin_mgr.get_plugin_inst(pid)
    await inst.initialize()

    # 构造 batch event（模拟真实批处理）
    from core.chat.message_utils import KiraMessageBatchEvent
    from core.chat.session import Session
    from core.chat.message_utils import KiraIMMessage

    batch = KiraMessageBatchEvent(
        supported_elements=["text"],
        timestamp=int(time.time()),
        adapter=adapter.info,
        session=Session(adapter_name="napcat", session_type="gm", session_id=GROUP_ID),
        messages=[KiraIMMessage(
            timestamp=int(time.time()), message_id="1", self_id=SELF_ID,
            chain=None, is_notice=False,
        )],
    )

    req = LLMRequest()
    from core.agent.tool import ToolSet
    req.tool_set = ToolSet()
    from core.tag import TagSet
    tag_set = TagSet()
    await inst.inject_tools(batch, req, tag_set)
    names = [t.name for t in req.tool_set.tools] if req.tool_set else []
    print(f"      注入工具: {names}")
    check("注入 qq_profile", "qq_profile" in names, str(names))
    check("注入 qq_group_profile", "qq_group_profile" in names, str(names))
    check("注入 fetch_emoji_like", "fetch_emoji_like" in names, str(names))

    # 检查工具绑定的 adapter 是当前会话的
    for t in (req.tool_set.tools if req.tool_set else []):
        if getattr(t, "name", "") == "qq_profile":
            check("工具绑定会话 adapter", t.adapter is adapter, str(t.adapter))
            check("工具 self_id 正确", t.self_id == SELF_ID, t.self_id)

    await inst.terminate()


try:
    asyncio.run(test_tool_inject())
except Exception as e:
    import traceback
    traceback.print_exc()
    check("工具注入", False, f"{type(e).__name__}: {e}")


print("\n" + "=" * 60)
print(f"实机验证: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 实机验证全部通过 ===")