# zhaoxinqbot

`zhaoxinqbot` 是一个基于 NapCat / OneBot 11 正向 WebSocket 的 QQ 招新群机器人，主要服务于“招新群”和“管理群”两类群聊场景。

项目使用 Python 实现，核心目标是把入群实名审核、群消息留档、被撤回消息追踪、预设问题自动回复这些高频管理工作自动化，并保留后续扩展接口。

## 功能总览

### 1. 群成员记录者和实名认证官

机器人会监听招新群的成员变动事件。只要有人加入或退出招新群，都会在本地写入一条带时间的成员事件日志；即使关闭实名认证功能，这部分记录也不会停止。

当实名认证功能开启时，新成员加入招新群后会触发以下流程：

1. 机器人立即对新成员执行群禁言。
2. 机器人在招新群 @ 新成员，并提示其私聊机器人提交实名信息。
3. 用户私聊机器人发送实名信息，例如：`实名 张三 2026001`。
4. 机器人立刻创建一条实名申请记录，并将状态设为 `auto_reviewing`。
5. 机器人把申请信息交给 `config.yaml` 指定的外部 Python 审核文件处理。
6. 外部审核文件返回通过时，机器人立即解除该用户在招新群的禁言，并记录为通过。
7. 外部审核文件返回拒绝时，机器人保持禁言，私聊告知用户拒绝原因，并允许用户重新提交。
8. 外部审核文件返回超时或调用超时时，机器人私聊提示“移交管理员人工审核”，并把申请发送到管理群。
9. 管理群内审核人可以批准或拒绝人工审核申请。

在 `auto_reviewing` 或 `manual_pending` 状态期间，同一个用户不能重复提交实名申请。机器人会私聊提示其等待当前审核完成；程序还会使用按 QQ 号划分的异步锁，避免并发消息在检查和创建申请之间产生竞态。

如果用户在自动审核或人工审核完成前退群，当前申请会记录为 `cancelled_by_leave` 并从待审核索引移除。之后即使外部审核器返回迟到结果，也不会重新解除禁言或写入通过记录。

管理群支持的实名审核命令都配置在 `strings.yaml`：

- `批准 QQ号`
- `拒绝 QQ号 原因`
- `取消实名 QQ号`

审核人也可以直接回复机器人发出的审核通知：

- 回复 `批准`
- 回复 `拒绝 原因`

外部审核文件默认为 [realname_reviewer.py](F:/zhaoxinqbot/realname_reviewer.py)。首次使用时，可以复制 [realname_reviewer.example.py](F:/zhaoxinqbot/realname_reviewer.example.py) 创建本地审核文件。`realname_reviewer.py` 已加入 `.gitignore`，适合放置本地审核逻辑、名单或接口密钥。该文件中的 `review_application(application)` 由你自行实现，机器人只负责调用和处理返回状态。支持返回：

- `"approve"` 或 `"通过"`
- `"reject"` 或 `"拒绝"`
- `"timeout"` 或 `"超时"`
- `{"status": "reject", "reason": "原因"}`
- `{"status": "approve", "reason": "名单匹配", "college": "学院名称"}`：自动审核通过时可返回学院；机器人仅接受并保存 `college` 这一扩展字段。

实名认证状态会保存在 `data/realname.json`，包括：

- `pending`：待审核记录
- `verified`：已通过记录
- `rejected`：被拒绝记录
- `revoked`：被手动取消的实名记录
- `applications`：每一次实名申请的完整记录，其中退群取消会以 `cancelled_by_leave` 状态保留
- `active_by_user`：用户当前正在审核中的申请索引
- `review_messages`：管理群审核通知消息 ID 与用户 QQ 的映射

每一次申请的状态变化还会追加写入 `data/realname_applications.jsonl`，其中包含申请 ID、发起用户、发起时间、提交的实名信息、状态、是否通过、状态说明等字段，方便审计和后续统计。

学校接口审核返回的学院会写入申请的 `identity.college`，并随通过、拒绝或转人工状态一同保留。学院不由用户输入。

已有已通过记录可以通过 `migrate_realname_colleges.py` 手工回填学院：

```powershell
python migrate_realname_colleges.py export
# 编辑 data/realname_college_backfill.csv 的“学院”列。
python migrate_realname_colleges.py import --dry-run
python migrate_realname_colleges.py import
```

导入会校验 QQ 号、申请 ID、姓名和学号，仅回写非空学院，并自动备份 `data/realname.json`。

当前保留的扩展点：

- 自动实名审核：实现 `realname_reviewer.py` 中的 `review_application(application)`
- 审核器模板：复制 `realname_reviewer.example.py` 为本地的 `realname_reviewer.py`
- 一人一实名：通过 `config.yaml` 的 `realname.one_qq_one_identity` 开关控制
- 手动取消实名：管理群发送 `取消实名 QQ号`
- 实名功能总开关：通过 `config.yaml` 的 `realname.enabled` 控制

### 2. 消息记录官

机器人会实时记录群聊消息副本。每条群消息会保存为一个 JSON 文件，包含原始 OneBot 事件、归档时间、媒体文件列表和撤回状态。可通过 `message_archive.group_ids` 设置归档群白名单；留空表示记录所有群。

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
  group_ids:
    - 810192062
  recall_after_seconds: 60
```

`qa.group_ids` 是问答生效群列表。当前只包含招新群，其他群消息不会进入本地或
大模型分类流程。

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

项目的公开配置分为两个 YAML 文件，敏感凭据单独保存在 `.secrets`。

首次运行前复制 `.secrets.example` 为 `.secrets`，然后填写 NapCat token、QA API Key
和实名审核登录账号密码。`.secrets` 已加入 `.gitignore`，不得提交到仓库。

### config.yaml

`config.yaml` 只放运行参数、功能开关、群号、外部服务连接信息，例如：

- NapCat WebSocket 地址
- NapCat token
- 招新群和管理群 QQ 群号
- 实名功能开关
- 禁言时长
- 审核人 QQ 白名单
- 外部实名审核 Python 文件路径、函数名和超时时间
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
- token：如果配置了 token，需要与 `.secrets` 的 `napcat.access_token` 保持一致

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
- `data/realname_applications.jsonl`：每一次实名申请和状态流转的详细日志
- `data/membership_events.jsonl`：招新群成员加入/退出日志
- `data/message_index.json`：消息 ID 到本地副本路径的索引
- `data/messages/<群号>/<消息ID>.json`：群消息 JSON 副本
- `data/media/<群号>/<消息ID>/`：媒体副本

## 仓库忽略规则

当前不会提交：

- `data/`
- `NapCatDocs/`
- `realname_reviewer.py`
- Python 缓存文件

`config.yaml`、`strings.yaml` 和 `.secrets.example` 会提交到仓库；真实 token、API Key 和登录凭据只填写在被忽略的 `.secrets` 中。

## 扩展建议

自动实名审核可以从 [realname_reviewer.py](F:/zhaoxinqbot/realname_reviewer.py) 的 `review_application()` 开始扩展，例如接入名单数据库、学号校验接口或 OCR。

问答能力可以从 `zhaoxinqbot/qa.py` 的 `match_question()` 扩展，例如改成向量检索、多轮问答或更复杂的权限判断。

消息归档可以从 `zhaoxinqbot/messages.py` 扩展，例如上传到对象存储、生成管理后台检索索引或定期压缩历史媒体文件。
