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
        notify_reply_to_msg_id: int | None,
        text: str,
    ) -> None:
        if not notify_chat_id:
            return
        reply_to = notify_reply_to_msg_id if notify_reply_to_msg_id else notify_topic_id
        await self.bot.send_message(
            notify_chat_id,
            text,
            reply_to=reply_to if reply_to else None,
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

    @staticmethod
    def _is_general_topic_msg(msg) -> bool:
        """判断消息是否属于 General 话题（无 reply_to 或不指向其他话题）。"""
        reply_to = getattr(msg, "reply_to", None)
        if not reply_to:
            return True
        top_id = getattr(reply_to, "reply_to_top_id", None)
        if top_id:
            return top_id == 1
        if getattr(reply_to, "forum_topic", False):
            return getattr(reply_to, "reply_to_msg_id", None) in (1, None)
        return True

    async def _collect_messages(
        self,
        task_id: int,
        source_chat_id: int,
        source_topic_id: int | None,
        offset_id: int,
        notify: Callable[[str], Awaitable[None]],
    ) -> list | None:
        all_msgs = []
        # General 话题 (id=1) 的消息不携带 reply_to 指向话题 1，
        # iter_messages(reply_to=1) 无法获取，需遍历全部消息后客户端过滤。
        iter_reply_to = None if source_topic_id == 1 else source_topic_id
        try:
            async for msg in self.userbot.iter_messages(
                source_chat_id,
                reverse=True,
                offset_id=offset_id,
                reply_to=iter_reply_to,
            ):
                if self._cancel_flags.get(task_id):
                    break
                if isinstance(msg, MessageService):
                    continue
                if source_topic_id == 1 and not self._is_general_topic_msg(msg):
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
    ) -> tuple[int, int, bool]:
        if unit_kind == "album":
            target_ids = await self.forwarder.forward_album(
                source_chat_id, source_ids, target_chat_id, mode, target_topic_id
            )
            for src_id, tgt_id in zip(source_ids, target_ids):
                await models.save_message_map(self.db, task_id, src_id, tgt_id)
            return len(target_ids), source_ids[-1], bool(target_ids)

        source_msg_id = source_ids[0]
        target_msg_id = await self.forwarder.forward_message(
            source_chat_id, source_msg_id, target_chat_id, mode, target_topic_id
        )
        if target_msg_id:
            await models.save_message_map(self.db, task_id, source_msg_id, target_msg_id)
            return 1, source_msg_id, True
        return 0, source_msg_id, False

    async def start_sync(self, task_id: int, source_chat_id: int,
                         target_chat_id: int, mode: str = "copy",
                         source_topic_id: int | None = None,
                         target_topic_id: int | None = None,
                         notify_chat_id: int | None = None,
                         notify_topic_id: int | None = None,
                         notify_reply_to_msg_id: int | None = None):
        """执行历史同步任务。"""
        self._cancel_flags[task_id] = False
        task = await models.get_task(self.db, task_id)
        offset_id = task["last_synced_msg_id"] if task else 0

        async def _notify(text: str) -> None:
            await self._notify(
                notify_chat_id,
                notify_topic_id,
                notify_reply_to_msg_id,
                text,
            )

        logger.info("同步任务 #%s 开始: source=%s topic=%s offset=%s",
                     task_id, source_chat_id, source_topic_id, offset_id)

        total_forwarded = 0
        restricted_messages = 0
        failed_units = 0
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

            restricted, restricted_field = await self.forwarder.detect_restriction(
                source_chat_id, source_ids[0]
            )
            if restricted:
                restricted_messages += len(source_ids)
                last_msg_id = source_ids[-1]
                await models.update_last_synced(self.db, task_id, last_msg_id)
                logger.info(
                    "同步任务 #%s 跳过受限消息单元 kind=%s ids=%s field=%s",
                    task_id, kind, source_ids, restricted_field
                )
                continue

            msg_count, last_msg_id, success = await self._forward_unit(
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
            if not success:
                failed_units += 1

            if total_forwarded % 100 == 0:
                logger.info("同步任务 #%s 进度: %d/%d", task_id, total_forwarded, total)
                await _notify(f"📊 同步进度: {total_forwarded}/{total}")

        await models.update_task_status(self.db, task_id, "completed")
        logger.info("同步任务 #%s 完成，共转发 %d 条", task_id, total_forwarded)
        await _notify(
            "✅ 同步完成"
            f"\n• 成功转发: {total_forwarded} 条"
            f"\n• 跳过受限: {restricted_messages} 条"
            f"\n• 其他失败: {failed_units} 组"
        )

    def cancel(self, task_id: int):
        self._cancel_flags[task_id] = True
