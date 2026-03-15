# tg_forward_bot

基于 Telethon 的 Telegram 转发工具，采用 `Bot + UserBot` 双客户端架构，支持：

- 私聊贴链接解析（公开/私有/评论区/话题）
- `/sync` 历史同步
- `/monitor` 实时监控转发
- 多策略降级转发（尽量低成本，失败自动兜底）

## 1. 功能概览

- `Bot` 负责命令交互与发送（写）
- `UserBot` 负责访问受限来源与下载（读）
- 支持 `copy`（默认）与 `forward` 模式
- 支持评论链接：`?comment=123`
- 支持单条相册消息：`?single`
- 无法转发时自动发送 `#fail2forward`

## 2. 项目结构

```text
tg_forward_bot/
├── main.py
├── config.example.yaml
├── bot/
│   ├── handlers.py
│   ├── link_parser.py
│   └── telegram_utils.py
├── core/
│   ├── forwarder.py
│   ├── message_logic.py
│   ├── monitor.py
│   ├── rate_limiter.py
│   └── syncer.py
├── db/
│   ├── database.py
│   └── models.py
└── docs/
    ├── architecture.md
    └── operations.md
```

## 3. 环境要求

- Python 3.11+
- Telegram `api_id` / `api_hash`
- 一个 Bot Token
- 一个 UserBot 账号（手机号登录）

## 4. 快速启动

1. 安装依赖

```bash
pip install -r requirements.txt
```

2. 复制并编辑配置

```bash
cp config.example.yaml config.yaml
```

3. 启动

```bash
python main.py
```

首次启动会要求 UserBot 完成登录验证码验证，成功后会在 `sessions/` 下保存会话。

## 5. 配置说明

参考 `config.example.yaml`：

- `api_id` / `api_hash`: Telegram API 凭证
- `bot_token`: Bot token
- `phone`: UserBot 手机号（国际格式）
- `admin_ids`: 管理员用户 ID 列表
- `allow_public_resolve`: 是否允许非管理员私聊解析链接
- `rate_limit`: 速率与 FloodWait 退避参数

## 6. 命令说明

- `/start`: 启动说明
- `/help`: 帮助
- `/sync <链接> [--forward]`: 同步历史消息到当前群/频道
- `/monitor <链接> [--forward]`: 监控新消息转发到当前群/频道
- `/list`: 任务管理（暂停/恢复/删除/清空）
- `/settings`: 查看限流配置

私聊直接贴 `t.me` 链接会触发解析与转发。

## 7. 转发策略

单条消息与相册都使用同一套降级策略：

1. 策略1：Bot 直接转发/复制
2. 策略2：UserBot 读取后由 Bot 转发/复制
3. 策略3：UserBot 下载再由 Bot 上传
4. 策略4：发送失败标记（`#fail2forward`）

说明：日志中的“策略1成功”表示“第一层尝试成功”，不一定是原生 `forward`。在 `copy` 模式下可能是 `send_file/send_message`。

## 8. 私有来源与评论区

- 公开频道主贴：Bot 常可直接处理
- 私有频道/群组：通常依赖 UserBot 读取
- 评论区链接 `?comment=`：实际消息在 discussion 群里；若 discussion 是私有，常会降级到策略3

## 9. Docker 运行

```bash
docker compose up -d --build
```

挂载目录：

- `./config.yaml` -> `/app/config.yaml`
- `./data` -> `/app/data`
- `./sessions` -> `/app/sessions`

## 10. 日志排查

关键 logger：

- `tg_forward_bot.handlers`: 命令与私聊解析入口
- `tg_forward_bot.link_parser`: 链接解析与 discussion 解析
- `tg_forward_bot.forwarder`: 策略执行与降级
- `tg_forward_bot.syncer`: 历史同步进度
- `tg_forward_bot.monitor`: 实时监控事件

更多运行细节见 `docs/operations.md`。

