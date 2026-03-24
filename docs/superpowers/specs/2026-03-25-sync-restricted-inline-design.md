# /sync 内联 Takeout 转发受限消息 + 日志统一 + 代码复用

日期: 2026-03-25

## 背景

- `/sync`（Syncer）同步全量历史，遇到受限消息只统计并跳过
- `/syncrestrictedmsg`（RestrictedSyncer）通过 Takeout 补发受限消息
- 现在 RestrictedSyncer 已能成功同步受限消息，`/sync` 应跟着支持
- Syncer 和 RestrictedSyncer 存在代码重复（`_is_general_topic_msg`、`_notify`、`_cancel_flags`）
- 两个命令都缺少每条消息的转发日志

## 设计决策

| 决策点 | 选择 |
|--------|------|
| 受限消息处理方式 | Inline Takeout（遇到即转发，保持目标消息顺序） |
| Takeout 会话生命周期 | 懒加载（首次遇到受限消息时开启，同步结束时关闭） |
| 日志方案 | Syncer 层统一打印，格式参考 Forwarder 风格，抽取为基类方法 |
| 代码复用 | RestrictedSyncer 继承 ForwardingComponent |

## 约束

- `/sync` 和 `/syncrestrictedmsg` 的命令接口、参数、功能完全不受影响
- `/syncrestrictedmsg` 的 start_sync 逻辑不变，仅新增每条消息日志
- `/monitor` 和 Forwarder 内部逻辑不动

## Section 1：基类重构 & 代码复用

### ForwardingComponent 基类扩展（`core/base_component.py`）

新增成员：

- `_cancel_flags: dict[int, bool]`：任务取消标记（原 Syncer/RestrictedSyncer 各有一份）
- `cancel(task_id)`：设置取消标记
- `_notify(notify_chat_id, notify_topic_id, notify_reply_to_msg_id, text)`：发送通知消息
- `_is_general_topic_msg(msg)` 静态方法：判断消息是否属于 General 话题
- `_log_forward_result(...)` 静态方法：统一日志格式

### RestrictedSyncer 改为继承 ForwardingComponent

- 删除 `__init__` 中手动持有的 `bot/userbot/db/config`
- 删除 `self.rl = RateLimiter(config)`（基类已提供）
- 删除重复的 `_is_general_topic_msg`
- 删除 `_cancel_flags` 和 `cancel()`
- 删除 `_notify` 闭包，改用基类 `self._notify()`
- 保留 `_copy_single`/`_copy_album`（Takeout 专用静态方法）
- 保留 `_collect_restricted_ids`、`_forward_restricted_batch`、`start_sync`

### Syncer 简化

- 删除自己的 `_is_general_topic_msg`、`_cancel_flags`、`cancel()`，改用基类

影响：纯重构，行为完全不变。

## Section 2：Syncer 内联 Takeout 转发受限消息

### Takeout 懒加载

- `start_sync` 新增 `takeout` 变量，初始 `None`
- 遇到第一个受限单元时调用 `self.userbot.takeout()` 开启
- 手动 `__aenter__`/`__aexit__` 管理（开启时机不确定，不用 `async with`）
- 同步结束时在 `finally` 块中关闭

### 新增 `_forward_restricted_unit` 方法

```python
async def _forward_restricted_unit(
    self, takeout, task_id, source_chat_id,
    target_chat_id, target_topic_id,
    unit_kind, source_ids
) -> tuple[int, int, bool]:
```

流程：
1. 用 takeout `get_messages(real_chat_id, ids=source_ids)` 拉取消息
2. `real_chat_id` 转换逻辑复用 RestrictedSyncer 已有的（带 `-100` 前缀）
3. 调用 `RestrictedSyncer._copy_single` / `RestrictedSyncer._copy_album`（静态方法）
4. 保存 `message_map`，更新 `last_synced_msg_id`
5. 返回 `(msg_count, last_msg_id, success)` 与 `_forward_unit` 一致

### start_sync 遍历逻辑变更

原来：
```python
if restricted:
    # 跳过，只更新 last_synced_msg_id
    continue
```

改为：
```python
if restricted:
    if takeout is None:
        takeout = await self._open_takeout()
    msg_count, last_msg_id, success = await self._forward_restricted_unit(
        takeout, task_id, source_chat_id, ...)
    # 统计、更新进度
```

### Takeout 异常处理

- Takeout 开启失败：该受限单元降级为跳过，记录失败，继续同步
- Takeout 转发单条失败：记录失败，继续下一个单元

### 完成通知调整

原来：`跳过受限: X 条`
改为：`受限(Takeout): X 条成功, Y 条失败`

## Section 3：每条消息同步日志

### 统一日志方法（基类）

```python
@staticmethod
def _log_forward_result(logger, task_type, task_id, unit_kind, source_ids,
                         target_ids, restricted=False):
```

### 日志格式

普通单条：
```
sync #1 msg=123 转发成功 -> target_msg=456
sync #1 msg=123 转发失败
```

普通相册：
```
sync #1 album=[101,102,103] 转发成功 -> target_msgs=[201,202,203]
sync #1 album=[101,102,103] 转发失败
```

受限单条：
```
sync #1 msg=123 [受限:Takeout] 转发成功 -> target_msg=456
```

受限相册：
```
sync #1 album=[101,102,103] [受限:Takeout] 转发成功 -> target_msgs=[201,202,203]
```

`/syncrestrictedmsg` 前缀用 `restricted_sync`：
```
restricted_sync #2 msg=55 [受限:Takeout] 转发成功 -> target_msg=88
```

### 调用位置

- Syncer: `_forward_unit` 返回后、`_forward_restricted_unit` 返回后
- RestrictedSyncer: `_forward_restricted_batch` 中每个单元转发后

## Section 4：文件变更总览

| 文件 | 变更 |
|------|------|
| `core/base_component.py` | 新增 `_notify`、`_is_general_topic_msg`、`_cancel_flags`、`cancel()`、`_log_forward_result` |
| `core/syncer.py` | 删除重复方法；新增 `_forward_restricted_unit`；takeout 懒加载；每条消息日志 |
| `core/restricted_syncer.py` | 继承 ForwardingComponent；删除重复方法；转发循环加日志 |
| `bot/handlers.py` | 无变更 |

### 不变的行为

- `/sync` 命令接口、参数不变
- `/syncrestrictedmsg` 命令完全不变（仅多日志）
- `/monitor` 不受影响
- Forwarder 内部逻辑和日志不动
- 断点续传机制不变
- `message_map` 保存逻辑不变
