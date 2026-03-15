"""转发引擎：智能降级策略，选择最小成本的转发方式。"""
import asyncio
import logging
import os
import tempfile

from telethon import TelegramClient, errors
from telethon.tl.types import Message, MessageMediaPhoto, MessageMediaDocument

from core.rate_limiter import RateLimiter

logger = logging.getLogger("tg_forward_bot.forwarder")


class Forwarder:
    def __init__(self, bot: TelegramClient, userbot: TelegramClient,
                 config: dict, rate_limiter: RateLimiter):
        self.bot = bot
        self.userbot = userbot
        self.config = config
        self.rl = rate_limiter

    async def forward_message(self, source_chat_id: int, msg_id: int,
                              target_chat_id: int, mode: str = "copy",
                              target_topic_id: int | None = None) -> int | None:
        """
        转发单条消息，返回目标消息 ID。失败返回 None。
        降级链：Bot直接转发 → UserBot读+Bot写 → UserBot下载+Bot上传 → 发失败标记
        """
        await self.rl.wait()

        # 策略1: Bot 直接转发/复制
        result = await self._try_bot_direct(source_chat_id, msg_id,
                                            target_chat_id, mode, target_topic_id)
        if result is not None:
            logger.info("msg=%s 策略1(Bot直接)成功 -> target_msg=%s", msg_id, result)
            return result

        # 策略2: UserBot 读取消息，Bot 转发
        result = await self._try_userbot_read_bot_forward(
            source_chat_id, msg_id, target_chat_id, mode, target_topic_id)
        if result is not None:
            logger.info("msg=%s 策略2(UserBot读+Bot写)成功 -> target_msg=%s", msg_id, result)
            return result

        # 策略3: UserBot 下载 + Bot 重新上传（突破转发保护）
        result = await self._try_userbot_download_bot_upload(
            source_chat_id, msg_id, target_chat_id, target_topic_id)
        if result is not None:
            logger.info("msg=%s 策略3(下载+上传)成功 -> target_msg=%s", msg_id, result)
            return result

        # 策略4: 发送失败标记
        logger.warning("msg=%s 所有策略失败，发送 #fail2forward 标记", msg_id)
        return await self._send_fail_marker(source_chat_id, msg_id,
                                            target_chat_id, target_topic_id)

    async def _try_bot_direct(self, source_chat_id, msg_id,
                              target_chat_id, mode, topic_id) -> int | None:
        try:
            if mode == "forward":
                result = await self.bot.forward_messages(
                    target_chat_id, msg_id, source_chat_id,
                    **({"reply_to": topic_id} if topic_id else {}))
            else:
                msgs = await self.bot.get_messages(source_chat_id, ids=msg_id)
                if not msgs:
                    logger.info("策略1: msg=%s Bot 无法获取消息", msg_id)
                    return None
                result = await self._copy_message(
                    self.bot, msgs, target_chat_id, topic_id)
            return result.id if result else None
        except (errors.ChatForwardsRestrictedError,
                errors.ChannelPrivateError,
                errors.ChatAdminRequiredError) as e:
            logger.info("策略1: msg=%s Bot 无权限: %s", msg_id, type(e).__name__)
            return None
        except errors.FloodWaitError as e:
            return await self._handle_flood(e, self._try_bot_direct,
                                            source_chat_id, msg_id,
                                            target_chat_id, mode, topic_id)
        except Exception as e:
            logger.warning("策略1: msg=%s Bot 异常: %s", msg_id, e)
            return None

    async def _try_userbot_read_bot_forward(self, source_chat_id, msg_id,
                                            target_chat_id, mode, topic_id) -> int | None:
        try:
            msg = await self.userbot.get_messages(source_chat_id, ids=msg_id)
            if not msg:
                logger.info("策略2: msg=%s UserBot 无法获取消息", msg_id)
                return None
            if mode == "forward":
                result = await self.userbot.forward_messages(
                    target_chat_id, msg_id, source_chat_id,
                    **({"reply_to": topic_id} if topic_id else {}))
            else:
                result = await self._copy_message(
                    self.bot, msg, target_chat_id, topic_id)
            return result.id if result else None
        except errors.ChatForwardsRestrictedError:
            logger.info("策略2: msg=%s 转发受限", msg_id)
            return None
        except errors.FloodWaitError as e:
            return await self._handle_flood(e, self._try_userbot_read_bot_forward,
                                            source_chat_id, msg_id,
                                            target_chat_id, mode, topic_id)
        except Exception as e:
            logger.warning("策略2: msg=%s 异常: %s", msg_id, e)
            return None

    async def _try_userbot_download_bot_upload(self, source_chat_id, msg_id,
                                               target_chat_id, topic_id) -> int | None:
        try:
            msg = await self.userbot.get_messages(source_chat_id, ids=msg_id)
            if not msg:
                logger.info("策略3: msg=%s UserBot 无法获取消息", msg_id)
                return None

            reply_to = topic_id if topic_id else None

            if msg.media:
                with tempfile.TemporaryDirectory() as tmpdir:
                    path = await self.userbot.download_media(msg, file=tmpdir)
                    if path:
                        result = await self.bot.send_file(
                            target_chat_id, path,
                            caption=msg.text or "",
                            reply_to=reply_to)
                        return result.id if result else None
                    else:
                        logger.warning("策略3: msg=%s 媒体下载失败", msg_id)
            elif msg.text:
                result = await self.bot.send_message(
                    target_chat_id, msg.text, reply_to=reply_to)
                return result.id if result else None
            else:
                logger.info("策略3: msg=%s 消息无文本也无媒体", msg_id)
            return None
        except errors.FloodWaitError as e:
            return await self._handle_flood(e, self._try_userbot_download_bot_upload,
                                            source_chat_id, msg_id,
                                            target_chat_id, topic_id)
        except Exception as e:
            logger.warning("策略3: msg=%s 异常: %s", msg_id, e)
            return None

    async def _send_fail_marker(self, source_chat_id, msg_id,
                                target_chat_id, topic_id) -> int | None:
        try:
            entity = await self.userbot.get_entity(source_chat_id)
            username = getattr(entity, "username", None)
            if username:
                link = f"https://t.me/{username}/{msg_id}"
            else:
                chat_id = str(source_chat_id).replace("-100", "")
                link = f"https://t.me/c/{chat_id}/{msg_id}"

            text = f"⚠️ 无法转发的消息: {link}\n#fail2forward"
            result = await self.bot.send_message(
                target_chat_id, text,
                reply_to=topic_id if topic_id else None)
            return result.id if result else None
        except Exception as e:
            logger.error("发送失败标记异常: msg=%s err=%s", msg_id, e)
            return None

    async def _copy_message(self, client: TelegramClient, msg: Message,
                            target_chat_id: int, topic_id: int | None):
        reply_to = topic_id if topic_id else None
        if msg.media:
            return await client.send_file(
                target_chat_id, msg.media,
                caption=msg.text or "",
                reply_to=reply_to)
        elif msg.text:
            return await client.send_message(
                target_chat_id, msg.text, reply_to=reply_to)
        return None

    async def _handle_flood(self, error: errors.FloodWaitError,
                            retry_func, *args):
        wait_seconds = error.seconds
        if wait_seconds > self.rl.max_flood_wait:
            logger.error("FloodWait %ds 超过最大等待 %ds，放弃", wait_seconds, self.rl.max_flood_wait)
            return None
        logger.warning("FloodWaitError: 等待 %ds 后重试", wait_seconds)
        self.rl.on_flood_wait()
        await asyncio.sleep(wait_seconds)
        return await retry_func(*args)
