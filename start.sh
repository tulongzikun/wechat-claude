#!/usr/bin/env bash
# 后台启动微信 Bot：独立会话 + 免挂起，脱离终端后仍常驻。
# 用法: bash start.sh      （会自动停掉旧实例再起新的）
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/bot.log"
PID="$DIR/bot.pid"

# 可选：从同目录 .env 载入本地配置（不纳入版本控制，放 user_id 等本地值）
if [ -f "$DIR/.env" ]; then
    set -a
    . "$DIR/.env"
    set +a
fi

# 先停掉旧实例（避免重复登录 / 端口/游标冲突）
if [ -f "$PID" ]; then
    OLD="$(cat "$PID")"
    if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
        echo "停止旧进程 PID=$OLD"
        kill "$OLD" 2>/dev/null || true
        sleep 1
    fi
fi

# 访问控制：WECHAT_ADMIN_USERS 里的微信 user_id 用全工具（Bash/Write/Edit），
# 其他人只读。值从 .env 或环境变量读（见 .env.example），别把真实 user_id 写进这里。

# setsid: 新会话，不受控制终端 SIGHUP 影响
# </dev/null: 断开 stdin，避免阻塞
# -u: 不缓冲，二维码能立刻写进日志
setsid python3 -u "$DIR/main.py" > "$LOG" 2>&1 < /dev/null &
echo $! > "$PID"
echo "✅ 已后台启动  PID=$(cat "$PID")"
echo "   日志: $LOG"
echo "   停止: kill \$(cat $PID)   或   bash stop.sh"
