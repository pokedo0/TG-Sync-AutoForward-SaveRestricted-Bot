# TG Forward Bot

[English](./README.md)

基于 Telethon 的 Telegram 消息转发工具，采用 **Bot + UserBot** 双客户端架构，支持私聊链接解析、历史消息同步、实时监控转发和多级策略自动降级。

## 功能特性

- **链接解析** — 私聊发送 `t.me/...` 链接即可触发，支持公开/私有频道、群组、评论区、话题（可通过 `append_source_link` 附带 `[Source Link](url)` 来源链接）
- **历史同步** — `/sync` 拉取历史消息，支持断点续传；受限消息自动通过 Takeout 内联转发
- **受限消息导出** — `/syncrestrictedmsg` 批量补发受限消息，`/takeout` 通过 Telegram Takeout 接口解析导出单条消息/相册
- **实时监控** — `/monitor` 监听新消息并自动转发
- **高速传输加速** — 支持 FastTelethon 多连接并发下载/上传与 `cryptg`（C/Rust AES）加速
- **多级降级** — Bot 直发 → UserBot 辅助 → 下载重传 → 失败标记 `#fail2forward`
- **相册感知** — 优先整组发送，失败后逐条降级
- **硬封禁过滤** — 自动跳过全平台受限（`platform=all`）的内容；仅限部分平台的限制不受影响
- **双模式** — 支持 `copy`（默认）和 `forward` 两种转发模式

## 架构

| 角色        | 职责                         |
| ----------- | ---------------------------- |
| **Bot**     | 命令交互、目标发送（写）     |
| **UserBot** | 受限来源读取、媒体下载（读） |

设计原则：**UserBot 读，Bot 写**，仅在必要时让 UserBot 参与写入。

## 环境要求

- Python 3.11+
- Telegram `api_id` / `api_hash`（[my.telegram.org](https://my.telegram.org)）
- Bot Token（[@BotFather](https://t.me/BotFather)）
- 一个或多个 UserBot 账号（手机号登录）

## 快速开始

### 1. 安装与配置

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 编辑填入你的配置 (api_id, api_hash, bot_token 及 phone/phones)
```

### 2. UserBot 交互式登录与会话初始化

首次启动前，需通过交互脚本完成 UserBot 登录（支持多账号，按顺序依次登录）：

* **自适应脚本（推荐，Linux / VPS）**：
  ```bash
  bash scripts/login_userbot.sh -p +8613800138000
  ```
* **Python 直接运行**：
  ```bash
  python scripts/login_userbot.py -p +8613800138000
  ```
* **Docker 交互运行**：
  ```bash
  docker compose run --rm bot python scripts/login_userbot.py -p +8613800138000
  ```

> **说明**：
> 1. 终端提示输入短信验证码及 2FA 密码后，会话凭证将持久化在 `sessions/` 目录。
> 2. 若配置了多个 UserBot（在 `config.yaml` 的 `phones` 列表中），请针对每个号码分别运行一次登录。
> 3. `.session` 文件跨平台通用，亦可在本地电脑登录生成后直接复制到服务器 `sessions/` 目录。

### 3. 启动主服务

- **直接运行**：
  ```bash
  python main.py
  ```
  *（Windows 也可直接运行 `start.bat`。）*
- **Linux Systemd 一键安装**（注册服务名 `forward-bot`）：
  ```bash
  sudo bash install.sh
  ```
  常用服务管理命令：
  ```bash
  systemctl status forward-bot     # 查看服务状态
  journalctl -u forward-bot -f     # 实时查看运行日志
  systemctl stop forward-bot       # 停止服务
  systemctl restart forward-bot    # 重启服务
  ```

## 核心配置说明（`config.yaml`）

完整注释见 [`config.example.yaml`](config.example.yaml)：

- **多 UserBot（`phones`）**：按顺序配置多个手机号（向下兼容单个 `phone`）。遇到私有群/频道时按从上到下的顺序依次探测可读性；探测与解析前会自动重连已掉线或半开异常的客户端。
- **传输加速（`transfer`）**：控制 FastTelethon 并发连接数（`fast_download_connections`、`fast_upload_connections`、`fast_transfer_min_size_mb`、`album_download_concurrency`、`upload_part_size_kb`、`download_part_size_kb`），并配合 `cryptg` 加速加解密。
- **链接解析开关**：`allow_public_resolve` 控制是否允许非管理员私聊解析链接；`append_source_link` 控制是否在解析返回消息末尾追加 `[Source Link](url)`。

## 命令

| 命令                          | 说明                                                      |
| ----------------------------- | --------------------------------------------------------- |
| `/sync <链接> [--forward]`    | 同步历史消息到当前会话；受限消息通过 Takeout 内联转发     |
| `/syncrestrictedmsg <链接>`   | 通过 Takeout 导出接口仅补发受限消息到当前会话             |
| `/takeout <链接>`             | 通过 Takeout 导出接口解析单条受限消息/相册（支持回复消息）|
| `/monitor <链接> [--forward]` | 监控新消息并转发到当前会话（需 UserBot 已加入源）         |
| `/list`                       | 任务管理：暂停 / 恢复 / 删除 / 清空                       |
| `/settings`                   | 查看限流配置                                              |
| `/start` · `/help`            | 启动说明与帮助                                            |

私聊直接发送 `t.me/...` 链接即可触发解析与转发。

## 来源访问要求

| 来源类型          | `/sync` 对 UserBot 的要求 | `/monitor` 对 UserBot 的要求 | `/syncrestrictedmsg` 与 `/takeout` 对 UserBot 的要求 |
| ----------------- | ------------------------- | ---------------------------- | ---------------------------------------------------- |
| 公开频道 / 群组   | 通常无需加入              | **必须加入**                 | 必须加入（Takeout 要求）                             |
| 私密频道 / 群组   | 必须已加入且可读          | **必须已加入且可读**         | 必须已加入且可读                                     |
| 群组话题（Forum） | 需能读取该群              | **必须加入**                 | 需能读取该群                                         |

- `/monitor` 创建任务前会校验 UserBot 是否已加入并可访问源，不满足会拒绝创建。
- `/syncrestrictedmsg` 与 `/takeout` 使用 Telegram Takeout 导出接口，要求 UserBot 已加入源；其中 `/syncrestrictedmsg` 会先扫描全部消息识别受限内容，再通过 Takeout 会话批量补发。
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
# 1. 复制并编辑配置文件
cp config.example.yaml config.yaml

# 2. 交互式初始化 UserBot 登录（首次必做）
docker compose run --rm bot python scripts/login_userbot.py -p +8613800138000

# 3. 后台构建并启动主服务
docker compose up -d --build

# 4. 查看日志 / 停止服务
docker compose logs -f bot
docker compose stop
```

挂载目录：

| 宿主机          | 容器               |
| --------------- | ------------------ |
| `./config.yaml` | `/app/config.yaml` |
| `./data`        | `/app/data`        |
| `./sessions`    | `/app/sessions`    |

## 注意事项

- **受限消息由 UserBot 身份发出**：Takeout 获取的媒体引用绑定 UserBot，Bot 无法直接使用.
- `forward` 模式保留原始转发语义；`copy` 模式兼容性更好但更容易触发限流
- 私有来源必须确保 UserBot 已加入并可读，否则策略 2/3 均会失败
- 话题转发依赖 `target_topic_id`，目标侧权限不足会导致发送失败
- 频繁出现 `FloodWait` 时，建议调大 `rate_limit` 相关参数
- 相册优先整组发送，失败后降级为逐条转发

## 日志与排障

日志默认输出到标准输出/控制台（systemd 部署可用 `journalctl -u forward-bot -f`，Docker 部署可用 `docker compose logs -f bot`）。

| Logger                             | 用途                                    |
| ---------------------------------- | --------------------------------------- |
| `tg_forward_bot.handlers`          | 命令与私聊解析入口                      |
| `tg_forward_bot.userbot_manager`   | 多 UserBot 探测、路由与按需自动重连     |
| `tg_forward_bot.link_parser`       | 链接解析与 discussion 解析              |
| `tg_forward_bot.forwarder`         | 策略执行与降级                          |
| `tg_forward_bot.media_transfer`    | 媒体下载/上传调度与降级                 |
| `tg_forward_bot.fast_telethon`     | FastTelethon 多连接并发传输             |
| `tg_forward_bot.syncer`            | 历史同步进度                            |
| `tg_forward_bot.restricted_syncer` | 受限消息 Takeout 同步                   |
| `tg_forward_bot.monitor`           | 实时监控事件                            |

详细运维文档见 [`docs/operations.md`](docs/operations.md) 与 [`docs/architecture.md`](docs/architecture.md)。
