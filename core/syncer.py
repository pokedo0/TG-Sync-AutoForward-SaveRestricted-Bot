"""历史同步：分批获取源消息并转发到目标，支持断点续传与受限消息 Takeout 内联转发。"""
import asyncio
import logging
from collections.abc import Awaitable, Callable

from telethon import TelegramClient, errors
from telethon.tl.types import MessageService

from core.base_component import ForwardingComponent
from core.message_logic import (
    build_forward_units,
    detect_hard_restriction,
    is_chat_globally_restricted,
)
from core.restricted_syncer import RestrictedSyncer
from db.database import Database
from db import models

logger = logging.getLogger("tg_forward_bot.syncer")


class Syncer(ForwardingComponent):
    def __init__(self, bot: TelegramClient, userbot: TelegramClient,
                 db: Database, config: dict):
        super().__init__(bot, userbot, db, config)

    async def _collect_messages(
        self,
        task_id: int,
        source_chat_id: int,
        source_topic_id: int | None,
        offset_id: int,
        notify: Callable[[str], Awaitable[None]],
    ) -> list | None:
        all_msgs = []
        try:
            async for msg in self.userbot.iter_messages(
                source_chat_id,
                reverse=True,
                offset_id=offset_id,
                reply_to=source_topic_id,
            ):
                if self._cancel_flags.get(task_id):
                    break
                if isinstance(msg, MessageService):
                    continue
                all_msgs.append(msg)
        except Exception as error:
            if isinstance(error, errors.FloodWaitError):
                logger.warning("同步任务 #%s 获取消息列表时 FloodWait %ds", task_id, error.seconds)
                await asyncio.sleep(error.seconds)
                return []
            if await self._handle_collect_error(task_id, error, notify):
                return None
            raise
        return all_msgs

    async def _forward_unit(
        self,
        task_id: int,
        source_chat_id: int,
        target_chat_id: int,
        mode: str,
        target_topic_id: int | None,
        unit_kind: str,
        source_ids: list[int],
    ) -> tuple[int, int, list[int]]:
        """转发普通消息单元，返回 (msg_count, last_msg_id, target_ids)。"""
        if unit_kind == "album":
            target_ids = await self.forwarder.forward_album(
                source_chat_id, source_ids, target_chat_id, mode, target_topic_id
            )
            for src_id, tgt_id in zip(source_ids, target_ids):
                await models.save_message_map(self.db, task_id, src_id, tgt_id)
            return len(target_ids), source_ids[-1], target_ids

        source_msg_id = source_ids[0]
        target_msg_id = await self.forwarder.forward_message(
            source_chat_id, source_msg_id, target_chat_id, mode, target_topic_id
        )
        if target_msg_id:
            await models.save_message_map(self.db, task_id, source_msg_id, target_msg_id)
            return 1, source_msg_id, [target_msg_id]
        return 0, source_msg_id, []

    async def _precheck_restricted_units(
        self,
        source_chat_id: int,
        all_msgs: list,
        units: list[tuple[str, list[int]]],
    ) -> tuple[dict[int, tuple[bool, str]], int]:
        """预检查每个转发单元是否受限，返回缓存与受限消息总数。"""
        chat_globally_restricted = False
        try:
            entity = await self.userbot.get_entity(source_chat_id)
            chat_globally_restricted = is_chat_globally_restricted(entity)
        except Exception:
            chat_globally_restricted = False

        msg_map = {m.id: m for m in all_msgs if m}
        restriction_cache: dict[int, tuple[bool, str]] = {}
        restricted_total = 0
        for _, source_ids in units:
            if not source_ids:
                continue
            anchor_id = source_ids[0]
            restricted, restricted_field = detect_hard_restriction(
                chat_globally_restricted, msg_map.get(anchor_id)
            )
            restriction_cache[anchor_id] = (restricted, restricted_field)
            if restricted:
                restricted_total += len(source_ids)
        return restriction_cache, restricted_total

    # ------------------------------------------------------------------
    # Takeout 内联转发受限消息
    # ------------------------------------------------------------------

    async def _open_takeout(self):
        """懒加载开启 Takeout 会话。"""
        takeout = self.userbot.takeout()
        return await takeout.__aenter__()

    @staticmethod
    async def _close_takeout(takeout) -> None:
        """安全关闭 Takeout，用 shield 防止被 task cancel 打断。"""
        if takeout is None:
            return
        try:
            await asyncio.shield(takeout.__aexit__(None, None, None))
        except Exception as e:
            logger.warning("关闭 Takeout 异常: %s", e)

    async def _forward_restricted_unit(
        self,
        takeout,
        task_id: int,
        source_chat_id: int,
        target_chat_id: int,
        target_topic_id: int | None,
        unit_kind: str,
        source_ids: list[int],
    ) -> tuple[int, int, list[int]]:
        """通过 Takeout 转发受限消息单元，返回 (msg_count, last_msg_id, target_ids)。"""
        real_chat_id = self._ensure_supergroup_id(source_chat_id)
        try:
            messages = await takeout.get_messages(real_chat_id, ids=source_ids)
        except Exception as e:
            logger.error("同步任务 #%s Takeout 获取消息失败 ids=%s: %s", task_id, source_ids, e)
            return 0, source_ids[-1], []

        valid_msgs = [m for m in (messages if isinstance(messages, list) else [messages]) if m]
        if not valid_msgs:
            return 0, source_ids[-1], []

        await self.rl.wait()
        try:
            if unit_kind == "album":
                target_ids = await RestrictedSyncer._copy_album(
                    takeout, valid_msgs, target_chat_id, target_topic_id
                )
                for src_id, tgt_id in zip(source_ids, target_ids):
                    await models.save_message_map(self.db, task_id, src_id, tgt_id)
                return len(target_ids), source_ids[-1], target_ids

            msg = valid_msgs[0]
            tgt_id = await RestrictedSyncer._copy_single(
                takeout, msg, target_chat_id, target_topic_id
            )
            if tgt_id:
                await models.save_message_map(self.db, task_id, source_ids[0], tgt_id)
                return 1, source_ids[0], [tgt_id]
            return 0, source_ids[0], []
        except errors.FloodWaitError as e:
            logger.warning("同步任务 #%s Takeout FloodWait %ds", task_id, e.seconds)
            await asyncio.sleep(e.seconds)
            return 0, source_ids[-1], []
        except Exception as e:
            logger.warning("同步任务 #%s Takeout 转发异常 ids=%s: %s", task_id, source_ids, e)
            return 0, source_ids[-1], []

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def start_sync(self, task_id: int, source_chat_id: int,
                         target_chat_id: int, mode: str = "copy",
                         source_topic_id: int | None = None,
                         target_topic_id: int | None = None,
                         notify_chat_id: int | None = None,
                         notify_topic_id: int | None = None,
                         notify_reply_to_msg_id: int | None = None):
        """执行历史同步任务。"""
        self._cancel_flags[task_id] = False
        task = await models.get_task(self.db, task_id)
        offset_id = task["last_synced_msg_id"] if task else 0

        async def _notify(text: str) -> None:
            await self._notify(
                notify_chat_id, notify_topic_id, notify_reply_to_msg_id, text,
            )

        logger.info("同步任务 #%s 开始: source=%s topic=%s offset=%s",
                     task_id, source_chat_id, source_topic_id, offset_id)

        total_forwarded = 0
        restricted_messages = 0
        restricted_success = 0
        restricted_fail = 0
        failed_units = 0
        takeout = None

        all_msgs = await self._collect_messages(
            task_id, source_chat_id, source_topic_id, offset_id, _notify
        )
        if all_msgs is None:
            return
        if not all_msgs and self._cancel_flags.get(task_id):
            await models.update_task_status(self.db, task_id, "paused")
            await _notify("⏸ 同步已暂停，已完成 0/0")
            return

        if source_topic_id and not all_msgs:
            logger.warning("同步任务 #%s topic=%s 无消息，可能已关闭", task_id, source_topic_id)
            await models.update_task_status(self.db, task_id, "failed")
            await _notify(f"❌ 同步任务 #{task_id} 失败: 话题#{source_topic_id} 无消息（可能已关闭或不存在）")
            return

        total = len(all_msgs)
        logger.info("同步任务 #%s 共获取 %d 条消息", task_id, total)
        units = build_forward_units(all_msgs)
        restriction_cache, restricted_messages = await self._precheck_restricted_units(
            source_chat_id, all_msgs, units
        )
        await _notify(
            f"📋 开始同步，共 {total} 条消息"
            f"\n• 受限消息: {restricted_messages} 条（将通过 Takeout 转发）"
            f"\n• 普通消息: {max(total - restricted_messages, 0)} 条"
        )

        try:
            for kind, source_ids in units:
                if self._cancel_flags.get(task_id):
                    await models.update_task_status(self.db, task_id, "paused")
                    await _notify(f"⏸ 同步已暂停，已完成 {total_forwarded}/{total}")
                    return

                is_restricted, restricted_field = restriction_cache.get(
                    source_ids[0], (False, "")
                )

                if is_restricted:
                    # 懒加载 Takeout
                    if takeout is None:
                        try:
                            takeout = await self._open_takeout()
                        except Exception as e:
                            logger.error("同步任务 #%s Takeout 开启失败: %s", task_id, e)
                            # 降级为跳过
                            last_msg_id = source_ids[-1]
                            await models.update_last_synced(self.db, task_id, last_msg_id)
                            restricted_fail += len(source_ids)
                            self._log_forward_result(
                                logger, "sync", task_id, kind, source_ids, [], restricted=True
                            )
                            continue

                    msg_count, last_msg_id, target_ids = await self._forward_restricted_unit(
                        takeout, task_id, source_chat_id,
                        target_chat_id, target_topic_id, kind, source_ids,
                    )
                    await models.update_last_synced(self.db, task_id, last_msg_id)
                    total_forwarded += msg_count
                    if target_ids:
                        restricted_success += msg_count
                    else:
                        restricted_fail += len(source_ids)

                    self._log_forward_result(
                        logger, "sync", task_id, kind, source_ids, target_ids, restricted=True
                    )
                else:
                    msg_count, last_msg_id, target_ids = await self._forward_unit(
                        task_id=task_id,
                        source_chat_id=source_chat_id,
                        target_chat_id=target_chat_id,
                        mode=mode,
                        target_topic_id=target_topic_id,
                        unit_kind=kind,
                        source_ids=source_ids,
                    )
                    await models.update_last_synced(self.db, task_id, last_msg_id)
                    total_forwarded += msg_count
                    if not target_ids:
                        failed_units += 1

                if total_forwarded % 100 == 0 and total_forwarded > 0:
                    logger.info("同步任务 #%s 进度: %d/%d", task_id, total_forwarded, total)
                    await _notify(f"📊 同步进度: {total_forwarded}/{total}")
        finally:
            await self._close_takeout(takeout)

        await models.update_task_status(self.db, task_id, "completed")
        logger.info("同步任务 #%s 完成，共转发 %d 条", task_id, total_forwarded)

        restricted_line = f"\n• 受限(Takeout): {restricted_success} 条成功"
        if restricted_fail:
            restricted_line += f", {restricted_fail} 条失败"
        await _notify(
            "✅ 同步完成"
            f"\n• 成功转发: {total_forwarded} 条"
            f"{restricted_line}"
            f"\n• 其他失败: {failed_units} 组"
        )
