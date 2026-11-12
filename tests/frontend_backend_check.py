"""前后端一致性审计：schema.json ↔ main.py ↔ README。

WebUI 读 schema.json 生成配置界面，插件代码读 plugin_cfg，
README 告诉用户每个开关干什么 —— 三者必须一一对应，否则会出现
「界面有开关但代码不读」「代码有配置但界面没有」这类隐性 bug。

检查项：
1. schema 的每个 key 都被 main.py 读取
2. main.py 读取的每个 key 都在 schema 里（否则界面上改不了）
3. 代码里的默认值与 schema 的 default 一致
4. schema 的每个 key 都写进了 README 的配置表
5. 数值型配置在代码里都走 safe_int / safe_float（防手改字符串崩）
6. 每个配置项都在 config_matrix.py 里有行为验证（或显式列在豁免表）

运行：
    cd <插件根目录> && python3 tests/frontend_backend_check.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCHEMA = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")

# 不是"插件配置"的键（schema 里没有，也不该有）
NON_CFG = set()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


def parse_code_cfg():
    """用 AST 从 main.py 提取 {key: (default, is_safe)}。

    必须用 AST：配置读取有两种写法
        self.x = self.plugin_cfg.get("k", 1)
        self.x = _safe_int(self.plugin_cfg.get("k"), 20)   # 默认值在 safe_int 第 2 参
    正则容易漏掉第二种（正是本次审计发现的问题）。
    """
    import ast

    def const(node):
        try:
            return ast.literal_eval(node)
        except Exception:
            return "<expr>"

    def unwrap(node):
        """剥掉 `... or ""` 这类 BoolOp，取左值。"""
        while isinstance(node, ast.BoolOp) and node.values:
            node = node.values[0]
        return node

    tree = ast.parse(MAIN_SRC)
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == "self"):
            continue
        val = unwrap(node.value)
        key = default = None
        is_safe = False
        if isinstance(val, ast.Call) and getattr(val.func, "id", "") in ("_safe_int", "_safe_float"):
            is_safe = True
            inner = unwrap(val.args[0]) if val.args else None
            if len(val.args) >= 2:
                default = const(val.args[1])
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "get" and inner.args):
                key = const(inner.args[0])
                if len(inner.args) >= 2:
                    default = const(inner.args[1])
        elif (isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute)
              and val.func.attr == "get" and isinstance(val.func.value, ast.Attribute)
              and val.func.value.attr == "plugin_cfg" and val.args):
            key = const(val.args[0])
            if len(val.args) >= 2:
                default = const(val.args[1])
        if key:
            out[key] = (default, is_safe)
    return out


def norm(v):
    """把代码里的字面量归一化成 Python 值（AST 已给出真实类型时直接返回）。"""
    if not isinstance(v, str):
        return v
    v = v.strip()
    if v in ("True", "true"):
        return True
    if v in ("False", "false"):
        return False
    if v in ("{}",):
        return {}
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    if v.startswith("'") and v.endswith("'"):
        return v[1:-1]
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


print("\n=== 1. schema ↔ 代码 ===")
code_cfg = parse_code_cfg()
schema_keys = set(SCHEMA)
code_keys = set(code_cfg)

missing_in_code = sorted(schema_keys - code_keys - NON_CFG)
check("schema 的每一项都被代码读取", not missing_in_code, str(missing_in_code))

extra_in_code = sorted(code_keys - schema_keys)
check("代码读取的键都在 schema 里", not extra_in_code, str(extra_in_code))

print("\n=== 2. 默认值一致 ===")
mismatch = []
for k, meta in SCHEMA.items():
    if k not in code_cfg:
        continue
    code_default, _ = code_cfg[k]
    schema_default = meta.get("default")
    a, b = norm(code_default), schema_default
    if isinstance(b, bool):
        same = (a is b)
    elif isinstance(b, (int, float)) and not isinstance(b, bool):
        same = (a == b)
    else:
        same = (str(a) == str(b))
    if not same:
        mismatch.append(f"{k}: 代码={code_default} schema={schema_default}")
check("所有默认值一致", not mismatch, "; ".join(mismatch))

print("\n=== 3. 数值配置走安全转换 ===")
unsafe = []
for k, meta in SCHEMA.items():
    if meta.get("type") not in ("int",):
        continue
    code_default, is_safe = code_cfg.get(k, ("", False))
    if not is_safe:
        unsafe.append(k)
check("int 配置全部 safe_int/safe_float", not unsafe, str(unsafe))

print("\n=== 4. README 覆盖 ===")
readme_missing = [k for k in SCHEMA if f"`{k}`" not in README]
check("README 配置表覆盖每个键", not readme_missing, str(readme_missing))

print("\n=== 5. 配置项都有行为验证 ===")
# 每个 key 至少要出现在某个测试脚本里（config_matrix 或专项 test_*.py）
all_tests = ""
for f in sorted((ROOT / "tests").glob("*.py")):
    all_tests += f.read_text(encoding="utf-8", errors="ignore")
not_covered = [k for k in SCHEMA if k not in all_tests]
check("每个配置项都有测试覆盖", not not_covered, str(not_covered))

print("\n=== 6. 工具参数 ↔ 实现 ===")
for tool_file, cls in (("tools/qq_profile.py", "QQProfileTool"),
                       ("tools/qq_group_profile.py", "QQGroupProfileTool")):
    src = (ROOT / tool_file).read_text(encoding="utf-8")
    # parameters 里声明的属性
    m = re.search(r'"properties":\s*\{(.*?)\n        \}', src, re.S)
    declared = set(re.findall(r'"(\w+)":\s*\{', m.group(1))) if m else set()
    # execute 签名里的 kwargs
    sig = re.search(r"async def execute\((.*?)\) -> str:", src, re.S)
    params = set(re.findall(r"(\w+):\s*[\w\[\] |\"]+\s*=", sig.group(1))) if sig else set()
    undeclared = sorted(params - declared)
    unimplemented = sorted(declared - params)
    check(f"{cls}: 参数全部有实现", not undeclared, str(undeclared))
    check(f"{cls}: 声明的参数都有默认值入口", not unimplemented, str(unimplemented))

print("\n" + "=" * 60)
print(f"前后端一致性: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for n, d in FAIL:
        print(f"  ✗ {n}: {d}")
    sys.exit(1)
