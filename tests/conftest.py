"""pytest 全局夹具：每个测试前清理插件模块级全局状态。

插件用了几个模块级全局字典（贴表情归档等），跨测试文件会互相污染，
导致"单独跑通过、全量跑失败"的假故障。

注意：
- 只有在**没有真实 KiraAI 框架**时才安装 stub。
  框架环境下（test_real_load / test_e2e）绝不能装，否则会污染
  真实的 core.plugin / core.chat 模块。
- 判断方式：尝试导入 core.plugin.manager，失败才装 stub。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _has_real_framework() -> bool:
    """检测是否在真实 KiraAI 环境下运行。"""
    try:
        import core.plugin.manager  # noqa: F401
        import core.chat.message_utils  # noqa: F401
        return True
    except Exception:
        return False


# 仅在独立环境（无框架）安装 stub
if not _has_real_framework():
    try:
        from tests.test_core import _install_framework_stubs  # noqa
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clean_plugin_globals():
    """每个测试用例前清空插件全局状态。"""
    try:
        from qq_core.emoji_rewriter import _EMOJI_STORE
        _EMOJI_STORE.clear()
    except Exception:
        pass
    yield
    try:
        from qq_core.emoji_rewriter import _EMOJI_STORE
        _EMOJI_STORE.clear()
    except Exception:
        pass