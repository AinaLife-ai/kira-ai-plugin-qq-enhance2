"""第六轮人工审计回归测试（Bug 32-36）。

覆盖：
32. 撤回不合并（同人多次撤回）
33. 贴表情不按 (actor,target) 合并 + message_id 截断
34. 时间前缀按 age 判断跨天误判
35. record_incoming 文本截断过短（撤回匹配失败）
36. 被踢唤醒不带操作者 / 唤醒文本空昵称输出"用户1001()"
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_core import _install_framework_stubs  # noqa

from qq_core.event_normalizer import QQEvent, E_POKE, E_RECALL, E_EMOJI_LIKE
from qq_core.env_renderer import render_env_summary, _time_prefix

PLUGIN_ROOT = Path(__file__).parent.parent


# ============ Bug 32: 撤回合并 ============

def test_bug32_recall_merge():
    now = int(time.time())
    evs = [
        QQEvent("1", E_RECALL, "s", now - 300, actor="1001", actor_name="张三"),
        QQEvent("2", E_RECALL, "s", now - 200, actor="1001", actor_name="张三"),
        QQEvent("3", E_RECALL, "s", now - 100, actor="1002", actor_name="李四"),
    ]
    t = render_env_summary(evs)
    print(f"  渲染: {t}")
    assert "张三" in t and "李四" in t
    # 张三两次应合并（出现 ×2）
    assert "×2" in t, f"同一人多次撤回应合并: {t}"
    # 张三只出现一次
    assert t.count("张三") == 1, f"张三应只出现一次: {t}"


def test_bug32_recall_identity():
    """区分撤回自己 vs 撤回他人。"""
    now = int(time.time())
    evs = [
        QQEvent("1", E_RECALL, "s", now - 100, actor="1001", actor_name="张三",
                target="1001"),
        QQEvent("2", E_RECALL, "s", now - 50, actor="8888", actor_name="管理员",
                target="1005"),
    ]
    t = render_env_summary(evs)
    print(f"  渲染: {t}")
    assert "撤回了一条消息" in t, f"撤回自己: {t}"
    assert "撤回了他人消息" in t, f"撤回他人: {t}"


# ============ Bug 33: 贴表情合并 ============

def test_bug33_emoji_merge():
    now = int(time.time())
    evs = [
        QQEvent(f"{i}", E_EMOJI_LIKE, "s", now - 100 + i, actor="1001", actor_name="张三",
                target="2002", target_name="李四",
                extra={"emoji_ids": ["4"], "count": 1, "message_id": "12345678"})
        for i in range(5)
    ]
    t = render_env_summary(evs)
    print(f"  5 次同人贴同目标: {t}")
    assert t.count("张三") == 1, f"应合并为一条: {t}"
    assert "×5" in t, f"次数应累加: {t}"


def test_bug33_full_message_id():
    """message_id 不应被截断（bot 回贴需要完整 ID）。"""
    now = int(time.time())
    evs = [QQEvent("1", E_EMOJI_LIKE, "s", now, actor="1001", actor_name="张三",
                   target="2002", target_name="李四",
                   extra={"emoji_ids": ["4"], "count": 1, "message_id": "1234567890123"})]
    t = render_env_summary(evs)
    print(f"  渲染: {t}")
    assert "1234567890123" in t, f"应含完整 message_id: {t}"
    assert "..." not in t, f"不应截断: {t}"


# ============ Bug 34: 时间前缀按日历 ============

def test_bug34_time_prefix_calendar():
    now = time.time()
    # 刚刚
    assert "刚刚" in _time_prefix([QQEvent("1", E_POKE, "s", int(now - 30))])
    # 过去 N 分钟
    assert "过去" in _time_prefix([QQEvent("1", E_POKE, "s", int(now - 300))])
    # 昨天（跨天场景：23 小时前可能是昨天也可能是今天，用 30 小时确保昨天）
    p = _time_prefix([QQEvent("1", E_POKE, "s", int(now - 30 * 3600))])
    print(f"  30 小时前: {p}")
    assert "昨天" in p or "-" in p, f"应显示昨天或日期: {p}"
    # 更早
    p2 = _time_prefix([QQEvent("1", E_POKE, "s", int(now - 10 * 86400))])
    print(f"  10 天前: {p2}")
    assert "-" in p2, f"应显示日期: {p2}"


def test_bug34_crossday():
    """跨天场景：昨天 23:00 → 今天 01:00（age=2h）不应显示"今天"。"""
    from datetime import datetime, timedelta, time as dtime
    # 构造"今天 01:00"
    today = datetime.now().date()
    t_today_0100 = datetime.combine(today, dtime(1, 0))
    now_ts = t_today_0100.timestamp()
    # 如果当前时间早于 01:00 则跳过（无法构造过去）
    if time.time() < now_ts:
        print("  (跳过：当前时间早于 01:00)")
        return
    # 昨天 23:00
    y_2300 = datetime.combine(today - timedelta(days=1), dtime(23, 0))
    p = _time_prefix([QQEvent("1", E_POKE, "s", int(y_2300.timestamp()))])
    print(f"  昨天23:00（age≈{int((time.time()-y_2300.timestamp())/3600)}h）: {p}")
    assert "昨天" in p, f"应按日历显示昨天: {p}"


# ============ Bug 35: 文本缓存长度 ============

def test_bug35_text_cache_length():
    src = (PLUGIN_ROOT / "qq_core" / "message_locator.py").read_text(encoding="utf-8")
    assert "text[:500]" in src, "文本缓存应保留 500 字符（撤回匹配需要）"
    assert "text[:200]" not in src, "不应只保留 200 字符"
    print("  ✓ Bug35: 消息文本缓存 500 字符")


# ============ Bug 36: 唤醒文本格式化 ============

def test_bug36_kicked_with_actor():
    """被踢唤醒应带操作者。"""
    src = (PLUGIN_ROOT / "qq_core" / "wake_scheduler.py").read_text(encoding="utf-8")
    assert "你被 {fmt_user(event.actor, event.actor_name)} 移出群聊" in src, \
        "被踢唤醒应带操作者"
    print("  ✓ Bug36: 被踢唤醒带操作者")


def test_bug36_no_empty_parens():
    """所有唤醒文本用 fmt_user（避免"用户1001()"）。"""
    src = (PLUGIN_ROOT / "qq_core" / "wake_scheduler.py").read_text(encoding="utf-8")
    import re
    # 只看代码行（排除 docstring/注释里的示例）
    code_lines = []
    in_docstring = False
    for line in src.split("\n"):
        s = line.strip()
        if s.startswith('"""') or s.startswith("'''"):
            in_docstring = not in_docstring
            continue
        if in_docstring or s.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)

    # 不应有 "用户{xxx}({yyy})" 的手写格式（在 f-string 里）
    bad = re.findall(r'f"[^"]*用户\{[^}]+\}\(\{[^}]+\}\)', code)
    assert not bad, f"应使用 fmt_user 而非手写格式: {bad}"
    print("  ✓ Bug36: 唤醒文本统一用 fmt_user（无空括号）")


def test_fmt_user_function():
    """fmt_user 各种输入都正确。"""
    from qq_core.fmt import fmt_user
    assert fmt_user("123", "张三") == "用户123(张三)"
    assert fmt_user("123", "") == "用户123"
    assert fmt_user("", "张三") == "用户(张三)"
    assert fmt_user("", "") == "用户(未知)"
    assert fmt_user("123", None) == "用户123"
    assert fmt_user(None, "张三") == "用户(张三)"
    print("  ✓ fmt_user 六种输入全部正确")


if __name__ == "__main__":
    test_bug32_recall_merge()
    test_bug32_recall_identity()
    test_bug33_emoji_merge()
    test_bug33_full_message_id()
    test_bug34_time_prefix_calendar()
    test_bug34_crossday()
    test_bug35_text_cache_length()
    test_bug36_kicked_with_actor()
    test_bug36_no_empty_parens()
    test_fmt_user_function()
    print("=== 第六轮 10 项回归测试全部通过 ===")