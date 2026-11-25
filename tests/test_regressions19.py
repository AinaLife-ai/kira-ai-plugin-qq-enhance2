"""第 21 轮回归：头像下载失败（ConnectError 空消息）与占位值显示。

实机（2026-11-25）：
1. 某个用户的头像恒报 `图片下载失败（ConnectError: ）` —— httpx 的 ConnectError
   常常 `str(e)` 为空，日志里只剩一个空括号；网络抖动/节点问题也没有重试。
2. `电话：-` 这种占位值被当成真实资料显示。
"""

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import qq_core.avatar_view as av  # noqa: E402
from tools.qq_profile import QQProfileTool  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


URL = "https://q1.qlogo.cn/g?b=qq&nk=2386319623&s=640"


def test_err_text_handles_empty_message():
    text = av._err_text(httpx.ConnectError(""))
    check("空消息异常也有可读文本", bool(text.strip()) and "ConnectError" in text, text)
    check("空消息不会只显示空括号", text.strip() not in ("", "()"), text)


def test_retry_then_success():
    """前两次失败（ConnectError），第三次走备用节点成功。"""
    calls = []

    class FakeImage:
        def __init__(self, url):
            self.url = url

        async def to_path(self):
            calls.append(self.url)
            if len(calls) < 3:
                raise httpx.ConnectError("")
            return "/tmp/fake_avatar.jpg"

    orig_img, orig_md5 = av.Image, av._md5_of_file
    av.Image = FakeImage

    async def fake_md5(path):
        return "md5-x"

    av._md5_of_file = fake_md5
    try:
        path, md5 = asyncio.run(av._prepare_image(URL))
    finally:
        av.Image, av._md5_of_file = orig_img, orig_md5

    check("失败后会重试并最终成功", path == "/tmp/fake_avatar.jpg" and md5 == "md5-x", (path, md5))
    check("共尝试 3 次", len(calls) == 3, calls)
    check("第三次换到备用节点", calls[-1] != calls[0] and "q1." not in calls[-1], calls)


def test_client_error_not_retried():
    """4xx（头像不存在）不重试，直接失败。"""
    calls = []

    class FakeImage:
        def __init__(self, url):
            self.url = url

        async def to_path(self):
            calls.append(self.url)
            req = httpx.Request("GET", self.url)
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404", request=req, response=resp)

    orig_img = av.Image
    av.Image = FakeImage
    try:
        try:
            asyncio.run(av._prepare_image(URL))
        except Exception as e:
            err = e
        else:
            err = None
    finally:
        av.Image = orig_img
    check("404 只请求一次", len(calls) == 1, calls)
    check("404 原样抛出", isinstance(err, httpx.HTTPStatusError), err)


def test_placeholders_are_hidden():
    from tests.test_regressions16 import _Bot, _Ev, _tool

    data = {
        "user_id": 2386319623, "nickname": "拾雨", "sex": "female", "age": 18,
        "qid": "-", "email": "N/A", "phone_num": "-", "college": "无",
        "long_nick": "-", "country": "中国", "province": "-", "city": "-",
        "remark": "-",
    }
    tool = _tool(_Bot({"get_stranger_info": {"status": "ok", "data": data}}))

    async def fake(url):
        return "头像描述"

    tool._describe_avatar = fake
    out = asyncio.run(tool.execute(_Ev(), user_id="2386319623", look_avatar=True))
    for needle in ("QID：", "邮箱：", "电话：", "学校：", "备注："):
        check(f"占位值不显示：{needle}", needle not in out, out)
    check("正常字段仍显示", "昵称：拾雨" in out and "年龄：18" in out, out)
    check("签名占位值回落为未提供", "个性签名：（该协议端未提供）" in out, out)
    check("地区只保留有效部分", "地区：中国" in out and "中国 -" not in out, out)


def _main():
    test_err_text_handles_empty_message()
    test_retry_then_success()
    test_client_error_not_retried()
    test_placeholders_are_hidden()
    print("\n" + "=" * 56)
    print(f"回归21: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for n, d in FAIL:
            print(f"  FAIL {n}: {d}")
        sys.exit(1)
    print("=== 全部通过 ===")


if __name__ == "__main__":
    _main()
