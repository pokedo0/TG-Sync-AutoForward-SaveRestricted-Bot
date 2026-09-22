#!/usr/bin/env bash
set -e

# 定位到项目根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

echo "=========================================="
echo "  📱 Telegram UserBot 登录初始化引导"
echo "=========================================="

# 检查配置文件
if [ ! -f "config.yaml" ]; then
    echo "⚠️ 未发现 config.yaml，请先基于 config.example.yaml 创建并配置 api_id / api_hash！"
    exit 1
fi

# 1. 检查是否存在本地虚拟环境 (venv)
if [ -f "venv/bin/python" ]; then
    echo "✅ 检测到本地虚拟环境 (venv)，直接启动..."
    venv/bin/python scripts/login_userbot.py "$@"
    exit 0
fi

# 2. 检查是否有 Docker 环境
if command -v docker >/dev/null 2>&1 && [ -f "docker-compose.yaml" ]; then
    echo "🐳 检测到 Docker 环境，使用容器环境交互登录..."
    docker compose run --rm bot python scripts/login_userbot.py "$@"
    exit 0
fi

# 3. 兜底：检测系统 python3 并自动构建轻量环境
if command -v python3 >/dev/null 2>&1; then
    echo "📦 未检测到现有环境，正在为你自动初始化轻量 venv..."
    python3 -m venv venv || {
        echo "❌ 创建 venv 失败，请先安装: sudo apt update && sudo apt install -y python3-venv python3-pip"
        exit 1
    }
    echo "⬇️ 安装登录所需依赖 (telethon, pyyaml, cryptg)..."
    venv/bin/pip install --upgrade pip -q
    venv/bin/pip install telethon pyyaml cryptg -q
    echo "🚀 启动登录交互..."
    venv/bin/python scripts/login_userbot.py "$@"
    exit 0
fi

# 4. 环境全无提示
echo "❌ 未检测到可用 Python3 或 Docker！"
echo "请先在服务器执行以下任一安装命令："
echo "  - 安装 Python: sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
echo "  - 或安装 Docker: curl -fsSL https://get.docker.com | bash"
exit 1
