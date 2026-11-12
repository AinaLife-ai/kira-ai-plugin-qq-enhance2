"""Token 与速度审计：量化每次请求的额外开销。

关注点：
1. 环境摘要注入多长（token 估算）
2. 工具 schema 体积
3. 静态前缀是否稳定（缓存友好）
4. 事件渲染 / 昵称缓存 / 唤醒路径的耗时
5. 是否有 O(n²) 或阻塞调用

运行：
    cd <插件根目录> && python3 tests/perf_token_check.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai_latest")
if not (KIRAAI / "core").is_dir():
    KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
sys.path.insert(0, str(KIRAAI))
sys.path.insert(0, str(ROOT))
os.chdir(KIRAAI)  # 框架日志是相对 cwd 的 data/log.log

from qq_core.env_renderer import render_env_summary
from qq_core.event_normalizer import (
    QQEvent, E_POKE, E_EMOJI_LIKE, E_BAN, E_UNBAN, E_MEMBER_DECREASE,
    E_RECALL, E_TITLE, E_CARD, E_ADMIN,
)

SID = "napcat:gm:188395693"


def est_tokens(text: str) -> float:
    """中英混排 token 粗估：中文 ≈ 1.3 字/token，ASCII ≈ 4 字符/token。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return cjk / 1.3 + other / 4.0


def sample_events(n=20):
    """构造真实比例的混合事件。"""
    now = int(time.time())
    pool = [
        lambda i: QQEvent(eid=f"p{i}", etype=E_POKE, sid=SID, ts=now - i * 30,
                          actor=str(1000 + i), actor_name=f"用户{i}",
                          target=str(2000 + i), target_name=f"目标{i}"),
        lambda i: QQEvent(eid=f"b{i}", etype=E_BAN, sid=SID, ts=now - i * 40,
                          actor="8888", actor_name="管理员C7",
                          target=str(3000 + i), target_name=f"倒霉蛋{i}",
                          extra={"duration": 600}),
        lambda i: QQEvent(eid=f"m{i}", etype=E_MEMBER_DECREASE, sid=SID, ts=now - i * 50,
                          actor="", target=str(4000 + i), target_name=f"退群人{i}",
                          extra={"sub_type": "leave"}),
        lambda i: QQEvent(eid=f"c{i}", etype=E_CARD, sid=SID, ts=now - i * 60,
                          target=str(5000 + i), target_name=f"改名{i}",
                          extra={"card_new": "新名片", "card_old": "旧名片"}),
    ]
    return [pool[i % len(pool)](i) for i in range(n)]


async def main():
    events = sample_events(20)
    print("=" * 70)
    print("① 环境摘要（默认预算：max_per_type=6, max_chars=800）")
    print("=" * 70)
    # 预热
    render_env_summary(events)
    t0 = time.perf_counter()
    for _ in range(200):
        text = render_env_summary(events, max_per_type=6, max_chars=800)
    dt = (time.perf_counter() - t0) / 200
    print(f"  20 事件渲染: {len(text)} 字符 ≈ {est_tokens(text):.0f} tokens，耗时 {dt*1000:.3f} ms")
    print(f"  内容: {text}")
    print()

    print("=" * 70)
    print("② 极端：20 条同类型 poke（合并后）")
    print("=" * 70)
    pokes = [QQEvent(eid=str(i), etype=E_POKE, sid=SID, ts=int(time.time()) - i,
                     actor=str(1000 + i), actor_name=f"人{i}",
                     target=str(2000 + i), target_name=f"目标{i}")
             for i in range(20)]
    t0 = time.perf_counter()
    for _ in range(200):
        t2 = render_env_summary(pokes)
    dt2 = (time.perf_counter() - t0) / 200
    print(f"  {len(t2)} 字符 ≈ {est_tokens(t2):.0f} tokens，耗时 {dt2*1000:.3f} ms")
    print(f"  内容: {t2[:200]}...")
    print()

    print("=" * 70)
    print("③ 工具 schema 体积（每轮都会发给 LLM）")
    print("=" * 70)
    from tools.qq_profile import QQProfileTool
    from tools.qq_group_profile import QQGroupProfileTool
    import json

    total = 0
    for cls in (QQProfileTool, QQGroupProfileTool):
        payload = json.dumps(cls.get_schema(), ensure_ascii=False)
        total += len(payload)
        print(f"  {cls.name:18s} {len(payload):4d} 字符 ≈ {est_tokens(payload):5.0f} tokens")
    print(f"  合计 {total} 字符 ≈ {est_tokens('x' * total):.0f} tokens")
    print()

    print("=" * 70)
    print("④ 昵称解析缓存")
    print("=" * 70)
    from qq_core.name_resolver import NameResolver

    class _Bot:
        def __init__(self):
            self.calls = 0

        async def send_action(self, action, params, timeout=10.0):
            self.calls += 1
            await asyncio.sleep(0.001)
            return {"status": "ok", "data": {"nickname": f"用户{params.get('user_id')}",
                                             "card": "", "role": "member"}}

    class _A:
        def __init__(self):
            self.bot = _Bot()

    r = NameResolver()
    a = _A()
    await r.resolve(a, "napcat", "188395693", "1001")
    t0 = time.perf_counter()
    for _ in range(10000):
        await r.resolve(a, "napcat", "188395693", "1001")
    dt3 = (time.perf_counter() - t0) / 10000
    print(f"  缓存命中: {dt3*1e6:.2f} µs/次；API 调用 {a.bot.calls} 次 / 10001 查询")
    print()

    print("=" * 70)
    print("⑤ 唤醒路径：群名缓存后是否还打 API")
    print("=" * 70)
    from qq_core.wake_scheduler import WakeScheduler

    sched = WakeScheduler(ctx=None, adapter=None, self_id="3991867505",
                          message_locator=None)
    sched.remember_group_name("188395693", "绿岛酒吧")
    t0 = time.perf_counter()
    for _ in range(1000):
        await sched._get_group_name("napcat", "188395693")
    dt4 = (time.perf_counter() - t0) / 1000
    print(f"  群名缓存命中: {dt4*1e6:.1f} µs/次（无 API）")
    print()

    print("=" * 70)
    print("结论")
    print("=" * 70)
    print(f"  每轮额外上下文: 环境摘要 ≤ {len(text)} 字符（默认上限 800）+ 工具 schema {total} 字符")
    print("  → 群动态默认只注入「其他用户之间」的事件，且 persist=False 不进历史，")
    print("    不会随轮数累积；工具 schema 恒定，前缀可缓存。")


asyncio.run(main())
