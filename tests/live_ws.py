"""实机验证 Part 3：真实 WebSocket 连接全链路。

之前 live_check 是直接调 `_on_notice_message`，跳过了 WS 层。
本测试起一个**真实 WebSocket 服务器**（模拟 NapCat），
让真实 QQAdapter 通过 `bot.run()` 连上来，然后**通过 WS 推送事件**，
验证：WS 收包 → JSON 解析 → 回调分发 → 适配器解析 → 插件处理 整条链路。

这覆盖了之前没验证的时序与并发问题。

运行：cd /path/to/kiraai && python3 <此文件>
"""

import asyncio
import json
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
KIRAAI_ROOT = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
sys.path.insert(0, str(KIRAAI_ROOT))
sys.path.insert(0, str(PLUGIN_ROOT))

import websockets

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


SELF_ID = "3003003"
GROUP_ID = 188395693


class FakeNapCatServer:
    """模拟 NapCat 的 WebSocket 服务器。

    行为：
    - 接受连接
    - 响应 API 请求（按 action 返回真实格式）
    - 主动推送事件（meta_event / notice / message）
    """

    def __init__(self, self_id: int = int(SELF_ID)):
        self.self_id = self_id
        self.ws = None
        self.received_actions = []
        self.connected = asyncio.Event()
        self.lifecycle_sent = False

    async def handler(self, ws):
        self.ws = ws
        self.connected.set()
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                action = data.get("action", "")
                params = data.get("params", {})
                echo = data.get("echo")
                self.received_actions.append((action, params))
                resp = self._api_response(action, params)
                if echo:
                    resp["echo"] = echo
                await ws.send(json.dumps(resp))
        except websockets.exceptions.ConnectionClosed:
            pass

    def _api_response(self, action, params):
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0,
                    "data": {"user_id": self.self_id, "nickname": "测试Bot"}}
        if action == "get_group_info":
            return {"status": "ok", "retcode": 0,
                    "data": {"group_id": params.get("group_id"),
                             "group_name": "绿岛酒吧"}}
        if action == "get_stranger_info":
            uid = str(params.get("user_id", ""))
            names = {"1001": "指尖落黑白", "1003": "Shana", "1005": "紫小贱-ai版",
                     "1007": "天然卷samurai", "8888": "管理员C7"}
            return {"status": "ok", "retcode": 0,
                    "data": {"user_id": uid,
                             "nickname": names.get(uid, f"用户{uid}"),
                             "sex": 1, "sign": ""}}
        if action == "get_group_member_info":
            uid = str(params.get("user_id", ""))
            return {"status": "ok", "retcode": 0,
                    "data": {"user_id": uid, "nickname": f"用户{uid}",
                             "card": "", "title": "",
                             "role": "admin" if uid == "8888" else "member"}}
        if action == "get_msg":
            return {"status": "ok", "retcode": 0,
                    "data": {"message_id": params.get("message_id"),
                             "user_id": 1001, "time": int(time.time()),
                             "sender": {"nickname": "指尖落黑白"},
                             "message": [{"type": "text",
                                          "data": {"text": "被撤回的消息"}}]}}
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send_event(self, event: dict):
        """推送一条事件。"""
        if self.ws is None:
            raise RuntimeError("未连接")
        await self.ws.send(json.dumps(event))

    async def send_lifecycle(self):
        """推送 lifecycle（适配器用它判断登录成功）。"""
        await self.send_event({
            "post_type": "meta_event", "meta_event_type": "lifecycle",
            "sub_type": "connect", "time": int(time.time()),
            "self_id": self.self_id,
        })


async def main():
    print("\n=== 1. 启动模拟 NapCat WS 服务器 ===")
    server = FakeNapCatServer()
    ws_server = await websockets.serve(server.handler, "127.0.0.1", 0)
    port = ws_server.sockets[0].getsockname()[1]
    print(f"  服务器监听 127.0.0.1:{port}")
    check("WS 服务器启动", port > 0)

    print("\n=== 2. 真实 QQAdapter 通过 WS 连接 ===")
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.context import AdapterContext
    from core.adapter.src.qq.qq import QQAdapter

    info = AdapterInfo(
        enabled=True, adapter_id="napcat", name="napcat", platform="QQ",
        config={
            "self_id": SELF_ID, "permission_mode": "deny_list",
            "group_allow_list": [], "group_deny_list": [],
            "user_allow_list": [], "user_deny_list": [],
            "ws_uri": f"ws://127.0.0.1:{port}", "ws_token": "",
            "bot_pid": SELF_ID, "debug_mode": False,
        },
    )
    queue: asyncio.Queue = asyncio.Queue()
    adapter = QQAdapter(AdapterContext(info=info, event_queue=queue))

    # 启动适配器（内部会 WS 连接 + 等 lifecycle）
    adapter_task = asyncio.create_task(adapter.bot.run(
        bt_uin=SELF_ID, ws_uri=f"ws://127.0.0.1:{port}", ws_token="",
    ))

    # 等连接
    try:
        await asyncio.wait_for(server.connected.wait(), timeout=5)
        print("  WS 已连接")
    except asyncio.TimeoutError:
        check("WS 连接", False, "超时")
        return

    # 推送 lifecycle（适配器等待这个才认为登录成功）
    await server.send_lifecycle()
    await asyncio.sleep(0.3)

    check("适配器完成登录握手", adapter.bot.login_success_event.is_set())
    check("适配器调用了 get_login_info",
          any(a == "get_login_info" for a, _ in server.received_actions),
          str([a for a, _ in server.received_actions]))

    print("\n=== 3. 通过 WS 推送 notice 事件 ===")
    now = int(time.time())
    # 需要等适配器真正进入 listen 状态
    await asyncio.sleep(0.3)

    events_to_send = [
        # 戳一戳（NapCat 真实格式）
        {"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
         "time": now - 100, "self_id": int(SELF_ID), "user_id": 1001,
         "target_id": 1003, "group_id": GROUP_ID,
         "raw_info": [{"type": "qq", "uid": "u_1001"},
                      {"type": "img", "src": ""},
                      {"txt": "戳了戳", "type": "nor"},
                      {"type": "qq", "uid": "u_1003"},
                      {"txt": "", "type": "nor"}]},
        # 贴表情
        {"post_type": "notice", "notice_type": "group_msg_emoji_like",
         "time": now - 50, "self_id": int(SELF_ID), "user_id": 1005,
         "group_id": GROUP_ID, "message_id": 999888777,
         "likes": [{"emoji_id": "4", "count": 2}], "is_add": True},
        # 禁言
        {"post_type": "notice", "notice_type": "group_ban", "sub_type": "ban",
         "time": now - 30, "self_id": int(SELF_ID), "user_id": 1007,
         "operator_id": 8888, "duration": 300, "group_id": GROUP_ID},
    ]
    for ev in events_to_send:
        await server.send_event(ev)
        await asyncio.sleep(0.12)  # 模拟真实事件间隔

    # 等适配器处理（WS 回调是 create_task，需要给时间）
    await asyncio.sleep(0.8)

    received = []
    while not queue.empty():
        received.append(queue.get_nowait())

    print(f"  收到 {len(received)} 个事件")
    for e in received:
        print(f"    - notice={e.is_notice} sid={e.session.sid} "
              f"raw_type={e.message.raw_message.get('notice_type')}")

    check("WS 推送的 3 个事件全部到达", len(received) == 3, str(len(received)))
    check("事件都是 notice 类型",
          all(e.is_notice for e in received), str([e.is_notice for e in received]))
    check("sid 正确", all(e.session.sid == f"napcat:gm:{GROUP_ID}" for e in received))
    check("raw_message 完整保留",
          all(isinstance(e.message.raw_message, dict) for e in received))

    print("\n=== 4. 插件处理 WS 收到的事件 ===")
    from core.plugin.manager import PluginManager
    from core.plugin.plugin_context import PluginContext
    from core.plugin import registry
    from qq_core.env_renderer import render_env_summary

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
    context.event_bus = type("EB", (), {
        "publish": staticmethod(lambda e: asyncio.sleep(0)),
    })()
    context.provider_mgr = None
    context.plugin_mgr = PluginManager(context)

    pid = await context.plugin_mgr.load_plugin_from_dir(PLUGIN_ROOT, auto_install=False)
    inst = context.plugin_mgr.get_plugin_inst(pid)
    await inst.initialize()

    handled = 0
    for ev in received:
        await inst.on_im_message(ev)
        handled += 1

    evs = await inst.store.get_events(f"napcat:gm:{GROUP_ID}")
    summary = render_env_summary(evs, max_per_type=6, max_chars=800)
    print(f"  插件处理 {handled} 个，store {len(evs)} 条")
    print(f"  摘要: {summary}")

    check("插件处理了所有事件", handled == len(received), f"{handled}/{len(received)}")
    check("事件进入 store", len(evs) >= 2, str(len(evs)))
    check("摘要含戳一戳", "戳了戳" in summary, summary)
    check("摘要含禁言", "禁言" in summary, summary)
    # 贴表情默认不进摘要（collect_emoji_like_env=false，用户要求），
    # 而是归档到插件自己的 emoji_rewriter 用于原文标注
    check("贴表情默认不进摘要（符合设计）", "贴了" not in summary, summary)
    _arch = inst.emoji_rewriter.get(f"napcat:gm:{GROUP_ID}") if inst.emoji_rewriter else []
    check("贴表情已归档（用于原文标注）", bool(_arch), f"{len(_arch)} 条")
    if _arch:
        ann = _arch[0]["annotation"]
        print(f"    表情归档标注: {ann!r}")
        check("表情标注含 ID", "ID: 4" in ann, ann)

    print("\n=== 5. 通过 WS 推送撤回（真实链路）===")
    # 先用普通消息建立 cache
    msg_event = {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "time": now, "self_id": int(SELF_ID), "message_id": 555666777,
        "group_id": GROUP_ID, "user_id": 1001,
        "sender": {"user_id": 1001, "nickname": "指尖落黑白"},
        "message": [{"type": "text", "data": {"text": "这条会被撤回"}}],
        "raw_message": "这条会被撤回",
    }
    await server.send_event(msg_event)
    await asyncio.sleep(0.4)

    msgs = []
    while not queue.empty():
        msgs.append(queue.get_nowait())
    print(f"  收到 {len(msgs)} 个消息事件")
    for m in msgs:
        await inst.on_im_message(m)

    # 推送撤回
    await server.send_event({
        "post_type": "notice", "notice_type": "group_recall",
        "time": int(time.time()), "self_id": int(SELF_ID),
        "user_id": 1001, "operator_id": 1001,
        "message_id": 555666777, "group_id": GROUP_ID,
    })
    await asyncio.sleep(0.5)

    recalls = []
    while not queue.empty():
        recalls.append(queue.get_nowait())
    for r in recalls:
        await inst.on_im_message(r)

    archived = await inst.locator.get_recalls(f"napcat:gm:{GROUP_ID}")
    print(f"  撤回归档: {len(archived)} 条")
    if archived:
        print(f"    标注: {archived[0]['annotation']!r}")
    check("WS 撤回链路产生归档", len(archived) >= 1, str(len(archived)))

    # 清理
    await inst.terminate()
    await adapter.bot.close()
    adapter_task.cancel()
    try:
        await adapter_task
    except (asyncio.CancelledError, Exception):
        pass
    ws_server.close()
    await ws_server.wait_closed()


try:
    asyncio.run(main())
except Exception as e:
    import traceback
    traceback.print_exc()
    FAIL.append(("WS 全链路", f"{type(e).__name__}: {e}"))


print("\n" + "=" * 60)
print(f"WS 实机验证: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部通过 ===")