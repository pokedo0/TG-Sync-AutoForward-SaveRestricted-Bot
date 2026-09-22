import asyncio
import logging
import os

from telethon import TelegramClient
from telethon.tl import types
from telethon.tl.types import DocumentAttributeVideo, Message, MessageMediaDocument

from core.fast_telethon import fast_download_file, fast_upload_file

logger = logging.getLogger("tg_forward_bot.media_transfer")


class MediaTransferHelper:
    def __init__(self, bot: TelegramClient, userbot: TelegramClient,
                 upload_part_size_kb: int, download_part_size_kb: int,
                 enable_fast_transfer: bool = True,
                 fast_transfer_connections: int = 4,
                 fast_transfer_min_size_mb: int = 10,
                 fast_download_connections: int | None = None,
                 fast_upload_connections: int | None = None):
        self.bot = bot
        self.userbot = userbot
        self.upload_part_size_kb = upload_part_size_kb
        self.download_part_size_kb = download_part_size_kb
        self.enable_fast_transfer = enable_fast_transfer
        default_conn = max(1, int(fast_transfer_connections))
        self.fast_transfer_connections = default_conn
        self.fast_download_connections = (
            max(1, int(fast_download_connections))
            if fast_download_connections is not None
            else default_conn
        )
        self.fast_upload_connections = (
            max(1, int(fast_upload_connections))
            if fast_upload_connections is not None
            else default_conn
        )
        self.fast_transfer_min_size_mb = max(1, int(fast_transfer_min_size_mb))
        self._large_download_lock = asyncio.Lock()
        self._large_upload_lock = asyncio.Lock()
        logger.info(
            "[MediaTransfer] 初始化配置: enable_fast=%s, download_conn=%d, upload_conn=%d, min_size=%dMB",
            self.enable_fast_transfer, self.fast_download_connections, self.fast_upload_connections, self.fast_transfer_min_size_mb
        )

    @staticmethod
    def is_video_message(msg: Message) -> bool:
        media = getattr(msg, "media", None)
        if not isinstance(media, MessageMediaDocument):
            return False
        document = getattr(media, "document", None)
        if not document:
            return False
        attrs = getattr(document, "attributes", []) or []
        if any(isinstance(a, DocumentAttributeVideo) for a in attrs):
            return True
        mime_type = getattr(document, "mime_type", "") or ""
        return mime_type.startswith("video/")

    @staticmethod
    def get_document_attributes(msg: Message):
        media = getattr(msg, "media", None)
        if not isinstance(media, MessageMediaDocument):
            return None
        document = getattr(media, "document", None)
        if not document:
            return None
        attrs = getattr(document, "attributes", None)
        return attrs or None

    @classmethod
    def ensure_video_streaming(cls, msg: Message):
        """若消息包含视频文档属性，确保其 supports_streaming 标志为 True。"""
        attrs = cls.get_document_attributes(msg)
        if not attrs:
            return
        for attr in attrs:
            if isinstance(attr, DocumentAttributeVideo):
                attr.supports_streaming = True

    @staticmethod
    def _has_document_thumbs(msg: Message) -> bool:
        media = getattr(msg, "media", None)
        if not isinstance(media, MessageMediaDocument):
            return False
        document = getattr(media, "document", None)
        if not document:
            return False
        thumbs = getattr(document, "thumbs", None) or []
        video_thumbs = getattr(document, "video_thumbs", None) or []
        return bool(thumbs or video_thumbs)

    @staticmethod
    def _get_message_video_cover(msg: Message):
        media = getattr(msg, "media", None)
        if not isinstance(media, MessageMediaDocument):
            return None
        return getattr(media, "video_cover", None)

    def _get_message_video_timestamp(self, msg: Message) -> int | None:
        media = getattr(msg, "media", None)
        if isinstance(media, MessageMediaDocument):
            ts = getattr(media, "video_timestamp", None)
            if isinstance(ts, int) and ts >= 0:
                return ts
        attrs = self.get_document_attributes(msg) or []
        for attr in attrs:
            if isinstance(attr, DocumentAttributeVideo):
                ts = getattr(attr, "video_start_ts", None)
                if ts is None:
                    continue
                try:
                    n = int(ts)
                except Exception:
                    continue
                if n >= 0:
                    return n
        return None

    def build_send_file_kwargs(self, msg: Message, reply_to: int | None,
                               thumb_path: str | None = None) -> dict:
        kwargs = {
            "caption": msg.text or "",
            "reply_to": reply_to,
            "part_size_kb": self.upload_part_size_kb,
        }
        if self.is_video_message(msg):
            kwargs["supports_streaming"] = True
            self.ensure_video_streaming(msg)
            attrs = self.get_document_attributes(msg)
            if attrs:
                kwargs["attributes"] = attrs
            if thumb_path:
                kwargs["thumb"] = thumb_path
            ts = self._get_message_video_timestamp(msg)
            if ts is not None:
                kwargs["video_timestamp"] = ts
        return kwargs

    def build_download_target_path(self, msg: Message, tmpdir: str) -> str:
        file_obj = getattr(msg, "file", None)
        ext = None
        if file_obj:
            file_name = getattr(file_obj, "name", None)
            if file_name:
                _, ext = os.path.splitext(file_name)
            if not ext:
                ext = getattr(file_obj, "ext", None)
        if not ext:
            ext = ".mp4" if self.is_video_message(msg) else ".bin"
        return os.path.join(tmpdir, f"{msg.id}{ext}")

    @staticmethod
    def get_message_file_size(msg: Message) -> int | None:
        file_obj = getattr(msg, "file", None)
        if file_obj and getattr(file_obj, "size", None):
            return file_obj.size
        media = getattr(msg, "media", None)
        if isinstance(media, MessageMediaDocument):
            doc = getattr(media, "document", None)
            if doc and getattr(doc, "size", None):
                return doc.size
        return None

    async def download_media_to_path(self, msg: Message, tmpdir: str,
                                     userbot: TelegramClient | None = None) -> str | None:
        path = self.build_download_target_path(msg, tmpdir)
        ub = userbot or self.userbot

        if self.enable_fast_transfer and ub:
            file_size = self.get_message_file_size(msg)
            min_bytes = self.fast_transfer_min_size_mb * 1024 * 1024
            if file_size and file_size >= min_bytes:
                async with self._large_download_lock:
                    try:
                        logger.info(
                            "[MediaTransfer] 尝试 FastTelethon 并发下载: msg_id=%s, 大小=%.2f MB (独占 %d 连接)",
                            msg.id, file_size / (1024 * 1024), self.fast_download_connections,
                        )
                        res_path = await fast_download_file(
                            client=ub,
                            location=msg,
                            out_file_path=path,
                            file_size=file_size,
                            connection_count=self.fast_download_connections,
                        )
                        if res_path and os.path.exists(res_path) and os.path.getsize(res_path) > 0:
                            return res_path
                    except Exception as e:
                        logger.warning(
                            "[MediaTransfer] FastTelethon 并发下载失败，降级回原生下载: msg_id=%s, 错误: %s",
                            msg.id, e,
                        )
                        if os.path.exists(path):
                            try:
                                os.remove(path)
                            except OSError:
                                pass

        return await self._download_media_with_compat(msg, file=path, userbot=userbot)

    async def download_video_thumb_to_path(self, msg: Message, tmpdir: str,
                                           userbot: TelegramClient | None = None) -> str | None:
        if not self.is_video_message(msg):
            return None
        cover = self._get_message_video_cover(msg)
        if cover:
            cover_base = os.path.join(tmpdir, f"{msg.id}_cover")
            try:
                return await self._download_media_with_compat(cover, file=cover_base, userbot=userbot)
            except Exception:
                pass

        if not self._has_document_thumbs(msg):
            return None

        thumb_base = os.path.join(tmpdir, f"{msg.id}_thumb")
        try:
            return await self._download_media_with_compat(msg, file=thumb_base, thumb=-1, userbot=userbot)
        except TypeError:
            return None

    async def _download_media_with_compat(self, media, userbot: TelegramClient | None = None, **kwargs):
        ub = userbot or self.userbot
        try:
            return await ub.download_media(
                media, part_size_kb=self.download_part_size_kb, **kwargs
            )
        except TypeError:
            return await ub.download_media(media, **kwargs)

    async def _fast_upload_if_needed(self, file_path):
        """若单个文件满足并发上传条件则调用 fast_upload_file，否则返回原始输入。"""
        if (
            self.enable_fast_transfer
            and isinstance(file_path, str)
            and os.path.isfile(file_path)
            and not isinstance(file_path, (types.InputFile, types.InputFileBig))
        ):
            try:
                file_size = os.path.getsize(file_path)
                min_bytes = self.fast_transfer_min_size_mb * 1024 * 1024
                if file_size >= min_bytes:
                    async with self._large_upload_lock:
                        logger.info(
                            "[MediaTransfer] 尝试 FastTelethon 并发上传: 文件=%s, 大小=%.2f MB (独占 %d 连接)",
                            os.path.basename(file_path), file_size / (1024 * 1024), self.fast_upload_connections,
                        )
                        input_file = await fast_upload_file(
                            client=self.bot,
                            file_path=file_path,
                            connection_count=self.fast_upload_connections,
                        )
                        if input_file:
                            return input_file
            except Exception as e:
                logger.warning(
                    "[MediaTransfer] FastTelethon 并发上传失败，降级回原生发送: 文件=%s, 错误: %s",
                    os.path.basename(file_path), e,
                )
        return file_path

    async def prepare_album_media_item(
        self,
        msg: Message,
        path: str,
        thumb_path: str | None = None,
    ):
        """预处理相册项：
        - 视频文件：上传缩略图并包装为带完整属性和封面的 InputMediaUploadedDocument，
          同时复用 FastTelethon 进行大文件并发上传。
        - 其他文件：按大小判定是否进行 FastTelethon 并发上传。
        """
        if not self.is_video_message(msg):
            return await self._fast_upload_if_needed(path)

        try:
            # 1. 上传视频本体（大文件并发加速，小文件原生上传）
            file_handle = await self._fast_upload_if_needed(path)
            if not isinstance(file_handle, (types.InputFile, types.InputFileBig)):
                file_handle = await self.bot.upload_file(path)

            # 2. 上传缩略图封面（若存在）
            thumb_handle = None
            if thumb_path and os.path.isfile(thumb_path):
                try:
                    thumb_handle = await self.bot.upload_file(thumb_path)
                except Exception as e:
                    logger.warning("[MediaTransfer] 上传相册视频封面失败: msg=%s err=%s", msg.id, e)

            # 3. 提取并保留原始视频属性（宽高、时长、流式播放等）
            self.ensure_video_streaming(msg)
            raw_attrs = self.get_document_attributes(msg)
            attrs = list(raw_attrs) if raw_attrs else []

            has_video_attr = any(isinstance(a, DocumentAttributeVideo) for a in attrs)
            if not has_video_attr:
                attrs.append(DocumentAttributeVideo(duration=0, w=0, h=0, supports_streaming=True))
            else:
                for a in attrs:
                    if isinstance(a, DocumentAttributeVideo):
                        a.supports_streaming = True

            has_fn_attr = any(isinstance(a, types.DocumentAttributeFilename) for a in attrs)
            if not has_fn_attr:
                attrs.append(types.DocumentAttributeFilename(file_name=os.path.basename(path)))

            mime_type = "video/mp4"
            media = getattr(msg, "media", None)
            if isinstance(media, MessageMediaDocument):
                doc = getattr(media, "document", None)
                if doc and getattr(doc, "mime_type", None):
                    mime_type = doc.mime_type

            logger.info(
                "[MediaTransfer] 相册视频成功组装封面与属性: msg_id=%s, 文件=%s, 携带封面=%s",
                msg.id, os.path.basename(path), bool(thumb_handle),
            )

            return types.InputMediaUploadedDocument(
                file=file_handle,
                mime_type=mime_type,
                attributes=attrs,
                thumb=thumb_handle,
            )
        except Exception as e:
            logger.warning(
                "[MediaTransfer] 组装相册视频封面/属性异常，降级发送裸文件: msg_id=%s, err=%s",
                msg.id, e,
            )
            return await self._fast_upload_if_needed(path)

    async def send_file_with_compat(self, target_chat_id: int, file, **kwargs):
        if isinstance(file, (list, tuple)):
            file_to_send = []
            accelerated_count = 0
            for f in file:
                prepared = await self._fast_upload_if_needed(f)
                if isinstance(prepared, (types.InputFile, types.InputFileBig)):
                    accelerated_count += 1
                file_to_send.append(prepared)
            if accelerated_count > 0:
                logger.info(
                    "[MediaTransfer] 相册文件上传预处理完成: 总数=%d, FastTelethon并发加速数=%d",
                    len(file), accelerated_count,
                )
        else:
            file_to_send = await self._fast_upload_if_needed(file)

        try:
            return await self.bot.send_file(target_chat_id, file_to_send, **kwargs)
        except TypeError:
            if "video_timestamp" not in kwargs:
                raise
            fallback = dict(kwargs)
            fallback.pop("video_timestamp", None)
            return await self.bot.send_file(target_chat_id, file_to_send, **fallback)
