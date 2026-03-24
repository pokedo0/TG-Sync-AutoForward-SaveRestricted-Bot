import asyncio
import os
import yaml
from pathlib import Path

from telethon import TelegramClient

project_root = Path(__file__).resolve().parent.parent

def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

async def _main():
    config_path = project_root / "config.yaml"
    if not config_path.exists():
        print("未找到 config.yaml")
        return

    cfg = load_config(config_path)
    api_id = cfg.get("api_id")
    api_hash = cfg.get("api_hash")
    phone = cfg.get("phone")

    if not api_id or not api_hash or not phone:
        print("config.yaml 中缺少 api_id, api_hash 或 phone")
        return

    session_path = project_root / "sessions" / "userbot"
    out_dir = project_root / "test" / "downloads"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"使用 UserBot Session: {session_path}.session")
    
    chat_id = 3195694920
    target_ids = [118, 119, 120, 121, 122]

    async with TelegramClient(str(session_path), api_id, api_hash) as client:
        # 确保登录
        if not await client.is_user_authorized():
            await client.start(phone=phone)

        print("正在启动 Takeout Session...")
        async with client.takeout() as takeout:
            print(f"尝试拉取 chat_id={chat_id}, ids={target_ids} 的消息")
            # 注意: 如果源频道是带有 -100的 channel_id, Telethon处理时有时需要加上 -100
            # 或者将其处理为 PeerChannel
            real_chat_id = chat_id if str(chat_id).startswith("-100") else int(f"-100{chat_id}")
            
            try:
                messages = await takeout.get_messages(real_chat_id, ids=target_ids)
            except Exception as e:
                print(f"获取消息失败: {e}")
                return
            
            # 1. 过滤掉获取不到的空消息 (None)
            valid_msgs = [m for m in messages if m]
            
            # 2. 按照 grouped_id 进行分组聚合（保持顺序）
            batches = []
            grouped_dict = {}
            for msg in valid_msgs:
                if getattr(msg, 'grouped_id', None):
                    gid = msg.grouped_id
                    if gid not in grouped_dict:
                        grouped_dict[gid] = []
                        batches.append(grouped_dict[gid])
                    grouped_dict[gid].append(msg)
                else:
                    batches.append([msg])
            
            # 3. 逐组进行转发操作
            for batch in batches:
                is_album = len(batch) > 1
                desc = f"相册组(共{len(batch)}张)" if is_album else "独立消息"
                first_id = batch[0].id
                
                print(f"\n=== 正在处理: {desc} | 起始ID: {first_id} ===")
                
                # 检查警告信息
                if getattr(batch[0], 'restriction_reason', None):
                    print(f"⚠️ 组首检测到封禁或限制规则: {batch[0].restriction_reason[0].reason}")

                # 过滤出存在 media 的媒体消息
                media_msgs = [m for m in batch if getattr(m, 'media', None)]
                if not media_msgs:
                    print(f"❌ 当前组由于全平台拦截或纯文本，暂无可用媒体对象 (Media 为 Null)。")
                    continue
                
                # --- 核心发出逻辑 (对应 forwarder.py _copy_album 判断机制) ---
                if len(media_msgs) == 1:
                    m = media_msgs[0]
                    print(f"✅ 单一媒体发送，类型: {type(m.media).__name__}")
                    print(f"--> 原文段落预览: {repr(m.text)[:30]}...")
                    try:
                        await takeout.send_file(
                            "me",
                            m.media,
                            caption=m.text or "",
                            formatting_entities=m.entities
                        )
                        print("🎉 成功以单挑方式带格式(Copy)发出！")
                    except Exception as e:
                        print(f"❌ 单条 Copy 异常: {e}")
                        print("建议执行备选的本地下载逻辑兜底...")
                else:
                    # 相册逻辑处理: 将多媒体转为列表，发送九宫格形态
                    files = [m.media for m in media_msgs]
                    captions = [m.text or "" for m in media_msgs]
                    print(f"✅ 相册模式群发聚合发送...")
                    try:
                        # 对于相册，只能提供一个数组 captions (格式高亮排版受限于 Telethon API 不支持带实体的数组输入)
                        await takeout.send_file(
                            "me",
                            files,
                            caption=captions
                        )
                        print("🎉 成功用原生 Album(相册) 九宫格形式完整搬运！")
                    except Exception as e:
                        print(f"❌ 相册聚合 Copy 异常: {e}")

if __name__ == "__main__":
    asyncio.run(_main())
