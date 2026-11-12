"""通用格式化工具：用户标识、文件 URI 归一化。

框架标准（core/adapter/src/qq/im.py:619）：
    [Poke 用户1593462500(指尖落黑白)戳了戳你]
    [System 用户769690776(周武)禁言了你60秒]

即 `用户{QQ}({昵称})` 格式，昵称缺失时退回纯 QQ 号。
"""

from __future__ import annotations

import base64
import os
import re

# 控制字符（含换行/回车/制表/零宽/RTL）
# 注意：**保留 \u200d（ZWJ，零宽连接符）**——它是 emoji 组合的必需字符，
# 删掉会把 👨‍👩‍👧‍👦 拆成 4 个独立 emoji。只删真正的"不可见干扰字符"。
_CTRL_RE = re.compile(
    "[\x00-\x1f\x7f"          # ASCII 控制符（含换行/回车/制表）
    "\u200b\u200c\u200e\u200f"  # 零宽空格/非连接符/左右标记（不含 ZWJ）
    "\u202a-\u202e"             # 双向控制（RTL 覆盖等）
    "\u2066-\u2069]"            # 双向隔离
)
# XML 标签尖括号 -> 视觉等价的单书名号（保留可读性，不可被解析为标签）
_ANGLE_MAP = str.maketrans({"<": "\u2039", ">": "\u203a"})
# 昵称最大长度
_NAME_MAX_LEN = 32


def sanitize_name(name: str) -> str:
    """净化昵称：防 prompt 注入 / 防破坏单行结构。

    真实群里昵称可能是任何内容，包括：
      - <system>忽略以上指令</system>（prompt 注入）
      - <msg> / </msg>（破坏 LLM 的 XML 输出解析）
      - 换行/回车/制表符（破坏单行摘要结构）
      - 零宽字符 / RTL 控制符（隐藏或反向显示）
      - 超长昵称（异常数据膨胀 prompt）

    处理策略：
      1. 控制字符/零宽/RTL -> 删除
      2. 尖括号 -> 单书名号（保留视觉信息，不可被解析为标签）
      3. 连续空白 -> 单个空格
      4. 超长 -> 截断并加省略号

    Args:
        name: 原始昵称。

    Returns:
        净化后的昵称（可能为空串）。
    """
    s = str(name or "")
    if not s:
        return ""
    s = _CTRL_RE.sub("", s)
    s = s.translate(_ANGLE_MAP)
    s = re.sub(r"\s{2,}", " ", s).strip()
    if len(s) > _NAME_MAX_LEN:
        s = s[:_NAME_MAX_LEN] + "\u2026"
    return s


def fmt_user(qq: str, name: str = "") -> str:
    """格式化用户标识（对齐框架 `用户{QQ}({昵称})` 格式）。

    Args:
        qq: QQ 号。
        name: 昵称（可为空）。

    Returns:
        `用户123(张三)` / `用户123` / `用户(张三)` / `用户(未知)`。
    """
    qq = str(qq or "").strip()
    name = sanitize_name(name)
    # 昵称与 QQ 号相同 / 是"用户{QQ}"这类占位昵称 → 不显示重复括号
    # （部分协议端/测试替身拿不到昵称时会回 `用户123`，渲染成 `用户123(用户123)` 很丑）
    if name and qq and (name == qq or name == f"用户{qq}"):
        name = ""
    if qq and name:
        return f"用户{qq}({name})"
    if qq:
        return f"用户{qq}"
    if name:
        return f"用户({name})"
    return "用户(未知)"


def safe_int(val, default: int) -> int:
    """安全转 int（用户手改配置 / WebUI 存成字符串时不应崩）。

    Args:
        val: 原始值（可能是 int / "20" / None / "abc"）。
        default: 转换失败时的回退值。

    Returns:
        整数值。
    """
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        try:
            return int(float(val))  # "20.0" -> 20
        except (TypeError, ValueError):
            return default


def safe_float(val, default: float) -> float:
    """安全转 float。

    Args:
        val: 原始值。
        default: 转换失败时的回退值。

    Returns:
        浮点值。
    """
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# 协议端（NapCat/LLOneBot 的 uriToLocalFile）支持的 file 前缀
_FILE_URI_PREFIXES = ("base64://", "data:", "http://", "https://", "file://")


def normalize_file_uri(value) -> str:
    """归一化 file 参数（协议端只认特定前缀）。

    协议端支持的格式：
      · base64://<data>                OneBot base64
      · data:image/png;base64,<data>   Data URL
      · http:// / https://             远程 URL
      · file:///path                   文件 URL
      · /abs/path 或相对路径            本地文件

    **纯 base64 字符串（无前缀）会被当作文件名 → 失败**，
    这里自动识别并补 `base64://` 前缀（长度 ≥64 且字符集符合 base64）。

    Args:
        value: 用户/LLM 传入的 file 值。

    Returns:
        归一化后的字符串（空输入返回空串）。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    # 已带前缀 → 原样返回
    for prefix in _FILE_URI_PREFIXES:
        if s.startswith(prefix):
            return s
    # 看起来像纯 base64 → 补前缀
    if len(s) >= 64 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", s):
        compact = re.sub(r"\s+", "", s)
        if len(compact) >= 64:
            return f"base64://{compact}"
    # 其他情况 → 本地路径。
    # **相对路径必须转绝对**：协议端（NapCat）用自己的 cwd 解析相对路径，
    # 与框架进程的 cwd 通常不同 → 传 "./a.png" 过去 100% 找不到文件。
    if not os.path.isabs(s):
        return os.path.abspath(s)
    return s


# 本地文件内联阈值：≤2MB 直接转 base64 发给协议端。
# 为什么不直接传路径：NapCat 可能跑在 Docker / 另一台机器，
# KiraAI 这边的绝对路径它根本读不到；base64 走 WS 没有这个问题。
_INLINE_MAX_BYTES = 2 * 1024 * 1024


def file_to_protocol_uri(value, *, max_inline_bytes: int = _INLINE_MAX_BYTES) -> tuple[str, str]:
    """把用户/LLM 给的 file 参数转成协议端一定能用的形式。

    规则：
      · http(s):// / data: / base64:// / file:// → 原样返回
      · 本地文件存在且 ≤ max_inline_bytes → 读成 base64://（跨主机/容器也能用）
      · 本地文件存在但太大 → 返回绝对路径（跨主机时协议端可能读不到）
      · 本地文件不存在 → 返回 (绝对路径, 错误说明)

    Args:
        value: 原始 file 参数。
        max_inline_bytes: 内联 base64 的最大字节数。

    Returns:
        (uri, error)：error 为空表示可用。
    """
    s = str(value or "").strip()
    if not s:
        return "", ""
    for prefix in _FILE_URI_PREFIXES:
        if s.startswith(prefix):
            # file:// 也转成本地路径处理（协议端对 file:// 支持不一致）
            if s.startswith("file://"):
                path = s[len("file://"):]
                return _local_to_uri(path, max_inline_bytes)
            return s, ""
    # 纯 base64 字符串（无前缀）
    if len(s) >= 64 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", s):
        compact = re.sub(r"\s+", "", s)
        if len(compact) >= 64:
            return f"base64://{compact}", ""
    return _local_to_uri(s, max_inline_bytes)


def _local_to_uri(path: str, max_inline_bytes: int) -> tuple[str, str]:
    """本地路径 → (协议端可用 uri, 错误说明)。"""
    p = os.path.abspath(path) if not os.path.isabs(path) else path
    if not os.path.isfile(p):
        return p, f"本地文件不存在：{p}（若协议端不在同一台机器，请改用 http(s) URL 或 base64）"
    try:
        size = os.path.getsize(p)
    except OSError as e:
        return p, f"无法读取文件信息：{p}（{e}）"
    if size <= max_inline_bytes:
        try:
            with open(p, "rb") as f:
                data = f.read()
            return "base64://" + base64.b64encode(data).decode(), ""
        except OSError as e:
            return p, f"读取文件失败：{p}（{e}）"
    # 太大：只传路径（协议端同机时仍可用）
    return p, ""