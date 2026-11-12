"""QQ增强2 插件入口：hook 注册、模块装配。

功能：
1. 感知层：im_message(LOW) 收集他人事件 → event_store；llm_request(LOW) 注入环境摘要。
2. 唤醒层：唤醒级事件 → wake_scheduler.trigger() → 进入会话 buffer（批次合并）。
3. 撤回/贴表情改写：llm_request 阶段在原文后追加标注。
4. 工具层：qq_profile / qq_group_profile / fetch_emoji_like。

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

from .qq_core.event_normalizer import normalize_notice, E_POKE, E_EMOJI_LIKE, E_BAN, E_UNBAN, E_MEMBER_DECREASE, E_RECALL, E_TITLE, E_CARD, E_ADMIN, E_PROFILE_LIKE_SELF, E_KICKED_SELF, E_TITLE_SELF
from .qq_core.event_store import EventStore
from .qq_core.env_renderer import render_env_summary
from .qq_core.wake_scheduler import WakeScheduler
from .qq_core.message_locator import MessageLocator
from .qq_core.recall_rewriter import RecallRewriter
from .qq_core.emoji_rewriter import EmojiRewriter
from .tools.qq_profile import QQProfileTool
from .tools.qq_group_profile import QQGroupProfileTool
from .tools.fetch_emoji_like import FetchEmojiLikeTool

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
        self.wake_title = self.plugin_cfg.get("wake_title", True)
        self.wake_kicked = self.plugin_cfg.get("wake_kicked", True)
        self.profile_like_merge_window_seconds = _safe_int(
            self.plugin_cfg.get("profile_like_merge_window_seconds"), 10)
        self.wake_quiet_hours = self.plugin_cfg.get("wake_quiet_hours", "") or ""
        self.wake_max_per_hour = _safe_int(self.plugin_cfg.get("wake_max_per_hour"), 60)

        self.perceive_window_seconds = _safe_int(self.plugin_cfg.get("perceive_window_seconds"), 1800)
        self.perceive_max_events = _safe_int(self.plugin_cfg.get("perceive_max_events"), 20)
        self.perceive_max_per_type = _safe_int(self.plugin_cfg.get("perceive_max_per_type"), 6)
        self.env_max_chars = _safe_int(self.plugin_cfg.get("env_max_chars"), 800)
        self.env_inject_min_interval = _safe_int(self.plugin_cfg.get("env_inject_min_interval"), 0)
        self.env_ttl_hours = _safe_int(self.plugin_cfg.get("env_ttl_hours"), 24)

        self.recall_rewrite = self.plugin_cfg.get("recall_rewrite", True)
        self.recall_show_content = self.plugin_cfg.get("recall_show_content", True)

        self.tools_profile = self.plugin_cfg.get("tools_profile", True)
        self.avatar_desc_prompt = self.plugin_cfg.get("avatar_desc_prompt", "") or ""
        self.avatar_desc_timeout = _safe_float(self.plugin_cfg.get("avatar_desc_timeout"), 20.0)
        self.tools_fetch_emoji_like = self.plugin_cfg.get("tools_fetch_emoji_like", True)

        self.debug_log = self.plugin_cfg.get("debug_log", False)

        # 运行时组件
        self.adapter = None
        self.self_id = ""
        self._adapters: dict[str, tuple] = {}   # adapter_name → (adapter, self_id)
        self.store: Optional[EventStore] = None
        self.wake_scheduler: Optional[WakeScheduler] = None
        self.locator: Optional[MessageLocator] = None
        self.emoji_rewriter: Optional[EmojiRewriter] = None
        self.recall_rewriter: Optional[RecallRewriter] = None
        self._last_env_inject: dict[str, float] = {}  # sid → 上次注入时间

    async def initialize(self):
        """初始化运行时组件。

        支持多 QQ 适配器（多号部署）：维护 adapter_name → (adapter, self_id) 映射，
        事件处理时按 event.adapter.name / sid 动态取对应适配器，
        避免"用第一个号的身份处理第二个号的事件"。
        """
        # 收集所有 QQ 适配器
        self._adapters: dict[str, tuple] = {}   # adapter_name → (adapter, self_id)
        for ada in self.ctx.adapter_mgr.get_all_adapters():
            # 平台字段位置因实现而异：
            # - 真实 QQAdapter（框架 3.x）：ada.info.platform（BaseAdapter 无 platform 属性）
            # - 部分实现可能直接暴露 ada.platform
            platform = (
                getattr(getattr(ada, "info", None), "platform", None)
                or getattr(ada, "platform", None)
                or ""
            )
            if str(platform).upper() != "QQ":
                continue
            info = getattr(ada, "info", None)
            name = (
                getattr(info, "name", None)
                or getattr(ada, "name", None)
                or getattr(info, "adapter_id", None)
                or ""
            )
            cfg = getattr(ada, "config", None) or getattr(info, "config", None) or {}
            sid_self = str(cfg.get("self_id", "") or "") if isinstance(cfg, dict) else ""
            if name:
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
            self_id_resolver=self._resolve_self_id,
        )

        self.recall_rewriter = RecallRewriter(locator=self.locator)
        self.emoji_rewriter = EmojiRewriter()

        logger.info("[QQ增强2] 初始化完成。")

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

        # 多号部署：按事件所属适配器取该号的 self_id
        # （否则会用第一个号的身份判断"是否自己"，导致 group_ban/poke 误判）
        adapter_name = sid.split(":", 1)[0] if ":" in sid else ""
        cur_self_id = self._resolve_self_id(adapter_name)

        qq_event = normalize_notice(
            raw,
            sid=sid,
            self_id=cur_self_id,
            group_id=group_id,
        )
        if not qq_event:
            return

        # 补昵称：normalizer 只解析字段，昵称由框架在构造 notice 事件时查到
        # （KiraIMMessage.sender.nickname）。不补的话渲染会出现"用户1005()"空括号。
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
        except Exception:
            pass

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
            # 资料卡点赞：合并防抖
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
                    await self.wake_scheduler.trigger_others_emoji_like(qq_event, msg_author, msg_author_name, msg_excerpt)
                elif self.collect_emoji_like_env:
                    # 不唤醒 → 入 store（群动态摘要可见）
                    # 默认关闭：贴表情已由"自己被贴唤醒 / 他人贴表情可唤醒"两路推送覆盖，
                    # 再默认进群动态摘要会重复且浪费 token
                    await self.store.add(qq_event)
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
            # 撤回归档后也入 store（环境摘要可见）
            await self.store.add(qq_event)
            return

        # 其他事件：入 store
        await self.store.add(qq_event)

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
            events = await self.store.get_events(
                sid,
                since_ts=time.time() - self.perceive_window_seconds,
                etypes={E_POKE, E_EMOJI_LIKE, E_BAN, E_UNBAN, E_MEMBER_DECREASE, E_RECALL, E_TITLE, E_CARD, E_ADMIN},
            )
            env_text = render_env_summary(
                events,
                max_per_type=self.perceive_max_per_type,
                max_chars=self.env_max_chars,
            )
            if env_text:
                # 插入到 user_prompt 最前（persist=False，不污染历史）
                req.user_prompt.insert(0, Prompt(env_text, name="qq_social_env", persist=False))
                self._last_env_inject[sid] = time.time()

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
                                           avatar_desc_timeout=self.avatar_desc_timeout))
            req.tool_set.add(QQGroupProfileTool(ctx=self.ctx, adapter=cur_adapter, self_id=cur_self_id))

        if self.tools_fetch_emoji_like:
            req.tool_set.add(FetchEmojiLikeTool(ctx=self.ctx, adapter=cur_adapter))