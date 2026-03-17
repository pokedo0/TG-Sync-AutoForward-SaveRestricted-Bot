"""Dump Telegram chat/topic/message raw response bodies to a JSONL file.

Usage examples:
  python test/dump_link_responses.py "https://t.me/c/3195694920/3/"
  python test/dump_link_responses.py "https://t.me/c/123456/789"
  python test/dump_link_responses.py "https://t.me/some_channel"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from telethon import TelegramClient
from telethon.errors import FloodWaitError

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from bot.link_parser import parse_link, resolve_chat_id


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


def _is_private_topic_link(url: str) -> bool:
    cleaned = url.split("?", 1)[0].split("#", 1)[0]
    return re.fullmatch(r"https?://t\.me/c/\d+/\d+/?", cleaned) is not None and cleaned.endswith("/")


def _ensure_out_path(path: Path | None, input_link: str) -> Path:
    if path:
        resolved = path if path.is_absolute() else project_root / path
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", input_link).strip("_")[:80] or "tg_dump"
    out = project_root / "test" / "dumps" / f"{stamp}_{slug}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def _resolve_config_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return project_root / path


async def dump_link(link: str, config_path: Path, out_path: Path) -> dict[str, Any]:
    cfg = load_config(config_path)
    api_id = cfg["api_id"]
    api_hash = cfg["api_hash"]
    phone = cfg.get("phone")
    if not phone:
        raise RuntimeError("config.yaml 缺少 phone")

    parsed = parse_link(link)
    if not parsed:
        raise ValueError("无法解析链接")

    treat_as_topic = False
    if parsed.is_private and parsed.msg_id and not parsed.topic_id and _is_private_topic_link(link):
        treat_as_topic = True
        parsed.topic_id = parsed.msg_id
        parsed.msg_id = None

    session_path = project_root / "sessions" / "userbot"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.start(phone=phone)
    try:
        source_chat_id = await resolve_chat_id(client, parsed)
        if source_chat_id is None:
            raise RuntimeError("无法解析 source chat id")

        fetch_mode = "chat"
        fetch_target: dict[str, Any] = {"chat_id": source_chat_id}
        iter_kwargs: dict[str, Any] = {"reverse": True}
        if parsed.topic_id:
            fetch_mode = "topic"
            fetch_target["topic_id"] = parsed.topic_id
            iter_kwargs["reply_to"] = parsed.topic_id
        elif parsed.msg_id:
            fetch_mode = "message"
            fetch_target["msg_id"] = parsed.msg_id

        chat_entity = await client.get_entity(source_chat_id)

        with out_path.open("w", encoding="utf-8") as fp:
            header = {
                "record_type": "meta",
                "input_link": link,
                "treat_as_topic_from_trailing_slash": treat_as_topic,
                "parsed_link": to_jsonable(parsed),
                "resolved_source_chat_id": source_chat_id,
                "fetch_mode": fetch_mode,
                "fetch_target": fetch_target,
                "chat_response_type": type(chat_entity).__name__ if chat_entity else None,
                "chat_response_body": to_jsonable(chat_entity),
                "dump_started_at": datetime.now().isoformat(),
            }
            fp.write(json.dumps(header, ensure_ascii=False) + "\n")

            count = 0
            if fetch_mode == "message":
                msg = await client.get_messages(source_chat_id, ids=parsed.msg_id)
                record = {
                    "record_type": "message",
                    "id": parsed.msg_id,
                    "message_response_type": type(msg).__name__ if msg else None,
                    "message_response_body": to_jsonable(msg),
                }
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                count = 1 if msg else 0
            else:
                while True:
                    try:
                        async for msg in client.iter_messages(source_chat_id, **iter_kwargs):
                            record = {
                                "record_type": "message",
                                "id": getattr(msg, "id", None),
                                "message_response_type": type(msg).__name__ if msg else None,
                                "message_response_body": to_jsonable(msg),
                            }
                            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                            count += 1
                        break
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds)

            tail = {
                "record_type": "summary",
                "messages_dumped": count,
                "dump_finished_at": datetime.now().isoformat(),
            }
            fp.write(json.dumps(tail, ensure_ascii=False) + "\n")

        return {
            "ok": True,
            "out_file": str(out_path),
            "fetch_mode": fetch_mode,
            "messages_dumped": count,
        }
    finally:
        await client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="输入频道/群组/话题链接，导出 chat + 全量 message 的原始响应到 JSONL 文件。"
    )
    parser.add_argument("link", help="Telegram 链接")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="配置文件路径（默认: config.yaml）",
    )
    parser.add_argument(
        "--out",
        default="",
        help="输出文件路径（默认自动生成到 test/dumps/*.jsonl）",
    )
    return parser


async def _main() -> int:
    args = build_parser().parse_args()
    config_path = _resolve_config_path(args.config)
    out_path = _ensure_out_path(Path(args.out) if args.out else None, args.link)
    try:
        result = await dump_link(args.link, config_path, out_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        err = {
            "ok": False,
            "error_type": type(e).__name__,
            "error": str(e),
            "out_file": str(out_path),
        }
        print(json.dumps(err, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
