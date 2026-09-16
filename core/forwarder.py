"""转发引擎：智能降级策略，选择最小成本的转发方式。"""
import asyncio
import logging
import random
import tempfile

from telethon import TelegramClient, errors, functions
from telethon.tl.functions.channels import GetMessagesRequest
from telethon.tl.types import Message
from telethon.tl.types import Channel, InputMessageID, MessageMediaDocument

from core.message_logic import (
    detect_hard_restriction,
    is_chat_globally_restricted,
    is_file_media,
    normalize_messages,
)
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

    @staticmethod
    def _select_best_alt_video(msg_or_media):
        """从文档对象的 alt_documents 中提取最高清晰度的 MP4 视频切片。

        排除 m3u8 播放列表与 storyboard；
        排序规则：分辨率高度 h 降序；同分辨率下优先 H.264 编码（全设备兼容性最佳）。
        返回: (best_doc, res_str, codec_str) 或 None
        """
        if not msg_or_media:
            return None
        media = getattr(msg_or_media, "media", msg_or_media)
        alt_docs = getattr(media, "alt_documents", None) or []
        if not alt_docs:
            return None

        candidates = []
        for alt in alt_docs:
            if getattr(alt, "mime_type", "") != "video/mp4":
                continue
            h = 0
            w = 0
            codec = ""
            for a in getattr(alt, "attributes", []):
                if hasattr(a, "h") and getattr(a, "h", 0):
                    h = a.h
                    w = getattr(a, "w", 0)
                if hasattr(a, "video_codec") and getattr(a, "video_codec", ""):
                    codec = a.video_codec.lower()
            if h > 0:
                is_h264 = 1 if ("h264" in codec or "avc" in codec) else 0
                candidates.append((h, is_h264, w, codec, alt))

        if not candidates:
            return None

        # 优先 h 降序，其次 is_h264 降序
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        best_h, best_is_h264, best_w, best_codec, best_alt = candidates[0]
        res_str = f"{best_w}x{best_h}" if best_w else f"{best_h}p"
        codec_str = best_codec or "未知编码"
        return best_alt, res_str, codec_str

    async def _forward_messages_drop_author(
        self, target_chat_id: int, msg_ids: int | list[int],
        source_ref, topic_id: int | None = None
    ):
        """原生无痕转发 (drop_author=True)，保留 100% 画质切片。
        若带有 topic_id 且发往超级群，注入 top_msg_id 确保落在指定论坛话题。
        """
        is_single = isinstance(msg_ids, int)
        ids_list = [msg_ids] if is_single else list(msg_ids)
        if not ids_list:
            return None

        # 无 topic_id 或目标是私聊，直接用 Telethon 官方 forward_messages
        if not topic_id or target_chat_id > 0:
            result = await self.bot.forward_messages(
                target_chat_id, ids_list if not is_single else ids_list[0],
                source_ref, drop_author=True
            )
            return result

        # 带有 topic_id 的超级群：使用 MTProto 底层 ForwardMessagesRequest 携带 top_msg_id
        try:
            to_peer = await self.bot.get_input_entity(target_chat_id)
            from_peer = await self.bot.get_input_entity(source_ref)
            random_ids = [random.randint(-2**63, 2**63 - 1) for _ in ids_list]
            req = functions.messages.ForwardMessagesRequest(
                from_peer=from_peer,
                id=ids_list,
                to_peer=to_peer,
                drop_author=True,
                top_msg_id=topic_id,
                random_id=random_ids
            )
            updates = await self.bot(req)
            sent_msgs = []
            for u in getattr(updates, "updates", []):
                m = getattr(u, "message", None)
                if m:
                    sent_msgs.append(m)
            if is_single:
                return sent_msgs[0] if sent_msgs else None
            return sent_msgs
        except Exception as e:
            logger.warning("带 topic_id 底层原生转发异常(%s: %s)，回退普通 forward_messages",
                           type(e).__name__, e)
            result = await self.bot.forward_messages(
                target_chat_id, ids_list if not is_single else ids_list[0],
                source_ref, drop_author=True
            )
            return result

    async def forward_message(self, source_chat_id: int, msg_id: int,
                              target_chat_id: int, mode: str = "copy",
                              target_topic_id: int | None = None,
                              caller: str = "monitor") -> int | None:
        """
        转发单条消息，返回目标消息 ID。失败返回 None。
        降级链：Bot直接转发 → UserBot读+Bot写 → UserBot下载+Bot上传 → 发失败标记
        """
        result = await self._run_message_strategies(
            source_chat_id, msg_id, target_chat_id, mode, target_topic_id, caller=caller
        )
        if result is not None:
            return result

        # 策略4: 发送失败标记（私聊环境跳过，避免向用户推送垃圾标记）
        is_private = (caller == "private") or (target_chat_id > 0)
        if is_private:
            logger.warning("msg=%s 所有策略失败，私聊环境跳过发送 #fail2forward 标记", msg_id)
            return None

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
        chat_globally_restricted = False
        try:
            entity = await self.userbot.get_entity(source_chat_id)
            chat_globally_restricted = is_chat_globally_restricted(entity)
        except Exception:
            chat_globally_restricted = False

        restricted, field = detect_hard_restriction(chat_globally_restricted, None)
        if restricted:
            return restricted, field

        if msg_id is None:
            return False, ""

        try:
            msg = await self._get_single_message(self.userbot, source_chat_id, msg_id)
            return detect_hard_restriction(chat_globally_restricted, msg)
        except Exception:
            pass

        return False, ""

    async def forward_album(self, source_chat_id: int, msg_ids: list[int],
                            target_chat_id: int, mode: str = "copy",
                            target_topic_id: int | None = None,
                            caller: str = "monitor") -> list[int]:
        """
        转发相册（grouped media），尽量保持为同一组发送。
        失败时降级为逐条转发。
        """
        if not msg_ids:
            return []

        msg_ids = sorted(set(msg_ids))
        result = await self._run_album_strategies(
            source_chat_id, msg_ids, target_chat_id, mode, target_topic_id, caller=caller
        )
        if result:
            return result

        forwarded: list[int] = []
        for mid in msg_ids:
            target_mid = await self.forward_message(
                source_chat_id, mid, target_chat_id, mode, target_topic_id, caller=caller)
            if target_mid:
                forwarded.append(target_mid)
        return forwarded

    async def _run_message_strategies(self, source_chat_id: int, msg_id: int,
                                      target_chat_id: int, mode: str,
                                      target_topic_id: int | None,
                                      caller: str = "monitor") -> int | None:
        await self.rl.wait()
        strategies = [
            ("策略1(Bot直接)", self._try_bot_direct,
             (source_chat_id, msg_id, target_chat_id, mode, target_topic_id, caller)),
            ("策略2(UserBot读+Bot写)", self._try_userbot_read_bot_forward,
             (source_chat_id, msg_id, target_chat_id, mode, target_topic_id, caller)),
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
                                    target_topic_id: int | None,
                                    caller: str = "monitor") -> list[int]:
        await self.rl.wait()
        strategies = [
            ("策略1(Bot直接)", self._try_bot_direct_album,
             (source_chat_id, msg_ids, target_chat_id, mode, target_topic_id, caller)),
            ("策略2(UserBot读+Bot写)", self._try_userbot_read_bot_forward_album,
             (source_chat_id, msg_ids, target_chat_id, mode, target_topic_id, caller)),
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
                              target_chat_id, mode, topic_id,
                              caller: str = "monitor") -> int | None:
        is_private = (caller == "private") or (target_chat_id > 0)
        logger.info("策略1: msg=%s 收到调用 caller=%s is_private=%s chat=%s mode=%s",
                    msg_id, caller, is_private, source_chat_id, mode)
        try:
            source_ref = await self._resolve_source_for_bot(source_chat_id)
            if mode == "forward":
                result = await self.bot.forward_messages(
                    target_chat_id, msg_id, source_ref)
                return result.id if result else None

            # mode == "copy":
            # 1. 优先尝试未受限频道原生无痕转发 (drop_author=True)，所有 caller 均生效 [保留全套切片与 HLS]
            can_drop_author = True
            try:
                source_entity = await self.bot.get_entity(source_ref)
                if getattr(source_entity, "noforwards", False):
                    can_drop_author = False
            except Exception:
                pass

            if can_drop_author:
                try:
                    logger.info("策略1: msg=%s 命中未受限频道，执行无痕原生转发(drop_author=True caller=%s) [保留原生全套画质切片]",
                                msg_id, caller)
                    result = await self._forward_messages_drop_author(
                        target_chat_id, msg_id, source_ref, topic_id=topic_id)
                    res_id = getattr(result, "id", None)
                    if res_id:
                        logger.info("策略1: msg=%s 无痕原生转发成功 -> target_msg=%s (全套画质切片保留 caller=%s)",
                                    msg_id, res_id, caller)
                        return res_id
                except errors.ChatForwardsRestrictedError:
                    logger.info("策略1: msg=%s 转发受限(ChatForwardsRestrictedError)，转入受限处理流程", msg_id)
                except Exception as e:
                    logger.info("策略1: msg=%s 无痕转发异常(%s: %s)，转入受限处理流程", msg_id, type(e).__name__, e)

            # 2. 受限或无痕转发失败：获取源消息
            msg = await self._get_single_message_for_bot(source_chat_id, source_ref, msg_id)
            if not msg:
                logger.info("策略1: msg=%s Bot 无法获取消息", msg_id)
                return None

            # 私聊特化逻辑：>300MB 视频切片提取（带体积反超熔断保护）
            if is_private:
                doc = getattr(getattr(msg, "media", None), "document", None)
                sz_bytes = getattr(doc, "size", 0)
                sz_mb = sz_bytes / 1024 / 1024
                if doc and sz_bytes > 300 * 1024 * 1024:
                    alt_res = self._select_best_alt_video(msg)
                    if alt_res:
                        best_alt, res_str, codec_str = alt_res
                        alt_sz_bytes = getattr(best_alt, "size", 0)
                        alt_sz_mb = alt_sz_bytes / 1024 / 1024
                        # 检查切片体积是否反超母文件 (需求 2)
                        if alt_sz_bytes > sz_bytes:
                            logger.info(
                                "策略1[私聊]: msg=%s 切片大小(%.1fMB) > 母文件大小(%.1fMB) -> 【放弃切片: 体积反超母文件】，回退发送原母文件",
                                msg_id, alt_sz_mb, sz_mb
                            )
                        else:
                            logger.info(
                                "策略1[私聊]: msg=%s 命中>300MB大文件规则 (母文件=%.1fMB) -> 【已选择切片】(清晰度=%s, 编码=%s, 切片大小=%.1fMB, id=%s)",
                                msg_id, sz_mb, res_str, codec_str, alt_sz_mb, best_alt.id
                            )
                            result = await self.bot.send_file(
                                target_chat_id, best_alt,
                                caption=msg.text or "",
                                supports_streaming=True,
                                **self._reply_kwargs(topic_id)
                            )
                            return result.id if result else None
                    else:
                        logger.info(
                            "策略1[私聊]: msg=%s 文件大小=%.1fMB > 300MB -> 【未选择切片: 无可用MP4切片】，回退常规复制母文件",
                            msg_id, sz_mb
                        )
                elif doc:
                    logger.info(
                        "策略1[私聊]: msg=%s 文件大小=%.1fMB <= 300MB -> 【未选择切片: 体积未超限】，走常规复制母文件",
                        msg_id, sz_mb
                    )

            # 常规复制（非私聊或切片放弃/回退）
            result = await self._copy_message(self.bot, msg, target_chat_id, topic_id)
            return result.id if result else None
        except (errors.ChatForwardsRestrictedError,
                errors.ChannelPrivateError,
                errors.ChatAdminRequiredError) as e:
            logger.info("策略1: msg=%s Bot 无权限: %s", msg_id, type(e).__name__)
            return None
        except errors.FloodWaitError as e:
            return await self._handle_flood(e, self._try_bot_direct,
                                            source_chat_id, msg_id,
                                            target_chat_id, mode, topic_id, caller)
        except Exception as e:
            logger.warning("策略1: msg=%s Bot 异常: %s", msg_id, e)
            return None

    async def _try_bot_direct_album(self, source_chat_id, msg_ids,
                                    target_chat_id, mode, topic_id,
                                    caller: str = "monitor") -> list[int]:
        is_private = (caller == "private") or (target_chat_id > 0)
        logger.info("策略1相册: msgs=%s 收到调用 caller=%s is_private=%s chat=%s mode=%s",
                    msg_ids, caller, is_private, source_chat_id, mode)
        try:
            source_ref = await self._resolve_source_for_bot(source_chat_id)
            if mode == "forward":
                result = await self.bot.forward_messages(
                    target_chat_id, msg_ids, source_ref)
                return self._extract_result_ids(result)

            # mode == "copy":
            # 1. 优先尝试未受限频道原生无痕相册转发 (drop_author=True)，所有 caller 均生效
            can_drop_author = True
            try:
                source_entity = await self.bot.get_entity(source_ref)
                if getattr(source_entity, "noforwards", False):
                    can_drop_author = False
            except Exception:
                pass

            if can_drop_author:
                try:
                    logger.info("策略1相册: msgs=%s 命中未受限频道，执行无痕原生相册转发(drop_author=True caller=%s)",
                                msg_ids, caller)
                    result = await self._forward_messages_drop_author(
                        target_chat_id, msg_ids, source_ref, topic_id=topic_id)
                    res_ids = self._extract_result_ids(result)
                    if res_ids:
                        logger.info("策略1相册: msgs=%s 无痕原生相册转发成功 -> target_msgs=%s (caller=%s)",
                                    msg_ids, res_ids, caller)
                        return res_ids
                except errors.ChatForwardsRestrictedError:
                    logger.info("策略1相册: msgs=%s 转发受限，转入受限处理流程", msg_ids)
                except Exception as e:
                    logger.info("策略1相册: msgs=%s 无痕相册转发异常(%s: %s)，转入受限处理流程",
                                msg_ids, type(e).__name__, e)

            # 2. 受限或无痕转发失败：获取消息列表
            msgs = await self._get_message_list_for_bot(source_chat_id, source_ref, msg_ids)
            if not msgs:
                logger.info("策略1相册: Bot 无法获取消息 %s", msg_ids)
                return []

            if is_private:
                for m in msgs:
                    doc = getattr(getattr(m, "media", None), "document", None)
                    sz_bytes = getattr(doc, "size", 0)
                    sz_mb = sz_bytes / 1024 / 1024
                    if doc and sz_bytes > 300 * 1024 * 1024:
                        alt_res = self._select_best_alt_video(m)
                        if alt_res:
                            best_alt, res_str, codec_str = alt_res
                            alt_sz_bytes = getattr(best_alt, "size", 0)
                            alt_sz_mb = alt_sz_bytes / 1024 / 1024
                            if alt_sz_bytes > sz_bytes:
                                logger.info(
                                    "策略1相册[私聊]: 子消息 msg=%s 切片大小(%.1fMB) > 母文件大小(%.1fMB) -> 【放弃切片: 体积反超母文件】，保留原母文件",
                                    m.id, alt_sz_mb, sz_mb
                                )
                            else:
                                logger.info(
                                    "策略1相册[私聊]: 子消息 msg=%s (%.1fMB) 命中>300MB规则 -> 【已选择切片】(%s, %s, %.1fMB, id=%s)",
                                    m.id, sz_mb, res_str, codec_str, alt_sz_mb, best_alt.id
                                )
                                m.media = MessageMediaDocument(document=best_alt)
                        else:
                            logger.info("策略1相册[私聊]: 子消息 msg=%s (%.1fMB) -> 【未选择切片: 无可用MP4切片】",
                                        m.id, sz_mb)
                    elif doc:
                        logger.info("策略1相册[私聊]: 子消息 msg=%s (%.1fMB) -> 【未选择切片: 体积未超限】",
                                    m.id, sz_mb)

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
                source_chat_id, msg_ids, target_chat_id, mode, topic_id, caller)
        except Exception as e:
            logger.warning("策略1相册: Bot 异常: %s", e)
            return []

    async def _try_userbot_read_bot_forward(self, source_chat_id, msg_id,
                                            target_chat_id, mode, topic_id,
                                            caller: str = "monitor") -> int | None:
        try:
            msg = await self._get_single_message(self.userbot, source_chat_id, msg_id)
            if not msg:
                logger.info("策略2: msg=%s UserBot 无法获取消息", msg_id)
                return None
            if mode == "forward":
                result = await self.userbot.forward_messages(
                    target_chat_id, msg_id, source_chat_id)
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
                                            target_chat_id, mode, topic_id, caller)
        except Exception as e:
            logger.warning("策略2: msg=%s 异常: %s", msg_id, e)
            return None

    async def _try_userbot_read_bot_forward_album(self, source_chat_id, msg_ids,
                                                  target_chat_id, mode, topic_id,
                                                  caller: str = "monitor") -> list[int]:
        try:
            msgs = await self._get_message_list(self.userbot, source_chat_id, msg_ids)
            if not msgs:
                logger.info("策略2相册: UserBot 无法获取消息 %s", msg_ids)
                return []
            if mode == "forward":
                result = await self.userbot.forward_messages(
                    target_chat_id, msg_ids, source_chat_id)
            else:
                result = await self._copy_album(self.bot, msgs, target_chat_id, topic_id)
            return self._extract_result_ids(result)
        except errors.ChatForwardsRestrictedError:
            logger.info("策略2相册: 转发受限")
            return []
        except errors.FloodWaitError as e:
            return await self._handle_flood(
                e, self._try_userbot_read_bot_forward_album,
                source_chat_id, msg_ids, target_chat_id, mode, topic_id, caller)
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
                has_video = any(self.media.is_video_message(m) for m in media_msgs)
                logger.info("策略3相册上传: 待发 %d 个文件, has_video=%s supports_streaming=%s",
                            len(files), has_video, has_video)
                result = await self.bot.send_file(
                    target_chat_id,
                    files,
                    caption=captions,
                    reply_to=reply_to,
                    part_size_kb=self.upload_part_size_kb,
                    supports_streaming=has_video,
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
            send_kwargs = {}
            is_video = self.media.is_video_message(msg)
            if is_video:
                send_kwargs["supports_streaming"] = True
                self.media.ensure_video_streaming(msg)
                attrs = self.media.get_document_attributes(msg)
                if attrs:
                    send_kwargs["attributes"] = attrs
            logger.info("单条消息复制: msg=%s has_video=%s supports_streaming=%s",
                        msg.id, is_video, bool(send_kwargs.get("supports_streaming")))
            return await client.send_file(
                target_chat_id, msg.media,
                caption=msg.text or "",
                reply_to=reply_to,
                **send_kwargs)
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

        has_video = any(self.media.is_video_message(m) for m in media_msgs)
        for m in media_msgs:
            if self.media.is_video_message(m):
                self.media.ensure_video_streaming(m)

        if len(media_msgs) == 1:
            m = media_msgs[0]
            send_kwargs = {}
            is_video = self.media.is_video_message(m)
            if is_video:
                send_kwargs["supports_streaming"] = True
                attrs = self.media.get_document_attributes(m)
                if attrs:
                    send_kwargs["attributes"] = attrs
            logger.info("相册单条复制: msg=%s has_video=%s supports_streaming=%s",
                        m.id, is_video, bool(send_kwargs.get("supports_streaming")))
            return await client.send_file(
                target_chat_id, m.media, caption=m.text or "", reply_to=reply_to, **send_kwargs)

        files = [m.media for m in media_msgs]
        captions = [m.text or "" for m in media_msgs]
        send_kwargs = {}
        if has_video:
            send_kwargs["supports_streaming"] = True
        logger.info("相册多条复制: count=%d has_video=%s supports_streaming=%s",
                    len(media_msgs), has_video, bool(send_kwargs.get("supports_streaming")))
        return await client.send_file(
            target_chat_id, files, caption=captions, reply_to=reply_to, **send_kwargs)

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

    async def _get_single_message_for_bot(
        self,
        source_chat_id,
        chat_or_entity,
        msg_id: int,
    ) -> Message | None:
        msg = await self._get_single_message(self.bot, chat_or_entity, msg_id)
        if msg:
            return msg
        logger.info(
            "策略1探测: 高层 get_messages 未命中 chat=%s msg=%s ref=%s",
            source_chat_id, msg_id, self._describe_peer(chat_or_entity),
        )
        msgs = await self._raw_get_messages_for_bot(source_chat_id, chat_or_entity, [msg_id])
        return msgs[0] if msgs else None

    async def _get_message_list(self, client: TelegramClient,
                                chat_or_entity, msg_ids: list[int]) -> list[Message]:
        msgs = await client.get_messages(chat_or_entity, ids=msg_ids)
        return normalize_messages(msgs)

    async def _get_message_list_for_bot(
        self,
        source_chat_id,
        chat_or_entity,
        msg_ids: list[int],
    ) -> list[Message]:
        msgs = await self._get_message_list(self.bot, chat_or_entity, msg_ids)
        found_ids = {m.id for m in msgs if getattr(m, "id", None) is not None}
        missing_ids = [mid for mid in msg_ids if mid not in found_ids]
        if not missing_ids:
            return msgs

        logger.info(
            "策略1探测: 高层 get_messages 部分未命中 chat=%s want=%s got=%s ref=%s missing=%s",
            source_chat_id, msg_ids, sorted(found_ids), self._describe_peer(chat_or_entity), missing_ids,
        )
        raw_msgs = await self._raw_get_messages_for_bot(source_chat_id, chat_or_entity, missing_ids)
        merged: dict[int, Message] = {
            m.id: m for m in msgs if getattr(m, "id", None) is not None
        }
        for msg in raw_msgs:
            if getattr(msg, "id", None) is not None:
                merged[msg.id] = msg
        return [merged[mid] for mid in msg_ids if mid in merged]

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

    async def _raw_get_messages_for_bot(
        self,
        source_chat_id,
        chat_or_entity,
        msg_ids: list[int],
    ) -> list[Message]:
        input_channel = await self._resolve_input_channel_for_bot(chat_or_entity, source_chat_id)
        if not input_channel:
            logger.info(
                "策略1探测: 无法构造 InputChannel chat=%s ref=%s",
                source_chat_id, self._describe_peer(chat_or_entity),
            )
            return []

        try:
            result = await self.bot(GetMessagesRequest(
                channel=input_channel,
                id=[InputMessageID(mid) for mid in msg_ids],
            ))
        except Exception as e:
            logger.info(
                "策略1探测: channels.getMessages 失败 chat=%s msg_ids=%s peer=%s err=%s",
                source_chat_id, msg_ids, self._describe_peer(input_channel), e,
            )
            return []

        messages = normalize_messages(getattr(result, "messages", None))
        valid = [
            m for m in messages
            if getattr(m, "id", None) is not None and type(m).__name__ != "MessageEmpty"
        ]
        logger.info(
            "策略1探测: channels.getMessages chat=%s msg_ids=%s peer=%s -> %s",
            source_chat_id, msg_ids, self._describe_peer(input_channel),
            [getattr(m, "id", None) for m in valid],
        )
        return valid

    async def _resolve_input_channel_for_bot(self, chat_or_entity, source_chat_id):
        candidates = [chat_or_entity]
        if source_chat_id not in candidates:
            candidates.append(source_chat_id)

        for candidate in candidates:
            try:
                input_peer = await self.bot.get_input_entity(candidate)
            except Exception:
                continue
            if hasattr(input_peer, "channel_id") and hasattr(input_peer, "access_hash"):
                return input_peer

        try:
            entity = await self.bot.get_entity(chat_or_entity)
        except Exception:
            entity = None
        if isinstance(entity, Channel):
            try:
                return await self.bot.get_input_entity(entity)
            except Exception:
                pass
        return None

    @staticmethod
    def _describe_peer(peer) -> str:
        if peer is None:
            return "None"
        fields = []
        for name in ("channel_id", "chat_id", "user_id", "access_hash", "username", "title"):
            value = getattr(peer, name, None)
            if value is not None:
                fields.append(f"{name}={value}")
        return f"{type(peer).__name__}({', '.join(fields)})" if fields else type(peer).__name__

