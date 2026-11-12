"""第五轮人工审计回归测试（Bug 29-31）。

覆盖：
29. main.py 未使用 import 清理 + im_message 优先级降为 LOW（避免同优先级顺序不确定）
30. event_store 加 TTL 定期清理（原来过期事件永不清理，只在 load_all 时过滤）
31. 唤醒事件 supported_elements=[] 会导致 text tag 未注册（LLM 输出无法解析）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_core import _install_framework_stubs  # noqa

PLUGIN_ROOT = Path(__file__).parent.parent


# ============ Bug 29: import 清理 + 优先级 ============

def test_bug29_imports_clean():
    """不应有未使用的 import。"""
    import ast
    src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 收集 import 的名字
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # 排除 __future__（如 from __future__ import annotations）
            if node.module == "__future__":
                continue
            for a in node.names:
                imported[a.asname or a.name] = node.lineno
        elif isinstance(node, ast.Import):
            for a in node.names:
                imported[a.asname or a.name.split(".")[0]] = node.lineno

    # 统计每个名字在源码中出现次数（排除 import 行本身）
    unused = []
    for name, lineno in imported.items():
        # 统计非 import 行的出现次数
        count = 0
        for i, line in enumerate(src.split("\n"), 1):
            if i == lineno:
                continue
            # 简单词边界匹配
            import re
            if re.search(rf"\b{re.escape(name)}\b", line):
                count += 1
        if count == 0:
            unused.append(name)

    assert not unused, f"存在未使用 import: {unused}"
    print(f"  ✓ Bug29: 无未使用 import（{len(imported)} 个导入全部使用）")


def test_bug29_priority_low():
    """im_message 应用 LOW 优先级（避免同优先级顺序不确定）。"""
    src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    # 找到 im_message 的装饰器
    import re
    m = re.search(r"@on\.im_message\(priority=Priority\.(\w+)\)", src)
    assert m, "未找到 im_message 装饰器"
    assert m.group(1) == "LOW", f"im_message 应用 LOW 优先级，实际: {m.group(1)}"
    print("  ✓ Bug29: im_message 使用 LOW 优先级（避免与 S/Z 同优先级）")


# ============ Bug 30: TTL 清理 ============

def test_bug30_ttl_cleanup():
    """_persist_loop 应包含 TTL 清理逻辑。"""
    import re
    src = (PLUGIN_ROOT / "qq_core" / "event_store.py").read_text(encoding="utf-8")
    assert "cutoff = time.time() - self.ttl_seconds" in src, "应在 persist_loop 内按 TTL 清理"
    # 清理逻辑在 _persist_loop 方法体内
    m = re.search(r"async def _persist_loop\(self\):(.*?)(?=\n    async def |\n    def |\Z)", src, re.S)
    assert m, "未找到 _persist_loop"
    assert "ttl_seconds" in m.group(1), "TTL 清理应在 _persist_loop 内"
    print("  ✓ Bug30: event_store 定期按 TTL 清理过期事件")


# ============ Bug 31: supported_elements ============

def test_bug31_supported_elements():
    """唤醒事件的 supported_elements 不能为空。"""
    src = (PLUGIN_ROOT / "qq_core" / "wake_scheduler.py").read_text(encoding="utf-8")
    assert "supported_elements=[]" not in src, "不能传空 supported_elements（text tag 不会注册）"
    assert "_get_supported_elements" in src, "应从适配器获取真实元素列表"
    # 应有回退到 ["text"]
    assert 'return ["text"]' in src, "应有 text 回退"
    print("  ✓ Bug31: 唤醒事件传真实 supported_elements（避免 text tag 未注册）")


def test_bug31_fallback_works():
    """_get_supported_elements 在拿不到适配器时回退 ["text"]。"""
    from qq_core.wake_scheduler import WakeScheduler

    class FakeInfo:
        name = "napcat"

    # 现在是实例方法（需要 self._self_id_resolver 等），用最小实例测试
    sched = object.__new__(WakeScheduler)
    sched.ctx = None
    sched._self_id_resolver = None
    sched.self_id = "1"

    res = sched._get_supported_elements(FakeInfo(), None)
    assert res == ["text"], f"应回退 [text]，实际: {res}"

    # 有 supported_elements 字段时优先用
    class FakeInfo2:
        supported_elements = ["text", "at", "reply"]
    res2 = sched._get_supported_elements(FakeInfo2(), None)
    assert res2 == ["text", "at", "reply"], f"应用字段值: {res2}"
    print(f"  ✓ Bug31: 回退与字段优先都正常（{res} / {res2}）")


if __name__ == "__main__":
    import re
    test_bug29_imports_clean()
    test_bug29_priority_low()
    test_bug30_ttl_cleanup()
    test_bug31_supported_elements()
    test_bug31_fallback_works()
    print("=== 第五轮 5 项回归测试全部通过 ===")