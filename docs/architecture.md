# Architecture

## 核心目标

在“可访问性不稳定（私有源、禁止转发、频道封禁）”的前提下，尽量低成本且可恢复地完成消息搬运。

## 双客户端职责

- `Bot Client`: 指令入口、任务反馈、目标发送
- `UserBot Client`: 受限读取、媒体下载、讨论群访问

设计原则：`UserBot 读，Bot 写`。只有在策略需要时才让 UserBot 参与写操作，降低风险面。

## 模块边界

- `bot/handlers.py`
  - 命令注册
  - 权限检查
  - 私聊链接解析入口
- `bot/link_parser.py`
  - 解析公开/私有链接、话题、评论参数
- `core/message_logic.py`
  - 消息分类（text/media/album/empty）
  - 相册候选判断与分组单元构建
- `core/forwarder.py`
  - 单条与相册的策略降级执行
- `core/syncer.py`
  - 历史消息拉取、断点续传、任务进度
- `core/monitor.py`
  - 实时事件监听、相册缓冲聚合
- `core/rate_limiter.py`
  - 全局节流与 FloodWait 退避
- `db/models.py`
  - 任务和消息映射持久化

## 统一消息判定

`private link`、`/sync`、`/monitor` 共享 `core/message_logic.py` 的规则，避免三处逻辑漂移：

- 是否可作为文件媒体发送
- 是否视为相册
- 历史消息如何分组为单元

## 策略链路

单条 `forward_message` 与相册 `forward_album` 都遵循：

1. Bot 直接处理
2. UserBot 读取 + Bot 处理
3. UserBot 下载 + Bot 上传
4. 发失败标记

## 数据模型

- `tasks`: 任务定义、状态、断点
- `message_map`: 源消息与目标消息映射

`sync` 依赖 `last_synced_msg_id` 实现断点续传。

