"""第 20 轮回归：资料卡字段补全（年龄/生日/QID/等级/注册时间/地区/学校/邮箱/电话/会员/备注）。

三家协议端字段名不同：
  · NapCat   : qqLevel / reg_time / is_vip(实为SVIP) / vip_level / province
  · SnowLuma : qq_level / birthday_* / college / email / is_qq_vip
  · LLOneBot : level / birthday_* / city / country / is_svip / login_days
展示规则：**有就显示、没有就跳过**（不输出一堆"（未提供）"占位）。
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tools.qq_profile import QQProfileTool  # noqa: E402
from tools.qq_group_profile import QQGroupProfileTool  # noqa: E402

SELF = "583893217"
USER = "769690776"


class _Bot:
    def __init__(self, table):
        self.table = table

    async def send_action(self, action, params=None, timeout=10.0):
        return self.table.get(action, {"status": "failed", "data": None})


class _Adapter:
    def __init__(self, bot):
        self.bot = bot
        self.config = {"bot_pid": SELF}


class _Ctx:
    class provider_mgr:
        @staticmethod
        def get_default_vlm():
            return object()

    config = {"locale": {"lang": "zh"}}
    image_desc_cache = None


class _Ev:
    self_id = SELF

    @staticmethod
    def is_group_message():
        return False


class _GroupEv:
    self_id = SELF

    @staticmethod
    def is_group_message():
        return True

    class session:
        session_type = "gm"
        session_id = "115985242"


def _profile_tool(data):
    tool = QQProfileTool(ctx=_Ctx(), adapter=_Adapter(_Bot({
        "get_stranger_info": {"status": "ok", "data": data},
    })), self_id=SELF)

    async def fake_desc(url):
        return "蓝发男生"

    tool._describe_avatar = fake_desc
    return tool


NAPCAT = {
    "user_id": USER, "nickname": "周武", "sex": "male", "age": 24, "qid": "zhouwu",
    "qqLevel": 48, "long_nick": "漏舟载酒", "reg_time": 1439251200,
    "country": "中国", "province": "江苏", "city": "南京",
    "is_vip": True, "is_years_vip": True, "vip_level": 8, "remark": "老周",
}
SNOWLUMA = {
    "user_id": USER, "nickname": "周武", "sex": "male", "age": 24, "qid": "zhouwu",
    "qq_level": 48, "long_nick": "漏舟载酒", "reg_time": 1439251200,
    "birthday_year": 2001, "birthday_month": 3, "birthday_day": 15,
    "country": "中国", "province": "江苏", "city": "南京",
    "college": "南京大学", "email": "a@b.c", "is_vip": True, "vip_level": 7,
}
LLONEBOT = {
    "user_id": USER, "nickname": "周武", "sex": "male", "age": 24, "qid": "zhouwu",
    "level": 48, "login_days": 1200, "reg_time": 1439251200, "long_nick": "漏舟载酒",
    "birthday_year": 2001, "birthday_month": 3, "birthday_day": 15,
    "city": "南京", "country": "中国",
    "is_vip": True, "is_svip": True, "is_years_vip": False, "vip_level": 9, "remark": "老周",
}


def _out(data, **kw):
    return asyncio.run(_profile_tool(data).execute(_Ev(), user_id=USER, **kw))


def test_napcat_fields():
    out = _out(NAPCAT)
    for needle in ("年龄：24", "QQ等级：48", "注册时间：2015-08-11", "地区：中国 江苏 南京",
                   "会员：超级会员", "备注：老周", "QID：zhouwu"):
        assert needle in out, (needle, out)


def test_snowluma_fields():
    out = _out(SNOWLUMA)
    for needle in ("生日：2001-03-15", "QQ等级：48", "地区：中国 江苏 南京",
                   "学校：南京大学", "邮箱：a@b.c", "QID：zhouwu", "年龄：24"):
        assert needle in out, (needle, out)


def test_llonebot_fields():
    out = _out(LLONEBOT)
    for needle in ("生日：2001-03-15", "QQ等级：48", "累计登录：1200 天",
                   "地区：中国 南京", "备注：老周"):
        assert needle in out, (needle, out)


def test_missing_fields_are_skipped():
    """没有的字段不输出占位，避免刷 token。"""
    out = _out({"user_id": USER, "nickname": "周武", "sex": "male"})
    for needle in ("年龄：", "生日：", "QID：", "QQ等级：", "注册时间：", "地区：",
                   "学校：", "邮箱：", "电话：", "会员：", "累计登录："):
        assert needle not in out, (needle, out)


def test_group_member_extra_fields():
    data = {
        "group_id": 115985242, "user_id": USER, "nickname": "周武", "card": "小周",
        "sex": "male", "age": 24, "join_time": 1439251200,
        "last_sent_time": 1764000000, "level": 12, "role": "admin", "title": "管理",
    }
    tool = QQGroupProfileTool(
        ctx=_Ctx(),
        adapter=_Adapter(_Bot({"get_group_member_info": {"status": "ok", "data": data}})),
        self_id=SELF,
    )
    out = asyncio.run(tool.execute(_GroupEv(), user_id=USER))
    for needle in ("群名片：小周", "性别：男", "年龄：24", "角色：管理员",
                   "群等级：12", "入群时间：2015-08-11", "最后发言："):
        assert needle in out, (needle, out)



def test_profile_detail_off_keeps_basic_fields():
    """关闭详细信息：只保留昵称/性别/签名/头像等基础字段。"""
    tool = _profile_tool(NAPCAT)
    tool.profile_detail = False
    out = asyncio.run(tool.execute(_Ev(), user_id=USER))
    for needle in ("年龄：", "生日：", "QID：", "QQ等级：", "注册时间：", "地区：",
                   "学校：", "邮箱：", "电话：", "会员：", "备注：", "累计登录："):
        assert needle not in out, (needle, out)
    for needle in ("昵称：周武", "性别：男", "个性签名：漏舟载酒", "头像：https://"):
        assert needle in out, (needle, out)


def _group_tool(info_data, ex_data=None, detail=True):
    table = {"get_group_info": {"status": "ok", "data": info_data}}
    if ex_data is not None:
        table["get_group_info_ex"] = {"status": "ok", "data": ex_data}
    tool = QQGroupProfileTool(ctx=_Ctx(), adapter=_Adapter(_Bot(table)), self_id=SELF)
    tool.profile_detail = detail

    async def fake_desc(url):
        return "蓝色猫头"

    tool._describe_avatar = fake_desc
    return tool


GROUP_INFO = {
    "group_id": 115985242, "group_name": "测试群", "group_remark": "内部群",
    "member_count": 42, "max_member_count": 200,
    "group_all_shut": -1, "group_create_time": 1557619200,
    "group_level": 12, "group_memo": "这是一个用来测试的群",
}


def test_group_info_detail_on():
    tool = _group_tool(GROUP_INFO)
    out = asyncio.run(tool.execute(_GroupEv(), look_group_avatar=True))
    for needle in ("群名：测试群", "人数：42/200", "建群时间：2019-05-12",
                   "群等级：12", "全员禁言：开启", "群简介：这是一个用来测试的群",
                   "群头像：https://p.qlogo.cn/gh/115985242/115985242/640"):
        assert needle in out, (needle, out)


def test_group_info_detail_off():
    tool = _group_tool(GROUP_INFO, detail=False)
    out = asyncio.run(tool.execute(_GroupEv(), look_group_avatar=True))
    for needle in ("建群时间：", "群等级：", "全员禁言：", "群简介："):
        assert needle not in out, (needle, out)
    for needle in ("群名：测试群", "人数：42/200", "群头像：https://"):
        assert needle in out, (needle, out)


def test_group_info_ex_merge():
    """基础接口没给的字段，从 get_group_info_ex 补。"""
    base = {"group_id": 115985242, "group_name": "测试群", "member_count": 42}
    ex = {"groupCreateTime": 1557619200, "groupLevel": 9, "groupMemo": "扩展简介"}
    tool = _group_tool(base, ex)
    out = asyncio.run(tool.execute(_GroupEv(), look_group_avatar=True))
    for needle in ("建群时间：2019-05-12", "群等级：9", "群简介：扩展简介"):
        assert needle in out, (needle, out)


def test_group_member_detail_off():
    data = {
        "user_id": USER, "nickname": "周武", "card": "小周", "sex": "male", "age": 24,
        "join_time": 1439251200, "last_sent_time": 1764000000, "level": 12,
        "role": "member", "title": "",
    }
    tool = QQGroupProfileTool(
        ctx=_Ctx(),
        adapter=_Adapter(_Bot({"get_group_member_info": {"status": "ok", "data": data}})),
        self_id=SELF,
    )
    tool.profile_detail = False
    out = asyncio.run(tool.execute(_GroupEv(), user_id=USER))
    for needle in ("性别：", "年龄：", "群等级：", "入群时间：", "最后发言："):
        assert needle not in out, (needle, out)
    for needle in ("群名片：小周", "昵称：周武", "角色：成员"):
        assert needle in out, (needle, out)



def test_all_shut_hidden_unless_enabled():
    """全员禁言只在开启时出现：关闭 / 字段缺失都不显示。"""
    tool = _group_tool({**GROUP_INFO, "group_all_shut": 0})
    out = asyncio.run(tool.execute(_GroupEv(), look_group_avatar=True))
    assert "全员禁言" not in out, out

    tool2 = _group_tool({k: v for k, v in GROUP_INFO.items() if k != "group_all_shut"})
    out2 = asyncio.run(tool2.execute(_GroupEv(), look_group_avatar=True))
    assert "全员禁言" not in out2, out2

    # 字符串形式 "-1" 也要识别为开启
    tool3 = _group_tool({**GROUP_INFO, "group_all_shut": "-1"})
    out3 = asyncio.run(tool3.execute(_GroupEv(), look_group_avatar=True))
    assert "全员禁言：开启" in out3, out3


def _main():
    test_napcat_fields()
    test_snowluma_fields()
    test_llonebot_fields()
    test_missing_fields_are_skipped()
    test_group_member_extra_fields()
    test_profile_detail_off_keeps_basic_fields()
    test_group_info_detail_on()
    test_group_info_detail_off()
    test_group_info_ex_merge()
    test_group_member_detail_off()
    test_all_shut_hidden_unless_enabled()
    print("回归20: 全部通过")


if __name__ == "__main__":
    _main()
