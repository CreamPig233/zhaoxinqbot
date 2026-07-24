# zhaoxinqbot

`zhaoxinqbot` 是一个基于 NapCat / OneBot 11 正向 WebSocket 的 QQ 招新群机器人，主要服务于“招新群”和“管理群”两类群聊场景。

当前默认群号：

- 招新群：`810192062`
- 管理群：`1065588188`

项目使用 Python 实现，核心目标是把入群实名审核、群消息留档、被撤回消息追踪、预设问题自动回复这些高频管理工作自动化，并保留后续扩展接口。

## 功能总览

### 1. 群成员记录者和实名认证官

机器人会监听招新群的成员变动事件。只要有人加入或退出招新群，都会在本地写入一条带时间的成员事件日志；即使关闭实名认证功能，这部分记录也不会停止。

当实名认证功能开启时，新成员加入招新群后会触发以下流程：

1. 机器人立即对新成员执行群禁言。
2. 机器人在招新群 @ 新成员，并提示其私聊机器人提交实名信息。
3. 用户私聊机器人发送实名信息，例如：`实名 张三 2026001 计算机学院`。
4. 机器人将实名信息推送到管理群，生成一条待审核通知。
5. 管理群内审核人可以批准或拒绝。
6. 批准后，机器人解除该用户在招新群的禁言，并私聊通知审核通过。
7. 拒绝后，机器人私聊通知用户重新提交实名信息和拒绝原因。

管理群支持的实名审核命令都配置在 `strings.yaml`：

- `批准 QQ号`
- `拒绝 QQ号 原因`
- `取消实名 QQ号`

审核人也可以直接回复机器人发出的审核通知：

- 回复 `批准`
- 回复 `拒绝 原因`

实名认证状态会保存在 `data/realname.json`，包括：

- `pending`：待审核记录
- `verified`：已通过记录
- `rejected`：被拒绝记录
- `revoked`：被手动取消的实名记录
- `review_messages`：管理群审核通知消息 ID 与用户 QQ 的映射

当前保留的扩展点：

- 自动实名审核：实现 `RealNameAuditor.auto_review()`，返回 `"approve"`、`"reject"` 或 `None`
- 一人一实名：通过 `config.yaml` 的 `realname.one_qq_one_identity` 开关控制
- 手动取消实名：管理群发送 `取消实名 QQ号`
- 实名功能总开关：通过 `config.yaml` 的 `realname.enabled` 控制

### 2. 消息记录官

机器人会实时记录所有群聊消息副本。每条群消息会保存为一个 JSON 文件，包含原始 OneBot 事件、归档时间、媒体文件列表和撤回状态。

支持记录的消息内容包括：

- 文字
- 图片
- 语音
- 视频
- 文件
- 表情或转成图片的商城表情
- OneBot 消息段中保留下来的其他原始数据

如果开启 `message_archive.download_media`，机器人会尽量把图片、语音、视频、文件等媒体副本下载或复制到本地。媒体文件是否能成功保存取决于 NapCat 上报内容以及 `get_image` / `get_file` 接口能否解析到 URL 或本地路径。

当群消息被撤回时，机器人不会删除本地副本，而是在对应消息 JSON 中追加撤回标记：

- `recalled: true`
- 撤回标记写入时间
- 群号
- 消息 ID
- 原发送者 QQ
- 撤回操作者 QQ
- 撤回事件原始内容

相关数据路径：

- `data/messages/<群号>/<消息ID>.json`
- `data/media/<群号>/<消息ID>/`
- `data/message_index.json`

### 3. AI 回答问题

机器人支持维护一组预设问题与回答。群内文字消息到来时，机器人会判断该消息是否属于某个预设问题；如果命中，就发送对应回答。

为了避免刷屏，机器人默认会在 60 秒后撤回自己发出的问答回复。这个时间可以在 `config.yaml` 中修改：

```yaml
qa:
  recall_after_seconds: 60
```

问答判断有两层：

1. 本地相似度判断：默认启用，不需要 API Key。
2. 大模型判断：可选启用，支持 OpenAI 兼容 Chat Completions 接口。

预设问题、别名和回答放在 `strings.yaml`：

```yaml
qa:
  preset_answers:
    - question: "怎么报名"
      aliases:
        - "如何报名"
        - "报名方式"
      answer: "请关注群公告中的报名链接，按要求填写信息并等待通知。"
```

如果启用大模型判断，模型只负责“分类是否命中预设问题”，最终回复内容仍然来自 `strings.yaml`，这样可以避免模型自由发挥导致口径不一致。

## 配置文件分层

项目现在只有两个需要人工维护的 YAML 文件。

### config.yaml

`config.yaml` 只放运行参数、功能开关、群号、外部服务连接信息，例如：

- NapCat WebSocket 地址
- NapCat token
- 招新群和管理群 QQ 群号
- 实名功能开关
- 禁言时长
- 审核人 QQ 白名单
- 消息归档开关
- 是否下载媒体
- 问答撤回时间
- LLM API 地址、模型和 Key
- 本地数据目录

### strings.yaml

`strings.yaml` 只放字符串内容，例如：

- 用户提交实名的命令词
- 管理群批准、拒绝、取消实名的命令词
- 入群提示
- 格式错误提示
- 审核通过/拒绝/取消的通知文案
- 管理群审核通知模板
- LLM 分类提示词
- 预设问题、别名和回答

## NapCat 配置

在 NapCat WebUI 中建议使用：

- 网络类型：WebSocket 服务端，即正向 WS
- 消息上报格式：`array`
- 是否上报自身消息：通常关闭
- token：如果配置了 token，需要与 `config.yaml` 的 `napcat.access_token` 保持一致

机器人侧默认连接：

```yaml
napcat:
  ws_url: ws://127.0.0.1:3001/
```

## 运行

安装依赖：

```powershell
pip install -r requirements.txt
```

启动机器人：

```powershell
python run_bot.py
```

启动后，终端会显示正在连接的 NapCat WebSocket 地址。连接断开时，机器人会按 `napcat.reconnect_seconds` 自动重连。

## 数据文件

运行时数据默认写入 `data/`，该目录已被 `.gitignore` 忽略。

- `data/realname.json`：实名审核状态
- `data/membership_events.jsonl`：招新群成员加入/退出日志
- `data/message_index.json`：消息 ID 到本地副本路径的索引
- `data/messages/<群号>/<消息ID>.json`：群消息 JSON 副本
- `data/media/<群号>/<消息ID>/`：媒体副本

## 仓库忽略规则

当前不会提交：

- `data/`
- `NapCatDocs/`
- Python 缓存文件

`config.yaml` 和 `strings.yaml` 会提交到仓库。群号按需求不脱敏；如果未来在配置中填写真实 token 或 API Key，请注意仓库可见性。

## 扩展建议

自动实名审核可以从 `zhaoxinqbot/realname.py` 的 `auto_review()` 开始扩展，例如接入名单数据库、学号校验接口或 OCR。

问答能力可以从 `zhaoxinqbot/qa.py` 的 `match_question()` 扩展，例如改成向量检索、多轮问答或更复杂的权限判断。

消息归档可以从 `zhaoxinqbot/messages.py` 扩展，例如上传到对象存储、生成管理后台检索索引或定期压缩历史媒体文件。
