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

from core.logging_manager import get_logger
from core.chat.message_utils import KiraMessageBatchEvent
from core.chat.message_elements import Image
from core.utils.tool_utils import BaseTool
from core.plugin import PluginContext

logger = get_logger("qq_enhance2.qq_profile", "cyan")


class QQProfileTool(BaseTool):
    """QQ 用户资料工具（读+写合体）。"""

    name = "qq_profile"
    description = "查询或修改QQ用户资料。不传 user_id 时操作自己；传 user_id 时查询他人（仅查询，不可修改他人）。修改时只传需要修改的字段。查询时会查看头像内容。支持设置自己头像（avatar 传图片路径/URL/base64）。"
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "目标QQ号。不传=操作自己；传=查询他人资料（只读）。",
            },
            "nickname": {
                "type": "string",
                "description": "新昵称（仅自己可改）。不传=不修改。",
            },
            "personal_note": {
                "type": "string",
                "description": "新个性签名（仅自己可改）。不传=不修改。",
            },
            "sex": {
                "type": "integer",
                "description": "性别：0未知 1男 2女（仅自己可改，部分协议端不支持）。不传=不修改。",
            },
            "avatar": {
                "type": "string",
                "description": "新头像（仅自己可改）。传图片路径、URL 或 base64。不传=不修改。",
            },
        },
    }

    def __init__(self, ctx: PluginContext, adapter=None, self_id: str = "",
                 avatar_desc_prompt: str = "", avatar_desc_timeout: float = 20.0):
        super().__init__(ctx=ctx)
        self.adapter = adapter
        self.self_id = str(self_id) if self_id else ""
        self.avatar_desc_prompt = avatar_desc_prompt or ""
        self.avatar_desc_timeout = float(avatar_desc_timeout or 20.0)

    async def execute(
        self,
        event: KiraMessageBatchEvent,
        *args,
        user_id: str = "",
        nickname: str = None,
        personal_note: str = None,
        sex: int = None,
        avatar: str = "",
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
        target = str(user_id) if user_id else self.self_id
        is_self = (target == self.self_id) if self.self_id else False

        if not is_self:
            # 查询模式：只查询，忽略修改字段
            return await self._query_user(target)

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
            update_fields["sex"] = int(sex)

        # 头像走独立 action（set_qq_avatar），参数是 file
        avatar_file = self._normalize_file_uri(avatar)
        if not update_fields and not avatar_file:
            # 未传修改字段：返回当前资料
            return await self._query_user(target)

        # 执行修改
        # 注意：不同协议端支持的字段不同（如 LLOneBot 的 set_qq_profile 只支持
        # nickname / personal_note，传 sex 会被 schema 拒绝）。
        # 逐字段尝试，避免"只改性别失败导致全部失败"。
        results = []
        try:
            # 先提交协议端普遍支持的字段（昵称、签名）
            common = {k: v for k, v in update_fields.items() if k in ("nickname", "personal_note")}
            if common:
                resp = await self.adapter.bot.send_action("set_qq_profile", common)
                if not isinstance(resp, dict) or resp.get("status") != "ok":
                    err = (resp or {}).get("wording") or str(resp)
                    return f"修改失败：{err}"
                results.append("昵称/签名已更新")

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
                    results.append(f"头像修改失败（当前协议端可能不支持：{type(e).__name__}）")

            if not results:
                return await self._query_user(target)
            # 返回修改结果 + 最新资料
            summary = "；".join(results)
            profile = await self._query_user(target)
            return f"修改完成（{summary}）。\n{profile}"
        except Exception as e:
            logger.debug(f"[QQ增强2] set_qq_profile 失败: {e}")
            return f"修改失败：{e}"

    async def _query_user(self, qq: str) -> str:
        """查询用户资料（含头像读图）。

        Args:
            qq: QQ 号。

        Returns:
            资料文本。
        """
        try:
            resp = await self.adapter.bot.send_action("get_stranger_info", {"user_id": qq})
            data = (resp or {}).get("data") or {}
            if not data:
                return f"查询失败：用户 {qq} 不存在或无权限。"

            nickname = data.get("nickname") or ""
            personal_note = data.get("sign") or data.get("personal_note") or ""
            # 性别：不同协议端可能返回 int 或 str（"1"/"male"），统一归一化
            sex_map = {0: "未知", 1: "男", 2: "女"}
            sex_str = self._normalize_sex(data.get("sex"))
            # 头像 URL：优先取 get_stranger_info 的 headUrl（三家都支持），
            # 缺失时兜底试 get_qq_avatar（仅 LLOneBot 支持）
            head_url = data.get("headUrl") or data.get("avatar") or ""
            if not head_url:
                head_url = await self._fetch_avatar_url(qq)

            result = f"用户 {qq} 的资料：\n"
            result += f"  昵称：{nickname or '（未提供）'}\n"
            result += f"  个性签名：{personal_note or '（未提供）'}\n"
            result += f"  性别：{sex_str}\n"

            # 头像：读图（VLM 描述），而非只给 URL
            if head_url:
                avatar_desc = await self._describe_avatar(head_url)
                if avatar_desc:
                    result += f"  头像：{avatar_desc}\n"
                else:
                    result += f"  头像：{head_url}\n"
            else:
                result += f"  头像：（该协议端未提供）\n"
            return result
        except Exception as e:
            logger.debug(f"[QQ增强2] get_stranger_info 失败: {qq}: {e}")
            return f"查询失败：{e}"

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
            resp = await self.adapter.bot.send_action("get_qq_avatar", {"user_id": qq})
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