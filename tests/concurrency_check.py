"""并发与生命周期审计：多群并发、热重载、长时间运行。

这些是功能测试覆盖不到的方向：
1. 多群同时来事件 → 累加器/归档的竞态
2. 插件 terminate 后重新 initialize → 全局状态是否干净
3. 长时间运行 → 内存/文件增长是否有界
4. 并发改写 → 同一 prompt 被多协程改写的安全性
"""

import asyncio
import gc
import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_core import _install_framework_stubs  # noqa

from qq_core.event_normalizer import QQEvent, E_POKE, E_EMOJI_LIKE
from qq_core.event_store import EventStore
from qq_core.env_renderer import render_env_summary
from qq_core.emoji_rewriter import archive_emoji_like, rewrite_emoji_like, _EMOJI_STORE
from qq_core.message_locator import MessageLocator

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
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


print("\n=== 并发 1：多群同时写入 event_store ===")


async def concurrent_store():
    with tempfile.TemporaryDirectory() as d:
        store = EventStore(d, max_events=20)

        async def writer(sid, n):
            for i in range(n):
                await store.add(QQEvent(eid=f"{sid}-{i}", etype="poke", sid=sid,
                                        ts=int(time.time()) + i))

        await asyncio.gather(*[writer(f"napcat:gm:{g}", 50) for g in range(10)])
        return [len(await store.get_events(f"napcat:gm:{g}")) for g in range(10)]


counts = run(concurrent_store())
print(f"  每群事件数: {counts}")
check("并发写入后每群恰好 20 条（LRU 无竞态）", all(c == 20 for c in counts), str(counts))


print("\n=== 并发 2：多群同时贴表情归档 ===")


async def concurrent_emoji():
    _EMOJI_STORE.clear()
    gate = asyncio.Event()

    async def archiver(sid, n):
        await gate.wait()
        for i in range(n):
            archive_emoji_like(sid=sid, message_id=f"m{i}", message_text=f"text{i}",
                               emoji_ids=["4"], count=1, operator=f"100{i}",
                               operator_name=f"用户{i}", ts=int(time.time()))

    tasks = [asyncio.create_task(archiver(f"napcat:gm:{g}", 80)) for g in range(10)]
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(*tasks)
    return {sid: len(v) for sid, v in _EMOJI_STORE.items()}


sizes = run(concurrent_emoji())
print(f"  每群归档数: {sorted(sizes.values())}")
check("并发归档后每群 ≤ 50（LRU 上限）", all(s <= 50 for s in sizes.values()), str(sizes))
check("10 个群都有归档（无丢失）", len(sizes) == 10, str(len(sizes)))


print("\n=== 并发 3：同一 prompt 被并发改写 ===")


async def concurrent_rewrite():
    _EMOJI_STORE.clear()
    archive_emoji_like(sid="s", message_id="m1", message_text="消息内容",
                       emoji_ids=["4"], count=1, operator="1001",
                       operator_name="张三", ts=int(time.time()))

    from core.provider import LLMRequest
    from core.prompt_manager import Prompt

    req = LLMRequest()
    req.user_prompt = [Prompt("消息内容", name="message", source="system")]
    await asyncio.gather(*[
        rewrite_emoji_like(sid="s", req=req, enabled=True) for _ in range(5)
    ])
    return req.user_prompt[0].content


content = run(concurrent_rewrite())
print(f"  改写后: {content!r}")
check("并发改写不重复追加标注", content.count("↑该条消息被贴了") == 1, content)


print("\n=== 生命周期 1：store 启动/停止多轮 ===")


async def lifecycle_store():
    with tempfile.TemporaryDirectory() as d:
        results = []
        for cycle in range(3):
            store = EventStore(d, max_events=20)
            await store.start()
            await store.add(QQEvent(eid=f"c{cycle}", etype="poke", sid="s",
                                    ts=int(time.time())))
            await store.stop()
            s2 = EventStore(d, max_events=20)
            await s2.load_all()
            results.append(len(await s2.get_events("s")))
        return results


lifec = run(lifecycle_store())
print(f"  三轮启停后事件数: {lifec}")
check("每轮 stop 都落盘（数据保留）", all(n >= 1 for n in lifec), str(lifec))


print("\n=== 生命周期 2：全局状态清理 ===")
from qq_core.emoji_rewriter import clear_emoji_archive
_EMOJI_STORE.clear()
archive_emoji_like(sid="s1", message_id="m", message_text="x",
                   emoji_ids=["4"], count=1, operator="1", operator_name="A", ts=1)
check("归档写入成功", len(_EMOJI_STORE) == 1)
clear_emoji_archive("s1")
check("clear_emoji_archive 清空指定 sid", "s1" not in _EMOJI_STORE)


print("\n=== 生命周期 3：MessageLocator 实例隔离 ===")
loc1 = MessageLocator(adapter=None, self_id="1")
loc2 = MessageLocator(adapter=None, self_id="1")
run(loc1.record_incoming("m1", "1001", "张三", "消息"))
check("实例 1 有数据", run(loc1.lookup("m1")) is not None)
check("实例 2 无数据（状态隔离）", run(loc2.lookup("m1")) is None)


print("\n=== 长时间运行：内存增长（10000 事件） ===")


async def memory_test():
    with tempfile.TemporaryDirectory() as d:
        store = EventStore(d, max_events=20)
        tracemalloc.start()
        base = tracemalloc.get_traced_memory()[0]
        for i in range(10000):
            await store.add(QQEvent(eid=str(i), etype="poke",
                                    sid=f"napcat:gm:{i % 100}", ts=int(time.time())))
        gc.collect()
        peak = tracemalloc.get_traced_memory()[1]
        cur = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        total = sum(len(v) for v in store._store.values())
        return total, (cur - base) / 1024, (peak - base) / 1024


total, growth_kb, peak_kb = run(memory_test())
print(f"  10000 次写入：{total} 条事件，内存 {growth_kb:.1f}KB（峰值 {peak_kb:.1f}KB）")
check("总事件数受控（100 群 × 20 = 2000）", total <= 2000, str(total))
check("内存增长 < 2MB（有界）", growth_kb < 2048, f"{growth_kb:.1f}KB")


print("\n=== 长时间运行：归档 LRU 有界 ===")
_EMOJI_STORE.clear()
for i in range(500):
    archive_emoji_like(sid="stress", message_id=f"m{i}", message_text=f"t{i}",
                       emoji_ids=["4"], count=1, operator="1", operator_name="A", ts=i)
n = len(_EMOJI_STORE["stress"])
print(f"  500 次归档后保留: {n} 条")
check("归档 LRU 上限 50 生效", n <= 50, str(n))


print("\n=== 长时间运行：撤回归档有界 ===")


async def recall_growth():
    loc = MessageLocator(adapter=None, self_id="1")
    for i in range(500):
        await loc.archive_recall(
            sid="s", message_id=f"m{i}", sender="1001", sender_name="张三",
            text=f"text{i}", ts=int(time.time()), operator="1001",
            operator_name="张三", sub_type="", is_group=True,
        )
    return len(await loc.get_recalls("s"))


n_recall = run(recall_growth())
print(f"  500 次撤回后保留: {n_recall} 条")
check("撤回归档 LRU 上限 50 生效", n_recall <= 50, str(n_recall))


print("\n=== 长时间运行：落盘文件有界 ===")


async def file_growth():
    with tempfile.TemporaryDirectory() as d:
        store = EventStore(d, max_events=20, ttl_hours=24)
        for i in range(1000):
            await store.add(QQEvent(eid=str(i), etype="poke",
                                    sid=f"napcat:gm:{i % 200}", ts=int(time.time())))
        await store._persist_all()
        files = [f for f in os.listdir(d) if f.endswith(".json")]
        sizes = [os.path.getsize(os.path.join(d, f)) for f in files]
        return len(files), (max(sizes) if sizes else 0)


n_files, max_size = run(file_growth())
print(f"  200 群 × 1000 事件 → {n_files} 文件，最大 {max_size} 字节")
check("文件数 ≤ 群数（200）", n_files <= 200, str(n_files))
check("单文件 < 50KB", max_size < 51200, str(max_size))


print("\n=== 并发 4：读写同时进行 ===")


async def concurrent_read_write():
    with tempfile.TemporaryDirectory() as d:
        store = EventStore(d, max_events=100)
        errors = []

        async def writer():
            for i in range(100):
                try:
                    await store.add(QQEvent(eid=str(i), etype="poke", sid="s",
                                            ts=int(time.time())))
                except Exception as e:
                    errors.append(("w", type(e).__name__))

        async def reader():
            for _ in range(100):
                try:
                    evs = await store.get_events("s", since_ts=time.time() - 3600)
                    if evs:
                        render_env_summary(evs)
                except Exception as e:
                    errors.append(("r", type(e).__name__))

        await asyncio.gather(writer(), reader(), writer(), reader())
        return errors, len(await store.get_events("s"))


errs, final = run(concurrent_read_write())
print(f"  并发读写 400 次：错误 {len(errs)}，最终 {final} 条")
check("并发读写无异常", len(errs) == 0, str(errs[:3]))
check("最终数据 ≤100", final <= 100, str(final))


print("\n=== 并发 5：点赞累加器并发 ===")


async def concurrent_like():
    from qq_core.wake_scheduler import WakeScheduler
    from qq_core.event_normalizer import E_PROFILE_LIKE_SELF

    class Bus:
        def __init__(self):
            self.pub = []
        async def publish(self, e):
            self.pub.append(e)

    class A:
        platform = "QQ"
        class bot:
            @staticmethod
            async def send_action(*a, **k):
                return {"status": "ok", "data": {}}
        class info:
            name = "napcat"
            platform = "QQ"
            config = {"self_id": "1"}

    class M:
        @staticmethod
        def get_adapter(n):
            return A()

    class C:
        def __init__(self):
            self.event_bus = Bus()
            self.adapter_mgr = M()
        def get_plugin_data_dir(self):
            return None

    ctx = C()
    sched = WakeScheduler(ctx=ctx, adapter=A(), self_id="1", message_locator=None,
                          wake_profile_like=True, profile_like_merge_window_seconds=1,
                          wake_max_per_hour=-1)
    # 10 个用户并发点赞
    async def liker(uid):
        for _ in range(5):
            ev = QQEvent(eid="x", etype=E_PROFILE_LIKE_SELF, sid="napcat:dm:1",
                         ts=int(time.time()), actor=str(uid),
                         actor_name=f"用户{uid}", extra={"times": 1})
            await sched.on_profile_like(ev)

    await asyncio.gather(*[liker(1000 + i) for i in range(10)])
    await asyncio.sleep(1.5)  # 等结算
    await sched.terminate()
    return len(ctx.event_bus.pub)


n_pub = run(concurrent_like())
print(f"  10 用户 × 5 次点赞 → {n_pub} 条唤醒")
check("每个用户合并为 1 条（共 10 条）", n_pub == 10, str(n_pub))


print("\n" + "=" * 60)
print(f"并发/生命周期审计: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部通过 ===")