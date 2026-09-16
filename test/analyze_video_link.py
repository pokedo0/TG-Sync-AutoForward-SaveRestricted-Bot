#!/usr/bin/env python3
"""Telegram 视频/相册链接深度诊断与兼容性测试工具

用法:
    python test/test/analyze_video_link.py "https://t.me/xxxx/26189"
    python test/test/analyze_video_link.py "https://t.me/xxxx/16658" --test-fwd
"""

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path
import yaml

# 项目根路径导入
project_root = Path(__file__).resolve().parents[2]
if not (project_root / "config.yaml").exists():
    project_root = Path.cwd()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from telethon import TelegramClient
from telethon.tl.types import (
    MessageMediaDocument,
    DocumentAttributeVideo,
    DocumentAttributeFilename,
)
from bot.link_parser import parse_link

sys.stdout.reconfigure(encoding="utf-8")


def load_config():
    cfg_path = project_root / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"找不到配置文件: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def check_mp4_faststart(client: TelegramClient, doc) -> tuple[bool, str]:
    """探测 MP4 文件是否有 FastStart (moov 在文件头还是文件尾)。"""
    if not doc or getattr(doc, "size", 0) == 0:
        return False, "非文件或文件为空"

    # 1. 读前 128KB
    first_chunk = b""
    async for chunk in client.iter_download(doc, offset=0, request_size=131072):
        first_chunk += chunk
        if len(first_chunk) >= 131072:
            break

    if b"moov" in first_chunk:
        return True, "✅ FastStart 正常 (moov 位于文件头部前 128KB 内，支持边下边播)"

    # 2. 若头部没有，读尾部 2MB
    offset = max(0, doc.size - 2 * 1024 * 1024)
    last_chunk = b""
    async for chunk in client.iter_download(doc, offset=offset, request_size=1048576):
        last_chunk += chunk
        if len(last_chunk) >= 2 * 1024 * 1024:
            break

    if b"moov" in last_chunk:
        pos = last_chunk.find(b"moov")
        dist = len(last_chunk) - pos
        return False, f"❌ 无 FastStart！moov 位于文件末尾（距 EOF 仅 {dist/1024:.1f} KB）。若无切片流，客户端必须下载完整 {doc.size/1024/1024:.1f}MB 才能播放！"

    return False, "⚠️ 未在文件首尾 2MB 内探测到 moov atom，可能为非标准 MP4 或其他容器。"


async def analyze_link(link: str, test_fwd: bool = False):
    parsed = parse_link(link)
    if not parsed or not parsed.msg_id:
        print(f"❌ 无法解析链接: {link}")
        return

    cfg = load_config()
    # 建立临时 session，避免锁死运行中的 main.py
    temp_session_dir = project_root / "test" / "test" / "dumps"
    temp_session_dir.mkdir(parents=True, exist_ok=True)
    temp_session = temp_session_dir / "temp_analyzer"

    src_session = project_root / "sessions" / "userbot.session"
    if src_session.exists():
        shutil.copy(src_session, f"{temp_session}.session")

    client = TelegramClient(str(temp_session), cfg["api_id"], cfg["api_hash"])
    await client.connect()

    try:
        # 1. 获取源频道实体
        channel_ref = parsed.chat_id
        try:
            entity = await client.get_entity(channel_ref)
        except Exception as e:
            print(f"❌ 无法获取频道实体 [{channel_ref}]: {e}")
            return

        noforwards = getattr(entity, "noforwards", False)
        print("=" * 60)
        print("【1. 频道基础信息】")
        print(f"  频道名称: {getattr(entity, 'title', 'N/A')}")
        print(f"  频道 ID: {entity.id}")
        print(f"  受限状态 (noforwards): {'🔴 受限 (禁止保存/转发)' if noforwards else '🟢 未受限 (允许原生转发)'}")
        print("=" * 60)

        # 2. 获取消息
        msg_id = parsed.msg_id
        msg = await client.get_messages(entity, ids=msg_id)
        if not msg:
            print(f"❌ 找不到消息 ID: {msg_id}")
            return

        # 检查是否为相册 (Album)
        album_msgs = [msg]
        if msg.grouped_id:
            surrounding = await client.get_messages(entity, ids=list(range(max(1, msg_id - 10), msg_id + 11)))
            album_msgs = [m for m in surrounding if m and m.grouped_id == msg.grouped_id]
            album_msgs.sort(key=lambda x: x.id)

        print("\n【2. 消息形态与结构】")
        if len(album_msgs) > 1:
            print(f"  形态: 媒体相册 (Album / Media Group)")
            print(f"  包含消息数: {len(album_msgs)} 条")
            print(f"  消息 ID 列表: {[m.id for m in album_msgs]}")
        else:
            print(f"  形态: 单条独立消息 (ID: {msg_id})")

        # 3. 逐个分析媒体
        print("\n【3. 媒体与切片详细诊断】")
        for i, m in enumerate(album_msgs):
            prefix = f"  [媒体 #{i+1} - 消息 {m.id}]"
            doc = getattr(m.media, "document", None)
            if not doc:
                if getattr(m.media, "photo", None):
                    print(f"{prefix} 静态照片 (Photo)")
                else:
                    print(f"{prefix} 无文件媒体或纯文本")
                continue

            # 属性提取
            size_mb = doc.size / 1024 / 1024
            video_attr = None
            filename_attr = None
            for a in doc.attributes:
                if isinstance(a, DocumentAttributeVideo):
                    video_attr = a
                elif isinstance(a, DocumentAttributeFilename):
                    filename_attr = a

            alt_docs = getattr(m.media, "alt_documents", None) or []
            print(f"{prefix}")
            print(f"    ├─ 文件大小: {size_mb:.2f} MB ({doc.size} 字节)")
            print(f"    ├─ MIME 类型: {doc.mime_type}")
            if video_attr:
                print(f"    ├─ 视频属性: {video_attr.w}x{video_attr.h}, 时长={video_attr.duration:.1f}秒, 流式标志(supports_streaming)={video_attr.supports_streaming}")
            if filename_attr:
                print(f"    ├─ ⚠️ 附加文件名: '{filename_attr.file_name}' (注意: 桌面端可能视作普通文件附件)")
            else:
                print(f"    ├─ 原生视频: 无文件名属性 (纯视频流)")

            # alt_documents 切片
            print(f"    ├─ 切片与转码 (alt_documents): 共 {len(alt_docs)} 个")
            if alt_docs:
                for idx, alt in enumerate(alt_docs):
                    codec = "未知"
                    res = ""
                    for a in alt.attributes:
                        if hasattr(a, "video_codec"):
                            codec = a.video_codec
                        if hasattr(a, "h"):
                            res = f"{a.w}x{a.h} "
                    print(f"    │    ├─ [{idx}] {res}{alt.mime_type} ({alt.size/1024/1024:.2f}MB, 编码={codec})")

            # MP4 FastStart 探测
            if doc.mime_type == "video/mp4":
                print(f"    └─ MP4 封装结构探测中...")
                is_fast, msg_fast = await check_mp4_faststart(client, doc)
                print(f"         {msg_fast}")

        # 4. 推荐转存策略与表现预测
        print("\n" + "=" * 60)
        print("【4. 转存策略表现与兼容性预测】")
        if not noforwards:
            print("  🟢 最佳方案: 原生匿名转发 `forward_messages(..., drop_author=True)`")
            print("     - 表现: 100% 保留所有清晰度切片 (Original / 720p / 480p 等)")
            print("     - 播放: 无论母文件是否有 FastStart，均可通过 Telegram HLS 切片流秒开播放")
            print("     - 外观: 没有任何转发来源提示，与 Copy 效果一致")
            if len(album_msgs) > 1:
                print("     - 相册: 完整保留相册聚合九宫格，不会被打散")
        else:
            print("  🔴 该频道受限 (`noforwards: True`)，无法使用原生转发。")
            print("  ⚠️ 若使用 `send_file(msg.media)` (Copy 模式):")
            print("     - Telegram 云端强制清空 `alt_documents`（切片全部丢失）")
            print("     - 客户端播放器只能降级请求原始母文件")
            print("     - 若母文件无 FastStart 且体积较大，将无法边下边播，出现严重转圈卡顿")

        # 5. 可选无害实测
        if test_fwd:
            print("\n【5. 实时转发测试 (--test-fwd)】")
            if noforwards:
                print("  ❌ 频道已受限，无法进行原生转发测试。")
            else:
                test_ids = [m.id for m in album_msgs]
                print(f"  正在向 Saved Messages ('me') 测试 forward_messages(drop_author=True)...")
                try:
                    fwd_res = await client.forward_messages("me", test_ids, entity, drop_author=True)
                    if not isinstance(fwd_res, list):
                        fwd_res = [fwd_res]
                    print(f"  ✅ 转发成功！返回 {len(fwd_res)} 条消息:")
                    for r in fwd_res:
                        r_alt = len(getattr(r.media, "alt_documents", None) or [])
                        print(f"     - 消息 {r.id}: grouped_id={r.grouped_id}, fwd_from={r.fwd_from}, alt_documents={r_alt}")
                    # 自动清理
                    await client.delete_messages("me", [r.id for r in fwd_res])
                    print("  🧹 已自动撤回并清理测试消息。")
                except Exception as e:
                    print(f"  ❌ 转发测试失败: {e}")

        print("=" * 60)

    finally:
        await client.disconnect()
        # 清理临时 session 文件
        for f in temp_session_dir.glob("temp_analyzer.*"):
            try:
                f.unlink()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Telegram 视频/相册链接深度诊断与兼容性测试工具")
    parser.add_argument("link", help="Telegram 消息链接 (如 https://t.me/cuckold_china/26189)")
    parser.add_argument("--test-fwd", action="store_true", help="是否执行无痕转发实测 (向 Saved Messages 发送后自动清理)")
    args = parser.parse_args()

    asyncio.run(analyze_link(args.link, test_fwd=args.test_fwd))


if __name__ == "__main__":
    main()
