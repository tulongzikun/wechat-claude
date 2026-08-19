#!/usr/bin/env bash
# 所有定时任务的统一入口（唯一的 wrapper，不再每个任务一份 .sh）。
#
# 用法：run.sh <任务名> [额外参数…]        # 任务名 = jobs/<任务名>.py
#   run.sh daily_update                    # 正常跑（cron 用）
#   run.sh games_news --dry-run            # 试跑参数原样透传
#
# 它做的四件事（历史上 daily_update.sh / weekly_papers.sh / games_news.sh
# 三个互为拷贝的 wrapper 的全部内容）：
#   1. source ~/.bashrc（cron 不读 bashrc，ANTHROPIC 网关配置在那里）
#   2. source 仓库根的 .env（set -a 导出 WECHAT_ADMIN_USERS / webhook 等）
#   3. PATH 加 ~/.local/bin
#   4. TZ = CRON_TZ（cron 注入）缺省 Asia/Shanghai
#
# 新增任务的注册规范见 jobs/tasks.conf 头部注释与 README「新增推送任务」。

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TASK="${1:?用法: run.sh <任务名> [额外参数…]（跑 jobs/<任务名>.py，任务清单见 tasks.conf）}"
shift
if [ ! -f "$DIR/$TASK.py" ]; then
    echo "run.sh: 未知任务「$TASK」（$DIR/$TASK.py 不存在）" >&2
    echo "已注册任务见 tasks.conf：$(awk -F'|' '!/^#/ && NF>=4 {printf "%s ", $1}' "$DIR/tasks.conf" 2>/dev/null)" >&2
    exit 1
fi

# 1) 用户环境（ANTHROPIC 网关配置在 ~/.bashrc；非交互 source 实测可用）
#    注意：不能开 set -u——.bashrc 里引用未设变量的写法会让 source 直接退出
#    （之前 cron 跑没输出就是这个原因）。
# shellcheck disable=SC1090
[ -f "$HOME/.bashrc" ] && source "$HOME/.bashrc" 2>/dev/null

# 2) 项目本地 .env（仓库根 = jobs/ 上一层；export 其中变量）
if [ -f "$DIR/../.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$DIR/../.env"
    set +a
fi

# 3) PATH：确保 git / python / claude 能找到
export PATH="$HOME/.local/bin:$PATH"

# 4) 时区：复用 cron 注入的 CRON_TZ（cronie 实测会传进 job 环境），换时区只改
#    crontab 一处；手动跑无 CRON_TZ 时默认上海（py 里的日期窗口都按进程 TZ 算）。
export TZ="${CRON_TZ:-Asia/Shanghai}"

cd "$DIR"
exec python3 "$DIR/$TASK.py" "$@"
