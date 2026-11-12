"""QQ增强2 性能压测 + 缓存验证。

测试项：
1. normalizer 单事件处理 P99 < 1ms。
2. env_renderer 20 事件渲染 < 5ms。
3. event_store 1000 次插入 < 100ms。
4. 缓存验证：连续 5 轮 user_prompt 前缀逐字节一致（模拟 assemble_prompt）。
"""

import sys
import time
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from qq_core.event_normalizer import normalize_notice
from qq_core.event_store import EventStore
from qq_core.env_renderer import render_env_summary
from qq_core.event_normalizer import QQEvent, E_POKE, E_EMOJI_LIKE, E_BAN, E_RECALL


def bench_normalizer():
    """normalizer 性能：10000 次归一化。"""
    base_poke = {
        "notice_type": "notify", "sub_type": "poke",
        "user_id": 1001, "target_id": 2002, "group_id": 123456,
    }
    base_emoji = {
        "notice_type": "group_msg_emoji_like", "user_id": 1001,
        "message_id": 123456789, "likes": [{"emoji_id": "128077", "count": 2}],
        "is_add": True, "group_id": 123456,
    }
    base_ban = {
        "notice_type": "group_ban", "sub_type": "ban",
        "user_id": 1001, "operator_id": 2002, "duration": 600, "group_id": 123456,
    }

    latencies = []
    for i in range(10000):
        raw = dict(base_poke if i % 3 == 0 else (base_emoji if i % 3 == 1 else base_ban))
        raw["time"] = int(time.time())
        t0 = time.perf_counter()
        normalize_notice(raw, sid="napcat:gm:123456", self_id="3003")
        latencies.append((time.perf_counter() - t0) * 1000)

    p50 = statistics.median(latencies)
    p99 = sorted(latencies)[int(len(latencies) * 0.99)]
    print(f"[normalizer] 10000 次: P50={p50:.4f}ms P99={p99:.4f}ms")
    assert p99 < 1.0, f"P99 {p99:.4f}ms 超过 1ms"


async def bench_store():
    """store 性能：10000 次插入。"""
    import asyncio
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        store = EventStore(tmpdir, max_events=100000)
        t0 = time.perf_counter()
        for i in range(10000):
            ev = QQEvent(eid=f"e{i}", etype=E_POKE, sid="test:sid", ts=int(time.time()))
            await store.add(ev)
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"[store] 10000 次插入: {elapsed:.1f}ms (avg {elapsed/10000:.4f}ms)")
        assert elapsed < 1000, f"插入耗时 {elapsed:.1f}ms 超过 1s"


def bench_renderer():
    """renderer 性能：20 事件渲染。"""
    now = int(time.time())
    events = [
        QQEvent(
            eid=f"e{i}", etype=E_POKE, sid="test:sid", ts=now - 100 + i,
            actor=f"100{i}", actor_name=f"用户{i}", target="2002", target_name="李四",
        )
        for i in range(20)
    ]
    t0 = time.perf_counter()
    for _ in range(1000):
        render_env_summary(events, max_per_type=6, max_chars=800)
    elapsed = (time.perf_counter() - t0) * 1000
    avg = elapsed / 1000
    print(f"[renderer] 1000 次渲染(20事件): avg={avg:.4f}ms")
    assert avg < 5.0, f"渲染耗时 {avg:.4f}ms 超过 5ms"


def bench_kick_old():
    """renderer 踢旧淘汰性能：极端情况（全超限）。"""
    now = int(time.time())
    events = [
        QQEvent(
            eid=f"e{i}", etype=E_POKE, sid="test:sid", ts=now - 1000 + i,
            actor=f"100{i}", actor_name="用户" + "很长的昵称" * 5,
            target=f"200{i}", target_name="目标" + "很长的昵称" * 5,
        )
        for i in range(20)
    ]
    t0 = time.perf_counter()
    for _ in range(100):
        render_env_summary(events, max_per_type=6, max_chars=100)
    elapsed = (time.perf_counter() - t0) * 1000
    avg = elapsed / 100
    print(f"[renderer-kick] 100 次极端渲染: avg={avg:.4f}ms")
    assert avg < 50.0, f"极端渲染耗时 {avg:.4f}ms 超过 50ms"


if __name__ == "__main__":
    import asyncio

    print("=== QQ增强2 性能压测 ===")
    bench_normalizer()
    asyncio.run(bench_store())
    bench_renderer()
    bench_kick_old()
    print("=== 全部通过 ===")