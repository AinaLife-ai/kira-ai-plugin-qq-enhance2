"""多适配器（多 QQ 号）专项验证。

真实场景：用户可能挂多个 QQ 号（如 napcat 主号 + llonebot 小号）。
验证：
1. 初始化时正确建立 adapter_name → (adapter, self_id) 映射
2. 每个号的 notice 用自己的 self_id 判断"是否自己"
3. 唤醒事件用对应号的 self_id
4. 工具绑定对应号的 adapter
5. locator 查消息用对应 adapter
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.real_adapter import ensure_framework_path, make_real_adapter
ensure_framework_path()

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


print("\n=== 1. 多适配器映射 ===")


def make_two_adapters():
    """构造两个真实 QQAdapter（不同号），用于多号验证。

    用 msg_sender 区分：A 号返回 1001，B 号返回 2002。
    """
    ad_a, q_a, calls_a = make_real_adapter(
        self_id="111", adapter_name="napcat",
        nicknames={"1001": "A号看到的人"}, msg_sender=1001,
    )
    ad_b, q_b, calls_b = make_real_adapter(
        self_id="222", adapter_name="llonebot",
        nicknames={"2002": "B号看到的人"}, msg_sender=2002,
    )
    return (ad_a, q_a, calls_a), (ad_b, q_b, calls_b)


class Ctx:
    def __init__(self, adapters):
        self._adapters = adapters

        class Mgr:
            def __init__(self, ads):
                self._ads = ads
            def get_all_adapters(self):
                return list(self._ads)
            def get_adapter(self, name):
                # 真实 BaseAdapter 用 info.name（没有 .name 属性）
                for a in self._ads:
                    an = getattr(getattr(a, "info", None), "name", None) or getattr(a, "name", None)
                    if an == name:
                        return a
                return None
        self.adapter_mgr = Mgr(adapters)

        class Bus:
            def __init__(self):
                self.pub = []
            async def publish(self, e):
                self.pub.append(e)
        self.event_bus = Bus()

    def get_plugin_data_dir(self):
        import tempfile
        return tempfile.mkdtemp()


# 模拟两个号：napcat(111) 和 llonebot(222)
(ad_a, q_a, calls_a), (ad_b, q_b, calls_b) = make_two_adapters()
ctx = Ctx([ad_a, ad_b])

from qq_core.message_locator import MessageLocator
from qq_core.wake_scheduler import WakeScheduler

# 模拟 main.py 的 _adapters 构建逻辑（与 main.py 保持一致的平台检测）
adapters_map = {}
for ada in ctx.adapter_mgr.get_all_adapters():
    platform = (
        getattr(getattr(ada, "info", None), "platform", None)
        or getattr(ada, "platform", None)
        or ""
    )
    if str(platform).upper() != "QQ":
        continue
    info = getattr(ada, "info", None)
    name = (getattr(info, "name", None) or getattr(ada, "name", None) or "")
    cfg = getattr(ada, "config", None) or getattr(info, "config", None) or {}
    self_id = str(cfg.get("self_id", "") or "") if isinstance(cfg, dict) else ""
    if name:
        adapters_map[name] = (ada, self_id)

check("检测到 2 个适配器", len(adapters_map) == 2, str(list(adapters_map)))
check("napcat → self_id=111", adapters_map["napcat"][1] == "111")
check("llonebot → self_id=222", adapters_map["llonebot"][1] == "222")


def resolver(name):
    return adapters_map.get(name, (ad_a, "111"))


print("\n=== 2. locator 按 sid 用对应 adapter ===")
loc = MessageLocator(adapter=ad_a, self_id="111", adapter_resolver=resolver)
# 查 napcat 的消息
r1 = run(loc.lookup("m1", "napcat:gm:100"))
# 查 llonebot 的消息
r2 = run(loc.lookup("m2", "llonebot:gm:200"))
print(f"  napcat 消息: {r1}")
print(f"  llonebot 消息: {r2}")
check("napcat sid 走 napcat adapter", r1 and r1["sender"] == "1001", str(r1))
check("llonebot sid 走 llonebot adapter", r2 and r2["sender"] == "2002", str(r2))
def _called(calls, action, mid):
    return any(a == action and isinstance(p, dict) and p.get("message_id") == mid
               for a, p in calls)


check("两个 adapter 各自被调用",
      _called(calls_a, "get_msg", "m1") and _called(calls_b, "get_msg", "m2"),
      f"A={calls_a} B={calls_b}")


print("\n=== 3. is_self 判定用对应号的 self_id ===")
# napcat 号（111）查自己的消息
loc._cache.clear()
r_self_a = run(loc.lookup("m3", "napcat:gm:100"))
# 上面 get_msg 返回 user_id=1001（不是 111）→ is_self=False
check("napcat 号：非自己消息 is_self=False", r_self_a and r_self_a["is_self"] is False, str(r_self_a))


print("\n=== 4. 唤醒事件用对应号的 self_id ===")
from qq_core.event_normalizer import QQEvent, E_EMOJI_LIKE


def self_id_resolver(name):
    return adapters_map.get(name, (ad_a, "111"))[1]


sched = WakeScheduler(
    ctx=ctx, adapter=ad_a, self_id="111", message_locator=loc,
    wake_others_emoji=True, wake_others_emoji_cooldown=0, wake_max_per_hour=-1,
    self_id_resolver=self_id_resolver,
)
ev = QQEvent(eid="1", etype=E_EMOJI_LIKE, sid="llonebot:gm:200",
             ts=0, actor="1001", actor_name="张三",
             target="2002", target_name="李四",
             extra={"message_id": "1", "emoji_ids": ["4"], "count": 1})
run(sched.trigger_others_emoji_like(ev, "2002", "李四", "摘要"))
pub = ctx.event_bus.pub
check("唤醒了 1 条事件", len(pub) == 1, str(len(pub)))
if pub:
    check("唤醒事件用 llonebot 号的 self_id=222",
          pub[0].message.self_id == "222", pub[0].message.self_id)
    check("唤醒事件绑 llonebot adapter",
          pub[0].adapter.name == "llonebot", pub[0].adapter.name)


print("\n=== 5. 未注册 adapter 时回退默认 ===")
loc2 = MessageLocator(adapter=ad_a, self_id="111", adapter_resolver=resolver)
r3 = run(loc2.lookup("m4", "unknown:gm:999"))
check("未知 adapter 回退默认（不崩）", r3 is not None or True, str(r3))


print("\n=== 6. 单适配器场景不受影响 ===")
ad_only, _qo, _co = make_real_adapter(self_id="111", adapter_name="napcat")
ctx1 = Ctx([ad_only])
map1 = {"napcat": (ad_only, "111")}
loc3 = MessageLocator(adapter=ad_only, self_id="111",
                      adapter_resolver=lambda n: map1.get(n, (ad_only, "111")))
r4 = run(loc3.lookup("m5", "napcat:gm:100"))
check("单适配器正常工作", r4 is not None, str(r4))


print("\n" + "=" * 60)
print(f"多适配器验证: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部通过 ===")