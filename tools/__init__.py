"""QQ增强2 工具层：资料查询/修改、群名片/头衔。"""

from .qq_profile import QQProfileTool
from .qq_group_profile import QQGroupProfileTool

__all__ = [
    "QQProfileTool",
    "QQGroupProfileTool",
]