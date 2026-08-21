#!/usr/bin/env bash
# 定时任务配置体检：以 jobs/tasks.conf 注册表为准，交叉核对 crontab 安装状态、
# 各任务企微 webhook 配置、个人微信收件人可达性。
# 用法：bash jobs/list_tasks.sh   （无需 root，读 tasks.conf + crontab + .env + bot 运行态）
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$DIR/tasks.conf"

# 载入 .env（只为了判断变量是否已配，不回显任何真实值）
[ -f "$DIR/../.env" ] && { set -a; source "$DIR/../.env" 2>/dev/null; set +a; }

# 统计一个 WECOM_WEBHOOK* 变量值里配了几个群：剥行内注释后按逗号/空白切 token，
# http(s) URL 直接计数；代号（= 已定义的 HOOK_* 变量名，如 HOOK_A）展开其值
# 再计（递归 ≤3 层防环）。
count_groups() {
    local raw="${1%%#*}" tok val n=0 depth="${2:-0}"
    [ "$depth" -ge 3 ] && { echo 0; return; }
    for tok in $(echo "$raw" | tr ',' ' '); do
        case "$tok" in http*) n=$((n + 1)); continue ;; esac
        if [[ "$tok" =~ ^HOOK_[A-Za-z0-9_]+$ ]]; then
            val="${!tok:-}"
            [ -n "$val" ] && n=$((n + $(count_groups "$val" "$((depth + 1))")))
        fi
    done
    echo "$n"
}

if [ ! -f "$CONF" ]; then
    echo "⚠️ 注册表 $CONF 不存在"
    exit 1
fi

echo "══ 已注册任务（tasks.conf）══"
# 任务名 | cron | webhook 变量 | 说明
while IFS='|' read -r name cron hook desc; do
    name="$(echo "$name" | xargs)"; [ -z "$name" ] && continue
    case "$name" in \#*) continue ;; esac
    cron="$(echo "$cron" | xargs)"; hook="$(echo "$hook" | xargs)"
    desc="$(echo "$desc" | xargs)"
    # cron 安装状态：crontab 里是否有 run.sh <任务名>
    if crontab -l 2>/dev/null | grep -q "run\.sh $name\>"; then
        inst="✅ 已挂 crontab"
    else
        inst="❌ 未挂 crontab（不会定时触发！）"
    fi
    # 实现文件存在性：注册名 ↔ jobs/<注册名>.py（run.sh 按此约定调起）
    if [ -f "$DIR/$name.py" ]; then
        impl="✅ $name.py"
    else
        impl="❌ $name.py 不存在（run.sh 会拒绝执行）"
    fi
    # 专属群配置状态
    val="${!hook:-}"
    if [ -n "$val" ]; then
        wh="🌐 ${hook}（$(count_groups "$val") 个群）"
    else
        wh="🌐 ${hook} 未配 → 回落 WECOM_WEBHOOK"
    fi
    # 代码关联：py 里 push() 的 hook_env 参数须与注册的 webhook 变量一致
    # （用通用 WECOM_WEBHOOK 的任务无 hook_env，跳过）
    if [ "$hook" != "WECOM_WEBHOOK" ] && [ -f "$DIR/$name.py" ]; then
        if grep -q "hook_env=\"$hook\"" "$DIR/$name.py"; then
            wh="$wh；✅ 代码已关联"
        else
            wh="$wh；❌ 代码未引用 $hook（push 的 hook_env 参数要与注册一致）"
        fi
    fi
    echo "• $name  [$cron]  $desc"
    echo "    $inst；$impl；$wh"
done < "$CONF"

# 反向核对：crontab 里有 run.sh 调用但没在注册表登记的任务
registered="$(awk -F'|' '!/^#/ && NF>=4 {gsub(/ /,"",$1); print $1}' "$CONF")"
stray="$(crontab -l 2>/dev/null | grep -o 'run\.sh [a-z_0-9]*' | awk '{print $2}' | sort -u)"
for s in $stray; do
    echo "$registered" | grep -qx "$s" || echo "⚠️ crontab 调用 run.sh $s，但 tasks.conf 未登记"
done
echo

echo "══ 通用企微 webhook（变量可逗号分隔多个群）══"
if [ -n "${WECOM_WEBHOOK:-}" ]; then
    echo "  WECOM_WEBHOOK 已配（$(count_groups "$WECOM_WEBHOOK") 个群）← 未配专属变量的任务都推这里"
else
    echo "  WECOM_WEBHOOK 未配（企微通道整体关闭，只走个人微信）"
fi
echo

echo "══ daily_update 仓库过滤 ══"
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
