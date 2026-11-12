"""第十轮审计回归测试（Bug 42-43）：锁定"mock 失真"这类问题。

教训来源：第九轮实机验证发现 FakeAdapter 多给了 `platform` 属性，
把"真实 QQAdapter 没有 platform"的 bug 藏了 8 轮。

本测试确保：
42. 测试用的适配器替身不能有真实类没有的属性
43. 代码里所有适配器属性访问都要有回退（不能假设单一结构）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.real_adapter import ensure_framework_path, _has_real_framework
ensure_framework_path()

PLUGIN_ROOT = Path(__file__).parent.parent

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


print("\n=== 42. 适配器替身结构必须对齐真实类 ===")

if _has_real_framework():
    from tests.real_adapter import make_real_adapter
    adapter, _q, _c = make_real_adapter()

    # 真实 QQAdapter（或对齐的 stub）不应有 platform 属性
    check("适配器没有 platform 属性（平台在 info.platform）",
          not hasattr(adapter, "platform"),
          f"hasattr={hasattr(adapter, 'platform')}")
    check("适配器有 info 属性", hasattr(adapter, "info"))
    check("info.platform == 'QQ'", adapter.info.platform == "QQ", adapter.info.platform)
    check("适配器有 bot 属性", hasattr(adapter, "bot"))
    check("适配器没有 name 属性（名字在 info.name）",
          not hasattr(adapter, "name"), f"hasattr={hasattr(adapter, 'name')}")
    check("info.name 存在", getattr(adapter.info, "name", None) is not None)
else:
    print("  (跳过：无真实框架，stub 模式)")

# 检查 real_adapter 的 stub 也遵守这个约束
import tests.real_adapter as ra
src = Path(ra.__file__).read_text(encoding="utf-8")
check("_StubAdapter 注释说明不能加 platform",
      "没有** platform 属性" in src or "没有 platform 属性" in src,
      "应保留教训注释")

# 检查 _StubAdapter 类体内确实没有 platform 赋值
stub_block = src[src.find("class _StubAdapter"):src.find("class _StubBot")]
check("_StubAdapter 没有 self.platform 赋值",
      "self.platform" not in stub_block, stub_block[:200])


print("\n=== 43. 代码不能假设单一适配器结构 ===")

main_src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")

# main.py 的平台检测必须有多级回退
check("平台检测用 info.platform 优先",
      'getattr(getattr(ada, "info", None), "platform", None)' in main_src)
check("平台检测有 ada.platform 回退",
      'or getattr(ada, "platform", None)' in main_src)
check("名字取 info.name 优先",
      'getattr(info, "name", None)' in main_src)
check("config 有双路径回退",
      'getattr(ada, "config", None) or getattr(info, "config", None)' in main_src)

# 不能有裸的 ada.platform 比较
import re
bad = re.findall(r'if\s+ada\.platform\s*[!=]=', main_src)
check("没有裸 ada.platform 比较", not bad, str(bad))
bad2 = re.findall(r'if\s+ada\.name\s*[!=]=', main_src)
check("没有裸 ada.name 比较", not bad2, str(bad2))


print("\n=== 44. 所有测试用真实适配器（不再自造类） ===")

test_files = [
    "scenario_check.py", "edge_check.py", "avatar_check.py",
    "concurrency_check.py", "multi_adapter_check.py", "robustness_check.py",
    "live_check.py", "live_check2.py", "test_e2e.py",
    "test_regressions.py", "test_regressions2.py",
]
for fn in test_files:
    p = PLUGIN_ROOT / "tests" / fn
    if not p.exists():
        continue
    src_p = p.read_text(encoding="utf-8")
    # 不应再定义 class FakeAdapter: / class FakeBot:（真实适配器工厂替代）
    # 注意用 "class FakeAdapter:" 精确匹配，避免误伤 FakeAdapterMgr
    import re as _re
    has_fake = bool(_re.search(r"class FakeAdapter\s*:", src_p)) or \
        bool(_re.search(r"class FakeBot\s*:", src_p))
    check(f"{fn} 不再自造 FakeAdapter", not has_fake,
          "应使用 tests.real_adapter.make_real_adapter")


print("\n" + "=" * 60)
print(f"第十轮审计: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部通过 ===")