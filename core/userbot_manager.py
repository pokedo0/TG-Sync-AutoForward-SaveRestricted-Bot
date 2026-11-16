"""多 UserBot 管理器：维护多个 UserBot 客户端生命周期，支持按顺序探测与路由。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import TYPE_CHECKING

from telethon import TelegramClient, errors

if TYPE_CHECKING:
    pass

logger = logging.getLogger("tg_forward_bot.userbot_manager")


class UserBotManager:
    """管理多个 UserBot 实例，支持按顺序探测私有会话可访问性。"""

    def __init__(self, config: dict):
        self.config = config
        self.api_id = config.get("api_id")
        self.api_hash = config.get("api_hash")

        # 统一归一化手机号列表，严格保持用户配置的顺序
        phones_cfg = config.get("phones")
        if isinstance(phones_cfg, list):
            self.phones: list[str] = [str(p).strip() for p in phones_cfg if str(p).strip()]
        elif config.get("phone"):
            self.phones = [str(config["phone"]).strip()]
        else:
            self.phones = []

        self.clients: list[TelegramClient] = []
        self._client_phone_map: dict[TelegramClient, str] = {}
        self._session_paths: list[str] = []
        self._connect_locks: dict[TelegramClient, asyncio.Lock] = {}
        self._connect_epochs: dict[TelegramClient, int] = {}

    @property
    def primary_userbot(self) -> TelegramClient:
        """主 UserBot（配置列表中的第一个），保持向后兼容。"""
        if not self.clients:
            raise RuntimeError("未初始化任何 UserBot 客户端")
        return self.clients[0]

    @staticmethod
    def get_session_path(phone: str, is_first: bool = False) -> str:
        """根据手机号计算 session 文件存储路径。"""
        clean_phone = re.sub(r"[^\w]", "", phone)
        # 若为首个账号且旧的 userbot.session 存在，优先沿用以避免重新登录
        if is_first and os.path.exists("sessions/userbot.session"):
            return "sessions/userbot"
        # 否则使用基于手机号命名的独立 session 文件
        return f"sessions/userbot_{clean_phone}"

    async def start(self) -> None:
        """按顺序初始化并启动所有配置的 UserBot。"""
        if not self.phones:
            logger.error("config.yaml 中未配置 phone 或 phones")
            raise ValueError("未配置任何 UserBot 手机号")

        os.makedirs("sessions", exist_ok=True)
        self.clients.clear()
        self._client_phone_map.clear()
        self._session_paths.clear()
        self._connect_locks.clear()
        self._connect_epochs.clear()

        for idx, phone in enumerate(self.phones):
            is_first = (idx == 0)
            session_path = self.get_session_path(phone, is_first=is_first)
            self._session_paths.append(session_path)

            logger.info("正在启动 UserBot #%d [%s] (Session: %s)...",
                        idx + 1, phone, session_path)
            client = TelegramClient(
                session_path,
                self.api_id,
                self.api_hash,
                connection_retries=10,
                retry_delay=5,
                timeout=30,
            )
            # 记录手机号属性，方便调试与日志
            setattr(client, "_phone", phone)
            setattr(client, "_userbot_index", idx + 1)

            await client.start(phone=phone)
            me = await client.get_me()
            first_name = getattr(me, "first_name", "") or ""
            username = getattr(me, "username", "") or ""
            logger.info("UserBot #%d [%s] 已登录: %s%s (ID: %s)",
                        idx + 1, phone, first_name,
                        f" (@{username})" if username else "", me.id)

            # 启动时执行轻量会话预热，将已加入群组/频道的 access_hash 刷入本地 Session 数据库
            try:
                dialogs = await client.get_dialogs(limit=50)
                logger.info("UserBot #%d [%s] 启动预热完成，已缓存 %d 个会话实体",
                            idx + 1, phone, len(dialogs))
            except Exception as e:
                logger.warning("UserBot #%d [%s] 启动预热会话失败（不影响主流程）: %s",
                               idx + 1, phone, e)

            self.clients.append(client)
            self._client_phone_map[client] = phone
            self._connect_locks[client] = asyncio.Lock()
            self._connect_epochs[client] = 0

        logger.info("所有 UserBot 客户端启动完成，共计 %d 个", len(self.clients))

    def _get_client_label(self, client: TelegramClient, idx: int | None = None) -> str:
        """生成统一的 UserBot 日志标识（如 '#2 [+8613148368821]'）。"""
        if idx is None:
            try:
                idx = self.clients.index(client)
            except ValueError:
                ub_idx = getattr(client, "_userbot_index", None)
                idx = (ub_idx - 1) if isinstance(ub_idx, int) and ub_idx > 0 else None
        phone = self._client_phone_map.get(client) or getattr(client, "_phone", None)
        if idx is not None and phone:
            return f"#{idx + 1} [{phone}]"
        if idx is not None:
            return f"#{idx + 1}"
        return f"[{phone}]" if phone else "#?"

    @staticmethod
    def _is_client_connected(client: TelegramClient) -> bool:
        is_conn = getattr(client, "is_connected", None)
        return bool(is_conn()) if callable(is_conn) else True

    @staticmethod
    def _is_connection_error(exc: Exception) -> bool:
        return isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError))

    async def ensure_connected(
        self,
        client: TelegramClient,
        *,
        force_reconnect: bool = False,
        expected_epoch: int | None = None,
    ) -> tuple[bool, str | None]:
        """确保指定 UserBot 已连接；按 client 加锁避免并发请求重复建连。"""
        if not force_reconnect and self._is_client_connected(client):
            return True, None

        lock = self._connect_locks.setdefault(client, asyncio.Lock())
        label = self._get_client_label(client)
        async with lock:
            cur_epoch = self._connect_epochs.get(client, 0)
            if force_reconnect:
                # 另一个协程在等待期间已完成重连时直接复用新连接，避免拆掉刚建好的链路
                if expected_epoch is not None and cur_epoch != expected_epoch and self._is_client_connected(client):
                    logger.debug("UserBot %s 连接已被并发请求刷新，跳过重复重连", label)
                    return True, None
                logger.warning("UserBot %s 检测到连接异常，正在断开并强制重连...", label)
                try:
                    await client.disconnect()
                except Exception:
                    pass
            else:
                if self._is_client_connected(client):
                    return True, None
                logger.info("UserBot %s 检测到已断线，正在自动重连...", label)

            try:
                await client.connect()
                self._connect_epochs[client] = cur_epoch + 1
                logger.info("UserBot %s 自动重连成功", label)
                return True, None
            except Exception as e:
                err_desc = f"{type(e).__name__}: {e}"
                logger.error("UserBot %s 自动重连失败: %s", label, err_desc)
                return False, err_desc

    async def _probe_client_access(
        self,
        idx: int,
        client: TelegramClient,
        int_chat_id: int,
        msg_id: int | None,
        *,
        refresh_dialogs: bool,
    ) -> tuple[bool, str | None]:
        """探测单个 UserBot 对目标会话的可读性，连接异常时自动重连并重试一次。"""
        label = self._get_client_label(client, idx)
        ok, conn_err = await self.ensure_connected(client)
        if not ok:
            return False, conn_err

        for attempt in range(2):
            epoch = self._connect_epochs.get(client, 0)
            try:
                if refresh_dialogs:
                    logger.debug("第 2 轮探测: UserBot %s 刷新最近会话...", label)
                    await client.get_dialogs(limit=30)
                else:
                    logger.debug("第 1 轮探测: UserBot %s 检查本地 Session 缓存...", label)

                await client.get_input_entity(int_chat_id)

                if not refresh_dialogs:
                    logger.info("UserBot %s 本地 Session 命中 chat_id=%s，进行可读性鉴权...",
                                label, int_chat_id)

                if msg_id:
                    msg = await client.get_messages(int_chat_id, ids=msg_id)
                    if not refresh_dialogs:
                        if msg is None:
                            # 消息为空可能已被删除，但未抛无权异常说明对群本身有权访问
                            logger.info("UserBot %s 可访问群 %s (目标消息 %s 不存在)",
                                        label, int_chat_id, msg_id)
                        else:
                            logger.info("UserBot %s 成功读取消息 msg_id=%s", label, msg_id)
                else:
                    await client.get_entity(int_chat_id)

                if refresh_dialogs:
                    logger.info("✅ UserBot %s 刷新后命中并鉴权通过 chat_id=%s，选定该客户端",
                                label, int_chat_id)
                else:
                    logger.info("✅ UserBot %s 鉴权通过，选定为执行客户端 (停止后续探测)", label)
                return True, None

            except (ValueError, errors.ChannelPrivateError,
                    errors.ChatAdminRequiredError, errors.UserNotParticipantError) as e:
                if refresh_dialogs:
                    logger.debug("UserBot %s 刷新后仍无法访问 (%s)", label, type(e).__name__)
                else:
                    logger.info("UserBot %s 无权访问 chat_id=%s (%s)，继续探测下一个...",
                                label, int_chat_id, type(e).__name__)
                return False, None
            except Exception as e:
                if self._is_connection_error(e) and attempt == 0:
                    logger.warning("UserBot %s 探测时连接异常 (%s)，尝试重连后重试...", label, e)
                    ok, conn_err = await self.ensure_connected(
                        client, force_reconnect=True, expected_epoch=epoch
                    )
                    if ok:
                        continue
                    return False, conn_err

                if self._is_connection_error(e):
                    err_desc = f"{type(e).__name__}: {e}"
                    logger.warning("UserBot %s 重连后仍连接异常 chat_id=%s: %s",
                                   label, int_chat_id, err_desc)
                    return False, err_desc

                if refresh_dialogs:
                    logger.debug("UserBot %s 刷新后仍无法访问: %s", label, e)
                else:
                    logger.warning("UserBot %s 探测异常 chat_id=%s: %s，继续探测下一个...",
                                   label, int_chat_id, e)
                return False, None

        return False, None

    async def resolve_accessible_userbot(
        self,
        chat_id: int | str,
        msg_id: int | None = None,
    ) -> tuple[TelegramClient | None, str | None]:
        """按配置顺序探测哪个 UserBot 可以访问指定的 chat_id。

        返回 (client, error_message)。
        若探测成功，返回 (client, None)；
        若所有 UserBot 均不可访问，返回 (None, 错误描述)。
        """
        if not self.clients:
            return None, "没有可用的 UserBot 客户端"

        # 若是公开用户名（如 "channel_name"）或非数字 chat_id，默认由主 UserBot 处理
        int_chat_id: int | None = None
        if not (isinstance(chat_id, str) and not chat_id.startswith("-100")):
            if isinstance(chat_id, (int, str)) and str(chat_id).lstrip("-").isdigit():
                int_chat_id = int(chat_id)

        if int_chat_id is None:
            logger.debug("公开/非数字会话 %s 直接由主 UserBot 处理", chat_id)
            primary = self.primary_userbot
            ok, conn_err = await self.ensure_connected(primary)
            if not ok:
                return None, f"主 UserBot 连接失败，请稍后重试 ({conn_err})"
            return primary, None

        logger.info("开始按顺序探测可访问 chat_id=%s (msg_id=%s) 的 UserBot (共 %d 个候选)...",
                    int_chat_id, msg_id, len(self.clients))

        conn_failures: list[str] = []
        failed_conn_indices: set[int] = set()

        # -------------------------------------------------------------
        # 第 1 轮探测：按配置顺序检查本地 Session 缓存，命中即校验
        # -------------------------------------------------------------
        for idx, client in enumerate(self.clients):
            accessible, conn_err = await self._probe_client_access(
                idx, client, int_chat_id, msg_id, refresh_dialogs=False
            )
            if accessible:
                return client, None
            if conn_err:
                failed_conn_indices.add(idx)
                conn_failures.append(f"{self._get_client_label(client, idx)}: {conn_err}")

        # -------------------------------------------------------------
        # 第 2 轮探测（兜底）：若第一轮全未命中，对每个客户端按顺序拉取最新会话并重试
        # -------------------------------------------------------------
        logger.info("第 1 轮探测均未命中，可能近期刚加入该私有群，触发 get_dialogs 刷新后再次按顺序尝试...")
        for idx, client in enumerate(self.clients):
            if idx in failed_conn_indices:
                continue
            accessible, conn_err = await self._probe_client_access(
                idx, client, int_chat_id, msg_id, refresh_dialogs=True
            )
            if accessible:
                return client, None
            if conn_err:
                conn_failures.append(f"{self._get_client_label(client, idx)}: {conn_err}")

        if conn_failures:
            logger.warning("❌ 探测私有会话 chat_id=%s 失败（存在连接异常: %s）",
                           int_chat_id, "; ".join(conn_failures))
            return None, "部分 UserBot 连接异常或重连失败，暂无法访问该私有频道/群组，请稍后重试"

        logger.warning("❌ 所有配置的 UserBot（共 %d 个）均无法访问私有会话 chat_id=%s",
                       len(self.clients), int_chat_id)
        return None, "所有配置的 UserBot 均未加入该私有频道/群组或无权访问"

    async def connect_all(self) -> None:
        """重连所有 UserBot。"""
        for client in self.clients:
            await self.ensure_connected(client)

    async def disconnect_all(self) -> None:
        """断开所有 UserBot。"""
        for idx, client in enumerate(self.clients):
            try:
                await client.disconnect()
            except Exception:
                pass
