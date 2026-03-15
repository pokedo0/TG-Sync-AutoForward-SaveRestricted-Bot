# Operations Guide

## 1. 常见流程

### 私聊解析

1. 用户私聊发送 `t.me/...` 链接
2. 解析链接得到来源 chat/msg
3. 若为评论链接，先解析 discussion 群
4. 按统一消息类型策略转发到私聊

### /sync

1. 解析源链接
2. 从 `last_synced_msg_id` 继续拉取历史
3. 构建单条/相册单元并转发
4. 持续写入 `message_map` 与进度

### /monitor

1. 解析源链接并注册事件监听
2. 新消息到达后即时处理
3. 对同 `grouped_id` 进行短暂缓冲并合并发送

## 2. 日志解读

- `策略1(Bot直接)成功`
  - `forward` 模式通常是 `forward_messages`
  - `copy` 模式通常是 `send_message/send_file`
- `策略1相册: Bot 无法获取消息`
  - Bot 对来源不可见（常见于私有 discussion 群）
- `策略2相册: ... media object is invalid`
  - media 句柄不可跨账号直接复用，通常降级到策略3
- `策略3(下载+上传)成功`
  - 读取来源成功，最终通过重新上传发出

## 3. 常见问题

### 为什么同频道主贴和评论区策略不同？

主贴在公开频道，Bot 可能能直接处理。评论区实际在 discussion 群，若该群私有，Bot 往往无法直接读，需降级。

### 为什么会 FloodWait？

`copy` 模式本质是发送消息/媒体请求，不是“零成本转发”。大量媒体会触发 Telegram 限流。

建议：

- 增大 `forward_interval`
- 调大 `batch_pause_every`
- 合理设置 `batch_pause_time`

## 4. 安全建议

- UserBot 只做读取为主
- 不要让 UserBot 频繁加入/退出大量群组
- 会话文件 `sessions/` 做持久化并保护权限
- 生产环境优先使用最小权限账号

