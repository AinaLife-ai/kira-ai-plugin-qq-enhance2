"""fetch_emoji_like 工具：拉取指定 QQ 消息的全部贴表情回应（含贴的人、表情 ID、次数）。

设计要点：
- 支持分页（cookie 透传）。
- 补齐"谁贴了"的事实核查。
- 三家协议端兼容：
  · NapCat   : emojiId（驼峰）+ data.emojiLikesList / cookie / isLastPage
  · LLOneBot : 同 OB11 action 名，字段 emoji_id
  · SnowLuma : 同 NapCat 形状（"NapCat 形状"注释见源码）

响应结构差异（重要）：
- NapCat/SnowLuma 的 fetch_emoji_like 返回 data 对象：
  { emojiLikesList: [...], cookie, isLastPage, isFirstPage, result: 0, errMsg: "" }
  注意 result 是**状态码**（0=成功），不是列表！
- 因此不能假设 data.result 是数组，必须按字段名取列表。
"""

from __future__ import annotations

from core.logging_manager import get_logger
from core.chat.message_utils import KiraMessageBatchEvent
from core.utils.tool_utils import BaseTool
from core.plugin import PluginContext

logger = get_logger("qq_enhance2.fetch_emoji_like", "cyan")


def _load_format_emoji():
    """动态加载 qq_core.emoji_display.format_emoji（兼容多种加载方式）。"""
    try:
        from qq_core.emoji_display import format_emoji as f
        return f
    except ImportError:
        pass
    try:
        import importlib
        pkg_parts = __name__.split(".")
        if len(pkg_parts) >= 2:
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
    description = "拉取指定QQ消息的全部贴表情回应（贴的人/表情ID/次数），支持分页。"
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
        # 注意：NapCat/SnowLuma 用驼峰 emojiId（必填），LLOneBot 用 emoji_id
        # 同时传两种写法，各协议端取自己认识的（多余的会被忽略）
        params = {"message_id": message_id, "count": 20}
        if emoji_id:
            params["emojiId"] = emoji_id
            params["emoji_id"] = emoji_id
        if cookie:
            params["cookie"] = cookie

        try:
            resp = await self.adapter.bot.send_action("fetch_emoji_like", params)
            if not isinstance(resp, dict):
                return f"查询失败：{resp}"

            if resp.get("status") != "ok":
                err = resp.get("wording") or str(resp)
                return f"查询失败：{err}"

            data = resp.get("data")
            # data 可能是对象（NapCat/SnowLuma）或直接是列表（部分实现）
            if isinstance(data, list):
                result_list = data
                next_cookie = ""
                is_last = True
            elif isinstance(data, dict):
                # NapCat: emojiLikesList / LLOneBot: emojiLikesList / 兼容 result 字段为列表的旧实现
                result_list = (
                    data.get("emojiLikesList")
                    or data.get("emoji_likes_list")
                    or (data.get("result") if isinstance(data.get("result"), list) else [])
                    or []
                )
                next_cookie = data.get("cookie") or ""
                is_last = bool(data.get("isLastPage", not next_cookie))
            else:
                result_list = []
                next_cookie = ""
                is_last = True

            if not result_list:
                return "未找到贴表情回应。"

            # 格式化结果（表情带中文名 + ID，方便 bot 回贴/跟贴）
            result = f"消息 {message_id} 的贴表情回应：\n"
            for item in result_list:
                if not isinstance(item, dict):
                    continue
                tiny_id = item.get("tinyId") or item.get("tiny_id") or ""
                nick_name = item.get("nickName") or item.get("nick_name") or ""
                emoji_val = (
                    item.get("emojiId") or item.get("emoji_id")
                    or item.get("emojiType") or ""
                )
                count = item.get("count") or 1
                result += f"  用户 {tiny_id}({nick_name}) 贴了 {format_emoji(str(emoji_val))} ×{count}\n"

            # 分页信息
            if next_cookie and not is_last:
                result += f"\n下一页 cookie：{next_cookie}"
            elif is_last:
                result += "\n（已是最后一页）"

            return result
        except Exception as e:
            logger.debug(f"[QQ增强2] fetch_emoji_like 失败: {message_id}: {e}")
            return f"查询失败：{e}"