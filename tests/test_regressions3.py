"""第四轮人工审计发现的 bug 回归测试（Bug 22-28）。

覆盖：
22. 性别字段归一化（int / 数字字符串 / 英文）
23. set_group_special_title 参数名应为 special_title（不是 title）
24. set_qq_profile 逐字段尝试（LLOneBot 不支持 sex）
25. emoji_dict 只在成功加载时标记 _LOADED
26. fetch_emoji_like 参数 emojiId（驼峰）
27. fetch_emoji_like 响应 data.result 是状态码不是列表（用 emojiLikesList）
28. 头像 API 三家均支持 set_qq_avatar（之前误以为无 API）
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_core import _install_framework_stubs  # noqa


# ============ Bug 22: 性别归一化 ============

def test_bug22_sex_normalize():
    from tools.qq_profile import QQProfileTool
    f = QQProfileTool._normalize_sex
    assert f(1) == "男"
    assert f(2) == "女"
    assert f(0) == "未知"
    assert f("1") == "男"          # 数字字符串
    assert f("2") == "女"
    assert f("male") == "男"       # 英文
    assert f("female") == "女"
    assert f(None) == "未知"
    assert f("") == "未知"
    assert f("unknown") == "未知"
    print("  ✓ Bug22: 性别字段归一化（int/str/英文/空）")


# ============ Bug 23: 头衔参数名 ============

def test_bug23_special_title_param():
    """set_group_special_title 必须用 special_title 参数名。"""
    src = Path(__file__).parent.parent.joinpath("tools", "qq_group_profile.py").read_text(encoding="utf-8")
    assert '"special_title": update_fields["title"]' in src, "必须用 special_title 参数"
    assert '"title": update_fields["title"]' not in src, "不能用 title 参数（协议端会忽略）"
    # duration 单位应为天（OB11 规范）
    assert "天" in src, "duration 单位应标注为天"
    print("  ✓ Bug23: 头衔参数名 special_title + duration 单位为天")


# ============ Bug 24: set_qq_profile 逐字段 ============

def test_bug24_field_by_field():
    """LLOneBot 不支持 sex → 逐字段尝试，不能整体失败。"""
    src = Path(__file__).parent.parent.joinpath("tools", "qq_profile.py").read_text(encoding="utf-8")
    # 性别单独尝试
    assert 'if "sex" in update_fields:' in src, "性别应单独尝试"
    # 昵称/签名走 common
    assert 'common = {k: v for k, v in update_fields.items()' in src, "昵称/签名应批量提交"
    print("  ✓ Bug24: set_qq_profile 逐字段尝试（兼容 LLOneBot）")


# ============ Bug 25: emoji_dict 加载标记 ============

def test_bug25_emoji_load_flag():
    """只有加载成功才标记 _LOADED。"""
    src = Path(__file__).parent.parent.joinpath("qq_core", "emoji_dict.py").read_text(encoding="utf-8")
    # _LOADED = True 应在成功分支内
    assert "if _try_load_from(" in src, "应使用统一的加载尝试函数"
    # 失败时不能设置 _LOADED
    lines = src.split("\n")
    for i, line in enumerate(lines):
        if "_LOADED = True" in line:
            # 前 6 行内应有条件判断（if ...:）
            ctx = "\n".join(lines[max(0, i - 6):i])
            assert "if " in ctx, f"_LOADED=True 应在条件分支内: {line}"
    # 加载函数不应无条件设置 _LOADED
    assert "    _LOADED = True\n\n\ndef" not in src, "不应无条件标记已加载"
    print("  ✓ Bug25: emoji_dict 只在成功加载时标记")


# ============ Bug 26/27: fetch_emoji_like 协议兼容 ============

def test_bug26_emoji_like_params():
    """请求参数用驼峰 emojiId（NapCat 要求）。"""
    src = Path(__file__).parent.parent.joinpath("tools", "fetch_emoji_like.py").read_text(encoding="utf-8")
    assert '"emojiId"' in src, "应传驼峰 emojiId（NapCat 要求）"
    assert '"emoji_id"' in src, "同时传下划线（LLOneBot 兼容）"
    print("  ✓ Bug26: fetch_emoji_like 同时传两种参数名")


def test_bug27_emoji_like_response():
    """响应解析用 emojiLikesList，不能把 result 当列表。"""
    src = Path(__file__).parent.parent.joinpath("tools", "fetch_emoji_like.py").read_text(encoding="utf-8")
    assert 'data.get("emojiLikesList")' in src, "应读 emojiLikesList"
    # 不能把 data.result 直接当列表
    assert 'result_list = data.get("result") or []' not in src, "result 是状态码，不能当列表"
    # 兼容 result 是列表的旧实现
    assert 'isinstance(data.get("result"), list)' in src, "应兼容 result 为列表的旧实现"
    print("  ✓ Bug27: 响应解析用 emojiLikesList（result 是状态码）")


# ============ Bug 28: 头像 API ============

def test_bug28_avatar_api():
    """三家协议均支持 set_qq_avatar（纠正"无 API"的错误结论）。"""
    profile_src = Path(__file__).parent.parent.joinpath("tools", "qq_profile.py").read_text(encoding="utf-8")
    group_src = Path(__file__).parent.parent.joinpath("tools", "qq_group_profile.py").read_text(encoding="utf-8")

    # qq_profile 支持 avatar 参数
    assert '"avatar"' in profile_src, "qq_profile 应有 avatar 参数"
    assert '"set_qq_avatar"' in profile_src, "应调用 set_qq_avatar"
    assert '"file": avatar_file' in profile_src, "参数名是 file"

    # qq_group_profile 支持 group_portrait
    assert '"group_portrait"' in group_src, "群工具应有 group_portrait"
    assert '"set_group_portrait"' in group_src, "应调用 set_group_portrait"

    # README 不应再说"不支持头像"
    readme = Path(__file__).parent.parent.joinpath("README.md").read_text(encoding="utf-8")
    assert "设置头像三家协议均无 API" not in readme, "README 不应残留'无 API'的错误说明"
    print("  ✓ Bug28: 头像 API 已实现（set_qq_avatar / set_group_portrait）")


# ============ 额外：协议端能力矩阵核对 ============

def test_protocol_matrix_documented():
    """README 应记录群头像的平台差异（SnowLuma 不支持）。"""
    readme = Path(__file__).parent.parent.joinpath("README.md").read_text(encoding="utf-8")
    assert "SnowLuma 不支持" in readme or "部分协议端不支持" in readme, \
        "应说明群头像的平台差异"
    print("  ✓ 协议端能力差异已在 README 说明")


if __name__ == "__main__":
    test_bug22_sex_normalize()
    test_bug23_special_title_param()
    test_bug24_field_by_field()
    test_bug25_emoji_load_flag()
    test_bug26_emoji_like_params()
    test_bug27_emoji_like_response()
    test_bug28_avatar_api()
    test_protocol_matrix_documented()
    print("=== 第四轮 8 项回归测试全部通过 ===")