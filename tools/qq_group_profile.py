"""qq_group_profile 工具：查询或修改群成员资料（群名片/头衔，读+写合体）。

设计要点：
- 不传 user_id → 操作自己在当前群的群名片/头衔。
- 传 user_id → 查询他人群名片/头衔（只读）。
- 修改群名片或头衔可能需要管理员权限。
- 返回群名片/头衔/角色（owner/admin/member）。
"""

from __future__ import annotations

import asyncio

from core.logging_manager import get_logger
from core.chat.message_utils import KiraMessageBatchEvent
from core.utils.tool_utils import BaseTool
from core.plugin import PluginContext

logger = get_logger("qq_enhance2.qq_group_profile", "cyan")


class QQGroupProfileTool(BaseTool):
    """QQ 群资料工具（群资料/群头像 + 群成员名片/头衔，读+写合体）。"""

    name = "qq_group_profile"
    description = "查询/修改群资料。look_group_avatar=true=看本群（群名/人数/群头像内容）；不传 user_id=自己在本群的群名片/头衔（可改）；传=查他人（只读）。改群头像用 group_portrait（需管理员）。"
    parameters = {
        "type": "object",
        "properties": {
            "look_group_avatar": {
                "type": "boolean",
                "description": "查看本群资料+群头像内容（群名/人数/头像描述）。true 时忽略 user_id",
            },
            "user_id": {
                "type": "string",
                "description": "目标QQ。不传=自己；传=查他人（只读）",
            },
            "card": {
                "type": "string",
                "description": "新群名片（仅自己）。不传=不改；空串=清除",
            },
            "title": {
                "type": "string",
                "description": "新头衔（仅自己，需权限）。不传=不改；空串=清除",
            },
            "title_duration": {
                "type": "integer",
                "description": "有效天数，-1=永久（OB11 单位为天）",
            },
            "group_portrait": {
                "type": "string",
                "description": "新群头像（需群主/管理员）：路径/URL/base64。部分协议端不支持",
            },
        },
    }

    def __init__(self, ctx: PluginContext, adapter=None, self_id: str = "",
                 avatar_desc_prompt: str = "", avatar_desc_timeout: float = 20.0,
                 avatar_describe: bool = True,
                 avatar_desc_cache_seconds: float = 60.0):
        super().__init__(ctx=ctx)
        self.adapter = adapter
        self.self_id = str(self_id) if self_id else ""
        # 头像识别配置与 qq_profile 共用同一组 schema 项（同一套提示词/缓存/开关）
        self.avatar_desc_prompt = avatar_desc_prompt or ""
        self.avatar_desc_timeout = float(avatar_desc_timeout or 20.0)
        self.avatar_describe = bool(avatar_describe)
        try:
            self.avatar_desc_cache_seconds = float(avatar_desc_cache_seconds)
        except (TypeError, ValueError):
            self.avatar_desc_cache_seconds = 60.0

    @staticmethod
    def _prepare_file(value) -> tuple[str, str]:
        """把 file 参数转成协议端可用形式，返回 (uri, 错误说明)。

        与 qq_profile 共用同一套逻辑：本地路径读成 base64://（≤2MB），
        文件不存在时给明确错误。

        Args:
            value: 原始 file 参数。

        Returns:
            (uri, error)。
        """
        try:
            from qq_core.fmt import file_to_protocol_uri
            return file_to_protocol_uri(value)
        except ImportError:
            pass
        try:
            import importlib
            parts = __name__.split(".")
            parent_pkg = ".".join(parts[:-1]) if len(parts) >= 2 else "qq_core"
            mod = importlib.import_module(f"{parent_pkg}.qq_core.fmt")
            return mod.file_to_protocol_uri(value)
        except Exception:
            pass
        return str(value or ""), ""

    def _resolve_self_id(self, event=None) -> str:
        """解析机器人自己的 QQ 号（多级兜底，避免 user_id="" 导致协议端超时）。

        顺序：事件自带 self_id → 注入值（config.bot_pid）→ bot.self_id → config 字段。

        Args:
            event: 消息批次事件（可选）。

        Returns:
            QQ 号字符串；取不到时为空串。
        """
        candidates = [
            getattr(event, "self_id", "") if event is not None else "",
            self.self_id,
            getattr(getattr(self.adapter, "bot", None), "self_id", ""),
        ]
        cfg = getattr(self.adapter, "config", None)
        if isinstance(cfg, dict):
            candidates += [cfg.get("bot_pid"), cfg.get("self_id"), cfg.get("bot_uin")]
        for c in candidates:
            if c:
                s = str(c).strip()
                if s and s != "None":
                    return s
        return ""

    async def execute(
        self,
        event: KiraMessageBatchEvent,
        *args,
        user_id: str = "",
        card: str = None,
        title: str = None,
        title_duration: int = -1,
        group_portrait: str = "",
        look_group_avatar: bool = False,
        **kwargs,
    ) -> str:
        """执行工具。

        Args:
            event: 消息批次事件。
            user_id: 目标 QQ 号（不传=操作自己）。
            card: 新群名片。None=不修改；空字符串=清除群名片。
            title: 新头衔。None=不修改；空字符串=清除头衔。
            title_duration: 头衔有效天数（-1=永久，OB11 单位为天）。
            group_portrait: 新群头像（路径/URL/base64）。空=不修改。
            look_group_avatar: True=查看群资料+群头像（忽略 user_id）。

        Returns:
            操作结果文本。
        """
        if not self.adapter or not self.adapter.bot:
            return "错误：适配器不可用。"

        # 检查是否群聊
        if not event.is_group_message():
            return "错误：此工具仅在群聊中可用。"

        group_id = event.session.session_id

        # 只读：看本群（群名/人数/群头像）——与"看成员"分开，避免和 user_id 语义打架
        if look_group_avatar:
            return await self._query_group(group_id)

        cur_self_id = self._resolve_self_id(event)
        target = str(user_id) if user_id else cur_self_id
        if not target:
            return (
                "查询失败：未指定 QQ 号，且无法确定机器人自己的 QQ 号。\n"
                "请在 QQ 适配器配置里填写「Bot QQ」(bot_pid)，或显式传入 user_id。"
            )
        is_self = (target == cur_self_id) if cur_self_id else False
        # 归一化 file → 协议端可用 uri（本地文件转 base64，跨容器也能用）
        portrait_file, portrait_err = self._prepare_file(group_portrait)
        if portrait_err:
            return f"修改群头像失败：{portrait_err}"
        # 群头像是群级操作（需管理员权限），与"操作自己/他人"无关，单独处理
        if portrait_file:
            try:
                resp = await self.adapter.bot.send_action("set_group_portrait", {
                    "group_id": group_id,
                    "file": portrait_file,
                })
                if isinstance(resp, dict) and resp.get("status") == "ok":
                    return "✅ 群头像已更新"
                err = (resp or {}).get("wording") if isinstance(resp, dict) else resp
                return f"修改群头像失败：{err or '当前协议端可能不支持'}"
            except Exception as e:
                return f"修改群头像失败（当前协议端可能不支持：{type(e).__name__}）"

        if not is_self:
            # 查询模式
            return await self._query_member(group_id, target)

        # 读写模式：操作自己
        # 注意：card/title 默认为 None（不修改），传空字符串表示"清除"
        update_fields = {}
        if card is not None:
            update_fields["card"] = card          # 含空字符串（清除）
        if title is not None:
            update_fields["title"] = title        # 含空字符串（清除）
            if title_duration != -1:
                update_fields["duration"] = title_duration

        if not update_fields:
            # 未传修改字段：返回当前资料
            return await self._query_member(group_id, target)

        # 执行修改
        try:
            if "card" in update_fields:
                resp = await self.adapter.bot.send_action("set_group_card", {
                    "group_id": group_id,
                    "user_id": target,
                    "card": update_fields["card"],
                })
                if not isinstance(resp, dict) or resp.get("status") != "ok":
                    err = (resp or {}).get("wording") or str(resp)
                    return f"修改群名片失败：{err}"

            if "title" in update_fields:
                # 注意：OB11 扩展 action 的参数名是 special_title（不是 title）
                # （对齐 group_manager 插件的已验证实现）
                resp = await self.adapter.bot.send_action("set_group_special_title", {
                    "group_id": group_id,
                    "user_id": target,
                    "special_title": update_fields["title"],
                    "duration": update_fields.get("duration", -1),
                })
                if not isinstance(resp, dict) or resp.get("status") != "ok":
                    err = (resp or {}).get("wording") or str(resp)
                    return f"修改头衔失败：{err}"

            # 返回修改后的完整资料
            return await self._query_member(group_id, target)
        except Exception as e:
            logger.debug(f"[QQ增强2] set_group_card/title 失败: {e}")
            return f"修改失败：{e}"

    async def _query_group(self, group_id: str) -> str:
        """查看本群资料（群名/人数/群头像内容）。

        **为什么头像 URL 自己拼**：三家协议端不一致——
          · LLOneBot `get_group_info` 返回 `avatar_url`
          · NapCat / SnowLuma 只返回群名/人数，**不带头像**
        腾讯群头像 CDN 是公开的：`https://p.qlogo.cn/gh/{群号}/{群号}/640`
        （与 qq_profile 用 `q1.qlogo.cn/g?b=qq&nk={QQ}` 是同一套路），
        所以协议端给 URL 就优先用，不给就自己拼。

        Args:
            group_id: 群号。

        Returns:
            资料文本。
        """
        data: dict = {}
        try:
            resp = await asyncio.wait_for(
                self.adapter.bot.send_action(
                    "get_group_info", {"group_id": group_id, "no_cache": False},
                ),
                timeout=5.0,
            )
            if isinstance(resp, dict):
                data = resp.get("data") or {}
        except Exception as e:
            logger.debug(f"[QQ增强2] get_group_info 失败: {group_id}: {e}")

        name = str(data.get("group_name") or "")
        remark = str(data.get("group_remark") or "")
        try:
            member_count = int(data.get("member_count") or 0)
        except (TypeError, ValueError):
            member_count = 0
        try:
            max_count = int(data.get("max_member_count") or 0)
        except (TypeError, ValueError):
            max_count = 0

        avatar_url = str(data.get("avatar_url") or data.get("avatar") or "")
        if not avatar_url and str(group_id).isdigit():
            avatar_url = f"https://p.qlogo.cn/gh/{group_id}/{group_id}/640"

        lines = [f"群 {group_id} 的资料："]
        lines.append(f"  群名：{name or '（协议端未提供）'}")
        if remark and remark != name:
            lines.append(f"  群备注：{remark}")
        if member_count:
            lines.append(
                f"  人数：{member_count}" + (f"/{max_count}" if max_count else "")
            )
        if data.get("group_all_shut") in (-1, True):
            lines.append("  全员禁言：开启")
        if avatar_url:
            lines.append(f"  群头像：{avatar_url}")
            if self.avatar_describe:
                desc = await self._describe_avatar(avatar_url)
                lines.append(f"  群头像内容：{desc}" if desc else "  群头像内容：（识别失败，仅提供链接）")
            else:
                lines.append("  群头像内容：（识别已关闭，仅提供链接）")
        return "\n".join(lines)

    async def _describe_avatar(self, head_url: str) -> str:
        """看群头像（复用 qq_profile 的同一套缓存/提示词/超时配置）。

        Args:
            head_url: 群头像 URL。

        Returns:
            描述文本；失败返回空串。
        """
        try:
            from qq_core.avatar_view import describe_avatar
        except ImportError:
            return ""

        vlm = None
        try:
            vlm = self.ctx.provider_mgr.get_default_vlm()
        except Exception:
            vlm = None

        return await describe_avatar(
            head_url,
            vlm=vlm,
            ctx=self.ctx,
            prompt=self.avatar_desc_prompt,
            timeout=self.avatar_desc_timeout,
            ttl=self.avatar_desc_cache_seconds,
            lang=self._get_locale_lang(),
        )

    def _get_locale_lang(self) -> str:
        """获取框架语言设置（config 可能是 dict 或 KiraConfig 对象）。"""
        cfg = getattr(self.ctx, "config", None)
        if cfg is None:
            return "zh"
        getter = getattr(cfg, "get_config", None)
        if callable(getter):
            try:
                return getter("locale.lang", "zh") or "zh"
            except Exception:
                pass
        if isinstance(cfg, dict):
            val = cfg.get("locale", {})
            if isinstance(val, dict):
                return val.get("lang") or "zh"
            return cfg.get("locale.lang") or "zh"
        return "zh"

    async def _query_member(self, group_id: str, qq: str) -> str:
        """查询群成员资料。

        Args:
            group_id: 群号。
            qq: QQ 号。

        Returns:
            资料文本。
        """
        try:
            resp = await self.adapter.bot.send_action("get_group_member_info", {
                "group_id": group_id,
                "user_id": qq,
                "no_cache": False,
            })
            data = (resp or {}).get("data") or {}
            if not data:
                return f"查询失败：用户 {qq} 不在群聊中。"

            card = data.get("card") or ""
            nickname = data.get("nickname") or ""
            title = data.get("title") or ""
            role_map = {"owner": "群主", "admin": "管理员", "member": "成员"}
            role_val = data.get("role", "member")
            role_str = role_map.get(role_val, role_val)

            result = f"用户 {qq} 在群聊中的资料：\n"
            result += f"  群名片：{card or '（无）'}\n"
            result += f"  昵称：{nickname or '（未提供）'}\n"
            result += f"  头衔：{title or '（无）'}\n"
            result += f"  角色：{role_str}\n"
            return result
        except Exception as e:
            logger.debug(f"[QQ增强2] get_group_member_info 失败: {group_id}/{qq}: {e}")
            return f"查询失败：{e}"