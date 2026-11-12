"""fetch_emoji_like 工具：拉取指定 QQ 消息的全部贴表情回应（含贴的人、表情 ID、次数）。

设计要点：
- 支持分页（cookie 透传）。
- 补齐"谁贴了"的事实核查。
- 三家协议端兼容：NapCat 的 fetch_emoji_like / LLOneBot 的同名 action / SnowLuma 的同名 action。
"""

from __future__ import annotations

from core.logging_manager import get_logger
from core.chat.message_utils import KiraMessageBatchEvent
from core.utils.tool_utils import BaseTool
from core.plugin import PluginContext

logger = get_logger("qq_enhance2.fetch_emoji_like", "cyan")

# 表情显示（容错导入：独立测试环境无 qq_core 包）
def _load_format_emoji():
    """动态加载 qq_core.emoji_display.format_emoji（兼容多种加载方式）。"""
    try:
        from qq_core.emoji_display import format_emoji as f
        return f
    except ImportError:
        pass
    # 框架内加载时，插件是 plugins.<dirname> 的子包
    try:
        import importlib
        pkg_parts = __name__.split(".")
        if len(pkg_parts) >= 2:
            # __name__ = plugins.<dirname>.tools.fetch_emoji_like → plugins.<dirname>.qq_core
            parent_pkg = ".".join(pkg_parts[:-2])
            mod = importlib.import_module(f"{parent_pkg}.qq_core.emoji_display")
            return mod.format_emoji
    except Exception:
        pass

    def _fallback(emoji_id: str) -> str:
        """降级：无 qq_core 时仅输出 ID（保证不崩，仍可回贴）。"""
        return f"ID: {emoji_id}" if emoji_id else ""

    return _fallback


format_emoji = _load_format_emoji()


class FetchEmojiLikeTool(BaseTool):
    """贴表情查询工具。"""

    name = "fetch_emoji_like"
    description = "拉取指定QQ消息的全部贴表情回应（含贴的人、表情ID、次数）。用于补齐\"谁贴了\"的事实核查。支持分页（cookie 透传）。"
    parameters = {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "QQ消息ID（事件或发送结果中的真实 message_id，负值合法）。",
            },
            "emoji_id": {
                "type": "string",
                "description": "可选：只查指定表情的回应列表。",
            },
            "cookie": {
                "type": "string",
                "description": "分页游标（上一页返回的 cookie，首页不传）。",
            },
        },
        "required": ["message_id"],
    }

    def __init__(self, ctx: PluginContext, adapter=None):
        super().__init__(ctx=ctx)
        self.adapter = adapter

    async def execute(
        self,
        event: KiraMessageBatchEvent,
        *args,
        message_id: str = "",
        emoji_id: str = "",
        cookie: str = "",
        **kwargs,
    ) -> str:
        """执行工具。

        Args:
            event: 消息批次事件。
            message_id: QQ 消息 ID。
            emoji_id: 可选，只查指定表情。
            cookie: 分页游标。

        Returns:
            查询结果文本。
        """
        if not self.adapter or not self.adapter.bot:
            return "错误：适配器不可用。"

        if not message_id:
            return "错误：message_id 不能为空。"

        # 构建参数
        params = {"message_id": message_id}
        if emoji_id:
            params["emoji_id"] = emoji_id
        if cookie:
            params["cookie"] = cookie

        try:
            resp = await self.adapter.bot.send_action("fetch_emoji_like", params)
            if not isinstance(resp, dict):
                return f"查询失败：{resp}"

            # 检查是否成功
            if resp.get("status") != "ok":
                err = resp.get("wording") or str(resp)
                return f"查询失败：{err}"

            data = resp.get("data") or {}
            result_list = data.get("result") or []
            if not result_list:
                return "未找到贴表情回应。"

            # 格式化结果（表情带中文名 + ID，方便 bot 回贴/跟贴）
            result = f"消息 {message_id} 的贴表情回应：\n"
            for item in result_list:
                tiny_id = item.get("tinyId") or ""
                nick_name = item.get("nickName") or ""
                emoji_id_val = item.get("emojiId") or item.get("emoji_id") or ""
                count = item.get("count") or 1
                result += f"  用户 {tiny_id}({nick_name}) 贴了 {format_emoji(emoji_id_val)} ×{count}\n"

            # 分页信息
            cookie_next = data.get("cookie") or ""
            if cookie_next:
                result += f"\n下一页 cookie：{cookie_next}"

            return result
        except Exception as e:
            logger.debug(f"[QQ增强2] fetch_emoji_like 失败: {message_id}: {e}")
            return f"查询失败：{e}"