"""真实 API 审计：核对插件对框架对象的所有调用是否真实存在。

背景：`AdapterManager.get_all_adapters` 不存在，但测试 mock 自己造了它，
导致实机安装直接报错。本脚本用 AST 提取插件代码里的真实调用，
再对照真实框架类反射验证。

运行：python3 tests/real_api_check.py
"""

import ast
import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai_latest")
if not (KIRAAI / "core").is_dir():
    KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
sys.path.insert(0, str(KIRAAI))
sys.path.insert(0, str(ROOT))

# 框架日志路径相对于工作目录（data/log.log），确保存在
try:
    (Path.cwd() / "data").mkdir(exist_ok=True)
except OSError:
    pass

FAIL, WARN, OK = [], [], []


def fail(msg):
    FAIL.append(msg)
    print(f"  FAIL {msg}")


def warn(msg):
    WARN.append(msg)
    print(f"  WARN {msg}")


def ok(msg):
    OK.append(msg)


print(f"框架: {KIRAAI.name}")


# ===== 收集真实类的成员（含实例属性） =====
def collect_members(cls) -> dict:
    """收集类的可调用成员 + 注解字段 + __init__ 里赋值的实例属性。"""
    methods = {n for n, _ in inspect.getmembers(cls, callable)}
    attrs = set(dir(cls))
    try:
        attrs |= set(getattr(cls, "__annotations__", {}) or {})
    except Exception:
        pass
    try:
        src = inspect.getsource(cls.__init__)
        # 支持 self.x = ... 与 self.x: Type = ... 两种形式
        for m in re.finditer(r"self\.([A-Za-z_]\w*)\s*(?::[^=\n]+)?=", src):
            attrs.add(m.group(1))
    except Exception:
        pass
    return {"methods": methods, "attrs": attrs}


print("\n=== 1. 收集真实框架 API ===")
REAL = {}

for name, importer in [
    ("AdapterManager", lambda: __import__(
        "core.adapter.adapter_registry", fromlist=["AdapterManager"]).AdapterManager),
    ("PluginContext", lambda: __import__(
        "core.plugin.plugin_context", fromlist=["PluginContext"]).PluginContext),
    ("QQAdapter", lambda: __import__(
        "core.adapter.src.qq.qq", fromlist=["QQAdapter"]).QQAdapter),
    ("NapCatClient", lambda: __import__(
        "core.adapter.src.qq.napcat_client.client",
        fromlist=["NapCatWebSocketClient"]).NapCatWebSocketClient),
    ("LLMRequest", lambda: __import__(
        "core.provider.llm_model", fromlist=["LLMRequest"]).LLMRequest),
    ("Session", lambda: __import__(
        "core.chat.session", fromlist=["Session"]).Session),
    ("KiraMessageEvent", lambda: __import__(
        "core.chat.message_utils", fromlist=["KiraMessageEvent"]).KiraMessageEvent),
    ("KiraIMMessage", lambda: __import__(
        "core.chat.message_utils", fromlist=["KiraIMMessage"]).KiraIMMessage),
    ("BaseTool", lambda: __import__(
        "core.utils.tool_utils", fromlist=["BaseTool"]).BaseTool),
    ("ToolSet", lambda: __import__(
        "core.agent.tool", fromlist=["ToolSet"]).ToolSet),
    ("AdapterInfo", lambda: __import__(
        "core.adapter.adapter_info", fromlist=["AdapterInfo"]).AdapterInfo),
]:
    try:
        cls = importer()
        REAL[name] = collect_members(cls)
        print(f"  {name}: {len(REAL[name]['methods'])} 方法 / "
              f"{len(REAL[name]['attrs'])} 属性")
    except Exception as e:
        fail(f"无法导入 {name}: {type(e).__name__}: {e}")


# ===== AST 提取插件里的调用 =====
print("\n=== 2. 提取插件代码的框架调用 ===")

# 对象名 → 真实类（覆盖插件里最常见的引用名）
OBJ_MAP = {
    "adapter_mgr": "AdapterManager",
    "self.ctx.adapter_mgr": "AdapterManager",
    "ctx.adapter_mgr": "AdapterManager",
    "self.ctx": "PluginContext",
    "ctx": "PluginContext",
    "self.adapter": "QQAdapter",
    "adapter": "QQAdapter",
    "self.adapter.bot": "NapCatClient",
    "adapter.bot": "NapCatClient",
    "req": "LLMRequest",
    "event.session": "Session",
    "event.adapter": "AdapterInfo",
    "self.tool_set": "ToolSet",
}

src_files = sorted(
    [ROOT / "main.py"]
    + list((ROOT / "qq_core").glob("*.py"))
    + list((ROOT / "tools").glob("*.py"))
)

# (obj_expr, member) → [位置]
calls = {}


def expr_of(node) -> str:
    """把 Attribute/Name 链还原成 'a.b.c' 形式。"""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


for f in src_files:
    try:
        tree = ast.parse(f.read_text(encoding="utf-8"))
    except SyntaxError as e:
        fail(f"{f.name} 语法错误: {e}")
        continue
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        obj = expr_of(node.value)
        if not obj:
            continue
        # 只关心我们映射过的对象（或其父对象）
        hit = None
        # 最长 key 优先（避免 "adapter" 抢走 "event.adapter"）
        for key in sorted(OBJ_MAP, key=len, reverse=True):
            if obj == key or obj.endswith("." + key):
                hit = key
                break
        if hit is None and obj in OBJ_MAP:
            hit = obj
        if hit is None:
            continue
        calls.setdefault((hit, node.attr), []).append(f"{f.name}:{node.lineno}")

print(f"  提取到 {len(calls)} 个 (对象, 成员) 组合")

# ===== 逐个核对 =====
print("\n=== 3. 核对每个调用 ===")
unknown_obj = set()
for (obj, member), locs in sorted(calls.items()):
    cls_name = OBJ_MAP.get(obj)
    if not cls_name or cls_name not in REAL:
        unknown_obj.add((obj, cls_name))
        continue
    api = REAL[cls_name]
    if member in api["methods"] or member in api["attrs"]:
        ok(f"{obj}.{member}")
    else:
        fail(f"{obj}.{member} 不在 {cls_name} 上  ← {locs[0]}")

if unknown_obj:
    for obj, cls in sorted(unknown_obj):
        warn(f"无法核对 {obj}（映射到 {cls}，但该类未导入）")


# ===== 专项：已知易错 API =====
print("\n=== 4. 已知易错 API 专项 ===")

# 4.1 AdapterManager 必须有的/不能有的
if "AdapterManager" in REAL:
    am = REAL["AdapterManager"]
    for m in ("get_adapters", "get_adapter", "get_adapters_info"):
        (ok if m in am["methods"] else fail)(f"AdapterManager.{m}")

    # 代码里不能有 .get_all_adapters 的 Attribute 访问（AST 已排除 docstring）
    for f in src_files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "get_all_adapters":
                fail(f"{f.name}:{node.lineno} 直接访问 .get_all_adapters")
    # 字符串 fallback 是安全的
    for f in src_files:
        txt = f.read_text(encoding="utf-8")
        if 'getattr(mgr, "get_all_adapters"' in txt:
            ok(f"{f.name} get_all_adapters 仅作 fallback（安全）")

# 4.2 PluginContext 我们实际用到的成员
if "PluginContext" in REAL:
    pc = REAL["PluginContext"]
    for attr in ("adapter_mgr", "config", "event_bus", "provider_mgr",
                 "tool_mgr", "plugin_mgr", "session_mgr", "get_plugin_data_dir"):
        (ok if (attr in pc["attrs"] or attr in pc["methods"]) else fail)(
            f"PluginContext.{attr}"
        )

# 4.3 NapCatClient 我们用到的方法
if "NapCatClient" in REAL:
    nc = REAL["NapCatClient"]
    for m in ("send_action", "get_msg", "get_user_info", "get_group_info",
              "login_success_event"):
        (ok if (m in nc["methods"] or m in nc["attrs"]) else fail)(f"NapCatClient.{m}")

# 4.4 Session 属性
if "Session" in REAL:
    se = REAL["Session"]
    for a in ("sid", "adapter_name", "session_type", "session_id"):
        (ok if a in se["attrs"] or a in se["methods"] else fail)(f"Session.{a}")

# 4.5 QQAdapter 有 bot（BaseAdapter 没有，但我们只用 QQ）
if "QQAdapter" in REAL:
    qa = REAL["QQAdapter"]
    (ok if "bot" in qa["attrs"] else fail)("QQAdapter.bot")

# 4.6 LLMRequest 字段
if "LLMRequest" in REAL:
    lr = REAL["LLMRequest"]
    for a in ("user_prompt", "system_prompt", "messages", "tool_set",
              "assemble_prompt"):
        (ok if (a in lr["attrs"] or a in lr["methods"]) else fail)(f"LLMRequest.{a}")


print("\n" + "=" * 60)
print(f"真实 API 审计: {len(OK)} 通过, {len(FAIL)} 失败, {len(WARN)} 警告")
if FAIL:
    print("\n失败项:")
    for f in FAIL:
        print(f"  ✗ {f}")
sys.exit(1 if FAIL else 0)