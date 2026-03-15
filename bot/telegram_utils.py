"""Telegram entity resolution helpers.

Utilities for resolving chat names, topic names, building source links,
and other display-related helpers extracted from handlers.py.
"""

from __future__ import annotations

from telethon import TelegramClient
from telethon.tl.types import Channel
from telethon.tl.functions.channels import GetForumTopicsByIDRequest

from bot.link_parser import ParsedLink


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def truncate(text: str, max_len: int) -> str:
    """将 *text* 截断到 *max_len* 字符，超出部分用省略号替代。"""
    if len(text) <= max_len:
        return text
    return text[: max(max_len - 1, 0)] + "\u2026"


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------

async def resolve_chat_name(client: TelegramClient, chat_id: int) -> str:
    """解析 chat_id 为可读名称，如 '@username' 或 '群组标题'。"""
    try:
        entity = await client.get_entity(chat_id)
        if hasattr(entity, "username") and entity.username:
            return f"@{entity.username}"
        if hasattr(entity, "title") and entity.title:
            return entity.title
        if hasattr(entity, "first_name"):
            return entity.first_name
    except Exception:
        pass
    return str(chat_id)


async def resolve_topic_name(
    client: TelegramClient, chat_id: int, topic_id: int
) -> str:
    """解析 topic_id 为话题标题。"""
    try:
        result = await client(
            GetForumTopicsByIDRequest(channel=chat_id, topics=[topic_id])
        )
        if result.topics:
            return result.topics[0].title
    except Exception:
        pass
    return f"#{topic_id}"


async def describe_source(
    client: TelegramClient, chat_id: int, parsed: ParsedLink
) -> str:
    """获取源的类型描述，如 '公开频道 @xxx' / '私有群组'。"""
    try:
        entity = await client.get_entity(chat_id)
        parts: list[str] = []
        if isinstance(entity, Channel):
            if entity.broadcast:
                parts.append("频道")
            else:
                parts.append("群组")
            if entity.username:
                parts.append(f"公开 @{entity.username}")
            else:
                parts.append(f"私有 {entity.title}")
        else:
            title = getattr(entity, "title", "")
            parts.append(f"群组 {title}" if title else "群组")
        if parsed.topic_id:
            topic_name = await resolve_topic_name(
                client, chat_id, parsed.topic_id
            )
            parts.append(f"话题「{topic_name}」")
        return " | ".join(parts)
    except Exception:
        kind = "私有" if parsed.is_private else "公开"
        return f"{kind} (ID: {chat_id})"


def get_target_topic_id(event) -> int | None:
    """从消息中提取当前所在的 topic ID。"""
    reply_to = getattr(event.message, "reply_to", None)
    if not reply_to:
        return None
    top_id = getattr(reply_to, "reply_to_top_id", None)
    if top_id:
        return top_id
    if getattr(reply_to, "forum_topic", False):
        return getattr(reply_to, "reply_to_msg_id", None)
    return None


def build_source_link(
    chat_id: int, topic_id: int | None = None
) -> str | None:
    """根据 chat_id 构建源链接。"""
    raw = str(chat_id)
    if raw.startswith("-100"):
        raw = raw[4:]
    elif raw.startswith("-"):
        raw = raw[1:]
    if topic_id:
        return f"https://t.me/c/{raw}/{topic_id}"
    return f"https://t.me/c/{raw}"
