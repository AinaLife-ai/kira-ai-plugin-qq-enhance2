"""多轮对话验证：环境块会不会污染历史 / prompt 会不会无限增长。

关键问题：
1. 连续 N 轮后，历史里会不会残留环境块？
2. 每轮的环境块是否只有 1 个（不叠加）？
3. system prompt 是否保持不变（缓存友好）？
4. 多轮后 prompt 总长度是否受控？
"""

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
if (KIRAAI / "core").is_dir():
    sys.path.insert(0, str(KIRAAI))
sys.path.insert(0, str(ROOT))

from tests.real_adapter import make_real_adapter, make_poke, make_ban, push_notice

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
        "get_adapters": staticmethod(lambda: {adapter.info.name: adapter}),
    })()
    context.event_bus = type("EB", (), {"publish": staticmethod(lambda e: asyncio.sleep(0))})()
    context.provider_mgr = None
    context.plugin_mgr = PluginManager(context)

    pid = await context.plugin_mgr.load_plugin_from_dir(ROOT, auto_install=False)
    inst = context.plugin_mgr.get_plugin_inst(pid)
    await inst.initialize()

    import tempfile
    from qq_core.event_store import EventStore
    if inst.store is not None:
        try:
            await inst.store.stop()
        except Exception:
            pass
        inst.store = EventStore(tempfile.mkdtemp(prefix="qqe2_multi_"), max_events=20)
        await inst.store.start()
    return inst


async def main():
    from core.provider import LLMRequest
    from core.agent.tool import ToolSet
    from core.prompt_manager import Prompt
    from core.chat.message_utils import KiraMessageBatchEvent, KiraIMMessage
    from core.chat.session import Session
    from core.chat import MessageChain
    from core.agent.message import OpenAIMessage
    from core.tag import TagSet

    adapter, q, _ = make_real_adapter(self_id=SELF_ID)
    inst = await build_plugin(adapter)

    history = []
    system_prompts = []
    env_counts = []
    total_lens = []

    for turn in range(10):
        # 注意：push_notice 会 drain 队列并返回事件，直接用它
        for ev in await push_notice(adapter, q, make_poke(1000 + turn, 2000, GID, SELF_ID)):
            await inst.on_im_message(ev)
        if turn % 3 == 0:
            for ev in await push_notice(adapter, q, make_ban(3000 + turn, 8888, GID, SELF_ID)):
                await inst.on_im_message(ev)

        req = LLMRequest(tool_set=ToolSet())
        req.messages = list(history)
        req.system_prompt = [
            Prompt("你是一个QQ群聊机器人。", name="base", source="system"),
            Prompt("当前群聊: 绿岛酒吧", name="chat_env", source="system"),
        ]
        req.user_prompt = [Prompt(f"第{turn}轮的用户消息", name="message", source="system")]

        batch = KiraMessageBatchEvent(
            supported_elements=["text"], timestamp=int(time.time()),
            adapter=adapter.info,
            session=Session(adapter_name="napcat", session_type="gm", session_id=str(GID)),
            messages=[KiraIMMessage(timestamp=int(time.time()), message_id=str(turn),
                                    self_id=SELF_ID, chain=MessageChain(), is_notice=False)],
        )
        await inst.on_llm_request(batch, req)
        await inst.inject_tools(batch, req, TagSet())

        env_prompts = [p for p in req.user_prompt if getattr(p, "name", "") == "qq_social_env"]
        env_counts.append(len(env_prompts))

        req.assemble_prompt(dynamic_position="latest_user")
        sp = next((m.content for m in req.messages if m.role == "system"), "")
        system_prompts.append(sp)
        total_lens.append(sum(len(m.content or "") for m in req.messages))

        persist_text = "".join(
            p.to_string() for p in req.user_prompt if getattr(p, "persist", True)
        )
        history.append(OpenAIMessage(role="user", content=persist_text))
        history.append(OpenAIMessage(role="assistant", content=f"第{turn}轮回复"))

    print(f"  10 轮完成")
    print(f"  每轮环境块数: {env_counts}")
    print(f"  每轮 prompt 总长度: {total_lens}")
    print(f"  system prompt 种类数: {len(set(system_prompts))}")

    check("每轮环境块恰好 1 个（不叠加）", all(c == 1 for c in env_counts), str(env_counts))
    check("system prompt 10 轮完全一致（缓存友好）",
          len(set(system_prompts)) == 1, f"{len(set(system_prompts))} 种")
    check("历史里不含环境块（不污染）",
          all("群动态" not in (m.content or "") for m in history),
          "历史被污染")
    check("prompt 长度线性增长而非爆炸",
          total_lens[-1] < total_lens[0] * 12, f"{total_lens[0]} → {total_lens[-1]}")

    print(f"\n  最后一轮历史尾部:")
    for m in history[-4:]:
        print(f"    {m.role}: {(m.content or '')[:60]!r}")

    await inst.terminate()


asyncio.run(main())

print("\n" + "=" * 60)
print(f"多轮对话验证: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部通过 ===")