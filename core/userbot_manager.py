"""多 UserBot 管理器：维护多个 UserBot 客户端生命周期，支持按顺序探测与路由。"""
from __future__ import annotations

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

        logger.info("所有 UserBot 客户端启动完成，共计 %d 个", len(self.clients))

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

        # 若是公开用户名（如 "channel_name"），默认由主 UserBot 即可处理
        if isinstance(chat_id, str) and not chat_id.startswith("-100"):
            logger.debug("公开频道/群组 %s 直接由主 UserBot 处理", chat_id)
            return self.primary_userbot, None

        int_chat_id = int(chat_id) if isinstance(chat_id, (int, str)) and str(chat_id).lstrip("-").isdigit() else None
        if int_chat_id is None:
            return self.primary_userbot, None

        logger.info("开始按顺序探测可访问 chat_id=%s (msg_id=%s) 的 UserBot (共 %d 个候选)...",
                    int_chat_id, msg_id, len(self.clients))

        # -------------------------------------------------------------
        # 第 1 轮探测：按配置顺序检查本地 Session 缓存，命中即校验
        # -------------------------------------------------------------
        for idx, client in enumerate(self.clients):
            phone = self._client_phone_map.get(client, f"#{idx+1}")
            try:
                logger.debug("第 1 轮探测: UserBot #%d [%s] 检查本地 Session 缓存...", idx + 1, phone)
                await client.get_input_entity(int_chat_id)

                # 本地存在实体缓存，发起真实鉴权
                logger.info("UserBot #%d [%s] 本地 Session 命中 chat_id=%s，进行可读性鉴权...",
                            idx + 1, phone, int_chat_id)
                if msg_id:
                    # 检查具体消息可读性
                    msg = await client.get_messages(int_chat_id, ids=msg_id)
                    if msg is None:
                        # 消息为空可能已被删除，但只要没有抛出无权访问异常，说明对群本身有权访问
                        logger.info("UserBot #%d [%s] 可访问群 %s (目标消息 %s 不存在)",
                                    idx + 1, phone, int_chat_id, msg_id)
                    else:
                        logger.info("UserBot #%d [%s] 成功读取消息 msg_id=%s", idx + 1, phone, msg_id)
                else:
                    await client.get_entity(int_chat_id)

                logger.info("✅ UserBot #%d [%s] 鉴权通过，选定为执行客户端 (停止后续探测)",
                            idx + 1, phone)
                return client, None

            except (ValueError, errors.ChannelPrivateError,
                    errors.ChatAdminRequiredError, errors.UserNotParticipantError) as e:
                logger.info("UserBot #%d [%s] 无权访问 chat_id=%s (%s)，继续探测下一个...",
                            idx + 1, phone, int_chat_id, type(e).__name__)
                continue
            except Exception as e:
                logger.warning("UserBot #%d [%s] 探测异常 chat_id=%s: %s，继续探测下一个...",
                               idx + 1, phone, int_chat_id, e)
                continue

        # -------------------------------------------------------------
        # 第 2 轮探测（兜底）：若第一轮全未命中，对每个客户端按顺序拉取最新会话并重试
        # -------------------------------------------------------------
        logger.info("第 1 轮探测均未命中，可能近期刚加入该私有群，触发 get_dialogs 刷新后再次按顺序尝试...")
        for idx, client in enumerate(self.clients):
            phone = self._client_phone_map.get(client, f"#{idx+1}")
            try:
                logger.debug("第 2 轮探测: UserBot #%d [%s] 刷新最近会话...", idx + 1, phone)
                await client.get_dialogs(limit=30)
                await client.get_input_entity(int_chat_id)

                if msg_id:
                    await client.get_messages(int_chat_id, ids=msg_id)
                else:
                    await client.get_entity(int_chat_id)

                logger.info("✅ UserBot #%d [%s] 刷新后命中并鉴权通过 chat_id=%s，选定该客户端",
                            idx + 1, phone, int_chat_id)
                return client, None
            except Exception as e:
                logger.debug("UserBot #%d [%s] 刷新后仍无法访问: %s", idx + 1, phone, e)
                continue

        logger.warning("❌ 所有配置的 UserBot（共 %d 个）均无法访问私有会话 chat_id=%s",
                       len(self.clients), int_chat_id)
        return None, "所有配置的 UserBot 均未加入该私有频道/群组或无权访问"

    async def connect_all(self) -> None:
        """重连所有 UserBot。"""
        for idx, client in enumerate(self.clients):
            phone = self._client_phone_map.get(client, f"#{idx+1}")
            try:
                if not client.is_connected():
                    await client.connect()
                    logger.info("UserBot #%d [%s] 重连成功", idx + 1, phone)
            except Exception as e:
                logger.error("UserBot #%d [%s] 重连失败: %s", idx + 1, phone, e)

    async def disconnect_all(self) -> None:
        """断开所有 UserBot。"""
        for idx, client in enumerate(self.clients):
            try:
                await client.disconnect()
            except Exception:
                pass
