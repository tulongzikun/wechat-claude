#!/usr/bin/env bash
# 每日 17:00 由 cron 调用：拉 ~/workspace 各项目 mainline 更新 → Claude 总结 → 推送微信。
#
# cron 环境是极简的（不读 ~/.bashrc），所以这里必须手动 source：
#   ~/.bashrc  —— 提供 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL（网关）等
#   .env       —— 项目本地配置（WECHAT_ADMIN_USERS 等）
# 并把 ~/.local/bin 加进 PATH（claude / 相关工具在那里）。
#
# 注意：不能开 set -u —— ~/.bashrc 里有引用未设变量的写法，开了 nounset 会让
# source 直接退出（之前 cron 跑没输出就是这个原因）。

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1) 用户环境（ANTHROPIC 网关配置在 ~/.bashrc；非交互 source 实测可用）
# shellcheck disable=SC1090
[ -f "$HOME/.bashrc" ] && source "$HOME/.bashrc" 2>/dev/null

# 2) 项目本地 .env（在仓库根目录 = jobs/ 上一层；export 其中变量）
if [ -f "$DIR/../.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$DIR/../.env"
    set +a
fi

# 3) PATH：确保 git / python / claude 能找到
export PATH="$HOME/.local/bin:$PATH"

# 4) 时区：复用 cron 注入的 CRON_TZ（cronie 会把它传进 job 环境，已实测），
#    让脚本里的 datetime / `git --since` 都按这个时区；手动跑无 CRON_TZ 时默认上海。
#    单一来源——换时区只改 crontab 的 CRON_TZ，这里和 daily_update.py 都零硬编码。
export TZ="${CRON_TZ:-Asia/Shanghai}"

cd "$DIR"
exec python3 "$DIR/daily_update.py" "$@"
