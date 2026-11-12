"""测试用真实适配器工厂：真实 QQAdapter + mock send_action。

**为什么用真实类**：
第九轮实机验证发现，自造的 FakeAdapter 多给了 `platform` 属性，
把"真实 QQAdapter 没有 platform"这个 bug 藏了 8 轮。
所以测试必须尽量用真实框架类，只把最外层的网络 IO（send_action）替换掉。

用法：
    from tests.real_adapter import make_real_adapter
    adapter, queue, calls = make_real_adapter(self_id="3003003")
    # adapter 是真实 QQAdapter，adapter.im 是真实 QQIMCapability
    # calls 记录所有 send_action 调用，便于断言
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

KIRAAI_ROOT = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")


def ensure_framework_path():
    """确保框架路径在 sys.path 最前（真实类优先）。"""
    p = str(KIRAAI_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)


def _has_real_framework() -> bool:
    """检测真实框架是否可用。"""
    try:
        ensure_framework_path()
        import core.adapter.context  # noqa: F401
        import core.adapter.src.qq.qq  # noqa: F401
        return True
    except Exception:
        return False


class _StubAdapter:
    """无真实框架时的替身——**结构严格对齐真实 QQAdapter**。

    关键：只有 info.platform，**没有** platform 属性。
    （第九轮的教训：多给一个 platform 属性会掩盖真实 bug。）
    """

    def __init__(self, info):
        self.info = info
        self.config = info.config
        self.bot = _StubBot()
        self.im = _StubIM(self)


class _StubBot:
    def __init__(self):
        self.login_success_event = asyncio.Event()
        self.self_id = ""
        self.send_action = None      # 由 make_real_adapter 覆盖
        self.get_msg = None


class _StubIM:
    """模拟 QQIMCapability 的最小接口。"""

    def __init__(self, adapter):
        self.adapter = adapter

    async def _on_notice_message(self, msg):
        """极简版：把原始 msg 包成事件（仅用于无框架环境）。"""
        from core.chat.message_utils import KiraMessageEvent, KiraIMMessage
        from core.chat import MessageChain
        from core.chat.session import Group, User

        gid = msg.get("group_id")
        group = Group(group_id=str(gid)) if gid else None
        user = User(user_id=str(msg.get("user_id") or ""), nickname="None")
        ev = KiraMessageEvent(
            adapter=self.adapter.info,
            supported_elements=["text"],
            message=KiraIMMessage(
                timestamp=int(msg.get("time") or 0),
                message_id="None", group=group, sender=user,
                is_notice=True, is_mentioned=False,
                self_id=str(msg.get("self_id") or ""),
                chain=MessageChain(), raw_message=msg,
            ),
            timestamp=int(msg.get("time") or 0),
        )
        self.adapter.publish(ev)


def make_real_adapter(
    *,
    self_id: str = "3003003",
    adapter_name: str = "napcat",
    group_name: str = "绿岛酒吧",
    nicknames: dict | None = None,
    roles: dict | None = None,
    msg_text: str = "被撤回的消息内容",
    msg_sender: int = 1001,
    permission_mode: str = "deny_list",
    api_overrides: dict | None = None,
):
    """构造真实 QQAdapter（mock 底层 send_action）。

    Args:
        self_id: 机器人自己的 QQ 号。
        adapter_name: 适配器名。
        group_name: get_group_info 返回的群名。
        nicknames: {qq: 昵称} 映射（get_stranger_info 用）。
        roles: {qq: role} 映射（get_group_member_info 用）。
        msg_text: get_msg 返回的消息文本。
        msg_sender: get_msg 返回的发送者 QQ。
        permission_mode: 权限模式（deny_list 表示全放行）。
        api_overrides: {action: callable} 覆盖特定 API 的返回。

    Returns:
        (adapter, event_queue, calls)
        - adapter: 真实 QQAdapter 实例
        - event_queue: asyncio.Queue（事件流入这里）
        - calls: list[(action, params)] 记录所有 API 调用
    """
    ensure_framework_path()
    use_real = _has_real_framework()

    from core.adapter.adapter_info import AdapterInfo
    if use_real:
        from core.adapter.context import AdapterContext
        from core.adapter.src.qq.qq import QQAdapter
    else:
        AdapterContext = None
        QQAdapter = None

    info = AdapterInfo(
        enabled=True,
        adapter_id=adapter_name,
        name=adapter_name,
        platform="QQ",
        description="",
        config={
            "self_id": self_id,
            "permission_mode": permission_mode,
            "group_allow_list": [],
            "group_deny_list": [],
            "user_allow_list": [],
            "user_deny_list": [],
            "ws_uri": "ws://127.0.0.1:0",
            "ws_token": "",
            "bot_pid": self_id,
            "debug_mode": False,
        },
    )
    queue: asyncio.Queue = asyncio.Queue()
    if use_real:
        adapter = QQAdapter(AdapterContext(info=info, event_queue=queue))
    else:
        adapter = _StubAdapter(info)
        adapter.publish = lambda ev: queue.put_nowait(ev)

    calls: list[tuple[str, dict]] = []
    nick_map = nicknames or {}
    role_map = roles or {}
    overrides = api_overrides or {}

    async def fake_send_action(action, params=None, timeout=10.0):
        params = params or {}
        calls.append((action, params))

        # 用户自定义覆盖优先
        if action in overrides:
            fn = overrides[action]
            r = fn(params)
            if asyncio.iscoroutine(r):
                return await r
            return r

        if action == "get_group_info":
            return {"status": "ok", "data": {
                "group_id": int(params.get("group_id", 0)),
                "group_name": group_name,
            }}
        if action == "get_stranger_info":
            uid = str(params.get("user_id", ""))
            return {"status": "ok", "data": {
                "user_id": uid,
                "nickname": nick_map.get(uid, f"用户{uid}"),
                "sex": 1,
                "sign": "签名内容",
            }}
        if action == "get_group_member_info":
            uid = str(params.get("user_id", ""))
            return {"status": "ok", "data": {
                "user_id": uid,
                "nickname": nick_map.get(uid, f"用户{uid}"),
                "card": "",
                "title": "",
                "role": role_map.get(uid, "member"),
            }}
        if action == "get_msg":
            return {"status": "ok", "data": {
                "message_id": params.get("message_id"),
                "user_id": msg_sender,
                "time": int(time.time()),
                "sender": {"nickname": nick_map.get(str(msg_sender), f"用户{msg_sender}")},
                "message": [{"type": "text", "data": {"text": msg_text}}],
            }}
        return {"status": "ok", "data": {}}

    # 替换底层 IO（保留真实事件解析逻辑）
    adapter.bot.send_action = fake_send_action
    adapter.bot.get_msg = lambda mid: fake_send_action("get_msg", {"message_id": mid})
    adapter.bot.login_success_event.set()
    adapter.bot.self_id = self_id
    return adapter, queue, calls


async def drain_queue(queue: asyncio.Queue) -> list:
    """取出队列里所有待处理事件。"""
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def push_notice(adapter, queue: asyncio.Queue, raw: dict) -> list:
    """推送一条 notice 到真实适配器，返回解析后的事件列表。"""
    await adapter.im._on_notice_message(raw)
    return await drain_queue(queue)


def make_poke(user_id: int, target_id: int, group_id: int, self_id: str,
              ts: int | None = None) -> dict:
    """构造真实格式的戳一戳 notice（NapCat 风格，含 raw_info）。"""
    return {
        "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
        "time": ts if ts is not None else int(time.time()),
        "self_id": int(self_id), "user_id": user_id,
        "target_id": target_id, "group_id": group_id,
        "raw_info": [
            {"type": "qq", "uid": f"u_{user_id}"},
            {"type": "img", "src": ""},
            {"txt": "戳了戳", "type": "nor"},
            {"type": "qq", "uid": f"u_{target_id}"},
            {"txt": "", "type": "nor"},
        ],
    }


def make_emoji_like(user_id: int, message_id: int, group_id: int, self_id: str,
                    emoji_id: str = "4", count: int = 1, is_add: bool = True,
                    ts: int | None = None) -> dict:
    """构造真实格式的贴表情 notice。"""
    return {
        "post_type": "notice", "notice_type": "group_msg_emoji_like",
        "time": ts if ts is not None else int(time.time()),
        "self_id": int(self_id), "user_id": user_id,
        "group_id": group_id, "message_id": message_id,
        "likes": [{"emoji_id": emoji_id, "count": count}],
        "is_add": is_add,
    }


def make_ban(user_id: int, operator_id: int, group_id: int, self_id: str,
             duration: int = 600, lift: bool = False,
             ts: int | None = None) -> dict:
    """构造真实格式的禁言/解禁 notice。"""
    return {
        "post_type": "notice", "notice_type": "group_ban",
        "sub_type": "lift_ban" if lift else "ban",
        "time": ts if ts is not None else int(time.time()),
        "self_id": int(self_id), "user_id": user_id,
        "operator_id": operator_id,
        "duration": 0 if lift else duration,
        "group_id": group_id,
    }


def make_recall(user_id: int, operator_id: int, message_id: int, group_id: int,
                self_id: str, ts: int | None = None) -> dict:
    """构造真实格式的撤回 notice。"""
    return {
        "post_type": "notice", "notice_type": "group_recall",
        "time": ts if ts is not None else int(time.time()),
        "self_id": int(self_id), "user_id": user_id,
        "operator_id": operator_id,
        "message_id": message_id, "group_id": group_id,
    }


def make_decrease(user_id: int, group_id: int, self_id: str,
                  sub_type: str = "leave", operator_id: int | None = None,
                  ts: int | None = None) -> dict:
    """构造真实格式的退群/被踢 notice。"""
    d = {
        "post_type": "notice", "notice_type": "group_decrease",
        "sub_type": sub_type,
        "time": ts if ts is not None else int(time.time()),
        "self_id": int(self_id), "user_id": user_id,
        "group_id": group_id,
    }
    if operator_id is not None:
        d["operator_id"] = operator_id
    return d


def make_profile_like(operator_id: int, self_id: str, times: int = 1,
                      ts: int | None = None) -> dict:
    """构造真实格式的资料卡点赞 notice。"""
    return {
        "post_type": "notice", "notice_type": "notify", "sub_type": "profile_like",
        "time": ts if ts is not None else int(time.time()),
        "self_id": int(self_id), "operator_id": operator_id,
        "operator_nick": f"用户{operator_id}", "times": times,
    }