"""QQ增强2 插件入口：hook 注册、模块装配。

功能：
1. 感知层：im_message(LOW) 收集他人事件 → event_store；llm_request(LOW) 注入环境摘要。
2. 唤醒层：唤醒级事件 → wake_scheduler.trigger() → 进入会话 buffer（批次合并）。
3. 撤回/贴表情改写：llm_request 阶段在原文后追加标注。
4. 工具层：qq_profile / qq_group_profile。

优先级说明：
- im_message 用 LOW(-50)：在 S/Z/anti-harass/QQ增强1（HIGH 及以上）之后执行，
  避免同优先级时执行顺序不确定。
- llm_request 用 LOW：最后注入，保证环境摘要位于 user_prompt 最前。
"""

from __future__ import annotations

import time
from typing import Optional

from core.plugin import BasePlugin, PluginContext, logger, on, Priority
from core.chat.message_utils import KiraMessageBatchEvent, KiraMessageEvent
from core.provider import LLMRequest
from core.prompt_manager import Prompt

from .qq_core.event_normalizer import normalize_notice, involves_self, E_POKE, E_EMOJI_LIKE, E_BAN, E_UNBAN, E_MEMBER_DECREASE, E_RECALL, E_TITLE, E_CARD, E_ADMIN, E_PROFILE_LIKE_SELF, E_KICKED_SELF, E_TITLE_SELF
from .qq_core.event_store import EventStore
from .qq_core.env_renderer import (render_env_summary, filter_env_events,
                                   prune_expiry_announced)
from .qq_core.wake_scheduler import WakeScheduler
from .qq_core.message_locator import MessageLocator
from .qq_core.recall_rewriter import RecallRewriter
from .qq_core.emoji_rewriter import EmojiRewriter
from .qq_core.name_resolver import NameResolver
from .tools.qq_profile import QQProfileTool
from .tools.qq_group_profile import QQGroupProfileTool

# 数值配置安全转换（用户手改配置 / WebUI 存成字符串时不崩）
from .qq_core.fmt import safe_int as _safe_int, safe_float as _safe_float


class QQEnhance2Plugin(BasePlugin):
    """QQ增强2 主插件类。"""

    def __init__(self, ctx: PluginContext, cfg: dict):
        super().__init__(ctx, cfg)

        # 配置
        self.collect_poke = self.plugin_cfg.get("collect_poke", True)
        self.collect_emoji_like = self.plugin_cfg.get("collect_emoji_like", True)
        self.collect_emoji_like_env = self.plugin_cfg.get("collect_emoji_like_env", False)
        self.collect_group_ban = self.plugin_cfg.get("collect_group_ban", True)
        self.collect_recall = self.plugin_cfg.get("collect_recall", True)
        self.collect_member_decrease = self.plugin_cfg.get("collect_member_decrease", True)
        self.collect_title = self.plugin_cfg.get("collect_title", False)
        self.collect_card_admin = self.plugin_cfg.get("collect_card_admin", False)

        self.wake_self_emoji = self.plugin_cfg.get("wake_self_emoji", True)
        self.wake_others_emoji = self.plugin_cfg.get("wake_others_emoji", False)
        # 数值配置用安全转换：用户手改配置文件或 WebUI 存成字符串时不应崩
        self.wake_others_emoji_cooldown = _safe_int(
            self.plugin_cfg.get("wake_others_emoji_cooldown"), 20)
        self.wake_profile_like = self.plugin_cfg.get("wake_profile_like", True)
        self.profile_like_dm_only = self.plugin_cfg.get(
            "profile_like_dm_only", False)
        self.wake_title = self.plugin_cfg.get("wake_title", True)
        self.wake_kicked = self.plugin_cfg.get("wake_kicked", True)
        self.wake_member_decrease = self.plugin_cfg.get("wake_member_decrease", True)
        self.profile_like_merge_window_seconds = _safe_int(
            self.plugin_cfg.get("profile_like_merge_window_seconds"), 10)
        self.wake_quiet_hours = self.plugin_cfg.get("wake_quiet_hours", "") or ""
        self.wake_max_per_hour = _safe_int(self.plugin_cfg.get("wake_max_per_hour"), 60)

        self.perceive_window_seconds = _safe_int(self.plugin_cfg.get("perceive_window_seconds"), 900)
        self.perceive_max_events = _safe_int(self.plugin_cfg.get("perceive_max_events"), 20)
        self.perceive_max_per_type = _safe_int(self.plugin_cfg.get("perceive_max_per_type"), 6)
        self.env_max_chars = _safe_int(self.plugin_cfg.get("env_max_chars"), 800)
        self.env_show_time = self.plugin_cfg.get("env_show_time", True)
        self.env_fresh_seconds = _safe_int(self.plugin_cfg.get("env_fresh_seconds"), 300)
        self.env_slow_seconds = _safe_int(self.plugin_cfg.get("env_slow_seconds"), 900)
        self.env_inject_min_interval = _safe_int(self.plugin_cfg.get("env_inject_min_interval"), 0)
        self.env_ttl_hours = _safe_int(self.plugin_cfg.get("env_ttl_hours"), 24)

        self.recall_rewrite = self.plugin_cfg.get("recall_rewrite", True)
        self.recall_show_content = self.plugin_cfg.get("recall_show_content", True)

        self.tools_profile = self.plugin_cfg.get("tools_profile", True)
        self.avatar_describe = self.plugin_cfg.get("avatar_describe", True)
        self.avatar_desc_prompt = self.plugin_cfg.get("avatar_desc_prompt", "") or ""
        self.avatar_desc_timeout = _safe_float(self.plugin_cfg.get("avatar_desc_timeout"), 20.0)
        self.avatar_desc_cache_seconds = _safe_int(
            self.plugin_cfg.get("avatar_desc_cache_seconds"), 60)

        self.resolve_nickname = self.plugin_cfg.get("resolve_nickname", True)
        self.resolve_cache_seconds = _safe_int(
            self.plugin_cfg.get("resolve_cache_seconds"), 10)
        self.recall_in_env = self.plugin_cfg.get("recall_in_env", False)
        # 默认值与 schema.json 保持一致（框架会注入 schema 默认值，这里只是兜底）
        self.wake_hint = self.plugin_cfg.get(
            "wake_hint",
            "可随手用工具互动（回贴表情、回赞等）；别刷屏，也可以不回应。",
        ) or ""
        self.poke_action_text = self.plugin_cfg.get("poke_action_text", False)
        self.debug_log = self.plugin_cfg.get("debug_log", False)

        # 运行时组件
        self.adapter = None
        self.self_id = ""
        self._adapters: dict[str, tuple] = {}   # adapter_name → (adapter, self_id)
        self.store: Optional[EventStore] = None
        self.wake_scheduler: Optional[WakeScheduler] = None
        self.locator: Optional[MessageLocator] = None
        self.emoji_rewriter: Optional[EmojiRewriter] = None
        self.name_resolver: Optional[NameResolver] = None
        self.recall_rewriter: Optional[RecallRewriter] = None
        self._last_env_inject: dict[str, float] = {}  # sid → 上次注入时间
        # 已"补报到期"的禁言事件（eid → 首次补报时间）：只补一次，之后不再渲染
        self._ban_expiry_announced: dict[str, float] = {}
        # 用户最近活跃的会话：(adapter_name, uid) → (sid, ts)
        # 用于 profile_like 这种"没有 group_id 的 notice"：协议端不告诉我们是哪个群/私聊点的赞，
        # 只能往"最近和这个用户有交集的会话"发，避免落到 qq:dm:None 这种幽灵会话。
        self._recent_sessions: dict[tuple[str, str], tuple[str, float]] = {}

    async def initialize(self):
        """初始化运行时组件。

        支持多 QQ 适配器（多号部署）：维护 adapter_name → (adapter, self_id) 映射，
        事件处理时按 event.adapter.name / sid 动态取对应适配器，
        避免"用第一个号的身份处理第二个号的事件"。
        """
        # 收集所有 QQ 适配器（按真实 AdapterManager API）
        self._adapters: dict[str, tuple] = {}   # adapter_name → (adapter, self_id)
        for ada, info in self._iter_qq_adapters():
            name = (
                getattr(info, "name", None)
                or getattr(ada, "name", None)
                or getattr(info, "adapter_id", None)
                or ""
            )
            if not name:
                continue
            cfg = (getattr(ada, "config", None)
                   or getattr(info, "config", None) or {})
            sid_self = self._extract_self_id(ada, info, cfg)
            self._adapters[name] = (ada, sid_self)

        if not self._adapters:
            logger.warning("[QQ增强2] 未找到 QQ 适配器，插件降级运行。")
            return

        # 默认适配器（第一个）—— 用于无会话上下文时的兜底
        first_name = next(iter(self._adapters))
        self.adapter, self.self_id = self._adapters[first_name]
        if len(self._adapters) > 1:
            logger.info(f"[QQ增强2] 检测到 {len(self._adapters)} 个 QQ 适配器: {list(self._adapters)}")

        # 初始化组件（locator 支持多适配器：按 sid 动态取）
        data_dir = self.ctx.get_plugin_data_dir()
        self.store = EventStore(
            data_dir,
            max_events=self.perceive_max_events,
            window_seconds=self.perceive_window_seconds,
            ttl_hours=self.env_ttl_hours,
        )
        await self.store.start()
        await self.store.load_all()

        self.locator = MessageLocator(
            adapter=self.adapter,
            self_id=self.self_id,
            cache_size=8192,
            adapter_resolver=self._resolve_adapter,
        )

        self.wake_scheduler = WakeScheduler(
            ctx=self.ctx,
            adapter=self.adapter,
            self_id=self.self_id,
            message_locator=self.locator,
            wake_self_emoji=self.wake_self_emoji,
            wake_others_emoji=self.wake_others_emoji,
            wake_others_emoji_cooldown=self.wake_others_emoji_cooldown,
            wake_profile_like=self.wake_profile_like,
            wake_title=self.wake_title,
            wake_kicked=self.wake_kicked,
            profile_like_merge_window_seconds=self.profile_like_merge_window_seconds,
            wake_quiet_hours=self.wake_quiet_hours,
            wake_max_per_hour=self.wake_max_per_hour,
            wake_hint=self.wake_hint,
            self_id_resolver=self._resolve_self_id,
        )

        self.recall_rewriter = RecallRewriter(locator=self.locator)
        self.emoji_rewriter = EmojiRewriter()
        self.name_resolver = NameResolver(ttl=self.resolve_cache_seconds)

        logger.info("[QQ增强2] 初始化完成。")

    @staticmethod
    def _get_self_nickname(adapter) -> str:
        """取 bot 自己的昵称（框架在登录时缓存在 bot.self_nickname）。

        Args:
            adapter: 适配器实例。

        Returns:
            昵称；取不到返回空串。
        """
        bot = getattr(adapter, "bot", None)
        if bot is not None:
            nm = getattr(bot, "self_nickname", None)
            if nm:
                return str(nm).strip()
        return ""

    @staticmethod
    def _extract_self_id(ada, info, cfg) -> str:
        """提取适配器对应账号的 self_id（机器人自己的 QQ 号）。

        **框架 QQ 适配器的配置字段是 `bot_pid`，不是 `self_id`**
        （见 core/adapter/src/qq/schema.json + qq.py: `bt_uin=self.config["bot_pid"]`）。
        用错字段会拿到空串 → 所有"这是不是我自己"的判断全部失效
        （例如 bot 自己戳别人会被当成他人事件记录）。

        提取顺序：
        1. config["bot_pid"]（框架标准字段）★
        2. config["self_id"]（部分实现/旧配置）
        3. ada.bot.self_id（运行时由 bot.run() 设置）
        4. info.config 里的同名字段

        Args:
            ada: 适配器实例。
            info: AdapterInfo。
            cfg: 已取到的 config dict。

        Returns:
            self_id 字符串；都取不到时返回空串。
        """
        # 1/2. 从 config 取（bot_pid 优先）
        if isinstance(cfg, dict):
            for key in ("bot_pid", "self_id", "bot_uin", "uin"):
                v = cfg.get(key)
                if v:
                    return str(v)
        # 3. 运行时 bot 实例上的 self_id
        bot = getattr(ada, "bot", None)
        if bot is not None:
            v = getattr(bot, "self_id", None)
            if v:
                return str(v)
        # 4. info.config 兜底
        icfg = getattr(info, "config", None)
        if isinstance(icfg, dict):
            for key in ("bot_pid", "self_id"):
                v = icfg.get(key)
                if v:
                    return str(v)
        return ""

    async def _add_env_event(self, ev, self_id: str) -> bool:
        """把事件加入群动态存储（**只收"其他用户之间"的事件**）。

        群动态的语义是"群里其他人之间发生了什么"。凡是涉及 bot 自己的事件
        （别人戳 bot / bot 禁言别人 / 自己的群名片被改 / 别人撤回 bot 的消息……）
        都不进摘要：
          · 其中大部分由专门通道处理（唤醒、框架原生 [Poke] 文本、QQ增强1 禁言提醒）；
          · 剩下的写进摘要只会污染上下文、让模型误以为"有人在对我做某事"。

        Args:
            ev: 归一化后的事件。
            self_id: 机器人自己的 QQ 号。

        Returns:
            True = 已入 store；False = 因涉及自己而跳过。
        """
        if involves_self(ev, self_id):
            if self.debug_log:
                logger.info(
                    f"[QQ增强2] 跳过与 bot 相关的事件（不进群动态）: "
                    f"{ev.etype} actor={ev.actor} target={ev.target}"
                )
            return False
        await self.store.add(ev)
        return True

    def _env_etypes(self) -> set:
        """环境摘要要渲染的事件类型。

        注意：**撤回（E_RECALL）默认不进摘要** —— 被撤回的原文已经通过
        `recall_rewriter` 在原消息下追加了 `[已撤回｜...]` 标注，摘要里再写
        一遍"X 撤回了一条消息"是重复的无效信息（且会挤占摘要预算）。
        如需保留，打开 `recall_in_env`。

        Returns:
            事件类型集合。
        """
        # 退群/被踢不进摘要（改走 notice 唤醒，对齐 QQ增强1 的入群感知）
        types = {E_POKE, E_EMOJI_LIKE, E_BAN, E_UNBAN, E_TITLE, E_CARD, E_ADMIN}
        if self.recall_in_env:
            types.add(E_RECALL)
        return types

    def _iter_qq_adapters(self):
        """遍历已启动的 QQ 适配器，产出 (adapter_instance, info)。

        **真实 AdapterManager 没有 `get_all_adapters()`**（3.1.2 / 3.2.0 实测）：
          · `get_adapters()`      → dict[str, BaseAdapter]（已启动的实例）★首选
          · `get_adapters_info()` → list[AdapterInfo]（含未启用的，来自配置）
          · `get_adapter(name)`   → BaseAdapter | None
          · `get_adapter_infos()` → get_adapters_info 的 deprecated 别名

        多路径兼容（首选最直接的）：
        1. get_adapters() → dict
        2. get_adapters_info() / get_adapter_infos() + get_adapter(name)
        3. get_all_adapters()（旧实现 / 测试替身）
        4. 内部 _adapters dict（最后手段）

        Yields:
            (adapter, info) 元组，只含平台为 QQ 的。
        """
        mgr = getattr(self.ctx, "adapter_mgr", None)
        if mgr is None:
            return

        def _is_qq(ada, info) -> bool:
            platform = (getattr(info, "platform", None)
                        or getattr(ada, "platform", None) or "")
            return str(platform).upper() == "QQ"

        # 路径 1：get_adapters() → dict[str, adapter]（最直接）
        fn_dict = getattr(mgr, "get_adapters", None)
        if callable(fn_dict):
            try:
                mapping = fn_dict()
                if isinstance(mapping, dict):
                    for ada in mapping.values():
                        info = getattr(ada, "info", None)
                        if _is_qq(ada, info):
                            yield ada, info
                    return
            except Exception:
                pass

        # 路径 2：info 列表 + get_adapter(name)
        infos = None
        for getter in ("get_adapters_info", "get_adapter_infos"):
            fn = getattr(mgr, getter, None)
            if callable(fn):
                try:
                    infos = fn()
                    if isinstance(infos, list):
                        break
                except Exception:
                    infos = None
        if isinstance(infos, list):
            get_one = getattr(mgr, "get_adapter", None)
            for info in infos:
                if str(getattr(info, "platform", None) or "").upper() != "QQ":
                    continue
                if not bool(getattr(info, "enabled", True)):
                    continue
                inst = None
                if callable(get_one):
                    try:
                        inst = get_one(
                            getattr(info, "name", "") or getattr(info, "adapter_id", "")
                        )
                    except Exception:
                        inst = None
                if inst is not None:
                    yield inst, info
            return

        # 路径 3：旧实现 / 测试替身
        get_all = getattr(mgr, "get_all_adapters", None)
        if callable(get_all):
            try:
                for ada in get_all() or []:
                    info = getattr(ada, "info", None)
                    if _is_qq(ada, info):
                        yield ada, info
                return
            except Exception:
                pass

        # 路径 4：内部 dict
        raw = getattr(mgr, "_adapters", None)
        if isinstance(raw, dict):
            for ada in raw.values():
                info = getattr(ada, "info", None)
                if _is_qq(ada, info):
                    yield ada, info

    def _resolve_adapter(self, adapter_name: str):
        """按适配器名取 (adapter, self_id)；找不到时回退默认。

        Args:
            adapter_name: 适配器名（sid 的第一段）。

        Returns:
            (adapter, self_id) 元组。
        """
        adapters = getattr(self, "_adapters", None) or {}
        hit = adapters.get(adapter_name)
        if hit:
            return hit
        return getattr(self, "adapter", None), getattr(self, "self_id", "")

    def _resolve_self_id(self, adapter_name: str) -> str:
        """按适配器名取该号的 self_id（多号部署必需）。

        Args:
            adapter_name: 适配器名。

        Returns:
            self_id 字符串；找不到时回退默认。
        """
        return self._resolve_adapter(adapter_name)[1]

    def _remember_session(self, adapter_name: str, uid: str, sid: str) -> None:
        """记录"某用户最近活跃的会话"（供没有 group_id 的事件兜底）。

        Args:
            adapter_name: 适配器名。
            uid: 用户 QQ。
            sid: 会话 ID（不能是 None 会话）。
        """
        uid = str(uid or "")
        sid = str(sid or "")
        if not uid or uid == "None" or not sid or sid.endswith(":None"):
            return
        self._recent_sessions[(adapter_name, uid)] = (sid, time.time())
        if len(self._recent_sessions) > 2000:
            cutoff = time.time() - 7 * 86400
            self._recent_sessions = {
                k: v for k, v in self._recent_sessions.items() if v[1] >= cutoff
            }
            if len(self._recent_sessions) > 2000:
                items = sorted(self._recent_sessions.items(),
                               key=lambda kv: kv[1][1], reverse=True)[:2000]
                self._recent_sessions = dict(items)

    def _resolve_like_sid(self, adapter_name: str, raw: dict, operator: str) -> str:
        """给"资料卡被赞"选一个真实存在的会话。

        profile_like 的 notice **没有 group_id**，框架会按 `user_id` 建会话，
        而该字段是空的 → `qq:dm:None`。必须自己挑：

        1. 协议端给了 group_id（部分实现会带）→ 用那个群；
        2. 否则用"这个用户最近活跃的会话"（群或私聊都行）；
        3. 都没有 → 用和他的私聊 `adapter:dm:uid`（至少是个合法会话）。

        Args:
            adapter_name: 适配器名。
            raw: 原始 notice。
            operator: 点赞的人 QQ。

        Returns:
            会话 ID。
        """
        gid = str((raw or {}).get("group_id") or "")
        if gid and gid != "None":
            return f"{adapter_name}:gm:{gid}"
        # 默认落到"最近互动会话"（群/私聊按时间比）；开关打开才强制私聊
        if not self.profile_like_dm_only:
            hit = self._recent_sessions.get((adapter_name, str(operator or "")))
            if hit and time.time() - hit[1] <= 7 * 86400:
                return hit[0]
        return f"{adapter_name}:dm:{operator}"

    def _message_in_buffer(self, sid: str, message_id: str) -> bool:
        """判断某条消息是否还在会话缓冲区里（= 会和本次唤醒同一批送给 LLM）。

        只读 `SessionBuffer.buffer` 的列表，**不加锁、不 await**：
        检查长度很小（前文上限一般 5 条），不会阻塞事件循环；
        列表快照用 list() 复制，避免并发修改导致的迭代异常。

        Args:
            sid: 会话 ID。
            message_id: 消息 ID（空=直接 False）。

        Returns:
            True = 还在缓冲区。
        """
        mid = str(message_id or "")
        if not mid:
            return False
        try:
            buf = self.ctx.get_buffer(sid)
            events = list(getattr(buf, "buffer", None) or [])
        except Exception:
            return False
        for ev in events:
            msg = getattr(ev, "message", None)
            if str(getattr(msg, "message_id", "") or "") == mid:
                return True
        return False

    def _self_id_for_event(self, adapter_name: str, event=None) -> str:
        """取当前事件所属账号的 self_id（三级兜底 + 自学习）。

        优先级：
        1. 注册表（initialize 时从 config.bot_pid 提取）
        2. **事件自带的 self_id** —— 框架每条 KiraIMMessage 都填了
           （core/adapter/src/qq/im.py 里 `self_id=str(self_id)`），最可靠。
           注册表因配置字段差异为空时靠这一步兜底：空 self_id 会导致
           · get_stranger_info(user_id="") 挂满 10 秒超时
           · "是否自己"判断全部失效（别人戳 bot 被当他人事件、bot 禁言被当他人禁言）
        3. adapter.bot.self_id（运行时由 bot.run() 设置）

        学到后写回注册表，后续事件零成本。

        Args:
            adapter_name: 适配器名（sid 第一段）。
            event: KiraMessageEvent（可选，用于读 message.self_id）。

        Returns:
            self_id 字符串（可能为空）。
        """
        cur = self._resolve_self_id(adapter_name)
        if cur:
            return str(cur)

        learned = ""
        msg = getattr(event, "message", None) if event is not None else None
        for cand in (
            getattr(msg, "self_id", "") if msg is not None else "",
            # KiraMessageBatchEvent 直接暴露 self_id 属性（= messages[-1].self_id）
            getattr(event, "self_id", "") if event is not None else "",
            getattr(getattr(self._resolve_adapter(adapter_name)[0], "bot", None), "self_id", ""),
        ):
            if cand:
                learned = str(cand)
                break

        if learned:
            # 写回注册表（adapter 实例保持不变）
            try:
                ada = self._adapters.get(adapter_name)
                if ada:
                    self._adapters[adapter_name] = (ada[0], learned)
                else:
                    self._adapters[adapter_name] = (
                        self._resolve_adapter(adapter_name)[0], learned)
            except Exception:
                pass
            logger.info(f"[QQ增强2] 自学习到 {adapter_name} 的 self_id={learned}")
        return learned

    async def terminate(self):
        """清理资源。"""
        if self.wake_scheduler:
            try:
                await self.wake_scheduler.terminate()
            except Exception as e:
                logger.debug(f"[QQ增强2] 唤醒调度器清理异常: {e}")
        if self.store:
            await self.store.stop()
        # 清空贴表情/撤回归档（模块级全局状态）
        if self.emoji_rewriter:
            self.emoji_rewriter.clear()
        if self.name_resolver:
            self.name_resolver.clear()
        logger.info("[QQ增强2] 已停止。")

    # ==================== 感知层：im_message 收集他人事件 ====================

    # 优先级选择：用 LOW（-50）而非 HIGH。
    # 原因：S/Z/anti-harass/QQ增强1 都在 HIGH(50) 或更高，
    # 同优先级时执行顺序取决于插件加载顺序（不稳定）。
    # 我们用 LOW 保证在它们之后执行，避免顺序不确定带来的行为差异。
    # （框架对 notice 事件的 discard/trigger 只设策略不中断，所有 handler 都会跑完，
    #   所以后执行是安全的。）
    @on.im_message(priority=Priority.LOW)
    async def on_im_message(self, event: KiraMessageEvent, *_):
        """感知 notice 事件（他人事件 → store；唤醒级事件 → trigger）。"""
        if not self.store or not self.wake_scheduler:
            return

        # 只处理 QQ 平台
        if event.adapter.platform != "QQ":
            return

        # 只处理 notice 事件
        if not event.is_notice:
            # 记录"谁最近在哪个会话活跃"（profile_like 兜底用）
            try:
                _sid0 = getattr(event.session, "sid", "") or ""
                _an0 = _sid0.split(":", 1)[0] if ":" in _sid0 else ""
                sender0 = getattr(event.message, "sender", None)
                uid0 = str(getattr(sender0, "user_id", "") or "")
                self._remember_session(_an0, uid0, _sid0)
            except Exception:
                pass
            # 顺便缓存群名（普通消息也带 group_name，成本 0）
            grp0 = getattr(event.message, "group", None)
            if self.wake_scheduler and grp0 is not None:
                self.wake_scheduler.remember_group_name(
                    str(getattr(grp0, "group_id", "") or ""),
                    getattr(grp0, "group_name", ""))
            # 记录普通消息到 locator（用于撤回定位）
            if event.message.message_id and event.message.chain:
                msg_text = " ".join(ele.repr for ele in event.message.chain)
                await self.locator.record_incoming(
                    message_id=event.message.message_id,
                    sender=str(event.message.sender.user_id) if event.message.sender else "",
                    sender_name=str(event.message.sender.nickname) if event.message.sender else "",
                    text=msg_text,
                    is_self=(str(event.message.self_id) == str(event.message.sender.user_id) if event.message.sender else False),
                )
            return

        # 归一化 notice 事件
        raw = event.message.raw_message
        if not isinstance(raw, dict):
            return

        group_id = str(raw.get("group_id") or "")
        sid = event.session.sid if hasattr(event.session, 'sid') else f"napcat:gm:{group_id}"

        # 记住群名：唤醒事件是我们自己构造的 KiraMessageEvent，框架不会帮查群名，
        # 不缓存的话 LLM 每轮看到 `[group_name: None group_id: 188395693 ...]`。
        if self.wake_scheduler and group_id:
            grp = getattr(event.message, "group", None)
            if grp is not None:
                self.wake_scheduler.remember_group_name(
                    group_id, getattr(grp, "group_name", ""))

        # 多号部署：按事件所属适配器取该号的 self_id
        # （否则会用第一个号的身份判断"是否自己"，导致 group_ban/poke 误判）
        adapter_name = sid.split(":", 1)[0] if ":" in sid else ""
        cur_self_id = self._self_id_for_event(adapter_name, event)

        qq_event = normalize_notice(
            raw,
            sid=sid,
            self_id=cur_self_id,
            group_id=group_id,
        )
        if not qq_event:
            return

        # 补昵称：框架的 notice 事件只填了 sender.nickname（= actor），
        # **target 的昵称根本没查**（所以会出现"用户769690776(周武) 戳了戳 用户1593462500"）。
        # 这里两层补全：① 先吃框架已查到的 actor 昵称（零成本）
        #              ② 缺的（尤其 target）走 NameResolver（带缓存）
        try:
            sender = getattr(event.message, "sender", None)
            if sender and str(getattr(sender, "user_id", "")) == qq_event.actor:
                nm = str(getattr(sender, "nickname", "") or "").strip()
                if nm and nm != "None":
                    qq_event.actor_name = nm
            if not qq_event.actor_name:
                # 兜底：从 raw_message 里找（部分协议端带 nickname 字段）
                for k in ("nickname", "card", "operator_nick", "sender_nick"):
                    v = raw.get(k)
                    if v:
                        qq_event.actor_name = str(v)
                        break

            # 走 resolver 补 actor / target 的昵称（内部有缓存，命中零成本）
            if self.name_resolver and self.resolve_nickname:
                cur_adapter, _ = self._resolve_adapter(adapter_name)
                gid_for_name = group_id or ""
                if qq_event.actor and not qq_event.actor_name:
                    qq_event.actor_name = await self.name_resolver.resolve(
                        cur_adapter, adapter_name, gid_for_name, qq_event.actor,
                    )
                if qq_event.target and not qq_event.target_name:
                    # 目标是自己时用 bot 自己的昵称（省一次查询）
                    if cur_self_id and str(qq_event.target) == str(cur_self_id):
                        qq_event.target_name = self._get_self_nickname(cur_adapter)
                    if not qq_event.target_name:
                        qq_event.target_name = await self.name_resolver.resolve(
                            cur_adapter, adapter_name, gid_for_name, qq_event.target,
                        )
        except Exception as e:
            logger.debug(f"[QQ增强2] 补昵称失败: {type(e).__name__}: {e}")

        # 记录"这个用户最近在哪个会话活跃"（profile_like 兜底用）
        if qq_event.actor:
            self._remember_session(adapter_name, qq_event.actor, sid)

        # 按类型过滤
        if qq_event.etype == E_POKE and not self.collect_poke:
            return
        if qq_event.etype == E_EMOJI_LIKE and not self.collect_emoji_like:
            return
        if qq_event.etype in (E_BAN, E_UNBAN) and not self.collect_group_ban:
            return
        if qq_event.etype == E_RECALL and not self.collect_recall:
            return
        if qq_event.etype == E_MEMBER_DECREASE and not self.collect_member_decrease:
            return
        if qq_event.etype == E_TITLE and not self.collect_title:
            return
        if qq_event.etype in (E_CARD, E_ADMIN) and not self.collect_card_admin:
            return

        # 唤醒级事件处理
        if qq_event.etype == E_PROFILE_LIKE_SELF:
            # 资料卡点赞：协议端不给 group_id，框架会建出 qq:dm:None 的幽灵会话 →
            # 这里换成"用户最近活跃的会话"（群或私聊），bot 的回复才有人看得到
            qq_event.sid = self._resolve_like_sid(adapter_name, raw, qq_event.actor)
            await self.wake_scheduler.on_profile_like(qq_event)
            return

        if qq_event.etype == E_KICKED_SELF:
            if self.wake_kicked:
                await self.wake_scheduler.trigger_kicked_self(qq_event)
            return

        if qq_event.etype == E_TITLE_SELF:
            if self.wake_title:
                await self.wake_scheduler.trigger_title_self(qq_event)
            return

        if qq_event.etype == E_EMOJI_LIKE:
            # 贴表情：判断是给自己还是他人（与 wake_self_emoji 开关解耦——判定必须准确）
            # 注意：lookup 只做一次，避免 _is_self_message 内外重复查询
            msg_id = qq_event.extra.get("message_id", "")
            info = await self.locator.lookup(msg_id, qq_event.sid) if msg_id else None
            is_self_msg = bool(info.get("is_self", False)) if info else False
            msg_author = info.get("sender", "") if info else ""
            msg_author_name = info.get("sender_name", "") if info else ""
            msg_excerpt = info.get("text", "")[:20] if info else ""

            if is_self_msg:
                # 给自己贴 → 立即唤醒（受 wake_self_emoji 开关控制）
                if self.wake_self_emoji:
                    await self.wake_scheduler.trigger_emoji_like_self(qq_event, msg_author, msg_author_name, msg_excerpt)
                else:
                    # 开关关闭：不唤醒，但也不能当成"他人贴"归档（自己的消息无需原文标注）
                    pass
            elif info is None:
                # 消息作者查不到（重启后 L1 空、get_msg 超时/被限流）：
                # **无法确认这条消息是不是 bot 自己的** —— 不能当成"他人事件"，
                # 否则"别人给 bot 的旧消息贴表情"会漏进群动态（作者显示成"用户(未知)"）。
                # 归档/唤醒也依赖作者信息，一律跳过，只留调试日志。
                if self.debug_log:
                    logger.info(
                        f"[QQ增强2] 跳过作者未知的贴表情事件: "
                        f"sid={sid} message_id={msg_id} actor={qq_event.actor}"
                    )
            else:
                # 他人贴 → 归档到贴表情存储（用于原文标注）
                if self.emoji_rewriter:
                    self.emoji_rewriter.archive(
                        sid=qq_event.sid,
                        message_id=msg_id,
                        message_text=info.get("text", "") if info else "",
                        emoji_ids=qq_event.extra.get("emoji_ids", []),
                        count=qq_event.extra.get("count", 1),
                        operator=qq_event.actor,
                        operator_name=qq_event.actor_name,
                        ts=qq_event.ts,
                    )
                # 检查是否需要唤醒（默认冷却 20 秒）
                if self.wake_scheduler.should_wake_now(qq_event):
                    # 被贴的消息是否还在会话缓冲区？
                    # 在 → 它会被同批次一起送给 LLM（自带 ↑ 标注）→ 唤醒正文不再复述事实
                    # 不在 → 事实是唯一信息来源，照常写
                    await self.wake_scheduler.trigger_others_emoji_like(
                        qq_event, msg_author, msg_author_name, msg_excerpt,
                        in_buffer=self._message_in_buffer(sid, msg_id),
                    )
                elif self.collect_emoji_like_env:
                    # 不唤醒 → 入 store（群动态摘要可见）
                    # 默认关闭：贴表情已由"自己被贴唤醒 / 他人贴表情可唤醒"两路推送覆盖，
                    # 再默认进群动态摘要会重复且浪费 token
                    await self._add_env_event(qq_event, cur_self_id)
            return

        # 撤回事件：归档
        if qq_event.etype == E_RECALL:
            msg_id = qq_event.extra.get("message_id", "")
            if msg_id:
                info = await self.locator.lookup(msg_id, qq_event.sid)
                if info:
                    await self.locator.archive_recall(
                        sid=qq_event.sid,
                        message_id=msg_id,
                        sender=info.get("sender", ""),
                        sender_name=info.get("sender_name", ""),
                        text=info.get("text", ""),
                        ts=qq_event.ts,
                        operator=qq_event.actor,
                        operator_name=qq_event.actor_name,
                        sub_type=qq_event.extra.get("sub_type", ""),
                        is_group=qq_event.extra.get("is_group", False),
                    )
            # 撤回归档后也入 store（环境摘要可见；涉及 bot 自己的不进）
            if self.debug_log:
                logger.info(
                    f"[QQ增强2] 收集事件 {sid}: recall "
                    f"actor={qq_event.actor} target={qq_event.target}"
                )
            await self._add_env_event(qq_event, cur_self_id)
            return

        # 退群/被踢：不进摘要，改成 notice（和 QQ增强1 的入群感知同款）
        if qq_event.etype == E_MEMBER_DECREASE:
            if self.wake_member_decrease:
                await self.wake_scheduler.trigger_member_decrease(qq_event)
            return

        # 其他事件：入 store（涉及 bot 自己的不进群动态）
        if self.debug_log:
            logger.info(
                f"[QQ增强2] 收集事件 {sid}: {qq_event.etype} "
                f"actor={qq_event.actor} target={qq_event.target} "
                f"extra={qq_event.extra}"
            )
        await self._add_env_event(qq_event, cur_self_id)

    # ==================== 环境注入：llm_request 阶段 ====================

    @on.llm_request(priority=Priority.LOW)
    async def on_llm_request(self, event: KiraMessageBatchEvent, req: LLMRequest, *_):
        """注入环境摘要 + 撤回/贴表情改写。

        注意：环境摘要受 env_inject_min_interval 节流，但撤回/贴表情改写
        必须每轮执行（否则标注会丢失），两者解耦。
        """
        if not self.store or not self.recall_rewriter:
            return

        sid = event.sid

        # 环境摘要注入（可节流）
        should_inject = True
        if self.env_inject_min_interval > 0:
            last = self._last_env_inject.get(sid, 0)
            if time.time() - last < self.env_inject_min_interval:
                should_inject = False

        if should_inject:
            now_ts = time.time()
            # 查询下限取三者最大值：全局窗口只作安全阀，真正的显示由分类型新鲜期决定
            lookback = max(self.perceive_window_seconds,
                           self.env_fresh_seconds, self.env_slow_seconds)
            events = await self.store.get_events(
                sid,
                since_ts=now_ts - lookback,
                etypes=self._env_etypes(),
            )
            # 禁言补报标记的容量保护（按 eid 记录，只增不减）
            self._ban_expiry_announced = prune_expiry_announced(
                self._ban_expiry_announced, now_ts)
            # 分类型新鲜期 + 禁言状态机（到期只补报一次）
            events = filter_env_events(
                events,
                now=now_ts,
                fresh_seconds=self.env_fresh_seconds,
                slow_seconds=self.env_slow_seconds,
                expiry_announced=self._ban_expiry_announced,
            )
            # 历史残留兜底：旧版本落盘/内存里可能还有"涉及 bot 自己"的事件
            # （例如别人戳 bot），渲染前按当前 self_id 再过滤一次。
            adapter_name_env = sid.split(":", 1)[0] if ":" in sid else ""
            cur_self_id_env = self._self_id_for_event(adapter_name_env, event)
            if cur_self_id_env:
                events = [e for e in events if not involves_self(e, cur_self_id_env)]
            env_text = render_env_summary(
                events,
                max_per_type=self.perceive_max_per_type,
                max_chars=self.env_max_chars,
                poke_action_text=self.poke_action_text,
                show_time=self.env_show_time,
                now=now_ts,
                expiry_announced=self._ban_expiry_announced,
            )
            if env_text:
                # 插入到 user_prompt 最前（persist=False，不污染历史）
                req.user_prompt.insert(0, Prompt(env_text, name="qq_social_env", persist=False))
                self._last_env_inject[sid] = time.time()
                # 框架日志只打印 persist=True 的 Prompt（避免刷屏），
                # 所以环境块默认不出现在 cmd 日志里。debug_log 开启时单独打一条，
                # 方便确认功能是否生效。
                if self.debug_log:
                    logger.info(f"[QQ增强2] 注入群动态 {sid}: {env_text}")

        # 撤回改写（每轮执行，不受节流影响）
        await self.recall_rewriter.rewrite(
            sid=sid,
            req=req,
            recall_rewrite=self.recall_rewrite,
            recall_show_content=self.recall_show_content,
        )

        # 贴表情标注改写（每轮执行，不受节流影响）
        if self.emoji_rewriter:
            await self.emoji_rewriter.rewrite(
                sid=sid,
                req=req,
                enabled=self.collect_emoji_like,
            )

    # ==================== 工具注入：llm_request 阶段 ====================

    @on.llm_request(priority=Priority.LOW)
    async def inject_tools(self, event: KiraMessageBatchEvent, req: LLMRequest, *_):
        """注入 QQ 工具（按当前会话的适配器绑定，支持多号）。"""
        if not self.adapter:
            return

        # 只处理 QQ 平台
        if event.adapter.platform != "QQ":
            return

        # 按会话的适配器取（多号部署时用对应账号）
        sid = getattr(event, "sid", "") or ""
        adapter_name = sid.split(":", 1)[0] if ":" in sid else ""
        cur_adapter, cur_self_id = self._resolve_adapter(adapter_name)
        if not cur_adapter:
            cur_adapter, cur_self_id = self.adapter, self.self_id

        if self.tools_profile:
            req.tool_set.add(QQProfileTool(ctx=self.ctx, adapter=cur_adapter, self_id=cur_self_id,
                                           avatar_desc_prompt=self.avatar_desc_prompt,
                                           avatar_desc_timeout=self.avatar_desc_timeout,
                                           avatar_describe=self.avatar_describe,
                                           avatar_desc_cache_seconds=self.avatar_desc_cache_seconds))
            req.tool_set.add(QQGroupProfileTool(ctx=self.ctx, adapter=cur_adapter, self_id=cur_self_id))
