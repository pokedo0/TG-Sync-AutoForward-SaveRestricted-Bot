"""消息判断与分组逻辑（私聊解析 / sync / monitor 共享）。"""
from __future__ import annotations

from telethon import TelegramClient
from telethon.tl.types import Message, MessageMediaPhoto, MessageMediaDocument


def is_file_media(msg: Message | None) -> bool:
    """仅图片/文件视为可 send_file 的媒体。"""
    if not msg:
        return False
    return isinstance(msg.media, (MessageMediaPhoto, MessageMediaDocument))


def is_album_candidate(msg: Message | None, single: bool = False) -> bool:
    """是否应按相册处理。"""
    if not msg or single:
        return False
    return bool(getattr(msg, "grouped_id", None)) and is_file_media(msg)


def classify_message_kind(msg: Message | None, single: bool = False) -> str:
    """
    统一消息类型判断。
    返回值: album | media | text | empty
    """
    if not msg:
        return "empty"
    if is_album_candidate(msg, single=single):
        return "album"
    if is_file_media(msg):
        return "media"
    if msg.text:
        return "text"
    return "empty"


def normalize_messages(msgs) -> list[Message]:
    """将 Telethon 单条/列表返回统一成列表，并过滤 None。"""
    if not msgs:
        return []
    if isinstance(msgs, list):
        return [m for m in msgs if m]
    return [msgs] if msgs else []


def build_forward_units(messages: list[Message]) -> list[tuple[str, list[int]]]:
    """
    将消息序列拆成统一转发单元:
    - ("single", [msg_id])
    - ("album",  [msg_id1, msg_id2, ...])
    """
    units: list[tuple[str, list[int]]] = []
    idx = 0
    while idx < len(messages):
        msg = messages[idx]
        if not is_album_candidate(msg):
            units.append(("single", [msg.id]))
            idx += 1
            continue

        gid = msg.grouped_id
        album_ids = [msg.id]
        j = idx + 1
        while j < len(messages):
            nxt = messages[j]
            if getattr(nxt, "grouped_id", None) != gid or not is_file_media(nxt):
                break
            album_ids.append(nxt.id)
            j += 1
        units.append(("album", album_ids))
        idx = j
    return units


async def collect_album_messages(client: TelegramClient, chat_id: int,
                                 anchor_msg: Message, window: int = 10) -> list[Message]:
    """围绕锚点消息收集同 grouped_id 的相册消息。"""
    if not is_album_candidate(anchor_msg):
        return [anchor_msg] if anchor_msg else []

    search_ids = list(range(anchor_msg.id - window, anchor_msg.id + window + 1))
    nearby = await client.get_messages(chat_id, ids=search_ids)
    nearby_msgs = normalize_messages(nearby)
    album_msgs = [
        m for m in nearby_msgs
        if getattr(m, "grouped_id", None) == anchor_msg.grouped_id and is_file_media(m)
    ]
    return sorted(album_msgs, key=lambda m: m.id)


def has_platform_all_reason(reasons) -> bool:
    """判断 restriction_reason 列表中是否包含 platform='all' 的条目。

    支持 Telethon 原生对象和 dict 两种格式。
    供 Forwarder.detect_restriction / RestrictedSyncer 等共用。
    """
    for reason in reasons or []:
        platform = None
        if isinstance(reason, dict):
            platform = reason.get("platform")
        else:
            platform = getattr(reason, "platform", None)
        if isinstance(platform, str) and platform.lower() == "all":
            return True
    return False


def is_chat_globally_restricted(entity) -> bool:
    """判断 chat 实体是否为全平台封禁（restricted 且 reason.platform=all）。"""
    if not entity or not getattr(entity, "restricted", False):
        return False
    reasons = getattr(entity, "restriction_reason", None) or []
    return has_platform_all_reason(reasons)


def detect_hard_restriction(
        chat_globally_restricted: bool,
        msg: Message | None = None,
) -> tuple[bool, str]:
    """统一硬封禁判定：优先 chat 级，其次 message 级。"""
    if chat_globally_restricted:
        return True, "chat.restricted+reason.platform_all"
    if is_restricted_message(msg):
        return True, "message.reason.platform_all"
    return False, ""


def is_restricted_message(msg: Message | None) -> bool:
    """判断单条消息是否为全平台受限消息（restriction_reason.platform=all）。"""
    if not msg:
        return False
    reasons = getattr(msg, "restriction_reason", None) or []
    return has_platform_all_reason(reasons)
