"""第八轮审计回归测试（Bug 38-39）。

覆盖：
38. 配置值类型错误会崩（裸 int() 转换）
39. 文档/注释过时（im_message 优先级已从 HIGH 改 LOW）
+ README 缺少配置热更新说明
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_core import _install_framework_stubs  # noqa

PLUGIN_ROOT = Path(__file__).parent.parent


def test_bug38_safe_int():
    """配置值类型异常时不崩。"""
    from qq_core.fmt import safe_int, safe_float

    # 正常情况
    assert safe_int(20, 5) == 20
    assert safe_int("20", 5) == 20
    assert safe_int("20.7", 5) == 20
    # 异常情况回退默认
    assert safe_int(None, 5) == 5
    assert safe_int("", 5) == 5
    assert safe_int("abc", 5) == 5
    assert safe_int([], 5) == 5
    assert safe_int({}, 5) == 5
    # 边界值保留
    assert safe_int(0, 5) == 0
    assert safe_int(-1, 5) == -1

    assert safe_float(20.5, 1.0) == 20.5
    assert safe_float("20.5", 1.0) == 20.5
    assert safe_float("abc", 1.0) == 1.0
    assert safe_float(None, 1.0) == 1.0
    print("  ✓ Bug38: safe_int/safe_float 全类型安全")


def test_bug38_no_raw_int():
    """main.py 不应有裸 int(plugin_cfg.get(...))。"""
    src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    import re
    # 匹配 int(self.plugin_cfg.get(...)) 或 float(...)
    bad = re.findall(r'\b(?:int|float)\(\s*self\.plugin_cfg\.get', src)
    assert not bad, f"应使用 _safe_int/_safe_float: {len(bad)} 处裸转换"
    # 确认用了安全转换
    assert "_safe_int(self.plugin_cfg.get" in src, "应使用 _safe_int"
    assert "_safe_float(self.plugin_cfg.get" in src, "应使用 _safe_float"
    print("  ✓ Bug38: 无裸 int()/float() 转换")


def test_bug39_doc_priority():
    """文档中的优先级描述应与代码一致。"""
    src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    doc = src[:src.find('"""', src.find('"""') + 3) + 3]
    # 代码实际用 LOW
    import re
    m = re.search(r"@on\.im_message\(priority=Priority\.(\w+)\)", src)
    actual = m.group(1) if m else "?"
    assert actual == "LOW", f"代码优先级: {actual}"
    # 文档不应说 HIGH
    assert "im_message(HIGH)" not in doc, f"文档过时（应为 LOW）: {doc[:200]}"
    print(f"  ✓ Bug39: 文档与代码优先级一致（{actual}）")


def test_readme_reload_note():
    """README 应说明配置需重载插件。"""
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert "重载插件" in readme or "重新加载" in readme, "应说明配置修改后需重载"
    assert "多 QQ 号" in readme or "多适配器" in readme, "应说明多号支持"
    print("  ✓ README: 已说明配置重载与多号支持")


def test_readme_path_correct():
    """README 安装路径应写 qq_core（不是 core）。"""
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    # 不应指导用户复制 core/（那是在插件目录下不存在的）
    if "/ `core/`" in readme or "`core/`" in readme:
        raise AssertionError("安装路径应为 qq_core/（插件内目录名）")
    assert "qq_core/" in readme, "应说明 qq_core/ 目录"
    print("  ✓ README: 安装路径正确（qq_core/）")


def test_config_extremes_no_crash():
    """极端配置值不应导致插件崩溃。"""
    from qq_core.fmt import safe_int
    # 模拟用户乱填
    weird = ["abc", None, "", [], {}, "  ", "20abc", "1e5", True, False]
    for v in weird:
        try:
            r = safe_int(v, 10)
            assert isinstance(r, int), f"{v!r} -> {r!r} 不是 int"
        except Exception as e:
            raise AssertionError(f"{v!r} 抛异常: {e}")
    print(f"  ✓ 极端配置值 {len(weird)} 种都不崩")


if __name__ == "__main__":
    test_bug38_safe_int()
    test_bug38_no_raw_int()
    test_bug39_doc_priority()
    test_readme_reload_note()
    test_readme_path_correct()
    test_config_extremes_no_crash()
    print("=== 第八轮 6 项回归测试全部通过 ===")