"""场景级集成验证：模拟真实群聊事件流，检查最终 prompt 是否符合预期。

不是单元测试，而是"真实使用场景"端到端验证：
1. 群里发生一连串事件（戳一戳/贴表情/禁言/撤回/退群）
2. 检查环境摘要的渲染结果
3. 检查撤回/贴表情标注是否正确追加
4. 检查缓存不变量（persist=False）
5. 检查事件存储的 LRU/过滤/踢旧

运行：python3 tests/scenario_check.py
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


SID = "napcat:gm:188395693"
SELF_ID = "3003003"


class FakeBot:
    """模拟协议端：记录调用，返回合理数据。"""
    def __init__(self):
        self.calls = []

    async def send_action(self, action, params, timeout=10.0):
        self.calls.append((action, params))
        if action == "get_group_member_info":
            uid = str(params.get("user_id", ""))
            role = "admin" if uid == "8888" else "member"
            return {"status": "ok", "data": {
                "card": "", "nickname": f"用户{uid}", "title": "", "role": role,
            }}
        if action == "get_stranger_info":
            return {"status": "ok", "data": {
                "nickname": f"用户{params.get('user_id')}",
                "sign": "签名内容", "sex": 1,
            }}
        if action == "get_msg":
            return {"status": "ok", "data": {
                "user_id": 1001, "time": int(time.time()),
                "sender": {"nickname": "张三"},
                "message": [{"type": "text", "data": {"text": "被撤回的原始消息内容"}}],
            }}
        return {"status": "ok", "data": {}}


class FakeAdapter:
    def __init__(self):
        self.bot = FakeBot()


print("\n=== 场景 1：群聊连续事件 → 环境摘要 ===")
now = int(time.time())
raw_events = [
    {"notice_type": "notify", "sub_type": "poke", "user_id": 1001, "target_id": 2002,
     "group_id": 188395693, "time": now - 600},
    {"notice_type": "notify", "sub_type": "poke", "user_id": 1001, "target_id": 2002,
     "group_id": 188395693, "time": now - 580},
    {"notice_type": "notify", "sub_type": "poke", "user_id": 1003, "target_id": 2002,
     "group_id": 188395693, "time": now - 480},
    {"notice_type": "group_ban", "sub_type": "ban", "user_id": 1005, "operator_id": 8888,
     "duration": 600, "group_id": 188395693, "time": now - 300},
    {"notice_type": "group_ban", "sub_type": "lift_ban", "user_id": 1005, "operator_id": 8888,
     "duration": 0, "group_id": 188395693, "time": now - 180},
    {"notice_type": "group_decrease", "sub_type": "leave", "user_id": 1007,
     "group_id": 188395693, "time": now - 60},
]

events = []
for raw in raw_events:
    ev = normalize_notice(raw, sid=SID, self_id=SELF_ID)
    if ev:
        events.append(ev)

check("6 个 notice 全部归一化成功", len(events) == 6, f"实际 {len(events)}")

name_map = {"1001": "张三", "1003": "王五", "1005": "赵六", "1007": "孙七",
            "2002": "李四", "8888": "管理员A"}
for ev in events:
    if ev.actor:
        ev.actor_name = name_map.get(ev.actor, "")
    if ev.target:
        ev.target_name = name_map.get(ev.target, "")

summary = render_env_summary(events, max_per_type=6, max_chars=800)
print(f"\n  渲染结果: {summary}\n")

check("包含群动态时间前缀", "群动态" in summary)
check("戳一戳按 target 合并（只出现一次'戳了戳'）",
      summary.count("戳了戳") == 1, f"出现 {summary.count('戳了戳')} 次")
check("戳一戳含两人（张三和王五）", "张三" in summary and "王五" in summary)
check("禁言显示已解除", "(已解除)" in summary, summary)
check("禁言不重复显示独立'解除禁言'条目",
      "解除禁言" not in summary, summary)
check("退群被渲染", "退群" in summary or "离开群聊" in summary)
check("摘要长度合理（<400 字符）", len(summary) < 400, f"{len(summary)} 字符")


print("\n=== 场景 2：撤回改写 ===")

locator = MessageLocator(adapter=FakeAdapter(), self_id=SELF_ID)
for mid, sender, name, text in [
    ("100", "1001", "张三", "这是要被撤回的消息"),
    ("101", "1003", "王五", "这条不会撤回"),
    ("102", "1005", "赵六", "管理员撤回我"),
]:
    run(locator.record_incoming(mid, sender, name, text))

run(locator.archive_recall(
    sid=SID, message_id="100", sender="1001", sender_name="张三",
    text="这是要被撤回的消息", ts=now, operator="1001",
    operator_name="张三", sub_type="", is_group=True,
))
run(locator.archive_recall(
    sid=SID, message_id="102", sender="1005", sender_name="赵六",
    text="管理员撤回我", ts=now, operator="8888",
    operator_name="管理员A", sub_type="", is_group=True,
))

req = LLMRequest()
req.user_prompt = [
    Prompt("这是要被撤回的消息", name="message", source="system"),
    Prompt("这条不会撤回", name="message", source="system"),
    Prompt("管理员撤回我", name="message", source="system"),
]

rewriter = RecallRewriter(locator=locator)
run(rewriter.rewrite(sid=SID, req=req, recall_rewrite=True, recall_show_content=True))

c0 = req.user_prompt[0].content
c1 = req.user_prompt[1].content
c2 = req.user_prompt[2].content
print(f"  msg100: {c0!r}")
print(f"  msg101: {c1!r}")
print(f"  msg102: {c2!r}")

check("被撤回消息追加了标注", "[已撤回｜" in c0, c0)
check("本人撤回措辞正确", "撤回了" in c0 and "张三" in c0, c0)
check("未撤回消息无标注", "[已撤回｜" not in c1, c1)
check("管理员撤回标注含身份", "（管理员）" in c2 or "(管理员)" in c2, c2)
check("原文保留（不删除）", "这是要被撤回的消息" in c0)
check("撤回归档已清空（已处理）", len(run(locator.get_recalls(SID))) == 0)


print("\n=== 场景 3：贴表情标注 ===")
_EMOJI_STORE.clear()
run(locator.record_incoming("200", "1003", "王五", "被贴表情的消息"))

archive_emoji_like(sid=SID, message_id="200", message_text="被贴表情的消息",
                   emoji_ids=["4"], count=1, operator="1001", operator_name="张三", ts=now)
archive_emoji_like(sid=SID, message_id="200", message_text="被贴表情的消息",
                   emoji_ids=["9"], count=2, operator="1005", operator_name="赵六", ts=now)

item = _EMOJI_STORE[SID][0]
print(f"  归档标注: {item['annotation']!r}")
check("同一消息合并为一条归档", len(_EMOJI_STORE[SID]) == 1)
check("多人的表情都保留", "张三" in item["annotation"] and "赵六" in item["annotation"])
check("表情带 ID（可回贴）", "ID: 4" in item["annotation"] and "ID: 9" in item["annotation"])

req2 = LLMRequest()
req2.user_prompt = [Prompt("被贴表情的消息", name="message", source="system")]
run(rewrite_emoji_like(sid=SID, req=req2, enabled=True))
print(f"  改写后: {req2.user_prompt[0].content!r}")
check("贴表情标注追加成功", "[贴表情｜" in req2.user_prompt[0].content)
check("贴表情归档已清空", not _EMOJI_STORE.get(SID))


print("\n=== 场景 4：缓存不变量 ===")
p = Prompt("[群动态·测试]", name="qq_social_env", persist=False)
check("环境摘要 persist=False", p.persist is False)
check("环境摘要 name 不与框架保留名冲突",
      p.name not in ("sessions", "chat_env", "time", "memory"))


print("\n=== 场景 5：事件存储与过期 ===")


async def store_scenario():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        s = EventStore(d, max_events=20, window_seconds=1800, ttl_hours=24)
        for i in range(25):
            await s.add(QQEvent(eid=str(i), etype="poke", sid=SID,
                                ts=int(time.time()) - 100 + i))
        evs = await s.get_events(SID)
        recent = await s.get_events(SID, since_ts=time.time() - 200)
        pokes = await s.get_events(SID, etypes={"poke"})
        none_evs = await s.get_events(SID, etypes={"recall"})
        return len(evs), len(recent), len(pokes), len(none_evs)


n_evs, n_recent, n_pokes, n_none = run(store_scenario())
print(f"  LRU 后: {n_evs} 条 | 时间过滤: {n_recent} | 类型过滤: {n_pokes} | 空类型: {n_none}")
check("LRU 上限生效（<=20）", n_evs <= 20, f"{n_evs}")
check("时间过滤生效", n_recent <= n_evs)
check("类型过滤生效", n_pokes == n_evs)
check("不存在的类型返回空", n_none == 0)


print("\n=== 场景 6：超长事件列表的踢旧淘汰 ===")
now2 = int(time.time())
many = []
for i in range(40):
    e = QQEvent(eid=str(i), etype="poke", sid=SID, ts=now2 - 4000 + i * 10,
                actor=f"100{i}", actor_name=f"昵称很长的用户{i}",
                target="2002", target_name="李四")
    many.append(e)
out = render_env_summary(many, max_per_type=6, max_chars=150)
print(f"  输入 40 条 -> 输出 {len(out)} 字符")
print(f"  {out}\n")
check("输出非空", len(out) > 0)
check("严格不超字符上限（或已删到仅剩 1 条）",
      len(out) <= 150 or len(many) <= 1, f"{len(out)} 字符")


print("\n" + "=" * 60)
print(f"场景验证: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部场景通过 ===")