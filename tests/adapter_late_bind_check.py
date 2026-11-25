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

# 模拟真实安装：框架把插件加载成 `plugins.<目录>` 包，**插件根目录不在 sys.path**，
# 顶层 `qq_core` 不可导入（测试环境默认能导入，会掩盖 tools 里的绝对导入 bug）。
sys.path[:] = [p for p in sys.path if str(p) != str(ROOT)]
for _n in list(sys.modules):
    if _n == "qq_core" or _n.startswith("qq_core."):
        del sys.modules[_n]

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

    # 5) 真实安装布局：tools 必须通过 _bridge 拿到 qq_core（2026-11-25 实机 bug）
    import importlib
    import importlib.util

    check("真实布局：顶层 qq_core 不可导入", importlib.util.find_spec("qq_core") is None)
    mod_name = [m for m in sys.modules if m.endswith(".tools.qq_profile")][0]
    tool_mod = importlib.import_module(mod_name)
    bridge = importlib.import_module(mod_name.replace("qq_profile", "_bridge"))
    av = bridge.load("avatar_view")
    check("_bridge 能拿到插件自己的 avatar_view", av is not None and hasattr(av, "describe_avatar_ex"),
          getattr(av, "__name__", None))
    check("avatar_view 属于插件包（不是顶层）",
          bool(av) and str(getattr(av, "__name__", "")).startswith(str(inst.__class__.__module__).rsplit(".", 1)[0]))

    # 5.1 头像识别：替换 desc_img，确认真的走到 VLM 分支
    class _PM:
        @staticmethod
        def get_default_vlm():
            return object()

    inst.ctx.provider_mgr = _PM()
    tool = tool_mod.QQProfileTool(ctx=inst.ctx, adapter=adapter, self_id="583893217")

    async def _fake_desc(**kw):
        return "一只蓝猫"

    orig_desc = getattr(av, "_desc_img", None)
    av._desc_img = _fake_desc
    try:
        from core.utils.common_utils import desc_img as _real
        # 真框架下 _resolve_desc_img 会从 core 取，直接替换模块函数
        import core.utils.common_utils as _cu
        orig_real = _cu.desc_img
        _cu.desc_img = _fake_desc
        desc = await tool._describe_avatar("https://q1.qlogo.cn/g?b=qq&nk=769690776&s=640")
        _cu.desc_img = orig_real
    finally:
        if orig_desc is not None:
            av._desc_img = orig_desc
    check("真实布局下头像能识别（不再报缺模块）", desc == "一只蓝猫",
          (desc, getattr(tool, "_avatar_desc_reason", "")))

    # 5.1b 群资料工具同样走桥
    group_mod = importlib.import_module(mod_name.replace("qq_profile", "qq_group_profile"))
    gt = group_mod.QQGroupProfileTool(ctx=inst.ctx, adapter=adapter, self_id="583893217")
    import core.utils.common_utils as _cu2
    orig2 = _cu2.desc_img
    _cu2.desc_img = _fake_desc
    try:
        gdesc = await gt._describe_avatar("https://p.qlogo.cn/gh/123/123/640")
    finally:
        _cu2.desc_img = orig2
    check("真实布局下群头像也能识别", gdesc == "一只蓝猫",
          (gdesc, getattr(gt, "_avatar_desc_reason", "")))

    # 5.2 fmt 桥：本地文件 → base64://
    tmp_png = Path(tempfile.mkdtemp()) / "a.png"
    tmp_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    uri, err = tool_mod.QQProfileTool._prepare_file(str(tmp_png))
    check("真实布局下 file→base64 桥可用", uri.startswith("base64://") and not err, (uri[:24], err))
    guri, gerr = group_mod.QQGroupProfileTool._prepare_file(str(tmp_png))
    check("真实布局下群工具 file 桥可用", guri.startswith("base64://") and not gerr, (guri[:24], gerr))

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
