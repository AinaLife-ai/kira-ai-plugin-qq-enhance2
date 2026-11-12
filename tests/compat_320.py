"""版本兼容性对比：KiraAI v3.1.2 → v3.2.0-beta.1

自动对比我们插件依赖的所有框架 API 签名，找出破坏性变化。
"""

import ast
import re
import sys
from pathlib import Path

OLD = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
NEW = Path("/var/minis/workspace/qq_enhance2_design/kiraai_latest")

PASS, FAIL, WARN = [], [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


def warn(name, detail=""):
    WARN.append((name, detail))
    print(f"  WARN {name}  {detail}")


def get_class_fields(path: Path, cls_name: str) -> set:
    if not path.exists():
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    fields = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == cls_name:
            for f in n.body:
                if isinstance(f, ast.AnnAssign) and isinstance(f.target, ast.Name):
                    fields.add(f.target.id)
    return fields


def get_class_methods(path: Path, cls_name: str) -> set:
    if not path.exists():
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == cls_name:
            for f in n.body:
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(f.name)
    return names


print("\n=== 1. 核心数据结构 ===")

for cls, rel_path in [
    ("KiraIMMessage", "core/chat/message_utils.py"),
    ("KiraMessageEvent", "core/chat/message_utils.py"),
    ("KiraMessageBatchEvent", "core/chat/message_utils.py"),
    ("AdapterInfo", "core/adapter/adapter_info.py"),
]:
    old_f = get_class_fields(OLD / rel_path, cls)
    new_f = get_class_fields(NEW / rel_path, cls)
    if not old_f and not new_f:
        warn(f"{cls} 字段提取失败")
        continue
    removed = old_f - new_f
    added = new_f - old_f
    check(f"{cls} 无字段删除", not removed, f"删除: {removed}")
    if added:
        print(f"      新增字段（向后兼容）: {added}")

print("\n=== 2. LLMRequest / Prompt ===")

old_lr = get_class_fields(OLD / "core/provider/llm_model.py", "LLMRequest")
new_lr = get_class_fields(NEW / "core/provider/llm_model.py", "LLMRequest")
check("LLMRequest 无字段删除", not (old_lr - new_lr), f"删除: {old_lr - new_lr}")
if new_lr - old_lr:
    print(f"      新增: {new_lr - old_lr}")

old_src = (OLD / "core/provider/llm_model.py").read_text(encoding="utf-8")
new_src = (NEW / "core/provider/llm_model.py").read_text(encoding="utf-8")
old_sig = re.search(r"def assemble_prompt\(([^)]*)\)", old_src)
new_sig = re.search(r"def assemble_prompt\(([^)]*)\)", new_src)
check("assemble_prompt 存在", bool(new_sig))
if new_sig:
    check("assemble_prompt 有 dynamic_position 参数",
          "dynamic_position" in new_sig.group(1), new_sig.group(1)[:80])

old_p = get_class_methods(OLD / "core/prompt_manager.py", "Prompt")
new_p = get_class_methods(NEW / "core/prompt_manager.py", "Prompt")
check("Prompt 无方法删除", not (old_p - new_p), f"删除: {old_p - new_p}")

new_dyn = re.search(r'DYNAMIC_PROMPT_NAMES\s*=\s*\(([^)]*)\)', new_src)
check("DYNAMIC_PROMPT_NAMES 仍是 sessions/chat_env/time",
      bool(new_dyn) and all(k in new_dyn.group(1) for k in ("sessions", "chat_env", "time")),
      new_dyn.group(1) if new_dyn else "未找到")

print("\n=== 3. PluginContext ===")

old_ctx = get_class_methods(OLD / "core/plugin/plugin_context.py", "PluginContext")
new_ctx = get_class_methods(NEW / "core/plugin/plugin_context.py", "PluginContext")
check("PluginContext 无方法删除", not (old_ctx - new_ctx), f"删除: {old_ctx - new_ctx}")
for need in ("get_plugin_data_dir", "get_plugin_inst", "get_buffer"):
    check(f"PluginContext.{need} 仍存在", need in new_ctx)

print("\n=== 4. BaseTool / BasePlugin ===")

for cls, rel in [("BaseTool", "core/utils/tool_utils.py"),
                 ("BasePlugin", "core/plugin/base.py")]:
    old_m = get_class_methods(OLD / rel, cls)
    new_m = get_class_methods(NEW / rel, cls)
    check(f"{cls} 无方法删除", not (old_m - new_m), f"删除: {old_m - new_m}")

print("\n=== 5. hook 装饰器 ===")

new_deco = NEW / "core/plugin/decorators.py"
old_deco = OLD / "core/plugin/decorators.py"
if new_deco.exists():
    new_m = get_class_methods(new_deco, "OnEventDeco")
    old_m = get_class_methods(old_deco, "OnEventDeco")
    for h in ("im_message", "llm_request", "llm_response", "after_xml_parse",
              "custom_event", "message_sent"):
        check(f"hook {h} 仍存在", h in new_m, str(sorted(new_m))[:120])
    check("hook 无删除", not (old_m - new_m), f"删除: {old_m - new_m}")
else:
    warn("decorators.py 不存在")

new_prio = NEW / "core/plugin/handlers.py"
if new_prio.exists():
    src = new_prio.read_text(encoding="utf-8")
    check("Priority 枚举仍存在", "class Priority" in src)
    for p in ("SYS_LOW", "LOW", "MEDIUM", "HIGH", "SYS_HIGH"):
        check(f"Priority.{p} 仍存在", p in src)

print("\n=== 6. QQ 适配器 ===")

new_qq = NEW / "core/adapter/src/qq"
check("QQ 适配器目录存在", new_qq.is_dir())
if new_qq.is_dir():
    files = sorted(f.name for f in new_qq.iterdir() if f.is_file())
    print(f"  QQ 适配器文件: {files}")
    check("im.py 存在（capability 架构）", "im.py" in files, str(files))
    check("qq.py 存在", "qq.py" in files, str(files))
    im_path = new_qq / "im.py"
    im_src = im_path.read_text(encoding="utf-8") if im_path.exists() else ""
    check("_on_notice_message 仍存在", "_on_notice_message" in im_src)
    check("notice 事件仍传 supported_elements", "supported_elements" in im_src)
    check("notice 事件仍有 is_notice 标记", "is_notice=True" in im_src)
    # 关键：是不是仍然只给 poke-self 写文本
    # （注意：文本由 f-string + motion_text 变量拼接，不能搜字面"戳了戳你"）
    check("poke-self 文本逻辑仍存在",
          '[Poke 用户' in im_src and "motion_text" in im_src, "未找到 poke 文本构造")

print("\n=== 7. 事件流 stage ===")

new_stages = NEW / "core/workflow/src/im/stages"
if new_stages.is_dir():
    files = sorted(f.name for f in new_stages.iterdir() if f.name.endswith(".py"))
    print(f"  stages: {files}")
    for s in ("receive.py", "assemble.py", "request.py"):
        check(f"stage {s} 存在", s in files, str(files))
    recv_path = new_stages / "receive.py"
    if recv_path.exists():
        recv = recv_path.read_text(encoding="utf-8")
        check("空 chain 仍被丢弃", "chain.is_empty()" in recv)
        check("trigger 策略仍存在", 'process_strategy == "trigger"' in recv)
    asm_path = new_stages / "assemble.py"
    if asm_path.exists():
        asm = asm_path.read_text(encoding="utf-8")
        check("assemble 仍跑 ON_LLM_REQUEST", "ON_LLM_REQUEST" in asm)
else:
    warn("stages 目录不存在")

print("\n" + "=" * 60)
print(f"兼容性对比: {len(PASS)} 通过, {len(FAIL)} 失败, {len(WARN)} 警告")
for name, detail in FAIL:
    print(f"  FAIL {name}: {detail}")
for name, detail in WARN:
    print(f"  WARN {name}: {detail}")
sys.exit(1 if FAIL else 0)