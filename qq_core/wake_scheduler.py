"""唤醒调度器：trigger 批次合并 + 点赞合并防抖 + 每小时上限 + 冷却。

设计要点：
- 唤醒事件 trigger() → 进入会话 buffer（由 Z/S/框架合并窗口统一 flush）。
- 点赞合并防抖：同 sid 同 operator 的点赞在 profile_like_merge_window_seconds（默认 10s）内合并为一条唤醒。
- 每小时上限：默认 60 次/sid；-1 = 无限制。
- 冷却：默认 0 秒（立即 trigger）；点赞合并窗口是唯一冷却机制。
- anti-harass 屏蔽名单检查：trigger 前读 data/ignore_list.json，避免唤醒被屏蔽用户。
- 性能：O(1) 插入/查询；点赞累加器 O(N) 遍历（N ≤ 10）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from core.logging_manager import get_logger
from core.chat import KiraMessageEvent, KiraIMMessage, MessageChain
from core.chat.session import Session, Group, User
from core.adapter.adapter_info import AdapterInfo

from .event_normalizer import (
    QQEvent, E_POKE, E_EMOJI_LIKE, E_PROFILE_LIKE_SELF, E_KICKED_SELF, E_TITLE_SELF,
    E_EMOJI_LIKE_SELF,
)

logger = get_logger("qq_enhance2.wake_scheduler", "cyan")


class WakeScheduler:
    """唤醒调度器。

    Attributes:
        ctx: PluginContext 实例。
        adapter: QQ 适配器实例。
        self_id: 机器人自己的 QQ 号。
        message_locator: MessageLocator 实例（用于反查被贴表情消息作者）。
        wake_self_emoji: 是否唤醒"自己消息被贴表情"。
        wake_profile_like: 是否唤醒"资料卡被赞"。
        wake_title: 是否唤醒"头衔授予"。
        wake_kicked: 是否唤醒"被移出群"。
        profile_like_merge_window_seconds: 点赞合并窗口（秒）。
        wake_quiet_hours: 静默时段（如 "23:00-08:00"）。
        wake_max_per_hour: 每 sid 每小时最大唤醒次数；-1=无限制。
    """

    def __init__(
        self,
        *,
        ctx,
        adapter,
        self_id: str,
        message_locator,
        wake_self_emoji: bool = True,
        wake_others_emoji: bool = False,
        others_emoji_cooldown: int = 20,
        wake_profile_like: bool = True,
        wake_title: bool = True,
        wake_kicked: bool = True,
        profile_like_merge_window_seconds: int = 10,
        wake_quiet_hours: str = "",
        wake_max_per_hour: int = 60,
    ):
        self.ctx = ctx
        self.adapter = adapter
        self.self_id = str(self_id) if self_id else ""
        self.message_locator = message_locator
        self.wake_self_emoji = wake_self_emoji
        self.wake_others_emoji = wake_others_emoji
        self.others_emoji_cooldown = others_emoji_cooldown
        self.wake_profile_like = wake_profile_like
        self.wake_title = wake_title
        self.wake_kicked = wake_kicked
        self.profile_like_merge_window_seconds = profile_like_merge_window_seconds
        self.wake_quiet_hours = wake_quiet_hours
        self.wake_max_per_hour = wake_max_per_hour

        # 点赞累加器：sid → {operator: {count, first_ts, last_ts}}
        self._like_accumulator: dict[str, dict[str, dict]] = {}
        # 每小时唤醒计数：sid → {hour_bucket: count}
        self._hourly_wake_count: dict[str, dict[int, int]] = {}
        # 他人贴表情唤醒冷却：sid → 上次唤醒时间
        self._others_emoji_wake_ts: dict[str, float] = {}
        # anti-harass 屏蔽名单缓存：{qq: expire_ts}
        self._harass_blocklist: dict[str, float] = {}
        self._last_blocklist_load: float = 0.0

    async def should_wake(self, event: QQEvent) -> bool:
        """判断是否应该唤醒（trigger）。

        Args:
            event: 归一化后的事件。

        Returns:
            True = 应该唤醒；False = 不唤醒（只入 store）。
        """
        now = time.time()

        # 静默时段检查
        if self._in_quiet_hours(now):
            return False

        # 每小时上限检查
        if not self._check_hourly_limit(event.sid):
            return False

        # 按事件类型判定
        if event.etype == E_EMOJI_LIKE_SELF:
            return self.wake_self_emoji
        elif event.etype == E_PROFILE_LIKE_SELF:
            return self.wake_profile_like
        elif event.etype == E_TITLE_SELF:
            return self.wake_title
        elif event.etype == E_KICKED_SELF:
            return self.wake_kicked
        elif event.etype == E_EMOJI_LIKE:
            # 贴表情：先判断是否给自己贴
            if self.wake_self_emoji and await self._is_self_message(event):
                return True
            # 他人贴表情：检查配置 + 冷却
            if self.wake_others_emoji:
                last = self._others_emoji_wake_ts.get(event.sid, 0)
                if now - last >= self.others_emoji_cooldown:
                    return True
            return False
        return False

    def should_wake_now(self, event: QQEvent) -> bool:
        """同步版唤醒判定（不递增每小时计数，仅供事件处理时快速判断）。

        与 should_wake 的区别：不检查每小时上限（上限在 _trigger_text 时统一计数），
        只做静默时段 + 冷却判定。

        Args:
            event: 归一化后的事件。

        Returns:
            True = 应该唤醒。
        """
        now = time.time()

        # 静默时段检查
        if self._in_quiet_hours(now):
            return False

        if event.etype == E_EMOJI_LIKE:
            if not self.wake_others_emoji:
                return False
            last = self._others_emoji_wake_ts.get(event.sid, 0)
            return (now - last) >= self.others_emoji_cooldown

        return False

    async def trigger_others_emoji_like(self, event: QQEvent, message_author: str, message_author_name: str, msg_excerpt: str):
        """触发"他人消息被贴表情"唤醒（默认冷却 20 秒）。

        Args:
            event: 贴表情事件。
            message_author: 被贴消息作者 QQ。
            message_author_name: 被贴消息作者昵称。
            msg_excerpt: 被贴消息摘要。
        """
        now = time.time()
        self._others_emoji_wake_ts[event.sid] = now

        emoji_ids = event.extra.get("emoji_ids", [])
        count = event.extra.get("count", 1)
        from .emoji_dict import get_emoji_name
        emoji_str = "/".join(get_emoji_name(e) for e in emoji_ids) if emoji_ids else "?"
        text = f"[System 用户{event.actor}({event.actor_name}) 给用户{message_author}({message_author_name})的消息贴了{emoji_str}×{count}"
        if msg_excerpt:
            text += f"（\"{msg_excerpt[:20]}...\"）"
        text += "]"
        await self._trigger_text(event.sid, text, "others_emoji_like")

    async def _is_self_message(self, event: QQEvent) -> bool:
        """判断事件的目标消息是否是 bot 自己的。

        Args:
            event: 贴表情事件。

        Returns:
            True = 给自己贴；False = 给他人贴。
        """
        msg_id = event.extra.get("message_id", "")
        if not msg_id:
            return False
        info = await self.message_locator.lookup(msg_id, event.sid)
        if not info:
            return False
        return bool(info.get("is_self", False))

    def _in_quiet_hours(self, now: float) -> bool:
        """检查是否在静默时段。

        Args:
            now: 当前时间戳。

        Returns:
            True = 静默时段；False = 非静默时段。
        """
        if not self.wake_quiet_hours:
            return False
        try:
            from datetime import datetime
            dt = datetime.fromtimestamp(now)
            current_hhmm = dt.hour * 60 + dt.minute
            # 解析静默时段（如 "23:00-08:00"）
            start_str, end_str = self.wake_quiet_hours.split("-")
            start_h, start_m = map(int, start_str.strip().split(":"))
            end_h, end_m = map(int, end_str.strip().split(":"))
            start = start_h * 60 + start_m
            end = end_h * 60 + end_m
            if start <= end:
                # 同日（如 09:00-17:00）
                return start <= current_hhmm < end
            else:
                # 跨日（如 23:00-08:00）
                return current_hhmm >= start or current_hhmm < end
        except Exception:
            return False

    def _check_hourly_limit(self, sid: str) -> bool:
        """检查每小时唤醒上限。

        Args:
            sid: 会话 ID。

        Returns:
            True = 未超限；False = 超限。
        """
        limit = self.wake_max_per_hour
        if limit == -1:
            return True  # 无限制

        now = time.time()
        hour_bucket = int(now // 3600)
        sid_counts = self._hourly_wake_count.setdefault(sid, {})
        current = sid_counts.get(hour_bucket, 0)
        if current >= limit:
            return False
        sid_counts[hour_bucket] = current + 1
        # 清理旧 bucket（保留最近 2 小时）
        for hb in list(sid_counts.keys()):
            if hb < hour_bucket - 2:
                sid_counts.pop(hb, None)
        return True

    async def on_profile_like(self, event: QQEvent) -> bool:
        """处理资料卡点赞事件（合并防抖）。

        Args:
            event: 点赞事件。

        Returns:
            True = 触发唤醒；False = 合并累加，不唤醒。
        """
        if not self.wake_profile_like:
            return False

        sid = event.sid
        operator = event.actor
        now = time.time()

        acc = self._like_accumulator.setdefault(sid, {}).get(operator)
        if acc and (now - acc["last_ts"]) <= self.profile_like_merge_window_seconds:
            # 合并窗口内：累加
            acc["count"] += event.extra.get("times", 1)
            acc["last_ts"] = now
            return False  # 不 trigger

        # 新窗口：trigger 之前的累计（如果有）
        if acc and acc["count"] > 0:
            await self._trigger_like_summary(sid, operator, acc["count"], now - acc["first_ts"])

        # 开始新累计
        self._like_accumulator[sid][operator] = {
            "count": event.extra.get("times", 1),
            "first_ts": now,
            "last_ts": now,
        }
        return False  # 本次不 trigger，等窗口结束或下次点赞

    async def _trigger_like_summary(self, sid: str, operator: str, count: int, duration: float):
        """触发点赞汇总唤醒。

        Args:
            sid: 会话 ID。
            operator: 点赞用户 QQ。
            count: 累计次数。
            duration: 累计时长（秒）。
        """
        # 构造唤醒文本
        text = f"[System 用户{operator} 赞了你的资料卡×{count}（过去{duration:.0f}秒）]"
        await self._trigger_text(sid, text, "profile_like_summary")

    async def trigger_emoji_like_self(self, event: QQEvent, message_author: str, message_author_name: str, msg_excerpt: str):
        """触发"自己消息被贴表情"唤醒。

        Args:
            event: 贴表情事件。
            message_author: 被贴消息作者 QQ（= self_id）。
            message_author_name: 被贴消息作者昵称。
            msg_excerpt: 被贴消息摘要。
        """
        emoji_ids = event.extra.get("emoji_ids", [])
        count = event.extra.get("count", 1)
        emoji_str = "/".join(emoji_ids) if emoji_ids else "?"
        text = f"[System 用户{event.actor}({event.actor_name}) 给你贴了{emoji_str}×{count}"
        if msg_excerpt:
            text += f"（\"{msg_excerpt[:20]}...\"）"
        text += "]"
        await self._trigger_text(event.sid, text, "emoji_like_self")

    async def trigger_title_self(self, event: QQEvent):
        """触发"头衔授予"唤醒。"""
        title = event.extra.get("title", "")
        text = f"[System 用户{event.actor}({event.actor_name}) 给你授予头衔\"{title}\"]"
        await self._trigger_text(event.sid, text, "title_self")

    async def trigger_kicked_self(self, event: QQEvent):
        """触发"被移出群"唤醒。"""
        text = f"[System 你被移出群聊]"
        await self._trigger_text(event.sid, text, "kicked_self")

    async def _trigger_text(self, sid: str, text: str, event_type: str):
        """构造 KiraMessageEvent 并 trigger（进入会话 buffer）。

        Args:
            sid: 会话 ID（格式: adapter:gm:group_id 或 adapter:dm:user_id）。
            text: 唤醒文本。
            event_type: 事件类型（用于日志）。
        """
        # 检查 anti-harass 屏蔽名单
        if await self._is_blocked(sid):
            logger.debug(f"[QQ增强2] 唤醒被屏蔽: sid={sid}, type={event_type}")
            return

        # 解析 sid
        parts = sid.split(":")
        if len(parts) != 3:
            logger.warning(f"[QQ增强2] 无效 sid: {sid}")
            return
        adapter_name, session_type, session_id = parts

        adapter_info = self.ctx.adapter_mgr.get_adapter(adapter_name)
        if not adapter_info:
            logger.warning(f"[QQ增强2] 适配器不存在: {adapter_name}")
            return

        # 构造 KiraMessageEvent
        group = None
        if session_type == "gm":
            group = Group(group_id=session_id)

        user = User(user_id="system_qq_enhance2", nickname="系统唤醒")
        chain = MessageChain([text])

        event = KiraMessageEvent(
            adapter=adapter_info.info,
            supported_elements=[],
            message=KiraIMMessage(
                timestamp=int(time.time()),
                sender=user,
                message_id=f"qq_enhance2_{event_type}",
                self_id=self.self_id,
                group=group,
                chain=chain,
                is_notice=False,  # 不是 notice，是系统唤醒
                is_mentioned=True,
            ),
            timestamp=int(time.time()),
        )

        # 覆盖 session（__post_init__ 会按 sender 生成错误 session）
        event.session = Session(
            adapter_name=adapter_name,
            session_type=session_type,
            session_id=session_id,
        )

        # trigger
        event.trigger()
        await self.ctx.event_bus.publish(event)
        logger.info(f"[QQ增强2] 唤醒已触发: sid={sid}, type={event_type}")

    async def _is_blocked(self, sid: str) -> bool:
        """检查是否被 anti-harass 屏蔽。

        Args:
            sid: 会话 ID。

        Returns:
            True = 被屏蔽；False = 未被屏蔽。
        """
        # 定期加载屏蔽名单（60s 缓存）
        now = time.time()
        if now - self._last_blocklist_load > 60:
            await self._load_blocklist()
            self._last_blocklist_load = now

        # 检查 sid 是否在屏蔽名单
        # anti-harass 的屏蔽名单格式：{qq: expire_ts} 或 {sid: expire_ts}
        # 简化：检查 sid 或 session_id 是否在屏蔽名单
        parts = sid.split(":")
        if len(parts) != 3:
            return False
        session_id = parts[2]
        return session_id in self._harass_blocklist and self._harass_blocklist[session_id] > now

    async def _load_blocklist(self):
        """加载 anti-harass 屏蔽名单。"""
        # anti-harass 的数据目录：ctx.get_plugin_data_dir() + "/KiraAI-anti-harass-plugin/ignore_list.json"
        # 简化：只检查 data/ignore_list.json（不依赖具体插件 ID）
        try:
            data_dir = self.ctx.get_plugin_data_dir()
            if not data_dir:
                return
            path = os.path.join(data_dir, "ignore_list.json")
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 格式：{qq: expire_ts} 或 {qq: {expire_ts: ...}}
            for qq, val in data.items():
                if isinstance(val, (int, float)):
                    self._harass_blocklist[qq] = float(val)
                elif isinstance(val, dict):
                    expire = val.get("expire_ts") or val.get("expire") or 0
                    if expire:
                        self._harass_blocklist[qq] = float(expire)
        except Exception:
            pass