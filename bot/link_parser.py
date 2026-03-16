"""Telegram 链接解析器：从各种格式的链接中提取 chat_id, msg_id, topic_id 等。"""
import logging
import re
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.tl.types import Channel
from telethon.tl.functions.channels import GetFullChannelRequest

logger = logging.getLogger("tg_forward_bot.link_parser")


@dataclass
class ParsedLink:
    chat_id: int | str | None = None  # 数字 ID 或 username
    msg_id: int | None = None
    topic_id: int | None = None
    comment_id: int | None = None
    is_private: bool = False
    single: bool = False  # ?single 标记，只取集合中的单条

    @property
    def has_topic(self) -> bool:
        return self.topic_id is not None


# 公开频道/群组: https://t.me/channel 或 https://t.me/channel/123
_PUBLIC_MSG_RE = re.compile(
    r"https?://t\.me/([a-zA-Z_]\w{3,})/(\d+)")
_PUBLIC_CHAT_RE = re.compile(
    r"https?://t\.me/([a-zA-Z_]\w{3,})/?$")

# 私有频道: https://t.me/c/123456 或 /c/123456/789 或 /c/123456/3/962
_PRIVATE_RE = re.compile(
    r"https?://t\.me/c/(\d+)(?:/(\d+))?(?:/(\d+))?")


def parse_link(url: str) -> ParsedLink | None:
    """解析 Telegram 消息链接。"""
    # 检测 ?single 标记
    is_single = "single" in url
    # 清理查询参数，保留 comment
    comment_id = None
    comment_match = re.search(r"[?&]comment=(\d+)", url)
    if comment_match:
        comment_id = int(comment_match.group(1))
    # 去掉所有查询参数，只保留路径
    url = url.split("?")[0].split("#")[0]

    # 私有频道链接: /c/chat_id, /c/chat_id/msg_id, /c/chat_id/topic_id/msg_id
    m = _PRIVATE_RE.match(url)
    if m:
        chat_id = int(m.group(1))
        second = int(m.group(2)) if m.group(2) else None
        third = int(m.group(3)) if m.group(3) else None

        if second is not None and third is not None:
            # /c/chat_id/topic_id/msg_id
            return ParsedLink(
                chat_id=-1000000000000 - chat_id,
                topic_id=second, msg_id=third,
                comment_id=comment_id, is_private=True,
                single=is_single)
        elif second is not None:
            # /c/chat_id/msg_id (或仅 topic 场景由调用方判断)
            return ParsedLink(
                chat_id=-1000000000000 - chat_id,
                msg_id=second, comment_id=comment_id,
                is_private=True, single=is_single)
        else:
            # /c/chat_id (纯频道链接)
            return ParsedLink(
                chat_id=-1000000000000 - chat_id,
                is_private=True, single=is_single)

    # 公开频道带消息 ID: /channel/123
    m = _PUBLIC_MSG_RE.match(url)
    if m:
        return ParsedLink(
            chat_id=m.group(1),
            msg_id=int(m.group(2)),
            comment_id=comment_id,
            is_private=False, single=is_single)

    # 公开频道纯链接: /channel
    m = _PUBLIC_CHAT_RE.match(url)
    if m:
        return ParsedLink(
            chat_id=m.group(1),
            is_private=False, single=is_single)

    return None


async def resolve_chat_id(client: TelegramClient, parsed: ParsedLink) -> int | None:
    """将 username 解析为带 -100 前缀的完整 chat_id。"""
    if isinstance(parsed.chat_id, int):
        return parsed.chat_id
    try:
        entity = await client.get_entity(parsed.chat_id)
        # 频道和超级群组需要 -100 前缀
        if isinstance(entity, Channel):
            full_id = -1000000000000 - entity.id
            logger.info("解析 %s -> channel ID %s", parsed.chat_id, full_id)
            return full_id
        logger.info("解析 %s -> ID %s", parsed.chat_id, entity.id)
        return entity.id
    except Exception as e:
        logger.warning("解析 %s 失败: %s", parsed.chat_id, e)
        return None


async def resolve_linked_chat(client: TelegramClient,
                              chat_id: int) -> int | None:
    """获取频道关联的讨论群组 ID（评论区所在群）。"""
    try:
        entity = await client.get_entity(chat_id)
        if not isinstance(entity, Channel) or not entity.broadcast:
            return None
        full = await client(GetFullChannelRequest(entity))
        linked = full.full_chat.linked_chat_id
        if linked:
            full_id = -1000000000000 - linked
            logger.info("频道 %s 的讨论群: %s", chat_id, full_id)
            return full_id
    except Exception as e:
        logger.warning("获取频道 %s 关联讨论群失败: %s", chat_id, e)
    return None
