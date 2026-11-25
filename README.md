# QQ增强2（kira-ai-plugin-qq-enhance2）

> 增强数字生命在 QQ 平台的能力：他人事件感知、自己相关事件唤醒、资料卡工具、三家协议端兼容。

## 功能

### 感知层（他人事件）
> **只记「其他用户之间」的互动**：涉及 bot 自己的事件一律不进群动态
> （别人戳 bot、bot 禁言别人、自己的名片/头衔变动……），避免"自己对自己"的噪音。

- **戳一戳**：收集群内他人之间的戳一戳事件，合并入环境摘要。
- **贴表情**：收集群内他人给他人消息贴表情的事件，在原消息下追加标注（`[贴表情｜用户X 贴了🔥]`），合并入环境摘要；可选唤醒（默认关，冷却 20 秒可配）。
- **禁言/解禁**：收集群内成员被禁言/解除禁言事件，合并入环境摘要（bot 自己被禁言归 QQ增强1）。
- **撤回**：收集消息撤回事件，在原文下补充撤回标注（区分操作者身份）。
- **退群/被踢**：**不进摘要**，改成 notice 唤醒（`[System 用户X(昵称)退群了]`，与 QQ增强1 的入群感知同款）。

### 唤醒层（自己相关事件）
> 唤醒事件按**框架 notice 形态**构造（`is_notice=True` + sender=真实触发者），
> LLM 看到的是与戳一戳/禁言/入群完全一致的
> `Notice [group_id: ..., user_id: ...] | [System ...]`。
>
> **他人消息被贴表情时按"消息在不在缓冲区"分两种写法**：
> - 在缓冲区（会同批送给 LLM，消息自带 `↑该条消息被贴了…`）→ 唤醒正文**不复述事实**，
>   只留互动提示，避免同一件事在一批里说两遍；
> - 不在缓冲区（消息已发出/被丢弃）→ 发完整通知（`[System …给…的消息贴了…]`），
>   因为这时通知是唯一的信息来源。

- **被贴表情**：自己的消息被贴表情时立即唤醒（trigger 进批次合并）。
- **资料卡被赞**：资料卡被点赞时唤醒（同用户 10 秒内合并防抖，可配）。
- **头衔授予**：自己被封头衔时立即唤醒。
- **被移出群**：自己被移出群聊时立即唤醒。

### 工具层
- **`qq_profile`**：查询或修改 QQ 用户资料（昵称/个性签名/性别/头像）。查询时可选是否识别头像（`look_avatar`，默认 true）。不传 user_id=操作自己；传 user_id=查询他人（只读）。
  - 查询时自动下载头像并用 VLM 查看头像内容（不是只给 URL）；同一头像在缓存期内（默认 60 秒，可配）不重复调 VLM。
  - **群聊里查询会额外多一行「群名片」**（与昵称并存，不替代昵称）和「群角色」。
  - 修改：昵称走 `set_qq_profile`；签名优先 NapCat 专用 `set_self_longnick`（失败回退 `set_qq_profile.personal_note`）；头像 `avatar` 传路径/URL/base64，或传 `recent` 用当前对话里最新的一张图。
- **`qq_group_profile`**：查询或修改群成员资料（群名片/头衔/角色）。不传 user_id=操作自己；传 user_id=查询他人（只读）。**支持设置群头像**（`group_portrait`，需管理员权限，SnowLuma 不支持）。

### 协议端兼容
- NapCat / LLOneBot / SnowLuma 三家协议端。
- 贴表情、资料卡点赞、戳一戳、禁言、进退群、撤回、名片/头衔/管理员/精华/文件 等事件全覆盖。

## 配置

详见 `schema.json`（28 个配置键）。完整列表：

### 感知层（他人事件）

| 配置 | 默认 | 说明 |
|------|------|------|
| `collect_poke` | true | 感知他人戳一戳 |
| `collect_emoji_like` | true | 感知他人贴表情（原文标注 + 唤醒判定） |
| `collect_emoji_like_env` | false | 贴表情是否进群动态摘要（默认关，已由两路推送覆盖） |
| `collect_group_ban` | true | 感知他人被禁言 |
| `collect_self_ban` | false | 记录**自己禁言别人**的操作（给自动禁言系统复盘用，一般不用开） |
| `collect_self_unban` | false | 记录**自己解除别人禁言**的操作（同上，独立开关） |
| `collect_recall` | true | 感知撤回 |
| `collect_member_decrease` | true | 感知退群/被踢（感知到就发 notice，不进摘要） |
| `collect_title` | false | 感知头衔授予 |
| `collect_card_admin` | false | 感知名片/管理员变更 |

### 唤醒层（自己相关事件）

| 配置 | 默认 | 说明 |
|------|------|------|
| `wake_self_emoji` | true | 自己消息被贴表情→唤醒 |
| `wake_self_emoji_cooldown` | 10 | 自己被贴表情的唤醒冷却（秒）；**0=每条都唤醒**；与他人贴表情冷却相互独立 |
| `wake_emoji_merge_seconds` | 5 | 同一条消息在 N 秒内的多个贴表情合并成一条唤醒（自己/他人共用）；0=不合并 |
| `wake_others_emoji` | false | 他人消息被贴表情→可唤醒（默认关） |
| `wake_others_emoji_cooldown` | 20 | 他人消息被贴表情唤醒冷却（秒） |
| `wake_profile_like` | true | 资料卡被赞→唤醒 |
| `profile_like_dm_only` | false | 资料卡点赞唤醒发到哪：**关（默认）=最近互动会话**（群/私聊按时间比）；开=只发与该用户的私聊（bot 非好友时可能发送失败） |
| `wake_title` | true | 头衔授予→唤醒 |
| `wake_kicked` | true | 被移出群→唤醒 |
| `wake_member_decrease` | true | 其他人退群/被踢→唤醒（notice，与入群同款） |
| `wake_hint` | 可随手用工具互动（回贴表情、回赞等）；别刷屏，也可以不回应。 | 被贴表情/资料卡被赞时附在唤醒里的引导；留空=不加 |
| `profile_like_merge_window_seconds` | 10 | 点赞合并窗口（秒） |
| `wake_quiet_hours` | "" | 静默时段（如 23:00-08:00），空=不静默 |
| `wake_max_per_hour` | 60 | 每会话每小时最大唤醒次数；-1=无限制 |

### 环境摘要

**只收「其他用户之间」的事件**：凡是涉及 bot 自己的（别人戳 bot、bot 禁言别人、
自己的群名片/管理员/头衔变动、别人撤回 bot 的消息……）都不进摘要 ——
这些事要么由专门通道处理（唤醒 / 框架原生 `[Poke ...你]` / QQ增强1 的禁言提醒），
要么 bot 本来就知道，写进摘要只会污染上下文、让模型误以为"有人在对我做某事"。

**时间与衰减**：
- 每条事件带绝对时间，块头是 `[群动态·截至 HH:MM]`；
- **分类型新鲜期**：戳一戳/贴表情 5 分钟，名片/管理员/头衔/撤回 15 分钟，过期自动消失；
- **禁言是状态**：进行中显示"剩约 N 分钟"，到期后只再出现**一轮**（"已于 HH:MM 到期"）然后消失；
- 退群/被踢**不进摘要**（走 notice 唤醒）。

| 配置 | 默认 | 说明 |
|------|------|------|
| `perceive_window_seconds` | 900 | 最多回看多久（安全阀）；实际显示由下面两档新鲜期决定 |
| `perceive_max_events` | 20 | 每会话最多保留事件数（LRU） |
| `perceive_max_per_type` | 6 | 每类型最多渲染条数 |
| `env_max_chars` | 800 | 环境摘要字符上限（超限从最旧事件整条删除，不截断） |
| `env_show_time` | true | 每条事件带绝对时间（当天 HH:MM / 昨天 HH:MM / 跨两天 MM-DD HH:MM） |
| `env_fresh_seconds` | 300 | 戳一戳/贴表情的新鲜期（秒），过期自动消失 |
| `env_slow_seconds` | 900 | 名片/管理员/头衔/撤回的新鲜期（秒） |
| `env_inject_min_interval` | 0 | 摘要注入最小间隔（秒），0=每轮注入增量 |
| `env_ttl_hours` | 24 | 事件落盘保留时长（小时） |

### 撤回 / 贴表情改写

| 配置 | 默认 | 说明 |
|------|------|------|
| `recall_rewrite` | true | 撤回消息原文下补充标注 |
| `recall_show_content` | true | 撤回时显示原文（关=只显示"某条消息被撤回"） |
| `recall_in_env` | false | 撤回是否进群动态摘要（默认关：原文已标注，摘要重复是无效信息） |

### 工具

| 配置 | 默认 | 说明 |
|------|------|------|
| `tools_profile` | true | 启用 qq_profile / qq_group_profile |
| `profile_detail` | true | 资料是否显示详细信息（年龄/生日/地区/群等级等）；关闭只显示基础字段 |
| `avatar_describe` | true | 查询资料时用视觉模型「看」头像（关=只回头像链接；开启时 bot 也可用 `look_avatar=false` 单次跳过） |
| `avatar_desc_prompt` | "" | 头像描述提示词，留空=跟随框架 WebUI 图片识别提示词设置 |
| `avatar_desc_timeout` | 20 | 头像描述超时（秒），超时降级为只显示 URL |
| `avatar_desc_cache_seconds` | 60 | 头像描述缓存（秒）；0=不缓存，每次重新识别 |

### 调试

| 配置 | 默认 | 说明 |
|------|------|------|
| `poke_action_text` | false | 戳一戳用协议端真实文案（默认关=统一显示"戳了戳"，与框架归一解析一致） |
| `resolve_nickname` | true | 补全事件昵称（框架只带发起人昵称，target 需额外查询） |
| `resolve_cache_seconds` | 10 | 昵称/群名片查询缓存（秒）；**0=不缓存**，每次都实时查；并发合并始终生效 |
| `debug_log` | false | 调试日志：打印事件收集与群动态注入（框架默认不打印群动态） |

## 兼容性

### 框架版本

`core_version: ">=3.1.2"`（无上界）。已实测通过的版本：
- **v3.1.2**（正式版）
- **v3.2.0-beta.1**（beta，框架用 `prereleases=True` 校验，beta/rc 也能装）

两个版本上各跑了一遍全量验证（14 个专项 + 10 轮回归 + 深度审计 + 框架 pytest）。

### 插件兼容

已核实以下插件零冲突：
- **QQ增强1**（互补不重叠：我们做退群/他人事件，它做进群/自己禁言）
- **S / Z**（互斥，trigger 走标准路径，与 buffer/批次模型同构）
  - 唤醒事件按**框架 notice 形态**构造（`is_notice=True`，sender = 真实触发者）
    → 与戳一戳/禁言/入群写法完全一致，S/Z 判为系统事件，不污染统计
  - 唤醒文本措辞避开 S 的禁言解析正则（`禁言了你N秒`）
- **默认聊天 / JEV / anti-harass / multi-decision / midflight / group_manager / history_plugin** 等 10+ 插件

## 安装

1. 下载本仓库代码。
2. 将 `manifest.json` / `schema.json` / `main.py` / `qq_core/` / `tools/` / `icon.png` 复制到 KiraAI 的 `data/plugins/kira-ai-plugin-qq-enhance2/` 目录。
3. 在 WebUI 插件管理中启用。

### 注意事项

- **修改配置后需重载插件**：框架不会热更新插件配置（`cfg` 仅在实例化时传入一次）。
  在 WebUI 中改完配置后，请禁用再启用本插件（或重启框架）。
- **多 QQ 号部署**：支持多适配器，每个号的 notice 会用自己账号的 `self_id` 判断，
  工具也绑定到当前会话所属账号（不会用错号发 API）。
- **群动态在控制台看不到是正常的**：环境摘要以 `persist=False` 注入 prompt
  （不进历史、不破坏缓存），而框架只打印 `persist=True` 的内容，
  所以 cmd 日志里不会出现 `[群动态·...]`。
  想确认功能是否生效，把 `debug_log` 打开，会打印：
  ```
  [QQ增强2] 收集事件 napcat:gm:123: poke actor=1001 target=2002
  [QQ增强2] 注入群动态 napcat:gm:123: [群动态·刚刚] 用户1001(张三) 戳了戳 用户2002(李四)
  ```
- **数据存储**：`data/plugin_data/kira-ai-plugin-qq-enhance2/` 下按会话存 JSON
  （每会话最多 20 条事件，30 秒落盘一次，24 小时 TTL）。

## 更新日志

<details>
<summary><b>v1.2.4</b>（2026-11-25）— 头像下载重试 + 占位值过滤</summary>

### 🐞 头像恒报「图片下载失败（ConnectError: ）」
- **原因**：`httpx.ConnectError` 的 `str(e)` 经常为空，日志里只剩一个空括号；而且下载没有重试。
- **修复**：
  - 异常文本改用 `repr(e)` + 异常链（`← ConnectError: ...`），不再出现空原因；
  - 网络类失败自动重试：原地址再试一次，仍失败则换 QQ 头像 CDN 备用节点（q1~q4）；
  - 4xx（头像不存在）不重试，直接给出原因，避免无意义等待。

### 🧹 占位值不再当资料显示
`-`、`—`、`无`、`未知`、`N/A`、`null`、`0` 这类占位值，在
电话/邮箱/学校/QID/备注/地区/个性签名/群简介 里一律视为空，不再出现 `电话：-`。

### ✅ 验证
pytest **204**（新增 `test_regressions19.py`：空异常文本、失败重试 3 次并换节点、
404 不重试、占位值过滤）；双版本专项全绿。

</details>

<details>
<summary><b>v1.2.3</b>（2026-11-25）— 资料卡字段补全（年龄/生日/等级/地区等）</summary>

### ✨ qq_profile 新增字段
以前只显示昵称/群名片/群角色/签名/性别/头像。现在把协议端能给、且确实返回了的字段都带上：

```
用户 769690776 的资料：
  昵称：周武
  备注：老周
  性别：男
  年龄：24
  生日：2001-03-15
  QID：zhouwu
  QQ等级：48
  注册时间：2015-08-11（Q龄约 11 年）
  地区：中国 江苏 南京
  学校：南京大学
  邮箱：a@b.c
  电话：13800000000
  会员：超级会员 年费 等级8
  累计登录：1200 天
  个性签名：漏舟载酒
  头像：https://q1.qlogo.cn/g?b=qq&nk=769690776&s=640
  头像内容：蓝发男生
```

规则：**有就显示、没有就跳过**，不输出占位，不浪费 token。

### 🎛 新增开关 `profile_detail`（默认开）
- 开：显示上面的完整字段（群资料里还会显示 群备注/建群时间/群等级/全员禁言/群简介）
- 关：只显示原来那几项（昵称/群名片/签名/性别/头像；群：群名/人数/头像）

### 🔌 三家协议端字段兼容
| 字段 | NapCat | SnowLuma | LLOneBot |
|---|---|---|---|
| 年龄/性别/签名 | ✓ | ✓ | ✓ |
| 生日 | 部分 | ✓ birthday_* | ✓ birthday_* |
| QID | ✓ | ✓ | ✓ |
| QQ 等级 | qqLevel | qq_level | level |
| 注册时间 | reg_time | reg_time | reg_time |
| 地区 | country/province/city | 同左 | country/city |
| 学校 | — | college | — |
| 邮箱/电话 | 部分透传 | ✓ | — |
| 会员 | is_vip/is_years_vip/vip_level | is_vip/is_qq_vip/vip_level | is_svip/is_vip/vip_level |
| 备注 | ✓ | ✓ | ✓ |
| 登录天数 | 失效(0) | — | login_days |

### ✨ qq_group_profile 字段补全
- 群成员：性别、年龄、群等级、入群时间、最后发言
- 群资料：群备注、建群时间、群等级、全员禁言、群简介（NapCat/LLOneBot 会补拉 `get_group_info_ex`，SnowLuma 的 `get_group_info` 已自带）

### ✅ 验证
pytest **194**；三家协议端的字段形态各有专门用例；双版本专项全绿。

</details>

<details>
<summary><b>v1.2.2</b>（2026-11-25）— 头像识别成功时也返回 URL</summary>

### 🔧 输出格式对齐
以前 `qq_profile` 的头像是二选一：识别成功只给描述、失败才给 URL。
现在**永远先给 URL**，识别成功再补一行描述（与群头像的输出格式一致）：

```
  头像：https://q1.qlogo.cn/g?b=qq&nk=769690776&s=640
  头像内容：一只蓝猫
```

- 关闭识别（`look_avatar=false` 或配置关）：只有 URL 一行
- 识别成功：URL + 头像内容
- 识别失败：URL + `头像内容：（未识别：原因）`

方便 bot 在需要时直接引用或发送头像原图。

</details>

<details>
<summary><b>v1.2.1</b>（2026-11-25）— 修复群头像识别"总是超时"</summary>

### 🐞 现象
群头像（look_group_avatar）识别总是报超时，但视觉模型本身很快。

### 🔍 根因
框架的 `Image.to_base64()` 对 `url` 类型会**重新下载**一次图片。
旧实现直接把 `url` 元素交给 `desc_img`，这次下载发生在 **VLM 的 20 秒超时窗口内**
（前面那次下载是在窗口外的），图片稍大或网络稍慢就把窗口耗光 → 报"识别超时"，
模型一次都没跑。

### 🛠 修复
1. 统一**先下载**到本地临时文件，下载有独立 10 秒超时；
2. 交给 VLM 的是 **本地文件（path）元素**，`to_base64()` 直接读文件，不再联网；
3. 下载超时/失败有独立原因：`图片下载超时（>10秒…）`、`图片下载失败（…）`，
   不再伪装成"识别超时"。

效果：20 秒只覆盖模型推理；图片下载慢会在 10 秒时给出明确原因。

### ✅ 验证
pytest **187**（新增 `test_regressions17.py` 5 项：VLM 拿到 path 元素、下载超时/失败独立原因、
下载耗时不占 VLM 超时、VLM 自身超时仍生效）；双版本专项全绿。

</details>

<details>
<summary><b>v1.2.0</b>（2026-11-25）— 修复真实安装下头像识别失效（tools 导入桥）</summary>

### 🐞 头像只显示 URL / 报"框架缺少 avatar_view 模块"
- **根因**：`tools/*` 里写的是 `from qq_core.avatar_view import ...`。真实安装时插件被加载成
  `plugins.<目录>` 包，**`qq_core` 不是顶层包** ⇒ ImportError。v1.1.7/1.1.8 静默返回空描述
  （只显示链接），v1.1.9 加了失败原因后才暴露出来。
- **影响面**：`qq_profile` 看头像、`qq_group_profile(look_group_avatar)` 看群头像，
  以及头像设置用的 `file_to_protocol_uri` / `normalize_file_uri`（本地文件 → base64）。
- **修复**：新增 `tools/_bridge.py`，用 importlib 按当前包逐级解析，
  兼容「真实安装 `plugins.<目录>`」「包内加载」「测试顶层」三种布局；上述四处全部改走桥。
- **回归防护**：`tests/adapter_late_bind_check.py` 会把插件根目录从 `sys.path` 移除、
  清掉顶层 `qq_core`，再验证头像/群头像识别、本地文件→base64、**所有插件模块可导入**（16 项）。
- `audit_full` 新增规则：tools 下不得直接 `from qq_core...`（必须走 `_bridge`）。

### 🧹 配置项文案统一
- 所有配置项的 name / hint 改为**纯文本**（前端不渲染 Markdown，`**加粗**`、反引号、换行会原样显示）
- hint 统一成一句话说清「做什么 + 默认值/关闭后什么效果」，非技术用户也能看懂

### ✅ 验证
pytest **182**；KiraAI **v3.3.0 / v3.1.2** 双版本
self_scope 46 / config 25 / deep 30 / scenario 27 / safety 35 / edge 23 / robustness 34 /
audit_full 14 / 前后端 10 / avatar 52 / late-bind 16 全绿。

</details>

<details>
<summary><b>v1.1.9</b>（2026-11-25）— 适配器实时绑定 / 工具不再消失 / 头像失败可见</summary>

### 🐞 工具列表里看不到 qq_profile（用户实机）
- **根因**：`initialize()` 在"启动时没发现适配器"时直接 `return` → `store/locator` 全空 +
  `inject_tools` 第一行 `if not self.adapter: return` ⇒ 工具永远不注册。
  适配器若是稍后才启用/新建（WebUI 里刚开），插件就一直"半死"。
- **修复**：不再早退（只告警）；工具按**每个请求的事件**实时解析适配器（晚绑定）；
  `LLMRequest.tool_set` 为 None 时补建，避免 `.add()` 抛错导致工具静默消失。

### 🐞 同一台机器连接正常，工具报 "NapCat WebSocket 未连接"
- **根因**：适配器在 WebUI 重启/改配置时会被 `stop_adapter()` 移除并**重建实例**，
  插件缓存的是已关闭的旧实例（`bot.websocket is None`）。
- **修复**：`qq_core/adapter_bind.py` 实时向 `adapter_mgr.get_adapter(name)` 取实例并刷新缓存；
  取不到才回退缓存 / 默认实例。
- 顺带把错误文案改准：连接类错误不再谎称"用户不存在或无权限"，而是提示"协议端连接不可用，
  请检查/重启该适配器"。

### 🐞 头像只有 URL、没有 VLM 描述，且看不出原因
- **根因**：`get_default_vlm()` 抛错被吞成 `vlm=None` → 直接返回空串，日志只有 debug。
- **修复**：`describe_avatar_ex()` 返回 `(描述, 失败原因)`；失败原因写到 **WARNING** 日志，
  并显示在工具输出里：
  - `头像：https://…（未识别：未配置默认视觉模型（models.default_vlm 为空））`
  - `（未识别：识别超时（>20s））`、`（未识别：识别失败（RuntimeError: …））`
- 群头像 `look_group_avatar` 同样显示原因。
- `avatar_desc_cache_seconds=0` 语义不变（完全不缓存）。

### ✅ 验证
pytest **182**（新增 `test_regressions16.py` 10 项：实时优先/缓存回退/自身 id 回退/
tool_set 防御/头像原因三态/连接错误文案）；KiraAI **v3.3.0** 与 **v3.1.2** 双版本
self_scope 46 / config 25 / deep 30 / scenario 27 / safety 35 / edge 23 / robustness 34 /
audit_full 14 / 前后端 10 / avatar 52 全绿。

</details>

<details>
<summary><b>v1.1.8</b>（2026-11-18）— 可选：记录自己的禁言/解禁操作</summary>

### ✨ 新增两个独立开关（默认都关）
| 配置 | 作用 |
|---|---|
| `collect_self_ban` | bot **禁言别人**的操作入动态 |
| `collect_self_unban` | bot **解除别人禁言**的操作入动态（与上一个相互独立） |

开启后的渲染：

```
[群动态·截至 11:42]
· 11:40:12 你禁言了 用户1005(赵六) 600秒（剩约9分钟）      ← collect_self_ban
· 11:41:03 你解除了 用户1005(赵六) 的禁言                  ← collect_self_unban
```

行为细节：
- **只开禁言、不开解禁**：禁言条目在解除后显示「（已解除）」，但不会多出一行"你解除了…"
  （解禁事件仍会被收集，专用于配对状态，不影响其它显示）
- **只开解禁、不开禁言**：只显示"你解除了 …"这一行
- 两个都关（默认）：与旧版完全一致，自己的操作一概不记录

用途：搭载**自动禁言系统**的 bot 需要复盘"我刚才处置了谁、还有多久到期"。

</details>

<details>
<summary><b>v1.1.7</b>（2026-11-18）— 私聊戳一戳归属 / 私聊撤回 / 群头像 / 头像缓存接框架</summary>

### 🐞 修复：私聊里 bot 戳人 → 被记成"用户戳自己"（实机 10:20）
SnowLuma 的私聊 poke notice 字段语义和 NapCat 不同：

| 字段 | SnowLuma 私聊 | NapCat 私聊 | 群聊（三家一致） |
|---|---|---|---|
| `user_id` | **会话对端**（永远是好友） | 发起者 | 发起者 |
| `sender_id` | **真正的发起者** | 无 | 无 |
| `target_id` | 被戳者 | 被戳者 | 被戳者 |

旧代码只看 `user_id` ⇒ bot 主动戳周武时记成"周武 戳了戳 周武"，还进了私聊动态。
现在私聊优先取 `sender_id`，`actor == self_id` 一律丢弃；群聊仍用 `user_id`（三家语义一致）。

### 🐞 修复：私聊撤回张冠李戴
`friend_recall` 的 `user_id` 同样是**会话对端**（SnowLuma 协议层注释明确：138/139 两个方向都取对端）：
bot 撤回自己的消息时会被写成"好友撤回了一条消息"。现在：
- 私聊不再把对端当作者（拿不到可靠操作者就**不点名**）
- 用插件自己的 message_locator 台账判定：撤回的是 bot 自己的消息 → 直接丢弃，不进动态

### ✨ 新增：看群头像 `qq_group_profile(look_group_avatar=true)`
```
群 999888777 的资料：
  群名：测试群
  人数：42/200
  群头像：https://p.qlogo.cn/gh/999888777/999888777/640
  群头像内容：一只蓝色的猫头
```
- 协议端给 `avatar_url`（LLOneBot）就优先用；NapCat/SnowLuma 不返回头像 → 按群号拼腾讯 CDN
- 与 `qq_profile` 的看头像共用同一套提示词/超时/开关（`avatar_describe` / `avatar_desc_prompt` / `avatar_desc_timeout`）

### ⚡ 头像描述缓存接入框架 `ImageDescCache`
两级缓存（`avatar_desc_cache_seconds`，0=全关）：
1. **L1 进程内**：URL + TTL，命中连图片都不用下载
2. **L2 框架 DB**：`ctx.image_desc_cache` 按**图片内容 MD5** 落库 —— 换头像自动失效、重启后仍命中、跟随框架 15/30 天清理，和框架识别聊天图片共用同一张表

### 🎯 修复：资料卡点赞发到"该用户最后活跃的会话"
旧逻辑每个用户只记**最后一个**会话（覆盖式），群/私聊互相顶掉；现在每个用户记多个会话
（群+私聊，最多 8 个），挑选顺序：

1. 协议端给了 `group_id` → 那个群
2. 7 天内该用户**发过言**的会话里取**最新**（群聊/私聊**一起比较**）
3. 没有发言记录 → 7 天内任何活跃会话（戳过/被贴过表情）取最新
4. 都没有 → `adapter:dm:uid`

`profile_like_dm_only=true` 仍可强制私聊。
所谓"活跃"优先看**真实发言**：只在群里被戳了一下、但今天没说话，不算"最近活跃"。

### 📌 已知边界：框架的"空 notice 消息"
poke 事件若 `target != self`（例如 bot 自己戳人），框架仍会 publish 一条 **chain 为空**的消息事件：
```
[message] [qq | None] [周武]:
```
- **不会进 LLM**：`core/workflow/src/im/stages/receive.py` 的 `RouteStage` 对 `chain.is_empty()` 直接 `return False`
- **本插件不会误判**：`normalize_notice` 对 `actor == self_id` 直接丢弃（v1.1.7 修复）
- ⚠️ **S/Z 聊天插件会看到它**：它们的 `_detect_kind` 把所有 poke notice 当"真实互动"，
  且发起者取 `message.sender.user_id`（私聊里是**会话对端**）⇒ bot 自己戳人会被它们记成"用户戳了一下"，
  影响存在感/骚扰计数（不影响回复流程）。属于框架 notice 构造（`sender=user_id`）的连带问题，
  建议上游按 `sender_id` 修正。

### 🏷 私聊不再叫"群动态"
私聊上下文里块头改为 `[私聊动态·截至 HH:MM]`，群聊仍是 `[群动态·截至 HH:MM]`。

### ✅ 验证
pytest **146**（新增 17 项 poke/撤回/缓存 + 5 项群头像/块头）；
KiraAI **v3.2.0** 与 **v3.1.2** 双版本：self_scope 46 / config_matrix 25 / 深度审计 30 / 场景 27 / 安全 35 / 边界 23 / 容错 34 / 发布审计 14 / 前后端 10 / 头像 52 / compat_320 41。

</details>

<details>
<summary><b>v1.1.6</b>（2026-11-14）— 群动态改为"最新在最后"</summary>

### 🔄 排序方向 + 事件时间精确到秒
群动态从"新的在最上面（降序）"改为**"新的在最后一行"（升序）**，符合从上往下读的聊天习惯；
同时**每条事件的时间显示到秒**（块头仍只到分钟）：

```
[群动态·截至 01:31]
· 01:23:47 用户1005(小A) 被 用户8888(管理员C7) 禁言 600秒（剩约3分钟）
· 01:30:58 用户769690776(周武) 戳了戳 用户3991867505(紫小贱)
```

- 排序键是事件的**原始时间戳（秒）**，精确到秒
- 同一秒内保持插入顺序（Python 稳定排序）
- 合并型条目（戳一戳/贴表情）用组内**最新**时间参与排序

</details>

<details>
<summary><b>v1.1.5</b>（2026-11-14）— 自己被贴表情的独立冷却</summary>

### ✨ 新增 `wake_emoji_merge_seconds`（默认 5 秒，自己/他人共用）
同一条消息在 5 秒内被贴的多个表情，**合并成一条唤醒**：

```
自己被贴：  [System 用户A(张三)、用户B(李四)给你贴了得意 (ID: 4)/爱心 (ID: 66)×3]
他人被贴：  [System 用户A(张三)、用户C(王五)给用户B(李四)的消息贴了得意 (ID: 4)/爱心 (ID: 66)×3]
```

窗口内每来一次新贴表情就重置计时（debounce）。`0 = 不合并`（每条单独唤醒）。

### ✨ 新增 `wake_self_emoji_cooldown`（默认 10 秒）
自己的消息被贴表情时，两次唤醒之间至少间隔 N 秒（按会话计）。
- **0 = 不冷却**（每条都唤醒，保持旧行为）
- 与他人贴表情的 `wake_others_emoji_cooldown` **完全独立**
- 冷却只在实际发布唤醒后计时：被静默时段/每小时配额/屏蔽名单拦下的不计入

### ✅ 验证
pytest **130** + 19 个专项脚本；self_scope_integration 新增 6 条用例：
冷却（0 → 2 次，10/60 → 1 次）、合并（同消息 1 条、不同消息 2 条、0=不合并）。

</details>

<details>
<summary><b>v1.1.4</b>（2026-11-14）— 点赞诊断 + 字段兜底</summary>

### 🔍 资料卡点赞：加一条"收到即打印"的诊断日志
```
[QQ增强2] 收到资料卡点赞: adapter=… operator=… times=… 框架会话=…
```
用途：区分"协议端没发事件"和"发了但唤醒链路有问题"。

### 🛠 点赞者字段兜底
`profile_like` 的点赞者字段按 `operator_id → user_id → target_id` 依次取，
避免某些实现把点赞者放在 `user_id` 时 actor 为空、会话解析成非法的 `dm:`。

### 📌 关于 SnowLuma
实测 SnowLuma 1.14.20 的打包产物里**已经包含**完整链路
（`convertFriendProfileLike` → `friend_profile_like: { handle: convertOnly }` →
`decodeProfileLike`，条件 `msgType=0 && subType=203`），
所以插件侧不是"没适配"；若实机收不到事件，需协议端排查（或升级）。

</details>

<details>
<summary><b>v1.1.3</b>（2026-11-13）— 实机问题修复</summary>

### 🐛 资料卡点赞落到 `qq:dm:None` 幽灵会话
`profile_like` 的 notice **没有 group_id**，框架按 `user_id` 建会话，而该字段为空
→ 唤醒发到 `qq:dm:None`，bot 的回复用户永远看不到。

修复：按优先级挑一个真实会话
1. 协议端给了 `group_id`（部分实现会带）→ 用那个群；
2. 新增开关 `profile_like_dm_only`（**默认关**）：
   - **关（默认）** → bot 与该用户**最近互动的会话**（群聊/私聊按时间比较，7 天内），
     回复更容易被看到；没有历史互动则回退私聊。
   - **开** → 固定发与该用户的私聊（⚠️ bot 不是对方好友时可能发送失败）。

### 🐛 唤醒事件触发 `message_types` 弃用告警
`hasattr(adapter, "message_types")` 会调用 deprecated 属性，实机日志刷 `DeprecationWarning`。
改为直接走 `IMCapability._supported_elements`，不再产生告警，同时保留完整的
`text/img/at/reply/record/emoji/sticker/poke/file/video/forward` 列表。

</details>

<details>
<summary><b>v1.1.2</b>（2026-11-13）— 群动态：时间 + 分类型新鲜期 + 禁言状态机</summary>

### 🕒 每条事件带绝对时间
块头改为 `[群动态·截至 01:31]`，每条事件前是 `HH:MM`；
跨天用 `昨天 HH:MM`，再早用 `MM-DD HH:MM`。不再用"刚刚"这类相对表述。

### ⏳ 分类型新鲜期（不再 30 分钟常驻）
| 类型 | 默认 | 配置 |
|---|---|---|
| 戳一戳 / 贴表情 | **5 分钟** | `env_fresh_seconds` |
| 名片 / 管理员 / 头衔 / 撤回 | **15 分钟** | `env_slow_seconds` |

过期自动从摘要里消失；全局 `perceive_window_seconds` 1800 → **900**（只作安全阀）。

### 🚫 禁言改成状态机
- 进行中：`… 禁言 600秒（剩约3分钟）`
- 到期后：`… 禁言 600秒（已于 01:33 到期）` —— **只补报一轮**
- 之后：消失（内存标记，不占 token、不写盘）

### 🔔 退群/被踢不再进摘要
改成 notice 唤醒（与 QQ增强1 的入群感知同款）：
`[System 用户1001(张三)退群了]` / `[System 用户1001(张三)被用户8888(管理员C7)移出群聊]`
新增 `wake_member_decrease`（默认开）。

### ⚡ 昵称缓存：600 秒 → 10 秒（可配）
`get_group_member_info` 的昵称/群名片缓存原来是 **600 秒**（我们插件自己定的），
对"即时查询"来说太长了——改名/换群名片后十分钟内还显示旧名字。
现在默认 **10 秒**，并新增 `resolve_cache_seconds`（0 = 完全不缓存，每次都实时查）；
同一 QQ 的**并发合并**始终生效，不会因为 TTL 短而打爆协议端。

### ✨ 新增配置
`env_show_time`（默认 true）、`env_fresh_seconds`（300）、`env_slow_seconds`（900）、
`wake_member_decrease`（true）、`resolve_cache_seconds`（10）。

### ✅ 验证
pytest **127** + 19 个专项脚本，KiraAI **3.1.2 / 3.2.0-beta.1** 双版本全过。

</details>

<details>
<summary><b>v1.1.1</b>（2026-11-13）— 去重复述 + 互动提示</summary>

### 🐛 修复：Notice 行被追加 `↑该条消息被贴了…`
唤醒通知里带了原文摘要，贴表情改写器用"原文前 50 字"做匹配 → 把通知行也当成原消息，
给它追加了一遍标注，和原消息上的标注重复。
现在改写器（贴表情 / 撤回）**跳过 `[时间] Notice [...]` 开头的系统通知行**。

### ✨ 新增：`wake_hint` 互动提示
被贴表情 / 资料卡被赞时，在唤醒正文后附一句可配置的引导（默认
`可随手用工具互动（回贴表情、回赞等）；别刷屏，也可以不回应。`），
让模型知道"可以随手互动，但别刷屏"。留空=不加。头衔/被踢不加（没什么可互动的）。

### ✨ 优化：同批次不再重复复述
他人消息被贴表情时，先看那条消息**是否还在会话缓冲区**：
- 在 → 它会和本次唤醒同批送给 LLM（自带 `↑` 标注）→ 唤醒正文只留互动提示；
- 不在 → 保留完整通知。
检查只是读一个 list（不加锁、不 await），不会卡事件循环。

### 🎨 图标与 QQ增强1 统一
之前是 512×512 带浅粉背景、图形只占 346×410（留白偏大）。
现在直接复用 QQ增强1 的 `icon.svg` 路径（官方 QQ logo，viewBox 24×24），
只换填充色 `#1EBAFC → #F8A5C2`，透明背景、图形 432×512 满幅 ——
和 QQ增强1 **逐像素同轮廓**（alpha 差异 0），就是换了个颜色。

### 🐛 顺带修复：唤醒事件的 `supported_elements` 只有 `["text"]`
适配器实例没有 `.name` 属性（只有 `.info.name`），旧代码按 `.name` 取实例失败 →
唤醒事件只注册了 text tag，唤醒轮里 LLM 输出 `<img>` / `<at>` / `<reply>` 会被丢掉。
现在兼容 AdapterInfo / 适配器实例两种入参，能拿到完整列表
（`text/img/at/reply/record/emoji/sticker/poke/file/video/forward`）。
（同时合并了 `WakeScheduler` 里重复定义两次的 `_get_supported_elements`。）

### 🗑 移除：`fetch_emoji_like` 工具
唤醒通知本身已带"谁贴的 + 表情 ID + 次数"，该工具只剩查历史名单的增量价值，
性价比低。工具数 3 → 2（`qq_profile` / `qq_group_profile`）。

</details>

<details>
<summary><b>v1.1.0</b>（2026-11-13）— 群动态边界 + 唤醒格式对齐 + 资料卡修复</summary>

### 🐛 修复：群动态混入"与自己相关"的事件
- 别人戳 bot 会进群动态（`用户X 戳了戳 用户<bot>`）→ **不应该**。
- 群动态现在**只记「其他用户之间」**：poke（他人↔他人）、贴表情、禁言/解禁、
  退群、头衔、名片、管理员变动，全部按 actor/target 判定；
  涉及 bot 自己的（actor 或 target == self_id）一律不入摘要。
- 这轮同时补掉同类问题：
  - bot 禁言/解禁别人（自己的动作）→ 不再进动态
  - bot 自己的群名片 / 管理员身份变动 → 不再进动态
  - bot 自己退群 → 不再进动态
  - bot 撤回消息（自己的动作）→ 不再进动态
  - bot 给别人贴表情 → 不再进动态
  - **自己被授予头衔**：此前 `E_TITLE_SELF` 是死常量（永远不产生），
    导致 `wake_title` 开关失效 + 头衔事件混进群动态 → 已修
- 历史残留兜底：旧版本落盘的事件在渲染前按当前 self_id 再过滤一次。

### 🎨 唤醒通知格式对齐框架 notice（与 QQ增强1 完全一致）
- 此前唤醒事件是自造的普通消息：`user_nickname: 系统唤醒, user_id: system_qq_enhance2`
  —— LLM 上下文里凭空多出一个"人"。
- 现在按**框架 notice 形态**构造：`is_notice=True` + sender = 真实触发者，
  LLM 看到的是：
  ```
  [Nov 13 2026 00:40 Fri] Notice [group_id: 188395693, user_id: 1001] | [System 用户1001(张三)给你贴了得意 (ID: 4)×2]
  ```
  与戳一戳（`[Poke ...]`）、禁言（`[System ...禁言了你N秒]`）、入群完全同一种写法。
- 顺带补齐唤醒事件里缺失的 `group_name`（此前 `group_name: None`），
  优先用真实群消息里学到的名字，未命中时 `get_group_info` 拉一次并缓存。

### 🐛 修复：`qq_profile` 查询超时 / 签名 / 头像
- `qq_profile args: {}` → `请求 get_stranger_info 超时`：
  self_id 解析为空 → 发了 `user_id=""`。现在多级兜底
  （事件 self_id → bot_pid → bot.self_id → config），彻底不空发；
  且每次调用带 6 秒独立超时，超时也返回已有信息而不是整条失败。
- 个性签名永远"（未提供）"：NapCat 的字段名是 **`long_nick`**，
  代码只读 `sign` / `personal_note`。现已兼容
  `long_nick / longNick / longnick / sign / personal_note`。
- 头像永远"（该协议端未提供）"：NapCat 的 `get_stranger_info`
  **不返回任何头像字段**。现在依次回退
  `headUrl/avatar` → `get_qq_avatar`（LLOneBot）→ 腾讯头像 CDN
  `https://q1.qlogo.cn/g?b=qq&nk={QQ}&s=640`（按 QQ 号直出，不依赖协议端）。
- `qq_group_profile` 同样补上 self_id 兜底（空 user_id 会挂满超时）。

### 🛠 工具增强 / 修复
- **改个性签名**：此前只调 `set_qq_profile(personal_note)`，NapCat 上实际改不动却报成功。
  现在优先 NapCat 专用 **`set_self_longnick`（longNick）**，失败再回退 OB11 参数。
- **改群名片**：`set_group_card` 独立提交，空字符串=清除；失败原因原样透出。
- **查资料显示群名片**：群聊里查询会**额外多一行**「群名片」+「群角色」（与昵称并存，不替代昵称）。
- **换头像支持 `recent`**：图片在 prompt 里只剩 `[Image: 描述]`，LLM 拿不到路径 →
  `avatar="recent"` 自动取当前对话里最新的一张图。
- **换头像支持本地路径**：≤2MB 的本地文件会**读成 `base64://`** 再发给协议端
  （NapCat 跑在 Docker / 另一台机器也能用）；文件不存在时直接报错，不发 API。
- 头像描述加缓存（默认 60 秒，可用 **`avatar_desc_cache_seconds`** 调整，0=不缓存、每次重新识别）。
- **头像识别新增开关 `avatar_describe`（默认开）**：关掉后查询只返回头像链接；
  开启时 bot 还可以按次传 `look_avatar=false` 跳过识别（由模型自己决定要不要"看"）。
- 工具 schema 精简 1701 → 1506 字符（≈425 → 376 tokens/轮）。

### ✅ 验证
- 新增 `tests/frontend_backend_check.py`（schema↔代码↔README 12 项一致性）、
  `tests/perf_token_check.py`（token/耗时量化）
- 新增 `tests/test_self_scope.py`（20 项，归一化边界）+
  `tests/self_scope_integration.py`（15 项，真实框架端到端）
  + `tests/test_profile_tool.py`（10 项）
- 新增 `tests/show_wake_notifications.py`：逐字节打印每条唤醒的 LLM 实际输入
- pytest 107 项 + 18 个专项脚本全部通过（3.1.2 / 3.2.0-beta.1）

</details>

<details>
<summary><b>v1.0.1</b>（2026-11-12）— 修复实机安装失败</summary>

### 🐛 修复
- **修复实机安装后插件初始化失败**：`'AdapterManager' object has no attribute 'get_all_adapters'`。
  真实框架的 `AdapterManager` 只有 `get_adapters()`（返回 `dict[str, BaseAdapter]`）
  和 `get_adapters_info()`（返回配置列表），**没有** `get_all_adapters()`。
  改为多路径兼容：`get_adapters()` → `get_adapters_info()` + `get_adapter()` → 兜底。
  （此前测试都用自造 mock，掩盖了这个问题；已同步修正所有测试 mock 并新增
  `tests/real_api_check.py` 用真实类反射核对全部框架 API 调用。）

### ✅ 兼容性
- 实测通过 **KiraAI v3.1.2** 与 **v3.2.0-beta.1** 两个版本的全量验证。

### 🔍 昵称补全
- 框架的 notice 事件**只带发起人昵称**（`sender.nickname`），
  被戳/被禁言/被贴表情的 target **没有昵称** → 摘要里出现
  `用户769690776(周武) 戳了戳 用户1593462500`（后一个只有 QQ 号）。
- 新增 `NameResolver`：自动查询补全 target 昵称，带 10 分钟缓存 + 并发合并，
  单次查询 2 秒超时（失败降级为纯 QQ 号）。
- 配置项 `resolve_nickname` 可关闭（省 API 调用）。

### 🔍 新增调试能力
- `debug_log` 开关现在真正生效（此前是死配置）：开启后打印
  「事件收集」与「群动态注入」日志，方便确认功能是否在工作。

</details>

<details>
<summary><b>v1.0.0</b>（2026-11-12）— 首个版本</summary>

### ✨ 新增

**感知层（他人事件）**
- 戳一戳 / 贴表情 / 禁言 / 撤回 / 退群 等他人事件收集
- 合并为紧凑环境摘要（带 QQ 号：`用户{QQ}({昵称})`）
- 分层预算渲染：超限时从最旧事件整条删除，不截断字符串

**唤醒层（自己相关）**
- 被贴表情 / 资料卡被赞 / 头衔授予 / 被移出群 → 唤醒
- 点赞合并防抖（同用户 10 秒窗口合并）
- 每小时上限 60 次（-1 无限制），支持静默时段
- 他人消息被贴表情可选唤醒（默认关，冷却 20 秒）

**改写**
- 撤回消息原文下追加标注（区分本人 / 管理员 / 群主操作）
- 他人消息被贴表情同样改写原文标注（带表情 ID，方便回贴）

**工具**
- `qq_profile`：读写合体（昵称 / 签名 / 性别 / **头像**）
- `qq_group_profile`：读写合体（群名片 / 头衔 / **群头像**）

**安全**
- 昵称净化：防 prompt 注入（`<system>...`）、防破坏单行结构、
  保留 emoji ZWJ 组合、超长截断

### 🔒 兼容
- NapCat / LLOneBot / SnowLuma 三家协议端
- QQ增强1 / S / Z / 默认聊天 / JEV / anti-harass / group_manager 等 10+ 插件

</details>

## 许可

AGPL-3.0