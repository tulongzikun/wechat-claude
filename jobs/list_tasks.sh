#!/usr/bin/env bash
# 定时任务配置体检：有哪些任务、什么时候跑、各推到哪个企微群/哪些微信。
# 用法：bash jobs/list_tasks.sh   （无需 root，读 crontab + .env + bot 运行态）
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 载入 .env（只为了判断变量是否已配，不回显任何真实值）
[ -f "$DIR/../.env" ] && { set -a; source "$DIR/../.env" 2>/dev/null; set +a; }

echo "══ 定时任务（crontab，CRON_TZ=Asia/Shanghai）══"
crontab -l 2>/dev/null | grep 'wechat/jobs/' | sed 's#/home/zhouzikun/workspace/wechat/jobs/##g' \
  || echo "（crontab 里没有 wechat 任务！）"
echo

echo "══ 企微 webhook 配置（变量可逗号分隔多个群）══"
count_groups() { echo "$1" | grep -o 'key=' | wc -l; }
if [ -n "${WECOM_WEBHOOK:-}" ]; then
    echo "  WECOM_WEBHOOK        已配（$(count_groups "$WECOM_WEBHOOK") 个群）← 通用兜底"
else
    echo "  WECOM_WEBHOOK        未配（企微通道整体关闭，只走个人微信）"
fi
# 任务专属变量从代码里自动发现，新增任务无需改本脚本
for v in $(grep -ho 'WECOM_WEBHOOK_[A-Z_]*' "$DIR"/*.py | sort -u); do
    val="${!v:-}"
    if [ -n "$val" ]; then
        echo "  $v 已配（$(count_groups "$val") 个群）"
    else
        echo "  $v 未配 → 回落 WECOM_WEBHOOK"
    fi
done
echo

echo "══ 每日仓库摘要过滤（daily_update）══"
if [ -n "${DAILY_REPO_FILTER:-}" ]; then
    echo "  DAILY_REPO_FILTER = $DAILY_REPO_FILTER（origin 含此子串的仓库才汇总）"
else
    echo "  DAILY_REPO_FILTER 未设 = ~/workspace 全部仓库"
fi
echo

echo "══ 个人微信收件人（ilink，24h 内有互动才可达）══"
admins="${WECHAT_ADMIN_USERS:-}"
if [ -n "$admins" ]; then
    n=$(echo "$admins" | tr ',' '\n' | grep -c .)
    echo "  WECHAT_ADMIN_USERS = ${n} 人"
else
    echo "  WECHAT_ADMIN_USERS 未设 = latest_ctx.json 里所有近期用户"
fi
ctx="$DIR/../bot/latest_ctx.json"
if [ -f "$ctx" ]; then
    echo "  latest_ctx.json 刷新于 $(date -r "$ctx" '+%m-%d %H:%M')（超过 24h 则个人微信收不到）"
else
    echo "  ⚠️ bot/latest_ctx.json 不存在（bot 没跑过或没收到过消息）"
fi
