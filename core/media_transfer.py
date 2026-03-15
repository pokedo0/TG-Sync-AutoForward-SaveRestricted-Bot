"""媒体下载与上传兼容辅助。"""
import os

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo, Message, MessageMediaDocument


class MediaTransferHelper:
    def __init__(self, bot: TelegramClient, userbot: TelegramClient,
                 upload_part_size_kb: int, download_part_size_kb: int):
        self.bot = bot
        self.userbot = userbot
        self.upload_part_size_kb = upload_part_size_kb
        self.download_part_size_kb = download_part_size_kb

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

    async def download_media_to_path(self, msg: Message, tmpdir: str) -> str | None:
        path = self.build_download_target_path(msg, tmpdir)
        return await self._download_media_with_compat(msg, file=path)

    async def download_video_thumb_to_path(self, msg: Message, tmpdir: str) -> str | None:
        if not self.is_video_message(msg):
            return None
        cover = self._get_message_video_cover(msg)
        if cover:
            cover_base = os.path.join(tmpdir, f"{msg.id}_cover")
            try:
                return await self._download_media_with_compat(cover, file=cover_base)
            except Exception:
                pass

        if not self._has_document_thumbs(msg):
            return None

        thumb_base = os.path.join(tmpdir, f"{msg.id}_thumb")
        try:
            return await self._download_media_with_compat(msg, file=thumb_base, thumb=-1)
        except TypeError:
            return None

    async def _download_media_with_compat(self, media, **kwargs):
        try:
            return await self.userbot.download_media(
                media, part_size_kb=self.download_part_size_kb, **kwargs
            )
        except TypeError:
            return await self.userbot.download_media(media, **kwargs)

    async def send_file_with_compat(self, target_chat_id: int, file, **kwargs):
        try:
            return await self.bot.send_file(target_chat_id, file, **kwargs)
        except TypeError:
            if "video_timestamp" not in kwargs:
                raise
            fallback = dict(kwargs)
            fallback.pop("video_timestamp", None)
            return await self.bot.send_file(target_chat_id, file, **fallback)
