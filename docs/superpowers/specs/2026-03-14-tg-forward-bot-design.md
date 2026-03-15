# Telegram 转发 Bot 设计文档

## 概述

基于 Telethon 的 Telegram 消息转发工具，结合 Bot + UserBot 双客户端架构，实现频道/群组内容的历史同步、实时监控转发、受限链接解析等功能。核心目的：备份内容以防源频道/群组被封禁后内容丢失。

## 技术栈

- **语言**: Python 3.11+
- **核心库**: Telethon（同时驱动 Bot 和 UserBot）
- **数据库**: SQLite（通过 aiosqlite 异步访问）
- **部署**: 开发直接运行，生产 Docker
- **配置**: YAML 文件

## 架构设计

### 双客户端模型

两个 Telethon 客户端运行在同一个 asyncio 事件循环中：

- **Bot Client**（Bot Token 登录）：接收用户命令、发送反馈、执行转发"写"操作
- **UserBot Client**（个人账号 session 登录）：访问私有/受限内容、执行"读"操作

核心原则：**UserBot 负责读，Bot 负责写**，最大限度降低 UserBot 封号风险。

### 智能降级转发策略

按优先级依次尝试，选择成本最低的方式：

| 场景 | 操作方式 |
|---|---|
| 公开源 + 无转发保护 | Bot 直接 forward/copy |
| 私有源 + 无转发保护 | UserBot 读 → Bot forward/copy |
| 任何源 + 有转发保护 | UserBot 下载 → Bot 重新上传 |
| 无法处理的内容 | 发送源链接 + `#fail2forward` 标记 |

Bot 只需加入目标群组/频道。公开源不需要任何客户端加入，私有源需要 UserBot（通常已在其中）。

## 核心功能模块

### 1. 链接解析器（Link Resolver）

私聊 Bot 直接发送链接，自动解析并返回内容。支持格式：
- `https://t.me/channel/123` — 公开频道消息
- `https://t.me/c/123456/789` — 私有频道消息
- `https://t.me/channel/123?comment=456` — 评论区消息
- `https://t.me/c/xxx/topic_id/msg_id?single` — 话题消息

自动识别链接中的 topic、comment 等参数，用户无需手动指定。

### 2. 历史同步（History Sync）

在目标群中输入 `/sync <链接>`，Bot 解析链接自动同步源的全部历史内容到当前群。

- 智能链接解析：自动识别频道/群组/话题
- 分批处理，断点续传（进度存 SQLite）
- 可配置限流参数
- 定期发送进度消息（每 100 条更新一次）

### 3. 实时监控转发（Monitor & Forward）

在目标群中输入 `/monitor <链接>`，Bot 注册事件监听，实时转发源的新消息。

- 默认 copy 模式（去署名），可用 `--forward` 保留署名
- 事件驱动，不主动拉取，限流压力小
- 多任务共享全局速率限制器

### 4. 权限管理

- 配置文件中设置 `admin_ids` 列表
- 只有 admin 可执行 /sync、/monitor 等管理命令
- 普通用户可使用链接解析功能（可配置）

## 命令体系

| 命令 | 使用场景 | 说明 |
|---|---|---|
| `/start` | 私聊 | 欢迎信息 + 功能菜单 |
| `/help` | 任意 | 命令帮助 |
| `/sync <链接>` | 目标群中 | 同步源历史内容到当前群 |
| `/monitor <链接>` | 目标群中 | 监控源新内容转发到当前群 |
| `/monitor <链接> --forward` | 目标群中 | 保留署名转发 |
| `/list` | 任意 | 查看并管理所有任务（暂停/恢复/删除） |
| `/settings` | 任意 | 查看限流配置 |
| 直接发链接 | 私聊 | 自动解析并返回消息内容 |

## 限流与防封策略

### 可配置限流参数（config.yaml）

```yaml
rate_limit:
  batch_size: 100            # 每批获取消息数
  forward_interval: [2, 5]   # 每条转发间隔范围（秒）
  batch_pause_every: 50      # 每转发N条后休息
  batch_pause_time: [30, 60] # 休息时间范围（秒）
  flood_wait_multiplier: 2   # FloodWait后间隔翻倍系数
  max_flood_wait: 300        # 最大等待时间（秒），超过则暂停并通知
```

### FloodWaitError 处理

- 捕获后自动 `asyncio.sleep(error.seconds)` 并重试
- 后续间隔按 `flood_wait_multiplier` 翻倍（退避策略）
- 超过 `max_flood_wait` 则暂停任务并通知管理员

### 防封措施

- UserBot 尽量只做读操作
- session 文件持久化，避免频繁重新登录
- 避免短时间内加入大量群组

## 数据模型（SQLite）

### tasks 表

```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,              -- 'sync' | 'monitor'
    source_chat_id INTEGER NOT NULL,
    source_topic_id INTEGER,
    target_chat_id INTEGER NOT NULL,
    target_topic_id INTEGER,
    mode TEXT DEFAULT 'copy',        -- 'forward' | 'copy'
    status TEXT DEFAULT 'running',   -- 'running' | 'paused' | 'completed' | 'failed'
    last_synced_msg_id INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### message_map 表

```sql
CREATE TABLE message_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    source_msg_id INTEGER NOT NULL,
    target_msg_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 项目结构

```
tg_forward_bot/
├── config.yaml              # 配置文件
├── main.py                  # 入口
├── bot/
│   ├── handlers.py          # Bot 命令处理
│   └── link_parser.py       # Telegram 链接解析器
├── core/
│   ├── forwarder.py         # 转发引擎（智能降级）
│   ├── message_logic.py     # 消息分类与相册分组规则（共享）
│   ├── syncer.py            # 历史同步（分批、断点续传）
│   ├── monitor.py           # 实时监控（事件监听）
│   └── rate_limiter.py      # 全局速率限制器
├── db/
│   ├── database.py          # SQLite 连接管理
│   └── models.py            # 数据模型
├── Dockerfile
├── docker-compose.yaml
└── requirements.txt
```
