"""第十四轮回归测试（Bug 51-54）：self_id / message_id / 撤回摘要 / 动作文案。

覆盖用户实机反馈的 4 个问题：
51. self_id 取错字段（配置是 bot_pid 不是 self_id）→ bot 自己戳别人被记录
52. 唤醒事件 message_id 太长（qq_enhance2_xxx）→ 对齐框架用 "None"
53. 撤回既在原文标注又在群动态摘要（重复）→ 摘要默认不含撤回
54. 戳一戳文案固定"戳了戳"→ 应用协议端真实文案（拍了拍等）
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai_latest")
if not (KIRAAI / "core").is_dir():
    KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
sys.path.insert(0, str(KIRAAI))
sys.path.insert(0, str(ROOT))

from qq_core.fmt import fmt_user
from qq_core.env_renderer import render_env_summary
from qq_core.event_normalizer import QQEvent, E_POKE, E_RECALL

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


def _main():
    import re
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    ws_src = (ROOT / "qq_core" / "wake_scheduler.py").read_text(encoding="utf-8")

    print("\n=== 51. self_id 必须从 bot_pid 取（不是 self_id）===")
    check("_extract_self_id 存在", "_extract_self_id" in main_src)
    # 必须优先 bot_pid
    m = re.search(r'for key in \(([^)]*)\):\s*\n\s*v = cfg\.get\(key\)', main_src)
    if m:
        keys = m.group(1)
        check("bot_pid 在候选字段里", '"bot_pid"' in keys, keys)
        # bot_pid 必须在 self_id 之前
        check("bot_pid 优先于 self_id",
              keys.find('"bot_pid"') < keys.find('"self_id"'), keys)
    else:
        check("找到 config 字段候选", False, "正则未匹配")

    # 必须还有 bot.self_id 兜底
    check("有 bot.self_id 兜底", 'getattr(bot, "self_id"' in main_src)

    print("\n=== 52. 唤醒事件 message_id 对齐框架（None）===")
    check("不再用 qq_enhance2_ 前缀", "qq_enhance2_" not in ws_src,
          "仍在使用长前缀")
    check('message_id="None"', 'message_id="None"' in ws_src,
          "应对齐框架用 None")

    print("\n=== 53. 撤回默认不进群动态摘要 ===")
    check("_env_etypes 存在", "_env_etypes" in main_src)
    check("recall_in_env 配置存在", "recall_in_env" in main_src)
    m2 = re.search(r'types = \{(E_[^}]*)\}', main_src)
    if m2:
        check("默认类型集不含 E_RECALL", "E_RECALL" not in m2.group(1), m2.group(1))
    check("recall_in_env 为真时才加入",
          re.search(r"if self\.recall_in_env:\s*\n\s*types\.add\(E_RECALL\)", main_src) is not None,
          "条件添加逻辑")

    print("\n=== 54. 戳一戳用真实动作文案 ===")
    norm_src = (ROOT / "qq_core" / "event_normalizer.py").read_text(encoding="utf-8")
    check("_extract_poke_texts 存在", "_extract_poke_texts" in norm_src)
    check("解析 NapCat raw_info", "raw_info" in norm_src)
    check("解析 action/suffix", '"action"' in norm_src and '"suffix"' in norm_src)

    # 渲染用真实文案
    render_src = (ROOT / "qq_core" / "env_renderer.py").read_text(encoding="utf-8")
    check("渲染读 raw_action", 'ev.extra.get("raw_action")' in render_src)

    import time
    now = int(time.time())
    for act, expect in (("拍了拍", "拍了拍"), ("戳了戳", "戳了戳"), ("", "戳了戳")):
        ev = QQEvent("1", E_POKE, "s", now, actor="1", actor_name="A",
                     target="2", target_name="B", extra={"raw_action": act})
        out = render_env_summary([ev])
        check(f"文案 {act!r} → 渲染含 {expect!r}", expect in out, out)

    print("\n=== 55. 昵称等于 QQ 号时不重复显示 ===")
    check("fmt_user 去重", fmt_user("3991867505", "3991867505") == "用户3991867505",
          fmt_user("3991867505", "3991867505"))
    check("正常昵称保留", fmt_user("1001", "张三") == "用户1001(张三)")


_main()

print("\n" + "=" * 60)
print(f"第十四轮审计: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部通过 ===")