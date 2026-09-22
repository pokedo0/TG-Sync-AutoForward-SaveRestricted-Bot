"""交互式 UserBot 登录与会话初始化辅助脚本。

用于在独立终端中一次性完成新 UserBot 手机号的登录、验证码输入与实体缓存预热，
生成对应的 sessions/userbot_<phone>.session 文件，避免后台守护进程因无 TTY 而抛出 EOFError。
"""
import argparse
import asyncio
import os
import sys
import yaml
from telethon import TelegramClient

# 将项目根目录添加到导入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.userbot_manager import UserBotManager


def load_config(path: str = "config.yaml") -> dict:
    if not os.path.exists(path):
        print(f"❌ 找不到配置文件: {path}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


async def interactive_login(phone: str):
    config = load_config()
    api_id = config.get("api_id")
    api_hash = config.get("api_hash")

    if not api_id or not api_hash:
        print("❌ config.yaml 中缺少 api_id 或 api_hash 配置！")
        sys.exit(1)

    os.makedirs("sessions", exist_ok=True)
    session_path = UserBotManager.get_session_path(phone, is_first=False)

    print("==================================================")
    print(f"📱 准备初始化 UserBot: {phone}")
    print(f"📁 会话存储文件: {session_path}.session")
    print("==================================================")
    print("提示：如果是首次登录，Telethon 将在下方提示你输入收到的验证码（以及2FA密码）。\n")

    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.start(phone=phone)
        me = await client.get_me()
        first_name = getattr(me, "first_name", "") or ""
        username = getattr(me, "username", "") or ""
        print(f"\n🎉 登录成功！")
        print(f"👤 用户名: {first_name} (@{username}) | ID: {me.id} | Phone: {me.phone}")

        print("🔄 正在拉取最近会话以预热实体缓存 (access_hash)...")
        dialogs = await client.get_dialogs(limit=100)
        print(f"✅ 预热完成，已缓存 {len(dialogs)} 个会话/群组到本地 Session 数据库！")

        print("\n--------------------------------------------------")
        print("📝 后续步骤：")
        print(f"请确保在 config.yaml 中的 phones 列表中加入此号码：")
        print("phones:")
        print(f"  - \"{phone}\"")
        print("--------------------------------------------------")
    except Exception as e:
        print(f"\n❌ 登录或初始化失败: {e}")
        sys.exit(1)
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Telegram UserBot 交互式登录与会话初始化")
    parser.add_argument("--phone", "-p", help="手机号（带国家码，如 +8613800138000）")
    args = parser.parse_args()

    phone = args.phone
    if not phone:
        phone = input("请输入 UserBot 手机号（国际格式，例如 +8613800138000）: ").strip()

    if not phone:
        print("❌ 手机号不能为空")
        sys.exit(1)

    asyncio.run(interactive_login(phone))


if __name__ == "__main__":
    main()
