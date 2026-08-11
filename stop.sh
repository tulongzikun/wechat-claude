#!/usr/bin/env bash
# 停止后台 Bot。
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID="$DIR/bot.pid"

if [ ! -f "$PID" ]; then
    echo "没有 pid 文件，尝试按进程名查找 ..."
    PIDS="$(pgrep -f "$DIR/main.py" || true)"
else
    PIDS="$(cat "$PID")"
fi

if [ -z "$PIDS" ]; then
    echo "没有在运行的 Bot 进程。"
    exit 0
fi

for p in $PIDS; do
    if kill -0 "$p" 2>/dev/null; then
        echo "停止进程 PID=$p"
        kill "$p" 2>/dev/null || true
    fi
done
sleep 1
rm -f "$PID"
echo "已停止。"
