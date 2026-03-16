"""Inspect Telegram API response body by message link.

Usage:
  python test/inspect_link_response.py "https://t.me/xxxx/123"
  python test/inspect_link_response.py "https://t.me/c/123456/789?comment=1" --pretty
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import yaml
from telethon import TelegramClient

from bot.link_parser import parse_link, resolve_chat_id, resolve_linked_chat


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def to_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if hasattr(value, "to_dict"):
        return to_jsonable(value.to_dict())
    if isinstance(value, (list, tuple)):
        return [to_jsonable(x) for x in value]
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    return value


async def inspect_link(link: str, config_path: Path) -> dict[str, Any]:
    cfg = load_config(config_path)
    api_id = cfg["api_id"]
    api_hash = cfg["api_hash"]
    phone = cfg.get("phone")
    if not phone:
        raise RuntimeError("config.yaml 缺少 phone")

    parsed = parse_link(link)
    if not parsed:
        raise ValueError("无法解析链接")
    if not parsed.msg_id:
        raise ValueError("链接缺少消息 ID")

    client = TelegramClient("sessions/userbot", api_id, api_hash)
    await client.start(phone=phone)
    try:
        source_chat_id = await resolve_chat_id(client, parsed)
        if source_chat_id is None:
            raise RuntimeError("无法解析 source chat id")

        fetch_chat_id = source_chat_id
        fetch_msg_id = parsed.msg_id
        if parsed.comment_id:
            linked = await resolve_linked_chat(client, source_chat_id)
            if linked is None:
                raise RuntimeError("comment 链接无法解析 discussion chat")
            fetch_chat_id = linked
            fetch_msg_id = parsed.comment_id

        chat_entity = await client.get_entity(fetch_chat_id)
        message = await client.get_messages(fetch_chat_id, ids=fetch_msg_id)

        return {
            "input_link": link,
            "parsed_link": to_jsonable(parsed),
            "resolved_source_chat_id": source_chat_id,
            "fetch_target": {
                "chat_id": fetch_chat_id,
                "msg_id": fetch_msg_id,
            },
            "chat_response_type": type(chat_entity).__name__ if chat_entity else None,
            "chat_response_body": to_jsonable(chat_entity),
            "message_response_type": type(message).__name__ if message else None,
            "message_response_body": to_jsonable(message),
        }
    finally:
        await client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="根据 Telegram 消息链接打印 chat/message 原始回应体。"
    )
    parser.add_argument("link", help="Telegram 消息链接")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="配置文件路径（默认: config.yaml）",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="格式化 JSON 输出",
    )
    return parser


async def _main() -> int:
    args = build_parser().parse_args()
    try:
        result = await inspect_link(args.link, Path(args.config))
        if args.pretty:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as e:
        err = {
            "ok": False,
            "error_type": type(e).__name__,
            "error": str(e),
        }
        print(json.dumps(err, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
