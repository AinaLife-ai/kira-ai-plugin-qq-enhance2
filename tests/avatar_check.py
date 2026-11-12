"""头像功能专项审计：设置/查询/读图/群头像 全链路。

检查点：
1. set_qq_avatar 参数格式（file，支持 5 种 URI 形式）
2. 纯 base64 自动补 base64:// 前缀
3. 群头像 set_group_portrait 参数（group_id + file）
4. SnowLuma 不支持群头像 → 明确报错
5. 头像查询：headUrl → VLM 描述（不是只给 URL）
6. VLM 不可用 → 降级显示 URL（不崩）
7. get_qq_avatar 兜底（headUrl 缺失时）
8. 错误处理：无权限/格式错误 都有明确提示
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 先加框架路径（让 test_core 的条件 stub 检测到真实框架，不装 stub）
from tests.real_adapter import ensure_framework_path, make_real_adapter
ensure_framework_path()

from qq_core.fmt import normalize_file_uri

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


def run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


print("\n=== 1. 头像 URI 归一化（协议端要求）===")

cases = [
    ("", "", "空输入"),
    ("https://q.qlogo.cn/x.png", "https://q.qlogo.cn/x.png", "HTTPS URL"),
    ("http://a.com/x.png", "http://a.com/x.png", "HTTP URL"),
    ("base64://iVBORw0KGgo", "base64://iVBORw0KGgo", "已带 base64:// 前缀"),
    ("data:image/png;base64,iVBOR", "data:image/png;base64,iVBOR", "Data URL"),
    ("file:///data/avatar.png", "file:///data/avatar.png", "file:// URL"),
    ("/data/avatar.png", "/data/avatar.png", "绝对路径"),
    ("./avatar.png", "./avatar.png", "相对路径"),
    ("A" * 200, "base64://" + "A" * 200, "纯 base64（200字符）"),
]
for inp, exp, desc in cases:
    got = normalize_file_uri(inp)
    check(f"归一化: {desc}", got == exp, f"{inp[:20]!r} -> {got[:30]!r}")

check("短字符串不当 base64", normalize_file_uri("avatar.png") == "avatar.png")
check("64 位以下 base64 字符不转换", normalize_file_uri("abc123") == "abc123")


print("\n=== 2. set_qq_avatar 调用格式 ===")
src = Path(__file__).parent.parent.joinpath("tools", "qq_profile.py").read_text(encoding="utf-8")
check("调用 set_qq_avatar", '"set_qq_avatar"' in src)
check("参数名是 file（协议端规范）", '"file": avatar_file' in src)
check("使用归一化后的值", "_normalize_file_uri(avatar)" in src)


print("\n=== 3. 群头像调用格式 ===")
gsrc = Path(__file__).parent.parent.joinpath("tools", "qq_group_profile.py").read_text(encoding="utf-8")
check("调用 set_group_portrait", '"set_group_portrait"' in gsrc)
check("传 group_id", '"group_id": group_id' in gsrc)
check("传 file", '"file": portrait_file' in gsrc)
check("群头像在 is_self 判断之前处理（群级操作）",
      gsrc.find("if portrait_file:") < gsrc.find("if not is_self:"),
      f"portrait@{gsrc.find('if portrait_file:')} vs is_self@{gsrc.find('if not is_self:')}")
check("提示协议端可能不支持", "可能不支持" in gsrc)


print("\n=== 4. 头像查询：VLM 读图 ===")
check("调用 desc_img（框架 VLM）", "from core.utils.common_utils import desc_img" in src)
check("提示词留空时跟随框架设置",
      'custom_prompt = (self.avatar_desc_prompt or "").strip() or None' in src)
check("VLM 不可用时降级显示 URL", "if avatar_desc:" in src)
check("lang 跟随框架 locale", "_get_locale_lang" in src)

from tools.qq_profile import QQProfileTool


class CtxDict:
    config = {"locale": {"lang": "ja"}}


class CtxObj:
    class config:
        @staticmethod
        def get_config(k, d=None):
            return "en" if k == "locale.lang" else d


class CtxNone:
    config = None


check("locale: dict 形态", QQProfileTool(ctx=CtxDict())._get_locale_lang() == "ja")
check("locale: 对象形态", QQProfileTool(ctx=CtxObj())._get_locale_lang() == "en")
check("locale: 无 config 回退 zh", QQProfileTool(ctx=CtxNone())._get_locale_lang() == "zh")


print("\n=== 5. headUrl 缺失时的兜底链（CDN 优先 → get_qq_avatar）===")
check("实现 _fetch_avatar_url", "_fetch_avatar_url" in src)
check("调用 get_qq_avatar", '"get_qq_avatar"' in src)
check("实现腾讯头像 CDN 兜底", "_fallback_avatar_url" in src and "q1.qlogo.cn" in src)
check("CDN 优先于 get_qq_avatar",
      src.index("self._fallback_avatar_url(qq)") < src.index("await self._fetch_avatar_url(qq)"),
      "NapCat 不支持 get_qq_avatar，CDN 必须先试")


print("\n=== 6. 头像设置错误处理 ===")
check("失败时返回错误信息（不静默）", "头像修改失败" in src)
check("异常也捕获", "results.append(f\"头像修改失败" in src)
check("成功时报告", 'results.append("头像已更新")' in src)


print("\n=== 7. 端到端：模拟设置头像 ===")


class CtxMin:
    class provider_mgr:
        @staticmethod
        def get_default_vlm():
            raise RuntimeError("no vlm")
    config = {"locale": {"lang": "zh"}}


def make_adapter_with(resp=None):
    """用真实 QQAdapter，通过 api_overrides 控制返回。"""
    overrides = {}
    if resp is not None:
        # 所有 action 都返回这个响应
        overrides = {a: (lambda p, r=resp: r) for a in (
            "set_qq_avatar", "set_group_portrait", "set_qq_profile",
            "set_group_card", "set_group_special_title", "get_qq_avatar",
        )}
    adapter, queue, calls = make_real_adapter(api_overrides=overrides)
    return adapter, calls


async def e2e_avatar():
    adapter, calls = make_adapter_with()
    tool = QQProfileTool(ctx=CtxMin(), adapter=adapter, self_id="100")

    class Ev:
        def is_group_message(self):
            return True

    out = await tool.execute(Ev(), avatar="A" * 200)
    return calls, out


calls, out = run(e2e_avatar())
avatar_calls = [(a, p) for a, p in calls if a == "set_qq_avatar"]
print(f"  调用: {avatar_calls}")
print(f"  输出: {out[:120]}")
check("调用了 set_qq_avatar", len(avatar_calls) == 1)
check("纯 base64 自动补前缀",
      bool(avatar_calls) and avatar_calls[0][1]["file"].startswith("base64://"),
      str(avatar_calls))


print("\n=== 8. 端到端：群头像 ===")
from tools.qq_group_profile import QQGroupProfileTool


async def e2e_group_portrait():
    adapter, calls = make_adapter_with()
    tool = QQGroupProfileTool(ctx=CtxMin(), adapter=adapter, self_id="100")

    class Ev:
        def is_group_message(self):
            return True
        class session:
            session_id = "188395693"

    out = await tool.execute(Ev(), group_portrait="https://a.com/g.png")
    return calls, out


gcalls, gout = run(e2e_group_portrait())
portrait_calls = [(a, p) for a, p in gcalls if a == "set_group_portrait"]
print(f"  调用: {portrait_calls}")
print(f"  输出: {gout}")
check("调用了 set_group_portrait", len(portrait_calls) == 1)
check("参数含 group_id",
      bool(portrait_calls) and portrait_calls[0][1].get("group_id") == "188395693")
check("参数含 file",
      bool(portrait_calls) and portrait_calls[0][1].get("file") == "https://a.com/g.png")


print("\n=== 9. 端到端：失败降级 ===")


async def e2e_fail():
    adapter, _calls = make_adapter_with(resp={"status": "failed", "wording": "权限不足"})
    tool = QQGroupProfileTool(ctx=CtxMin(), adapter=adapter, self_id="100")

    class Ev:
        def is_group_message(self):
            return True
        class session:
            session_id = "1"

    return await tool.execute(Ev(), group_portrait="https://a/g.png")


fout = run(e2e_fail())
print(f"  失败输出: {fout}")
check("失败返回明确提示", "失败" in fout and "权限不足" in fout, fout)


print("\n" + "=" * 60)
print(f"头像功能审计: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 头像功能全部通过 ===")