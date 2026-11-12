"""贴表情标注改写：llm_request 阶段在消息 Prompt 后追加贴表情标注。

设计要点（对齐撤回改写机制）：
- 贴表情事件（他人给他人贴）在 im_message 时归档到 store。
- llm_request 阶段遍历 user_prompt，定位被贴消息的 Prompt，追加标注：
  \n↑该条消息被贴了 🔥×2（用户X(贴的人)）
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

_MAX_ARCHIVE = 50


class EmojiRewriter:
    """贴表情归档 + 原文标注改写（实例级状态）。

    为什么用实例而不是模块级全局：
    框架以 `plugins.<id>.qq_core.emoji_rewriter` 加载模块，
    而直接 `from qq_core.emoji_rewriter import ...` 会得到**另一个模块实例**，
    两者的模块级全局字典互不相通。实例级状态避免这类"影子状态"问题，
    也让插件重载时自然清空。
    """

    def __init__(self, max_archive: int = _MAX_ARCHIVE):
        # sid → [{message_id, text, emoji_ids, count, operator, operator_name, ts, annotation, sub_entries}]
        self._store: dict[str, list[dict]] = {}
        self.max_archive = max_archive

    # ===== 归档 =====

    def archive(self, *, sid: str, message_id: str, message_text: str,
                emoji_ids: list, count=1, operator: str = "",
                operator_name: str = "", ts: int = 0) -> None:
        """归档一条贴表情事件（同一 message_id 合并）。"""
        if not message_id:
            return
        from .emoji_display import format_emoji_list
        from .fmt import fmt_user
        try:
            cnt = int(float(count))
        except (TypeError, ValueError):
            cnt = 1
        ids = list(emoji_ids) if isinstance(emoji_ids, (list, tuple)) else []
        emoji_str = format_emoji_list(ids, cnt)
        # ↑ 明确指向"上面那条消息"，避免多条消息时无法判断哪条被贴
        annotation = f"\n↑该条消息被贴了 {emoji_str}（{fmt_user(operator, operator_name)}）"

        store = self._store.setdefault(sid, [])
        for item in store:
            if item["message_id"] != message_id:
                continue
            merged = False
            for sub in item["sub_entries"]:
                if sub["operator"] == operator:
                    sub["count"] += cnt
                    for e in ids:
                        if e and e not in sub["emoji_ids"]:
                            sub["emoji_ids"].append(e)
                    merged = True
                    break
            if not merged:
                item["sub_entries"].append({
                    "operator": operator, "operator_name": operator_name,
                    "emoji_ids": list(ids), "count": cnt,
                })
            item["annotation"] = _build_annotation(item["sub_entries"])
            item["ts"] = ts
            return

        store.append({
            "message_id": message_id,
            "text": message_text,
            "emoji_ids": list(ids),
            "count": cnt,
            "operator": operator,
            "operator_name": operator_name,
            "ts": ts,
            "sub_entries": [{
                "operator": operator, "operator_name": operator_name,
                "emoji_ids": list(ids), "count": cnt,
            }],
            "annotation": annotation,
        })
        if len(store) > self.max_archive:
            store.pop(0)

    # ===== 改写 =====

    async def rewrite(self, *, sid: str, req, enabled: bool = True) -> None:
        """把归档的贴表情标注追加到对应消息后。"""
        if not enabled:
            return
        store = self._store.get(sid)
        if not store:
            return

        rewritten_ids = set()

        def _try_rewrite(text: str) -> tuple[str, set]:
            """在给定文本里匹配所有归档，返回 (新文本, 命中的 message_id)。"""
            hit = set()
            # Notice 行是"事件描述"，不是被贴的那条消息。
            # 唤醒文案里会带原文摘要，若不跳过，摘要会命中 → 给通知也追加一遍标注，
            # 和原消息上的标注重复（用户实机反馈的就是这个）。
            from .fmt import is_notice_line
            if is_notice_line(text):
                return text, hit
            for item in store:
                original_text = item.get("text", "")
                if not original_text:
                    continue
                match_key = original_text[:50] if len(original_text) >= 50 else original_text
                if match_key not in text:
                    continue
                if text.count(match_key) > 1:
                    logger.debug(f"[QQ增强2] 贴表情原文匹配到多处，跳过: {match_key[:20]}")
                    continue
                annotation = item.get("annotation", "")
                mid = item.get("message_id", "")
                if annotation in text:
                    hit.add(mid)
                    continue
                text = text + annotation
                hit.add(mid)
                logger.debug(f"[QQ增强2] 贴表情标注已追加: sid={sid}, message_id={mid}")
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
                rewritten_ids |= hit

        # ② 历史 messages（OpenAIMessage 列表）
        # 关键：被贴表情的消息通常在历史里而非本轮，只扫 user_prompt 会漏掉。
        for m in getattr(req, "messages", []) or []:
            if getattr(m, "role", None) != "user":
                continue
            content = getattr(m, "content", None)
            if not isinstance(content, str):
                continue
            new_content, hit = _try_rewrite(content)
            if hit:
                m.content = new_content
                rewritten_ids |= hit

        if rewritten_ids:
            remain = [it for it in store if it.get("message_id") not in rewritten_ids]
            if remain:
                self._store[sid] = remain
            else:
                self._store.pop(sid, None)

    # ===== 维护 =====

    def clear(self, sid: str = "") -> None:
        """清空归档（不传 sid 则全清）。"""
        if sid:
            self._store.pop(sid, None)
        else:
            self._store.clear()

    def get(self, sid: str) -> list:
        """读取某会话的归档（测试/诊断用）。"""
        return list(self._store.get(sid, []))


# 模块级默认实例（兼容旧 API；插件应优先用 EmojiRewriter 实例）
_DEFAULT = EmojiRewriter()
_EMOJI_STORE = _DEFAULT._store


def _build_annotation(sub_entries: list[dict]) -> str:
    """根据结构化条目构建标注文本。

    Args:
        sub_entries: [{operator, operator_name, emoji_ids, count}, ...]

    Returns:
        标注文本（多行）。
    """
    from .emoji_display import format_emoji_list
    from .fmt import fmt_user
    parts = []
    for sub in sub_entries:
        emoji_str = format_emoji_list(sub.get("emoji_ids", []), sub.get("count", 1))
        # 用 fmt_user 统一格式：昵称为空时不会产生"用户1005()"空括号
        parts.append(f"{emoji_str}（{fmt_user(sub.get('operator', ''), sub.get('operator_name', ''))}）")
    # ↑ 明确指向"上面那条消息"
    return "\n↑该条消息被贴了 " + "；".join(parts)

# ===== 模块级兼容 API（转发到默认实例，保持向后兼容）=====

def archive_emoji_like(*, sid: str, message_id: str, message_text: str,
                       emoji_ids: list, count=1, operator: str = "",
                       operator_name: str = "", ts: int = 0) -> None:
    """归档贴表情事件（转发到默认实例）。

    新代码建议用 EmojiRewriter 实例（避免模块级状态在多实例/重载时失配）。
    """
    _DEFAULT.archive(
        sid=sid, message_id=message_id, message_text=message_text,
        emoji_ids=emoji_ids, count=count, operator=operator,
        operator_name=operator_name, ts=ts,
    )


async def rewrite_emoji_like(*, sid: str, req, enabled: bool = True) -> None:
    """改写贴表情标注（转发到默认实例）。"""
    await _DEFAULT.rewrite(sid=sid, req=req, enabled=enabled)


def clear_emoji_archive(sid: str = "") -> None:
    """清空贴表情归档（转发到默认实例）。"""
    _DEFAULT.clear(sid)
