"""撤回消息改写：llm_request 阶段在被撤回消息的 Prompt 后追加撤回标注。

设计要点：
- 参考 video_comprehension 的 _rewrite_video_notes 模式。
- 遍历 req.user_prompt 中 name=="message" 的 Prompt，定位被撤回消息的原文。
- 追加标注（幂等：检查是否已追加）。
- 缓存验证：原文保留在历史里，追加标注在尾部，不破坏前缀缓存。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from core.logging_manager import get_logger
from core.provider import LLMRequest
from core.prompt_manager import Prompt

from .message_locator import MessageLocator

logger = get_logger("qq_enhance2.recall_rewriter", "cyan")


class RecallRewriter:
    """撤回消息改写器。

    Attributes:
        locator: MessageLocator 实例（用于获取撤回归档）。
    """

    def __init__(self, *, locator: MessageLocator):
        self.locator = locator

    async def rewrite(
        self,
        *,
        sid: str,
        req: LLMRequest,
        recall_rewrite: bool = True,
        recall_show_content: bool = True,
    ):
        """改写 req.user_prompt 中的撤回标注。

        Args:
            sid: 会话 ID。
            req: LLMRequest 实例。
            recall_rewrite: 是否启用撤回改写。
            recall_show_content: 是否显示原文。
        """
        if not recall_rewrite:
            return

        recalls = await self.locator.get_recalls(sid)
        if not recalls:
            return

        # 遍历 user_prompt 中 name=="message" 的 Prompt
        for p in req.user_prompt:
            if not isinstance(p, Prompt):
                continue
            if p.name != "message" or p.source != "system":
                continue
            if not isinstance(p.content, str):
                continue

            # 定位被撤回消息的原文
            for recall_info in recalls:
                original_text = recall_info.get("text", "")
                if not original_text:
                    continue

                # 检查原文是否在 Prompt 中（取前 50 字做匹配键；
                # 同时要求匹配位置唯一，避免相似消息误伤）
                match_key = original_text[:50] if len(original_text) >= 50 else original_text
                if match_key not in p.content:
                    continue
                if p.content.count(match_key) > 1:
                    # 多个位置命中：无法唯一定位，跳过（保守）
                    logger.debug(f"[QQ增强2] 撤回原文匹配到多处，跳过: {match_key[:20]}")
                    continue

                # 按 recall_show_content 决定是否带原文
                annotation = recall_info.get("annotation", "")
                if not recall_show_content:
                    # 不显示原文：只保留"谁撤回了什么"，去掉原文引用
                    import re as _re
                    annotation = _re.sub(r'（"[^"]*"）', "", annotation)
                    annotation = annotation.replace("撤回此消息", "撤回了一条消息")

                # 检查是否已追加（幂等）
                if annotation in p.content:
                    continue
                if recall_info.get("annotation", "") in p.content:
                    continue

                # 追加撤回标注
                p.content += annotation
                logger.debug(f"[QQ增强2] 撤回标注已追加: sid={sid}, message_id={recall_info.get('message_id')}")

        # 清空已处理的撤回归档（避免重复改写）
        await self.locator.clear_recalls(sid)