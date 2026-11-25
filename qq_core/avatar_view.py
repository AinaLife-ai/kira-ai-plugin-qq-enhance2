"""头像查看：URL → 视觉描述（两级缓存 + VLM）。

为什么抽成独立模块：
- `qq_profile`（用户头像）和 `qq_group_profile`（群头像）都要"看头像"，
  缓存必须共用；否则同一个人/同一个群在两处各自调用一次 VLM，白花钱。
- 缓存分两级：
  · **L1 进程内（URL + TTL）**：`avatar_desc_cache_seconds` 秒内连下载都省了。
    头像 URL 里带 QQ 号/群号，换头像时 URL 不变，所以 L1 必须带 TTL。
  · **L2 框架 ImageDescCache**（`ctx.image_desc_cache`，KiraAI v3 起向插件暴露）：
    按**图片内容 MD5** 落库，换头像自动失效、重启后仍然命中，
    并跟随框架自己的 15/30 天清理策略；框架识别聊天图片时也写同一张表，
    所以"同一个人头像在聊天里出现过"再查资料时可以直接复用描述。

降级策略（任何一步失败都不抛错）：
- 没 VLM → 返回空串，调用方只给 URL；
- 下载/md5/DB 失败 → 跳过缓存，照常调 VLM；
- VLM 超时 → 返回空串。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from typing import Optional

from core.chat.message_elements import Image
from core.logging_manager import get_logger

# 兜底引用：真框架下每次调用都会重新从 core.utils.common_utils 取（见 _resolve_desc_img），
# 这样运行时打补丁/热替换能生效；测试 stub 环境里则是 None，由测试直接替身。
_desc_img = None

logger = get_logger("qq_enhance2.avatar", "cyan")

# L1：URL → (描述, 写入时间)，进程内共享；128 条足够挡住"连续查同一批人"
_L1: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_L1_MAX = 128


# 失败原因去重（同一 URL+原因只告警一次，避免刷日志）
_WARNED: "OrderedDict[str, float]" = OrderedDict()

def clear_l1_cache() -> None:
    """清空进程内缓存（测试用）。"""
    _L1.clear()
    _WARNED.clear()

def _warn_once(url: str, reason: str) -> None:
    """把失败原因打到 WARNING（同一 URL+原因只打一次）。"""
    key = f"{url}|{reason}"
    if key in _WARNED:
        return
    _WARNED[key] = time.time()
    while len(_WARNED) > 128:
        _WARNED.popitem(last=False)
    logger.warning(f"[QQ增强2] 头像识别失败: {reason} | url={url}")


def _l1_get(url: str, ttl: float) -> str:
    """读 L1（带 TTL）。"""
    if ttl <= 0:
        return ""
    item = _L1.get(url)
    if not item:
        return ""
    desc, ts = item
    if time.time() - ts >= ttl:
        _L1.pop(url, None)
        return ""
    _L1.move_to_end(url)
    return desc


def _l1_put(url: str, desc: str, ttl: float) -> None:
    """写 L1（只缓存成功结果）。"""
    if ttl <= 0 or not desc:
        return
    _L1[url] = (desc, time.time())
    _L1.move_to_end(url)
    while len(_L1) > _L1_MAX:
        _L1.popitem(last=False)


async def _md5_of_file(path: str) -> str:
    """本地文件 MD5（与框架 Image.hash_image 的算法一致：对**图片字节**取 md5）。"""
    def _read() -> str:
        h = hashlib.new("md5")
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    return await asyncio.to_thread(_read)


async def _prepare_image(url: str, need_md5: bool):
    """构造 Image 元素；需要时下载一次并算出内容 md5。

    `Image.to_path()` 会把下载结果缓存在元素内部，后续 `desc_img` 复用它，
    所以"算 md5"不会造成第二次下载。

    Args:
        url: 图片 URL。
        need_md5: 是否需要 md5（只有真能用上 L2 缓存时才值得下载）。

    Returns:
        (image, md5)；md5 取不到时为空串。构造失败抛异常，由调用方兜底。
    """
    img = Image(url)
    if not need_md5:
        return img, ""
    path = await img.to_path()
    md5 = await _md5_of_file(path)
    img.md5 = md5  # 框架若再次 hash_image() 不必重复下载
    return img, md5


def _resolve_desc_img():
    """取当前的 desc_img 实现（优先真框架模块，支持运行时补丁）。"""
    try:
        from core.utils.common_utils import desc_img
        return desc_img
    except Exception:
        return _desc_img


def _framework_cache(ctx):
    """取框架的 ImageDescCache（老版本/未初始化时返回 None）。"""
    cache = getattr(ctx, "image_desc_cache", None) if ctx is not None else None
    if cache is None:
        return None
    if not hasattr(cache, "get") or not hasattr(cache, "set"):
        return None
    return cache


async def describe_avatar_ex(
    url: str,
    *,
    vlm=None,
    ctx=None,
    prompt: str = "",
    timeout: float = 20.0,
    ttl: float = 60.0,
    lang: Optional[str] = None,
    max_len: int = 300,
) -> tuple[str, str]:
    """下载头像并用 VLM 生成描述，同时返回**失败原因**。

    与 `describe_avatar` 的区别：失败时不再只给空串，而是给出可读原因，
    方便用户在工具输出里直接看到"为什么没识别"（而不是以为模型坏了）。

    Args:
        url: 头像 URL（腾讯 CDN 或协议端返回的地址）。
        vlm: LLM 客户端；为 None 时尝试从 ctx.provider_mgr 取默认 VLM。
        ctx: PluginContext（用它的 image_desc_cache 做 L2；可为 None）。
        prompt: 自定义提示词；空串=跟随框架 WebUI 的图片识别提示词。
        timeout: VLM 超时（秒）。
        ttl: L1/L2 缓存时长（秒）；<=0 表示完全不走缓存，每次都重新识别。
        lang: 语言代码（决定框架默认提示词）。
        max_len: 描述最大长度（防止 VLM 长篇大论刷爆工具输出）。

    Returns:
        (描述, 失败原因)。成功时原因为空串；失败时描述为空串。
    """
    url = (url or "").strip()
    if not url:
        return "", "头像 URL 为空"

    # ---- L1 ----
    cached = _l1_get(url, ttl)
    if cached:
        return cached, ""

    if vlm is None and ctx is not None:
        try:
            vlm = ctx.provider_mgr.get_default_vlm()
        except Exception as e:
            reason = f"未配置默认视觉模型（models.default_vlm：{type(e).__name__}: {e}）"
            _warn_once(url, reason)
            return "", reason
    if vlm is None:
        reason = "未配置默认视觉模型（models.default_vlm 为空）"
        _warn_once(url, reason)
        return "", reason

    # ---- 图片 + 内容 md5（只有能用上 L2 才下载）----
    fw_cache = _framework_cache(ctx) if ttl > 0 else None
    img = None
    md5 = ""
    prepare_error = ""
    try:
        img, md5 = await _prepare_image(url, need_md5=fw_cache is not None)
    except Exception as e:
        prepare_error = f"{type(e).__name__}: {e}"
        logger.debug(f"[QQ增强2] 头像准备失败（转为直接识别）: {url}: {e}")
        img = None

    # ---- L2：框架 ImageDescCache（按内容 md5，落库、重启仍在）----
    if fw_cache is not None and md5:
        try:
            desc = await fw_cache.get(md5)
            if desc:
                desc = desc.strip()
                _l1_put(url, desc, ttl)
                return desc, ""
        except Exception as e:
            logger.debug(f"[QQ增强2] 读 ImageDescCache 失败: {e}")

    # ---- VLM ----
    if img is None:
        try:
            img = Image(url)
        except Exception as e:
            reason = f"图片地址无效（{type(e).__name__}: {e}）"
            _warn_once(url, reason)
            return "", reason
    desc_fn = _resolve_desc_img()
    if desc_fn is None:
        reason = "框架缺少 desc_img（core.utils.common_utils 不兼容）"
        _warn_once(url, reason)
        return "", reason
    try:
        custom_prompt = (prompt or "").strip() or None
        desc = await asyncio.wait_for(
            desc_fn(client=vlm, image=img, prompt=custom_prompt, lang=lang),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        reason = f"识别超时（>{timeout:g}s）"
        _warn_once(url, reason)
        return "", reason
    except Exception as e:
        reason = f"识别失败（{type(e).__name__}: {str(e)[:120]}）"
        if prepare_error:
            reason += f"；图片准备也失败（{prepare_error[:80]}）"
        _warn_once(url, reason)
        return "", reason

    desc = (desc or "").strip()
    if not desc:
        reason = "模型返回空描述"
        _warn_once(url, reason)
        return "", reason
    if max_len and len(desc) > max_len:
        desc = desc[:max_len] + "…"

    # ---- 回写缓存（ttl<=0 = 用户要求不缓存）----
    if ttl > 0:
        _l1_put(url, desc, ttl)
        if fw_cache is not None and md5:
            try:
                await fw_cache.set(md5, desc)
            except Exception as e:
                logger.debug(f"[QQ增强2] 写 ImageDescCache 失败: {e}")
    return desc, ""

async def describe_avatar(url: str, **kwargs) -> str:
    """兼容旧接口：只返回描述（失败为空串）。"""
    desc, _ = await describe_avatar_ex(url, **kwargs)
    return desc
