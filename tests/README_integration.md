"""QQ增强2 真实框架加载验证：用 KiraAI v3.1.2 的 PluginManager 实际加载插件。

运行方式（在框架环境）：
  cd /path/to/kiraai && python3 -c "
import sys; sys.path.insert(0, '.')
import asyncio
from pathlib import Path
from core.plugin import PluginManager, PluginContext
from core.event_bus import EventBus

async def main():
    bus = EventBus()
    ctx = PluginContext(event_bus=bus)
    mgr = PluginManager(ctx)
    pid = await mgr.load_plugin_from_dir(Path('/path/to/qq-enhance2'), auto_install=False)
    print('loaded:', pid)
    # 验证组件
    from core.plugin import registry
    comps = registry._plugin_components.get(pid)
    print('hooks:', [h.event_type.value for h in comps.hooks])
    print('tools:', [t.name for t in comps.tools])
    await mgr.terminate(pid)

asyncio.run(main())
"

本文件是文档化的手动验证脚本（需真实框架环境），不参与自动化测试。
"""

# 此文件仅作验证说明，无可执行测试。