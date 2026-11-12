"""QQ增强2 核心模块：事件归一化、状态机、渲染、唤醒、定位、撤回、存储。

注意：本包模块分为两类：
- 纯模块（无框架依赖）：event_normalizer、event_store、env_renderer — 可独立单测。
- 框架依赖模块（需 KiraAI 环境）：wake_scheduler、message_locator、recall_rewriter。
__init__ 只导入纯模块，框架依赖模块由 main.py 按需导入，避免独立测试时 ImportError。
"""

from .event_normalizer import QQEvent, normalize_notice
from .event_store import EventStore
from .env_renderer import render_env_summary

__all__ = [
    "QQEvent",
    "normalize_notice",
    "EventStore",
    "render_env_summary",
]