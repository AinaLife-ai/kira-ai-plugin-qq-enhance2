"""qq_profile 工具：查询或修改 QQ 用户资料（读+写合体）。

设计要点：
- 不传 user_id → 操作自己（可修改昵称/签名/性别）。
- 传 user_id 且 != self_id → 查询他人（只读）。
- 返回完整资料（昵称/签名/性别/头像URL best-effort）。
- 头像修改：不支持（三家协议均无 API）。
"""

from __future__ import annotations

from core.logging_manager import get_logger
from core.chat.message_utils import KiraMessageBatchEvent
from core.utils.tool_utils import BaseTool
from core.plugin import PluginContext

logger = get_logger("qq_enhance2.qq_profile", "cyan")


class QQProfileTool(BaseTool):
    """QQ 用户资料工具（读+写合体）。"""

    name = "qq_profile"
    description = "查询或修改QQ用户资料。不传 user_id 时操作自己；传 user_id 时查询他人（仅查询，不可修改他人）。修改时只传需要修改的字段。"
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "目标QQ号。不传=操作自己；传=查询他人资料（只读）。",
            },
            "nickname": {
                "type": "string",
                "description": "新昵称（仅自己可改）。不传=不修改。",
            },
            "personal_note": {
                "type": "string",
                "description": "新个性签名（仅自己可改）。不传=不修改。",
            },
            "sex": {
                "type": "integer",
                "description": "性别：0未知 1男 2女（仅自己可改）。不传=不修改。",
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
        nickname: str = "",
        personal_note: str = "",
        sex: int = None,
        **kwargs,
    ) -> str:
        """执行工具。

        Args:
            event: 消息批次事件。
            user_id: 目标 QQ 号（不传=操作自己）。
            nickname: 新昵称。
            personal_note: 新个性签名。
            sex: 新性别。

        Returns:
            操作结果文本。
        """
        if not self.adapter or not self.adapter.bot:
            return "错误：适配器不可用。"

        # 判定读写模式
        target = str(user_id) if user_id else self.self_id
        is_self = (target == self.self_id) if self.self_id else False

        if not is_self:
            # 查询模式：只查询，忽略修改字段
            return await self._query_user(target)

        # 读写模式：操作自己
        # 构建修改字段
        update_fields = {}
        if nickname:
            update_fields["nickname"] = nickname
        if personal_note:
            update_fields["personal_note"] = personal_note
        if sex is not None:
            update_fields["sex"] = int(sex)

        if not update_fields:
            # 未传修改字段：返回当前资料
            return await self._query_user(target)

        # 执行修改
        try:
            resp = await self.adapter.bot.send_action("set_qq_profile", update_fields)
            if not isinstance(resp, dict) or resp.get("status") != "ok":
                err = (resp or {}).get("wording") or str(resp)
                return f"修改失败：{err}"
            # 返回修改后的完整资料
            return await self._query_user(target)
        except Exception as e:
            logger.debug(f"[QQ增强2] set_qq_profile 失败: {e}")
            return f"修改失败：{e}"

    async def _query_user(self, qq: str) -> str:
        """查询用户资料。

        Args:
            qq: QQ 号。

        Returns:
            资料文本。
        """
        try:
            resp = await self.adapter.bot.send_action("get_stranger_info", {"user_id": qq})
            data = (resp or {}).get("data") or {}
            if not data:
                return f"查询失败：用户 {qq} 不存在或无权限。"

            nickname = data.get("nickname") or ""
            personal_note = data.get("sign") or data.get("personal_note") or ""
            sex_map = {0: "未知", 1: "男", 2: "女"}
            sex_val = data.get("sex", 0)
            sex_str = sex_map.get(sex_val, "未知")
            head_url = data.get("headUrl") or data.get("avatar") or ""

            result = f"用户 {qq} 的资料：\n"
            result += f"  昵称：{nickname or '（未提供）'}\n"
            result += f"  个性签名：{personal_note or '（未提供）'}\n"
            result += f"  性别：{sex_str}\n"
            if head_url:
                result += f"  头像：{head_url}\n"
            else:
                result += f"  头像：（该协议端未提供）\n"
            return result
        except Exception as e:
            logger.debug(f"[QQ增强2] get_stranger_info 失败: {qq}: {e}")
            return f"查询失败：{e}"