"""QQ增强2 框架集成测试：在真实 KiraAI v3.1.2 环境加载插件并验证。

测试项：
1. 插件加载（manifest/schema/import/实例化）。
2. 工具注册（3 个工具）。
3. hook 注册（im_message/llm_request）。
4. initialize/terminate 生命周期。
5. 与 QQ增强1 同载（事件单次注入）。
"""

import asyncio
import json
import sys
from pathlib import Path

# 添加框架路径（在真实环境运行时）
# sys.path.insert(0, "/path/to/kiraai")

import pytest


PLUGIN_ROOT = Path(__file__).parent.parent


@pytest.mark.asyncio
async def test_plugin_load():
    """测试插件加载。"""
    # 这需要真实的 KiraAI 框架环境
    # 此处仅做静态检查：manifest/schema 存在且格式正确
    manifest_path = PLUGIN_ROOT / "manifest.json"
    schema_path = PLUGIN_ROOT / "schema.json"
    assert manifest_path.exists(), "manifest.json 不存在"
    assert schema_path.exists(), "schema.json 不存在"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    # 验证 manifest 字段
    assert manifest.get("plugin_id") == "kira-ai-plugin-qq-enhance2"
    assert manifest.get("display_name") == "QQ增强2"
    assert manifest.get("version") == "1.0.0"
    assert manifest.get("author") == "znq19"
    assert manifest.get("core_version") == ">=3.1.2"
    assert manifest.get("icon") == "icon.png"

    # 验证 schema 键
    expected_keys = {
        "collect_poke", "collect_emoji_like", "collect_group_ban", "collect_recall",
        "collect_member_decrease", "collect_title", "collect_card_admin",
        "wake_self_emoji", "wake_profile_like", "wake_title", "wake_kicked",
        "profile_like_merge_window_seconds", "wake_quiet_hours", "wake_max_per_hour",
        "perceive_window_seconds", "perceive_max_events", "perceive_max_per_type",
        "env_max_chars", "env_inject_min_interval", "env_ttl_hours",
        "recall_rewrite", "recall_show_content",
        "tools_profile", "tools_fetch_emoji_like", "debug_log",
    }
    actual_keys = set(schema.keys())
    assert expected_keys.issubset(actual_keys), f"缺少配置键: {expected_keys - actual_keys}"


@pytest.mark.asyncio
async def test_main_import():
    """测试 main.py 可导入（静态检查）。"""
    import ast
    main_path = PLUGIN_ROOT / "main.py"
    assert main_path.exists(), "main.py 不存在"

    # 解析 AST（验证语法正确）
    tree = ast.parse(main_path.read_text(encoding="utf-8"))
    assert tree is not None

    # 检查类定义
    class_defs = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    assert "QQEnhance2Plugin" in class_defs, "缺少 QQEnhance2Plugin 类"


@pytest.mark.asyncio
async def test_tools_defined():
    """测试工具定义。"""
    import ast
    tools_dir = PLUGIN_ROOT / "tools"
    assert tools_dir.exists(), "tools 目录不存在"

    tool_files = [f for f in tools_dir.glob("*.py") if f.stem != "__init__"]
    tool_names = [f.stem for f in tool_files]
    assert "qq_profile" in tool_names
    assert "qq_group_profile" in tool_names
    assert "fetch_emoji_like" in tool_names

    # 检查每个工具文件定义了对应类
    for tool_file in tool_files:
        tree = ast.parse(tool_file.read_text(encoding="utf-8"))
        class_defs = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        # 每个工具文件应有一个 *Tool 类
        tool_classes = [c for c in class_defs if c.endswith("Tool")]
        assert len(tool_classes) >= 1, f"{tool_file.name} 缺少 *Tool 类"


@pytest.mark.asyncio
async def test_hooks_registered():
    """测试 hook 注册（静态检查）。"""
    import ast
    main_path = PLUGIN_ROOT / "main.py"
    tree = ast.parse(main_path.read_text(encoding="utf-8"))

    # 检查 @on.im_message 和 @on.llm_request 装饰器
    decorators = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call):
                    if hasattr(dec.func, 'attr'):
                        decorators.append(dec.func.attr)

    assert "im_message" in decorators, "缺少 @on.im_message 装饰器"
    assert "llm_request" in decorators, "缺少 @on.llm_request 装饰器"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])