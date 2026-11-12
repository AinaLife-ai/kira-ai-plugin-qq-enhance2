"""S/Z 聊天插件兼容性验证：我们的唤醒事件会不会污染它们的统计。

关键机制（从 S/Z 源码核实）：
- Z 的 `_is_system_event()` 判定：`sender.user_id` 以 `system_` 开头 → 系统事件
  → 不参与存在感/评分/骚扰统计
- 我们的唤醒事件 sender = `system_qq_enhance2`（正是为此设计）

本测试验证：
1. 我们的唤醒事件 sender 确实以 `system_` 开头
2. 用 Z 的真实判定逻辑跑一遍，确认返回 True
3. 我们的**他人事件**（不 trigger、chain 空）会被框架丢弃，不进 S/Z
4. 唤醒文本不含 S 的禁言解析模式（防误触发 mute 状态）
"""

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
SZ_DIR = Path("/var/minis/workspace/qq_enhance2_design")

if (KIRAAI / "core").is_dir():
    sys.path.insert(0, str(KIRAAI))
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


print("\n=== 1. 唤醒事件的 sender 必须是 system_ 前缀 ===")

# 从源码确认
ws_src = (ROOT / "qq_core" / "wake_scheduler.py").read_text(encoding="utf-8")
m = re.search(r'User\(user_id="([^"]+)",\s*nickname="([^"]+)"\)', ws_src)
print(f"  找到唤醒事件 sender: {m.groups() if m else 'NOT FOUND'}")
check("唤醒事件 sender 有 system_ 前缀",
      bool(m) and m.group(1).startswith("system_"),
      m.group(1) if m else "未找到")

SENDER_ID = m.group(1) if m else ""

print("\n=== 2. 用 Z 的真实判定逻辑验证 ===")

# Z 的判定：sender in _SYSTEM_SENDER_IDS or sender.startswith("system_")
z_src = (SZ_DIR / "default_chat_z" / "chat_enhance.py").read_text(encoding="utf-8")
has_prefix_check = 'sender.startswith("system_")' in z_src
check("Z 用 system_ 前缀判定系统事件", has_prefix_check)

# 实际用 Z 的规则判断我们的 sender
def z_is_system_event(sender_id: str, is_notice: bool, raw: dict = None) -> bool:
    """复刻 Z 的 _is_system_event 判定逻辑。"""
    sender = str(sender_id or "")
    if sender.startswith("system_"):
        return True
    if is_notice:
        is_poke = (isinstance(raw, dict)
                   and raw.get("notice_type") == "notify"
                   and raw.get("sub_type") == "poke")
        return not is_poke
    return False


check(f"Z 判定我们的 sender ({SENDER_ID}) 为系统事件",
      z_is_system_event(SENDER_ID, is_notice=False), SENDER_ID)
check("普通用户的 sender 不被判为系统事件",
      not z_is_system_event("1001", is_notice=False))

print("\n=== 3. S 的同类逻辑 ===")
s_src = (SZ_DIR / "sustained_chat" / "main.py").read_text(encoding="utf-8")
# S 里也有 _is_system_event（或类似）
s_has = "system_" in s_src
check("S 也有 system_ 前缀相关判定", s_has)


print("\n=== 4. 唤醒文本不含 S 的禁言解析模式 ===")

# S 用正则解析 "[System 用户X(昵称)禁言了你N秒]"
s_mute_pattern = re.search(r'_MUTE_NOTICE_RE\s*=\s*re\.compile\(([^\n]+)', s_src)
print(f"  S 的禁言正则: {s_mute_pattern.group(1)[:80] if s_mute_pattern else '未找到'}")

# 我们的所有唤醒文本模板
templates = re.findall(r'text = f?"(\[System[^"]{0,200})"', ws_src)
print(f"  我们的唤醒模板 {len(templates)} 条")
bad = [t for t in templates if "禁言" in t]
check("唤醒文本不含'禁言'关键词（防 S 误触发 mute）", not bad, str(bad))

# 用 S 的正则实际跑一遍我们的文本
if s_mute_pattern:
    pat_str = s_mute_pattern.group(1)
    # 提取正则字面量（简化：找 r"..." 或 '...'）
    lit = re.search(r'[rR]?["\'](.+?)["\']', pat_str)
    if lit:
        try:
            pat = re.compile(lit.group(1))
            for t in templates:
                if pat.search(t):
                    check(f"模板 {t[:30]!r} 不被 S 的禁言正则命中", False, "会被误判")
                    break
            else:
                check("所有唤醒模板都不会被 S 的禁言正则命中", True)
        except re.error:
            check("S 的正则可解析", False, "正则语法无法解析")


print("\n=== 5. 他人事件不入 S/Z（chain 空 → 框架丢弃）===")


async def test_others_events_empty():
    from tests.real_adapter import make_real_adapter, make_poke, push_notice
    a, q, _ = make_real_adapter(self_id="3003003")
    evs = await push_notice(a, q, make_poke(1001, 1003, 188395693, "3003003"))
    check("他人 poke 事件产生", len(evs) == 1)
    if evs:
        e = evs[0]
        # 框架的 receive stage: if event.message.chain.is_empty(): return False
        # 我们的他人事件不 trigger，chain 保持空
        check("他人事件 chain 为空（框架会丢弃）", len(e.message.chain) == 0,
              str(len(e.message.chain)))
        check("他人事件 is_mentioned 为假（不会触发）",
              not e.is_mentioned, str(e.is_mentioned))


asyncio.run(test_others_events_empty())


print("\n=== 6. 我们的唤醒事件带 is_mentioned=True（会正常触发）===")


async def test_wake_event():
    from tests.real_adapter import make_real_adapter, make_emoji_like, push_notice
    from core.plugin.manager import PluginManager
    from core.plugin.plugin_context import PluginContext
    from core.plugin import registry
    import tempfile

    for n in ("_plugin_classes", "_plugin_components", "_plugin_load_errors",
              "_plugin_manifests", "_plugin_module_dirs", "_plugin_module_paths",
              "_plugin_schemas", "_plugin_infos", "_module_to_plugin"):
        if hasattr(registry, n):
            getattr(registry, n).clear()

    a, q, _ = make_real_adapter(self_id="3003003")
    published = []

    ctx = object.__new__(PluginContext)
    ctx.config = {}
    ctx.tool_mgr = None
    ctx.adapter_mgr = type("AM", (), {
        "get_adapter": staticmethod(lambda n: a),
        "get_all_adapters": staticmethod(lambda: [a]),
    })()

    class EB:
        @staticmethod
        async def publish(e):
            published.append(e)
    ctx.event_bus = EB()
    ctx.provider_mgr = None
    ctx.plugin_mgr = PluginManager(ctx)

    pid = await ctx.plugin_mgr.load_plugin_from_dir(ROOT, auto_install=False)
    inst = ctx.plugin_mgr.get_plugin_inst(pid)
    await inst.initialize()

    # 记录自己的消息 → 触发"自己被贴表情"
    await inst.locator.record_incoming("888999", "3003003", "我", "我的消息", is_self=True)
    for e in await push_notice(a, q, make_emoji_like(1005, 888999, 188395693, "3003003")):
        await inst.on_im_message(e)

    check("唤醒事件已发布", len(published) == 1, str(len(published)))
    if published:
        w = published[0]
        check("唤醒事件 sender 带 system_ 前缀",
              str(w.message.sender.user_id).startswith("system_"),
              w.message.sender.user_id)
        check("唤醒事件 is_mentioned=True", w.is_mentioned is True)
        check("唤醒事件 process_strategy=trigger", w.process_strategy == "trigger",
              w.process_strategy)
        # 用 Z 的规则判定
        check("Z 会把它当系统事件（不污染统计）",
              z_is_system_event(w.message.sender.user_id, w.is_notice))

    await inst.terminate()


asyncio.run(test_wake_event())


print("\n" + "=" * 60)
print(f"S/Z 兼容性验证: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部通过 ===")