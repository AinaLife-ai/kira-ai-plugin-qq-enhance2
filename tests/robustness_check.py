"""坏数据容错验证：配置文件/落盘数据/协议端返回异常时的健壮性。

真实场景：
1. 用户手改 plugin_data 里的 json（填错类型）
2. 协议端返回字段类型不符（time 是字符串、duration 是 None）
3. 落盘文件损坏（截断的 JSON、条目不是 dict）

核心要求：**任何单条坏数据都不能让整个功能崩掉**。
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_core import _install_framework_stubs  # noqa

from qq_core.event_normalizer import QQEvent, E_BAN, E_POKE, E_EMOJI_LIKE
from qq_core.event_store import EventStore
from qq_core.env_renderer import render_env_summary
from qq_core.fmt import safe_int, safe_float

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


print("\n=== 1. 落盘文件损坏 ===")


async def corrupted_files():
    with tempfile.TemporaryDirectory() as d:
        now = int(time.time())
        # 各种损坏情况
        files = {
            "bad1.json": "{not valid json",                      # 截断 JSON
            "bad2.json": json.dumps({"sid": None, "events": []}),  # 无 sid
            "bad3.json": json.dumps({"sid": "s3", "events": "not-a-list"}),  # events 不是 list
            "bad4.json": json.dumps({"sid": "s4", "events": [None, 1, "str"]}),  # 条目不是 dict
            "bad5.json": json.dumps({"sid": "s5", "events": [
                {"eid": "a", "etype": "poke", "sid": "s5", "ts": "abc"},   # ts 坏
                {"eid": "b", "etype": "poke", "sid": "s5", "ts": now},     # 正常
            ]}),
            "bad6.json": json.dumps({"sid": "s6", "events": [
                {"eid": "c", "etype": "poke", "sid": "s6", "ts": now, "extra": "not-dict"},
            ]}),
            "good.json": json.dumps({"sid": "s7", "events": [
                {"eid": "d", "etype": "poke", "sid": "s7", "ts": now},
            ]}),
        }
        for fn, content in files.items():
            with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
                f.write(content)

        store = EventStore(d, max_events=20)
        await store.load_all()
        return {sid: len(await store.get_events(sid)) for sid in
                ("s3", "s4", "s5", "s6", "s7")}


res = run(corrupted_files())
print(f"  各 sid 加载结果: {res}")
check("损坏 JSON 不影响其他文件", res["s7"] == 1, str(res))
check("坏 ts 条目被跳过（保留正常的）", res["s5"] == 1, f"s5={res['s5']}")
check("非 dict 条目被跳过", res["s4"] == 0, f"s4={res['s4']}")
check("extra 非 dict 自动归一化", res["s6"] == 1, f"s6={res['s6']}")


print("\n=== 2. 协议端字段类型异常 ===")
now = int(time.time())
weird_cases = [
    ("duration 是字符串", QQEvent("1", E_BAN, "s", now - 100, actor="1", target="2",
                                  extra={"duration": "600"})),
    ("duration 是 None", QQEvent("2", E_BAN, "s", now - 100, actor="1", target="2",
                                 extra={"duration": None})),
    ("duration 是 'abc'", QQEvent("3", E_BAN, "s", now - 100, actor="1", target="2",
                                  extra={"duration": "abc"})),
    ("duration 缺失", QQEvent("4", E_BAN, "s", now - 100, actor="1", target="2")),
    ("emoji_ids 是字符串", QQEvent("5", E_EMOJI_LIKE, "s", now - 100, actor="1",
                                   extra={"emoji_ids": "not-a-list", "count": 1})),
    ("count 是字符串", QQEvent("6", E_EMOJI_LIKE, "s", now - 100, actor="1",
                               extra={"emoji_ids": ["4"], "count": "2"})),
]
for desc, ev in weird_cases:
    try:
        out = render_env_summary([ev])
        check(f"渲染: {desc}", isinstance(out, str), f"返回 {type(out).__name__}")
    except Exception as e:
        check(f"渲染: {desc}", False, f"{type(e).__name__}: {e}")


print("\n=== 3. 归一化器吃异常 payload ===")
from qq_core.event_normalizer import normalize_notice

bad_payloads = [
    ("likes 是字符串", {"notice_type": "group_msg_emoji_like", "user_id": 1,
                        "message_id": 1, "likes": "bad", "group_id": 1, "time": now}),
    ("likes 含 None", {"notice_type": "group_msg_emoji_like", "user_id": 1,
                       "message_id": 1, "likes": [None], "group_id": 1, "time": now}),
    ("count 是 'abc'", {"notice_type": "group_msg_emoji_like", "user_id": 1, "message_id": 1,
                        "likes": [{"emoji_id": "4", "count": "abc"}], "group_id": 1, "time": now}),
    ("time 是字符串", {"notice_type": "notify", "sub_type": "poke", "user_id": 1,
                       "target_id": 2, "group_id": 1, "time": "not-a-number"}),
    ("time 是 None", {"notice_type": "notify", "sub_type": "poke", "user_id": 1,
                      "target_id": 2, "group_id": 1, "time": None}),
    ("duration 是 'abc'", {"notice_type": "group_ban", "sub_type": "ban", "user_id": 1,
                           "operator_id": 2, "duration": "abc", "group_id": 1, "time": now}),
]
for desc, raw in bad_payloads:
    try:
        ev = normalize_notice(raw, sid="napcat:gm:1", self_id="99")
        check(f"归一化: {desc}", True, f"→ {ev.etype if ev else None}")
    except Exception as e:
        check(f"归一化: {desc}", False, f"{type(e).__name__}: {e}")


print("\n=== 4. 极端配置值 ===")
weird_cfg = ["abc", None, "", [], {}, "  ", "20abc", "1e5", True, False, -1, 0, 1e9]
for v in weird_cfg:
    try:
        i = safe_int(v, 10)
        f = safe_float(v, 1.0)
        ok = isinstance(i, int) and isinstance(f, float)
        check(f"配置值 {v!r}", ok, f"int={i} float={f}")
    except Exception as e:
        check(f"配置值 {v!r}", False, f"{type(e).__name__}: {e}")


print("\n=== 5. 空/无字段事件渲染 ===")
bare_events = [
    QQEvent("1", E_POKE, "s", now),                                    # 全空
    QQEvent("2", E_BAN, "s", now),                                     # 全空
    QQEvent("3", E_EMOJI_LIKE, "s", now),                              # 全空
    QQEvent("4", "unknown_type", "s", now),                            # 未知类型
]
for ev in bare_events:
    try:
        out = render_env_summary([ev])
        check(f"渲染空字段事件 {ev.etype}", isinstance(out, str), repr(out))
    except Exception as e:
        check(f"渲染空字段事件 {ev.etype}", False, f"{type(e).__name__}: {e}")


print("\n=== 6. 超长时间跨度 ===")
huge = [
    QQEvent("1", E_POKE, "s", 0),                    # 1970 年
    QQEvent("2", E_POKE, "s", now),                  # 现在
    QQEvent("3", E_POKE, "s", now + 86400 * 365),    # 一年后（时钟异常）
]
try:
    out = render_env_summary(huge)
    check("时钟异常不崩", isinstance(out, str), out[:60])
except Exception as e:
    check("时钟异常不崩", False, f"{type(e).__name__}: {e}")


print("\n" + "=" * 60)
print(f"坏数据容错验证: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部通过 ===")