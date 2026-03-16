"""Bot 命令处理器：注册所有 Telegram Bot 命令。"""
import asyncio
import logging

from telethon import TelegramClient, events, errors, Button
from telethon.tl import types

from bot.link_parser import (
    ParsedLink,
    parse_link,
    resolve_chat_id,
    resolve_linked_chat,
)
from bot.handler_common import (
    STATUS_EMOJI,
    FetchTarget,
    ParsedSource,
    extract_tg_link,
)
from bot.telegram_utils import (
    resolve_chat_name, resolve_topic_name, describe_source,
    get_target_topic_id, build_source_link, truncate,
)
from core.message_logic import (
    classify_message_kind,
    collect_album_messages,
)
from core.syncer import Syncer
from core.monitor import MonitorManager
from core.rate_limiter import _get_dynamic_rate_limit
from db.database import Database
from db import models

logger = logging.getLogger("tg_forward_bot.handlers")

def register_handlers(bot: TelegramClient, userbot: TelegramClient,
                      db: Database, config: dict,
                      monitor_manager: MonitorManager):
    admin_ids = set(config.get("admin_ids", []))
    allow_public = config.get("allow_public_resolve", False)
    syncer = Syncer(bot, userbot, db, config)
    forwarder = monitor_manager.forwarder
    _sync_tasks: dict[int, asyncio.Task] = {}

    def is_admin(user_id: int) -> bool:
        return user_id in admin_ids

    def _is_admin_via_chat_identity(event) -> bool:
        """兼容匿名管理员/频道身份发言。"""
        if not (event.is_group or event.is_channel):
            return False
        msg = getattr(event, "message", None)
        if msg is None:
            return False
        from_id = getattr(msg, "from_id", None)
        if from_id is None:
            # 匿名管理员消息常见为 from_id 为空（Telethon）。
            return True
        return isinstance(from_id, types.PeerChannel)

    async def _chat_has_configured_admin(chat_id: int) -> bool:
        """检查目标群/频道中是否包含配置里的管理员。"""
        try:
            admins = await bot.get_participants(
                chat_id,
                filter=types.ChannelParticipantsAdmins(),
            )
        except Exception as e:
            logger.warning("拉取管理员列表失败 chat=%s err=%s", chat_id, e)
            return False
        admin_member_ids = {u.id for u in admins if getattr(u, "id", None) is not None}
        return bool(admin_member_ids & admin_ids)

    async def _require_admin(event, *, alert: bool = False) -> bool:
        """统一管理员校验。"""
        if event.sender_id is not None and is_admin(event.sender_id):
            return True
        if event.is_private:
            pass
        elif _is_admin_via_chat_identity(event):
            if await _chat_has_configured_admin(event.chat_id):
                return True
        if alert:
            await event.answer("⛔ 无权限", alert=True)
        else:
            await event.reply("⛔ 无权限")
        return False

    async def _get_task_or_alert(event, task_id: int) -> dict | None:
        """按 task_id 获取任务，不存在时给出回调提示。"""
        task = await models.get_task(db, task_id)
        if task:
            return task
        await event.answer("任务不存在", alert=True)
        return None

    # ------------------------------------------------------------------
    # 内部共享 helpers
    # ------------------------------------------------------------------

    async def _parse_source(event) -> ParsedSource | None:
        """从命令消息中解析链接并返回 ParsedSource。"""
        text = event.raw_text
        mode = "forward" if "--forward" in text else "copy"
        link = extract_tg_link(text)
        if not link:
            await event.reply("❌ 请提供有效的 Telegram 链接")
            return None
        parsed = parse_link(link)
        if not parsed:
            await event.reply("❌ 无法解析链接")
            return None
        source_id = await resolve_chat_id(userbot, parsed)
        if not source_id:
            await event.reply("❌ 无法访问源频道/群组")
            return None
        return ParsedSource(parsed=parsed, source_id=source_id, mode=mode)

    async def _format_target_topic(target_chat_id: int,
                                   target_topic_id: int | None) -> str:
        """格式化目标话题信息。"""
        if not target_topic_id:
            return ""
        name = await resolve_topic_name(userbot, target_chat_id,
                                        target_topic_id)
        return f"\n📍 目标话题:「{name}」"

    async def _stop_running_task(task: dict):
        """停止一个运行中的任务（sync 或 monitor）。"""
        tid = task["id"]
        if task["type"] == "sync":
            syncer.cancel(tid)
            at = _sync_tasks.pop(tid, None)
            if at:
                at.cancel()
        else:
            await monitor_manager.stop_monitor(tid)

    async def _build_task_buttons(tasks: list) -> list:
        """为任务列表构建 inline button 行。"""
        buttons = []
        for t in tasks:
            emoji = "🔄" if t["type"] == "sync" else "👁"
            status = STATUS_EMOJI.get(t["status"], "❓")
            src_name = truncate(
                await resolve_chat_name(userbot, t["source_chat_id"]), 20)
            topic_part = ""
            if t["source_topic_id"]:
                topic_name = truncate(
                    await resolve_topic_name(
                        userbot, t["source_chat_id"], t["source_topic_id"]),
                    12)
                topic_part = f"/{topic_name}"
            label = f"{status}{emoji} #{t['id']} {src_name}{topic_part}"
            buttons.append([Button.inline(label, data=f"task:{t['id']}")])
        buttons.append([Button.inline("🗑 清空所有任务", data="clear_all")])
        return buttons

    async def _resolve_private_fetch_target(parsed: ParsedLink,
                                            source_id: int) -> FetchTarget | None:
        """将私聊解析的 parsed link 解析为真实抓取 chat/msg。"""
        fetch_chat_id = source_id
        target_msg_id = parsed.msg_id
        if parsed.comment_id:
            linked_id = await resolve_linked_chat(userbot, source_id)
            if not linked_id:
                return None
            fetch_chat_id = linked_id
            target_msg_id = parsed.comment_id
            logger.info("评论链接 → 讨论群 %s 消息 %s", fetch_chat_id, parsed.comment_id)
        return FetchTarget(chat_id=fetch_chat_id, msg_id=target_msg_id)

    async def _forward_private_message(event, fetch_chat_id: int, msg,
                                       parsed: ParsedLink):
        """私聊链接解析后的统一转发逻辑。"""
        reply_to_id = event.message.id
        kind = classify_message_kind(msg, single=parsed.single)
        if kind == "album":
            album_msgs = await collect_album_messages(
                userbot, fetch_chat_id, msg, window=10)
            logger.info("媒体集合: %d 条, grouped_id=%s",
                        len(album_msgs), msg.grouped_id)
            source_ids = [m.id for m in album_msgs]
            target_ids = await forwarder.forward_album(
                fetch_chat_id, source_ids, event.chat_id, mode="copy",
                target_topic_id=reply_to_id)
            if not target_ids:
                await event.reply("❌ 转发失败，已尝试所有策略")
            return

        if kind in ("media", "text"):
            if kind == "media":
                logger.info("单条媒体消息")
            else:
                logger.info("纯文本消息")
            target_id = await forwarder.forward_message(
                fetch_chat_id, msg.id, event.chat_id, mode="copy",
                target_topic_id=reply_to_id)
            if not target_id:
                await event.reply("❌ 转发失败，已尝试所有策略")
            return

        await event.reply("❌ 消息内容为空")

    async def _parse_private_link_and_source(
        event,
    ) -> tuple[ParsedLink, int] | None:
        link = extract_tg_link(event.raw_text)
        if not link:
            return None

        parsed = parse_link(link)
        if not parsed or not parsed.msg_id:
            await event.reply("❌ 无法解析链接，需要包含消息ID")
            return None

        source_id = await resolve_chat_id(userbot, parsed)
        if not source_id:
            await event.reply("❌ 无法访问该频道/群组")
            return None

        return parsed, source_id

    async def _fetch_private_target_message(event, fetch_target: FetchTarget):
        try:
            msg = await userbot.get_messages(fetch_target.chat_id, ids=fetch_target.msg_id)
            if not msg:
                await event.reply("❌ 消息不存在")
                return None
            return msg
        except ValueError as e:
            if "input entity" in str(e):
                await event.reply("❌ UserBot 未加入该私有频道/群组，无法访问")
            else:
                await event.reply(f"❌ 获取消息失败: {e}")
            logger.warning("私聊解析失败: %s", e)
        except errors.ChannelPrivateError:
            await event.reply("❌ 该频道/群组为私有，UserBot 未加入无法访问")
        except Exception as e:
            logger.warning("私聊解析失败: %s", e)
            await event.reply(f"❌ 获取消息失败: {e}")
        return None

    # ------------------------------------------------------------------
    # 命令处理器
    # ------------------------------------------------------------------

    @bot.on(events.NewMessage(pattern=r"/start(?:@\w+)?$"))
    async def on_start(event):
        logger.info("收到 /start 命令 from user=%s chat=%s", event.sender_id, event.chat_id)
        await event.reply(
            "🤖 Telegram 转发 Bot\n\n"
            "功能:\n"
            "• 私聊发链接 → 解析受限内容\n"
            "• /sync <链接> → 同步历史消息到当前群\n"
            "• /monitor <链接> → 监控新消息转发到当前群\n"
            "• /list → 管理所有任务\n"
            "• /settings → 查看配置")

    @bot.on(events.NewMessage(pattern=r"/help(?:@\w+)?$"))
    async def on_help(event):
        await event.reply(
            "📖 使用说明:\n\n"
            "/sync <链接> [--forward] — 同步源历史到当前群\n"
            "/monitor <链接> [--forward] — 监控源新消息到当前群\n"
            "/list — 管理所有任务（含暂停/恢复/删除）\n"
            "/settings — 查看限流配置\n\n"
            "链接格式:\n"
            "• https://t.me/channel/123\n"
            "• https://t.me/c/123456/789\n"
            "• https://t.me/c/123456/3/962 (话题)")

    @bot.on(events.NewMessage(pattern=r"/sync(?:@\w+)?\s+"))
    async def on_sync(event):
        logger.info("收到 /sync 命令 from user=%s chat=%s", event.sender_id, event.chat_id)
        if not await _require_admin(event):
            return

        result = await _parse_source(event)
        if not result:
            return
        parsed = result.parsed
        source_id = result.source_id
        mode = result.mode

        target_chat_id = event.chat_id
        target_topic_id = get_target_topic_id(event)
        logger.info("创建同步任务: source=%s topic=%s -> target=%s target_topic=%s mode=%s",
                     source_id, parsed.topic_id, target_chat_id, target_topic_id, mode)
        task_id = await models.create_task(
            db, "sync", source_id, target_chat_id, mode,
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id)

        rl = _get_dynamic_rate_limit(config)
        interval = rl.get("forward_interval", [2, 5])
        source_desc = await describe_source(userbot, source_id, parsed)
        topic_info = await _format_target_topic(target_chat_id, target_topic_id)
        start_msg = await event.reply(
            f"🚀 同步任务 #{task_id} 已创建\n"
            f"📌 源: {source_desc}\n"
            f"⏱ 转发间隔: {interval[0]}-{interval[1]}秒/条\n"
            f"📋 模式: {mode}{topic_info}")

        task = asyncio.create_task(syncer.start_sync(
            task_id, source_id, target_chat_id, mode,
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id,
            notify_chat_id=event.chat_id,
            notify_topic_id=target_topic_id,
            notify_reply_to_msg_id=start_msg.id))
        _sync_tasks[task_id] = task

    @bot.on(events.NewMessage(pattern=r"/monitor(?:@\w+)?\s+"))
    async def on_monitor(event):
        logger.info("收到 /monitor 命令 from user=%s chat=%s", event.sender_id, event.chat_id)
        if not await _require_admin(event):
            return

        result = await _parse_source(event)
        if not result:
            return
        parsed = result.parsed
        source_id = result.source_id
        mode = result.mode

        target_chat_id = event.chat_id
        target_topic_id = get_target_topic_id(event)
        logger.info("创建监控任务: source=%s topic=%s -> target=%s target_topic=%s mode=%s",
                     source_id, parsed.topic_id, target_chat_id, target_topic_id, mode)
        task_id = await models.create_task(
            db, "monitor", source_id, target_chat_id, mode,
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id)

        await monitor_manager.start_monitor(
            task_id, source_id, target_chat_id, mode,
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id)

        source_desc = await describe_source(userbot, source_id, parsed)
        topic_info = await _format_target_topic(target_chat_id, target_topic_id)
        logger.info("监控任务 #%s 已启动", task_id)
        await event.reply(
            f"👁 监控任务 #{task_id} 已启动\n"
            f"📌 源: {source_desc}\n"
            f"📋 模式: {mode}{topic_info}")

    @bot.on(events.NewMessage(pattern=r"/list(?:@\w+)?$"))
    async def on_list(event):
        if not await _require_admin(event):
            return

        tasks = await models.get_all_active_tasks(db)
        if not tasks:
            await event.reply("📭 没有活跃的任务")
            return

        buttons = await _build_task_buttons(tasks)
        await event.reply("📋 任务列表（点击管理）:", buttons=buttons)

    async def _show_task_detail(event, task_id: int):
        """渲染任务详情内联键盘（共享逻辑）。"""
        t = await models.get_task(db, task_id)
        if not t:
            await event.answer("任务不存在", alert=True)
            return

        emoji = "🔄" if t["type"] == "sync" else "👁"
        status = STATUS_EMOJI.get(t["status"], "❓")

        # 解析源名称
        src_name = await resolve_chat_name(userbot, t["source_chat_id"])
        src_topic = ""
        if t["source_topic_id"]:
            topic_name = await resolve_topic_name(
                userbot, t["source_chat_id"], t["source_topic_id"])
            src_topic = f" →「{topic_name}」"

        # 解析目标名称
        tgt_name = await resolve_chat_name(bot, t["target_chat_id"])
        tgt_topic = ""
        if t["target_topic_id"]:
            topic_name = await resolve_topic_name(
                userbot, t["target_chat_id"], t["target_topic_id"])
            tgt_topic = f" →「{topic_name}」"

        source_link = build_source_link(t["source_chat_id"],
                                         t["source_topic_id"])

        text = (
            f"{emoji} 任务 #{t['id']}\n"
            f"类型: {t['type']} | 模式: {t['mode']}\n"
            f"状态: {status} {t['status']}\n"
            f"源: {src_name}{src_topic}\n"
            f"目标: {tgt_name}{tgt_topic}")

        buttons = []
        if source_link:
            buttons.append([Button.url("🔗 打开源", source_link)])

        action_row = []
        if t["status"] == "running":
            action_row.append(Button.inline("⏸ 暂停", data=f"pause:{task_id}"))
        elif t["status"] in ("paused", "failed"):
            if t["type"] == "monitor":
                action_row.append(Button.inline("▶️ 恢复", data=f"resume:{task_id}"))
        action_row.append(Button.inline("🗑 删除", data=f"delete:{task_id}"))
        buttons.append(action_row)

        buttons.append([Button.inline("« 返回列表", data="back_list")])
        await event.edit(text, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=rb"task:(\d+)"))
    async def on_task_detail(event):
        if not await _require_admin(event, alert=True):
            return
        task_id = int(event.pattern_match.group(1))
        await _show_task_detail(event, task_id)

    @bot.on(events.CallbackQuery(pattern=rb"pause:(\d+)"))
    async def on_pause(event):
        if not await _require_admin(event, alert=True):
            return

        task_id = int(event.pattern_match.group(1))
        t = await _get_task_or_alert(event, task_id)
        if not t:
            return

        if t["status"] == "running":
            await _stop_running_task(t)
        await models.update_task_status(db, task_id, "paused")
        logger.info("任务 #%s 已暂停", task_id)
        await event.answer(f"⏸ 任务 #{task_id} 已暂停")
        await _show_task_detail(event, task_id)

    @bot.on(events.CallbackQuery(pattern=rb"resume:(\d+)"))
    async def on_resume(event):
        if not await _require_admin(event, alert=True):
            return

        task_id = int(event.pattern_match.group(1))
        t = await _get_task_or_alert(event, task_id)
        if not t:
            return
        if t["type"] != "monitor":
            await event.answer("仅支持恢复监控任务", alert=True)
            return

        await models.update_task_status(db, task_id, "running")
        await monitor_manager.start_monitor(
            task_id, t["source_chat_id"], t["target_chat_id"],
            t["mode"], t["source_topic_id"], t["target_topic_id"])

        logger.info("监控任务 #%s 已恢复", task_id)
        await event.answer(f"▶️ 监控任务 #{task_id} 已恢复")
        await _show_task_detail(event, task_id)

    @bot.on(events.CallbackQuery(pattern=rb"delete:(\d+)"))
    async def on_delete(event):
        if not await _require_admin(event, alert=True):
            return

        task_id = int(event.pattern_match.group(1))
        t = await _get_task_or_alert(event, task_id)
        if not t:
            return

        if t["status"] == "running":
            await _stop_running_task(t)

        await models.update_task_status(db, task_id, "completed")
        logger.info("任务 #%s 已删除", task_id)
        await event.answer(f"🗑 任务 #{task_id} 已删除")
        await _refresh_list(event)

    @bot.on(events.CallbackQuery(pattern=rb"clear_all"))
    async def on_clear_all(event):
        if not await _require_admin(event, alert=True):
            return

        tasks = await models.get_all_active_tasks(db)
        for t in tasks:
            if t["status"] == "running":
                await _stop_running_task(t)
            await models.update_task_status(db, t["id"], "completed")

        logger.info("已清空所有任务，共 %d 个", len(tasks))
        await event.answer(f"🗑 已清空 {len(tasks)} 个任务")
        await event.edit("📭 所有任务已清空", buttons=None)

    @bot.on(events.CallbackQuery(pattern=rb"back_list"))
    async def on_back_list(event):
        if not await _require_admin(event, alert=True):
            return
        await _refresh_list(event)

    async def _refresh_list(event):
        """刷新任务列表内联键盘。"""
        tasks = await models.get_all_active_tasks(db)
        if not tasks:
            await event.edit("📭 没有活跃的任务", buttons=None)
            return
        buttons = await _build_task_buttons(tasks)
        await event.edit("📋 任务列表（点击管理）:", buttons=buttons)

    @bot.on(events.NewMessage(pattern=r"/settings(?:@\w+)?$"))
    async def on_settings(event):
        if not await _require_admin(event):
            return

        rl = _get_dynamic_rate_limit(config)
        await event.reply(
            "⚙️ 当前限流配置:\n"
            f"• batch_size: {rl.get('batch_size', 100)}\n"
            f"• forward_interval: {rl.get('forward_interval', [2, 5])}\n"
            f"• batch_pause_every: {rl.get('batch_pause_every', 50)}\n"
            f"• batch_pause_time: {rl.get('batch_pause_time', [30, 60])}\n"
            f"• flood_wait_multiplier: {rl.get('flood_wait_multiplier', 2)}\n"
            f"• max_flood_wait: {rl.get('max_flood_wait', 300)}")

    @bot.on(events.NewMessage(func=lambda e: e.is_private))
    async def on_private_link(event):
        """私聊发链接，自动解析并返回内容。"""
        if event.raw_text.startswith("/"):
            return
        if not allow_public and not is_admin(event.sender_id):
            return

        parsed_source = await _parse_private_link_and_source(event)
        if not parsed_source:
            return
        parsed, source_id = parsed_source

        source_desc = await describe_source(userbot, source_id, parsed)
        comment_info = f" comment={parsed.comment_id}" if parsed.comment_id else ""
        single_info = " [single]" if parsed.single else ""
        logger.info("私聊解析: user=%s 源=%s msg=%s%s%s",
                    event.sender_id, source_desc, parsed.msg_id,
                    comment_info, single_info)

        fetch_target = await _resolve_private_fetch_target(parsed, source_id)
        if not fetch_target:
            await event.reply("❌ 无法获取频道的讨论群，评论消息无法解析")
            return

        msg = await _fetch_private_target_message(event, fetch_target)
        if not msg:
            return
        await _forward_private_message(event, fetch_target.chat_id, msg, parsed)
