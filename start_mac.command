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

if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    PIDS_CSV=${PIDS//$'\n'/,}
    echo "端口 ${PORT} 已被占用，占用进程："
    ps -p "$PIDS_CSV" -o pid=,command= 2>/dev/null | sed 's/^/  /'
    echo ""

    # 判断占用端口的是不是本工具自己（之前没关的驾驶舱）
    MINE=1
    for pid in ${=PIDS}; do
      CMD=$(ps -p "$pid" -o command= 2>/dev/null || true)
      case "$CMD" in
        *engine/app.py*) ;;            # 是本工具
        *) MINE=0 ;;                   # 是别的程序
      esac
    done

    if [ "$MINE" -eq 1 ]; then
      echo "看起来是之前没关的『投资周报驾驶舱』。"
      read "ans?要关掉旧进程并接管端口吗？[Y/n] "
      if [ "$ans" = "" ] || [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
        echo "正在关闭旧进程 ${PIDS_CSV} ..."
        kill ${=PIDS} 2>/dev/null || true
        # 最多等约 5 秒让端口释放
        for i in {1..10}; do
          lsof -ti "tcp:${PORT}" -sTCP:LISTEN >/dev/null 2>&1 || break
          sleep 0.5
        done
        # 还没退出就强制关闭
        if lsof -ti "tcp:${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
          echo "进程未退出，强制关闭..."
          kill -9 ${=PIDS} 2>/dev/null || true
          sleep 1
        fi
        if lsof -ti "tcp:${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
          echo "仍无法释放端口 ${PORT}。可改用：PORT=5058 ./start_mac.command"
          read -k 1 "?按任意键退出..."
          exit 1
        fi
        echo "✓ 端口 ${PORT} 已释放，继续启动。"
      else
        echo "已取消。可改用其它端口：PORT=5058 ./start_mac.command"
        read -k 1 "?按任意键退出..."
        exit 1
      fi
    else
      echo "占用端口的不是本工具，未自动关闭以免影响其它程序。"
      echo "可改用其它端口：PORT=5058 ./start_mac.command"
      read -k 1 "?按任意键退出..."
      exit 1
    fi
  fi
fi

echo "启动投资周报驾驶舱：${URL}"
(sleep 1.5; open "${URL}") >/dev/null 2>&1 &
python3 engine/app.py
