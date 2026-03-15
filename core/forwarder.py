"""转发引擎：智能降级策略，选择最小成本的转发方式。"""
import asyncio
import logging
import tempfile

from telethon import TelegramClient, errors
from telethon.tl.types import Message

from core.message_logic import is_file_media, normalize_messages
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

    async def forward_album(self, source_chat_id: int, msg_ids: list[int],
                            target_chat_id: int, mode: str = "copy",
                            target_topic_id: int | None = None) -> list[int]:
        """
        转发相册（grouped media），尽量保持为同一组发送。
        失败时降级为逐条转发。
        """
        if not msg_ids:
            return []

        msg_ids = sorted(set(msg_ids))
        await self.rl.wait()

        result = await self._try_bot_direct_album(
            source_chat_id, msg_ids, target_chat_id, mode, target_topic_id)
        if result:
            logger.info("album=%s 策略1(Bot直接)成功 -> target_msgs=%s", msg_ids, result)
            return result

        result = await self._try_userbot_read_bot_forward_album(
            source_chat_id, msg_ids, target_chat_id, mode, target_topic_id)
        if result:
            logger.info("album=%s 策略2(UserBot读+Bot写)成功 -> target_msgs=%s", msg_ids, result)
            return result

        result = await self._try_userbot_download_bot_upload_album(
            source_chat_id, msg_ids, target_chat_id, target_topic_id)
        if result:
            logger.info("album=%s 策略3(下载+上传)成功 -> target_msgs=%s", msg_ids, result)
            return result

        forwarded: list[int] = []
        for mid in msg_ids:
            target_mid = await self.forward_message(
                source_chat_id, mid, target_chat_id, mode, target_topic_id)
            if target_mid:
                forwarded.append(target_mid)
        return forwarded

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

    async def _try_bot_direct_album(self, source_chat_id, msg_ids,
                                    target_chat_id, mode, topic_id) -> list[int]:
        try:
            if mode == "forward":
                result = await self.bot.forward_messages(
                    target_chat_id, msg_ids, source_chat_id,
                    **({"reply_to": topic_id} if topic_id else {}))
            else:
                msgs = await self.bot.get_messages(source_chat_id, ids=msg_ids)
                msgs = normalize_messages(msgs)
                if not msgs:
                    logger.info("策略1相册: Bot 无法获取消息 %s", msg_ids)
                    return []
                result = await self._copy_album(self.bot, msgs, target_chat_id, topic_id)
            return self._extract_result_ids(result)
        except (errors.ChatForwardsRestrictedError,
                errors.ChannelPrivateError,
                errors.ChatAdminRequiredError) as e:
            logger.info("策略1相册: Bot 无权限: %s", type(e).__name__)
            return []
        except errors.FloodWaitError as e:
            return await self._handle_flood(
                e, self._try_bot_direct_album,
                source_chat_id, msg_ids, target_chat_id, mode, topic_id)
        except Exception as e:
            logger.warning("策略1相册: Bot 异常: %s", e)
            return []

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

    async def _try_userbot_read_bot_forward_album(self, source_chat_id, msg_ids,
                                                  target_chat_id, mode, topic_id) -> list[int]:
        try:
            msgs = await self.userbot.get_messages(source_chat_id, ids=msg_ids)
            msgs = normalize_messages(msgs)
            if not msgs:
                logger.info("策略2相册: UserBot 无法获取消息 %s", msg_ids)
                return []
            if mode == "forward":
                result = await self.userbot.forward_messages(
                    target_chat_id, msg_ids, source_chat_id,
                    **({"reply_to": topic_id} if topic_id else {}))
            else:
                result = await self._copy_album(self.bot, msgs, target_chat_id, topic_id)
            return self._extract_result_ids(result)
        except errors.ChatForwardsRestrictedError:
            logger.info("策略2相册: 转发受限")
            return []
        except errors.FloodWaitError as e:
            return await self._handle_flood(
                e, self._try_userbot_read_bot_forward_album,
                source_chat_id, msg_ids, target_chat_id, mode, topic_id)
        except Exception as e:
            logger.warning("策略2相册: 异常: %s", e)
            return []

    async def _try_userbot_download_bot_upload(self, source_chat_id, msg_id,
                                               target_chat_id, topic_id) -> int | None:
        try:
            msg = await self.userbot.get_messages(source_chat_id, ids=msg_id)
            if not msg:
                logger.info("策略3: msg=%s UserBot 无法获取消息", msg_id)
                return None

            reply_to = topic_id if topic_id else None

            if is_file_media(msg):
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

    async def _try_userbot_download_bot_upload_album(self, source_chat_id, msg_ids,
                                                     target_chat_id, topic_id) -> list[int]:
        try:
            msgs = await self.userbot.get_messages(source_chat_id, ids=msg_ids)
            msgs = normalize_messages(msgs)
            if not msgs:
                logger.info("策略3相册: UserBot 无法获取消息 %s", msg_ids)
                return []

            reply_to = topic_id if topic_id else None
            media_msgs = [m for m in msgs if is_file_media(m)]
            if not media_msgs:
                logger.info("策略3相册: 无可下载媒体")
                return []

            with tempfile.TemporaryDirectory() as tmpdir:
                files = []
                captions = []
                for m in media_msgs:
                    path = await self.userbot.download_media(m, file=tmpdir)
                    if path:
                        files.append(path)
                        captions.append(m.text or "")
                if not files:
                    logger.warning("策略3相册: 媒体下载失败")
                    return []
                result = await self.bot.send_file(
                    target_chat_id, files, caption=captions, reply_to=reply_to)
                return self._extract_result_ids(result)
        except errors.FloodWaitError as e:
            return await self._handle_flood(
                e, self._try_userbot_download_bot_upload_album,
                source_chat_id, msg_ids, target_chat_id, topic_id)
        except Exception as e:
            logger.warning("策略3相册: 异常: %s", e)
            return []

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
        if is_file_media(msg):
            return await client.send_file(
                target_chat_id, msg.media,
                caption=msg.text or "",
                reply_to=reply_to)
        elif msg.text:
            return await client.send_message(
                target_chat_id, msg.text, reply_to=reply_to)
        return None

    async def _copy_album(self, client: TelegramClient, msgs: list[Message],
                          target_chat_id: int, topic_id: int | None):
        reply_to = topic_id if topic_id else None
        media_msgs = [m for m in msgs if is_file_media(m)]
        if not media_msgs:
            return None
        if len(media_msgs) == 1:
            m = media_msgs[0]
            return await client.send_file(
                target_chat_id, m.media, caption=m.text or "", reply_to=reply_to)
        files = [m.media for m in media_msgs]
        captions = [m.text or "" for m in media_msgs]
        return await client.send_file(
            target_chat_id, files, caption=captions, reply_to=reply_to)

    @staticmethod
    def _extract_result_ids(result) -> list[int]:
        if not result:
            return []
        if isinstance(result, list):
            return [m.id for m in result if m]
        if getattr(result, "id", None):
            return [result.id]
        return []

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
