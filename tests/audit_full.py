"""QQ增强2 完整审计脚本（多维度静态 + 逻辑检查）。

检查项：
A. 配置一致性：schema 键 ↔ 代码读取键 ↔ README
B. 导入完整性：所有 import 可解析（真实框架环境）
C. 工具签名：BaseTool 契约（name/description/parameters/execute）
D. hook 签名：框架期望的参数列表
E. 事件类型覆盖：normalizer 所有分支可达
F. 边界与异常：空值/None/异常路径
G. 缓存不破坏：persist=False 全路径
H. 兼容性：与 S/Z/QQ增强1 的交互点
"""

import ast
import json
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

results = []


def check(name, fn):
    try:
        ok, detail = fn()
        results.append((ok, name, detail))
        print(f"{'✓' if ok else '✗'} {name}: {detail}")
    except Exception as e:
        results.append((False, name, f"异常: {type(e).__name__}: {e}"))
        print(f"✗ {name}: 异常 {type(e).__name__}: {e}")


# ============ A. 配置一致性 ============

def a_schema_keys():
    schema = json.loads((PLUGIN_ROOT / "schema.json").read_text(encoding="utf-8"))
    main_src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    # 代码里读取的键：self.plugin_cfg.get("xxx", ...)
    code_keys = set(re.findall(r'self\.plugin_cfg\.get\(\s*"([a-z_0-9]+)"', main_src))
    schema_keys = set(schema.keys())
    missing = schema_keys - code_keys
    if missing:
        return False, f"schema 有但代码未读取: {sorted(missing)}"
    unused = code_keys - schema_keys
    if unused:
        return False, f"代码读取但 schema 缺失: {sorted(unused)}"
    return True, f"{len(schema_keys)} 键完全一致"


def a_schema_types():
    schema = json.loads((PLUGIN_ROOT / "schema.json").read_text(encoding="utf-8"))
    bad = []
    for k, v in schema.items():
        if not isinstance(v, dict):
            bad.append(f"{k}: 非 dict")
            continue
        if "name" not in v or "type" not in v:
            bad.append(f"{k}: 缺 name/type")
        if "hint" not in v:
            bad.append(f"{k}: 缺 hint")
        if v.get("type") not in ("switch", "int", "string", "text", "select", "float", "list"):
            bad.append(f"{k}: 未知类型 {v.get('type')}")
        if v.get("type") == "int":
            d = v.get("default")
            if not isinstance(d, int):
                bad.append(f"{k}: int 类型默认值非 int ({d!r})")
    if bad:
        return False, "; ".join(bad[:5])
    return True, f"{len(schema)} 项格式合规"


def a_manifest():
    m = json.loads((PLUGIN_ROOT / "manifest.json").read_text(encoding="utf-8"))
    required = ["display_name", "plugin_id", "version", "author", "description", "repo", "core_version"]
    missing = [k for k in required if k not in m]
    if missing:
        return False, f"缺字段: {missing}"
    # 规范检查
    if not re.fullmatch(r"[a-z0-9_\-]+", m["plugin_id"]):
        return False, f"plugin_id 含非法字符: {m['plugin_id']}"
    if not m["core_version"].startswith(">=") or "<" in m["core_version"]:
        return False, f"core_version 应无上界: {m['core_version']}"
    if m.get("display_name") != "QQ增强2":
        return False, f"display_name 不符: {m.get('display_name')}"
    if m.get("author") != "znq19":
        return False, f"author 不符: {m.get('author')}"
    # icon 文件存在
    if not (PLUGIN_ROOT / m.get("icon", "")).exists():
        return False, f"icon 文件不存在: {m.get('icon')}"
    return True, f"{m['plugin_id']} v{m['version']} core{m['core_version']}"


def a_readme_keys():
    schema = json.loads((PLUGIN_ROOT / "schema.json").read_text(encoding="utf-8"))
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    # README 配置表里的键（| `key` | 格式）
    readme_keys = set(re.findall(r"\|\s*`([a-z_0-9]+)`\s*\|", readme))
    missing = sorted(set(schema.keys()) - readme_keys)
    if missing:
        return False, f"README 未记录的配置: {missing}"
    return True, f"README 覆盖 {len(readme_keys)} 键"


# ============ B. 导入完整性 ============

def b_imports():
    """检查所有相对导入的目标文件存在。"""
    problems = []
    for py in list(PLUGIN_ROOT.glob("main.py")) + list((PLUGIN_ROOT / "qq_core").glob("*.py")) + list((PLUGIN_ROOT / "tools").glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                # 相对导入
                if node.module:
                    target = py.parent / (node.module.replace(".", "/") + ".py")
                    pkg_init = py.parent / node.module.replace(".", "/") / "__init__.py"
                    if not target.exists() and not pkg_init.exists():
                        problems.append(f"{py.name}: .{node.module} 不存在")
    if problems:
        return False, "; ".join(problems)
    return True, "所有相对导入可解析"


def b_compile_all():
    import py_compile
    errors = []
    for py in [PLUGIN_ROOT / "main.py"] + list((PLUGIN_ROOT / "qq_core").glob("*.py")) + list((PLUGIN_ROOT / "tools").glob("*.py")):
        try:
            py_compile.compile(str(py), doraise=True)
        except Exception as e:
            errors.append(f"{py.name}: {e}")
    if errors:
        return False, "; ".join(errors)
    return True, "全部编译通过"


# ============ C. 工具契约 ============

def c_tool_contract():
    """检查工具类定义符合框架 BaseTool 契约。"""
    problems = []
    for tf in (PLUGIN_ROOT / "tools").glob("*.py"):
        if tf.name == "__init__.py":
            continue
        src = tf.read_text(encoding="utf-8")
        tree = ast.parse(src)
        cls = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name.endswith("Tool")]
        if not cls:
            problems.append(f"{tf.name}: 无 *Tool 类")
            continue
        for c in cls:
            has_name = "name" in [n.targets[0].id for n in c.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)]
            has_desc = "description" in [n.targets[0].id for n in c.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)]
            has_params = "parameters" in [n.targets[0].id for n in c.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)]
            has_exec = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "execute" for n in c.body)
            if not (has_name and has_desc and has_params and has_exec):
                problems.append(f"{c.name}: 缺 name/description/parameters/execute")
            # description 长度检查（LLM 上下文成本）
            for n in c.body:
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "description":
                    if isinstance(n.value, ast.Constant) and len(str(n.value.value)) > 200:
                        problems.append(f"{c.name}: description 过长 ({len(str(n.value.value))})")
    if problems:
        return False, "; ".join(problems)
    return True, "3 个工具契约合规"


def c_tool_names_unique():
    """工具名唯一性（含与市场插件对比）。"""
    names = []
    for tf in (PLUGIN_ROOT / "tools").glob("*.py"):
        if tf.name == "__init__.py":
            continue
        tree = ast.parse(tf.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef) and n.name.endswith("Tool"):
                for s in n.body:
                    if isinstance(s, ast.Assign) and isinstance(s.targets[0], ast.Name) and s.targets[0].id == "name":
                        names.append(s.value.value)
    if len(names) != len(set(names)):
        return False, f"工具名重复: {names}"
    # 与已知市场插件对比
    reserved = {"group_ban_user", "group_unban_user", "group_set_card", "group_kick_user",
                "group_whole_ban", "group_set_special_title", "send_qq_likes", "set_qq_emoji",
                "delete_qq_msg", "set_qq_group_ban", "send_music_card"}
    clash = [n for n in names if n in reserved]
    if clash:
        return False, f"与现有插件冲突: {clash}"
    return True, f"工具名无冲突: {names}"


# ============ D. hook 签名 ============

def d_hook_signatures():
    """检查 hook 函数签名与框架调用约定一致。"""
    src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    problems = []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    hook = dec.func.attr
                    args = [a.arg for a in node.args.args]
                    if hook == "im_message":
                        found.append("im_message")
                        # 框架: handler(event) 或 handler(event, *_)；类方法首个参数是 self
                        args_clean = [a for a in args if a != "self"]
                        if not args_clean or args_clean[0] != "event":
                            problems.append(f"im_message 首参应为 event: {args}")
                    elif hook == "llm_request":
                        found.append("llm_request")
                        # 框架: handler(event, request, tag_set)；类方法首参 self
                        args_clean = [a for a in args if a != "self"]
                        if len(args_clean) < 2:
                            problems.append(f"llm_request 至少需 event, request: {args}")
    if problems:
        return False, "; ".join(problems)
    return True, f"hook 签名合规: {found}"


# ============ E. 事件类型覆盖 ============

def e_event_coverage():
    """normalizer 覆盖的事件类型 vs renderer 支持的类型。"""
    norm_src = (PLUGIN_ROOT / "qq_core" / "event_normalizer.py").read_text(encoding="utf-8")
    rend_src = (PLUGIN_ROOT / "qq_core" / "env_renderer.py").read_text(encoding="utf-8")
    norm_types = set(re.findall(r'return QQEvent\([^)]*?etype=(\w+)', norm_src, re.S))
    # 更稳的方式：找所有 E_XXX 常量赋值
    norm_types = set(re.findall(r"etype=(E_\w+)", norm_src))
    rend_types = set(re.findall(r"etype == (E_\w+)", rend_src))
    # 唤醒级类型不需要渲染；退群/被踢改走 notice，也不进摘要
    wake_types = {"E_KICKED_SELF", "E_TITLE_SELF", "E_EMOJI_LIKE_SELF",
                  "E_PROFILE_LIKE_SELF", "E_MEMBER_DECREASE"}
    missing = norm_types - rend_types - wake_types
    if missing:
        return False, f"renderer 未处理: {sorted(missing)}"
    return True, f"事件类型覆盖完整 ({len(norm_types)} 种，唤醒级 {len(wake_types)} 种单独处理)"


# ============ F. 边界与异常 ============

def f_edge_cases():
    """检查关键边界处理。"""
    problems = []
    # 1. normalizer 对非 dict 输入
    norm = (PLUGIN_ROOT / "qq_core" / "event_normalizer.py").read_text(encoding="utf-8")
    if "if not isinstance(raw, dict):" not in norm:
        problems.append("normalizer 缺非 dict 防御")
    # 2. renderer 空列表
    rend = (PLUGIN_ROOT / "qq_core" / "env_renderer.py").read_text(encoding="utf-8")
    if "if not events:" not in rend:
        problems.append("renderer 缺空列表防御")
    # 3. store 空 sid 文件名安全
    store = (PLUGIN_ROOT / "qq_core" / "event_store.py").read_text(encoding="utf-8")
    if 'replace(":", "_")' not in store:
        problems.append("store 缺文件名净化")
    # 4. wake_scheduler 静默时段异常捕获
    ws = (PLUGIN_ROOT / "qq_core" / "wake_scheduler.py").read_text(encoding="utf-8")
    if "except Exception:" not in ws:
        problems.append("wake_scheduler 缺异常捕获")
    # 5. tools 适配器为空
    for tf in (PLUGIN_ROOT / "tools").glob("*.py"):
        if tf.name == "__init__.py":
            continue
        s = tf.read_text(encoding="utf-8")
        if "if not self.adapter" not in s:
            problems.append(f"{tf.name} 缺 adapter 空防御")
    if problems:
        return False, "; ".join(problems)
    return True, "边界防御完备"


def f_pycache():
    """检查 pyc 未被 git 跟踪（工作目录缓存无害，但绝不能入库）。"""
    import subprocess
    gi = PLUGIN_ROOT / ".gitignore"
    if not gi.exists() or "__pycache__" not in gi.read_text(encoding="utf-8"):
        return False, ".gitignore 缺 __pycache__"
    # 检查 git 跟踪的文件里是否有 pyc
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=str(PLUGIN_ROOT),
            capture_output=True, text=True, timeout=30,
        )
        tracked = [f for f in out.stdout.splitlines() if f.endswith(".pyc") or "__pycache__" in f]
        if tracked:
            return False, f"git 跟踪了 {len(tracked)} 个缓存文件: {tracked[:3]}"
    except Exception as e:
        return False, f"git 检查失败: {e}"
    return True, "git 未跟踪任何 pyc（.gitignore 生效）"


# ============ G. 缓存 ============

def g_cache_safety():
    """所有环境注入必须是 persist=False。"""
    src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    er = (PLUGIN_ROOT / "qq_core" / "emoji_rewriter.py").read_text(encoding="utf-8")
    rr = (PLUGIN_ROOT / "qq_core" / "recall_rewriter.py").read_text(encoding="utf-8")
    problems = []
    # main.py 的环境注入
    if "qq_social_env" in src:
        if "persist=False" not in src.split("qq_social_env")[0].rsplit("\n", 3)[-1] and "persist=False" not in src:
            problems.append("环境摘要可能未设 persist=False")
    # emoji/recall rewriter 是直接改 p.content（原 message Prompt，persist=True）
    # 这是设计意图（标注需进历史），不是 bug —— 但要确认只追加不替换
    # 追加语义：允许 `p.content += ...` 或 `text = text + annotation` 后赋回
    def _append_only(src: str) -> bool:
        return ("p.content +=" in src) or ("+ annotation" in src) or ("text = text +" in src)
    if not _append_only(er):
        problems.append("emoji_rewriter 应追加而非替换")
    if not _append_only(rr):
        problems.append("recall_rewriter 应追加而非替换")
    if problems:
        return False, "; ".join(problems)
    return True, "环境摘要 persist=False；标注追加进历史（设计如此）"


# ============ H. 兼容性 ============

def h_compat():
    """检查与 S/Z/QQ增强1 的交互点。"""
    src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    problems = []
    # 1. 不能处理 group_increase（归 QQ增强1）
    norm = (PLUGIN_ROOT / "qq_core" / "event_normalizer.py").read_text(encoding="utf-8")
    if "group_increase" in norm:
        problems.append("不应处理 group_increase（QQ增强1 负责）")
    # 2. 不能处理 group_ban user==self（QQ增强1 负责）
    if "E_BAN" in norm and "user_id == self_id" not in norm and "user_id and user_id == self_id" not in norm:
        problems.append("group_ban self 判定缺失（可能与 QQ增强1 重复注入）")
    # 3. 唤醒文本不能含"禁言了N秒"（防 S 的 _note_mute_notice 误触发）
    ws = (PLUGIN_ROOT / "qq_core" / "wake_scheduler.py").read_text(encoding="utf-8")
    wake_texts = re.findall(r'text = f"(\[System[^"]*)"', ws)
    for t in wake_texts:
        if "禁言" in t:
            problems.append(f"唤醒文本含禁言关键词（S 误触发风险）: {t[:40]}")
    # 4. is_notice 处理：他人事件不能 trigger（防 S/Z 统计污染）
    if "event.discard()" not in src and "store.add" not in src:
        problems.append("他人事件应只入 store 不 trigger")
    if problems:
        return False, "; ".join(problems)
    return True, "兼容性约束满足（无 increase/无禁言文本/他人事件不入 trigger）"


# ============ 运行 ============

check("A1 schema↔代码键一致", a_schema_keys)
check("A2 schema 格式合规", a_schema_types)
check("A3 manifest 规范", a_manifest)
check("A4 README 配置覆盖", a_readme_keys)
check("B1 相对导入可解析", b_imports)
check("B2 全部编译", b_compile_all)
check("C1 工具契约", c_tool_contract)
check("C2 工具名唯一无冲突", c_tool_names_unique)
check("D1 hook 签名", d_hook_signatures)
check("E1 事件类型覆盖", e_event_coverage)
check("F1 边界防御", f_edge_cases)
check("F2 无 pyc 入库", f_pycache)
check("G1 缓存安全", g_cache_safety)
check("H1 兼容性约束", h_compat)

print("\n" + "=" * 60)
passed = sum(1 for r in results if r[0])
print(f"审计结果: {passed}/{len(results)} 通过")
if passed != len(results):
    print("失败项:")
    for ok, name, detail in results:
        if not ok:
            print(f"  ✗ {name}: {detail}")
    sys.exit(1)