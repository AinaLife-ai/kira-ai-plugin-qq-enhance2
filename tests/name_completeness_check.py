"""昵称补全验证：所有事件类型的 actor / target 昵称是否完整。

用户反馈：日志里 "用户769690776(周武) 戳了戳 用户1593462500" —— target 没有昵称。
根因：框架的 notice 只填 sender.nickname（=actor），target 根本没查。

本测试覆盖全部事件类型。
"""

import asyncio
import re
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

from tests.real_adapter import make_real_adapter, push_notice  # noqa

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
NOW = int(time.time())

NICKS = {
    "769690776": "周武",
    "1593462500": "指尖落黑白",
    "1001": "张三",
    "1003": "Shana",
    "1005": "紫小贱-ai版",
    "1007": "天然卷samurai",
    "8888": "管理员C7",
    "9009": "退群的人",
}


def make_raw(notice_type, sub_type="", **fields):
    d = {
        "post_type": "notice", "notice_type": notice_type,
        "time": NOW, "self_id": int(SELF_ID), "group_id": GID,
    }
    if sub_type:
        d["sub_type"] = sub_type
    d.update(fields)
    return d


async def build_plugin(adapter):
    from core.plugin.manager import PluginManager
    from core.plugin.plugin_context import PluginContext
    from core.plugin import registry

    for n in ("_plugin_classes", "_plugin_components", "_plugin_load_errors",
              "_plugin_manifests", "_plugin_module_dirs", "_plugin_module_paths",
              "_plugin_schemas", "_plugin_infos", "_module_to_plugin"):
        if hasattr(registry, n):
            getattr(registry, n).clear()

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

    from qq_core.event_store import EventStore
    if inst.store is not None:
        try:
            await inst.store.stop()
        except Exception:
            pass
        inst.store = EventStore(tempfile.mkdtemp(prefix="qqe2_name_"), max_events=50)
        await inst.store.start()
    return inst


async def main():
    from qq_core.env_renderer import render_env_summary

    adapter, q, calls = make_real_adapter(
        self_id=SELF_ID, adapter_name="napcat", nicknames=NICKS,
        api_overrides={
            "get_group_member_info": lambda p: {
                "status": "ok", "data": {
                    "user_id": p.get("user_id"),
                    "nickname": NICKS.get(str(p.get("user_id")), ""),
                    "card": "",
                    "title": "",
                    "role": "admin" if str(p.get("user_id")) == "8888" else "member",
                }
            },
        },
    )
    inst = await build_plugin(adapter)

    cases = [
        ("poke", make_raw("notify", "poke", user_id=769690776, target_id=1593462500,
                          raw_info=[{"type": "qq"}, {"type": "img"}, {"txt": "戳了戳"},
                                    {"type": "qq"}, {"txt": ""}])),
        ("group_ban", make_raw("group_ban", "ban", user_id=1005, operator_id=8888,
                               duration=600)),
        ("emoji_like", make_raw("group_msg_emoji_like", user_id=1007,
                                message_id=123456789,
                                likes=[{"emoji_id": "4", "count": 1}], is_add=True)),
        ("group_decrease", make_raw("group_decrease", "leave", user_id=9009,
                                    operator_id=8888)),
        ("group_recall", make_raw("group_recall", user_id=1001, operator_id=1001,
                                  message_id=555)),
        ("title", make_raw("notify", "title", user_id=1003, title="活跃分子")),
    ]

    print("\n=== 逐类型推送 ===")
    for name, raw in cases:
        try:
            evs = await push_notice(adapter, q, raw)
            for e in evs:
                await inst.on_im_message(e)
            print(f"  {name:16} -> {len(evs)} 个事件")
        except Exception as e:
            check(f"{name} 推送不崩", False, f"{type(e).__name__}: {e}")

    evs = await inst.store.get_events(SID)
    print(f"\n=== store 中 {len(evs)} 条事件 ===")
    for e in evs:
        print(f"  {e.etype:16} actor={e.actor!r:12}({e.actor_name!r:15}) "
              f"target={e.target!r:12}({e.target_name!r})")

    print("\n=== 昵称完整性 ===")
    for e in evs:
        if e.actor and e.actor != SELF_ID:
            check(f"{e.etype}: actor {e.actor} 有昵称",
                  bool(e.actor_name), f"actor_name={e.actor_name!r}")
        if e.target:
            check(f"{e.etype}: target {e.target} 有昵称",
                  bool(e.target_name), f"target_name={e.target_name!r}")

    print("\n=== 渲染结果 ===")
    summary = render_env_summary(evs, max_per_type=10, max_chars=3000)
    print(f"  {summary}")

    empty_paren = re.findall(r"用户\d+\)", summary)
    check("渲染无空昵称括号", not empty_paren, str(empty_paren))
    # 裸 QQ = "用户123456" 后面既不跟括号也不跟数字（避免贪婪回溯误判）
    bare = re.findall(r"用户(\d{6,})(?![0-9（(])", summary)
    check("渲染无裸 QQ 号（都有昵称）", not bare, str(bare))

    print(f"\n=== 缓存统计 ===")
    print(f"  {inst.name_resolver.stats()}")

    await inst.terminate()


asyncio.run(main())

print("\n" + "=" * 60)
print(f"昵称补全验证: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部通过 ===")