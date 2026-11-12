"""贴表情标注改写：llm_request 阶段在消息 Prompt 后追加贴表情标注。

设计要点（对齐撤回改写机制）：
- 贴表情事件（他人给他人贴）在 im_message 时归档到 store。
- llm_request 阶段遍历 user_prompt，定位被贴消息的 Prompt，追加标注：
  \n[贴表情｜用户X(贴的人) 贴了🔥×2]
- 幂等：检查标注是否已追加。
- 与撤回改写互不影响（各自独立归档、独立追加）。
- 缓存验证：原文保留，标注追加在尾部，不破坏前缀缓存。
"""

from __future__ import annotations

import time

try:
    from core.logging_manager import get_logger
    logger = get_logger("qq_enhance2.emoji_rewriter", "cyan")
except ImportError:  # 独立测试环境（无框架）
    class _Logger:
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
    logger = _Logger()

try:
    from core.provider import LLMRequest
    from core.prompt_manager import Prompt
except ImportError:  # 独立测试环境
    LLMRequest = object
    Prompt = object

# 贴表情归档存储：sid → [{message_id, text, emoji_id, count, operator, operator_name, ts, annotation}]
_EMOJI_STORE: dict[str, list[dict]] = {}
_MAX_ARCHIVE = 50


def _build_annotation(sub_entries: list[dict]) -> str:
    """根据结构化条目构建标注文本。

    Args:
        sub_entries: [{operator, operator_name, emoji_ids, count}, ...]

    Returns:
        标注文本（多行）。
    """
    from .emoji_dict import get_emoji_name
    parts = []
    for sub in sub_entries:
        emoji_str = "/".join(get_emoji_name(e) for e in sub.get("emoji_ids", [])) or "?"
        cnt = sub.get("count", 1)
        if cnt > 1:
            emoji_str += f"×{cnt}"
        parts.append(f"用户{sub.get('operator', '')}({sub.get('operator_name', '')}) 贴了{emoji_str}")
    return "\n[贴表情｜" + "；".join(parts) + "]"


def archive_emoji_like(
    *,
    sid: str,
    message_id: str,
    message_text: str,
    emoji_ids: list[str],
    count: int,
    operator: str,
    operator_name: str,
    ts: int,
):
    """归档贴表情事件（供 llm_request 阶段改写）。

    Args:
        sid: 会话 ID。
        message_id: 被贴消息 ID。
        message_text: 被贴消息文本。
        emoji_ids: 表情 ID 列表。
        count: 次数。
        operator: 贴的人 QQ。
        operator_name: 贴的人昵称。
        ts: 时间戳。
    """
    if not message_id:
        return
    # 表情显示：优先 emoji_dict 中文名，否则 emoji_id
    from .emoji_dict import get_emoji_name
    emoji_str = "/".join(get_emoji_name(e) for e in emoji_ids) if emoji_ids else "?"
    if count > 1:
        emoji_str += f"×{count}"

    annotation = f"\n[贴表情｜用户{operator}({operator_name}) 贴了{emoji_str}]"

    store = _EMOJI_STORE.setdefault(sid, [])
    # 同一 message_id 合并标注（多条贴表情合并为一条）
    for item in store:
        if item["message_id"] == message_id:
            # 合并：同一贴的人只累加次数，不同贴的人追加条目
            merged = False
            for sub in item["sub_entries"]:
                if sub["operator"] == operator:
                    sub["count"] += count
                    merged = True
                    break
            if not merged:
                item["sub_entries"].append({
                    "operator": operator,
                    "operator_name": operator_name,
                    "emoji_ids": list(emoji_ids),
                    "count": count,
                })
            item["annotation"] = _build_annotation(item["sub_entries"])
            item["ts"] = ts
            return
    store.append({
        "message_id": message_id,
        "text": message_text,
        "emoji_ids": list(emoji_ids),
        "count": count,
        "operator": operator,
        "operator_name": operator_name,
        "ts": ts,
        "sub_entries": [{
            "operator": operator,
            "operator_name": operator_name,
            "emoji_ids": list(emoji_ids),
            "count": count,
        }],
        "annotation": annotation,
    })
    # LRU 淘汰
    if len(store) > _MAX_ARCHIVE:
        store.pop(0)


async def rewrite_emoji_like(
    *,
    sid: str,
    req: LLMRequest,
    enabled: bool = True,
):
    """改写 req.user_prompt 中的贴表情标注。

    Args:
        sid: 会话 ID。
        req: LLMRequest 实例。
        enabled: 是否启用。
    """
    if not enabled:
        return

    store = _EMOJI_STORE.get(sid)
    if not store:
        return

    rewritten = 0
    for p in req.user_prompt:
        if not isinstance(p, Prompt):
            continue
        if p.name != "message" or p.source != "system":
            continue
        if not isinstance(p.content, str):
            continue

        for item in store:
            original_text = item.get("text", "")
            if not original_text:
                continue
            # 模糊匹配：前 50 字
            match_key = original_text[:50] if len(original_text) >= 50 else original_text
            if match_key not in p.content:
                continue
            # 幂等：检查是否已追加
            annotation = item.get("annotation", "")
            if annotation in p.content:
                continue
            p.content += annotation
            rewritten += 1
            logger.debug(f"[QQ增强2] 贴表情标注已追加: sid={sid}, message_id={item.get('message_id')}")

    # 清空已处理的归档（避免重复改写）
    if rewritten:
        _EMOJI_STORE.pop(sid, None)


def clear_emoji_archive(sid: str):
    """清空 sid 的贴表情归档。"""
    _EMOJI_STORE.pop(sid, None)