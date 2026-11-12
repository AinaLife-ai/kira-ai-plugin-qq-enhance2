"""第十二轮审计回归测试（Bug 45-46）。

覆盖：
45. 静默时段在"直接 trigger_*"路径不生效（绕过 should_wake_now）
46. 配置项是否真的生效（配置矩阵的静态复核）
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tests.real_adapter import ensure_framework_path
ensure_framework_path()

PLUGIN_ROOT = ROOT

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


def _main():
    print("\n=== 45. 静默时段必须在所有唤醒路径生效 ===")

    src = (PLUGIN_ROOT / "qq_core" / "wake_scheduler.py").read_text(encoding="utf-8")

    # _trigger_text 是唯一收口，静默检查必须在这里
    trigger_block_start = src.find("async def _trigger_text")
    trigger_block = src[trigger_block_start:trigger_block_start + 2000]
    check("_trigger_text 内有静默时段检查",
          "_in_quiet_hours(time.time())" in trigger_block,
          "静默应在统一收口处检查")
    check("静默检查在配额检查之前",
          trigger_block.find("_in_quiet_hours") < trigger_block.find("_check_hourly_limit"),
          "顺序：静默 → 配额 → 屏蔽")

    # 所有 trigger_* 方法都走 _trigger_text（不直接 publish）
    import re
    trigger_methods = re.findall(r"async def (trigger_\w+)\(self.*?\n(.*?)(?=\n    async def |\n    def |\Z)",
                                 src, re.S)
    check(f"找到 {len(trigger_methods)} 个 trigger_* 方法", len(trigger_methods) >= 4,
          str([m[0] for m in trigger_methods]))
    for name, body in trigger_methods:
        check(f"{name} 走 _trigger_text 收口",
              "_trigger_text" in body, "应统一收口而非直接 publish")
        check(f"{name} 不直接调 event_bus.publish",
              "event_bus.publish" not in body, "应通过 _trigger_text")


    print("\n=== 46. 配置项全部被读取（静态复核） ===")

    import json
    schema = json.loads((PLUGIN_ROOT / "schema.json").read_text(encoding="utf-8"))
    main_src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")

    missing = []
    for key in schema:
        if f'"{key}"' not in main_src:
            missing.append(key)
    check(f"S{schema.__len__()} 个配置项全部在 main.py 中被读取",
          not missing, str(missing))

    # 每个配置都要有 hint
    no_hint = [k for k, v in schema.items()
               if not isinstance(v, dict) or not v.get("hint")]
    check("所有配置项都有 hint", not no_hint, str(no_hint))

    # 每个配置都要有 default
    no_default = [k for k, v in schema.items()
                  if not isinstance(v, dict) or "default" not in v]
    check("所有配置项都有 default", not no_default, str(no_default))


    print("\n=== 47. 配置读取必须用安全转换 ===")

    # 数值型配置必须走 _safe_int/_safe_float
    numeric_keys = [k for k, v in schema.items()
                    if isinstance(v, dict) and v.get("type") in ("int", "float")]
    print(f"  数值型配置 {len(numeric_keys)} 个: {numeric_keys}")
    import re as _re
    unsafe = []
    for k in numeric_keys:
        # 允许换行/空白：_safe_int(\n    self.plugin_cfg.get("k")
        pat_safe = _re.compile(
            r'_safe_(?:int|float)\(\s*self\.plugin_cfg\.get\(\s*["\']' + _re.escape(k) + r'["\']'
        )
        pat_raw = _re.compile(
            r'(?<!_safe_)\b(?:int|float)\(\s*self\.plugin_cfg\.get\(\s*["\']' + _re.escape(k) + r'["\']'
        )
        if pat_raw.search(main_src):
            unsafe.append(f"{k}(裸转换)")
        elif not pat_safe.search(main_src):
            unsafe.append(f"{k}(未找到读取)")
    check(f"{len(numeric_keys)} 个数值配置都用安全转换",
          not unsafe, str(unsafe))


    print("\n" + "=" * 60)
    print(f"第十二轮审计: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for name, detail in FAIL:
            print(f"  FAIL {name}: {detail}")
        sys.exit(1)
    print("=== 全部通过 ===")


if __name__ == "__main__":
    _main()
