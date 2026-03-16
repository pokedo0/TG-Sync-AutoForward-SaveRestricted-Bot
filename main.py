import os
import asyncio
import logging
import yaml
from telethon import TelegramClient
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

from db.database import Database
from bot.handlers import register_handlers
from core.monitor import MonitorManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("tg_forward_bot")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


async def main():
    config = load_config()

    # 确保所需目录存在
    os.makedirs("data", exist_ok=True)
    os.makedirs("sessions", exist_ok=True)

    # 初始化数据库
    db = Database("data/bot.db")
    await db.init()
    logger.info("数据库初始化完成")

    # 初始化双客户端
    bot = TelegramClient("sessions/bot", config["api_id"], config["api_hash"])
    userbot = TelegramClient("sessions/userbot", config["api_id"], config["api_hash"])

    await bot.start(bot_token=config["bot_token"])
    logger.info("Bot 客户端已启动")

    # UserBot 登录：首次需要输入验证码，之后 session 持久化
    phone = config.get("phone")
    if not phone:
        logger.error("config.yaml 中未配置 phone")
        return
    await userbot.start(phone=phone)
    me = await userbot.get_me()
    logger.info("UserBot 已登录: %s (ID: %s)", me.first_name, me.id)

    # 设置 Bot 命令菜单（覆盖旧命令）
    await bot(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=[
            BotCommand(command="start", description="开始使用"),
            BotCommand(command="help", description="使用说明"),
            BotCommand(command="sync", description="同步历史消息到当前群"),
            BotCommand(command="monitor", description="监控新消息转发到当前群"),
            BotCommand(command="list", description="管理所有任务"),
            BotCommand(command="settings", description="查看限流配置"),
        ],
    ))
    logger.info("Bot 命令菜单已更新")

    # 注册 Bot 命令处理器
    monitor_manager = MonitorManager(bot, userbot, db, config)
    register_handlers(bot, userbot, db, config, monitor_manager)
    logger.info("命令处理器已注册")

    # 恢复之前运行中的 monitor 任务
    await monitor_manager.restore_tasks()

    logger.info("所有服务就绪，等待消息...")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
