"""第十六轮回归：记录"自己的禁言/解禁操作"（两个独立开关，默认关）。

需求（2026-11-18）：
    动态里增加"自己禁言操作"的展示，格式与他人被禁言类似；默认关。
    补充：**解禁操作要单独一个开关**（collect_self_unban），与禁言互不影响。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai_latest")
if not (KIRAAI / "core").is_dir():
    KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
sys.path.insert(0, str(KIRAAI))
sys.path.insert(0, str(ROOT))

from qq_core.event_normalizer import (  # noqa: E402
    normalize_notice, E_BAN, E_UNBAN,
)
from qq_core.env_renderer import render_env_summary  # noqa: E402

BOT = "583893217"
USER = "1005"
ADMIN = "8888"
GROUP = "999888777"
SID = f"qq:gm:{GROUP}"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


def _ban(**kw):
    raw = {"notice_type": "group_ban", "sub_type": "ban", "user_id": USER,
           "operator_id": BOT, "duration": 600, "group_id": GROUP, "time": 1000}
    raw.update(kw)
    return raw


def _unban(**kw):
    raw = {"notice_type": "group_ban", "sub_type": "lift_ban", "user_id": USER,
           "operator_id": BOT, "duration": 0, "group_id": GROUP, "time": 1200}
    raw.update(kw)
    return raw


def _norm(raw, **flags):
    return normalize_notice(raw, sid=SID, self_id=BOT, **flags)


def test_self_ban_default_off():
    ev = _norm(_ban())
    check("默认关：自己禁言别人 → 不记录", ev is None, repr(ev))


def test_self_unban_default_off():
    ev = _norm(_unban())
    check("默认关：自己解禁 → 不记录", ev is None, repr(ev))


def test_self_ban_on():
    ev = _norm(_ban(), collect_self_ban=True)
    check("开 collect_self_ban：记录", ev is not None and ev.etype == E_BAN, repr(ev))
    check("标记 self_action", bool(ev) and ev.extra.get("self_action") is True, getattr(ev, "extra", None))
    check("actor=bot / target=被禁言者",
          bool(ev) and ev.actor == BOT and ev.target == USER, (getattr(ev, "actor", None), getattr(ev, "target", None)))
    ev.actor_name, ev.target_name = "紫小贱", "赵六"
    text = render_env_summary([ev], now=1100, show_time=False)
    check("渲染为「你禁言了 用户1005(赵六) 600秒」",
          "你禁言了 用户1005(赵六) 600秒" in text, text)
    check("禁言中显示剩余时间", "剩约" in text, text)


def test_self_unban_needs_own_switch_but_still_pairs():
    """只开禁言、不开解禁：不单独成行，但禁言条目要显示（已解除）。"""
    ban = _norm(_ban(), collect_self_ban=True)
    unban = _norm(_unban(), collect_self_ban=True)
    check("只开禁言时，解禁事件仍被收集（配对用）", unban is not None, repr(unban))

    ban.actor_name, unban.actor_name = "紫小贱", "紫小贱"
    ban.target_name, unban.target_name = "赵六", "赵六"
    text = render_env_summary([ban, unban], now=1300, show_time=False,
                              show_self_unban=False)
    check("禁言条目显示（已解除）", "（已解除）" in text, text)
    check("解禁不单独成行", "你解除了" not in text, text)


def test_self_unban_on_shows_separate_line():
    ban = _norm(_ban(), collect_self_ban=True)
    unban = _norm(_unban(), collect_self_unban=True)
    ban.actor_name, unban.actor_name = "紫小贱", "紫小贱"
    ban.target_name, unban.target_name = "赵六", "赵六"
    text = render_env_summary([ban, unban], now=1300, show_time=False,
                              show_self_unban=True)
    check("开 collect_self_unban：单独一行",
          "你解除了 用户1005(赵六) 的禁言" in text, text)


def test_self_unban_only_without_ban():
    """只开解禁：解禁单独成行；禁言事件本身不进动态。"""
    check("只开解禁时，自己不记录禁言", _norm(_ban(), collect_self_unban=True) is None)
    unban = _norm(_unban(), collect_self_unban=True)
    unban.actor_name, unban.target_name = "紫小贱", "赵六"
    text = render_env_summary([unban], now=1300, show_time=False, show_self_unban=True)
    check("只有解禁时渲染「你解除了 …」", "你解除了 用户1005(赵六) 的禁言" in text, text)


def test_others_ban_unaffected():
    """他人之间的禁言不受这两个开关影响。"""
    raw = _ban(operator_id=ADMIN)
    ev = _norm(raw)
    check("他人禁言照常记录", ev is not None and ev.etype == E_BAN, repr(ev))
    check("他人禁言不带 self_action", bool(ev) and not ev.extra.get("self_action"), getattr(ev, "extra", None))
    ev.actor_name, ev.target_name = "管理员A", "赵六"
    text = render_env_summary([ev], now=1100, show_time=False)
    check("他人禁言渲染不变", "用户1005(赵六) 被 用户8888(管理员A) 禁言 600秒" in text, text)


def test_self_target_still_dropped():
    """自己被禁言（别人禁 bot）始终不进动态，与开关无关。"""
    raw = _ban(user_id=BOT, operator_id=ADMIN)
    check("自己被禁言 → 不记录", _norm(raw, collect_self_ban=True, collect_self_unban=True) is None)


def test_main_wiring():
    """主流程接线检查：两个开关都要传给 normalizer / 渲染层。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    check("读取 collect_self_ban", 'plugin_cfg.get("collect_self_ban"' in src)
    check("读取 collect_self_unban", 'plugin_cfg.get("collect_self_unban"' in src)
    check("normalize 传参 collect_self_unban", "collect_self_unban=self.collect_self_unban" in src)
    check("渲染传 show_self_unban", "show_self_unban=self.collect_self_unban" in src)
    check("self_action 放行 involves_self",
          'ev.extra.get("self_action")' in src)


def _main():
    test_self_ban_default_off()
    test_self_unban_default_off()
    test_self_ban_on()
    test_self_unban_needs_own_switch_but_still_pairs()
    test_self_unban_on_shows_separate_line()
    test_self_unban_only_without_ban()
    test_others_ban_unaffected()
    test_self_target_still_dropped()
    test_main_wiring()
    print("\n" + "=" * 60)
    print(f"回归16: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for name, detail in FAIL:
            print(f"  FAIL {name}: {detail}")
        sys.exit(1)
    print("=== 全部通过 ===")


if __name__ == "__main__":
    _main()
