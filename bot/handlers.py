"""Bot 命令处理器：注册所有 Telegram Bot 命令。"""
import asyncio
import html
import logging
import re

from telethon import TelegramClient, events, errors, Button
from telethon.tl import types
from telethon.tl.functions.channels import GetParticipantRequest, GetFullChannelRequest

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
    resolve_topic_name, describe_source,
    get_target_topic_id, build_source_link, truncate,
)
from core.message_logic import (
    classify_message_kind,
    collect_album_messages,
    is_file_media,
)
from core.base_component import SyncComponentBase
from core.syncer import Syncer
from core.restricted_syncer import RestrictedSyncer
from core.monitor import MonitorManager
from core.rate_limiter import _get_dynamic_rate_limit
from core.userbot_manager import UserBotManager
from db.database import Database
from db import models

logger = logging.getLogger("tg_forward_bot.handlers")

def register_handlers(bot: TelegramClient, userbot: TelegramClient,
                      db: Database, config: dict,
                      monitor_manager: MonitorManager,
                      userbot_manager: UserBotManager | None = None):
    admin_ids = set(config.get("admin_ids", []))
    allow_public = config.get("allow_public_resolve", False)
    append_source_link = config.get("append_source_link", False)
    syncer = Syncer(bot, userbot, db, config, userbot_manager=userbot_manager)
    restricted_syncer = RestrictedSyncer(bot, userbot, db, config, userbot_manager=userbot_manager)
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

    async def _resolve_topic_display_name(
        chat_id: int, topic_id: int | None, *, prefer_bot: bool = False
    ) -> str:
        """解析话题/线程名称，优先论坛话题名，失败回退线程顶层消息。"""
        if not topic_id:
            return ""

        fallback = f"#{topic_id}"
        forum_clients = [userbot] if prefer_bot else [userbot, bot]
        for client in forum_clients:
            name = await resolve_topic_name(client, chat_id, topic_id)
            if name and name != fallback:
                return name

        # General 话题 (id=1) 没有对应的 messageActionTopicCreate 服务消息，
        # 不能用 get_messages(ids=1) 回退，否则会取到第一条用户消息的文本。
        if topic_id == 1:
            return "General"

        # 非 forum 场景（评论区线程/普通线程）也可能带 top_msg_id。
        thread_clients = [bot, userbot] if prefer_bot else [userbot, bot]
        for client in thread_clients:
            try:
                top_msg = await client.get_messages(chat_id, ids=topic_id)
            except Exception:
                continue
            if not top_msg:
                continue
            action = getattr(top_msg, "action", None)
            action_title = getattr(action, "title", None) if action else None
            if action_title:
                return action_title
            raw = (getattr(top_msg, "raw_text", None) or "").strip()
            if raw:
                return truncate(raw.splitlines()[0], 20)
            text = (getattr(top_msg, "message", None) or "").strip()
            if text:
                return truncate(text.splitlines()[0], 20)

        return "未知线程"

    async def _describe_source_for_display(
        source_id: int, parsed: ParsedLink, client: TelegramClient | None = None
    ) -> str:
        """构造源描述，确保话题展示优先使用真实名称。"""
        ub = client or userbot
        source_desc = await describe_source(ub, source_id, parsed)
        if not parsed.topic_id:
            return source_desc
        topic_name = await _resolve_topic_display_name(source_id, parsed.topic_id)
        return source_desc.replace(
            f"话题「#{parsed.topic_id}」",
            f"话题「{topic_name}」",
        )

    async def _resolve_chat_meta(
        client: TelegramClient, chat_id: int
    ) -> tuple[str, str, str]:
        """解析实体元信息: (kind, visibility, name)。"""
        try:
            entity = await client.get_entity(chat_id)
            if isinstance(entity, types.Channel):
                kind = "频道" if getattr(entity, "broadcast", False) else "群组"
                username = getattr(entity, "username", None)
                if username:
                    return kind, "公开", f"@{username}"
                title = getattr(entity, "title", None) or str(chat_id)
                return kind, "私有", title

            title = getattr(entity, "title", None)
            if title:
                return "群组", "", title
            first_name = getattr(entity, "first_name", None)
            if first_name:
                return "群组", "", first_name
        except Exception:
            pass
        return "频道/群组", "", str(chat_id)

    async def _format_chat_topic_display(
        chat_id: int,
        topic_id: int | None,
        *,
        prefer_bot: bool = False,
        short: bool = False,
    ) -> str:
        """统一格式化: 频道/群组 + 话题。"""
        clients = [bot, userbot] if prefer_bot else [userbot, bot]
        kind, visibility, name = "频道/群组", "", str(chat_id)
        for client in clients:
            kind, visibility, name = await _resolve_chat_meta(client, chat_id)
            if name != str(chat_id):
                break

        if short:
            base = f"{kind[:1]}:{truncate(name, 12)}"
        else:
            vis_part = f"{visibility} " if visibility else ""
            base = f"{kind} | {vis_part}{name}"

        if topic_id:
            topic_name = await _resolve_topic_display_name(
                chat_id, topic_id, prefer_bot=prefer_bot
            )
            if short:
                base += f"/{truncate(topic_name, 8)}"
            else:
                base += f" + 话题「{topic_name}」"
        return base

    async def _ensure_userbot_joined_source_for_monitor(
        source_chat_id: int, source_topic_id: int | None
    ) -> tuple[bool, str | None, TelegramClient | None]:
        """校验 monitor 前提: userbot 已加入源。返回 (ok, reason, active_userbot)。"""
        active_ub = None
        if userbot_manager:
            active_ub, _ = await userbot_manager.resolve_accessible_userbot(source_chat_id)
        if not active_ub:
            active_ub = userbot

        try:
            entity = await active_ub.get_entity(source_chat_id)
        except (ValueError, errors.ChannelPrivateError):
            source_text = await _format_chat_topic_display(
                source_chat_id, source_topic_id
            )
            return (
                False,
                "❌ /monitor 要求 UserBot 必须先加入源后才会生效。\n"
                f"📌 源: {source_text}\n"
                "请先让 UserBot 加入该源，再执行 /monitor。",
                None,
            )
        except Exception as e:
            logger.warning("校验 userbot 源加入状态失败 source=%s err=%s", source_chat_id, e)
            source_text = await _format_chat_topic_display(
                source_chat_id, source_topic_id
            )
            return (
                False,
                "❌ 无法确认 UserBot 对源的可访问性，请先确保 UserBot 已加入源。\n"
                f"📌 源: {source_text}",
                None,
            )

        # Channel/超级群走参与者校验；其它会话默认放行。
        if not isinstance(entity, types.Channel):
            return True, None, active_ub

        try:
            me = await active_ub.get_me()
            await active_ub(GetParticipantRequest(channel=entity, participant=me.id))
            return True, None, active_ub
        except errors.UserNotParticipantError:
            source_text = await _format_chat_topic_display(
                source_chat_id, source_topic_id
            )
            return (
                False,
                "❌ /monitor 未生效：UserBot 尚未加入源。\n"
                f"📌 源: {source_text}\n"
                "请先让 UserBot 加入该频道/群组后，再执行 /monitor。",
                None,
            )
        except errors.ChannelPrivateError:
            source_text = await _format_chat_topic_display(
                source_chat_id, source_topic_id
            )
            return (
                False,
                "❌ /monitor 未生效：源为私有且 UserBot 无访问权限。\n"
                f"📌 源: {source_text}\n"
                "请先让 UserBot 加入该源后重试。",
                None,
            )
        except Exception as e:
            logger.warning("GetParticipant 校验失败 source=%s err=%s", source_chat_id, e)
            # 某些场景参与者列表受限，回退到读取探测。
            try:
                await active_ub.get_messages(source_chat_id, limit=1)
                return True, None, active_ub
            except Exception:
                source_text = await _format_chat_topic_display(
                    source_chat_id, source_topic_id
                )
                return (
                    False,
                    "❌ /monitor 要求 UserBot 可读取源消息，但当前不可读。\n"
                    f"📌 源: {source_text}\n"
                    "请先让 UserBot 加入该源后重试。",
                    None,
                )

    async def _stop_running_task(task: dict):
        """停止一个运行中的任务（sync / sync_restricted / monitor）。"""
        tid = task["id"]
        if task["type"] in ("sync", "sync_restricted"):
            syncer.cancel(tid)
            restricted_syncer.cancel(tid)
            at = _sync_tasks.pop(tid, None)
            if at:
                at.cancel()
        else:
            await monitor_manager.stop_monitor(tid)

    async def _build_task_buttons(tasks: list) -> list:
        """为任务列表构建单列 inline button。"""
        buttons = []
        for t in tasks:
            emoji = "🔄" if t["type"] in ("sync", "sync_restricted") else "👁"
            status = STATUS_EMOJI.get(t["status"], "❓")
            src = await _format_chat_topic_display(
                t["source_chat_id"], t["source_topic_id"], short=True
            )
            label = f"{status}{emoji} #{t['id']} {src}"
            buttons.append([Button.inline(truncate(label, 48), data=f"task:{t['id']}")])
        buttons.append([Button.inline("🗑 清空所有任务", data="clear_all")])
        return buttons

    async def _resolve_private_fetch_target(parsed: ParsedLink,
                                            source_id: int,
                                            client: TelegramClient | None = None) -> FetchTarget | None:
        """将私聊解析的 parsed link 解析为真实抓取 chat/msg。"""
        fetch_chat_id = source_id
        target_msg_id = parsed.msg_id
        ub = client or userbot
        if parsed.comment_id:
            linked_id = await resolve_linked_chat(ub, source_id)
            if not linked_id:
                return None
            fetch_chat_id = linked_id
            target_msg_id = parsed.comment_id
            logger.info("评论链接 → 讨论群 %s 消息 %s", fetch_chat_id, parsed.comment_id)
        return FetchTarget(chat_id=fetch_chat_id, msg_id=target_msg_id)

    async def _pre_warm_bot_cache(
        parsed: ParsedLink, source_id: int, fetch_chat_id: int
    ):
        """尝试预热 Bot 实体缓存，使策略1(Bot直接)能命中。

        在 handler 层仍持有原始用户名字符串 (parsed.chat_id)，
        利用它让 Bot 主动向 Telegram 解析并缓存 access_hash。
        """
        # 已有缓存则跳过
        try:
            await bot.get_input_entity(fetch_chat_id)
            return
        except Exception:
            pass

        original_username = (
            parsed.chat_id if isinstance(parsed.chat_id, str) else None
        )
        if not original_username:
            return  # 私有链接(纯数字 ID)，无用户名可用

        try:
            channel_entity = await bot.get_entity(original_username)
            logger.info("Bot 预热: 已解析 @%s", original_username)
        except Exception as e:
            logger.debug("Bot 预热 @%s 失败: %s", original_username, e)
            return

        try:
            await bot.get_input_entity(channel_entity)
            logger.info("Bot 预热: 已缓存源 peer @%s", original_username)
        except Exception as e:
            logger.debug("Bot 预热源 peer 失败 @%s: %s", original_username, e)

        # 评论链接: fetch_chat_id 是讨论群而非频道，需额外解析讨论群
        if parsed.comment_id and fetch_chat_id != source_id:
            try:
                await bot(GetFullChannelRequest(channel_entity))
                logger.info("Bot 预热: 已解析讨论群(来自 @%s)", original_username)
            except Exception as e:
                logger.debug("Bot 预热讨论群失败: %s", e)
            try:
                discussion_entity = await bot.get_entity(fetch_chat_id)
                await bot.get_input_entity(discussion_entity)
                logger.info("Bot 预热: 已缓存讨论群 peer %s", fetch_chat_id)
            except Exception as e:
                logger.debug("Bot 预热讨论群 peer 失败 %s: %s", fetch_chat_id, e)

    def _build_source_url(raw_text: str, parsed: ParsedLink, source_id: int) -> str:
        raw_link = extract_tg_link(raw_text)
        if raw_link:
            cleaned = re.sub(r'([?&])single\b(=[^&]*)?', '', raw_link)
            cleaned = cleaned.replace("?&", "?").rstrip("?&")
            if cleaned.startswith("http://") or cleaned.startswith("https://"):
                return cleaned
        if parsed.is_private:
            clean_id = str(source_id).replace("-100", "").lstrip("-")
            if parsed.topic_id:
                url = f"https://t.me/c/{clean_id}/{parsed.topic_id}/{parsed.msg_id}"
            else:
                url = f"https://t.me/c/{clean_id}/{parsed.msg_id}"
        else:
            if parsed.topic_id:
                url = f"https://t.me/{parsed.chat_id}/{parsed.topic_id}/{parsed.msg_id}"
            else:
                url = f"https://t.me/{parsed.chat_id}/{parsed.msg_id}"
        if parsed.comment_id:
            url += f"?comment={parsed.comment_id}"
        return url

    async def _append_link_to_target(target_chat_id: int, target_msg_id: int,
                                     base_text: str, source_url: str,
                                     is_media: bool = False):
        link_tag = f"[Source Link]({source_url})"
        max_len = 1024 if is_media else 4096
        base = base_text or ""
        if base:
            combined = f"{base}\n\n{link_tag}"
            if len(combined) > max_len:
                overflow = len(combined) - max_len
                cut_len = max(0, len(base) - overflow - 3)
                base = base[:cut_len] + "..."
                combined = f"{base}\n\n{link_tag}"
        else:
            combined = link_tag

        logger.info("私聊来源链接准备追加: target_msg=%s is_media=%s source_url=%s",
                    target_msg_id, is_media, source_url)
        try:
            await bot.edit_message(
                target_chat_id, target_msg_id,
                text=combined,
                parse_mode="md",
                link_preview=False,
            )
            logger.info("私聊来源链接追加成功: target_msg=%s", target_msg_id)
        except errors.MessageNotModifiedError:
            pass
        except Exception as e:
            logger.warning("私聊编辑追加 Markdown 来源链接失败 (%s: %s)，尝试 HTML 回退",
                           type(e).__name__, e)
            try:
                html_link = f'<a href="{html.escape(source_url)}">Source Link</a>'
                if base:
                    esc_base = html.escape(base)
                    html_combined = f"{esc_base}\n\n{html_link}"
                    if len(html_combined) > max_len:
                        overflow = len(html_combined) - max_len
                        cut_len = max(0, len(esc_base) - overflow - 3)
                        esc_base = esc_base[:cut_len] + "..."
                        html_combined = f"{esc_base}\n\n{html_link}"
                else:
                    html_combined = html_link

                await bot.edit_message(
                    target_chat_id, target_msg_id,
                    text=html_combined,
                    parse_mode="html",
                    link_preview=False,
                )
                logger.info("私聊来源链接 HTML 回退追加成功: target_msg=%s", target_msg_id)
            except Exception as e2:
                logger.warning("私聊追加来源链接失败: target_msg=%s, err=%s", target_msg_id, e2)

    async def _forward_private_message(event, fetch_chat_id: int, msg,
                                       parsed: ParsedLink,
                                       client: TelegramClient | None = None,
                                       source_url: str | None = None):
        """私聊链接解析后的统一转发逻辑。"""
        ub = client or userbot
        reply_to_id = event.message.id
        kind = classify_message_kind(msg, single=parsed.single)
        if kind == "album":
            album_msgs = await collect_album_messages(
                ub, fetch_chat_id, msg, window=10)
            logger.info("媒体集合: %d 条, grouped_id=%s",
                        len(album_msgs), msg.grouped_id)
            source_ids = [m.id for m in album_msgs]
            target_ids = await forwarder.forward_album(
                fetch_chat_id, source_ids, event.chat_id, mode="copy",
                target_topic_id=reply_to_id, caller="private", userbot=ub)
            if not target_ids:
                await event.reply("❌ 转发失败，已尝试所有策略")
                return

            if append_source_link and source_url and target_ids:
                # 寻找原相册中带有 caption 的索引，若全无则默认取第 1 条 (index 0)
                caption_idx = 0
                for idx, m in enumerate(album_msgs):
                    if m.text:
                        caption_idx = idx
                        break
                target_msg_id = target_ids[caption_idx] if caption_idx < len(target_ids) else target_ids[0]
                base_text = album_msgs[caption_idx].text if caption_idx < len(album_msgs) else ""
                await _append_link_to_target(event.chat_id, target_msg_id, base_text or "", source_url, is_media=True)
            return

        if kind in ("media", "text"):
            if kind == "media":
                logger.info("单条媒体消息")
            else:
                logger.info("纯文本消息")
            target_id = await forwarder.forward_message(
                fetch_chat_id, msg.id, event.chat_id, mode="copy",
                target_topic_id=reply_to_id, caller="private", userbot=ub)
            if not target_id:
                await event.reply("❌ 转发失败，已尝试所有策略")
                return

            if append_source_link and source_url and target_id:
                is_media = (kind == "media")
                await _append_link_to_target(event.chat_id, target_id, msg.text or "", source_url, is_media=is_media)
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

    async def _fetch_private_target_message(event, fetch_target: FetchTarget,
                                            client: TelegramClient | None = None):
        ub = client or userbot
        try:
            msg = await ub.get_messages(fetch_target.chat_id, ids=fetch_target.msg_id)
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
            "• /syncrestrictedmsg <链接> → 通过 Takeout导出数据接口 补发受限消息\n"
            "• /takeout <链接> → 通过 Takeout导出数据接口 解析单条消息/相册\n"
            "• /monitor <链接> → 监控新消息转发到当前群（需 UserBot 先加入源）\n"
            "• /list → 管理所有任务\n"
            "• /settings → 查看配置")

    @bot.on(events.NewMessage(pattern=r"/help(?:@\w+)?$"))
    async def on_help(event):
        await event.reply(
            "📖 使用说明:\n\n"
            "/sync <链接> [--forward] — 同步源历史到当前群\n"
            "/syncrestrictedmsg <链接> — 通过 Takeout导出数据接口 补发受限消息到当前群\n"
            "/takeout <链接> — 通过 Takeout导出数据接口 解析单条消息/相册到当前群\n"
            "/monitor <链接> [--forward] — 监控源新消息到当前群（要求 UserBot 已加入源）\n"
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

        active_ub = None
        if userbot_manager:
            active_ub, err_msg = await userbot_manager.resolve_accessible_userbot(source_id)
            if not active_ub and parsed.is_private:
                await event.reply(f"❌ {err_msg or '所有配置的 UserBot 均未加入该私有频道/群组'}")
                return

        logger.info("创建同步任务: source=%s topic=%s -> target=%s target_topic=%s mode=%s (UserBot: %s)",
                     source_id, parsed.topic_id, target_chat_id, target_topic_id, mode,
                     getattr(active_ub, '_phone', 'default'))
        task_id = await models.create_task(
            db, "sync", source_id, target_chat_id, mode,
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id)

        rl = _get_dynamic_rate_limit(config)
        interval = rl.get("forward_interval", [2, 5])
        source_desc = await _format_chat_topic_display(source_id, parsed.topic_id)
        target_desc = await _format_chat_topic_display(
            target_chat_id, target_topic_id, prefer_bot=True
        )
        start_msg = await event.reply(
            f"🚀 同步任务 #{task_id} 已创建\n"
            f"📌 源: {source_desc}\n"
            f"🎯 目标: {target_desc}\n"
            f"⏱ 转发间隔: {interval[0]}-{interval[1]}秒/条\n"
            f"📋 模式: {mode}")

        task = asyncio.create_task(syncer.start_sync(
            task_id, source_id, target_chat_id, mode,
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id,
            notify_chat_id=event.chat_id,
            notify_topic_id=target_topic_id,
            notify_reply_to_msg_id=start_msg.id,
            userbot=active_ub))
        _sync_tasks[task_id] = task

    @bot.on(events.NewMessage(pattern=r"/syncrestrictedmsg(?:@\w+)?\s+"))
    async def on_sync_restricted(event):
        logger.info("收到 /syncRestrictedMsg 命令 from user=%s chat=%s", event.sender_id, event.chat_id)
        if not await _require_admin(event):
            return

        result = await _parse_source(event)
        if not result:
            return
        parsed = result.parsed
        source_id = result.source_id

        target_chat_id = event.chat_id
        target_topic_id = get_target_topic_id(event)

        active_ub = None
        if userbot_manager:
            active_ub, err_msg = await userbot_manager.resolve_accessible_userbot(source_id)
            if not active_ub and parsed.is_private:
                await event.reply(f"❌ {err_msg or '所有配置的 UserBot 均未加入该私有频道/群组'}")
                return

        logger.info(
            "创建受限同步任务: source=%s topic=%s -> target=%s target_topic=%s (UserBot: %s)",
            source_id, parsed.topic_id, target_chat_id, target_topic_id,
            getattr(active_ub, '_phone', 'default')
        )
        task_id = await models.create_task(
            db, "sync_restricted", source_id, target_chat_id, "copy",
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id,
        )

        rl = _get_dynamic_rate_limit(config)
        interval = rl.get("forward_interval", [2, 5])
        source_desc = await _format_chat_topic_display(source_id, parsed.topic_id)
        target_desc = await _format_chat_topic_display(
            target_chat_id, target_topic_id, prefer_bot=True
        )
        start_msg = await event.reply(
            f"🔓 受限消息同步任务 #{task_id} 已创建\n"
            f"📌 源: {source_desc}\n"
            f"🎯 目标: {target_desc}\n"
            f"⏱ 转发间隔: {interval[0]}-{interval[1]}秒/条\n"
            f"📋 模式: Takeout + Copy（仅同步受限消息）"
        )

        task = asyncio.create_task(restricted_syncer.start_sync(
            task_id, source_id, target_chat_id,
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id,
            notify_chat_id=event.chat_id,
            notify_topic_id=target_topic_id,
            notify_reply_to_msg_id=start_msg.id,
            userbot=active_ub,
        ))
        _sync_tasks[task_id] = task

    @bot.on(events.NewMessage(pattern=r"^/takeout(?:@\w+)?(?:\s+(.+))?$"))
    async def on_takeout(event):
        """通过 Takeout 导出接口解析单条链接或回复消息。"""
        if event.is_private:
            if not allow_public and not is_admin(event.sender_id):
                await event.reply("⛔ 无权限使用此命令")
                return
        else:
            if not await _require_admin(event):
                return

        link = extract_tg_link(event.raw_text)
        if not link and event.is_reply:
            reply = await event.get_reply_message()
            if reply and reply.raw_text:
                link = extract_tg_link(reply.raw_text)

        if not link:
            await event.reply("❌ 请提供有效的 Telegram 消息链接，或回复包含链接的消息。\n格式：`/takeout <链接>`")
            return

        parsed = parse_link(link)
        if not parsed or not parsed.msg_id:
            await event.reply("❌ 无法解析链接，链接中必须包含消息 ID")
            return

        source_id = await resolve_chat_id(userbot, parsed)
        if not source_id:
            await event.reply("❌ 无法访问源频道/群组")
            return

        fetch_target = await _resolve_private_fetch_target(parsed, source_id)
        if not fetch_target:
            await event.reply("❌ 无法获取讨论群实体，评论链接无法解析")
            return

        # 多 UserBot 按配置优先顺序探测（第 1 轮 Session 本地缓存命中，第 2 轮 dialogs 刷新命中）
        active_ub = userbot
        if userbot_manager:
            active_ub, err_msg = await userbot_manager.resolve_accessible_userbot(
                fetch_target.chat_id, fetch_target.msg_id
            )
            if not active_ub:
                await event.reply(f"❌ {err_msg or '所有配置的 UserBot 均未加入该私有频道/群组，无法访问'}")
                return

        phone = getattr(active_ub, "_phone", "default")
        ub_idx = getattr(active_ub, "_userbot_index", 1)
        source_desc = await _describe_source_for_display(source_id, parsed, client=active_ub)
        logger.info("收到 /takeout 命令: user=%s chat=%s 源=%s msg=%s (UserBot: #%d %s)",
                    event.sender_id, event.chat_id, source_desc, fetch_target.msg_id, ub_idx, phone)

        target_chat_id = event.chat_id
        target_topic_id = get_target_topic_id(event)
        real_chat_id = SyncComponentBase._ensure_supergroup_id(fetch_target.chat_id)
        source_url = _build_source_url(link, parsed, source_id)

        try:
            logger.info("正在启动 UserBot #%d [%s] 的 Takeout 会话...", ub_idx, phone)
            async with active_ub.takeout() as takeout:
                logger.info("Takeout 会话已建立，拉取目标消息 real_chat=%s msg_id=%s...",
                            real_chat_id, fetch_target.msg_id)
                msg = await takeout.get_messages(real_chat_id, ids=fetch_target.msg_id)
                if not msg:
                    await event.reply("❌ 消息不存在或无法通过 Takeout 获取")
                    return

                kind = classify_message_kind(msg, single=parsed.single)
                if kind == "album":
                    album_msgs = await collect_album_messages(
                        takeout, real_chat_id, msg, window=10
                    )
                    logger.info("Takeout 媒体集合: %d 条, grouped_id=%s",
                                len(album_msgs), msg.grouped_id)

                    if append_source_link and source_url:
                        caption_idx = 0
                        for idx, m in enumerate(album_msgs):
                            if m.text:
                                caption_idx = idx
                                break
                        base = album_msgs[caption_idx].text or ""
                        link_tag = f"[Source Link]({source_url})"
                        new_text = f"{base}\n\n{link_tag}" if base else link_tag
                        if len(new_text) > 1024:
                            overflow = len(new_text) - 1024
                            cut_len = max(0, len(base) - overflow - 3)
                            base = base[:cut_len] + "..."
                            new_text = f"{base}\n\n{link_tag}"
                        album_msgs[caption_idx].message = new_text
                        album_msgs[caption_idx]._text = new_text
                        album_msgs[caption_idx].entities = None

                    target_ids = await RestrictedSyncer._copy_album(
                        takeout, album_msgs, target_chat_id, target_topic_id
                    )
                    if not target_ids:
                        await event.reply("❌ Takeout 相册发送失败")
                    else:
                        logger.info("✅ Takeout 相册发送成功: %s", target_ids)
                    return

                if kind in ("media", "text"):
                    if append_source_link and source_url:
                        base = msg.text or ""
                        link_tag = f"[Source Link]({source_url})"
                        max_len = 1024 if is_file_media(msg) else 4096
                        new_text = f"{base}\n\n{link_tag}" if base else link_tag
                        if len(new_text) > max_len:
                            overflow = len(new_text) - max_len
                            cut_len = max(0, len(base) - overflow - 3)
                            base = base[:cut_len] + "..."
                            new_text = f"{base}\n\n{link_tag}"
                        msg.message = new_text
                        msg._text = new_text
                        msg.entities = None

                    target_id = await RestrictedSyncer._copy_single(
                        takeout, msg, target_chat_id, target_topic_id
                    )
                    if not target_id:
                        await event.reply("❌ Takeout 消息发送失败")
                    else:
                        logger.info("✅ Takeout 消息发送成功: %s", target_id)
                    return

                await event.reply("❌ 消息内容为空")

        except errors.TakeoutInitDelayError as e:
            logger.warning("Takeout 启动延迟: 需要等待 %s 秒", e.seconds)
            await event.reply(f"⚠️ Telegram Takeout 初始化限制：需等待 {e.seconds} 秒，或需在官方客户端确认导出授权。")
        except errors.ChatForwardsRestrictedError:
            logger.warning("Takeout 仍提示转发受限")
            await event.reply("❌ Takeout 转发失败：来源受限或目标群组无法接收")
        except errors.UserPrivacyRestrictedError:
            logger.warning("用户隐私设置限制私聊")
            await event.reply("❌ 发送失败：目标用户的隐私设置不允许 UserBot 发送私聊消息")
        except errors.FloodWaitError as e:
            logger.warning("Takeout 触发 FloodWait: %s秒", e.seconds)
            await event.reply(f"⏳ 触发 Telegram 频率限制，请等待 {e.seconds} 秒后重试")
        except Exception as e:
            logger.error("Takeout 执行异常: %s", e, exc_info=True)
            await event.reply(f"❌ Takeout 处理失败: {e}")

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
        ok, reason, active_ub = await _ensure_userbot_joined_source_for_monitor(
            source_id, parsed.topic_id
        )
        if not ok:
            await event.reply(reason)
            return

        logger.info("创建监控任务: source=%s topic=%s -> target=%s target_topic=%s mode=%s (UserBot: %s)",
                     source_id, parsed.topic_id, target_chat_id, target_topic_id, mode,
                     getattr(active_ub, '_phone', 'default'))
        task_id = await models.create_task(
            db, "monitor", source_id, target_chat_id, mode,
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id)

        await monitor_manager.start_monitor(
            task_id, source_id, target_chat_id, mode,
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id,
            userbot=active_ub)

        source_desc = await _format_chat_topic_display(source_id, parsed.topic_id)
        target_desc = await _format_chat_topic_display(
            target_chat_id, target_topic_id, prefer_bot=True
        )
        logger.info("监控任务 #%s 已启动", task_id)
        await event.reply(
            f"👁 监控任务 #{task_id} 已启动\n"
            f"📌 源: {source_desc}\n"
            f"🎯 目标: {target_desc}\n"
            "✅ 已校验 UserBot 可访问该源\n"
            f"📋 模式: {mode}")

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

        emoji = "🔄" if t["type"] in ("sync", "sync_restricted") else "👁"
        status = STATUS_EMOJI.get(t["status"], "❓")

        # 解析源/目标完整信息（频道/群组 + 话题）。
        src_desc = await _format_chat_topic_display(
            t["source_chat_id"], t["source_topic_id"]
        )
        tgt_desc = await _format_chat_topic_display(
            t["target_chat_id"], t["target_topic_id"], prefer_bot=True
        )

        source_link = build_source_link(t["source_chat_id"],
                                         t["source_topic_id"])

        text = (
            f"{emoji} 任务 #{t['id']}\n"
            f"类型: {t['type']} | 模式: {t['mode']}\n"
            f"状态: {status} {t['status']}\n"
            f"源: {src_desc}\n"
            f"目标: {tgt_desc}")

        buttons = []
        if source_link:
            buttons.append([Button.url("🔗 打开源", source_link)])

        action_row = []
        if t["status"] == "running":
            action_row.append(Button.inline("⏸ 暂停", data=f"pause:{task_id}"))
        elif t["status"] in ("paused", "failed"):
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

        if t["type"] == "monitor":
            ok, reason = await _ensure_userbot_joined_source_for_monitor(
                t["source_chat_id"], t["source_topic_id"]
            )
            if not ok:
                alert_text = truncate(reason.replace("\n", " "), 80) if reason else "UserBot 未加入源，无法恢复"
                await event.answer(alert_text, alert=True)
                await _show_task_detail(event, task_id)
                return

            await models.update_task_status(db, task_id, "running")
            await monitor_manager.start_monitor(
                task_id, t["source_chat_id"], t["target_chat_id"],
                t["mode"], t["source_topic_id"], t["target_topic_id"])

            logger.info("监控任务 #%s 已恢复", task_id)
            await event.answer(f"▶️ 监控任务 #{task_id} 已恢复")
            await _show_task_detail(event, task_id)

        elif t["type"] == "sync_restricted":
            await models.update_task_status(db, task_id, "running")
            start_msg_id = event.message_id
            task = asyncio.create_task(restricted_syncer.start_sync(
                task_id, t["source_chat_id"], t["target_chat_id"],
                source_topic_id=t["source_topic_id"],
                target_topic_id=t["target_topic_id"],
                notify_chat_id=t["target_chat_id"],
                notify_topic_id=t["target_topic_id"],
            ))
            _sync_tasks[task_id] = task
            logger.info("受限同步任务 #%s 已恢复", task_id)
            await event.answer(f"▶️ 受限同步任务 #{task_id} 已恢复")
            await _show_task_detail(event, task_id)

        elif t["type"] == "sync":
            await models.update_task_status(db, task_id, "running")
            task = asyncio.create_task(syncer.start_sync(
                task_id, t["source_chat_id"], t["target_chat_id"],
                t["mode"],
                source_topic_id=t["source_topic_id"],
                target_topic_id=t["target_topic_id"],
                notify_chat_id=t["target_chat_id"],
                notify_topic_id=t["target_topic_id"],
            ))
            _sync_tasks[task_id] = task
            logger.info("同步任务 #%s 已恢复", task_id)
            await event.answer(f"▶️ 同步任务 #{task_id} 已恢复")
            await _show_task_detail(event, task_id)

        else:
            await event.answer("未知任务类型，无法恢复", alert=True)

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

        fetch_target = await _resolve_private_fetch_target(parsed, source_id)
        if not fetch_target:
            await event.reply("❌ 无法获取频道的讨论群，评论消息无法解析")
            return

        # 多 UserBot 按顺序探测与鉴权
        active_ub = userbot
        if userbot_manager:
            active_ub, err_msg = await userbot_manager.resolve_accessible_userbot(
                fetch_target.chat_id, fetch_target.msg_id
            )
            if not active_ub:
                await event.reply(f"❌ {err_msg or '所有配置的 UserBot 均未加入该私有频道/群组，无法访问'}")
                return

        source_desc = await _describe_source_for_display(source_id, parsed, client=active_ub)
        comment_info = f" comment={parsed.comment_id}" if parsed.comment_id else ""
        single_info = " [single]" if parsed.single else ""
        phone = getattr(active_ub, "_phone", "default")
        logger.info("私聊解析: user=%s 源=%s msg=%s%s%s (UserBot: %s)",
                    event.sender_id, source_desc, parsed.msg_id,
                    comment_info, single_info, phone)

        # 预热 Bot 实体缓存，让策略1尽量命中
        await _pre_warm_bot_cache(parsed, source_id, fetch_target.chat_id)

        source_url = _build_source_url(event.raw_text, parsed, source_id)
        msg = await _fetch_private_target_message(event, fetch_target, client=active_ub)
        if not msg:
            return
        await _forward_private_message(event, fetch_target.chat_id, msg, parsed, client=active_ub, source_url=source_url)
