"""第 17 轮回归：群资料/群头像查看 + 私聊动态标题。

覆盖：
58. `qq_group_profile(look_group_avatar=true)` → 群名/人数/群头像 URL/头像描述
59. 群头像 URL 三家协议端不一致（LLOneBot 给 avatar_url，NapCat/SnowLuma 不给）→ 自己拼 CDN
60. 私聊摘要块头是「私聊动态」，不再是「群动态」
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai_latest")
if not (KIRAAI / "core").is_dir():
    KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
sys.path.insert(0, str(KIRAAI))
sys.path.insert(0, str(ROOT))

from qq_core.env_renderer import env_header, render_env_summary
from qq_core.event_normalizer import QQEvent, E_POKE
from tools.qq_group_profile import QQGroupProfileTool

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


class _Session:
    session_id = "999888777"


class _Ev:
    self_id = "583893217"
    session = _Session()

    @staticmethod
    def is_group_message():
        return True


class _Bot:
    def __init__(self, table):
        self.table = table
        self.calls = []

    async def send_action(self, action, params=None, timeout=10.0):
        self.calls.append((action, dict(params or {})))
        r = self.table.get(action)
        if r is None:
            return {"status": "failed", "retcode": 1404, "data": None}
        return r


class _Adapter:
    def __init__(self, bot):
        self.bot = bot
        self.config = {"bot_pid": "583893217"}


class _Ctx:
    class provider_mgr:
        @staticmethod
        def get_default_vlm():
            return object()

    config = {"locale": {"lang": "zh"}}

    def __init__(self, describe=None):
        self._describe = describe

    async def _noop(self):
        pass


def _tool(bot, *, describe=True):
    tool = QQGroupProfileTool(
        ctx=_Ctx(), adapter=_Adapter(bot), self_id="583893217",
        avatar_describe=describe, avatar_desc_cache_seconds=0,
    )

    async def fake_describe(url):
        return "一只蓝色的猫头"

    tool._describe_avatar = fake_describe
    return tool


def test_group_info_with_avatar():
    bot = _Bot({
        "get_group_info": {"status": "ok", "data": {
            "group_id": 999888777, "group_name": "测试群", "member_count": 42,
            "max_member_count": 200,
        }},
    })
    out = asyncio.run(_tool(bot).execute(_Ev(), look_group_avatar=True))
    check("返回群名", "测试群" in out, out)
    check("返回人数", "42" in out, out)
    check("协议端不给头像 → 自己拼 CDN",
          "https://p.qlogo.cn/gh/999888777/999888777/640" in out, out)
    check("附带头像描述", "一只蓝色的猫头" in out, out)


def test_group_info_prefers_protocol_avatar():
    bot = _Bot({
        "get_group_info": {"status": "ok", "data": {
            "group_id": 999888777, "group_name": "测试群", "member_count": 42,
            "avatar_url": "https://p.qlogo.cn/gh/999888777/999888777/0",
        }},
    })
    out = asyncio.run(_tool(bot).execute(_Ev(), look_group_avatar=True))
    check("优先用协议端返回的 avatar_url", "/0" in out and "640" not in out, out)


def test_group_info_describe_off():
    bot = _Bot({
        "get_group_info": {"status": "ok", "data": {
            "group_id": 999888777, "group_name": "测试群", "member_count": 1,
        }},
    })
    out = asyncio.run(_tool(bot, describe=False).execute(_Ev(), look_group_avatar=True))
    check("关闭识别时只给链接", "群头像内容：（识别已关闭" in out, out)


def test_group_info_fallback_when_action_unsupported():
    bot = _Bot({})  # 协议端不支持 get_group_info
    out = asyncio.run(_tool(bot).execute(_Ev(), look_group_avatar=True))
    check("协议端不支持也能给头像链接",
          "https://p.qlogo.cn/gh/999888777/999888777/640" in out, out)


def test_private_header_named_differently():
    h_group = env_header(1700000000, is_group=True)
    h_dm = env_header(1700000000, is_group=False)
    check("群聊块头是「群动态」", h_group.startswith("[群动态"), h_group)
    check("私聊块头是「私聊动态」", h_dm.startswith("[私聊动态"), h_dm)

    ev = QQEvent(eid="1", etype=E_POKE, sid="qq:dm:10001", ts=1700000000,
                 actor="10002", target="10003")
    ev.actor_name, ev.target_name = "A", "B"
    dm_text = render_env_summary([ev], now=1700000000, is_group=False)
    grp_text = render_env_summary([ev], now=1700000000, is_group=True)
    check("私聊摘要用私聊块头", "私聊动态" in dm_text and "群动态" not in dm_text, dm_text)
    check("群聊摘要用群块头", "群动态" in grp_text, grp_text)


def _main():
    test_group_info_with_avatar()
    test_group_info_prefers_protocol_avatar()
    test_group_info_describe_off()
    test_group_info_fallback_when_action_unsupported()
    test_private_header_named_differently()

    print("\n" + "=" * 60)
    print(f"回归17: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for name, detail in FAIL:
            print(f"  FAIL {name}: {detail}")
        sys.exit(1)
    print("=== 全部通过 ===")


if __name__ == "__main__":
    _main()
