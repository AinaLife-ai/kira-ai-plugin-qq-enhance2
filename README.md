# QQ增强2（kira-ai-plugin-qq-enhance2）

> 增强数字生命在 QQ 平台的能力：他人事件感知、自己相关事件唤醒、资料卡工具、三家协议端兼容。

## 功能

### 感知层（他人事件）
- **戳一戳**：收集群内他人之间的戳一戳事件，合并入环境摘要。
- **贴表情**：收集群内他人给他人消息贴表情的事件，在原消息下追加标注（`[贴表情｜用户X 贴了🔥]`），合并入环境摘要；可选唤醒（默认关，冷却 20 秒可配）。
- **禁言/解禁**：收集群内成员被禁言/解除禁言事件，合并入环境摘要。
- **撤回**：收集消息撤回事件，在原文下补充撤回标注（区分操作者身份）。
- **退群/被踢**：收集成员退群/被移出群聊事件（进群归 QQ增强1）。

### 唤醒层（自己相关事件）
- **被贴表情**：自己的消息被贴表情时立即唤醒（trigger 进批次合并）。
- **资料卡被赞**：资料卡被点赞时唤醒（同用户 10 秒内合并防抖，可配）。
- **头衔授予**：自己被封头衔时立即唤醒。
- **被移出群**：自己被移出群聊时立即唤醒。

### 工具层
- **`qq_profile`**：查询或修改 QQ 用户资料（昵称/个性签名/性别/头像）。不传 user_id=操作自己；传 user_id=查询他人（只读）。**查询时自动下载头像并用 VLM 查看头像内容**（不是只给 URL，LLM 能真正"看到"头像）。**支持设置自己头像**（`avatar` 传图片路径/URL/base64）。
- **`qq_group_profile`**：查询或修改群成员资料（群名片/头衔）。不传 user_id=操作自己；传 user_id=查询他人（只读）。**支持设置群头像**（`group_portrait`，需管理员权限，SnowLuma 不支持）。
- **`fetch_emoji_like`**：拉取指定 QQ 消息的全部贴表情回应（含贴的人、表情 ID、次数）。支持分页。

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
| `collect_recall` | true | 感知撤回 |
| `collect_member_decrease` | true | 感知退群/被踢 |
| `collect_title` | false | 感知头衔授予 |
| `collect_card_admin` | false | 感知名片/管理员变更 |

### 唤醒层（自己相关事件）

| 配置 | 默认 | 说明 |
|------|------|------|
| `wake_self_emoji` | true | 自己消息被贴表情→唤醒 |
| `wake_others_emoji` | false | 他人消息被贴表情→可唤醒（默认关） |
| `wake_others_emoji_cooldown` | 20 | 他人消息被贴表情唤醒冷却（秒） |
| `wake_profile_like` | true | 资料卡被赞→唤醒 |
| `wake_title` | true | 头衔授予→唤醒 |
| `wake_kicked` | true | 被移出群→唤醒 |
| `profile_like_merge_window_seconds` | 10 | 点赞合并窗口（秒） |
| `wake_quiet_hours` | "" | 静默时段（如 23:00-08:00），空=不静默 |
| `wake_max_per_hour` | 60 | 每会话每小时最大唤醒次数；-1=无限制 |

### 环境摘要

| 配置 | 默认 | 说明 |
|------|------|------|
| `perceive_window_seconds` | 1800 | 感知时间窗口（秒） |
| `perceive_max_events` | 20 | 每会话最多保留事件数（LRU） |
| `perceive_max_per_type` | 6 | 每类型最多渲染条数 |
| `env_max_chars` | 800 | 环境摘要字符上限（超限从最旧事件整条删除，不截断） |
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
| `tools_fetch_emoji_like` | true | 启用 fetch_emoji_like |
| `avatar_desc_prompt` | "" | 头像描述提示词，留空=跟随框架 WebUI 图片识别提示词设置 |
| `avatar_desc_timeout` | 20 | 头像描述超时（秒），超时降级为只显示 URL |

### 调试

| 配置 | 默认 | 说明 |
|------|------|------|
| `poke_action_text` | false | 戳一戳用协议端真实文案（默认关=统一显示"戳了戳"，与框架归一解析一致） |
| `resolve_nickname` | true | 补全事件昵称（框架只带发起人昵称，target 需额外查询；带 10 分钟缓存） |
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
  - 唤醒事件 sender 用 `system_qq_enhance2` 前缀 → S/Z 判为系统事件，不污染统计
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
- `fetch_emoji_like`：贴表情查询（分页）

**安全**
- 昵称净化：防 prompt 注入（`<system>...`）、防破坏单行结构、
  保留 emoji ZWJ 组合、超长截断

### 🔒 兼容
- NapCat / LLOneBot / SnowLuma 三家协议端
- QQ增强1 / S / Z / 默认聊天 / JEV / anti-harass / group_manager 等 10+ 插件

</details>

## 许可

AGPL-3.0