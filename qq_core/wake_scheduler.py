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
from core.chat.message_elements import Text
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
        wake_others_emoji_cooldown: int = 20,
        wake_profile_like: bool = True,
        wake_title: bool = True,
        wake_kicked: bool = True,
        profile_like_merge_window_seconds: int = 10,
        wake_quiet_hours: str = "",
        wake_max_per_hour: int = 60,
        wake_hint: str = "",
        wake_self_emoji_cooldown: int = 0,
        wake_emoji_merge_seconds: int = 5,
        self_id_resolver=None,
    ):
        self.ctx = ctx
        self.adapter = adapter
        self.self_id = str(self_id) if self_id else ""
        # 多号部署：按 adapter_name 取该号的 self_id（唤醒事件要用对的号）
        self._self_id_resolver = self_id_resolver
        self.message_locator = message_locator
        self.wake_self_emoji = wake_self_emoji
        self.wake_others_emoji = wake_others_emoji
        self.wake_others_emoji_cooldown = wake_others_emoji_cooldown
        self.wake_profile_like = wake_profile_like
        self.wake_title = wake_title
        self.wake_kicked = wake_kicked
        self.profile_like_merge_window_seconds = profile_like_merge_window_seconds
        self.wake_quiet_hours = wake_quiet_hours
        self.wake_max_per_hour = wake_max_per_hour
        # 互动提示（单个配置，空=不加）。只加在"互动类"唤醒上：
        # 被贴表情（自己/他人）、资料卡被赞；头衔/被踢不加（没什么可互动的）。
        self.wake_hint = str(wake_hint or "").strip()
        # 自己被贴表情的唤醒冷却（秒）；0 = 不冷却（默认）
        self.wake_self_emoji_cooldown = int(wake_self_emoji_cooldown or 0)
        # 同一条消息的多个贴表情在这个窗口内合并成一条唤醒（自己/他人共用）；0 = 不合并
        self.wake_emoji_merge_seconds = int(wake_emoji_merge_seconds or 0)

        # 点赞累加器：sid → {operator: {count, first_ts, last_ts}}
        self._like_accumulator: dict[str, dict[str, dict]] = {}
        # 点赞结算定时器：(sid, operator) → Task（debounce，每次点赞重置）
        self._like_settle_tasks: dict[tuple[str, str], asyncio.Task] = {}
        # 每小时唤醒计数：sid → {hour_bucket: count}
        self._hourly_wake_count: dict[str, dict[int, int]] = {}
        # 他人贴表情唤醒冷却：sid → 上次唤醒时间
        self._others_emoji_wake_ts: dict[str, float] = {}
        # 自己被贴表情唤醒冷却：sid → 上次唤醒时间
        self._self_emoji_wake_ts: dict[str, float] = {}
        # 自己被贴表情合并： (sid, message_id) → 累计条目
        self._self_emoji_acc: dict[tuple, dict] = {}
        self._self_emoji_tasks: dict[tuple, asyncio.Task] = {}
        # 他人消息被贴表情合并（同样的窗口）
        self._others_emoji_acc: dict[tuple, dict] = {}
        self._others_emoji_tasks: dict[tuple, asyncio.Task] = {}
        # anti-harass 屏蔽名单缓存：{qq: expire_ts}
        self._harass_blocklist: dict[str, float] = {}
        self._last_blocklist_load: float = 0.0
        # 群名缓存：group_id → group_name
        # 唤醒事件是自己构造的 KiraMessageEvent，不是协议端下发的 notice，
        # 框架不会帮我们查群名 —— 不补的话 LLM 每轮看到的是
        # `[group_name: None group_id: 188395693 ...]`，与真实消息不一致。
        self._group_names: dict[str, str] = {}

    def remember_group_name(self, group_id: str, group_name: str):
        """记住群名（从任意一条真实群消息里学到，唤醒时复用）。

        Args:
            group_id: 群号。
            group_name: 群名（空/None 时忽略）。
        """
        gid = str(group_id or "")
        name = str(group_name or "").strip()
        if gid and name and name != "None":
            self._group_names[gid] = name

    async def _get_group_name(self, adapter_name: str, group_id: str) -> str:
        """取群名：缓存 → get_group_info（2s 超时）→ 空串。"""
        gid = str(group_id or "")
        if not gid:
            return ""
        cached = self._group_names.get(gid)
        if cached:
            return cached
        try:
            adapter, _ = self._resolve_target_adapter(adapter_name)
            bot = getattr(adapter, "bot", None)
            if bot is None:
                return ""
            resp = await asyncio.wait_for(
                bot.send_action("get_group_info", {"group_id": gid}), timeout=2.0)
            name = str(((resp or {}).get("data") or {}).get("group_name") or "")
            if name:
                self._group_names[gid] = name
            return name
        except Exception:
            return ""

    def _resolve_target_adapter(self, adapter_name: str):
        """按适配器名取 (adapter, self_id)。"""
        try:
            adapter = self.ctx.adapter_mgr.get_adapter(adapter_name)
            if adapter is not None:
                return adapter, self._resolve_self_id(adapter_name)
        except Exception:
            pass
        return self.adapter, self.self_id

    def _self_nickname(self, adapter_name: str = "") -> str:
        """取 bot 自己的昵称（框架登录时缓存在 bot.self_nickname）。"""
        adapter, _ = self._resolve_target_adapter(adapter_name)
        bot = getattr(adapter, "bot", None)
        for attr in ("self_nickname", "nickname"):
            nm = getattr(bot, attr, None)
            if nm:
                return str(nm).strip()
        return ""

    def _sender_identity(self, adapter_name: str, actor_qq: str,
                         actor_name: str = "") -> tuple[str, str]:
        """确定唤醒事件的 sender（notice 的 user_id 字段）。

        原则：**谁触发的就写谁** —— 与框架 notice 一致
        （戳一戳：user_id=戳人的人；禁言：user_id=被禁言的人 + 文本说明操作者）。
        触发者缺失时（例如被踢但协议端没给 operator_id）退化为 bot 自己，
        绝不再造一个 "系统唤醒 / system_qq_enhance2" 的假用户。

        Args:
            adapter_name: 适配器名。
            actor_qq: 触发者 QQ（可能为空）。
            actor_name: 触发者昵称。

        Returns:
            (user_id, nickname) 元组。
        """
        qq = str(actor_qq or "").strip()
        name = str(actor_name or "").strip()
        if qq and qq != "None":
            return qq, name or f"用户{qq}"
        self_id = self._resolve_self_id(adapter_name)
        if self_id:
            return self_id, self._self_nickname(adapter_name) or f"用户{self_id}"
        # 实在没有任何身份信息（无适配器/无 self_id）：退回框架的系统 sender
        return "system", "system"

    def should_wake_now(self, event: QQEvent) -> bool:
        """同步版唤醒判定（不递增每小时计数，仅供事件处理时快速判断）。

        与 should_wake 的区别：只做静默时段 + 冷却判定，不占用每小时配额
        （配额在 _trigger_text 实际发送唤醒时统一计数）。

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
            return (now - last) >= self.wake_others_emoji_cooldown

        return False

    def _compose_text(self, fact: str, *, with_fact: bool = True) -> str:
        """拼唤醒正文：事实（可选）+ 互动提示（可选）。

        Args:
            fact: 事实描述，如 `[System 用户X 给你贴了爱心 (ID: 66)]`。
            with_fact: 是否带上事实。False 用于"被贴消息和本次唤醒会同批送达"
                的场景 —— 此时那条消息自己带着 `↑该条消息被贴了…` 标注，
                再复述一遍就是重复。

        Returns:
            唤醒正文。极端情况（不带事实且没配提示）会回退为事实，
            因为空正文的 notice 会被 S/Z 的空通知过滤直接丢弃。
        """
        parts = []
        if with_fact:
            parts.append(fact)
        if self.wake_hint:
            parts.append(f"[互动提示] {self.wake_hint}")
        if not parts:
            return fact
        return "\n".join(parts)

    async def trigger_others_emoji_like(self, event: QQEvent, message_author: str,
                                        message_author_name: str, msg_excerpt: str,
                                        *, in_buffer: bool = False):
        """触发"他人消息被贴表情"唤醒（同一消息短时间内的多次贴表情会合并）。

        合并窗口与他人/自己被贴共用 `wake_emoji_merge_seconds`（默认 5 秒）。
        冷却 `wake_others_emoji_cooldown`（默认 20 秒）在**真正发布**时计时。

        Args:
            event: 贴表情事件。
            message_author: 被贴消息作者 QQ。
            message_author_name: 被贴消息作者昵称。
            msg_excerpt: 被贴消息摘要。
            in_buffer: 被贴的那条消息是否还在会话缓冲区里。
                True = 它会被同批次一起送给 LLM（自己带 `↑` 标注）→ 不再复述事实，
                只留互动提示（若没配提示则仍用事实兜底，否则唤醒无正文）。
        """
        key = (event.sid, str(event.extra.get("message_id") or ""))
        meta = {
            "author": str(message_author or ""),
            "author_name": str(message_author_name or ""),
            "excerpt": str(msg_excerpt or ""),
            "in_buffer": bool(in_buffer),
        }

        if self.wake_emoji_merge_seconds <= 0:
            acc = self._merge_others_emoji(None, event, meta)
            await self._settle_others_emoji(key, acc)
            return

        acc = self._others_emoji_acc.get(key)
        if acc is None:
            acc = {"emoji_ids": [], "count": 0, "actors": {},
                   "first_ts": time.time(), "last_ts": time.time(), **meta}
            self._others_emoji_acc[key] = acc
        self._merge_others_emoji(acc, event, meta)
        self._schedule_others_emoji_settle(key)

    @staticmethod
    def _merge_others_emoji(acc: dict | None, event: QQEvent, meta: dict) -> dict:
        """把一次"他人消息被贴"并入累计条目（acc=None 时新建）。"""
        if acc is None:
            acc = {"emoji_ids": [], "count": 0, "actors": {},
                   "first_ts": time.time(), "last_ts": time.time(), **meta}
        for e in event.extra.get("emoji_ids", []) or []:
            if e and e not in acc["emoji_ids"]:
                acc["emoji_ids"].append(e)
        try:
            cnt = int(event.extra.get("count", 1) or 1)
        except (TypeError, ValueError):
            cnt = 1
        acc["count"] += max(1, cnt)
        if event.actor:
            acc["actors"][str(event.actor)] = event.actor_name or ""
        acc["last_ts"] = time.time()
        return acc

    def _schedule_others_emoji_settle(self, key: tuple):
        old = self._others_emoji_tasks.pop(key, None)
        if old and not old.done():
            old.cancel()
        self._others_emoji_tasks[key] = asyncio.create_task(
            self._others_emoji_settle_after(key))

    async def _others_emoji_settle_after(self, key: tuple):
        try:
            await asyncio.sleep(self.wake_emoji_merge_seconds)
        except asyncio.CancelledError:
            return
        self._others_emoji_tasks.pop(key, None)
        acc = self._others_emoji_acc.pop(key, None)
        if not acc:
            return
        await self._settle_others_emoji(key, acc)

    async def _settle_others_emoji(self, key: tuple, acc: dict):
        """组装并发布一条"他人消息被贴表情"唤醒。"""
        sid = key[0]
        now = time.time()

        # 冷却（独立配置，默认 20 秒；0 = 不冷却）
        if self.wake_others_emoji_cooldown > 0:
            last = self._others_emoji_wake_ts.get(sid, 0.0)
            if now - last < self.wake_others_emoji_cooldown:
                logger.debug(f"[QQ增强2] 他人贴表情唤醒冷却中: sid={sid}")
                return

        from .emoji_display import format_emoji_list
        from .fmt import fmt_user
        actors = list(acc.get("actors", {}).items())
        if actors:
            names = [fmt_user(qq, nm) for qq, nm in actors[:5]]
            more = f"等{len(actors)}人" if len(actors) > 5 else ""
            who = "、".join(names) + more
        else:
            who = "有人"
        target = fmt_user(acc.get("author", ""), acc.get("author_name", ""))
        emoji_str = format_emoji_list(acc.get("emoji_ids", []), acc.get("count", 1))
        text = f"[System {who}给{target}的消息贴了{emoji_str}"
        excerpt = str(acc.get("excerpt") or "")
        if excerpt:
            text += f"（\"{excerpt[:20]}...\"）"
        text += "]"
        # in_buffer 时不复述事实（那条消息自己带着 ↑ 标注，会同批送达）
        body = self._compose_text(text, with_fact=not acc.get("in_buffer", False))
        actor_qq = actors[0][0] if actors else ""
        actor_name = actors[0][1] if actors else ""
        ok = await self._trigger_text(sid, body, "others_emoji_like",
                                      actor_qq=actor_qq, actor_name=actor_name)
        if ok:
            self._others_emoji_wake_ts[sid] = time.time()

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
        if not self.message_locator:
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

        合并窗口语义：同一用户对资料卡的连续点赞在
        profile_like_merge_window_seconds 内合并为一条唤醒。

        结算时机（关键）：窗口内最后一次点赞后，必须启动一个定时器在
        窗口结束时结算——否则"只点一次就走"的场景永远收不到唤醒。

        Args:
            event: 点赞事件。

        Returns:
            True = 触发唤醒；False = 合并累加（等待窗口结束结算）。
        """
        if not self.wake_profile_like:
            return False

        sid = event.sid
        operator = event.actor
        now = time.time()

        acc = self._like_accumulator.get(sid, {}).get(operator)
        if acc and (now - acc["last_ts"]) <= self.profile_like_merge_window_seconds:
            # 合并窗口内：累加并顺延结算定时器
            acc["count"] += event.extra.get("times", 1)
            acc["last_ts"] = now
            self._schedule_like_settle(sid, operator)
            return False

        # 新窗口：先结算上一个窗口（如果还有未结算的）
        if acc and acc["count"] > 0:
            await self._settle_like(sid, operator)

        # 开始新累计 + 启动结算定时器
        self._like_accumulator.setdefault(sid, {})[operator] = {
            "count": event.extra.get("times", 1),
            "first_ts": now,
            "last_ts": now,
            "name": event.actor_name or "",
        }
        self._schedule_like_settle(sid, operator)
        return False

    def _schedule_like_settle(self, sid: str, operator: str):
        """（重）调度点赞结算定时器：窗口结束后结算。

        每次点赞都重置定时器（debounce），保证"最后一次点赞后 N 秒"才结算。
        """
        key = (sid, operator)
        old = self._like_settle_tasks.pop(key, None)
        if old and not old.done():
            old.cancel()
        task = asyncio.create_task(self._like_settle_after(sid, operator))
        self._like_settle_tasks[key] = task

    async def _like_settle_after(self, sid: str, operator: str):
        """等待合并窗口结束后结算点赞。"""
        try:
            await asyncio.sleep(self.profile_like_merge_window_seconds)
        except asyncio.CancelledError:
            return
        self._like_settle_tasks.pop((sid, operator), None)
        await self._settle_like(sid, operator)

    async def _settle_like(self, sid: str, operator: str):
        """结算某个用户的点赞累计（触发一次唤醒并清空）。"""
        acc = self._like_accumulator.get(sid, {}).get(operator)
        if not acc or acc["count"] <= 0:
            return
        count = acc["count"]
        duration = max(time.time() - acc["first_ts"], 1.0)
        actor_name = acc.get("name", "")
        # 先清空再触发，避免重入
        self._like_accumulator[sid].pop(operator, None)
        if not self._like_accumulator[sid]:
            self._like_accumulator.pop(sid, None)
        await self._trigger_like_summary(sid, operator, count, duration, actor_name)

    async def _trigger_like_summary(self, sid: str, operator: str, count: int,
                                    duration: float, actor_name: str = ""):
        """触发点赞汇总唤醒。

        Args:
            sid: 会话 ID。
            operator: 点赞用户 QQ。
            count: 累计次数。
            duration: 累计时长（秒）。
            actor_name: 点赞用户昵称（可为空）。
        """
        from .fmt import fmt_user
        # 构造唤醒文本（带昵称，对齐框架 用户{QQ}({昵称}) 格式）
        text = f"[System {fmt_user(operator, actor_name)}赞了你的资料卡×{count}（过去{duration:.0f}秒）]"
        text = self._compose_text(text)
        await self._trigger_text(sid, text, "profile_like_summary", actor_qq=operator,
                                actor_name=actor_name)

    async def trigger_emoji_like_self(self, event: QQEvent, message_author: str,
                                      message_author_name: str, msg_excerpt: str):
        """"自己消息被贴表情"唤醒（同一消息短时间内的多次贴表情会合并）。

        - 合并窗口：`wake_self_emoji_merge_seconds`（默认 5 秒，0=不合并）。
          同一条消息在窗口内被贴多个表情 → 组装成一条通知。
        - 冷却：`wake_self_emoji_cooldown`（默认 10 秒，0=不冷却），
          在真正发布唤醒后计时，被静默/配额拦截不占用。

        Args:
            event: 贴表情事件。
            message_author: 被贴消息作者 QQ（= self_id）。
            message_author_name: 被贴消息作者昵称。
            msg_excerpt: 被贴消息摘要。
        """
        key = (event.sid, str(event.extra.get("message_id") or ""))
        now = time.time()

        if self.wake_emoji_merge_seconds <= 0:
            # 不合并：直接走一次性结算
            acc = self._merge_self_emoji(None, event, msg_excerpt)
            await self._settle_self_emoji(key, acc)
            return

        acc = self._self_emoji_acc.get(key)
        if acc is None:
            acc = {
                "emoji_ids": [], "count": 0, "actors": {},
                "excerpt": msg_excerpt or "", "first_ts": now, "last_ts": now,
            }
            self._self_emoji_acc[key] = acc
        self._merge_self_emoji(acc, event, msg_excerpt)
        self._schedule_self_emoji_settle(key)

    @staticmethod
    def _merge_self_emoji(acc: dict | None, event: QQEvent, msg_excerpt: str) -> dict:
        """把一次贴表情并入累计条目（acc=None 时新建一个一次性条目）。"""
        if acc is None:
            acc = {"emoji_ids": [], "count": 0, "actors": {},
                   "excerpt": msg_excerpt or "", "first_ts": time.time(),
                   "last_ts": time.time()}
        for e in event.extra.get("emoji_ids", []) or []:
            if e and e not in acc["emoji_ids"]:
                acc["emoji_ids"].append(e)
        try:
            cnt = int(event.extra.get("count", 1) or 1)
        except (TypeError, ValueError):
            cnt = 1
        acc["count"] += max(1, cnt)
        if event.actor:
            acc["actors"][str(event.actor)] = event.actor_name or ""
        if not acc.get("excerpt") and msg_excerpt:
            acc["excerpt"] = msg_excerpt
        acc["last_ts"] = time.time()
        return acc

    def _schedule_self_emoji_settle(self, key: tuple):
        """（重）调度合并结算定时器：窗口内每次新贴都重置。"""
        old = self._self_emoji_tasks.pop(key, None)
        if old and not old.done():
            old.cancel()
        self._self_emoji_tasks[key] = asyncio.create_task(
            self._self_emoji_settle_after(key))

    async def _self_emoji_settle_after(self, key: tuple):
        try:
            await asyncio.sleep(self.wake_emoji_merge_seconds)
        except asyncio.CancelledError:
            return
        self._self_emoji_tasks.pop(key, None)
        acc = self._self_emoji_acc.pop(key, None)
        if not acc:
            return
        await self._settle_self_emoji(key, acc)

    async def _settle_self_emoji(self, key: tuple, acc: dict):
        """组装并发布一条"自己被贴表情"唤醒。"""
        sid = key[0]
        now = time.time()

        # 冷却（独立配置，默认 10 秒；0 = 不冷却）
        if self.wake_self_emoji_cooldown > 0:
            last = self._self_emoji_wake_ts.get(sid, 0.0)
            if now - last < self.wake_self_emoji_cooldown:
                logger.debug(
                    f"[QQ增强2] 自己被贴表情唤醒冷却中: sid={sid}, "
                    f"剩余 {self.wake_self_emoji_cooldown - (now - last):.0f}s")
                return

        from .emoji_display import format_emoji_list
        from .fmt import fmt_user
        actors = list(acc.get("actors", {}).items())
        if actors:
            names = [fmt_user(qq, nm) for qq, nm in actors[:5]]
            more = f"等{len(actors)}人" if len(actors) > 5 else ""
            who = "、".join(names) + more
        else:
            who = "有人"
        emoji_str = format_emoji_list(acc.get("emoji_ids", []), acc.get("count", 1))
        text = f"[System {who}给你贴了{emoji_str}"
        excerpt = str(acc.get("excerpt") or "")
        if excerpt:
            text += f"（\"{excerpt[:20]}...\"）"
        text += "]"
        # 自己的消息是 assistant 角色，改写器不标注 → 事实必须保留，再补提示
        text = self._compose_text(text)
        actor_qq = actors[0][0] if actors else ""
        actor_name = actors[0][1] if actors else ""
        ok = await self._trigger_text(sid, text, "emoji_like_self",
                                      actor_qq=actor_qq, actor_name=actor_name)
        if ok:
            self._self_emoji_wake_ts[sid] = time.time()

    async def trigger_member_decrease(self, event: QQEvent):
        """退群/被踢 → notice 唤醒（对齐 QQ增强1 的入群感知）。

        格式与入群同款：`[System 用户X(昵称)退群了]` /
        `[System 用户X(昵称)被用户Y(昵称)移出群聊]`。

        Args:
            event: 成员减少事件（E_MEMBER_DECREASE）。
        """
        from .fmt import fmt_user
        sub = str(event.extra.get("sub_type") or "")
        who = fmt_user(event.target, event.target_name)
        if sub in ("kick", "kick_me"):
            if event.actor:
                text = f"[System {who}被{fmt_user(event.actor, event.actor_name)}移出群聊]"
            else:
                text = f"[System {who}被移出群聊]"
        else:
            text = f"[System {who}退群了]"
        await self._trigger_text(event.sid, text, "member_decrease",
                                actor_qq=event.target, actor_name=event.target_name)

    async def trigger_title_self(self, event: QQEvent):
        """触发"头衔授予"唤醒。"""
        title = event.extra.get("title", "")
        from .fmt import fmt_user
        text = f"[System {fmt_user(event.actor, event.actor_name)}给你授予头衔\"{title}\"]"
        await self._trigger_text(event.sid, text, "title_self",
                                actor_qq=event.actor, actor_name=event.actor_name)

    async def trigger_kicked_self(self, event: QQEvent):
        """触发"被移出群"唤醒。

        注意：必须带上操作者（谁踢的），否则 bot 无法判断是被管理员踢出
        还是自己退群，也无法在后续对话中理解上下文。
        """
        from .fmt import fmt_user
        if event.actor:
            text = f"[System 你被{fmt_user(event.actor, event.actor_name)}移出群聊]"
        else:
            text = "[System 你被移出群聊]"
        await self._trigger_text(event.sid, text, "kicked_self",
                                actor_qq=event.actor, actor_name=event.actor_name)

    def _get_supported_elements(self, adapter_info, ctx=None) -> list[str]:
        """获取适配器支持的消息元素类型（唤醒事件用）。

        唤醒事件需要正确的 `supported_elements`，否则 assemble 阶段不会注册
        对应 tag（text/img/at/reply/emoji…），LLM 输出 `<img>`、`<at>` 会被丢掉。

        **不要碰 `message_types`**：它是框架的 deprecated 属性，
        连 `hasattr(adapter, "message_types")` 都会触发 DeprecationWarning 刷日志。
        这里直接走 `IMCapability._supported_elements`。

        Args:
            adapter_info: AdapterInfo 或适配器实例。
            ctx: PluginContext（用于按名字取适配器实例）。

        Returns:
            元素类型列表；都取不到时回退 ["text"]。
        """
        # 1. 直接字段
        try:
            els = getattr(adapter_info, "supported_elements", None)
            if isinstance(els, (list, tuple)) and els:
                return list(els)
        except Exception:
            pass

        # 2. 定位适配器实例
        adapter = None
        try:
            if hasattr(adapter_info, "get_capability"):
                adapter = adapter_info
            elif ctx is not None:
                name = (getattr(adapter_info, "name", None)
                        or getattr(getattr(adapter_info, "info", None), "name", None))
                if name:
                    adapter = ctx.adapter_mgr.get_adapter(name)
        except Exception:
            adapter = None

        # 3. 现代 API：IMCapability._supported_elements
        if adapter is not None:
            try:
                from core.adapter.capabilities import IMCapability
                im = adapter.get_capability(IMCapability)
                els = getattr(im, "_supported_elements", None)
                if isinstance(els, (list, tuple)) and els:
                    return list(els)
            except Exception:
                pass

        return ["text"]

    async def _trigger_text(self, sid: str, text: str, event_type: str,
                            actor_qq: str = "", actor_name: str = ""):
        """构造 KiraMessageEvent 并 trigger（进入会话 buffer）。

        所有唤醒路径的**唯一收口**，依次检查：
        1. 静默时段（wake_quiet_hours）
        2. 每小时配额（wake_max_per_hour）
        3. anti-harass 屏蔽名单

        这样任何唤醒（被贴表情/点赞/头衔/被踢/他人贴表情）都受统一约束。
        （原来静默只在 should_wake_now 里查，直接调 trigger_* 的路径绕过了它。）

        **消息形态对齐框架 notice / QQ增强1**：
        框架的 `_format_user_message` 对 `is_notice=True` 的消息走另一个分支：

            [time] Notice [group_id: 188395693, user_id: 1001] | [System ...]

        而普通消息走：

            [time] [message_id: None] [group_name: ... user_nickname: 系统唤醒,
             user_id: system_qq_enhance2] | [System ...]

        后者会往上下文里塞 "系统唤醒/system_qq_enhance2" 这种**不存在的用户**，
        与真实的 notice（戳一戳、禁言、入群）风格不一致，模型也容易把它当成一个人。
        所以唤醒事件统一按 notice 构造：is_notice=True，sender = 真正的触发者。

        Args:
            sid: 会话 ID（格式: adapter:gm:group_id 或 adapter:dm:user_id）。
            text: 唤醒文本。
            event_type: 事件类型（用于日志）。
            actor_qq: 触发者 QQ（用于 anti-harass 屏蔽名单检查 + notice 的 user_id）。
            actor_name: 触发者昵称（日志/兼容用）。

        Returns:
            True = 已发布唤醒；False = 被静默/配额/屏蔽/无效 sid 拦截。
        """
        # 静默时段（统一收口，避免直接 trigger_* 绕过）
        if self._in_quiet_hours(time.time()):
            logger.debug(f"[QQ增强2] 唤醒被静默时段拦截: sid={sid}, type={event_type}")
            return False

        # 每小时配额（唯一计数点）
        if not self._check_hourly_limit(sid):
            logger.debug(f"[QQ增强2] 唤醒被每小时上限拦截: sid={sid}, type={event_type}")
            return False

        # 检查 anti-harass 屏蔽名单（按触发者用户检查）
        if await self._is_blocked(sid, actor_qq):
            logger.debug(f"[QQ增强2] 唤醒被屏蔽: sid={sid}, type={event_type}, actor={actor_qq}")
            return False

        # 解析 sid
        parts = sid.split(":")
        if len(parts) != 3:
            logger.warning(f"[QQ增强2] 无效 sid: {sid}")
            return False
        adapter_name, session_type, session_id = parts

        adapter_info = self.ctx.adapter_mgr.get_adapter(adapter_name)
        if not adapter_info:
            logger.warning(f"[QQ增强2] 适配器不存在: {adapter_name}")
            return False

        # 构造 KiraMessageEvent
        group = None
        if session_type == "gm":
            # 群名：缓存 → get_group_info；否则 LLM 看到 group_name: None
            group_name = await self._get_group_name(adapter_name, session_id)
            group = Group(group_id=session_id, group_name=group_name or session_id)

        # 发送者 = 真正的触发者（notice 语义：user_id 就是事件主体）
        sender_qq, sender_name = self._sender_identity(adapter_name, actor_qq, actor_name)
        user = User(user_id=sender_qq, nickname=sender_name)
        # 注意：MessageChain 只接受 BaseMessageElement，不能直接传 str
        # （框架 KiraMessageEvent.__post_init__ 会访问 ele.repr，传 str 会崩）
        chain = MessageChain([Text(text)])

        event = KiraMessageEvent(
            adapter=adapter_info.info,
            # 必须传真实支持的标签，否则 assemble 阶段不会注册 text tag，
            # 导致 LLM 输出 <msg><text> 时无法解析。
            # 从适配器元数据取（拿不到时回退为 ["text"]）。
            supported_elements=self._get_supported_elements(adapter_info, self.ctx),
            message=KiraIMMessage(
                timestamp=int(time.time()),
                sender=user,
                # 对齐框架 notice 的写法（core/adapter/src/qq/im.py:468 用 "None"）。
                message_id="None",
                # 多号部署：用该会话对应账号的 self_id
                self_id=self._resolve_self_id(adapter_name),
                group=group,
                chain=chain,
                # **is_notice=True**：走框架的 Notice 格式（与戳一戳/禁言/入群完全一致）
                is_notice=True,
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
        return True

    async def _is_blocked(self, sid: str, actor_qq: str = "", kind: str = "all") -> bool:
        """检查触发者是否被 anti-harass / S / Z 的屏蔽名单拦截。

        对齐 S/Z 的 `is_blocked` 语义（default_chat_z/chat_enhance.py:396）：
        屏蔽名单是 **{sid}|{uid}|{kind} → expire_ts** 的三元组，匹配分三级：
          1. 全局屏蔽     `*|*|kind`        → 所有会话所有用户
          2. 会话级屏蔽   `{sid}|*|kind`    → 该会话所有用户
          3. 用户级屏蔽   `{sid}|{uid}|kind` → 该会话该用户

        注意：`kind == "poke"` 的屏蔽**只挡戳一戳通知，不算拉黑**，
        因此不能用它来拦截"贴表情/点赞/头衔"这类唤醒（对齐 Z 的实现）。

        Args:
            sid: 会话 ID（adapter:gm:group_id）。
            actor_qq: 触发者 QQ 号。
            kind: 屏蔽类型（默认 "all" = 拉黑语义）。

        Returns:
            True = 被屏蔽（不应唤醒）；False = 未被屏蔽。
        """
        # 定期加载屏蔽名单（60s 缓存）
        now = time.time()
        if now - self._last_blocklist_load > 60:
            await self._load_blocklist()
            self._last_blocklist_load = now

        if not actor_qq:
            return False

        # 三级匹配（对齐 S/Z 的 is_blocked）：拉黑语义，跳过 poke 类屏蔽
        for key, expire in self._harass_blocklist.items():
            if expire <= now:
                continue
            parts = key.split("|")
            if len(parts) != 3:
                continue
            k_sid, k_uid, k_kind = parts
            if k_kind == "poke":
                # poke 屏蔽只挡戳一戳通知，不拉黑该用户的其他行为
                continue
            if k_sid not in ("*", sid):
                continue
            if k_uid not in ("*", actor_qq):
                continue
            return True
        return False

    async def _load_blocklist(self):
        """加载屏蔽名单（anti-harass 插件的持久化文件）。

        anti-harass 把屏蔽名单写在 `data/plugin_data/{plugin_id}/ignore_list.json`，
        格式为 `{"{sid}|{uid}|{kind}": expire_ts}`（与 S/Z 内存态 `_ignored` 同构）。

        S/Z 的屏蔽名单是纯内存态（不落盘），无法直接读取——这是已知限制：
        只有 anti-harass 插件持久化了名单，我们兼容它即可。

        路径探测：遍历 plugin_data 下的目录，找第一个含 ignore_list.json 的。
        """
        try:
            base = self._plugin_data_base()
            if not base or not os.path.isdir(base):
                return
            path = None
            for name in sorted(os.listdir(base)):
                cand = os.path.join(base, name, "ignore_list.json")
                if os.path.exists(cand):
                    path = cand
                    break
            if not path:
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            self._harass_blocklist = {}
            for key, val in data.items():
                if not isinstance(key, str) or key.count("|") != 2:
                    continue
                try:
                    expire = float(val)
                except (TypeError, ValueError):
                    continue
                self._harass_blocklist[key] = expire
        except Exception:
            pass

    def _plugin_data_base(self) -> str:
        """定位 plugin_data 根目录（不依赖调用方 plugin_id）。"""
        try:
            d = self.ctx.get_plugin_data_dir()
            if d:
                # get_plugin_data_dir 返回的是本插件的目录，上一级就是 plugin_data/
                return os.path.dirname(str(d))
        except Exception:
            pass
        return ""

    def _resolve_self_id(self, adapter_name: str = "") -> str:
        """按适配器名取该号的 self_id（多号部署必需）。

        Args:
            adapter_name: 适配器名（sid 第一段）。

        Returns:
            self_id；无 resolver 或找不到时回退默认。
        """
        if self._self_id_resolver and adapter_name:
            try:
                return self._self_id_resolver(adapter_name) or self.self_id
            except Exception:
                pass
        return self.self_id

    async def terminate(self):
        """清理：取消所有待结算的点赞定时器（防任务泄漏）。

        注意：不结算未完成的点赞累计——插件卸载时不应该再触发唤醒。
        """
        tasks = list(self._like_settle_tasks.values())
        self._like_settle_tasks.clear()
        tasks += list(self._self_emoji_tasks.values())
        self._self_emoji_tasks.clear()
        self._self_emoji_acc.clear()
        tasks += list(self._others_emoji_tasks.values())
        self._others_emoji_tasks.clear()
        self._others_emoji_acc.clear()
        for t in tasks:
            if not t.done():
                t.cancel()
        # 等待取消完成（忽略异常）
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._like_accumulator.clear()