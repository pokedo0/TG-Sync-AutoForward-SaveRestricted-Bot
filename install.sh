#!/bin/bash

# 遇到错误即刻退出
set -e

# 获取当前工作目录
APP_DIR=$(pwd)
SERVICE_NAME="forward-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER=$(whoami)

echo "====================================="
echo "  🚀 开始安装 TG-Sync-AutoForward-SaveRestricted-Bot (Debian)"
echo "====================================="

# 检查是否以 root 或 sudo 运行（因为写入 /etc/systemd/ 需要权限）
if [ "$EUID" -ne 0 ]; then
  echo "❌ 请使用 root 权限运行此脚本 (例如: sudo bash install.sh)"
  exit 1
fi

# 检查 config.yaml 是否存在
if [ ! -f "${APP_DIR}/config.yaml" ]; then
    echo "⚠️ 警告：未找到 config.yaml 文件！"
    echo "请在继续启动服务前，复制 config.example.yaml 为 config.yaml 并填好配置哦。"
fi

echo "📦 正在创建 Python 虚拟环境 (venv)..."
# 如果没有安装 python3-venv，可能需要提示
if ! command -v python3 &> /dev/null; then
    echo "❌ 找不到 python3，请先执行: apt update && apt install python3 python3-venv python3-pip"
    exit 1
fi

if [ ! -d "venv" ]; then
    python3 -m venv venv
else
    echo "✅ 发现已有的 venv，跳过创建..."
fi
# 激活环境
source venv/bin/activate

echo "⬇️ 正在安装依赖..."
pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    pip install --upgrade -r requirements.txt
else
    echo "⚠️ 找不到 requirements.txt，跳过安装依赖。"
fi

echo "⚙️ 正在生成 Systemd 服务文件: ${SERVICE_FILE}"
cat > "${SERVICE_FILE}" << EOF
[Unit]
Description=Telegram Forwarder Bot service
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "🔄 重新加载 Systemd 配置..."
systemctl daemon-reload

echo "✅ 设置服务开机自启..."
systemctl enable ${SERVICE_NAME}.service

echo "▶️ 启动服务..."
systemctl restart ${SERVICE_NAME}.service

echo "====================================="
echo "🎉 安装完成且服务已启动！"
echo "====================================="
echo "常用系统命令指令:"
echo "状态检查  : systemctl status ${SERVICE_NAME}"
echo "查看日志  : journalctl -u ${SERVICE_NAME} -f"
echo "停止服务  : systemctl stop ${SERVICE_NAME}"
echo "重启服务  : systemctl restart ${SERVICE_NAME}"
echo "禁用自启  : systemctl disable ${SERVICE_NAME}"
