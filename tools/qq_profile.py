"""qq_profile 工具：查询或修改 QQ 用户资料（读+写合体）。

设计要点：
- 不传 user_id → 操作自己（可修改昵称/签名/性别）。
- 传 user_id 且 != self_id → 查询他人（只读）。
- 返回完整资料（昵称/签名/性别/头像）。
- 头像查看：下载头像 → 用框架 VLM 生成图片描述（真正"看"头像，而非只给 URL）。
- 头像修改：不支持（三家协议均无 API）。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

from core.logging_manager import get_logger
from core.chat.message_utils import KiraMessageBatchEvent
from core.chat.message_elements import Image
from core.utils.tool_utils import BaseTool
from core.plugin import PluginContext

logger = get_logger("qq_enhance2.qq_profile", "cyan")

# 头像描述缓存：同一 URL 1 分钟内不重复调 VLM。
# 每次 VLM 调用都是钱 + 几秒延迟；1 分钟足够挡住"连续查同一个人"的重复调用，
# 又不会让刚换的头像在描述里停留太久。
# 只缓存成功结果（失败可能是网络抖动，不该被记住）。
_AVATAR_DESC_CACHE: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_AVATAR_DESC_TTL = 60.0
_AVATAR_DESC_MAX = 128


class QQProfileTool(BaseTool):
    """QQ 用户资料工具（读+写合体）。"""

    name = "qq_profile"
    description = "查询/修改QQ资料。不传 user_id=操作自己（可改）；传=查他人（只读）。查询时会看头像内容。改头像：avatar 传路径/URL/base64 或 recent（用当前对话最新图片）。"
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "目标QQ。不传=自己；传=查他人（只读）",
            },
            "nickname": {
                "type": "string",
                "description": "新昵称（仅自己）。不传=不改",
            },
            "personal_note": {
                "type": "string",
                "description": "新签名（仅自己）。不传=不改；空串=清除",
            },
            "sex": {
                "type": "integer",
                "description": "0未知/1男/2女（仅自己，部分协议端不支持）",
            },
            "look_avatar": {
                "type": "boolean",
                "description": "是否用视觉模型识别头像内容（默认 true）；false=只返回头像链接，省时间/token",
            },
            "avatar": {
                "type": "string",
                "description": "新头像（仅自己）：本地路径/URL/base64，或 recent=对话里最新图片。不传=不改",
            },
        },
    }

    def __init__(self, ctx: PluginContext, adapter=None, self_id: str = "",
                 avatar_desc_prompt: str = "", avatar_desc_timeout: float = 20.0,
                 avatar_describe: bool = True):
        super().__init__(ctx=ctx)
        self.adapter = adapter
        self.self_id = str(self_id) if self_id else ""
        self.avatar_desc_prompt = avatar_desc_prompt or ""
        self.avatar_desc_timeout = float(avatar_desc_timeout or 20.0)
        # 总开关（配置项 avatar_describe）：关掉后连 VLM 都不调，只给头像链接
        self.avatar_describe = bool(avatar_describe)

    # ==================== 基础取值工具 ====================

    @staticmethod
    def _pick(data: dict, *keys: str) -> str:
        """从 dict 里按顺序取第一个非空字符串值。

        Args:
            data: 协议端返回的 data 字典。
            *keys: 候选字段名（兼容不同协议端的命名差异）。

        Returns:
            取到的字符串；都没有时返回空串。
        """
        if not isinstance(data, dict):
            return ""
        for k in keys:
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return str(v)
        return ""

    def _resolve_self_id(self, event=None) -> str:
        """解析机器人自己的 QQ 号（多级兜底）。

        **为什么必须兜底**：`user_id=""` 发给协议端会挂满 10 秒才超时
        （实机日志：`qq_profile args: {}` → `请求 get_stranger_info 超时`）。

        取值顺序：
        1. 事件自带的 self_id（框架每条 KiraIMMessage 都填，最可靠）
        2. 注入的 self_id（适配器 config.bot_pid）
        3. adapter.bot.self_id（运行时）
        4. adapter.config["bot_pid"] / ["self_id"]

        Args:
            event: 消息批次事件（可选）。

        Returns:
            QQ 号字符串；都取不到时为空串。
        """
        candidates = [
            # KiraMessageBatchEvent.self_id 是 property（messages[-1].self_id），
            # messages 为空时会抛 IndexError → 必须兜住
            self._safe_attr(event, "self_id"),
            self.self_id,
            getattr(getattr(self.adapter, "bot", None), "self_id", ""),
        ]
        cfg = getattr(self.adapter, "config", None)
        if isinstance(cfg, dict):
            candidates += [cfg.get("bot_pid"), cfg.get("self_id"), cfg.get("bot_uin")]
        for c in candidates:
            if c:
                s = str(c).strip()
                if s and s != "None":
                    return s
        return ""

    @staticmethod
    def _safe_attr(obj, name: str) -> str:
        """安全读属性（property 可能抛异常，例如 messages 为空时的 self_id）。"""
        try:
            v = getattr(obj, name, "") if obj is not None else ""
            return str(v) if v else ""
        except Exception:
            return ""

    async def _api(self, action: str, params: dict, timeout: float = 6.0) -> dict:
        """调用协议端 action 并返回 data 字典（带独立超时）。

        框架的 send_action 默认 10s 超时；工具内再收紧到 6s，
        避免一个卡死的接口把整轮工具链拖住。

        Args:
            action: OneBot action 名。
            params: 参数。
            timeout: 超时秒数。

        Returns:
            `data` 字典（没有则空 dict）。

        Raises:
            asyncio.TimeoutError: 协议端超时未响应。
            Exception: 其它协议端错误。
        """
        resp = await asyncio.wait_for(
            self.adapter.bot.send_action(action, params),
            timeout=timeout,
        )
        if isinstance(resp, dict):
            data = resp.get("data")
            if isinstance(data, dict):
                return data
        return {}


    async def execute(
        self,
        event: KiraMessageBatchEvent,
        *args,
        user_id: str = "",
        nickname: str = None,
        personal_note: str = None,
        sex: int = None,
        avatar: str = "",
        look_avatar: bool = True,
        **kwargs,
    ) -> str:
        """执行工具。

        Args:
            event: 消息批次事件。
            user_id: 目标 QQ 号（不传=操作自己）。
            nickname: 新昵称。None=不修改（QQ 昵称不可为空）。
            personal_note: 新个性签名。None=不修改；空字符串=清除签名。
            sex: 新性别。None=不修改。
            avatar: 新头像（图片路径/URL/base64）。空=不修改。

        Returns:
            操作结果文本。
        """
        if not self.adapter or not self.adapter.bot:
            return "错误：适配器不可用。"

        # 判定读写模式
        # 关键：不传 user_id 时用**多级兜底**解析 self_id。
        # 实机日志里 self_id 为空 → user_id="" → 协议端挂 10s 超时。
        cur_self_id = self._resolve_self_id(event)
        target = str(user_id) if user_id else cur_self_id
        if not target:
            return (
                "查询失败：未指定 QQ 号，且无法确定机器人自己的 QQ 号。\n"
                "请在 QQ 适配器配置里填写「Bot QQ」(bot_pid)，"
                "或显式传入 user_id。"
            )
        is_self = (target == cur_self_id) if cur_self_id else False

        group_id = self._event_group_id(event)

        if not is_self:
            # 查询模式：只查询，忽略修改字段
            return await self._query_user(
                target, is_self=False, group_id=group_id,
                describe=bool(look_avatar) and self.avatar_describe)

        # 读写模式：操作自己
        # 构建修改字段
        # 注意：nickname 传空字符串无意义（QQ 不允许空昵称），故只判断非空；
        #      personal_note 允许空字符串（= 清除签名），故用 is not None
        update_fields = {}
        if nickname:
            update_fields["nickname"] = nickname
        if personal_note is not None:
            update_fields["personal_note"] = personal_note
        if sex is not None:
            # LLM 可能传 "male" / "1" / 1 → 统一转 int，非法值忽略（不整条报错）
            try:
                update_fields["sex"] = int(sex)
            except (TypeError, ValueError):
                logger.debug(f"[QQ增强2] 忽略非法 sex 值: {sex!r}")

        # 头像走独立 action（set_qq_avatar），参数是 file
        # 「换头像」最常见的用法是"用我刚发的图"。
        # 注意：框架的 message_formatter 会把图片渲染成
        #   [Image <描述>, file_path: data/xxx.jpg]
        # 所以 LLM **本来就能**拿到路径（S/Z/默认聊天插件都会走到这个格式化器）；
        # 但不同插件/模型抄路径容易出错，额外提供 avatar="recent" 直接从
        # 当前批次的消息链里取最新一张图，省掉"抄路径"这一步。
        avatar_raw = str(avatar or "").strip()
        if avatar_raw.lower() in ("recent", "last", "latest", "current",
                                  "最近", "刚才", "最新"):
            avatar_raw = self._latest_image_file(event)
            if not avatar_raw:
                return (
                    "修改头像失败：当前消息里没有图片。\n"
                    "请先发送一张图片，或把 avatar 传成图片 URL / 本地路径 / base64。"
                )
        avatar_file, avatar_err = self._prepare_file(avatar_raw)
        if avatar_err:
            return f"修改头像失败：{avatar_err}"
        if not update_fields and not avatar_file:
            # 未传修改字段：返回当前资料
            return await self._query_user(
                target, is_self=is_self, group_id=group_id,
                describe=bool(look_avatar) and self.avatar_describe)

        # 执行修改
        # 注意：不同协议端支持的字段不同（如 LLOneBot 的 set_qq_profile 只支持
        # nickname / personal_note，传 sex 会被 schema 拒绝）。
        # 逐字段尝试，避免"只改性别失败导致全部失败"。
        results = []
        try:
            # 昵称：OB11 标准 set_qq_profile
            if "nickname" in update_fields:
                resp = await self.adapter.bot.send_action(
                    "set_qq_profile", {"nickname": update_fields["nickname"]}
                )
                if isinstance(resp, dict) and resp.get("status") == "ok":
                    results.append("昵称已更新")
                else:
                    err = (resp or {}).get("wording") or str(resp)
                    results.append(f"昵称修改失败：{err}")

            # 个性签名：优先 NapCat 专用 set_self_longnick（参数 longNick），
            # 回退 OB11 的 set_qq_profile(personal_note)。
            # 之前只用 set_qq_profile → NapCat 上签名实际改不动却报成功。
            if "personal_note" in update_fields:
                note = update_fields["personal_note"]
                ok = False
                err = ""
                try:
                    resp = await self.adapter.bot.send_action(
                        "set_self_longnick", {"longNick": note}
                    )
                    if isinstance(resp, dict) and resp.get("status") == "ok":
                        ok = True
                    else:
                        err = (resp or {}).get("wording") or str(resp)
                except Exception as e:
                    err = str(e).strip() or type(e).__name__
                if not ok:
                    try:
                        resp = await self.adapter.bot.send_action(
                            "set_qq_profile", {"personal_note": note}
                        )
                        if isinstance(resp, dict) and resp.get("status") == "ok":
                            ok = True
                    except Exception as e:
                        err = err or str(e).strip() or type(e).__name__
                results.append("个性签名已更新" if ok else f"个性签名修改失败：{err}")

            # 性别：单独尝试（部分协议端不支持）
            if "sex" in update_fields:
                try:
                    resp = await self.adapter.bot.send_action(
                        "set_qq_profile", {"sex": update_fields["sex"]}
                    )
                    if isinstance(resp, dict) and resp.get("status") == "ok":
                        results.append("性别已更新")
                    else:
                        results.append("性别修改失败（当前协议端可能不支持）")
                except Exception as e:
                    results.append(f"性别修改失败（当前协议端不支持：{type(e).__name__}）")

            # 头像：单独 action set_qq_avatar（参数 file，支持路径/URL/base64）
            if avatar_file:
                try:
                    resp = await self.adapter.bot.send_action(
                        "set_qq_avatar", {"file": avatar_file}
                    )
                    if isinstance(resp, dict) and resp.get("status") == "ok":
                        results.append("头像已更新")
                    else:
                        err = (resp or {}).get("wording") if isinstance(resp, dict) else resp
                        results.append(f"头像修改失败（{err or '当前协议端可能不支持'}）")
                except Exception as e:
                    # NapCat 的 set_qq_avatar 失败时是 **throw**（不是返回 failed），
                    # 且 message 里有具体原因（文件不是图片 / 无法获取图片 / api无返回…）
                    reason = str(e).strip() or type(e).__name__
                    results.append(f"头像修改失败：{reason}")

            if not results:
                return await self._query_user(
                    target, is_self=is_self, group_id=group_id,
                    describe=bool(look_avatar) and self.avatar_describe)
            # 返回修改结果 + 最新资料
            summary = "；".join(results)
            profile = await self._query_user(
                target, is_self=is_self, group_id=group_id,
                describe=bool(look_avatar) and self.avatar_describe)
            return f"修改完成（{summary}）。\n{profile}"
        except Exception as e:
            logger.debug(f"[QQ增强2] set_qq_profile 失败: {e}")
            return f"修改失败：{e}"

    async def _query_user(self, qq: str, *, is_self: bool = False,
                          group_id: str = "", describe: bool = True) -> str:
        """查询用户资料（含头像读图）。

        字段来源（**实测 NapCat 与文档差异**）：
          · 昵称 / 性别      ← get_stranger_info 的 nickname / sex
          · 个性签名         ← get_stranger_info 的 **long_nick**（NapCat 文档字段名），
                              兼容 longNick / longnick / sign / personal_note
                              ★ 只读 sign 会永远拿不到 → 实机表现"（未提供）"
          · 头像             ← get_stranger_info **不返回任何头像字段**（NapCat 实测），
                              回退 ① get_qq_avatar（LLOneBot）
                                   ② 腾讯头像 CDN（q1.qlogo.cn，按 QQ 号直出，永久可用）
          · 自己             ← get_login_info（永不超时）兜底昵称，再叠加 get_stranger_info

        Args:
            qq: QQ 号。
            is_self: 是否查询自己（决定是否走 get_login_info 兜底）。
            group_id: 当前群号（群聊里查询时补一行「群名片」，**不替代昵称**）。
            describe: 是否用 VLM 识别头像（False → 只输出 URL）。

        Returns:
            资料文本。
        """
        qq = str(qq or "").strip()
        if not qq:
            return ("查询失败：QQ 号为空。（如果这是「查询自己」的调用，"
                    "说明适配器配置的 bot_pid 为空，请在适配器设置里填写 Bot QQ。）")

        data: dict = {}
        timed_out = False
        err_note = ""

        # 查自己：**先**拿 get_login_info（最快最稳，永不超时），
        # 这样即使后面的 get_stranger_info 卡住，也已经有昵称可展示。
        login: dict = {}
        if is_self:
            try:
                login = await self._api("get_login_info", {}, timeout=4.0)
            except Exception as e:
                logger.debug(f"[QQ增强2] get_login_info 失败: {e}")

        try:
            # no_cache 是 NapCat 文档里的 required 字段，之前没传
            data = await self._api(
                "get_stranger_info",
                {"user_id": qq, "no_cache": False},
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            timed_out = True
        except Exception as e:
            err_note = f"{type(e).__name__}: {e}"
            logger.debug(f"[QQ增强2] get_stranger_info 失败: {qq}: {e}")

        nickname = (self._pick(data, "nickname", "nick")
                    or self._pick(login, "nickname", "nick"))
        personal_note = self._pick(
            data, "long_nick", "longNick", "longnick",
            "sign", "personal_note", "personalNote",
        )
        sex_str = self._normalize_sex(data.get("sex"))

        # 头像：API 字段 → get_qq_avatar → 腾讯 CDN（按 QQ 号直出）
        head_url = self._pick(
            data, "headUrl", "head_url", "avatar", "avatarUrl",
            "avatar_url", "head_img",
        )
        if not head_url:
            # 腾讯头像 CDN **优先**：按 QQ 号直出、零 API 调用、零超时风险。
            # get_qq_avatar 是 LLOneBot 扩展接口，NapCat 不支持（要等 2.5s 超时），
            # 只在 QQ 号不是纯数字（CDN 拼不出来）时才试。
            head_url = self._fallback_avatar_url(qq)
        if not head_url:
            head_url = await self._fetch_avatar_url(qq)

        # 群名片：群聊里额外补一行（与 QQ 昵称**并存**，不替代）。
        # 群里大家通常按群名片称呼，昵称往往完全对不上人。
        # 注意顺序：先取群名片，再做"完全没信息"判定 ——
        # 否则 get_stranger_info 失败但群成员信息可用时会误报查询失败。
        card_line = ""
        member: dict = {}
        if group_id:
            try:
                member = await self._api(
                    "get_group_member_info",
                    {"group_id": group_id, "user_id": qq, "no_cache": False},
                    timeout=4.0,
                )
            except Exception as e:
                logger.debug(f"[QQ增强2] 群名片查询失败: {group_id}/{qq}: {e}")
                member = {}
            if member:
                card = str(member.get("card") or "").strip()
                role_map = {"owner": "群主", "admin": "管理员", "member": "成员"}
                role = role_map.get(str(member.get("role") or ""), "")
                card_line = f"  群名片：{card or '（无）'}\n"
                if role:
                    card_line += f"  群角色：{role}\n"
                if not nickname:
                    nickname = str(member.get("nickname") or "").strip()

        # 完全拿不到任何信息 → 明确报错（区分超时/无权限）
        if not data and not login and not nickname and not member:
            if timed_out:
                return (
                    f"查询失败：协议端 6 秒内没有响应 get_stranger_info（user_id={qq}）。\n"
                    "可能原因：① 该协议端未实现此接口 ② WebSocket 断线 ③ 协议端正在重连。"
                )
            return (f"查询失败：用户 {qq} 不存在或无权限。"
                    + (f"（{err_note}）" if err_note else ""))

        result = f"用户 {qq} 的资料：\n"
        result += f"  昵称：{nickname or '（未提供）'}\n"
        result += card_line
        if personal_note:
            result += f"  个性签名：{personal_note}\n"
        elif timed_out:
            result += "  个性签名：（协议端未响应，暂无法获取）\n"
        else:
            result += "  个性签名：（该协议端未提供）\n"
        result += f"  性别：{sex_str}\n"

        # 头像：读图（VLM 描述），而非只给 URL
        if head_url:
            avatar_desc = await self._describe_avatar(head_url) if describe else ""
            if avatar_desc:
                result += f"  头像：{avatar_desc}\n"
            else:
                result += f"  头像：{head_url}\n"
        else:
            result += "  头像：（无法获取）\n"

        if timed_out:
            result += "  （注：get_stranger_info 超时，以上为可获取到的部分信息）\n"
        return result

    @staticmethod
    def _event_group_id(event) -> str:
        """取当前会话的群号（私聊返回空串）。

        Args:
            event: KiraMessageBatchEvent。

        Returns:
            群号字符串；私聊/取不到时为空。
        """
        session = getattr(event, "session", None)
        if session is not None and getattr(session, "session_type", "") == "gm":
            gid = str(getattr(session, "session_id", "") or "")
            if gid:
                return gid
        for msg in getattr(event, "messages", []) or []:
            grp = getattr(msg, "group", None)
            if grp is not None:
                gid = str(getattr(grp, "group_id", "") or "")
                if gid:
                    return gid
        return ""

    @staticmethod
    def _latest_image_file(event) -> str:
        """取当前消息批次里最新一张图片的 file（路径 / URL / base64）。

        框架的 formatter 会给出 `[Image <描述>, file_path: data/xxx.jpg]`，
        LLM 通常能拿到路径；`recent` 只是让"用我刚发的图"更省事、更不容易抄错。

        Args:
            event: KiraMessageBatchEvent。

        Returns:
            图片 file 字符串；没有图片时返回空串。
        """
        for msg in reversed(list(getattr(event, "messages", []) or [])):
            chain = list(getattr(msg, "chain", []) or [])
            for ele in reversed(chain):
                if not isinstance(ele, Image):
                    continue
                f = getattr(ele, "file", "") or getattr(ele, "image", "")
                if f:
                    return str(f)
        return ""

    @staticmethod
    def _fallback_avatar_url(qq: str) -> str:
        """用腾讯头像 CDN 按 QQ 号直出头像 URL。

        协议端（NapCat）的 get_stranger_info **不返回头像字段**，
        但 QQ 头像有公开 CDN：`https://q1.qlogo.cn/g?b=qq&nk={QQ}&s=640`
        （NapCat 官方 issue #1378 也推荐这个方式，无需任何 API）。

        Args:
            qq: QQ 号。

        Returns:
            头像 URL；QQ 号非纯数字时返回空串。
        """
        s = str(qq or "").strip()
        if not s.isdigit():
            return ""
        return f"https://q1.qlogo.cn/g?b=qq&nk={s}&s=640"


    async def _describe_avatar(self, head_url: str) -> str:
        """下载头像并用 VLM 生成描述（真正"看"头像）。

        提示词对齐框架 message_formatter.py 的处理方式：
        - 默认（avatar_desc_prompt 为空）→ prompt=None → desc_img 内部使用框架
          默认提示词（跟随 WebUI 中 image_recognition.desc_prompt 的设置）
        - 用户在本插件 schema 里填了 avatar_desc_prompt → 使用该自定义提示词

        Args:
            head_url: 头像 URL。

        Returns:
            头像描述文本；失败时返回空串。
        """
        # 缓存命中直接返回（省一次 VLM 调用）
        cached = _AVATAR_DESC_CACHE.get(head_url)
        if cached and time.time() - cached[1] < _AVATAR_DESC_TTL:
            _AVATAR_DESC_CACHE.move_to_end(head_url)
            return cached[0]

        try:
            from core.utils.common_utils import desc_img

            img = Image(head_url)
            vlm = None
            try:
                vlm = self.ctx.provider_mgr.get_default_vlm()
            except Exception:
                vlm = None
            if vlm is None:
                return ""

            # 提示词对齐框架：留空则跟随框架 WebUI 设置（prompt=None → 框架默认提示词）
            custom_prompt = (self.avatar_desc_prompt or "").strip() or None
            lang = self._get_locale_lang()

            # 加超时：VLM 可能很慢（框架对普通工具无超时保护），
            # 超时则降级为只显示 URL，避免卡住整个工具链。
            try:
                desc = await asyncio.wait_for(
                    desc_img(client=vlm, image=img, prompt=custom_prompt, lang=lang),
                    timeout=self.avatar_desc_timeout,
                )
            except asyncio.TimeoutError:
                logger.debug(f"[QQ增强2] 头像描述超时（>{self.avatar_desc_timeout}s）: {head_url}")
                return ""
            # 限制描述长度（避免 VLM 长篇大论刷爆工具输出）
            desc = (desc or "").strip()
            if len(desc) > 300:
                desc = desc[:300] + "…"
            if desc:
                _AVATAR_DESC_CACHE[head_url] = (desc, time.time())
                _AVATAR_DESC_CACHE.move_to_end(head_url)
                while len(_AVATAR_DESC_CACHE) > _AVATAR_DESC_MAX:
                    _AVATAR_DESC_CACHE.popitem(last=False)
            return desc
        except Exception as e:
            logger.debug(f"[QQ增强2] 头像描述失败: {head_url}: {e}")
            return ""

    async def _fetch_avatar_url(self, qq: str) -> str:
        """兜底获取头像 URL（get_qq_avatar 仅 LLOneBot 支持）。

        Args:
            qq: QQ 号。

        Returns:
            头像 URL；不支持或无返回时为空串。
        """
        try:
            # 2.5s 超时：get_qq_avatar 是 LLOneBot 扩展接口，NapCat 不支持。
            # 不支持时协议端可能不回包，用框架默认 10s 会把工具拖死。
            resp = await asyncio.wait_for(
                self.adapter.bot.send_action("get_qq_avatar", {"user_id": qq}),
                timeout=2.5,
            )
            if isinstance(resp, dict) and resp.get("status") == "ok":
                data = resp.get("data")
                if isinstance(data, dict):
                    return str(data.get("url") or "")
                if isinstance(data, str):
                    return data
        except Exception:
            pass
        return ""

    @staticmethod
    def _prepare_file(value) -> tuple[str, str]:
        """把 file 参数转成协议端可用形式，返回 (uri, 错误说明)。

        本地路径会读成 `base64://`（≤2MB），这样即使 NapCat 跑在 Docker /
        另一台机器也能用；文件不存在时返回明确错误，而不是让协议端报
        "文件可能不存在"。

        Args:
            value: 原始 file 参数。

        Returns:
            (uri, error)。error 为空表示可用。
        """
        try:
            from qq_core.fmt import file_to_protocol_uri
            return file_to_protocol_uri(value)
        except ImportError:
            pass
        try:
            import importlib
            parts = __name__.split(".")
            if len(parts) >= 2:
                parent_pkg = ".".join(parts[:-1])
                mod = importlib.import_module(f"{parent_pkg}.qq_core.fmt")
                return mod.file_to_protocol_uri(value)
        except Exception:
            pass
        # 兜底：只做前缀归一化（不读文件）
        return QQProfileTool._normalize_file_uri(value), ""

    @staticmethod
    def _normalize_file_uri(value) -> str:
        """归一化 file 参数（协议端只认特定前缀）。

        协议端支持的格式：
          · base64://<data>                OneBot base64
          · data:image/png;base64,<data>   Data URL
          · http:// / https://             远程 URL
          · file:///path                   文件 URL
          · /abs/path 或相对路径            本地文件

        **纯 base64 字符串（无前缀）会被当作文件名 → 失败**，
        这里自动识别并补 `base64://` 前缀。

        Args:
            value: 用户/LLM 传入的 file 值。

        Returns:
            归一化后的字符串（空输入返回空串）。
        """
        # 优先用 qq_core.fmt 的实现（统一维护）
        try:
            from qq_core.fmt import normalize_file_uri
            return normalize_file_uri(value)
        except ImportError:
            pass
        # 框架内加载时（plugins.<id>.tools）走包内相对定位
        try:
            import importlib
            parts = __name__.split(".")
            if len(parts) >= 2:
                parent_pkg = ".".join(parts[:-1])  # plugins.<id>
                mod = importlib.import_module(f"{parent_pkg}.qq_core.fmt")
                return mod.normalize_file_uri(value)
        except Exception:
            pass
        # 最后兜底：就地实现（极简版）
        import re as _re
        s = str(value or "").strip()
        if not s:
            return ""
        for prefix in ("base64://", "data:", "http://", "https://", "file://"):
            if s.startswith(prefix):
                return s
        if len(s) >= 64 and _re.fullmatch(r"[A-Za-z0-9+/=\s]+", s):
            compact = _re.sub(r"\s+", "", s)
            if len(compact) >= 64:
                return f"base64://{compact}"
        return s

    @staticmethod
    def _normalize_sex(val) -> str:
        """归一化性别字段（兼容 int / 数字字符串 / 英文）。

        Args:
            val: 原始值（可能是 1 / "1" / "male" / None）。

        Returns:
            "男" | "女" | "未知"。
        """
        if val is None or val == "":
            return "未知"
        try:
            n = int(val)
            return {0: "未知", 1: "男", 2: "女"}.get(n, "未知")
        except (TypeError, ValueError):
            pass
        s = str(val).strip().lower()
        if s in ("male", "m", "男"):
            return "男"
        if s in ("female", "f", "女"):
            return "女"
        return "未知"

    def _get_locale_lang(self) -> str:
        """获取框架语言设置（兼容 config 为 dict 或 KiraConfig 对象两种情况）。"""
        cfg = getattr(self.ctx, "config", None)
        if cfg is None:
            return "zh"
        # KiraConfig 对象
        getter = getattr(cfg, "get_config", None)
        if callable(getter):
            try:
                return getter("locale.lang", "zh") or "zh"
            except Exception:
                pass
        # dict
        if isinstance(cfg, dict):
            try:
                val = cfg.get("locale", {})
                if isinstance(val, dict):
                    return val.get("lang") or "zh"
                # 扁平 key 形式
                return cfg.get("locale.lang") or "zh"
            except Exception:
                pass
        return "zh"