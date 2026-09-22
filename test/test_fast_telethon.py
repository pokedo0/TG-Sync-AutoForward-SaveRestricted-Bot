import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fast_telethon import (
    clamp_connections,
    fast_download_file,
    fast_upload_file,
    PART_SIZE,
    MIN_PARALLEL_FILE_SIZE,
)
from core.media_transfer import MediaTransferHelper
from telethon import types


class TestFastTelethon(unittest.IsolatedAsyncioTestCase):
    def test_clamp_connections(self):
        """测试并发连接数仅约束下限（>=1），不设硬编码上限。"""
        self.assertEqual(clamp_connections(1), 1)
        self.assertEqual(clamp_connections(0), 1)
        self.assertEqual(clamp_connections(-5), 1)
        self.assertEqual(clamp_connections(4), 4)
        self.assertEqual(clamp_connections(8), 8)
        self.assertEqual(clamp_connections(16), 16)
        self.assertEqual(clamp_connections(32), 32)
        self.assertEqual(clamp_connections("abc"), 4)
        self.assertEqual(clamp_connections(None), 4)

    async def test_small_file_rejection(self):
        """测试小文件 (< 10MB) 及空文件被拦截并抛出 ValueError。"""
        mock_client = MagicMock()
        mock_client.session.dc_id = 2

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "small.bin")

            # 0 字节
            with self.assertRaises(ValueError):
                await fast_download_file(mock_client, MagicMock(), out_path, file_size=0)

            # 5 MB (小于 10MB 阈值)
            with self.assertRaises(ValueError):
                await fast_download_file(
                    mock_client, MagicMock(), out_path, file_size=5 * 1024 * 1024
                )

            # 上传小文件拦截
            with open(out_path, "wb") as f:
                f.write(b"a" * 1024)
            with self.assertRaises(ValueError):
                await fast_upload_file(mock_client, out_path)

    async def test_fast_download_same_dc(self):
        """测试同 DC 下载：直接使用 session.auth_key 连接，不调用 _borrow_exported_sender。"""
        mock_client = MagicMock()
        mock_client.session.dc_id = 2
        mock_client.session.auth_key = b"my_auth_key"
        mock_client._get_dc = AsyncMock(return_value=MagicMock(ip_address="127.0.0.1", port=443, id=2))
        mock_client._connection = MagicMock()
        mock_client._borrow_exported_sender = AsyncMock()

        test_file_size = 12 * 1024 * 1024
        mock_location = MagicMock()
        mock_location.dc_id = 2
        mock_location.size = test_file_size

        disconnected_senders = []

        class FakeSender:
            def __init__(self, auth_key, **kwargs):
                self.auth_key = auth_key
            async def connect(self, conn):
                pass
            async def send(self, req):
                res = MagicMock()
                res.bytes = b"X" * PART_SIZE
                return res
            async def disconnect(self):
                disconnected_senders.append(self)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "out_same_dc.bin")

            with patch("core.fast_telethon.utils.get_input_location", return_value=(2, mock_location)):
                with patch("core.fast_telethon.MTProtoSender", side_effect=FakeSender):
                    result = await fast_download_file(
                        client=mock_client,
                        location=mock_location,
                        out_file_path=out_path,
                        file_size=test_file_size,
                        connection_count=4,
                    )

            self.assertEqual(result, out_path)
            self.assertTrue(os.path.exists(out_path))
            self.assertEqual(os.path.getsize(out_path), test_file_size)
            # 同 DC 不应调用 _borrow_exported_sender
            mock_client._borrow_exported_sender.assert_not_called()
            # 4 个 sender 均安全断开
            self.assertEqual(len(disconnected_senders), 4)

    async def test_fast_download_cross_dc(self):
        """测试异地 DC 下载：首个连接调用 _borrow_exported_sender，其余复用 auth_key。"""
        mock_client = MagicMock()
        mock_client.session.dc_id = 2  # 本地在 DC 2
        mock_client._get_dc = AsyncMock(return_value=MagicMock(ip_address="127.0.0.1", port=443, id=5))
        mock_client._connection = MagicMock()

        # 模拟被借调的首个 sender
        mock_borrowed = AsyncMock()
        mock_borrowed.auth_key = b"exported_auth_key_dc5"
        mock_res = MagicMock()
        mock_res.bytes = b"D" * PART_SIZE
        mock_borrowed.send = AsyncMock(return_value=mock_res)

        mock_client._borrow_exported_sender = AsyncMock(return_value=mock_borrowed)
        mock_client._return_exported_sender = AsyncMock()

        test_file_size = 12 * 1024 * 1024
        mock_location = MagicMock()
        mock_location.dc_id = 5  # 文件在异地 DC 5
        mock_location.size = test_file_size

        disconnected_senders = []

        class FakeSender:
            def __init__(self, auth_key, **kwargs):
                self.auth_key = auth_key
            async def connect(self, conn):
                pass
            async def send(self, req):
                res = MagicMock()
                res.bytes = b"D" * PART_SIZE
                return res
            async def disconnect(self):
                disconnected_senders.append(self)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "out_cross_dc.bin")

            with patch("core.fast_telethon.utils.get_input_location", return_value=(5, mock_location)):
                with patch("core.fast_telethon.MTProtoSender", side_effect=FakeSender):
                    result = await fast_download_file(
                        client=mock_client,
                        location=mock_location,
                        out_file_path=out_path,
                        file_size=test_file_size,
                        connection_count=4,
                    )

            self.assertEqual(result, out_path)
            self.assertTrue(os.path.exists(out_path))
            # 首个连接借调并归还
            mock_client._borrow_exported_sender.assert_awaited_once_with(5)
            mock_client._return_exported_sender.assert_awaited_once_with(mock_borrowed)
            # 另外 3 个额外建立的连接安全断开
            self.assertEqual(len(disconnected_senders), 3)

    async def test_fast_upload_same_dc_success(self):
        """测试同 DC 上传（核心修复场景）：使用本地 auth_key，不触发 ExportAuthorizationRequest。"""
        mock_client = MagicMock()
        mock_client.session.dc_id = 5
        mock_client.session.auth_key = b"bot_auth_key"
        mock_client._get_dc = AsyncMock(return_value=MagicMock(ip_address="127.0.0.1", port=443, id=5))
        mock_client._connection = MagicMock()
        mock_client._borrow_exported_sender = AsyncMock()

        disconnected_senders = []

        class FakeUploadSender:
            def __init__(self, auth_key, **kwargs):
                self.auth_key = auth_key
            async def connect(self, conn):
                pass
            async def send(self, req):
                return True
            async def disconnect(self):
                disconnected_senders.append(self)

        test_file_size = 11 * 1024 * 1024

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "video.mp4")
            with open(file_path, "wb") as f:
                f.write(b"Z" * test_file_size)

            with patch("core.fast_telethon.MTProtoSender", side_effect=FakeUploadSender):
                input_file = await fast_upload_file(
                    client=mock_client,
                    file_path=file_path,
                    connection_count=4,
                )

            self.assertIsInstance(input_file, types.InputFileBig)
            self.assertEqual(input_file.name, "video.mp4")
            self.assertEqual(input_file.parts, (test_file_size + PART_SIZE - 1) // PART_SIZE)
            # 同 DC 上传绝不调用 _borrow_exported_sender（避开 Telegram 报错）
            mock_client._borrow_exported_sender.assert_not_called()
            # 4 条连接全部安全断开
            self.assertEqual(len(disconnected_senders), 4)

    async def test_fast_upload_error_cleanup(self):
        """测试并发上传发生异常时，所有连接全部安全断开。"""
        mock_client = MagicMock()
        mock_client.session.dc_id = 5
        mock_client.session.auth_key = b"bot_auth_key"
        mock_client._get_dc = AsyncMock(return_value=MagicMock(ip_address="127.0.0.1", port=443, id=5))
        mock_client._connection = MagicMock()

        disconnected_senders = []

        class FakeErrorSender:
            def __init__(self, auth_key, **kwargs):
                self.auth_key = auth_key
            async def connect(self, conn):
                pass
            async def send(self, req):
                raise RuntimeError("FLOOD_PREMIUM_WAIT_3")
            async def disconnect(self):
                disconnected_senders.append(self)

        test_file_size = 11 * 1024 * 1024

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "video.mp4")
            with open(file_path, "wb") as f:
                f.write(b"Y" * test_file_size)

            with patch("core.fast_telethon.MTProtoSender", side_effect=FakeErrorSender):
                with self.assertRaises(RuntimeError):
                    await fast_upload_file(
                        client=mock_client,
                        file_path=file_path,
                        connection_count=4,
                    )

            self.assertEqual(len(disconnected_senders), 4)

    async def test_media_transfer_helper_fallback(self):
        """测试 MediaTransferHelper 在并发失败时能自动降级至原生传输。"""
        mock_bot = MagicMock()
        mock_ub = MagicMock()

        helper = MediaTransferHelper(
            bot=mock_bot,
            userbot=mock_ub,
            upload_part_size_kb=512,
            download_part_size_kb=512,
            enable_fast_transfer=True,
            fast_transfer_connections=4,
            fast_transfer_min_size_mb=10,
        )

        mock_msg = MagicMock()
        mock_msg.id = 1001
        mock_msg.file = MagicMock()
        mock_msg.file.size = 15 * 1024 * 1024
        mock_msg.file.name = "test.mp4"

        with patch("core.media_transfer.fast_download_file", side_effect=RuntimeError("Fast download broke")):
            with patch.object(helper, "_download_media_with_compat", new_callable=AsyncMock) as mock_compat:
                mock_compat.return_value = "/tmp/downloaded_fallback.mp4"

                with tempfile.TemporaryDirectory() as tmpdir:
                    res = await helper.download_media_to_path(mock_msg, tmpdir)
                    self.assertEqual(res, "/tmp/downloaded_fallback.mp4")
                    mock_compat.assert_awaited_once()

    async def test_send_file_with_compat_fast_upload_success_and_fallback(self):
        """测试 send_file_with_compat 并发上传成功与异常降级行为。"""
        mock_bot = MagicMock()
        mock_bot.send_file = AsyncMock(return_value=MagicMock(id=999))
        mock_ub = MagicMock()

        helper = MediaTransferHelper(
            bot=mock_bot,
            userbot=mock_ub,
            upload_part_size_kb=512,
            download_part_size_kb=512,
            enable_fast_transfer=True,
            fast_transfer_connections=4,
            fast_transfer_min_size_mb=10,
        )

        test_file_size = 12 * 1024 * 1024
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "movie.mp4")
            with open(file_path, "wb") as f:
                f.write(b"M" * test_file_size)

            fake_input_file = types.InputFileBig(id=123, parts=24, name="movie.mp4")

            # 1. 成功场景：fast_upload_file 返回 InputFileBig，并传给 bot.send_file
            with patch("core.media_transfer.fast_upload_file", new_callable=AsyncMock) as mock_upload:
                mock_upload.return_value = fake_input_file
                await helper.send_file_with_compat(target_chat_id=-1001234, file=file_path)
                mock_bot.send_file.assert_awaited_once_with(-1001234, fake_input_file)

            mock_bot.send_file.reset_mock()

            # 2. 失败降级场景：fast_upload_file 异常，回退传递原始文件路径
            with patch("core.media_transfer.fast_upload_file", side_effect=Exception("Upload failure")):
                await helper.send_file_with_compat(target_chat_id=-1001234, file=file_path)
                mock_bot.send_file.assert_awaited_once_with(-1001234, file_path)

    async def test_send_file_with_compat_album_mixed_files(self):
        """测试相册混合文件（小图片 + 大视频）分别按阈值触发并发加速。"""
        mock_bot = MagicMock()
        mock_bot.send_file = AsyncMock(return_value=[MagicMock(id=101), MagicMock(id=102)])
        mock_ub = MagicMock()

        helper = MediaTransferHelper(
            bot=mock_bot,
            userbot=mock_ub,
            upload_part_size_kb=512,
            download_part_size_kb=512,
            enable_fast_transfer=True,
            fast_transfer_connections=4,
            fast_transfer_min_size_mb=10,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            small_photo = os.path.join(tmpdir, "pic.jpg")
            with open(small_photo, "wb") as f:
                f.write(b"P" * (500 * 1024))  # 500 KB

            large_video = os.path.join(tmpdir, "video.mp4")
            with open(large_video, "wb") as f:
                f.write(b"V" * (12 * 1024 * 1024))  # 12 MB

            fake_video_input = types.InputFileBig(id=999, parts=24, name="video.mp4")

            with patch("core.media_transfer.fast_upload_file", new_callable=AsyncMock) as mock_upload:
                mock_upload.return_value = fake_video_input

                album_files = [small_photo, large_video]
                res = await helper.send_file_with_compat(target_chat_id=-1001234, file=album_files)

                # fast_upload_file 只应对 12MB 的视频触发 1 次，不应对 500KB 图片触发
                mock_upload.assert_awaited_once_with(
                    client=mock_bot,
                    file_path=large_video,
                    connection_count=4,
                )

                # 验证传递给 send_file 的列表：图片保持原路径，视频被替换为 InputFileBig
                mock_bot.send_file.assert_awaited_once_with(
                    -1001234,
                    [small_photo, fake_video_input],
                )
                self.assertEqual(len(res), 2)

    async def test_prepare_album_media_item_video_with_thumb(self):
        """测试相册视频准备项能正确组装封面与视频文档属性。"""
        mock_bot = MagicMock()
        mock_bot.upload_file = AsyncMock(return_value=types.InputFile(888, 1, "thumb.jpg", ""))
        mock_ub = MagicMock()

        helper = MediaTransferHelper(
            bot=mock_bot,
            userbot=mock_ub,
            upload_part_size_kb=512,
            download_part_size_kb=512,
            enable_fast_transfer=True,
            fast_transfer_connections=4,
            fast_transfer_min_size_mb=10,
        )

        msg_video = MagicMock(spec=types.Message)
        msg_video.id = 555
        v_attr = types.DocumentAttributeVideo(duration=45, w=1280, h=720)
        msg_video.media = types.MessageMediaDocument(
            document=types.Document(
                id=1, access_hash=2, file_reference=b"", date=None,
                mime_type="video/mp4", size=15 * 1024 * 1024, dc_id=2,
                attributes=[v_attr],
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = os.path.join(tmpdir, "555.mp4")
            with open(video_path, "wb") as f:
                f.write(b"V" * (15 * 1024 * 1024))

            thumb_path = os.path.join(tmpdir, "555_thumb.jpg")
            with open(thumb_path, "wb") as f:
                f.write(b"T" * 1024)

            fake_video_input = types.InputFileBig(id=777, parts=30, name="555.mp4")

            with patch("core.media_transfer.fast_upload_file", new_callable=AsyncMock) as mock_fast_upload:
                mock_fast_upload.return_value = fake_video_input

                item = await helper.prepare_album_media_item(msg_video, video_path, thumb_path=thumb_path)

                self.assertIsInstance(item, types.InputMediaUploadedDocument)
                self.assertEqual(item.file, fake_video_input)
                self.assertIsNotNone(item.thumb)
                self.assertEqual(item.thumb.id, 888)
                self.assertTrue(
                    any(
                        isinstance(a, types.DocumentAttributeVideo) and a.supports_streaming
                        for a in item.attributes
                    )
                )

    async def test_large_download_mutex_lock(self):
        """测试多个大文件并发下载时由 _large_download_lock 严格保证串行互斥，防止连接乘积放大。"""
        mock_bot = MagicMock()
        mock_ub = MagicMock()
        mock_ub.download_media = AsyncMock()

        helper = MediaTransferHelper(
            bot=mock_bot,
            userbot=mock_ub,
            upload_part_size_kb=512,
            download_part_size_kb=512,
            enable_fast_transfer=True,
            fast_transfer_connections=5,
            fast_transfer_min_size_mb=10,
        )

        active_downloads = 0
        max_concurrent_downloads = 0

        async def fake_fast_download(*args, **kwargs):
            nonlocal active_downloads, max_concurrent_downloads
            active_downloads += 1
            max_concurrent_downloads = max(max_concurrent_downloads, active_downloads)
            await asyncio.sleep(0.02)
            out_file = kwargs.get("out_file_path")
            with open(out_file, "wb") as f:
                f.write(b"X" * 100)
            active_downloads -= 1
            return out_file

        def make_msg(msg_id):
            m = MagicMock(spec=types.Message)
            m.id = msg_id
            mock_file = MagicMock()
            mock_file.size = 20 * 1024 * 1024
            mock_file.name = f"{msg_id}.mp4"
            mock_file.ext = ".mp4"
            m.file = mock_file
            m.media = None
            return m

        msg1 = make_msg(1)
        msg2 = make_msg(2)
        msg3 = make_msg(3)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("core.media_transfer.fast_download_file", side_effect=fake_fast_download):
                t1 = helper.download_media_to_path(msg1, tmpdir)
                t2 = helper.download_media_to_path(msg2, tmpdir)
                t3 = helper.download_media_to_path(msg3, tmpdir)
                res = await asyncio.gather(t1, t2, t3)

                self.assertEqual(len(res), 3)
                # 核心断言：同一瞬间最多只有 1 个大文件在下载，连接数严格控制在 5
                self.assertEqual(max_concurrent_downloads, 1)

    async def test_separated_download_and_upload_connections(self):
        """测试上传与下载并发连接数可以独立设置并生效。"""
        mock_bot = MagicMock()
        mock_bot.send_file = AsyncMock(return_value=MagicMock(id=888))
        mock_ub = MagicMock()

        # 1. 独立配置场景
        helper_split = MediaTransferHelper(
            bot=mock_bot,
            userbot=mock_ub,
            upload_part_size_kb=512,
            download_part_size_kb=512,
            enable_fast_transfer=True,
            fast_transfer_connections=4,
            fast_transfer_min_size_mb=10,
            fast_download_connections=7,
            fast_upload_connections=3,
        )
        self.assertEqual(helper_split.fast_download_connections, 7)
        self.assertEqual(helper_split.fast_upload_connections, 3)

        mock_msg = MagicMock(spec=types.Message)
        mock_msg.id = 100
        mock_file = MagicMock()
        mock_file.size = 15 * 1024 * 1024
        mock_file.name = "split_test.mp4"
        mock_file.ext = ".mp4"
        mock_msg.file = mock_file
        mock_msg.media = None

        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = os.path.join(tmpdir, "split_test.mp4")
            with open(out_file, "wb") as f:
                f.write(b"S" * (15 * 1024 * 1024))

            # 验证下载传递的是 fast_download_connections (7)
            with patch("core.media_transfer.fast_download_file", new_callable=AsyncMock) as mock_dl:
                mock_dl.return_value = out_file
                await helper_split.download_media_to_path(mock_msg, tmpdir)
                mock_dl.assert_awaited_once_with(
                    client=mock_ub,
                    location=mock_msg,
                    out_file_path=os.path.join(tmpdir, "100.mp4"),
                    file_size=15 * 1024 * 1024,
                    connection_count=7,
                )

            # 验证上传传递的是 fast_upload_connections (3)
            with patch("core.media_transfer.fast_upload_file", new_callable=AsyncMock) as mock_up:
                mock_up.return_value = types.InputFileBig(id=1, parts=30, name="split_test.mp4")
                await helper_split.send_file_with_compat(-100123, out_file)
                mock_up.assert_awaited_once_with(
                    client=mock_bot,
                    file_path=out_file,
                    connection_count=3,
                )

        # 2. 缺省配置场景：未单独配置时，均回退到 fast_transfer_connections
        helper_fallback = MediaTransferHelper(
            bot=mock_bot,
            userbot=mock_ub,
            upload_part_size_kb=512,
            download_part_size_kb=512,
            enable_fast_transfer=True,
            fast_transfer_connections=5,
            fast_transfer_min_size_mb=10,
        )
        self.assertEqual(helper_fallback.fast_download_connections, 5)
        self.assertEqual(helper_fallback.fast_upload_connections, 5)


if __name__ == "__main__":
    unittest.main()
