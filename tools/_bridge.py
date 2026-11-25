"""tools → qq_core 的导入桥。

为什么需要（2026-11-25 实机）：
    插件在真实环境里由框架以 `plugins.<插件目录>` 这个包名加载，
    `tools/qq_profile.py` 的模块名是 `plugins.<插件目录>.tools.qq_profile`，
    **`qq_core` 不是顶层包** ⇒ `from qq_core.avatar_view import ...` 直接 ImportError，
    实机表现：`头像：https://...（未识别：框架缺少 avatar_view 模块）`。

    测试/独立脚本里因为把插件根目录加进了 sys.path，顶层 `qq_core` 可导入，
    所以这个坑在测试里复现不出来（必须在"插件目录不在 sys.path"的环境跑）。

这里用 importlib 逐级尝试，兼容三种加载方式：
    1. 真实安装：`plugins.<目录>.tools`        → `plugins.<目录>.qq_core.<sub>`
    2. 包内加载：`<插件包>.tools`              → `<插件包>.qq_core.<sub>`
    3. 测试/脚本：插件根目录在 sys.path 上     → `qq_core.<sub>`
"""

from __future__ import annotations

import importlib
import sys
from typing import Optional


def load(submodule: str) -> Optional[object]:
    """加载插件内的 `qq_core.<submodule>` 模块。

    Args:
        submodule: 子模块名（如 "avatar_view" / "fmt"），不带 `.py`。

    Returns:
        模块对象；所有路径都失败时返回 None（调用方负责降级）。
    """
    submodule = str(submodule or "").strip()
    if not submodule:
        return None

    names: list[str] = []

    # 1) 从当前包逐级向上拼 ……qq_core.<sub>
    pkg = __package__ or ""
    parts = [p for p in pkg.split(".") if p]
    for i in range(len(parts), 0, -1):
        names.append(".".join(parts[:i] + ["qq_core", submodule]))

    # 2) 顶层（测试环境）
    names.append(f"qq_core.{submodule}")

    # 3) 已经被加载过的 qq_core 包（最可靠，避免重复导入出两个模块实例）
    for name in list(sys.modules):
        if name == "qq_core" or name.endswith(".qq_core"):
            names.append(f"{name}.{submodule}")

    for name in dict.fromkeys(names):
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None
