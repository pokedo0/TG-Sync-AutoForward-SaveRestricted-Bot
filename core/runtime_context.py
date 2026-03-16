"""构建转发运行时依赖，减少 Syncer/Monitor 初始化重复。"""
from dataclasses import dataclass

from telethon import TelegramClient

from core.forwarder import Forwarder
from core.rate_limiter import RateLimiter


@dataclass(slots=True)
class ForwardRuntime:
    rl: RateLimiter
    forwarder: Forwarder


def build_forward_runtime(
    bot: TelegramClient,
    userbot: TelegramClient,
    config: dict,
) -> ForwardRuntime:
    rl = RateLimiter(config)
    forwarder = Forwarder(bot, userbot, config, rl)
    return ForwardRuntime(rl=rl, forwarder=forwarder)
