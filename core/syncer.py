"""历史同步：分批获取源消息并转发到目标，支持断点续传。"""
import asyncio
import logging

from telethon import TelegramClient, errors
from telethon.tl.types import MessageService

from core.forwarder import Forwarder
from core.message_logic import build_forward_units
from core.rate_limiter import RateLimiter
from db.database import Database
from db import models

logger = logging.getLogger("tg_forward_bot.syncer")


class Syncer:
    def __init__(self, bot: TelegramClient, userbot: TelegramClient,
                 db: Database, config: dict):
        self.bot = bot
        self.userbot = userbot
        self.db = db
        self.config = config
        self.rl = RateLimiter(config)
        self.forwarder = Forwarder(bot, userbot, config, self.rl)
        self._cancel_flags: dict[int, bool] = {}

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

        async def _notify(text: str):
            if notify_chat_id:
                await self.bot.send_message(
                    notify_chat_id, text,
                    reply_to=notify_topic_id if notify_topic_id else None)

        logger.info("同步任务 #%s 开始: source=%s topic=%s offset=%s",
                     task_id, source_chat_id, source_topic_id, offset_id)

        total_forwarded = 0
        # 收集所有消息（从旧到新）
        all_msgs = []
        try:
            async for msg in self.userbot.iter_messages(
                source_chat_id, reverse=True, offset_id=offset_id,
                reply_to=source_topic_id):
                if self._cancel_flags.get(task_id):
                    break
                # 跳过系统消息（加人、建群、置顶等）
                if isinstance(msg, MessageService):
                    continue
                all_msgs.append(msg)
        except errors.FloodWaitError as e:
            logger.warning("同步任务 #%s 获取消息列表时 FloodWait %ds", task_id, e.seconds)
            await asyncio.sleep(e.seconds)
        except (errors.ChannelPrivateError, errors.ChatAdminRequiredError) as e:
            logger.error("同步任务 #%s 源不可访问: %s", task_id, type(e).__name__)
            await models.update_task_status(self.db, task_id, "failed")
            await _notify(f"❌ 同步任务 #{task_id} 失败: 源频道/群组不可访问（可能已变为私有或被封禁）")
            return
        except ValueError as e:
            if "input entity" in str(e):
                logger.error("同步任务 #%s 无法访问源: %s", task_id, e)
                await models.update_task_status(self.db, task_id, "failed")
                await _notify(f"❌ 同步任务 #{task_id} 失败: UserBot 未加入该私有频道/群组")
                return
            raise

        # topic 指定了但没获取到消息，可能 topic 已关闭
        if source_topic_id and not all_msgs:
            logger.warning("同步任务 #%s topic=%s 无消息，可能已关闭", task_id, source_topic_id)
            await models.update_task_status(self.db, task_id, "failed")
            await _notify(f"❌ 同步任务 #{task_id} 失败: 话题#{source_topic_id} 无消息（可能已关闭或不存在）")
            return

        total = len(all_msgs)
        logger.info("同步任务 #%s 共获取 %d 条消息", task_id, total)
        await _notify(f"📋 开始同步，共 {total} 条消息")

        # 组装统一转发单元（单条/相册）
        units = build_forward_units(all_msgs)

        # 逐单元转发
        for kind, source_ids in units:
            if self._cancel_flags.get(task_id):
                await models.update_task_status(self.db, task_id, "paused")
                await _notify(f"⏸ 同步已暂停，已完成 {total_forwarded}/{total}")
                return

            if kind == "album":
                target_ids = await self.forwarder.forward_album(
                    source_chat_id, source_ids, target_chat_id, mode, target_topic_id)
                for src_id, tgt_id in zip(source_ids, target_ids):
                    await models.save_message_map(self.db, task_id, src_id, tgt_id)
                msg_count = len(source_ids)
                last_msg_id = source_ids[-1]
            else:
                source_msg_id = source_ids[0]
                target_msg_id = await self.forwarder.forward_message(
                    source_chat_id, source_msg_id, target_chat_id, mode, target_topic_id)
                if target_msg_id:
                    await models.save_message_map(self.db, task_id, source_msg_id, target_msg_id)
                msg_count = 1
                last_msg_id = source_msg_id

            await models.update_last_synced(self.db, task_id, last_msg_id)
            total_forwarded += msg_count

            # 每 100 条发送进度
            if total_forwarded % 100 == 0:
                logger.info("同步任务 #%s 进度: %d/%d", task_id, total_forwarded, total)
                await _notify(f"📊 同步进度: {total_forwarded}/{total}")

        await models.update_task_status(self.db, task_id, "completed")
        logger.info("同步任务 #%s 完成，共转发 %d 条", task_id, total_forwarded)
        await _notify(f"✅ 同步完成，共转发 {total_forwarded} 条消息")

    def cancel(self, task_id: int):
        self._cancel_flags[task_id] = True
