"""handlers 共享类型与轻量工具。"""
import re
from dataclasses import dataclass

from bot.link_parser import ParsedLink

_TG_LINK_RE = re.compile(r"https?://t\.me/\S+")

STATUS_EMOJI = {
    "running": "🟢",
    "paused": "⏸",
    "completed": "✅",
    "failed": "❌",
}


@dataclass
class ParsedSource:
    parsed: ParsedLink
    source_id: int
    mode: str


@dataclass
class FetchTarget:
    chat_id: int
    msg_id: int


def extract_tg_link(text: str) -> str | None:
    match = _TG_LINK_RE.search(text)
    return match.group() if match else None
