"""昵称解析器：补全事件里缺失的昵称（尤其是 target）。

为什么需要：
框架的 notice 事件只填了 `sender.nickname`（= actor 的昵称，来自
`get_stranger_info`），**target 的昵称根本没查**。所以环境摘要里会出现
"用户769690776(周武) 戳了戳 用户1593462500"——后一个只有 QQ 号。

设计：
- 缓存 (adapter_name, group_id, qq) → name，TTL 默认 10 秒（可配，0=不缓存），LRU 1024
- 群聊优先 `get_group_member_info`（一次拿到 nickname/card/role），
  回退 `get_stranger_info`
- 单次查询 2 秒超时，失败静默（渲染侧会降级为纯 QQ 号）
- 同一 qq 的并发查询合并（避免同时打多个请求）
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Optional

try:
    from core.logging_manager import get_logger
    logger = get_logger("qq_enhance2.name_resolver", "cyan")
except ImportError:  # 独立测试环境
    class _Logger:
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
    logger = _Logger()


class NameResolver:
    """昵称解析器（带缓存 + 并发合并）。

    Attributes:
        ttl: 缓存有效期（秒），默认 10；<=0 表示不缓存（仍保留并发合并）。
        max_cache: 缓存上限（LRU），默认 1024。
        timeout: 单次查询超时（秒），默认 2.0。
    """

    def __init__(self, *, ttl: float = 10.0, max_cache: int = 1024,
                 timeout: float = 2.0):
        self.ttl = ttl
        self.max_cache = max_cache
        self.timeout = timeout
        # key: (adapter_name, group_id, qq) → (name, ts)
        self._cache: "OrderedDict[tuple, tuple[str, float]]" = OrderedDict()
        # 并发合并：key → Future
        self._inflight: dict[tuple, asyncio.Future] = {}
        # 统计（debug 用）
        self.hits = 0
        self.misses = 0

    def _key(self, adapter_name: str, group_id: str, qq: str) -> tuple:
        return (str(adapter_name or ""), str(group_id or ""), str(qq or ""))

    def _cache_get(self, key: tuple) -> Optional[str]:
        """读缓存（顺带做过期清理 + LRU touch）。"""
        if self.ttl <= 0:
            return None
        item = self._cache.get(key)
        if item is None:
            return None
        name, ts = item
        if time.time() - ts > self.ttl:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return name

    def _cache_set(self, key: tuple, name: str) -> None:
        """写缓存（LRU 淘汰）。"""
        if not name or self.ttl <= 0:
            return
        self._cache[key] = (name, time.time())
        self._cache.move_to_end(key)
        if len(self._cache) > self.max_cache:
            self._cache.popitem(last=False)

    async def resolve(self, adapter, adapter_name: str, group_id: str,
                      qq: str, *, use_cache: bool = True) -> str:
        """解析某人的昵称。

        Args:
            adapter: 适配器实例（需有 bot.send_action）。
            adapter_name: 适配器名（缓存键的一部分）。
            group_id: 群号（空=私聊，走 get_stranger_info）。
            qq: 目标 QQ 号。
            use_cache: 是否用缓存。

        Returns:
            昵称；解析失败返回空串。
        """
        qq = str(qq or "").strip()
        if not qq or adapter is None:
            return ""
        bot = getattr(adapter, "bot", None)
        if bot is None or not hasattr(bot, "send_action"):
            return ""

        key = self._key(adapter_name, group_id, qq)
        if use_cache:
            hit = self._cache_get(key)
            if hit is not None:
                self.hits += 1
                return hit
        self.misses += 1

        # 并发合并：同一 key 只发一次请求
        existing = self._inflight.get(key)
        if existing is not None and not existing.done():
            try:
                return await asyncio.wait_for(asyncio.shield(existing), timeout=self.timeout)
            except Exception:
                return ""

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._inflight[key] = fut
        try:
            name = await self._fetch(bot, group_id, qq)
            if not fut.done():
                fut.set_result(name)
            if name:
                self._cache_set(key, name)
            return name
        except Exception as e:
            logger.debug(f"[QQ增强2] 昵称解析失败 {qq}: {type(e).__name__}: {e}")
            if not fut.done():
                fut.set_result("")
            return ""
        finally:
            self._inflight.pop(key, None)

    async def _fetch(self, bot, group_id: str, qq: str) -> str:
        """实际查询（群成员信息优先，回退陌生人信息）。"""
        # 1. 群成员信息（一次拿到 nickname/card/role）
        if group_id:
            try:
                resp = await asyncio.wait_for(
                    bot.send_action("get_group_member_info", {
                        "group_id": group_id,
                        "user_id": qq,
                        "no_cache": False,
                    }),
                    timeout=self.timeout,
                )
                data = (resp or {}).get("data") or {}
                if isinstance(data, dict):
                    # 对齐框架：notice 的 sender 用 get_stranger_info 的 nickname（QQ 昵称），
                    # 这里也优先 nickname 保持一致性；没有则退回 card。
                    nm = str(data.get("nickname") or data.get("card") or "").strip()
                    if nm:
                        return nm
            except Exception:
                pass

        # 2. 陌生人信息（私聊 / 群成员信息失败时）
        try:
            resp = await asyncio.wait_for(
                bot.send_action("get_stranger_info", {"user_id": qq}),
                timeout=self.timeout,
            )
            data = (resp or {}).get("data") or {}
            if isinstance(data, dict):
                nm = str(data.get("nickname") or "").strip()
                if nm:
                    return nm
        except Exception:
            pass

        return ""

    async def resolve_many(self, adapter, adapter_name: str, group_id: str,
                           qqs: list) -> dict:
        """并发解析多个 QQ 的昵称。

        Args:
            adapter: 适配器实例。
            adapter_name: 适配器名。
            group_id: 群号。
            qqs: QQ 号列表。

        Returns:
            {qq: name}（解析失败的值为空串）。
        """
        uniq = [q for q in dict.fromkeys(str(x or "") for x in qqs) if q]
        if not uniq:
            return {}
        results = await asyncio.gather(*[
            self.resolve(adapter, adapter_name, group_id, q) for q in uniq
        ], return_exceptions=True)
        out = {}
        for q, r in zip(uniq, results):
            out[q] = r if isinstance(r, str) else ""
        return out

    def clear(self) -> None:
        """清空缓存（插件卸载时调用）。"""
        self._cache.clear()
        self._inflight.clear()

    def stats(self) -> dict:
        """缓存统计（诊断用）。"""
        return {
            "size": len(self._cache),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": (self.hits / (self.hits + self.misses))
            if (self.hits + self.misses) else 0.0,
        }