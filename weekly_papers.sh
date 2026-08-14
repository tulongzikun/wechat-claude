#!/usr/bin/env bash
# 每周一 10:00 由 cron 调用：抓 arXiv q-fin 上周论文 → Claude 总结 → 推送微信。
#
# cron 环境极简（不读 ~/.bashrc），这里手动 source：
#   ~/.bashrc  —— ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL（网关）
#   .env       —— WECHAT_ADMIN_USERS 等
# 并把 ~/.local/bin 加进 PATH。不能开 set -u——.bashrc 引用未设变量会让 source 退出。
#
# 时区：复用 cron 注入的 CRON_TZ（cronie 实测会传进 job 环境），
# 让 weekly_papers.py 按 Asia/Shanghai 算「上个自然周」。手动跑无 CRON_TZ 时默认上海。

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1) 用户环境（ANTHROPIC 网关配置在 ~/.bashrc；非交互 source 实测可用）
# shellcheck disable=SC1090
[ -f "$HOME/.bashrc" ] && source "$HOME/.bashrc" 2>/dev/null

# 2) 项目本地 .env（export 其中的变量，如 WECHAT_ADMIN_USERS）
if [ -f "$DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$DIR/.env"
    set +a
fi

# 3) PATH：确保 git / python / claude 能找到
export PATH="$HOME/.local/bin:$PATH"

# 4) 时区：复用 cron 注入的 CRON_TZ，手动跑默认上海（与 daily_update 一致，单一来源）
export TZ="${CRON_TZ:-Asia/Shanghai}"

cd "$DIR"
exec python3 "$DIR/weekly_papers.py" "$@"
