"""晚绑定验证（独立脚本，需真实框架）：
    python3 tests/adapter_late_bind_check.py

覆盖 2026-11-25 实机：
1. 插件启动时**没有任何 QQ 适配器** → 不能早退（store/locator 必须初始化），
   工具仍要注册进 tool_set（用户反馈"开了资料工具却看不到 qq_profile"）。
2. 适配器稍后出现 → 按事件实时解析到新实例（不是缓存里的僵尸）。
3. 适配器被"重启"（换新实例）→ 解析到新实例，缓存自动刷新。
"""

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path(os.environ.get("KIRA_ROOT", "/var/minis/workspace/qq_enhance2_design/kiraai_latest"))
sys.path.insert(0, str(KIRAAI))
sys.path.insert(0, str(ROOT))
os.chdir(KIRAAI)

from tests.real_adapter import make_real_adapter  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


async def build():
    from core.plugin.manager import PluginManager
    from core.plugin import registry
    from core.plugin.plugin_context import PluginContext

    for name in ("_plugin_classes", "_plugin_components", "_plugin_load_errors",
                 "_plugin_manifests", "_plugin_module_dirs", "_plugin_module_paths",
                 "_plugin_schemas", "_plugin_infos", "_module_to_plugin"):
        if hasattr(registry, name):
            getattr(registry, name).clear()

    state = {"adapter": None}

    class AM:
        def get_adapter(self, name):
            return state["adapter"]

        def get_adapters(self):
            return {state["adapter"].info.name: state["adapter"]} if state["adapter"] else {}

    class EB:
        @staticmethod
        async def publish(e):
            pass

    ctx = object.__new__(PluginContext)
    ctx.config = {}
    ctx.tool_mgr = None
    ctx.adapter_mgr = AM()
    ctx.event_bus = EB()
    ctx.provider_mgr = None
    tmp = tempfile.mkdtemp(prefix="late_bind_")
    ctx.get_plugin_data_dir = lambda *a, **k: tmp
    ctx.get_buffer = lambda sid: types.SimpleNamespace(buffer=[])
    ctx.plugin_mgr = PluginManager(ctx)
    pid = await ctx.plugin_mgr.load_plugin_from_dir(ROOT, auto_install=False)
    inst = ctx.plugin_mgr.get_plugin_inst(pid)
    return inst, state


async def main():
    inst, state = await build()

    # 1) 启动时无适配器
    await inst.initialize()
    check("无适配器时 initialize 不早退（store 已建）", inst.store is not None)
    check("无适配器时 wake_scheduler 已建", inst.wake_scheduler is not None)
    check("无适配器时 adapter 为 None", inst.adapter is None, repr(inst.adapter))

    adapter, queue, calls = make_real_adapter(self_id="583893217")
    name = adapter.info.name

    # 2) 工具照样注册
    from core.agent.tool import ToolSet
    req = types.SimpleNamespace(tool_set=ToolSet())
    ev = types.SimpleNamespace(adapter=adapter.info, sid=f"{name}:gm:115985242")
    await inst.inject_tools(ev, req)
    names = [t.name for t in req.tool_set.tools]
    check("无适配器时仍注入 qq_profile", "qq_profile" in names, names)
    check("无适配器时仍注入 qq_group_profile", "qq_group_profile" in names, names)

    # 3) 适配器出现 → 实时解析到它
    state["adapter"] = adapter
    got, sid = inst._resolve_adapter(name)
    check("适配器出现后实时解析到活实例", got is adapter, type(got).__name__)
    check("self_id 正确", sid == "583893217", sid)

    # 4) 适配器被重启（新实例）→ 仍取新实例
    adapter2, _q2, _c2 = make_real_adapter(self_id="583893217")
    state["adapter"] = adapter2
    got2, _ = inst._resolve_adapter(name)
    check("适配器重启后解析到新实例（不是缓存僵尸）", got2 is adapter2, type(got2).__name__)

    await inst.terminate()
    print("\n" + "=" * 60)
    print(f"晚绑定验证: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for n, d in FAIL:
            print(f"  FAIL {n}: {d}")
        sys.exit(1)
    print("=== 全部通过 ===")


if __name__ == "__main__":
    asyncio.run(main())
