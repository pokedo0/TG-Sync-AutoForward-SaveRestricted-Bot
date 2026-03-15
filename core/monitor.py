"""实时监控：监听源的新消息并转发到目标。"""
import asyncio
import logging

from telethon import TelegramClient, events, errors
from telethon.tl.types import MessageService

from core.forwarder import Forwarder
from core.rate_limiter import RateLimiter
from db.database import Database
from db import models

logger = logging.getLogger("tg_forward_bot.monitor")


class MonitorManager:
    def __init__(self, bot: TelegramClient, userbot: TelegramClient,
                 db: Database, config: dict):
        self.bot = bot
        self.userbot = userbot
        self.db = db
        self.config = config
        self.rl = RateLimiter(config)
        self.forwarder = Forwarder(bot, userbot, config, self.rl)
        self._handlers: dict[int, callable] = {}

    async def start_monitor(self, task_id: int, source_chat_id: int,
                            target_chat_id: int, mode: str = "copy",
                            source_topic_id: int | None = None,
                            target_topic_id: int | None = None):
        """启动一个监控任务。"""
        async def handler(event):
            # 跳过系统消息
            if isinstance(event.message, MessageService):
                return
            # 如果指定了 topic，只处理该 topic 的消息
            if source_topic_id:
                reply_to = getattr(event.message, "reply_to", None)
                if not reply_to:
                    return
                top_msg_id = getattr(reply_to, "reply_to_top_id", None) or \
                             getattr(reply_to, "reply_to_msg_id", None)
                if top_msg_id != source_topic_id:
                    return

            logger.info("监控 #%s 收到新消息: chat=%s msg=%s",
                        task_id, source_chat_id, event.message.id)
            try:
                target_msg_id = await self.forwarder.forward_message(
                    source_chat_id, event.message.id,
                    target_chat_id, mode, target_topic_id)

                if target_msg_id:
                    await models.save_message_map(self.db, task_id, event.message.id, target_msg_id)
            except (errors.ChannelPrivateError, errors.ChatAdminRequiredError) as e:
                logger.error("监控 #%s 源不可访问: %s，标记为失败", task_id, type(e).__name__)
                await models.update_task_status(self.db, task_id, "failed")
                self._handlers.pop(task_id, None)
                self.userbot.remove_event_handler(handler)
                try:
                    await self.bot.send_message(
                        target_chat_id,
                        f"❌ 监控任务 #{task_id} 已失败: 源不可访问（可能已变为私有或被封禁）",
                        reply_to=target_topic_id if target_topic_id else None)
                except Exception:
                    pass
            except Exception as e:
                logger.warning("监控 #%s 转发异常: %s", task_id, e)

        self.userbot.add_event_handler(
            handler, events.NewMessage(chats=source_chat_id))
        self._handlers[task_id] = handler
        logger.info("监控任务 #%s 已注册: source=%s topic=%s", task_id, source_chat_id, source_topic_id)

    async def stop_monitor(self, task_id: int):
        handler = self._handlers.pop(task_id, None)
        if handler:
            self.userbot.remove_event_handler(handler)
        await models.update_task_status(self.db, task_id, "paused")
        logger.info("监控任务 #%s 已停止", task_id)

    async def restore_tasks(self):
        """启动时恢复之前运行中的 monitor 任务。"""
        tasks = await models.get_tasks_by_status(self.db, "running")
        restored = 0
        for t in tasks:
            if t["type"] == "monitor":
                await self.start_monitor(
                    t["id"], t["source_chat_id"], t["target_chat_id"],
                    t["mode"], t["source_topic_id"], t["target_topic_id"])
                restored += 1
        if restored:
            logger.info("已恢复 %d 个监控任务", restored)
