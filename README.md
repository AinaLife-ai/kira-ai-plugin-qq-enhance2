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
- **`qq_profile`**：查询或修改 QQ 用户资料（昵称/个性签名/性别）。不传 user_id=操作自己；传 user_id=查询他人（只读）。**查询时自动下载头像并用 VLM 查看头像内容**（不是只给 URL，LLM 能真正"看到"头像）。
- **`qq_group_profile`**：查询或修改群成员资料（群名片/头衔）。不传 user_id=操作自己；传 user_id=查询他人（只读）。
- **`fetch_emoji_like`**：拉取指定 QQ 消息的全部贴表情回应（含贴的人、表情 ID、次数）。支持分页。

### 协议端兼容
- NapCat / LLOneBot / SnowLuma 三家协议端。
- 贴表情、资料卡点赞、戳一戳、禁言、进退群、撤回、名片/头衔/管理员/精华/文件 等事件全覆盖。

## 配置

详见 `schema.json`。关键配置：

| 配置 | 默认 | 说明 |
|------|------|------|
| `collect_poke` | true | 感知他人戳一戳 |
| `collect_emoji_like` | true | 感知他人贴表情 |
| `collect_group_ban` | true | 感知他人被禁言 |
| `collect_recall` | true | 感知撤回 |
| `collect_member_decrease` | true | 感知退群/被踢 |
| `wake_self_emoji` | true | 被贴表情→唤醒 |
| `wake_others_emoji` | false | 他人消息被贴表情→可唤醒（默认关） |
| `wake_others_emoji_cooldown` | 20 | 他人消息被贴表情唤醒冷却（秒） |
| `wake_profile_like` | true | 资料卡被赞→唤醒 |
| `wake_title` | true | 头衔授予→唤醒 |
| `wake_kicked` | true | 被移出群→唤醒 |
| `profile_like_merge_window_seconds` | 10 | 点赞合并窗口（秒） |
| `wake_max_per_hour` | 60 | 每会话每小时最大唤醒次数；-1=无限制 |
| `perceive_window_seconds` | 1800 | 感知时间窗口（秒） |
| `env_max_chars` | 800 | 环境摘要字符上限（超限踢旧淘汰） |

## 兼容性

已核实以下插件零冲突：
- **QQ增强1**（互补不重叠：我们做退群/他人事件，它做进群/自己禁言）
- **S / Z**（互斥，trigger 走标准路径，与 buffer/批次模型同构）
- **默认聊天 / JEV / anti-harass / multi-decision / midflight / group_manager / history_plugin** 等 10+ 插件

## 安装

1. 下载本仓库代码。
2. 将 `manifest.json` / `schema.json` / `main.py` / `core/` / `tools/` / `icon.png` 复制到 KiraAI 的 `data/plugins/kira-ai-plugin-qq-enhance2/` 目录。
3. 在 WebUI 插件管理中启用。

## 许可

AGPL-3.0