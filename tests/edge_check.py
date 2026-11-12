"""边界场景验证：真实使用中会遇到但单元测试容易漏掉的情况。

重点验证：
1. 私聊场景（dm）全流程
2. 自己被贴表情 / 自己撤回
3. 协议端字段缺失（降级不崩）
4. 同一消息被多人贴表情的合并
5. 撤回消息不在当前 prompt（标注不丢失）
6. 空群/无事件
7. 配置极端值（0 / -1 / 超大）
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_core import _install_framework_stubs  # noqa

from core.prompt_manager import Prompt
from core.provider import LLMRequest

from qq_core.event_normalizer import normalize_notice, QQEvent
from qq_core.env_renderer import render_env_summary
from qq_core.event_store import EventStore
from qq_core.emoji_rewriter import (
    archive_emoji_like, rewrite_emoji_like, _EMOJI_STORE,
)
from qq_core.message_locator import MessageLocator
from qq_core.recall_rewriter import RecallRewriter

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK  {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


def run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


print("\n=== 边界 1：私聊场景 ===")
DM_SID = "napcat:dm:1001"
SELF = "3003003"

# 私聊被戳
ev = normalize_notice(
    {"notice_type": "notify", "sub_type": "poke", "user_id": 1001,
     "target_id": 3003003, "time": int(time.time())},
    sid=DM_SID, self_id=SELF,
)
check("私聊 poke 归一化成功", ev is not None)
check("私聊 poke 记录 target=self", ev.target == "3003003" if ev else False)

# 私聊撤回（operator 可能为空）
locator = MessageLocator(adapter=None, self_id=SELF)
run(locator.record_incoming("900", "1001", "张三", "私聊消息内容"))
run(locator.archive_recall(
    sid=DM_SID, message_id="900", sender="1001", sender_name="张三",
    text="私聊消息内容", ts=int(time.time()),
    operator="", operator_name="",  # 私聊无 operator
    sub_type="", is_group=False,
))
recalls = run(locator.get_recalls(DM_SID))
check("私聊撤回归档成功", len(recalls) == 1)
# operator 为空时应回退到 sender，不显示"用户()"
ann = recalls[0]["annotation"] if recalls else ""
print(f"  私聊撤回标注: {ann!r}")
check("私聊撤回不出现'用户()'", "用户()" not in ann, ann)
check("私聊撤回回退到发送者", "1001" in ann, ann)


print("\n=== 边界 2：自己被贴表情 / 自己撤回 ===")
# 自己撤回自己的消息
run(locator.record_incoming("901", SELF, "我", "我自己发的"))
run(locator.archive_recall(
    sid=DM_SID, message_id="901", sender=SELF, sender_name="我",
    text="我自己发的", ts=int(time.time()),
    operator=SELF, operator_name="我", sub_type="", is_group=False,
))
recalls2 = run(locator.get_recalls(DM_SID))
ann2 = recalls2[-1]["annotation"]
print(f"  自己撤回标注: {ann2!r}")
check("自己撤回措辞正确（无角色前缀）", "（管理员）" not in ann2 and "（群主）" not in ann2, ann2)


print("\n=== 边界 3：协议端字段缺失（降级不崩）===")
malformed = [
    # NapCat 缺 likes
    {"notice_type": "group_msg_emoji_like", "user_id": 1001, "message_id": 1,
     "group_id": 100, "time": int(time.time())},
    # 禁言缺 duration
    {"notice_type": "group_ban", "sub_type": "ban", "user_id": 1001,
     "operator_id": 2, "group_id": 100, "time": int(time.time())},
    # 撤回缺 message_id
    {"notice_type": "group_recall", "user_id": 1001, "operator_id": 2,
     "group_id": 100, "time": int(time.time())},
    # poke 缺 target
    {"notice_type": "notify", "sub_type": "poke", "user_id": 1001,
     "group_id": 100, "time": int(time.time())},
    # 空 dict
    {},
]
crashed = []
for i, raw in enumerate(malformed):
    try:
        ev = normalize_notice(raw, sid="napcat:gm:100", self_id=SELF)
        # 渲染也不应崩
        if ev:
            render_env_summary([ev])
    except Exception as e:
        crashed.append((i, type(e).__name__, str(e)))
check(f"{len(malformed)} 种字段缺失输入不崩", not crashed, str(crashed))


print("\n=== 边界 4：同一消息被多人贴表情 ===")
_EMOJI_STORE.clear()
SID = "napcat:gm:100"
archive_emoji_like(sid=SID, message_id="777", message_text="热门消息",
                   emoji_ids=["4"], count=1, operator="1001", operator_name="张三", ts=1)
archive_emoji_like(sid=SID, message_id="777", message_text="热门消息",
                   emoji_ids=["9"], count=1, operator="1002", operator_name="李四", ts=2)
archive_emoji_like(sid=SID, message_id="777", message_text="热门消息",
                   emoji_ids=["4"], count=2, operator="1001", operator_name="张三", ts=3)

item = _EMOJI_STORE[SID][0]
print(f"  标注: {item['annotation']!r}")
check("同一消息只有一条归档", len(_EMOJI_STORE[SID]) == 1)
check("张三出现一次（不重复）", item["annotation"].count("张三") == 1, item["annotation"])
check("张三次数累加（1+2=3）", "×3" in item["annotation"], item["annotation"])
check("李四的表情也保留", "李四" in item["annotation"])


print("\n=== 边界 5：撤回消息不在本轮 prompt（标注不丢失）===")
locator2 = MessageLocator(adapter=None, self_id=SELF)
run(locator2.record_incoming("888", "1001", "张三", "较早的历史消息"))
run(locator2.archive_recall(
    sid=SID, message_id="888", sender="1001", sender_name="张三",
    text="较早的历史消息", ts=int(time.time()),
    operator="1001", operator_name="张三", sub_type="", is_group=True,
))
# 本轮 prompt 只有其他消息
req = LLMRequest()
req.user_prompt = [Prompt("完全不相关的新消息", name="message", source="system")]
rw = RecallRewriter(locator=locator2)
run(rw.rewrite(sid=SID, req=req))
remain = run(locator2.get_recalls(SID))
check("未匹配的撤回归档被保留（不丢失）", len(remain) == 1, f"{len(remain)}")
check("不相关消息未被误改", req.user_prompt[0].content == "完全不相关的新消息")

# 第二轮：该消息出现在 prompt 中 → 应能补上标注
req2 = LLMRequest()
req2.user_prompt = [Prompt("较早的历史消息", name="message", source="system")]
run(rw.rewrite(sid=SID, req=req2))
check("下一轮补上标注", "[已撤回｜" in req2.user_prompt[0].content,
      req2.user_prompt[0].content)
check("补上后归档清空", len(run(locator2.get_recalls(SID))) == 0)


print("\n=== 边界 6：空事件 ===")
check("空列表返回空串", render_env_summary([]) == "")
check("空列表不崩（max_chars=0）", render_env_summary([], max_chars=0) == "")


print("\n=== 边界 7：配置极端值 ===")


async def test_store_extremes():
    import tempfile
    results = []
    # max_events=1
    with tempfile.TemporaryDirectory() as d:
        s = EventStore(d, max_events=1)
        for i in range(5):
            await s.add(QQEvent(eid=str(i), etype="poke", sid="s", ts=i))
        results.append(("max_events=1", len(await s.get_events("s")) == 1))
    # max_events=0（边界：应至少保留 1 条或清空，不崩）
    with tempfile.TemporaryDirectory() as d:
        s = EventStore(d, max_events=0)
        for i in range(3):
            await s.add(QQEvent(eid=str(i), etype="poke", sid="s", ts=i))
        n = len(await s.get_events("s"))
        results.append(("max_events=0 不崩", n >= 0))
    return results


for name, ok in run(test_store_extremes()):
    check(name, ok)

# 渲染极端配置
evs = [QQEvent(eid="1", etype="poke", sid="s", ts=int(time.time()),
               actor="1", actor_name="A", target="2", target_name="B")]
check("max_per_type=0 不崩", isinstance(render_env_summary(evs, max_per_type=0), str))
check("max_chars=1 不崩", isinstance(render_env_summary(evs, max_chars=1), str))


print("\n=== 边界 8：唤醒文本格式 ===")
# 检查所有唤醒文本模板都带 QQ 号
import re
ws_src = Path(__file__).parent.parent.joinpath("qq_core", "wake_scheduler.py").read_text(encoding="utf-8")
templates = re.findall(r'text = f"(\[System[^"]*)"', ws_src)
print(f"  唤醒模板 {len(templates)} 条:")
for t in templates:
    print(f"    {t[:80]}")
all_have_qq = all("{" in t and ("actor" in t or "operator" in t) for t in templates)
check("所有唤醒模板含用户标识", all_have_qq)
check("唤醒模板无'禁言N秒'（防 S 误触发）",
      not any("禁言" in t for t in templates))


print("\n" + "=" * 60)
print(f"边界验证: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部边界场景通过 ===")