"""qq_group_profile 工具：查询或修改群成员资料（群名片/头衔，读+写合体）。

设计要点：
- 不传 user_id → 操作自己在当前群的群名片/头衔。
- 传 user_id → 查询他人群名片/头衔（只读）。
- 修改群名片或头衔可能需要管理员权限。
- 返回群名片/头衔/角色（owner/admin/member）。
"""

from __future__ import annotations

from core.logging_manager import get_logger
from core.chat.message_utils import KiraMessageBatchEvent
from core.utils.tool_utils import BaseTool
from core.plugin import PluginContext

logger = get_logger("qq_enhance2.qq_group_profile", "cyan")


class QQGroupProfileTool(BaseTool):
    """QQ 群成员资料工具（群名片/头衔，读+写合体）。"""

    name = "qq_group_profile"
    description = "查询或修改群成员资料。不传 user_id 时操作自己在当前群的群名片/头衔；传 user_id 时查询他人群名片/头衔。修改群名片或头衔可能需要管理员权限。"
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "目标QQ号。不传=操作自己；传=查询他人（只读）。",
            },
            "card": {
                "type": "string",
                "description": "新群名片（仅自己可改）。不传=不修改；空字符串=清除群名片。",
            },
            "title": {
                "type": "string",
                "description": "新群头衔（仅自己可改，需管理员/群头衔权限）。不传=不修改；空字符串=清除头衔。",
            },
            "title_duration": {
                "type": "integer",
                "description": "头衔有效时长（秒），-1=永久。默认 -1。",
            },
        },
    }

    def __init__(self, ctx: PluginContext, adapter=None, self_id: str = ""):
        super().__init__(ctx=ctx)
        self.adapter = adapter
        self.self_id = str(self_id) if self_id else ""

    async def execute(
        self,
        event: KiraMessageBatchEvent,
        *args,
        user_id: str = "",
        card: str = "",
        title: str = "",
        title_duration: int = -1,
        **kwargs,
    ) -> str:
        """执行工具。

        Args:
            event: 消息批次事件。
            user_id: 目标 QQ 号（不传=操作自己）。
            card: 新群名片。
            title: 新群头衔。
            title_duration: 头衔有效时长。

        Returns:
            操作结果文本。
        """
        if not self.adapter or not self.adapter.bot:
            return "错误：适配器不可用。"

        # 检查是否群聊
        if not event.is_group_message():
            return "错误：此工具仅在群聊中可用。"

        group_id = event.session.session_id
        target = str(user_id) if user_id else self.self_id
        is_self = (target == self.self_id) if self.self_id else False

        if not is_self:
            # 查询模式
            return await self._query_member(group_id, target)

        # 读写模式：操作自己
        update_fields = {}
        if card is not None and card != "":
            update_fields["card"] = card
        if title is not None and title != "":
            update_fields["title"] = title
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
                resp = await self.adapter.bot.send_action("set_group_special_title", {
                    "group_id": group_id,
                    "user_id": target,
                    "title": update_fields["title"],
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