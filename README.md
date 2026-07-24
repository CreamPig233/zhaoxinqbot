# zhaoxinqbot

基于 NapCat / OneBot 11 正向 WebSocket 的 QQ 招新群机器人。

## 功能

- 招新群成员进退群记录。
- 新成员入群后禁言并提示私聊提交实名信息。
- 私聊提交 `实名 姓名 学号/工号 学院/部门` 后推送到管理群审核。
- 管理群指定审核人可发送 `批准 QQ号`、`拒绝 QQ号 原因`，或回复审核通知发送 `批准` / `拒绝 原因`。
- 已通过实名审核后自动解除招新群禁言。
- 本地维护 QQ 号与实名信息对应关系，支持取消实名：`取消实名 QQ号`。
- 实时保存群消息 JSON 副本，并尽量下载图片、视频、表情、文件等媒体到本地。
- 群消息撤回时在本地副本中追加撤回标记，记录撤回人和时间，不删除原副本。
- 对预设问答做轻量相似度判断，命中后回复并在 1 分钟后撤回。

## 配置

首次运行会从 `config.example.yaml` 自动生成 `config.yaml`。请重点修改：

- `napcat.ws_url`：NapCat 正向 WS 地址，例如 `ws://127.0.0.1:3001/`
- `napcat.access_token`：NapCat 网络配置中的 token，没有则留空
- `realname.admin_approvers`：允许审核的 QQ 号列表；留空表示管理群所有人可审核
- `qa.preset_answers`：预设问题、别名和回答
- `qa.llm`：可配置 OpenAI 兼容 Chat Completions 接口；未启用或失败时自动用本地相似度兜底

NapCat WebUI 中请启用 WebSocket 服务端，消息上报格式建议选择 `array`。

## 运行

```powershell
pip install -r requirements.txt
python run_bot.py
```

## 数据文件

- `data/realname.json`：实名审核状态、待审核索引、撤销记录。
- `data/membership_events.jsonl`：招新群成员加入/退出日志。
- `data/messages/<群号>/<消息ID>.json`：群消息副本。
- `data/media/<群号>/<消息ID>/`：媒体副本。

## 扩展点

- 自动实名审核：实现 `zhaoxinqbot.realname.RealNameAuditor.auto_review()`，返回 `"approve"`、`"reject"` 或 `None`。
- 更强的 AI 语义判断：可直接配置 `qa.llm`，也可重写 `zhaoxinqbot.qa.QuestionAnswerer.match_question()`。
