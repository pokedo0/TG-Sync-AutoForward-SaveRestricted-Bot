# TG-Sync-AutoForward-SaveRestricted-Bot

[English README](./README.md)

基于 Telethon 的 Telegram 消息搬运工具，采用 `Bot + UserBot` 双客户端架构，支持私聊链接解析、历史同步、实时监控和多级策略降级转发。

### 1. 功能概览

- 私聊贴链接解析：公开/私有/评论区/话题链接
- `/sync`：历史消息同步（支持频道、群组、群组话题）
- `/monitor`：实时消息监控转发（支持频道、群组、群组话题）
- 同步/转发内置“硬封禁”自动过滤：
  仅过滤全平台受限（`platform=all`，典型表现为 `can't be displayed`）的频道/群组或消息；
  仅限部分平台（如 iOS/Android）的限制不会被过滤，仍会保留转发
- 统一降级策略：优先低成本，失败自动兜底
- 支持 `copy`（默认）与 `forward` 模式
- 支持 `?comment=<id>` 与 `?single`
- 全部策略失败后发送 `#fail2forward`

### 2. 架构与职责

- `Bot`：命令交互、目标发送（写）
- `UserBot`：受限来源读取、下载媒体（读）
- 设计原则：`UserBot 读，Bot 写`，仅在需要时让 UserBot 参与写入

### 3. 项目结构

```text
TG-Sync-AutoForward-SaveRestricted-Bot/
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

### 4. 环境要求

- Python 3.11+
- Telegram `api_id` / `api_hash`
- Bot Token
- 一个 UserBot 账号（手机号登录）

### 5. 快速启动

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

首次启动会要求 UserBot 验证码登录，成功后会在 `sessions/` 目录保存会话。

### 6. 命令说明

- `/start`：启动说明
- `/help`：帮助
- `/sync <链接> [--forward]`：同步历史消息到当前群/频道（支持频道、群组、群组话题）
- `/monitor <链接> [--forward]`：监控新消息并转发到当前群/频道（支持频道、群组、群组话题）
- `/list`：任务管理（暂停/恢复/删除/清空）
- `/settings`：查看限流配置

私聊直接发送 `t.me/...` 链接会触发解析与转发。

### 7. 转发策略（重点）

程序执行时固定按 1 -> 2 -> 3 -> 4 尝试：

1. 策略1：`Bot` 直接处理（最优先）
2. 策略2：`UserBot` 读取后处理
3. 策略3：`UserBot` 下载后由 `Bot` 重新上传
4. 策略4：发送失败标记 `#fail2forward`

结合部署与来源类型，建议按以下优先级理解：

- 优先级A（策略1命中率最高）：
  Bot 在目标群组/频道可正常发消息，且为管理员（无论私密或公开目标）。
- 优先级B（通常仍可优先策略1）：
  来源为公开频道/公开群组时，Bot 往往可直接读取来源并处理。
- 优先级C（常降到策略3）：
  来源是私密评论区 discussion 群，或源聊天开启了“禁止转发/保护内容（Protected Content）”。

说明：

- 日志里的“策略1成功”表示第一层尝试成功，不等价于一定使用原生 `forward`；
  在 `copy` 模式下可能是 `send_message/send_file`。
- 评论链接 `?comment=` 实际消息位于 discussion 群，主贴公开不代表评论区也公开。

### 8. 其他注意点

- `forward` 模式更接近原始转发；`copy` 模式更稳定但更容易触发发送限流。
- 私有来源必须确保 UserBot 已加入并可读，否则策略2/3都会失败。
- Topic/论坛群转发依赖 `target_topic_id`，目标侧权限不足会导致发送失败。
- 硬封禁采用并集判定：只要 chat 级或 message 级出现 `restriction_reason.platform=all`，即视为已封禁并过滤。
  仅在部分平台生效的限制不视为硬封禁，仍正常转发。
- 相册会优先整组发送，失败后才降级为逐条转发。
- 出现大量 `FloodWait` 时，建议调大 `rate_limit` 相关参数。

### 9. Docker 运行

```bash
docker compose up -d --build
```

挂载目录：

- `./config.yaml` -> `/app/config.yaml`
- `./data` -> `/app/data`
- `./sessions` -> `/app/sessions`

### 10. 日志与排障

关键 logger：

- `tg_forward_bot.handlers`：命令与私聊解析入口
- `tg_forward_bot.link_parser`：链接解析与 discussion 解析
- `tg_forward_bot.forwarder`：策略执行与降级
- `tg_forward_bot.syncer`：历史同步进度
- `tg_forward_bot.monitor`：实时监控事件

更多细节见 `docs/operations.md` 与 `docs/architecture.md`。
