# TG Forward Bot

[English](./README.md)

基于 Telethon 的 Telegram 消息转发工具，采用 **Bot + UserBot** 双客户端架构，支持私聊链接解析、历史消息同步、实时监控转发和多级策略自动降级。

## 功能特性

- **链接解析** — 私聊发送 `t.me/...` 链接即可触发，支持公开/私有频道、群组、评论区、话题
- **历史同步** — `/sync` 拉取历史消息，支持断点续传；受限消息自动通过 Takeout 内联转发
- **受限消息补发** — `/syncrestrictedmsg` 通过 Telegram Takeout 导出接口，仅补发被平台限制的消息
- **实时监控** — `/monitor` 监听新消息并自动转发
- **多级降级** — Bot 直发 → UserBot 辅助 → 下载重传 → 失败标记 `#fail2forward`
- **相册感知** — 优先整组发送，失败后逐条降级
- **硬封禁过滤** — 自动跳过全平台受限（`platform=all`）的内容；仅限部分平台的限制不受影响
- **双模式** — 支持 `copy`（默认）和 `forward` 两种转发模式

## 架构

| 角色 | 职责 |
|------|------|
| **Bot** | 命令交互、目标发送（写） |
| **UserBot** | 受限来源读取、媒体下载（读） |

设计原则：**UserBot 读，Bot 写**，仅在必要时让 UserBot 参与写入。

## 环境要求

- Python 3.11+
- Telegram `api_id` / `api_hash`（[my.telegram.org](https://my.telegram.org)）
- Bot Token（[@BotFather](https://t.me/BotFather)）
- UserBot 账号（手机号登录）

## 快速开始

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 编辑填入你的配置
python main.py
```

> 首次启动需要 UserBot 验证码登录，会话文件保存在 `sessions/` 目录。

## 命令

| 命令 | 说明 |
|------|------|
| `/sync <链接> [--forward]` | 同步历史消息到当前会话；受限消息通过 Takeout 内联转发 |
| `/syncrestrictedmsg <链接>` | 通过 Takeout 导出接口仅补发受限消息到当前会话 |
| `/monitor <链接> [--forward]` | 监控新消息并转发到当前会话（需 UserBot 已加入源） |
| `/list` | 任务管理：暂停 / 恢复 / 删除 / 清空 |
| `/settings` | 查看限流配置 |
| `/start` · `/help` | 启动说明与帮助 |

私聊直接发送 `t.me/...` 链接即可触发解析与转发。

## 来源访问要求

| 来源类型 | `/sync` 对 UserBot 的要求 | `/monitor` 对 UserBot 的要求 | `/syncrestrictedmsg` 对 UserBot 的要求 |
|----------|--------------------------|------------------------------|----------------------------------------|
| 公开频道 / 群组 | 通常无需加入 | **必须加入** | 必须加入（Takeout 要求） |
| 私密频道 / 群组 | 必须已加入且可读 | **必须已加入且可读** | 必须已加入且可读 |
| 群组话题（Forum） | 需能读取该群 | **必须加入** | 需能读取该群 |

- `/monitor` 创建任务前会校验 UserBot 是否已加入并可访问源，不满足会拒绝创建。
- `/syncrestrictedmsg` 使用 Telegram Takeout 导出接口，要求 UserBot 已加入源。先扫描全部消息识别受限内容，再通过 Takeout 会话批量补发。
- Bot 始终负责目标侧发送；来源侧读取能力取决于来源是否公开。

## 转发策略

运行时固定按以下顺序尝试，失败自动降级：

1. **Bot 直接处理** — 成本最低，Bot 可读源时优先命中
2. **UserBot 读取 + Bot 处理** — 源受限时由 UserBot 辅助读取
3. **UserBot 下载 + Bot 重新上传** — 源禁止转发或内容受保护时的兜底
4. **失败标记** — 全部失败后发送 `#fail2forward`

> **提示**：日志中的"策略1成功"表示第一层尝试成功，`copy` 模式下可能实际使用的是 `send_message` / `send_file`。评论链接 `?comment=` 的消息位于 discussion 群，主贴公开不代表评论区也公开。

## Docker 部署

```bash
docker compose up -d --build
```

挂载目录：

| 宿主机 | 容器 |
|--------|------|
| `./config.yaml` | `/app/config.yaml` |
| `./data` | `/app/data` |
| `./sessions` | `/app/sessions` |

## 注意事项

- `forward` 模式保留原始转发语义；`copy` 模式兼容性更好但更容易触发限流
- 私有来源必须确保 UserBot 已加入并可读，否则策略 2/3 均会失败
- 话题转发依赖 `target_topic_id`，目标侧权限不足会导致发送失败
- 频繁出现 `FloodWait` 时，建议调大 `rate_limit` 相关参数
- 相册优先整组发送，失败后降级为逐条转发

## 日志与排障

| Logger | 用途 |
|--------|------|
| `tg_forward_bot.handlers` | 命令与私聊解析入口 |
| `tg_forward_bot.link_parser` | 链接解析与 discussion 解析 |
| `tg_forward_bot.forwarder` | 策略执行与降级 |
| `tg_forward_bot.syncer` | 历史同步进度 |
| `tg_forward_bot.restricted_syncer` | 受限消息 Takeout 同步 |
| `tg_forward_bot.monitor` | 实时监控事件 |

详细运维文档见 [`docs/operations.md`](docs/operations.md) 与 [`docs/architecture.md`](docs/architecture.md)。
