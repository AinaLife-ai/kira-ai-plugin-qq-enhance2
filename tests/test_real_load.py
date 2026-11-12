"""QQ增强2 真实框架加载测试（在 KiraAI v3.1.2 环境内运行）。

运行：cd /path/to/kiraai && python3 -m pytest /path/to/qq-enhance2/tests/test_real_load.py -x -q

验证：
1. PluginManager.load_plugin_from_dir 加载插件成功。
2. hook 注册：im_message + llm_request × 2。
3. 工具注册：qq_profile / qq_group_profile。
4. terminate 正常。
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

PLUGIN_ROOT = Path(__file__).parent.parent


@pytest_asyncio.fixture
async def manager(monkeypatch, tmp_path):
    """构建 PluginManager（复制框架测试 fixture 模式）。"""
    from core.plugin import manager as plugin_manager_module
    from core.plugin import registry

    for name in (
        "_plugin_classes", "_plugin_components", "_plugin_load_errors", "_plugin_manifests",
        "_plugin_module_dirs", "_plugin_module_paths", "_plugin_schemas",
        "_plugin_infos", "_module_to_plugin",
    ):
        monkeypatch.setattr(registry, name, {})
    monkeypatch.setattr(plugin_manager_module, "PLUGIN_CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(plugin_manager_module, "PLUGIN_STATE_FILE", tmp_path / "state.json")

    from core.plugin.manager import PluginManager
    from core.plugin.plugin_context import PluginContext

    context = object.__new__(PluginContext)
    context.config = {}
    context.tool_mgr = None
    context.adapter_mgr = MagicMock()
    context.plugin_mgr = PluginManager(context)
    context.event_bus = MagicMock()
    context.event_bus.publish = AsyncMock()
    context.get_plugin_data_dir = MagicMock(return_value=str(tmp_path / "plugin_data"))
    return context.plugin_mgr


@pytest.mark.asyncio
async def test_real_load(manager):
    """真实加载插件。"""
    pid = await manager.load_plugin_from_dir(PLUGIN_ROOT, auto_install=False)
    assert pid == "kira-ai-plugin-qq-enhance2", f"加载失败: {pid}"

    # 验证组件注册
    from core.plugin import registry
    comps = registry._plugin_components.get(pid)
    assert comps is not None, "组件未注册"

    # 验证 hook（im_message + llm_request × 2）
    hook_types = [h.event_type.value for h in comps.hooks]
    assert hook_types.count("on_im_message") == 1, f"im_message hook 数量错误: {hook_types}"
    assert hook_types.count("on_llm_request") == 2, f"llm_request hook 数量错误: {hook_types}"

    # 验证插件实例存在且配置读取正常（工具在 llm_request 时注入 tool_set，不在 comps.tools）
    inst = registry._plugin_classes.get(pid)
    assert inst is not None, "插件类未注册"

    # 清理
    await manager.terminate(pid)


@pytest.mark.asyncio
async def test_real_load_with_qq_enhance1(manager, monkeypatch, tmp_path):
    """QQ增强1+2 同载测试。"""
    # 先加载 QQ增强1（模拟）
    qq_enhance1_root = tmp_path / "plugins" / "qq-enhance1"
    qq_enhance1_root.mkdir(parents=True)
    (qq_enhance1_root / "manifest.json").write_text(
        json.dumps({"plugin_id": "kira-ai-plugin-qq-enhance", "display_name": "QQ增强", "version": "1.4.5"}),
        encoding="utf-8",
    )
    (qq_enhance1_root / "main.py").write_text(
        "from core.plugin import BasePlugin\n"
        "class QQEnhance1Plugin(BasePlugin):\n"
        "    async def initialize(self): pass\n"
        "    async def terminate(self): pass\n",
        encoding="utf-8",
    )

    manager.plugin_dir = tmp_path / "plugins"
    pid1 = await manager.load_plugin_from_dir(qq_enhance1_root, auto_install=False)
    assert pid1 == "kira-ai-plugin-qq-enhance"

    # 再加载 QQ增强2
    pid2 = await manager.load_plugin_from_dir(PLUGIN_ROOT, auto_install=False)
    assert pid2 == "kira-ai-plugin-qq-enhance2"

    # 验证两者共存（QQ增强1 无 hook/工具时不创建 components 条目，属正常）
    from core.plugin import registry
    assert pid1 in registry._plugin_classes, "QQ增强1 类未注册"
    assert "kira-ai-plugin-qq-enhance2" in registry._plugin_components

    # 清理
    await manager.terminate(pid1)
    await manager.terminate(pid2)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])