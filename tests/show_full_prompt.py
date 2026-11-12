"""完整展示：bot 实际收到的 prompt（逐字节，不截断）。

用真实框架组件组装，展示 LLM 真正拿到的 system + user 消息全文。

运行：cd /path/to/kiraai && python3 <此文件>
"""

import asyncio
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

from tests.real_adapter import make_real_adapter, make_poke, push_notice  # noqa

SELF_ID = "3991867505"
GID = 188395693
NICKS = {"769690776": "周武", "1593462500": "指尖落黑白", "8888": "管理员C7"}


def fmt_msg(msg) -> str:
    """逐字复刻框架 core/plugin/builtin_plugins/kira-ai/main.py:_format_user_message。"""
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


async def main():
    from core.plugin.manager import PluginManager
    from core.plugin.plugin_context import PluginContext
    from core.plugin import registry
    from core.provider import LLMRequest
    from core.agent.tool import ToolSet
    from core.agent.message import OpenAIMessage
    from core.prompt_manager import Prompt
    from core.chat.message_utils import (KiraMessageBatchEvent, KiraIMMessage,
                                         KiraMessageEvent)
    from core.chat.session import Session, Group, User
    from core.chat.message_elements import Text
    from core.chat import MessageChain
    from qq_core.event_store import EventStore

    for n in ("_plugin_classes", "_plugin_components", "_plugin_load_errors",
              "_plugin_manifests", "_plugin_module_dirs", "_plugin_module_paths",
              "_plugin_schemas", "_plugin_infos", "_module_to_plugin"):
        if hasattr(registry, n):
            getattr(registry, n).clear()

    adapter, q, _ = make_real_adapter(
        self_id=SELF_ID, adapter_name="qq", nicknames=NICKS,
        msg_text="画上去的还能换色号（",
    )
    adapter.bot.self_nickname = "紫小贱"

    ctx = object.__new__(PluginContext)
    ctx.config = {}
    ctx.tool_mgr = None
    ctx.adapter_mgr = type("AM", (), {
        "get_adapter": staticmethod(lambda n: adapter),
        "get_adapters": staticmethod(lambda: {adapter.info.name: adapter}),
    })()
    ctx.event_bus = type("EB", (), {"publish": staticmethod(lambda e: asyncio.sleep(0))})()
    ctx.provider_mgr = None
    ctx.plugin_mgr = PluginManager(ctx)

    pid = await ctx.plugin_mgr.load_plugin_from_dir(ROOT, auto_install=False)
    inst = ctx.plugin_mgr.get_plugin_inst(pid)
    await inst.initialize()
    if inst.store:
        try:
            await inst.store.stop()
        except Exception:
            pass
        inst.store = EventStore(tempfile.mkdtemp(), max_events=20)
        await inst.store.start()

    now = int(time.time())

    # ===== 场景：周武发消息 → bot自己戳人(应过滤) → 别人戳bot → 管理员撤回 =====
    await inst.on_im_message(KiraMessageEvent(
        adapter=adapter.info, supported_elements=["text"],
        message=KiraIMMessage(
            timestamp=now - 120, message_id="900001", self_id=SELF_ID,
            group=Group(group_id=GID, group_name="绿岛酒吧"),
            sender=User(user_id="769690776", nickname="周武"),
            chain=MessageChain([Text("画上去的还能换色号（")]), is_notice=False,
        ),
        timestamp=now - 120,
    ))
    for e in await push_notice(adapter, q, make_poke(int(SELF_ID), 769690776, GID, SELF_ID)):
        await inst.on_im_message(e)
    for e in await push_notice(adapter, q, make_poke(769690776, int(SELF_ID), GID, SELF_ID)):
        await inst.on_im_message(e)
    # 其他用户 → 其他用户：这条**应该**出现在群动态里（第 15 轮规则）
    for e in await push_notice(adapter, q, make_poke(1001, 1002, GID, SELF_ID)):
        await inst.on_im_message(e)
    await adapter.im._on_notice_message({
        "post_type": "notice", "notice_type": "group_recall",
        "time": now, "self_id": int(SELF_ID),
        "user_id": 769690776, "operator_id": 8888,
        "message_id": 900001, "group_id": GID,
    })
    while not q.empty():
        await inst.on_im_message(q.get_nowait())

    history_msg = KiraIMMessage(
        timestamp=now - 120, message_id="900001", self_id=SELF_ID,
        group=Group(group_id=GID, group_name="绿岛酒吧"),
        sender=User(user_id="769690776", nickname="周武"),
        chain=MessageChain([Text("画上去的还能换色号（")]), is_notice=False,
    )
    new_msg = KiraIMMessage(
        timestamp=now, message_id="900002", self_id=SELF_ID,
        group=Group(group_id=GID, group_name="绿岛酒吧"),
        sender=User(user_id="1593462500", nickname="指尖落黑白"),
        chain=MessageChain([Text("哈哈哈哈哈")]), is_notice=False,
    )

    # ===== 组装完整 LLMRequest（模拟框架真实流程）=====
    req = LLMRequest(tool_set=ToolSet())
    req.system_prompt = [
        Prompt("你是一个QQ群聊机器人，人格是「紫小贱」。", name="persona", source="system"),
        Prompt("[格式示例] 用 <msg><text>...</text></msg> 回复。", name="format", source="system"),
    ]
    # 真实结构（对齐框架 handle_im_batch_message）：
    #   messages = 历史 session_memory
    #   user_prompt = 仅本轮新收到的消息
    req.messages = [
        OpenAIMessage(role="user", content=fmt_msg(history_msg)),
        OpenAIMessage(role="assistant", content="<msg><text>确实</text></msg>"),
    ]
    req.user_prompt.append(Prompt(fmt_msg(new_msg), name="message", source="system"))

    batch = KiraMessageBatchEvent(
        supported_elements=["text"], timestamp=now,
        adapter=adapter.info,
        session=Session(adapter_name="qq", session_type="gm", session_id=str(GID)),
        messages=[new_msg],
    )
    from core.tag import TagSet
    await inst.on_llm_request(batch, req)
    await inst.inject_tools(batch, req, TagSet())
    req.assemble_prompt(dynamic_position="latest_user")

    # ===== 输出 =====
    print("=" * 76)
    print("bot 实际收到的完整 prompt（LLM 请求体，逐字节不截断）")
    print("=" * 76)
    for i, m in enumerate(req.messages):
        print(f"\n┌── messages[{i}]  role={m.role}  ({len(m.content or '')} 字符) ──")
        print(m.content)
        print("└" + "─" * 74)
    print()
    print("=" * 76)
    print(f"tools: {[t.name for t in (req.tool_set.tools or [])]}")
    print(f"总字符数: {sum(len(m.content or '') for m in req.messages)}")
    print("=" * 76)

    await inst.terminate()


asyncio.run(main())