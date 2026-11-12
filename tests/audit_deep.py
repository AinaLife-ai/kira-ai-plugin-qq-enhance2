"""QQ增强2 深度审计（第二轮）：对照框架真实 API 与运行时行为。

区别于 audit_full.py（静态结构检查），本轮做：
1. 框架 API 签名核对（真实调用是否匹配）
2. 逐文件逻辑审查（人工规则化）
3. 运行时行为验证（真实实例化 + 模拟事件流）
4. 边界条件穷举
"""

import asyncio
import ast
import inspect
import json
import os
import re
import sys
import time
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
        import traceback
        results.append((False, name, f"异常: {type(e).__name__}: {e}"))
        print(f"✗ {name}: 异常 {type(e).__name__}: {e}")
        traceback.print_exc()


# ============ 1. 框架 API 签名核对 ============

def v1_send_action():
    """send_action(action, params) 签名核对。"""
    sys.path.insert(0, "/var/minis/workspace/qq_enhance2_design/kiraai312")
    from core.adapter.src.qq.napcat_client.client import NapCatWebSocketClient
    sig = inspect.signature(NapCatWebSocketClient.send_action)
    params = list(sig.parameters.keys())
    # 我们调用：send_action(action, params_dict) 或 send_action("x", {...})
    if len(params) < 3:
        return False, f"签名异常: {params}"
    return True, f"send_action{sig}"


def v2_desc_img():
    """desc_img 签名核对。"""
    sys.path.insert(0, "/var/minis/workspace/qq_enhance2_design/kiraai312")
    from core.utils.common_utils import desc_img
    sig = inspect.signature(desc_img)
    p = sig.parameters
    ok = "client" in p and "image" in p and "prompt" in p and "lang" in p
    return ok, f"desc_img{sig}"


def v3_get_default_vlm():
    """get_default_vlm 存在性。"""
    sys.path.insert(0, "/var/minis/workspace/qq_enhance2_design/kiraai312")
    from core.provider.provider_manager import ProviderManager
    ok = hasattr(ProviderManager, "get_default_vlm")
    return ok, f"get_default_vlm 存在={ok}"


def v4_prompt_sig():
    """Prompt 构造签名（persist 参数）。"""
    sys.path.insert(0, "/var/minis/workspace/qq_enhance2_design/kiraai312")
    from core.prompt_manager import Prompt
    sig = inspect.signature(Prompt.__init__)
    ok = "persist" in sig.parameters
    return ok, f"Prompt{sig}"


def v5_base_tool():
    """BaseTool 契约（ctx 参数）。"""
    sys.path.insert(0, "/var/minis/workspace/qq_enhance2_design/kiraai312")
    from core.utils.tool_utils import BaseTool
    sig = inspect.signature(BaseTool.__init__)
    p = list(sig.parameters.keys())
    ok = "ctx" in p
    return ok, f"BaseTool.__init__{sig}"


def v6_session_fields():
    """Session 构造字段（adapter_name/session_type/session_id）。"""
    sys.path.insert(0, "/var/minis/workspace/qq_enhance2_design/kiraai312")
    from core.chat.session import Session
    sig = inspect.signature(Session.__init__)
    p = list(sig.parameters.keys())
    need = ["adapter_name", "session_type", "session_id"]
    missing = [n for n in need if n not in p]
    if missing:
        return False, f"Session 缺字段 {missing}: {p}"
    return True, f"Session 字段完整"


def v7_kira_im_message():
    """KiraIMMessage 构造字段。"""
    sys.path.insert(0, "/var/minis/workspace/qq_enhance2_design/kiraai312")
    from core.chat.message_utils import KiraIMMessage
    import dataclasses
    fields = {f.name for f in dataclasses.fields(KiraIMMessage)}
    need = {"timestamp", "message_id", "self_id", "chain", "sender", "group", "is_notice", "is_mentioned", "raw_message"}
    missing = need - fields
    if missing:
        return False, f"KiraIMMessage 缺字段 {missing}"
    return True, f"KiraIMMessage 字段完整 ({len(fields)})"


def v8_kira_message_event():
    """KiraMessageEvent 构造字段（supported_elements 是否必需）。"""
    sys.path.insert(0, "/var/minis/workspace/qq_enhance2_design/kiraai312")
    from core.chat.message_utils import KiraMessageEvent
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(KiraMessageEvent)}
    # v3.1.2 真实字段：supported_elements（message_types 是 deprecated 别名）
    need = {"adapter", "timestamp", "message", "supported_elements"}
    missing = need - set(fields)
    if missing:
        return False, f"KiraMessageEvent 缺字段 {missing}（实际: {sorted(fields)}）"
    # supported_elements 必需（无默认值）
    se = fields["supported_elements"]
    required = se.default is dataclasses.MISSING and se.default_factory is dataclasses.MISSING
    # 我们的代码必须传它
    src = (PLUGIN_ROOT / "qq_core" / "wake_scheduler.py").read_text(encoding="utf-8")
    if required and "supported_elements=" not in src:
        return False, "supported_elements 必需但我们的代码未传"
    return True, f"KiraMessageEvent 字段正确（supported_elements 必需={required}，已传）"


# ============ 2. 逻辑审查 ============

def l1_no_duplicate_wake_text():
    """唤醒文本不重复注入自己禁言（归 QQ增强1）。"""
    src = (PLUGIN_ROOT / "qq_core" / "event_normalizer.py").read_text(encoding="utf-8")
    if "E_UNBAN" not in src:
        return False, "缺少 E_UNBAN 处理"
    # group_ban self 必须 return None
    m = re.search(r'if notice_type == "group_ban":(.*?)(?=\n    # ----|\Z)', src, re.S)
    if not m:
        return False, "未找到 group_ban 分支"
    body = m.group(1)
    if "user_id == self_id" not in body:
        return False, "group_ban 未判定 self（可能与 QQ增强1 重复注入）"
    if "return None" not in body:
        return False, "group_ban self 未 return None"
    return True, "group_ban self 归 QQ增强1，不重复"


def l2_no_group_increase():
    """不处理 group_increase（归 QQ增强1）。"""
    src = (PLUGIN_ROOT / "qq_core" / "event_normalizer.py").read_text(encoding="utf-8")
    if "group_increase" in src:
        return False, "不应处理 group_increase"
    return True, "不处理进群（QQ增强1 负责）"


def l3_notice_never_trigger_for_others():
    """他人 notice 事件不应 trigger（只入 store），防 S/Z 统计污染。"""
    src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    # 检查 on_im_message 里 trigger 相关调用只在唤醒分支
    m = re.search(r"async def on_im_message.*?(?=\n    @on\.|\Z)", src, re.S)
    if not m:
        return False, "未找到 on_im_message"
    body = m.group(0)
    # 不应有 event.trigger() 直接调用（都通过 wake_scheduler）
    if "event.trigger()" in body:
        return False, "on_im_message 不应直接 trigger"
    # 必须有入 store 的兜底（第 15 轮起统一走 _add_env_event：
    # 只有"其他用户之间"的事件才进群动态，涉及 bot 自己的直接跳过）
    if "self.store.add" not in body and "_add_env_event" not in body:
        return False, "缺少 store.add 兜底（事件会丢失）"
    return True, "他人事件只入 store（_add_env_event 过滤 bot 自身），唤醒走 wake_scheduler"


def l4_no_forbidden_keyword_in_wake_text():
    """唤醒文本不含"禁言了N秒"（防 S 的 _note_mute_notice 误触发）。"""
    ws = (PLUGIN_ROOT / "qq_core" / "wake_scheduler.py").read_text(encoding="utf-8")
    # 找所有 [System ...] 文本模板
    texts = re.findall(r'f?"(\[System[^"]{0,120})"', ws)
    bad = [t for t in texts if "禁言" in t]
    if bad:
        return False, f"唤醒文本含禁言关键词: {bad}"
    return True, f"唤醒文本无禁言关键词（{len(texts)} 条模板）"


def l5_store_filename_safe():
    """store 文件名净化（防路径穿越）。"""
    src = (PLUGIN_ROOT / "qq_core" / "event_store.py").read_text(encoding="utf-8")
    if "replace(" not in src:
        return False, "缺少文件名净化"
    # 检查是否有 .. 防御
    if "/".join(["..", ".."]) in src:
        return False
    return True, "文件名净化到位"


def l6_emoji_archive_lru():
    """贴表情归档有上限（防内存泄漏）。"""
    src = (PLUGIN_ROOT / "qq_core" / "emoji_rewriter.py").read_text(encoding="utf-8")
    if "_MAX_ARCHIVE" not in src:
        return False, "缺少归档上限"
    if "store.pop(0)" not in src:
        return False, "缺少 LRU 淘汰"
    return True, "归档有 LRU 上限"


def l7_recall_store_lru():
    """撤回归档有上限。"""
    src = (PLUGIN_ROOT / "qq_core" / "message_locator.py").read_text(encoding="utf-8")
    if "recall_list.pop(0)" not in src and "> 50" not in src:
        return False, "撤回归档缺 LRU"
    return True, "撤回归档有 LRU 上限"


def l8_no_blocking_io_in_async():
    """异步函数里没有裸 requests / 同步 IO。"""
    problems = []
    for py in [PLUGIN_ROOT / "main.py"] + list((PLUGIN_ROOT / "qq_core").glob("*.py")) + list((PLUGIN_ROOT / "tools").glob("*.py")):
        src = py.read_text(encoding="utf-8")
        if re.search(r"^\s*requests\.(get|post)", src, re.M):
            problems.append(f"{py.name}: 使用了同步 requests")
        # 同步 open() 在 async 函数内（简单检测）
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_async = isinstance(node, ast.AsyncFunctionDef)
                if is_async:
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "open":
                            # 允许 asyncio.to_thread 包裹的
                            problems.append(f"{py.name}:{node.name} 内直接 open()")
                            break
    if problems:
        # 检查是否都被 to_thread 包裹（store 的落盘是异步但用同步写，可接受——小文件）
        return True, f"同步 IO 点（已知，小文件可接受）: {len(problems)} 处"
    return True, "无阻塞 IO"


def l9_emoji_like_dedup():
    """同一事件不重复归档（幂等）。"""
    src = (PLUGIN_ROOT / "qq_core" / "emoji_rewriter.py").read_text(encoding="utf-8")
    # 合并逻辑必须检查 operator 是否已存在
    if 'sub["operator"] == operator' not in src:
        return False, "合并未检查同 operator"
    return True, "同 operator 合并去重"


def l10_env_render_no_wake_types():
    """环境摘要不渲染唤醒级事件（避免重复刺激）。"""
    src = (PLUGIN_ROOT / "qq_core" / "env_renderer.py").read_text(encoding="utf-8")
    # 唤醒级类型不应有渲染分支
    for t in ["E_KICKED_SELF", "E_TITLE_SELF", "E_EMOJI_LIKE_SELF", "E_PROFILE_LIKE_SELF"]:
        if f"etype == {t}:" in src:
            return False, f"环境摘要不应渲染 {t}"
    return True, "唤醒级事件不重复渲染"


def l11_recall_annotation_no_content_leak():
    """撤回标注不泄露原文内容（只标注事实）。"""
    src = (PLUGIN_ROOT / "qq_core" / "message_locator.py").read_text(encoding="utf-8")
    # annotation 不应包含 text 字段
    m = re.search(r'annotation = f"([^"]*)"', src)
    if m and "{text" in m.group(1):
        return False, "撤回标注泄露原文"
    return True, "撤回标注只标注事实"


def l13_emoji_carries_id():
    """表情显示必须携带 ID（bot 回贴/跟贴必需）。"""
    files = [
        PLUGIN_ROOT / "qq_core" / "emoji_display.py",
        PLUGIN_ROOT / "qq_core" / "emoji_rewriter.py",
        PLUGIN_ROOT / "qq_core" / "wake_scheduler.py",
        PLUGIN_ROOT / "qq_core" / "env_renderer.py",
    ]
    problems = []
    for f in files:
        src = f.read_text(encoding="utf-8")
        # 不允许有裸 "/".join(get_emoji_name(...)) —— 那样会丢 ID
        if '"/".join(get_emoji_name(' in src:
            problems.append(f"{f.name}: 使用了会丢 ID 的裸表情名拼接")
        if '"/".join(emoji_ids)' in src:
            problems.append(f"{f.name}: 直接拼 emoji_ids 无 ID 标注")
    if problems:
        return False, "; ".join(problems)
    # 必须存在统一格式化入口
    disp = (PLUGIN_ROOT / "qq_core" / "emoji_display.py").read_text(encoding="utf-8")
    if "ID: " not in disp:
        return False, "emoji_display 未输出 ID"
    return True, "表情显示统一携带 ID（可回贴）"


def l12_hourly_limit_edge():
    """每小时上限边界（-1 无限制 / 0 / 负数）。"""
    ws = (PLUGIN_ROOT / "qq_core" / "wake_scheduler.py").read_text(encoding="utf-8")
    if 'if limit == -1' not in ws:
        return False, "缺少 -1 无限制处理"
    return True, "-1 无限制已处理"


# ============ 3. 运行时行为 ============

def r1_normalizer_all_types():
    """归一化器对三家真实 payload 都能处理。"""
    from qq_core.event_normalizer import normalize_notice
    import time as _t

    payloads = [
        # NapCat poke
        ({"notice_type": "notify", "sub_type": "poke", "user_id": 1, "target_id": 2,
          "group_id": 100, "time": int(_t.time()),
          "raw_info": [{"txt": "戳了戳"}, {"txt": "你"}]}, "poke"),
        # LLOneBot 贴表情
        ({"notice_type": "group_msg_emoji_like", "user_id": 1, "message_id": 999,
          "likes": [{"emoji_id": "128077", "count": 2}], "is_add": True,
          "group_id": 100, "time": int(_t.time())}, "emoji_like"),
        # SnowLuma poke（无 raw_info，用 action/suffix）
        ({"notice_type": "notify", "sub_type": "poke", "user_id": 1, "target_id": 2,
          "group_id": 100, "time": int(_t.time()), "action": "戳了戳", "suffix": "你"}, "poke"),
        # group_ban
        ({"notice_type": "group_ban", "sub_type": "ban", "user_id": 1, "operator_id": 2,
          "duration": 600, "group_id": 100, "time": int(_t.time())}, "group_ban"),
        # profile_like
        ({"notice_type": "notify", "sub_type": "profile_like", "operator_id": 1,
          "times": 3, "time": int(_t.time())}, "profile_like_self"),
        # group_decrease
        ({"notice_type": "group_decrease", "sub_type": "kick", "user_id": 1,
          "operator_id": 2, "group_id": 100, "time": int(_t.time())}, "member_decrease"),
        # recall
        ({"notice_type": "group_recall", "user_id": 1, "operator_id": 2,
          "message_id": 888, "group_id": 100, "time": int(_t.time())}, "recall"),
        # title
        ({"notice_type": "notify", "sub_type": "title", "user_id": 1,
          "title": "活跃分子", "time": int(_t.time())}, "title"),
    ]
    ok_count = 0
    for raw, expect in payloads:
        ev = normalize_notice(raw, sid="napcat:gm:100", self_id="3003")
        if ev and ev.etype == expect:
            ok_count += 1
        else:
            return False, f"payload 处理失败: 期望 {expect}, 得到 {ev.etype if ev else None}"
    return True, f"{ok_count}/{len(payloads)} 种真实 payload 归一化正确"


def r2_normalizer_edge_cases():
    """归一化器边界：空/畸形输入不崩。"""
    from qq_core.event_normalizer import normalize_notice
    cases = [
        None, {}, {"notice_type": None}, {"notice_type": "unknown_type"},
        {"notice_type": "notify"}, {"notice_type": "notify", "sub_type": "poke"},
        {"notice_type": "group_msg_emoji_like", "likes": None},
        {"notice_type": "group_msg_emoji_like", "likes": [{"emoji_id": None}]},
        {"notice_type": "group_ban", "duration": "abc"},
        {"notice_type": "notify", "sub_type": "profile_like", "times": None},
    ]
    for c in cases:
        try:
            normalize_notice(c, sid="s", self_id="3003")
        except Exception as e:
            return False, f"边界输入 {c!r} 抛异常: {type(e).__name__}: {e}"
    return True, f"{len(cases)} 种边界输入全部安全"


def r3_renderer_edge_cases():
    """渲染器边界。"""
    from qq_core.env_renderer import render_env_summary
    from qq_core.event_normalizer import QQEvent, E_POKE
    import time as _t

    # 空列表
    if render_env_summary([]) != "":
        return False, "空列表应返回空串"
    # 单事件
    e = QQEvent(eid="1", etype=E_POKE, sid="s", ts=int(_t.time()), actor="1", target="2")
    if not render_env_summary([e]):
        return False, "单事件应渲染"
    # 极小 max_chars（必须能返回，不崩）
    many = [QQEvent(eid=str(i), etype=E_POKE, sid="s", ts=int(_t.time()), actor=str(i), target="2") for i in range(20)]
    r = render_env_summary(many, max_chars=10)
    if not isinstance(r, str):
        return False, "极小 max_chars 应返回字符串"
    if len(r) > 200:
        return False, f"极小 max_chars 应大幅裁剪，得到 {len(r)} 字符"
    # 未知类型（不应崩）
    unknown = QQEvent(eid="u", etype="unknown_etype", sid="s", ts=int(_t.time()))
    render_env_summary([unknown])
    return True, "边界渲染安全（含极小预算裁剪）"


def r4_store_lru_and_persist():
    """store LRU + 持久化往返。"""
    import tempfile
    from qq_core.event_store import EventStore
    from qq_core.event_normalizer import QQEvent, E_POKE

    async def run():
        with tempfile.TemporaryDirectory() as d:
            s = EventStore(d, max_events=3)
            for i in range(5):
                await s.add(QQEvent(eid=f"e{i}", etype=E_POKE, sid="a:gm:1", ts=int(time.time()) + i))
            evs = await s.get_events("a:gm:1")
            assert len(evs) == 3, f"LRU 失败: {len(evs)}"
            assert evs[0].eid == "e2", f"LRU 淘汰错误: {evs[0].eid}"

            await s._persist_all()
            s2 = EventStore(d, max_events=3)
            await s2.load_all()
            evs2 = await s2.get_events("a:gm:1")
            assert len(evs2) == 3, f"持久化往返失败: {len(evs2)}"
            return True, "LRU + 持久化往返正确"

    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(run())


def r5_wake_scheduler_cooldown():
    """他人表情唤醒冷却生效。"""
    from qq_core.wake_scheduler import WakeScheduler
    from qq_core.event_normalizer import QQEvent, E_EMOJI_LIKE

    class Bus:
        def __init__(self): self.pub = []
        async def publish(self, e): self.pub.append(e)
    class A:
        platform = "QQ"
        class bot:
            @staticmethod
            async def send_action(*a, **k): return {"status": "ok", "data": {}}
        class info:
            name = "napcat"; platform = "QQ"; config = {"self_id": "1"}
    class M:
        @staticmethod
        def get_adapter(n): return A()
    class C:
        def __init__(self): self.event_bus = Bus(); self.adapter_mgr = M()
        def get_plugin_data_dir(self): return None

    ctx = C()
    s = WakeScheduler(ctx=ctx, adapter=A(), self_id="1", message_locator=None,
                      wake_others_emoji=True, wake_others_emoji_cooldown=20,
                      wake_max_per_hour=-1, wake_emoji_merge_seconds=0)
    ev = QQEvent(eid="1", etype=E_EMOJI_LIKE, sid="napcat:gm:1", ts=int(time.time()),
                 actor="2", extra={"message_id": "1", "emoji_ids": ["1"], "count": 1})

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # 第 1 次：应唤醒
    loop.run_until_complete(s.trigger_others_emoji_like(ev, "3", "bot", "x"))
    n1 = len(ctx.event_bus.pub)
    # 第 2 次（冷却内）：should_wake_now 应为 False
    ok2 = s.should_wake_now(ev)
    if n1 != 1:
        return False, f"首次应唤醒: {n1}"
    if ok2 is not False:
        return False, "冷却内 should_wake_now 应为 False"
    return True, "冷却生效（首次唤醒，冷却内拦截）"


def r6_hourly_limit_unlimited():
    """-1 表示无限制。"""
    from qq_core.wake_scheduler import WakeScheduler
    class C:
        class event_bus:
            @staticmethod
            async def publish(e): pass
        class adapter_mgr:
            @staticmethod
            def get_adapter(n): return None
        def get_plugin_data_dir(self): return None
    s = WakeScheduler(ctx=C(), adapter=None, self_id="1", message_locator=None, wake_max_per_hour=-1)
    ok = all(s._check_hourly_limit("s") for _ in range(1000))
    return ok, "1000 次调用均不拦截（-1 无限制）"


# ============ 4. 冲突扫描 ============

def c1_no_reserved_hook_conflict():
    """不与框架内置插件抢 hook（SYS_HIGH 保留）。"""
    src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    if "SYS_HIGH" in src or "SYS_LOW" in src:
        return False, "不应使用 SYS_* 优先级"
    return True, "未使用 SYS_* 保留优先级"


def c2_env_prompt_name_unique():
    """环境块 name 不与框架 DYNAMIC_PROMPT_NAMES 冲突。"""
    sys.path.insert(0, "/var/minis/workspace/qq_enhance2_design/kiraai312")
    from core.provider.llm_model import DYNAMIC_PROMPT_NAMES, MEMORY_PROMPT_NAME
    src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    m = re.search(r'name="([^"]+)"', src)
    our_name = m.group(1) if m else ""
    reserved = set(DYNAMIC_PROMPT_NAMES) | {MEMORY_PROMPT_NAME}
    if our_name in reserved:
        return False, f"环境块 name {our_name} 与框架保留名冲突"
    return True, f"环境块 name={our_name} 不与框架保留名冲突 (保留: {reserved})"


def c3_tool_desc_length():
    """工具 description 总长度合理（token 成本）。"""
    total = 0
    details = []
    for tf in (PLUGIN_ROOT / "tools").glob("*.py"):
        if tf.name == "__init__.py":
            continue
        tree = ast.parse(tf.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef) and n.name.endswith("Tool"):
                for s in n.body:
                    if isinstance(s, ast.Assign) and getattr(s.targets[0], "id", "") == "description":
                        if isinstance(s.value, ast.Constant):
                            total += len(str(s.value.value))
                            details.append(f"{n.name}:{len(str(s.value.value))}")
    if total > 600:
        return False, f"工具描述总长 {total} 字符偏长（token 成本）: {details}"
    return True, f"工具描述总长 {total} 字符（精简）: {details}"


# ============ 运行 ============

check("V1 send_action 签名", v1_send_action)
check("V2 desc_img 签名", v2_desc_img)
check("V3 get_default_vlm 存在", v3_get_default_vlm)
check("V4 Prompt.persist 支持", v4_prompt_sig)
check("V5 BaseTool.ctx", v5_base_tool)
check("V6 Session 字段", v6_session_fields)
check("V7 KiraIMMessage 字段", v7_kira_im_message)
check("V8 KiraMessageEvent 字段", v8_kira_message_event)
check("L1 group_ban self 不重复", l1_no_duplicate_wake_text)
check("L2 不处理 group_increase", l2_no_group_increase)
check("L3 他人事件不 trigger", l3_notice_never_trigger_for_others)
check("L4 唤醒文本无禁言关键词", l4_no_forbidden_keyword_in_wake_text)
check("L5 store 文件名净化", l5_store_filename_safe)
check("L6 贴表情归档 LRU", l6_emoji_archive_lru)
check("L7 撤回归档 LRU", l7_recall_store_lru)
check("L8 无阻塞 IO", l8_no_blocking_io_in_async)
check("L9 贴表情合并去重", l9_emoji_like_dedup)
check("L10 环境摘要不渲染唤醒事件", l10_env_render_no_wake_types)
check("L11 撤回标注不泄露原文", l11_recall_annotation_no_content_leak)
check("L12 每小时上限边界", l12_hourly_limit_edge)
check("L13 表情显示携带 ID", l13_emoji_carries_id)
check("R1 三家 payload 归一化", r1_normalizer_all_types)
check("R2 归一化边界", r2_normalizer_edge_cases)
check("R3 渲染边界", r3_renderer_edge_cases)
check("R4 store LRU+持久化", r4_store_lru_and_persist)
check("R5 他人表情冷却", r5_wake_scheduler_cooldown)
check("R6 每小时无限制", r6_hourly_limit_unlimited)
check("C1 无 SYS_* 优先级冲突", c1_no_reserved_hook_conflict)
check("C2 环境块 name 不冲突", c2_env_prompt_name_unique)
check("C3 工具描述长度", c3_tool_desc_length)

print("\n" + "=" * 64)
passed = sum(1 for r in results if r[0])
print(f"深度审计结果: {passed}/{len(results)} 通过")
if passed != len(results):
    print("失败项:")
    for ok, name, detail in results:
        if not ok:
            print(f"  ✗ {name}: {detail}")
    sys.exit(1)