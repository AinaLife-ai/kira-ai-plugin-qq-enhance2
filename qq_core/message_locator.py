"""消息定位：message_id → (发送者, 内容摘要) 定位 + 撤回归档。

设计要点：
- L1: 本地 LRU（8192 条，全局共享，记录最近收发消息 message_id → {sender, text, ts, is_self}）。
- L2: get_msg(message_id)（send_action，2s 超时，失败静默）。
- 反查作者判定"是不是 bot 自己的消息"：NapCat/LLOneBot 的 get_msg 返回 data.user_id == self_id；LLOneBot 缓存命中路径返回结构可能不同——双保险：L1 记录时若 sender==self 直接标记，L2 返回后比对 user_id/self_id。
- 撤回归档：group_recall/friend_recall 到达时，从 L1/L2 查被撤回 message_id，归档 (message_id → {sender, text, ts}) 到 recall_store（按 sid）。
- 性能：L1 O(1) 插入/查询；L2 令牌桶限流（单 sid 每分钟 ≤ 10 次）。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Optional

from core.logging_manager import get_logger

logger = get_logger("qq_enhance2.locator", "cyan")


class MessageLocator:
    """消息定位器：message_id → (发送者, 内容摘要) + 撤回归档。

    Attributes:
        cache_size: LRU 缓存条数（默认 8192）。
        adapter: QQ 适配器实例（用于 get_msg）。
        self_id: 机器人自己的 QQ 号。
    """

    def __init__(
        self,
        *,
        adapter=None,
        self_id: str = "",
        cache_size: int = 8192,
    ):
        self.adapter = adapter
        self.self_id = str(self_id) if self_id else ""
        self.cache_size = cache_size
        self._cache: OrderedDict[str, dict] = OrderedDict()  # message_id → {sender, sender_name, text, ts, is_self}
        self._recall_store: dict[str, list[dict]] = {}  # sid → [{message_id, sender, sender_name, text, ts, operator, operator_name, annotation}]
        self._rate_limiter: dict[str, list[float]] = {}  # sid → [timestamps]
        self._lock = asyncio.Lock()

    async def record_incoming(self, message_id: str, sender: str, sender_name: str, text: str, is_self: bool = False):
        """记录一条收到的消息（L1 缓存）。

        Args:
            message_id: QQ 消息 ID。
            sender: 发送者 QQ 号。
            sender_name: 发送者昵称。
            text: 消息文本摘要（截断 200 字）。
            is_self: 是否是 bot 自己发的。
        """
        if not message_id:
            return
        async with self._lock:
            self._cache[message_id] = {
                "sender": sender,
                "sender_name": sender_name,
                # 保留 500 字符：撤回标注需要用前 50 字做匹配键，
                # 但长消息（如引用）也可能被撤回，500 足够覆盖常见场景。
                "text": text[:500] if text else "",
                "ts": time.time(),
                "is_self": is_self,
            }
            # LRU 淘汰
            if len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    async def lookup(self, message_id: str, sid: str = "") -> Optional[dict]:
        """查找消息信息（L1 → L2）。

        Args:
            message_id: QQ 消息 ID。
            sid: 会话 ID（用于令牌桶限流）。

        Returns:
            {sender, sender_name, text, ts, is_self} 或 None。
        """
        if not message_id:
            return None

        # L1 缓存
        async with self._lock:
            cached = self._cache.get(message_id)
            if cached:
                self._cache.move_to_end(message_id)
                return cached.copy()

        # L2 get_msg
        if not self.adapter:
            return None

        # 令牌桶限流（单 sid 每分钟 ≤ 10 次）
        now = time.time()
        async with self._lock:
            timestamps = self._rate_limiter.setdefault(sid, [])
            # 清理 60s 前的记录
            timestamps[:] = [t for t in timestamps if now - t < 60]
            if len(timestamps) >= 10:
                logger.debug(f"[QQ增强2] get_msg 限流: sid={sid}")
                return None
            timestamps.append(now)

        # 调用 get_msg（2s 超时）
        try:
            resp = await asyncio.wait_for(
                self.adapter.bot.get_msg(message_id),
                timeout=2.0,
            )
            data = (resp or {}).get("data") or {}
            if not data:
                return None
            sender = str(data.get("user_id") or "")
            sender_name = str((data.get("sender") or {}).get("nickname") or "")
            # 提取文本（简化：只取第一段 text）
            text = ""
            for seg in (data.get("message") or []):
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text = str((seg.get("data") or {}).get("text") or "")
                    break
            is_self = (sender == self.self_id) if self.self_id else False
            result = {
                "sender": sender,
                "sender_name": sender_name,
                "text": text[:500],
                "ts": int(data.get("time") or time.time()),
                "is_self": is_self,
            }
            # 写入 L1
            async with self._lock:
                self._cache[message_id] = result
                if len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            return result
        except asyncio.TimeoutError:
            logger.debug(f"[QQ增强2] get_msg 超时: {message_id}")
            return None
        except Exception as e:
            logger.debug(f"[QQ增强2] get_msg 失败: {message_id}: {e}")
            return None

    async def archive_recall(
        self,
        *,
        sid: str,
        message_id: str,
        sender: str,
        sender_name: str,
        text: str,
        ts: int,
        operator: str,
        operator_name: str,
        sub_type: str,
        is_group: bool,
    ):
        """归档撤回消息（用于撤回改写）。

        Args:
            sid: 会话 ID。
            message_id: 被撤回消息 ID。
            sender: 被撤回消息发送者 QQ。
            sender_name: 被撤回消息发送者昵称。
            text: 被撤回消息文本。
            ts: 撤回时间。
            operator: 操作者 QQ（撤回者）。
            operator_name: 操作者昵称。
            sub_type: 事件子类型。
            is_group: 是否群聊。
        """
        # 判定操作者身份（区分具体动作差别）
        # 注意：私聊撤回（friend_recall）可能没有 operator_id，回退到消息发送者
        effective_operator = operator or sender
        effective_operator_name = operator_name or ("" if operator else sender_name)

        if not operator or operator == sender:
            # 自己撤回 或 无操作者信息（私聊常见）
            action_desc = "撤回了一条消息"
            role_prefix = ""
        else:
            # 检查是否是群主/管理员（需 get_group_member_info）
            role = await self._get_group_member_role(sid, operator)
            if role == "owner":
                action_desc = "撤回了此消息"
                role_prefix = "(群主)"
            elif role == "admin":
                action_desc = "撤回了此消息"
                role_prefix = "(管理员)"
            else:
                action_desc = "撤回了此消息"
                role_prefix = ""

        # 标注格式：用户{QQ}({昵称}){角色前缀} {动作}
        if role_prefix:
            annotation = f"\n[已撤回｜{_fmt_user(effective_operator, effective_operator_name)}{role_prefix} {action_desc}]"
        else:
            annotation = f"\n[已撤回｜{_fmt_user(effective_operator, effective_operator_name)} {action_desc}]"

        async with self._lock:
            recall_list = self._recall_store.setdefault(sid, [])
            recall_list.append({
                "message_id": message_id,
                "sender": sender,
                "sender_name": sender_name,
                "text": text,
                "ts": ts,
                "operator": operator,
                "operator_name": operator_name,
                "annotation": annotation,
            })
            # LRU 淘汰（保留最近 50 条）
            if len(recall_list) > 50:
                recall_list.pop(0)

    async def _get_group_member_role(self, sid: str, qq: str) -> str:
        """获取群成员角色（owner/admin/member）。

        Args:
            sid: 会话 ID（格式: adapter:gm:group_id）。
            qq: 成员 QQ 号。

        Returns:
            "owner" | "admin" | "member" | ""（查询失败）。
        """
        # 框架 sid 格式：adapter:gm:group_id（如 napcat:gm:123456）
        if not self.adapter or not self.adapter.bot:
            return ""
        parts = sid.split(":")
        if len(parts) != 3 or parts[1] != "gm":
            return ""
        group_id = parts[2]
        try:
            resp = await asyncio.wait_for(
                self.adapter.bot.send_action("get_group_member_info", {
                    "group_id": group_id,
                    "user_id": qq,
                    "no_cache": False,
                }),
                timeout=2.0,
            )
            data = (resp or {}).get("data") or {}
            return str(data.get("role") or "")
        except Exception:
            return ""

    async def get_recalls(self, sid: str) -> list[dict]:
        """获取 sid 的撤回归档列表。"""
        async with self._lock:
            return list(self._recall_store.get(sid, []))

    async def clear_recalls(self, sid: str, keep: set[str] | None = None):
        """清理 sid 的撤回归档。

        Args:
            sid: 会话 ID。
            keep: 要保留的 message_id 集合（已成功追加标注的）。
                  None = 全部清空；空集合 = 也全部清空。
                  注意：未匹配到原文的归档必须保留，否则标注永久丢失
                  （被撤回消息可能不在本轮 prompt 里）。
        """
        async with self._lock:
            if not keep:
                self._recall_store.pop(sid, None)
                return
            lst = self._recall_store.get(sid, [])
            remain = [r for r in lst if r.get("message_id") not in keep]
            if remain:
                self._recall_store[sid] = remain
            else:
                self._recall_store.pop(sid, None)

    async def prune_recalls(self, max_age_seconds: float = 600):
        """清理过期撤回归档（默认 10 分钟）。

        未匹配的归档会一直保留到过期——避免标注永久丢失，
        同时防止无限增长。
        """
        cutoff = time.time() - max_age_seconds
        async with self._lock:
            for sid in list(self._recall_store.keys()):
                lst = self._recall_store[sid]
                remain = [r for r in lst if r.get("ts", 0) >= cutoff]
                if remain:
                    self._recall_store[sid] = remain
                else:
                    self._recall_store.pop(sid, None)


def _fmt_user(qq: str, name: str) -> str:
    if qq and name:
        return f"用户{qq}({name})"
    elif qq:
        return f"用户{qq}"
    elif name:
        return f"用户({name})"
    else:
        return "用户(未知)"