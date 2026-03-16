"""转发引擎：智能降级策略，选择最小成本的转发方式。"""
import asyncio
import logging
import tempfile

from telethon import TelegramClient, errors
from telethon.tl.types import Message

from core.message_logic import is_file_media, normalize_messages
from core.media_transfer import MediaTransferHelper
from core.rate_limiter import RateLimiter

logger = logging.getLogger("tg_forward_bot.forwarder")


class Forwarder:
    def __init__(self, bot: TelegramClient, userbot: TelegramClient,
                 config: dict, rate_limiter: RateLimiter):
        self.bot = bot
        self.userbot = userbot
        self.config = config
        self.rl = rate_limiter

        transfer_cfg = (config or {}).get("transfer", {})
        self.album_download_concurrency = max(
            1, int(transfer_cfg.get("album_download_concurrency", 3))
        )
        self.upload_part_size_kb = self._clamp_part_size_kb(
            transfer_cfg.get("upload_part_size_kb", 512)
        )
        self.download_part_size_kb = self._clamp_part_size_kb(
            transfer_cfg.get("download_part_size_kb", 512)
        )
        self.media = MediaTransferHelper(
            bot=self.bot,
            userbot=self.userbot,
            upload_part_size_kb=self.upload_part_size_kb,
            download_part_size_kb=self.download_part_size_kb,
        )

    async def forward_message(self, source_chat_id: int, msg_id: int,
                              target_chat_id: int, mode: str = "copy",
                              target_topic_id: int | None = None) -> int | None:
        """
        转发单条消息，返回目标消息 ID。失败返回 None。
        降级链：Bot直接转发 → UserBot读+Bot写 → UserBot下载+Bot上传 → 发失败标记
        """
        result = await self._run_message_strategies(
            source_chat_id, msg_id, target_chat_id, mode, target_topic_id
        )
        if result is not None:
            return result

        # 策略4: 发送失败标记
        logger.warning("msg=%s 所有策略失败，发送 #fail2forward 标记", msg_id)
        return await self.send_fail_marker(
            source_chat_id, msg_id, target_chat_id, target_topic_id
        )

    async def detect_restriction(
        self,
        source_chat_id: int,
        msg_id: int | None = None,
    ) -> tuple[bool, str]:
        """硬封禁并集：message.restriction_reason.platform=all 或 chat级全平台封禁。"""
        try:
            entity = await self.userbot.get_entity(source_chat_id)
            if getattr(entity, "restricted", False):
                chat_reasons = getattr(entity, "restriction_reason", None) or []
                if self._has_platform_all_reason(chat_reasons):
                    return True, "chat.restricted+reason.platform_all"
        except Exception:
            pass

        if msg_id is None:
            return False, ""

        try:
            msg = await self._get_single_message(self.userbot, source_chat_id, msg_id)
            if msg:
                msg_reasons = getattr(msg, "restriction_reason", None) or []
                if self._has_platform_all_reason(msg_reasons):
                    return True, "message.reason.platform_all"
        except Exception:
            pass

        return False, ""

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
        result = await self._run_album_strategies(
            source_chat_id, msg_ids, target_chat_id, mode, target_topic_id
        )
        if result:
            return result

        forwarded: list[int] = []
        for mid in msg_ids:
            target_mid = await self.forward_message(
                source_chat_id, mid, target_chat_id, mode, target_topic_id)
            if target_mid:
                forwarded.append(target_mid)
        return forwarded

    async def _run_message_strategies(self, source_chat_id: int, msg_id: int,
                                      target_chat_id: int, mode: str,
                                      target_topic_id: int | None) -> int | None:
        await self.rl.wait()
        strategies = [
            ("策略1(Bot直接)", self._try_bot_direct,
             (source_chat_id, msg_id, target_chat_id, mode, target_topic_id)),
            ("策略2(UserBot读+Bot写)", self._try_userbot_read_bot_forward,
             (source_chat_id, msg_id, target_chat_id, mode, target_topic_id)),
            ("策略3(下载+上传)", self._try_userbot_download_bot_upload,
             (source_chat_id, msg_id, target_chat_id, target_topic_id)),
        ]
        for strategy_name, strategy_func, args in strategies:
            result = await strategy_func(*args)
            if result is not None:
                logger.info("msg=%s %s成功 -> target_msg=%s", msg_id, strategy_name, result)
                return result
        return None

    async def _run_album_strategies(self, source_chat_id: int, msg_ids: list[int],
                                    target_chat_id: int, mode: str,
                                    target_topic_id: int | None) -> list[int]:
        await self.rl.wait()
        strategies = [
            ("策略1(Bot直接)", self._try_bot_direct_album,
             (source_chat_id, msg_ids, target_chat_id, mode, target_topic_id)),
            ("策略2(UserBot读+Bot写)", self._try_userbot_read_bot_forward_album,
             (source_chat_id, msg_ids, target_chat_id, mode, target_topic_id)),
            ("策略3(下载+上传)", self._try_userbot_download_bot_upload_album,
             (source_chat_id, msg_ids, target_chat_id, target_topic_id)),
        ]
        for strategy_name, strategy_func, args in strategies:
            result = await strategy_func(*args)
            if result:
                logger.info("album=%s %s成功 -> target_msgs=%s", msg_ids, strategy_name, result)
                return result
        return []

    async def _try_bot_direct(self, source_chat_id, msg_id,
                              target_chat_id, mode, topic_id) -> int | None:
        try:
            source_ref = await self._resolve_source_for_bot(source_chat_id)
            if mode == "forward":
                result = await self.bot.forward_messages(
                    target_chat_id, msg_id, source_ref,
                    **self._reply_kwargs(topic_id))
            else:
                msg = await self._get_single_message(self.bot, source_ref, msg_id)
                if not msg:
                    logger.info("策略1: msg=%s Bot 无法获取消息", msg_id)
                    return None
                result = await self._copy_message(
                    self.bot, msg, target_chat_id, topic_id)
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
            source_ref = await self._resolve_source_for_bot(source_chat_id)
            if mode == "forward":
                result = await self.bot.forward_messages(
                    target_chat_id, msg_ids, source_ref,
                    **self._reply_kwargs(topic_id))
            else:
                msgs = await self._get_message_list(self.bot, source_ref, msg_ids)
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
            msg = await self._get_single_message(self.userbot, source_chat_id, msg_id)
            if not msg:
                logger.info("策略2: msg=%s UserBot 无法获取消息", msg_id)
                return None
            if mode == "forward":
                result = await self.userbot.forward_messages(
                    target_chat_id, msg_id, source_chat_id,
                    **self._reply_kwargs(topic_id))
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
            msgs = await self._get_message_list(self.userbot, source_chat_id, msg_ids)
            if not msgs:
                logger.info("策略2相册: UserBot 无法获取消息 %s", msg_ids)
                return []
            if mode == "forward":
                result = await self.userbot.forward_messages(
                    target_chat_id, msg_ids, source_chat_id,
                    **self._reply_kwargs(topic_id))
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
            msg = await self._get_single_message(self.userbot, source_chat_id, msg_id)
            if not msg:
                logger.info("策略3: msg=%s UserBot 无法获取消息", msg_id)
                return None

            reply_to = self._reply_to(topic_id)

            if is_file_media(msg):
                with tempfile.TemporaryDirectory() as tmpdir:
                    path = await self.media.download_media_to_path(msg, tmpdir)
                    if path:
                        thumb_path = await self.media.download_video_thumb_to_path(msg, tmpdir)
                        send_kwargs = self.media.build_send_file_kwargs(
                            msg, reply_to, thumb_path=thumb_path
                        )
                        result = await self.media.send_file_with_compat(
                            target_chat_id, path, **send_kwargs
                        )
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
            msgs = await self._get_message_list(self.userbot, source_chat_id, msg_ids)
            if not msgs:
                logger.info("策略3相册: UserBot 无法获取消息 %s", msg_ids)
                return []

            reply_to = self._reply_to(topic_id)
            media_msgs = [m for m in msgs if is_file_media(m)]
            if not media_msgs:
                logger.info("策略3相册: 无可下载媒体")
                return []

            with tempfile.TemporaryDirectory() as tmpdir:
                ok_items = await self._download_album_media(media_msgs, tmpdir)
                files, captions = self._build_album_upload_payload(ok_items)
                if not files:
                    logger.warning("策略3相册: 媒体下载失败")
                    return []
                result = await self.bot.send_file(
                    target_chat_id,
                    files,
                    caption=captions,
                    reply_to=reply_to,
                    part_size_kb=self.upload_part_size_kb,
                )
                return self._extract_result_ids(result)
        except errors.FloodWaitError as e:
            return await self._handle_flood(
                e, self._try_userbot_download_bot_upload_album,
                source_chat_id, msg_ids, target_chat_id, topic_id)
        except Exception as e:
            logger.warning("策略3相册: 异常: %s", e)
            return []

    async def build_source_link(self, source_chat_id: int, msg_id: int) -> str:
        """构造源消息链接（公开或私有）。"""
        entity = await self.userbot.get_entity(source_chat_id)
        username = getattr(entity, "username", None)
        if username:
            return f"https://t.me/{username}/{msg_id}"
        chat_id = str(source_chat_id).replace("-100", "")
        return f"https://t.me/c/{chat_id}/{msg_id}"

    async def send_fail_marker(
        self,
        source_chat_id,
        msg_id,
        target_chat_id,
        topic_id,
        reason: str | None = None,
    ) -> int | None:
        try:
            link = await self.build_source_link(source_chat_id, msg_id)
            reason_suffix = f"（{reason}）" if reason else ""
            text = f"⚠️ 无法转发的消息{reason_suffix}: {link}\n#fail2forward"
            result = await self.bot.send_message(
                target_chat_id, text,
                reply_to=topic_id if topic_id else None)
            return result.id if result else None
        except Exception as e:
            logger.error("发送失败标记异常: msg=%s err=%s", msg_id, e)
            return None

    async def _download_album_media(self, media_msgs: list[Message], tmpdir: str):
        sem = asyncio.Semaphore(self.album_download_concurrency)

        async def _download_one(index: int, message: Message):
            async with sem:
                path = await self.media.download_media_to_path(message, tmpdir)
                return index, message, path

        tasks = [
            asyncio.create_task(_download_one(idx, m))
            for idx, m in enumerate(media_msgs)
        ]
        downloaded = await asyncio.gather(*tasks, return_exceptions=True)

        ok_items: list[tuple[int, Message, str]] = []
        for item in downloaded:
            if isinstance(item, Exception):
                logger.warning("策略3相册: 并发下载异常: %s", item)
                continue
            index, message, path = item
            if path:
                ok_items.append((index, message, path))
        return ok_items

    @staticmethod
    def _build_album_upload_payload(
        ok_items: list[tuple[int, Message, str]]
    ) -> tuple[list[str], list[str]]:
        ordered_items = sorted(ok_items, key=lambda item: item[0])
        files = [path for _, _, path in ordered_items]
        captions = [message.text or "" for _, message, _ in ordered_items]
        return files, captions

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

    @staticmethod
    def _reply_to(topic_id: int | None) -> int | None:
        return topic_id if topic_id else None

    @staticmethod
    def _reply_kwargs(topic_id: int | None) -> dict:
        return {"reply_to": topic_id} if topic_id else {}

    async def _get_single_message(self, client: TelegramClient,
                                  chat_or_entity, msg_id: int) -> Message | None:
        msg = await client.get_messages(chat_or_entity, ids=msg_id)
        return msg if msg else None

    async def _get_message_list(self, client: TelegramClient,
                                chat_or_entity, msg_ids: list[int]) -> list[Message]:
        msgs = await client.get_messages(chat_or_entity, ids=msg_ids)
        return normalize_messages(msgs)

    @staticmethod
    def _clamp_part_size_kb(value) -> int:
        try:
            n = int(value)
        except Exception:
            n = 512
        return max(32, min(512, n))

    async def _resolve_source_for_bot(self, source_chat_id):
        try:
            return await self.bot.get_input_entity(source_chat_id)
        except Exception:
            pass

        try:
            src = await self.userbot.get_entity(source_chat_id)
        except Exception:
            return source_chat_id

        username = getattr(src, "username", None)
        if username:
            try:
                return await self.bot.get_input_entity(username)
            except Exception:
                pass
        return source_chat_id

    @staticmethod
    def _has_platform_all_reason(reasons) -> bool:
        for reason in reasons or []:
            platform = None
            if isinstance(reason, dict):
                platform = reason.get("platform")
            else:
                platform = getattr(reason, "platform", None)
            if isinstance(platform, str) and platform.lower() == "all":
                return True
        return False
