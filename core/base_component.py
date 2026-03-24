"""核心组件基类：统一注入 bot/userbot/db/config，提供共享方法。"""
import logging
from collections.abc import Awaitable, Callable

from telethon import TelegramClient, errors

from core.rate_limiter import RateLimiter
from core.runtime_context import build_forward_runtime
from db.database import Database
from db import models


class SyncComponentBase:
    """轻量基类：持有 bot/userbot/db/config/rl，不创建 Forwarder。"""

    def __init__(
        self,
        bot: TelegramClient,
        userbot: TelegramClient,
        db: Database,
        config: dict,
    ) -> None:
        self.bot = bot
        self.userbot = userbot
        self.db = db
        self.config = config
        self.rl = RateLimiter(config)
        self._cancel_flags: dict[int, bool] = {}

    def cancel(self, task_id: int) -> None:
        self._cancel_flags[task_id] = True

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

    @staticmethod
    def _ensure_supergroup_id(chat_id: int) -> int:
        """确保 chat_id 为 -100 前缀的超级群/频道 ID（Takeout 需要）。"""
        n = abs(chat_id)
        if n > 10**12:
            return -int(str(n)) if chat_id > 0 else chat_id
        return int(f"-100{n}")

    @staticmethod
    def _log_forward_result(
        logger: logging.Logger,
        task_type: str,
        task_id: int,
        unit_kind: str,
        source_ids: list[int],
        target_ids: list[int],
        restricted: bool = False,
    ) -> None:
        """统一打印每条/每组消息的转发日志。"""
        tag = " [受限:Takeout]" if restricted else ""
        if unit_kind == "album":
            if target_ids:
                logger.info(
                    "%s #%s album=%s%s 转发成功 -> target_msgs=%s",
                    task_type, task_id, source_ids, tag, target_ids,
                )
            else:
                logger.info(
                    "%s #%s album=%s%s 转发失败",
                    task_type, task_id, source_ids, tag,
                )
        else:
            src = source_ids[0] if source_ids else "?"
            if target_ids:
                logger.info(
                    "%s #%s msg=%s%s 转发成功 -> target_msg=%s",
                    task_type, task_id, src, tag, target_ids[0],
                )
            else:
                logger.info(
                    "%s #%s msg=%s%s 转发失败",
                    task_type, task_id, src, tag,
                )

    async def _handle_collect_error(
        self,
        task_id: int,
        error: Exception,
        notify: Callable[[str], Awaitable[None]],
    ) -> bool:
        """处理消息收集阶段的常见错误，返回 True 表示已处理。"""
        if isinstance(error, (errors.ChannelPrivateError, errors.ChatAdminRequiredError)):
            await models.update_task_status(self.db, task_id, "failed")
            await notify(f"❌ 任务 #{task_id} 失败: 源不可访问（{type(error).__name__}）")
            return True
        if isinstance(error, ValueError) and "input entity" in str(error):
            await models.update_task_status(self.db, task_id, "failed")
            await notify(f"❌ 任务 #{task_id} 失败: UserBot 未加入该私有频道/群组")
            return True
        return False


class ForwardingComponent(SyncComponentBase):
    """含 Forwarder 的组件基类，供 Syncer / Monitor 使用。"""

    def __init__(
        self,
        bot: TelegramClient,
        userbot: TelegramClient,
        db: Database,
        config: dict,
    ) -> None:
        super().__init__(bot, userbot, db, config)
        runtime = build_forward_runtime(bot, userbot, config)
        self.rl = runtime.rl
        self.forwarder = runtime.forwarder
