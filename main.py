import os
import sys
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

# ---------------------------------------------------------------------------
# 重连策略常量
# ---------------------------------------------------------------------------
MAX_RECONNECT_ATTEMPTS = 20      # 应用层最多重连次数 (20 × 30min = 10h)
RECONNECT_BASE_DELAY = 1800      # 重连间隔 (秒) = 30 分钟
RECONNECT_MAX_DELAY = 1800       # 最大间隔 (秒) = 30 分钟


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


async def _start_clients(bot: TelegramClient, userbot: TelegramClient,
                         config: dict) -> None:
    """启动 Bot 和 UserBot 客户端。"""
    await bot.start(bot_token=config["bot_token"])
    logger.info("Bot 客户端已启动")

    phone = config.get("phone")
    if not phone:
        logger.error("config.yaml 中未配置 phone")
        sys.exit(1)
    await userbot.start(phone=phone)
    me = await userbot.get_me()
    logger.info("UserBot 已登录: %s (ID: %s)", me.first_name, me.id)


async def _setup_bot(bot: TelegramClient, userbot: TelegramClient,
                     db: Database, config: dict) -> MonitorManager:
    """注册命令菜单、处理器，恢复任务。返回 MonitorManager 实例。"""
    # 设置 Bot 命令菜单（覆盖旧命令）
    await bot(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=[
            BotCommand(command="start", description="开始使用"),
            BotCommand(command="help", description="使用说明"),
            BotCommand(command="sync", description="同步历史消息到当前群"),
            BotCommand(command="syncrestrictedmsg", description="通过 Takeout导出数据接口 补发受限消息"),
            BotCommand(command="monitor", description="监控新消息（需UserBot先加入源）"),
            BotCommand(command="list", description="管理所有任务"),
            BotCommand(command="settings", description="查看限流配置"),
        ],
    ))
    logger.info("Bot 命令菜单已更新")

    # 注册 Bot 命令处理器
    monitor_manager = MonitorManager(bot, userbot, db, config)
    register_handlers(bot, userbot, db, config, monitor_manager)
    logger.info("命令处理器已注册")

    # 恢复之前运行中的 monitor 任务；将孤立的 sync 任务自动暂停
    from db import models as _models
    _running = await _models.get_tasks_by_status(db, "running")
    _paused_count = 0
    for _t in _running:
        if _t["type"] in ("sync", "sync_restricted"):
            await _models.update_task_status(db, _t["id"], "paused")
            _paused_count += 1
    if _paused_count:
        logger.info("已将 %d 个孤立的同步任务自动暂停（可通过 /list 恢复）", _paused_count)
    await monitor_manager.restore_tasks()

    return monitor_manager


async def main():
    config = load_config()

    # 确保所需目录存在
    os.makedirs("data", exist_ok=True)
    os.makedirs("sessions", exist_ok=True)

    # 初始化数据库
    db = Database("data/bot.db")
    await db.init()
    logger.info("数据库初始化完成")

    # 初始化双客户端（增大内置重试参数以覆盖短暂网络波动）
    bot = TelegramClient(
        "sessions/bot", config["api_id"], config["api_hash"],
        connection_retries=10, retry_delay=5, timeout=30,
    )
    userbot = TelegramClient(
        "sessions/userbot", config["api_id"], config["api_hash"],
        connection_retries=10, retry_delay=5, timeout=30,
    )

    # === 首次启动 ===
    await _start_clients(bot, userbot, config)
    await _setup_bot(bot, userbot, db, config)

    # === 运行 + 断线重连循环 ===
    reconnect_count = 0
    while True:
        try:
            logger.info("所有服务就绪，等待消息...")
            await bot.run_until_disconnected()
        except (ConnectionError, OSError) as e:
            logger.error("连接丢失: %s", e)
        except Exception as e:
            logger.error("未预期异常: %s", e, exc_info=True)

        # run_until_disconnected() 返回 → 说明连接已断开，进入重连
        reconnect_count += 1
        if reconnect_count > MAX_RECONNECT_ATTEMPTS:
            logger.critical(
                "连续 %d 次重连均失败，进程退出（交由 systemd 重启）",
                MAX_RECONNECT_ATTEMPTS,
            )
            sys.exit(1)

        delay = min(
            RECONNECT_BASE_DELAY * (2 ** (reconnect_count - 1)),
            RECONNECT_MAX_DELAY,
        )
        logger.warning(
            "第 %d/%d 次重连，等待 %d 秒...",
            reconnect_count, MAX_RECONNECT_ATTEMPTS, delay,
        )
        await asyncio.sleep(delay)

        try:
            await bot.connect()
            await userbot.connect()
            reconnect_count = 0   # 重连成功，重置计数
            logger.info("重连成功")
        except Exception as e:
            logger.error("重连失败: %s", e)
            # 回到 while 循环顶部，reconnect_count 已递增，继续退避


if __name__ == "__main__":
    asyncio.run(main())

