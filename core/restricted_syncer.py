"""受限消息同步：通过 Takeout Session 重新拉取被平台封禁的消息并转发。

与 Syncer 的区别：
- Syncer: 同步全量历史，跳过受限消息
- RestrictedSyncer: 仅补发那些被 Syncer 跳过的受限消息，利用 Takeout 绕过限制
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable

from telethon import TelegramClient, errors
from telethon.tl.types import Message, MessageService

from core.message_logic import (
    build_forward_units, is_file_media, is_restricted_message,
)
from core.rate_limiter import RateLimiter
from db.database import Database
from db import models

logger = logging.getLogger("tg_forward_bot.restricted_syncer")


class RestrictedSyncer:
    def __init__(self, bot: TelegramClient, userbot: TelegramClient,
                 db: Database, config: dict):
        self.bot = bot
        self.userbot = userbot
        self.db = db
        self.config = config
        self.rl = RateLimiter(config)
        self._cancel_flags: dict[int, bool] = {}

    # ------------------------------------------------------------------
    # Takeout 转发核心
    # ------------------------------------------------------------------

    @staticmethod
    async def _copy_single(takeout: TelegramClient, msg: Message,
                           target_chat_id: int, topic_id: int | None) -> int | None:
        """通过 Takeout 会话复制单条消息到目标群。"""
        reply_to = topic_id if topic_id else None
        if is_file_media(msg):
            result = await takeout.send_file(
                target_chat_id, msg.media,
                caption=msg.text or "",
                formatting_entities=msg.entities,
                reply_to=reply_to,
            )
        elif msg.text:
            result = await takeout.send_message(
                target_chat_id, msg.text,
                formatting_entities=msg.entities,
                reply_to=reply_to,
            )
        else:
            return None
        return result.id if result else None

    @staticmethod
    async def _copy_album(takeout: TelegramClient, msgs: list[Message],
                          target_chat_id: int, topic_id: int | None) -> list[int]:
        """通过 Takeout 会话以相册形式复制多条媒体消息。"""
        reply_to = topic_id if topic_id else None
        media_msgs = [m for m in msgs if is_file_media(m)]
        if not media_msgs:
            return []
        if len(media_msgs) == 1:
            m = media_msgs[0]
            result = await takeout.send_file(
                target_chat_id, m.media,
                caption=m.text or "",
                formatting_entities=m.entities,
                reply_to=reply_to,
            )
            return [result.id] if result else []
        files = [m.media for m in media_msgs]
        captions = [m.text or "" for m in media_msgs]
        result = await takeout.send_file(
            target_chat_id, files,
            caption=captions,
            reply_to=reply_to,
        )
        if not result:
            return []
        if isinstance(result, list):
            return [m.id for m in result if m]
        return [result.id] if getattr(result, "id", None) else []

    # ------------------------------------------------------------------
    # 消息收集阶段（普通 userbot 拉历史，筛选受限消息 ID）
    # ------------------------------------------------------------------

    async def _collect_restricted_ids(
        self,
        task_id: int,
        source_chat_id: int,
        source_topic_id: int | None,
        notify: Callable[[str], Awaitable[None]],
    ) -> tuple[list[int], int] | tuple[None, int]:
        """扫描源的全部历史消息，收集被判定为受限的消息 ID 列表。

        返回 (restricted_ids, total_scanned)。失败时 restricted_ids 为 None。
        """
        restricted_ids: list[int] = []
        total_scanned = 0
        iter_reply_to = None if source_topic_id == 1 else source_topic_id

        try:
            async for msg in self.userbot.iter_messages(
                source_chat_id,
                reverse=True,
                reply_to=iter_reply_to,
            ):
                if self._cancel_flags.get(task_id):
                    break
                if isinstance(msg, MessageService):
                    continue
                if source_topic_id == 1 and not self._is_general_topic_msg(msg):
                    continue
                total_scanned += 1
                if is_restricted_message(msg):
                    restricted_ids.append(msg.id)
        except (errors.ChannelPrivateError, errors.ChatAdminRequiredError) as e:
            logger.error("受限同步任务 #%s 源不可访问: %s", task_id, type(e).__name__)
            await notify(f"❌ 受限同步 #{task_id} 失败: 源不可访问")
            return None, total_scanned
        except errors.FloodWaitError as e:
            logger.warning("受限同步任务 #%s 扫描时 FloodWait %ds", task_id, e.seconds)
            await asyncio.sleep(e.seconds)
            return [], total_scanned
        except Exception as e:
            logger.error("受限同步任务 #%s 扫描异常: %s", task_id, e)
            await notify(f"❌ 受限同步 #{task_id} 扫描失败: {e}")
            return None, total_scanned
        return restricted_ids, total_scanned

    @staticmethod
    def _is_general_topic_msg(msg) -> bool:
        """判断消息是否属于 General 话题。复用 Syncer 同款逻辑。"""
        reply_to = getattr(msg, "reply_to", None)
        if not reply_to:
            return True
        top_id = getattr(reply_to, "reply_to_top_id", None)
        if top_id:
            return top_id == 1
        if getattr(reply_to, "forum_topic", False):
            return getattr(reply_to, "reply_to_msg_id", None) in (1, None)
        return True

    # ------------------------------------------------------------------
    # Takeout 阶段：用 Takeout 重新拉取并转发
    # ------------------------------------------------------------------

    async def _forward_restricted_batch(
        self,
        task_id: int,
        takeout: TelegramClient,
        source_chat_id: int,
        target_chat_id: int,
        target_topic_id: int | None,
        msg_ids: list[int],
        notify: Callable[[str], Awaitable[None]],
    ) -> tuple[int, int]:
        """使用 Takeout 拉取一批受限消息 ID 并转发。返回 (成功数, 失败数)。"""
        real_chat_id = (
            source_chat_id
            if str(source_chat_id).startswith("-100")
            else int(f"-100{source_chat_id}")
        )

        try:
            messages = await takeout.get_messages(real_chat_id, ids=msg_ids)
        except Exception as e:
            logger.error("受限同步 #%s Takeout 获取消息失败: %s", task_id, e)
            return 0, len(msg_ids)

        valid_msgs = [m for m in messages if m]
        if not valid_msgs:
            return 0, len(msg_ids)

        # 按 grouped_id 分组
        units = build_forward_units(valid_msgs)
        success_count = 0
        fail_count = 0

        for kind, source_ids in units:
            if self._cancel_flags.get(task_id):
                break
            await self.rl.wait()
            try:
                if kind == "album":
                    album_msgs = [m for m in valid_msgs if m.id in source_ids]
                    target_ids = await self._copy_album(
                        takeout, album_msgs, target_chat_id, target_topic_id
                    )
                    for src_id, tgt_id in zip(source_ids, target_ids):
                        await models.save_message_map(self.db, task_id, src_id, tgt_id)
                    success_count += len(target_ids)
                    if len(target_ids) < len(source_ids):
                        fail_count += len(source_ids) - len(target_ids)
                else:
                    src_id = source_ids[0]
                    msg = next((m for m in valid_msgs if m.id == src_id), None)
                    if not msg:
                        fail_count += 1
                        continue
                    tgt_id = await self._copy_single(
                        takeout, msg, target_chat_id, target_topic_id
                    )
                    if tgt_id:
                        await models.save_message_map(self.db, task_id, src_id, tgt_id)
                        success_count += 1
                    else:
                        fail_count += 1
            except errors.FloodWaitError as e:
                logger.warning("受限同步 #%s FloodWait %ds", task_id, e.seconds)
                await asyncio.sleep(e.seconds)
                fail_count += len(source_ids)
            except Exception as e:
                logger.warning("受限同步 #%s 转发异常 ids=%s: %s", task_id, source_ids, e)
                fail_count += len(source_ids)

        return success_count, fail_count

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def start_sync(
        self,
        task_id: int,
        source_chat_id: int,
        target_chat_id: int,
        source_topic_id: int | None = None,
        target_topic_id: int | None = None,
        notify_chat_id: int | None = None,
        notify_topic_id: int | None = None,
        notify_reply_to_msg_id: int | None = None,
    ):
        """执行受限消息同步任务。"""
        self._cancel_flags[task_id] = False

        async def _notify(text: str) -> None:
            if not notify_chat_id:
                return
            reply_to = notify_reply_to_msg_id or notify_topic_id
            await self.bot.send_message(
                notify_chat_id, text,
                reply_to=reply_to if reply_to else None,
            )

        logger.info(
            "受限同步任务 #%s 开始: source=%s topic=%s",
            task_id, source_chat_id, source_topic_id,
        )

        # 阶段 1: 扫描收集受限消息 ID
        await _notify("🔍 正在扫描源消息，识别受限内容...")
        restricted_ids, total_scanned = await self._collect_restricted_ids(
            task_id, source_chat_id, source_topic_id, _notify
        )

        if restricted_ids is None:
            await models.update_task_status(self.db, task_id, "failed")
            return
        if not restricted_ids:
            if self._cancel_flags.get(task_id):
                await models.update_task_status(self.db, task_id, "paused")
                await _notify("⏸ 受限同步已暂停")
            else:
                await models.update_task_status(self.db, task_id, "completed")
                await _notify(
                    f"✅ 扫描完毕，共 {total_scanned} 条消息\n"
                    f"未发现任何受限消息，无需同步"
                )
            return

        total = len(restricted_ids)
        logger.info("受限同步任务 #%s 扫描 %d 条消息，发现 %d 条受限", task_id, total_scanned, total)
        await _notify(
            f"📋 扫描完毕，共 {total_scanned} 条消息\n"
            f"• 受限消息: {total} 条\n"
            f"正在启动 Takeout 会话..."
        )

        # 阶段 2: 启动 Takeout 分批拉取并转发
        batch_size = 100
        total_success = 0
        total_fail = 0

        try:
            async with self.userbot.takeout() as takeout:
                for i in range(0, total, batch_size):
                    if self._cancel_flags.get(task_id):
                        await models.update_task_status(self.db, task_id, "paused")
                        await _notify(
                            f"⏸ 受限同步已暂停，已完成 {total_success}/{total}"
                        )
                        return

                    batch_ids = restricted_ids[i : i + batch_size]
                    s, f = await self._forward_restricted_batch(
                        task_id, takeout, source_chat_id,
                        target_chat_id, target_topic_id,
                        batch_ids, _notify,
                    )
                    total_success += s
                    total_fail += f

                    if (i + batch_size) < total and total > batch_size:
                        await _notify(
                            f"📊 受限同步进度: {total_success}/{total}"
                        )
        except Exception as e:
            logger.error("受限同步任务 #%s Takeout 会话异常: %s", task_id, e)
            await models.update_task_status(self.db, task_id, "failed")
            await _notify(
                f"❌ 受限同步 #{task_id} Takeout 会话异常: {e}\n"
                f"• 已成功: {total_success} 条\n"
                f"• 失败: {total_fail} 条"
            )
            return

        await models.update_task_status(self.db, task_id, "completed")
        logger.info("受限同步任务 #%s 完成: 成功=%d 失败=%d", task_id, total_success, total_fail)
        await _notify(
            f"✅ 受限消息同步完成\n"
            f"• 源消息总数: {total_scanned} 条\n"
            f"• 受限消息: {total} 条\n"
            f"• Takeout 成功转发: {total_success} 条\n"
            f"• 失败/无媒体: {total_fail} 条"
        )

    def cancel(self, task_id: int):
        self._cancel_flags[task_id] = True
