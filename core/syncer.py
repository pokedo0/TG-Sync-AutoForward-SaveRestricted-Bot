"""历史同步：分批获取源消息并转发到目标，支持断点续传。"""
import asyncio
import logging
from collections.abc import Awaitable, Callable

from telethon import TelegramClient, errors
from telethon.tl.types import MessageService

from core.base_component import ForwardingComponent
from core.message_logic import build_forward_units
from db.database import Database
from db import models

logger = logging.getLogger("tg_forward_bot.syncer")


class Syncer(ForwardingComponent):
    def __init__(self, bot: TelegramClient, userbot: TelegramClient,
                 db: Database, config: dict):
        super().__init__(bot, userbot, db, config)
        self._cancel_flags: dict[int, bool] = {}

    async def _notify(
        self,
        notify_chat_id: int | None,
        notify_topic_id: int | None,
        text: str,
    ) -> None:
        if not notify_chat_id:
            return
        await self.bot.send_message(
            notify_chat_id,
            text,
            reply_to=notify_topic_id if notify_topic_id else None,
        )

    async def _handle_collect_error(
        self,
        task_id: int,
        error: Exception,
        notify: Callable[[str], Awaitable[None]],
    ) -> bool:
        if isinstance(error, (errors.ChannelPrivateError, errors.ChatAdminRequiredError)):
            logger.error("同步任务 #%s 源不可访问: %s", task_id, type(error).__name__)
            await models.update_task_status(self.db, task_id, "failed")
            await notify(f"❌ 同步任务 #{task_id} 失败: 源频道/群组不可访问（可能已变为私有或被封禁）")
            return True
        if isinstance(error, ValueError) and "input entity" in str(error):
            logger.error("同步任务 #%s 无法访问源: %s", task_id, error)
            await models.update_task_status(self.db, task_id, "failed")
            await notify(f"❌ 同步任务 #{task_id} 失败: UserBot 未加入该私有频道/群组")
            return True
        return False

    async def _collect_messages(
        self,
        task_id: int,
        source_chat_id: int,
        source_topic_id: int | None,
        offset_id: int,
        notify: Callable[[str], Awaitable[None]],
    ) -> list | None:
        all_msgs = []
        try:
            async for msg in self.userbot.iter_messages(
                source_chat_id,
                reverse=True,
                offset_id=offset_id,
                reply_to=source_topic_id,
            ):
                if self._cancel_flags.get(task_id):
                    break
                if isinstance(msg, MessageService):
                    continue
                all_msgs.append(msg)
        except Exception as error:
            if isinstance(error, errors.FloodWaitError):
                logger.warning("同步任务 #%s 获取消息列表时 FloodWait %ds", task_id, error.seconds)
                await asyncio.sleep(error.seconds)
                return []
            if await self._handle_collect_error(task_id, error, notify):
                return None
            raise
        return all_msgs

    async def _forward_unit(
        self,
        task_id: int,
        source_chat_id: int,
        target_chat_id: int,
        mode: str,
        target_topic_id: int | None,
        unit_kind: str,
        source_ids: list[int],
    ) -> tuple[int, int]:
        if unit_kind == "album":
            target_ids = await self.forwarder.forward_album(
                source_chat_id, source_ids, target_chat_id, mode, target_topic_id
            )
            for src_id, tgt_id in zip(source_ids, target_ids):
                await models.save_message_map(self.db, task_id, src_id, tgt_id)
            return len(source_ids), source_ids[-1]

        source_msg_id = source_ids[0]
        target_msg_id = await self.forwarder.forward_message(
            source_chat_id, source_msg_id, target_chat_id, mode, target_topic_id
        )
        if target_msg_id:
            await models.save_message_map(self.db, task_id, source_msg_id, target_msg_id)
        return 1, source_msg_id

    async def start_sync(self, task_id: int, source_chat_id: int,
                         target_chat_id: int, mode: str = "copy",
                         source_topic_id: int | None = None,
                         target_topic_id: int | None = None,
                         notify_chat_id: int | None = None,
                         notify_topic_id: int | None = None):
        """执行历史同步任务。"""
        self._cancel_flags[task_id] = False
        task = await models.get_task(self.db, task_id)
        offset_id = task["last_synced_msg_id"] if task else 0

        async def _notify(text: str) -> None:
            await self._notify(notify_chat_id, notify_topic_id, text)

        logger.info("同步任务 #%s 开始: source=%s topic=%s offset=%s",
                     task_id, source_chat_id, source_topic_id, offset_id)

        total_forwarded = 0
        all_msgs = await self._collect_messages(
            task_id, source_chat_id, source_topic_id, offset_id, _notify
        )
        if all_msgs is None:
            return
        if not all_msgs and self._cancel_flags.get(task_id):
            await models.update_task_status(self.db, task_id, "paused")
            await _notify("⏸ 同步已暂停，已完成 0/0")
            return

        if source_topic_id and not all_msgs:
            logger.warning("同步任务 #%s topic=%s 无消息，可能已关闭", task_id, source_topic_id)
            await models.update_task_status(self.db, task_id, "failed")
            await _notify(f"❌ 同步任务 #{task_id} 失败: 话题#{source_topic_id} 无消息（可能已关闭或不存在）")
            return

        total = len(all_msgs)
        logger.info("同步任务 #%s 共获取 %d 条消息", task_id, total)
        await _notify(f"📋 开始同步，共 {total} 条消息")

        units = build_forward_units(all_msgs)

        for kind, source_ids in units:
            if self._cancel_flags.get(task_id):
                await models.update_task_status(self.db, task_id, "paused")
                await _notify(f"⏸ 同步已暂停，已完成 {total_forwarded}/{total}")
                return

            msg_count, last_msg_id = await self._forward_unit(
                task_id=task_id,
                source_chat_id=source_chat_id,
                target_chat_id=target_chat_id,
                mode=mode,
                target_topic_id=target_topic_id,
                unit_kind=kind,
                source_ids=source_ids,
            )

            await models.update_last_synced(self.db, task_id, last_msg_id)
            total_forwarded += msg_count

            if total_forwarded % 100 == 0:
                logger.info("同步任务 #%s 进度: %d/%d", task_id, total_forwarded, total)
                await _notify(f"📊 同步进度: {total_forwarded}/{total}")

        await models.update_task_status(self.db, task_id, "completed")
        logger.info("同步任务 #%s 完成，共转发 %d 条", task_id, total_forwarded)
        await _notify(f"✅ 同步完成，共转发 {total_forwarded} 条消息")

    def cancel(self, task_id: int):
        self._cancel_flags[task_id] = True
