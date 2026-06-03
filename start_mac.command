#!/bin/zsh
set -e

cd "$(dirname "$0")"

PORT="${PORT:-5057}"
URL="http://127.0.0.1:${PORT}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3。请先安装 Python 3。"
  read -k 1 "?按任意键退出..."
  exit 1
fi

if ! python3 -c "import flask, yaml" >/dev/null 2>&1; then
  echo "缺少依赖。请先运行："
  echo "  pip install -r engine/requirements.txt"
  read -k 1 "?按任意键退出..."
  exit 1
fi

if command -v lsof >/dev/null 2>&1 && lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
  echo "端口 ${PORT} 已被占用。你可以改用："
  echo "  PORT=5058 ./start_mac.command"
  read -k 1 "?按任意键退出..."
  exit 1
fi

echo "启动投资周报驾驶舱：${URL}"
(sleep 1.5; open "${URL}") >/dev/null 2>&1 &
python3 engine/app.py
