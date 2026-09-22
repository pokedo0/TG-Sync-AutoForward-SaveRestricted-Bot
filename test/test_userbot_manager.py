import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import errors
from core.userbot_manager import UserBotManager


class TestUserBotManager(unittest.IsolatedAsyncioTestCase):

    def test_phone_parsing_single(self):
        config = {"phone": "+8613800138001"}
        mgr = UserBotManager(config)
        self.assertEqual(mgr.phones, ["+8613800138001"])

    def test_phone_parsing_multiple_order(self):
        config = {"phones": ["+8613800138001", "+8613800138000", "+1234567890"]}
        mgr = UserBotManager(config)
        self.assertEqual(mgr.phones, ["+8613800138001", "+8613800138000", "+1234567890"])

    def test_session_path_compatibility(self):
        with patch("os.path.exists", return_value=True):
            path = UserBotManager.get_session_path("+8613800138001", is_first=True)
            self.assertEqual(path, "sessions/userbot")

        with patch("os.path.exists", return_value=False):
            path = UserBotManager.get_session_path("+8613800138001", is_first=True)
            self.assertEqual(path, "sessions/userbot_8613800138001")

        path2 = UserBotManager.get_session_path("+8613800138000", is_first=False)
        self.assertEqual(path2, "sessions/userbot_8613800138000")

    async def test_resolve_sequential_first_hits(self):
        config = {"phones": ["+8613800138001", "+8613800138000"]}
        mgr = UserBotManager(config)

        client1 = MagicMock()
        client1.get_input_entity = AsyncMock(return_value="peer1")
        client1.get_messages = AsyncMock(return_value=MagicMock(id=192279))

        client2 = MagicMock()
        client2.get_input_entity = AsyncMock()
        client2.get_messages = AsyncMock()

        mgr.clients = [client1, client2]
        mgr._client_phone_map = {client1: "+8613800138001", client2: "+8613800138000"}

        # 探测特定消息
        resolved, err = await mgr.resolve_accessible_userbot(-1002306348030, msg_id=192279)
        self.assertEqual(resolved, client1)
        self.assertIsNone(err)

        # 验证按顺序：client1 命中后，client2 绝不应被调用
        client1.get_input_entity.assert_awaited_once_with(-1002306348030)
        client1.get_messages.assert_awaited_once_with(-1002306348030, ids=192279)
        client2.get_input_entity.assert_not_called()
        client2.get_messages.assert_not_called()

    async def test_resolve_sequential_second_hits(self):
        config = {"phones": ["+8613800138001", "+8613800138000"]}
        mgr = UserBotManager(config)

        # Client 1: 本地 Session 查不到该私有群实体
        client1 = MagicMock()
        client1.get_input_entity = AsyncMock(side_effect=ValueError("Could not find input entity"))
        client1.get_dialogs = AsyncMock()

        # Client 2: 本地存在实体且鉴权成功
        client2 = MagicMock()
        client2.get_input_entity = AsyncMock(return_value="peer2")
        client2.get_messages = AsyncMock(return_value=MagicMock(id=192279))

        mgr.clients = [client1, client2]
        mgr._client_phone_map = {client1: "+8613800138001", client2: "+8613800138000"}

        resolved, err = await mgr.resolve_accessible_userbot(-1002306348030, msg_id=192279)
        self.assertEqual(resolved, client2)
        self.assertIsNone(err)

        # 验证 client1 先被检查，失败后顺序进入 client2
        client1.get_input_entity.assert_awaited_once_with(-1002306348030)
        client2.get_input_entity.assert_awaited_once_with(-1002306348030)
        client2.get_messages.assert_awaited_once_with(-1002306348030, ids=192279)

    async def test_resolve_all_fail(self):
        config = {"phones": ["+8613800138001", "+8613800138000"]}
        mgr = UserBotManager(config)

        # 两个客户端均未加群
        client1 = MagicMock()
        client1.get_input_entity = AsyncMock(side_effect=ValueError("Could not find input entity"))
        client1.get_dialogs = AsyncMock()

        client2 = MagicMock()
        client2.get_input_entity = AsyncMock(side_effect=errors.ChannelPrivateError(None))
        client2.get_dialogs = AsyncMock()

        mgr.clients = [client1, client2]
        mgr._client_phone_map = {client1: "+8613800138001", client2: "+8613800138000"}

        resolved, err = await mgr.resolve_accessible_userbot(-1002306348030, msg_id=192279)
        self.assertIsNone(resolved)
        self.assertIn("未加入该私有频道/群组", err)


if __name__ == "__main__":
    unittest.main()
