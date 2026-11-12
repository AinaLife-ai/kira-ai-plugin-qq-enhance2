"""事件状态机：收集/合并/过期/TTL/LRU/持久化。

设计要点：
- 每 sid 独立存储；内存 dict + 定期落盘（30s 或满 50 条）。
- LRU 淘汰：超过 perceive_max_events 条时丢弃最旧事件。
- 过期清理：超过 perceive_window_seconds 的事件不再渲染（但仍保留在 store 直到 TTL）。
- 持久化：JSON 格式，按 sid 分文件；启动时异步加载。
- 性能：O(1) 插入/查询；渲染时 O(N log N) 排序（N ≤ 20）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from .event_normalizer import QQEvent


class EventStore:
    """QQ 社会事件状态机。

    Attributes:
        data_dir: 插件数据目录（由 main.py 传入）。
        max_events: 每 sid 最多保留事件数（LRU）。
        window_seconds: 感知时间窗口（秒）。
        ttl_hours: 落盘保留时长（小时）。
    """

    def __init__(
        self,
        data_dir: str,
        *,
        max_events: int = 20,
        window_seconds: int = 1800,
        ttl_hours: int = 24,
    ):
        self.data_dir = data_dir
        self.max_events = max_events
        self.window_seconds = window_seconds
        self.ttl_seconds = ttl_hours * 3600
        self._store: dict[str, list[QQEvent]] = {}  # sid → [QQEvent]
        self._dirty: set[str] = set()
        self._persist_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def start(self):
        """启动持久化任务（30s 定时 + 50 条触发）。"""
        self._persist_task = asyncio.create_task(self._persist_loop())

    async def stop(self):
        """停止持久化任务，最后同步落盘一次。"""
        if self._persist_task and not self._persist_task.done():
            self._persist_task.cancel()
            try:
                await self._persist_task
            except asyncio.CancelledError:
                pass
        await self._persist_all()

    async def add(self, event: QQEvent):
        """添加事件到 store。

        - LRU 淘汰：超过 max_events 条时丢弃最旧事件。
        - 标记 sid 为 dirty（待落盘）。
        """
        async with self._lock:
            ev_list = self._store.setdefault(event.sid, [])
            ev_list.append(event)
            # LRU 淘汰
            if len(ev_list) > self.max_events:
                ev_list.pop(0)
            self._dirty.add(event.sid)

    async def get_events(
        self,
        sid: str,
        *,
        since_ts: Optional[float] = None,
        etypes: Optional[set[str]] = None,
    ) -> list[QQEvent]:
        """获取 sid 的事件列表（可按时间/类型过滤）。

        Args:
            sid: 会话 ID。
            since_ts: 可选，只返回该时间后的事件。
            etypes: 可选，只返回指定类型的事件。

        Returns:
            事件列表（按时间升序）。
        """
        async with self._lock:
            ev_list = self._store.get(sid, [])
            if since_ts is None and etypes is None:
                return list(ev_list)
            result = []
            for ev in ev_list:
                if since_ts is not None and ev.ts < since_ts:
                    continue
                if etypes is not None and ev.etype not in etypes:
                    continue
                result.append(ev)
            return result

    async def clear_old(self, sid: str, before_ts: float):
        """清理过期事件（TTL）。

        Args:
            sid: 会话 ID。
            before_ts: 清理该时间之前的事件。
        """
        async with self._lock:
            ev_list = self._store.get(sid, [])
            self._store[sid] = [ev for ev in ev_list if ev.ts >= before_ts]
            if not self._store[sid]:
                self._store.pop(sid, None)
            self._dirty.add(sid)

    async def _persist_loop(self):
        """定期落盘（每 30s 写一次所有 dirty sid）。

        注意：不能只在 dirty ≥ 50 时才写——那样少量会话的变化会永远不落盘
        （重启后丢失）。dirty 非空即写，30s 节流足够（小文件、写入量低）。
        """
        while True:
            try:
                await asyncio.sleep(30)
                if self._dirty:
                    await self._persist_all()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _persist_all(self):
        """落盘所有 dirty sid。"""
        if not self._dirty:
            return
        async with self._lock:
            dirty_copy = list(self._dirty)
            self._dirty.clear()
        for sid in dirty_copy:
            await self._persist_sid(sid)

    async def _persist_sid(self, sid: str):
        """落盘单个 sid 的事件。"""
        async with self._lock:
            ev_list = self._store.get(sid, [])
            if not ev_list:
                # 空列表：删除文件
                path = self._sid_path(sid)
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                return
            # 转换为可序列化格式
            data = {
                "sid": sid,
                "events": [
                    {
                        "eid": ev.eid,
                        "etype": ev.etype,
                        "sid": ev.sid,
                        "ts": ev.ts,
                        "actor": ev.actor,
                        "actor_name": ev.actor_name,
                        "target": ev.target,
                        "target_name": ev.target_name,
                        "extra": ev.extra,
                    }
                    for ev in ev_list
                ],
            }
        # 写入文件（原子写：先写临时文件再重命名）
        path = self._sid_path(sid)
        tmp_path = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.rename(tmp_path, path)
        except OSError:
            pass

    def _sid_path(self, sid: str) -> str:
        """sid → 文件路径（替换非法字符）。"""
        safe = sid.replace(":", "_").replace("/", "_")
        return os.path.join(self.data_dir, f"{safe}.json")

    async def load_all(self):
        """启动时加载所有落盘事件。"""
        if not os.path.isdir(self.data_dir):
            return
        for fn in os.listdir(self.data_dir):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(self.data_dir, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sid = data.get("sid")
                events = data.get("events", [])
                if not sid or not events:
                    continue
                ev_list = []
                for ev_data in events:
                    ev = QQEvent(
                        eid=ev_data.get("eid", ""),
                        etype=ev_data.get("etype", ""),
                        sid=ev_data.get("sid", ""),
                        ts=int(ev_data.get("ts", 0)),
                        actor=ev_data.get("actor", ""),
                        actor_name=ev_data.get("actor_name", ""),
                        target=ev_data.get("target", ""),
                        target_name=ev_data.get("target_name", ""),
                        extra=ev_data.get("extra", {}),
                    )
                    ev_list.append(ev)
                # 清理过期事件
                cutoff = time.time() - self.ttl_seconds
                ev_list = [ev for ev in ev_list if ev.ts >= cutoff]
                # 应用 LRU 上限（磁盘遗留可能超过 max_events）
                if len(ev_list) > self.max_events:
                    # 保留最新的 max_events 条
                    ev_list.sort(key=lambda e: e.ts)
                    ev_list = ev_list[-self.max_events:]
                if ev_list:
                    async with self._lock:
                        self._store[sid] = ev_list
            except (OSError, json.JSONDecodeError):
                continue