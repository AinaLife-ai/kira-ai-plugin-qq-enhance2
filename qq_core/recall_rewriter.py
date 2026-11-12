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

try:
    from core.logging_manager import get_logger
    from core.provider import LLMRequest
    from core.prompt_manager import Prompt
except ImportError:  # 独立测试环境（无框架）
    class _Logger:
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
    def get_logger(*a, **k):
        return _Logger()
    LLMRequest = object
    Prompt = object

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

        handled_ids: set[str] = set()  # 已成功追加标注的 message_id

        def _try_rewrite(text: str) -> tuple[str, set]:
            """在给定文本里匹配所有撤回归档，返回 (新文本, 命中的 message_id)。"""
            hit: set[str] = set()
            for recall_info in recalls:
                original_text = recall_info.get("text", "")
                if not original_text:
                    continue
                # 取前 50 字做匹配键；要求唯一命中，避免相似消息误伤
                match_key = original_text[:50] if len(original_text) >= 50 else original_text
                if match_key not in text:
                    continue
                if text.count(match_key) > 1:
                    logger.debug(f"[QQ增强2] 撤回原文匹配到多处，跳过: {match_key[:20]}")
                    continue

                annotation = recall_info.get("annotation", "")
                if not recall_show_content:
                    # 不显示原文：去掉形如（"原文摘要"）的引用片段
                    import re as _re
                    annotation = _re.sub(r'（"[^"]*"）', "", annotation)

                mid = recall_info.get("message_id", "")
                # 幂等：已追加过就不再重复
                if annotation in text or recall_info.get("annotation", "") in text:
                    hit.add(mid)
                    continue
                text = text + annotation
                hit.add(mid)
                logger.debug(f"[QQ增强2] 撤回标注已追加: sid={sid}, message_id={mid}")
            return text, hit

        # ① 本轮 user_prompt（Prompt 对象）
        for p in getattr(req, "user_prompt", []) or []:
            if not isinstance(p, Prompt):
                continue
            if getattr(p, "name", None) != "message" or getattr(p, "source", None) != "system":
                continue
            if not isinstance(getattr(p, "content", None), str):
                continue
            new_content, hit = _try_rewrite(p.content)
            if hit:
                p.content = new_content
                handled_ids |= hit

        # ② 历史 messages（OpenAIMessage 列表）
        # 关键：被撤回的消息通常在历史里（不在本轮），只扫 user_prompt 会完全失效。
        for m in getattr(req, "messages", []) or []:
            if getattr(m, "role", None) != "user":
                continue
            content = getattr(m, "content", None)
            if not isinstance(content, str):
                continue
            new_content, hit = _try_rewrite(content)
            if hit:
                m.content = new_content
                handled_ids |= hit

        # 只清空已成功处理的归档（未匹配的保留，等下一轮或过期清理）
        # 注意：不能无条件全清——被撤回消息可能不在本轮 prompt 里，
        # 全清会导致标注永久丢失
        if handled_ids:
            await self.locator.clear_recalls(sid, keep=handled_ids)