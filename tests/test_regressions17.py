"""第 19 轮回归：群头像"总是超时"（下载被算进了 VLM 超时）。

实机（2026-11-25）：群头像识别总是超时，但 VLM 本身很快。
根因：框架 `Image.to_base64()` 对 `url` 类型会**重新下载**；
旧实现只把 url 元素交给 `desc_img`，第二次下载发生在 VLM 的
`asyncio.wait_for(20s)` 里 → 图片稍慢就报"识别超时"，模型根本没跑。

修复：
1. 统一先下载到本地（独立 10s 超时），再构造 **path 类型**元素交给 VLM；
2. 下载超时/失败有独立原因文案，不再伪装成"识别超时"。
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import qq_core.avatar_view as av  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


URL = "https://p.qlogo.cn/gh/115985242/115985242/640"


def _run_with(prepare, desc, timeout=20.0):
    """用替身跑一次 describe_avatar_ex。"""
    orig_prepare, orig_desc = av._prepare_image, av._desc_img
    av._prepare_image = prepare
    av._desc_img = desc
    av.clear_l1_cache()
    try:
        return asyncio.run(av.describe_avatar_ex(URL, vlm=object(), ttl=0, timeout=timeout))
    finally:
        av._prepare_image, av._desc_img = orig_prepare, orig_desc
        av.clear_l1_cache()


def test_vlm_gets_local_path():
    seen = {}

    async def prepare(url, timeout=av.DOWNLOAD_TIMEOUT):
        return "/tmp/fake_avatar.jpg", "md5-abc"

    async def desc(**kw):
        img = kw.get("image")
        seen["file_type"] = getattr(img, "file_type", "?")
        seen["file"] = getattr(img, "file", "?")
        return "一只蓝猫"

    d, reason = _run_with(prepare, desc)
    check("VLM 拿到的是本地文件元素（不会二次下载）", seen.get("file_type") == "path", seen)
    check("文件指向下载好的本地路径", seen.get("file") == "/tmp/fake_avatar.jpg", seen)
    check("识别成功", d == "一只蓝猫" and reason == "", (d, reason))


def test_download_timeout_has_own_reason():
    async def prepare(url, timeout=av.DOWNLOAD_TIMEOUT):
        raise asyncio.TimeoutError()

    async def desc(**kw):  # 不应被调用
        raise AssertionError("下载超时后不应再调 VLM")

    d, reason = _run_with(prepare, desc)
    check("下载超时不调 VLM", d == "", d)
    check("下载超时有独立原因", "图片下载超时" in reason, reason)


def test_download_failure_has_own_reason():
    async def prepare(url, timeout=av.DOWNLOAD_TIMEOUT):
        raise RuntimeError("404 Not Found")

    async def desc(**kw):
        raise AssertionError("下载失败后不应再调 VLM")

    d, reason = _run_with(prepare, desc)
    check("下载失败不调 VLM", d == "", d)
    check("下载失败原因可读", "图片下载失败" in reason and "404" in reason, reason)


def test_download_time_not_counted_in_vlm_timeout():
    """下载花 0.3s、VLM 花 0.01s，VLM 超时设 0.05s → 仍应成功。"""
    async def prepare(url, timeout=av.DOWNLOAD_TIMEOUT):
        await asyncio.sleep(0.3)
        return "/tmp/fake2.jpg", "md5-2"

    async def desc(**kw):
        await asyncio.sleep(0.01)
        return "蓝色猫头"

    d, reason = _run_with(prepare, desc, timeout=0.05)
    check("下载时间不计入 VLM 超时", d == "蓝色猫头" and reason == "", (d, reason))


def test_vlm_timeout_still_works():
    async def prepare(url, timeout=av.DOWNLOAD_TIMEOUT):
        return "/tmp/fake3.jpg", "md5-3"

    async def desc(**kw):
        await asyncio.sleep(0.5)
        return "太慢了"

    d, reason = _run_with(prepare, desc, timeout=0.05)
    check("VLM 自身超时仍能识别", d == "" and "识别超时" in reason, (d, reason))


def _main():
    test_vlm_gets_local_path()
    test_download_timeout_has_own_reason()
    test_download_failure_has_own_reason()
    test_download_time_not_counted_in_vlm_timeout()
    test_vlm_timeout_still_works()
    print("\n" + "=" * 60)
    print(f"回归19: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for n, d in FAIL:
            print(f"  FAIL {n}: {d}")
        sys.exit(1)
    print("=== 全部通过 ===")


if __name__ == "__main__":
    _main()
