"""核心组件基类：统一注入 bot/userbot/db/config/forward runtime。"""
from telethon import TelegramClient

from core.runtime_context import build_forward_runtime
from db.database import Database


class ForwardingComponent:
    def __init__(
        self,
        bot: TelegramClient,
        userbot: TelegramClient,
        db: Database,
        config: dict,
    ) -> None:
        self.bot = bot
        self.userbot = userbot
        self.db = db
        self.config = config
        runtime = build_forward_runtime(bot, userbot, config)
        self.rl = runtime.rl
        self.forwarder = runtime.forwarder
