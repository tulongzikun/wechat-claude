# iLink + Claude 微信 Bot

把微信消息经 iLink（微信 ClawBot）协议接进来，交给一个**常驻 Claude agent**
（[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)）处理，
再原路发回。agent 自带工具——能跑命令、读写文件、联网搜索，且跨消息记得上文。

```
微信用户发消息
     │
     ▼
 iLink 长轮询 getupdates  ──►  main.py
                                   │
                            Claude Agent SDK（每用户一个常驻 agent 会话）
                            ├─ Bash / Read / Edit / Write
                            ├─ Glob / Grep
                            └─ WebSearch / WebFetch
                                   │
 iLink sendmessage  ◄──────────────┘  （每条带唯一 client_id + 原路 context_token）
     │
     ▼
微信用户收到回复
```

## 目录结构

```
wechat/
├── bot/               # 微信通讯层：iLink 收发 + Claude agent
│   ├── main.py        #   主循环：iLink 收消息 → agent → 发回复
│   ├── claude.py      #   Claude Agent SDK 后端（per-user agent 会话 + 工具分层）
│   ├── ilink.py       #   iLink API 客户端（扫码 / 收消息 / 发消息）
│   ├── login.py       #   扫码登录 + token 持久化
│   └── start.sh / stop.sh   # 后台常驻启停
├── jobs/              # 定时任务层（cron 调用，主动推送）
│   ├── run.sh         #   统一入口：环境装载 + 时区，跑 jobs/<任务名>.py
│   ├── tasks.conf     #   任务注册表：任务名 | cron | 专属 webhook 变量 | 说明
│   ├── daily_update.py    # 每天 17:00：workspace 各仓 mainline 提交摘要
│   ├── weekly_papers.py   # 每周一 10:00：arXiv q-fin 上周论文 Top10 速递
│   ├── games_news.py      # 每周二 10:00：米哈游游戏资讯（更新/未实装情报/联动）
│   ├── macro_weekly.py    # 每周四 10:00：宏观周报（中美基本面/市场行业/国际局势/前瞻）
│   └── list_tasks.sh      # 配置体检：以注册表核对 crontab / webhook / 收件人
├── .env.example       # 本地配置模板（复制为 .env，bot 和 jobs 共用）
├── requirements.txt
└── README.md
```

bot 与 jobs 的关系：bot 负责微信通讯（被动回复 + 把每个用户的
`context_token` 落盘到 `bot/latest_ctx.json`）；jobs 是独立进程的定时任务，
推送时读 bot 落盘的 token 走 ilink，优先走企业微信群机器人 webhook
（`.env` 的 `WECOM_WEBHOOK`，无需 token、无条件主动推）。

## 推送通道（jobs/ 定时任务）

定时推送走两条**可独立配置**的通道，`push()` 会各推一份、互不影响：

| 通道 | 配置（`.env`） | 到达条件 | 特点 |
|---|---|---|---|
| 企业微信群机器人 | `WECOM_WEBHOOK`（群聊「···」→群机器人→添加，复制 webhook 地址） | 无条件 | 主动推、不依赖互动；markdown 上限 4096 字节；仅内部群可用；~20 条/分钟 |
| 个人微信（ilink） | `WECHAT_ADMIN_USERS`（收件人 user_id 列表） | 24h 内给 bot 发过消息 | 与 bot 对话同一路径，`context_token` 约 24h 时效，过期当天只在企微群收到 |

- 只想要企微：留空 `WECHAT_ADMIN_USERS` 之外无需其他操作（ilink 找不到 token 自动跳过）。
- 只想要微信：留空 `WECOM_WEBHOOK` 即回退纯 ilink。
- **每个定时任务可推不同的群**：任务专用变量优先、`WECOM_WEBHOOK` 兜底——
  `WECOM_WEBHOOK_DAILY`（每天 17:00 仓库摘要）、`WECOM_WEBHOOK_PAPERS`（每周一
  10:00 论文速递）。专用变量留空自动回落通用值；新增任务时在 `.env` 加一个
  `WECOM_WEBHOOK_<任务名>` 并在调用 `push(text, hook_env=...)` 时传入即可。
- **推多个群**：任一 webhook 变量都可写多个地址（逗号/空白分隔），一份内容
  同时发到所有群（去重保序，20 条/分钟限频内基本够用）。
- webhook 地址和 user_id 都是敏感信息，只放 `.env`（gitignore，不入库）。

## 新增推送任务（注册规范）

任务三件套：**`jobs/<任务名>.py`（实现）→ `jobs/tasks.conf`（登记）→ crontab（挂载）**，
都通过唯一的 `run.sh` 入口调起（环境装载/时区不用每个任务抄一份 wrapper）。

1. **写实现** `jobs/<任务名>.py`，复用 `daily_update` 的基建：
   ```python
   from daily_update import push, fit_bytes, log   # 双通道推送/字节预算/日志
   ```
   - 推送：`push(text, hook_env="WECOM_WEBHOOK_<大写任务名>")`——企微群 + 个人微信双通道；
   - 长度：企微 markdown 上限 **4096 字节**（中文 1 字=3 字节），全文过 `fit_bytes()`；
     LLM 产出超预算先压缩重生成一次，`fit_bytes` 兜底（整行删、绝不半句截断）；
   - LLM 输入/输出过网关内容过滤（敏感措辞会 1301 拒绝），送模型前脱敏 + try/except 兜底；
   - 支持 `--dry-run`（只抓+总结+打印，不推送）。
2. **登记**：`jobs/tasks.conf` 加一行 `任务名 | cron 表达式 | 专属 webhook 变量 | 说明`。
3. **挂载**：crontab 的 `CRON_TZ=Asia/Shanghai` 段内加一条
   `<cron> /path/to/wechat/jobs/run.sh <任务名> >> /path/to/wechat/jobs/<任务名>.log 2>&1`，
   装完 `ls -l jobs/run.sh` 确认可执行位（cron 直调脚本必须 +x）。
4. **分群（可选）**：要专属群就在 `.env` 加 `WECOM_WEBHOOK_<变量>`（可逗号分隔多个群），
   不加则回落通用 `WECOM_WEBHOOK`。

验证：`bash jobs/run.sh <任务名> --dry-run` 看输出，`bash jobs/list_tasks.sh` 体检
（会自动核对注册表 ↔ crontab ↔ webhook 三者一致性，漏挂/漏登记都会标出来）。

## 安装

```bash
cd wechat
pip install -r requirements.txt      # 含 claude-agent-sdk（捆绑原生 claude 二进制）
```

定时任务挂 crontab（统一用 `run.sh <任务名>` 调起；`CRON_TZ=Asia/Shanghai`）：

```
CRON_TZ=Asia/Shanghai
0 17 * * * /path/to/wechat/jobs/run.sh daily_update   >> /path/to/wechat/jobs/daily_update.log 2>&1
0 10 * * 1 /path/to/wechat/jobs/run.sh weekly_papers  >> /path/to/wechat/jobs/weekly_papers.log 2>&1
0 10 * * 2 /path/to/wechat/jobs/run.sh games_news     >> /path/to/wechat/jobs/games_news.log 2>&1
```

## 配置

**1. Claude 鉴权**（任选其一）：

- **复用本机已登录的 `claude`（推荐）**：Agent SDK 会 spawn claude 子进程，自动继承
  环境里的 `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`，无需额外 key。
- 或用 API key：`export ANTHROPIC_API_KEY="你的 key"`

**2. 访问控制**（决定谁能用全工具）：

```bash
cp .env.example .env
# 编辑 .env，填入你自己的微信 user_id
```

`.env` 里 `WECHAT_ADMIN_USERS` 列出的 user_id 用**全工具**（Bash/Write/Edit），
其他人只读（Read/Glob/Grep/WebSearch/WebFetch）。留空 = 所有人全工具（仅自用）。
你的 user_id 在首次发消息后可从 `bot.log` 入站消息的 `from_user_id` 字段看到。

## 运行

```bash
bash bot/start.sh      # 后台常驻（setsid + nohup，脱离终端）
tail -f bot/bot.log    # 看日志
bash bot/stop.sh       # 停止

bash jobs/list_tasks.sh                  # 配置体检：注册表 ↔ crontab ↔ webhook 一致性
bash jobs/run.sh daily_update --dry-run  # 定时任务试跑（只打印不推送，任务名见 tasks.conf）
bash jobs/run.sh weekly_papers --dry-run
```

## Bot 指令（微信里直接发，均以 `/` 开头）

| 指令 | 作用 |
|---|---|
| `/help` | 指令一览 |
| `/bg <任务>` | 后台跑长任务（完成推回，不占主循环） |
| `/jobs` | 看进行中的后台作业 |
| `/reset` | 清空当前用户的对话，下条消息开新会话 |
| `/sessions [N]` | 列出所有 Claude 会话（跨项目，按最近活动降序） |
| `/tail <序号\|id前缀> [N]` | 看某会话最近 N 条对话 |
| `/use <序号\|id前缀\|job_id>` | 把当前对话切到指定会话/某后台作业的会话继续 |
| `/exit` | 退出当前会话（transcript 保留，可 `/use` 找回） |
| `/del <序号\|id前缀>` | 硬删某会话（不可恢复；删除当前会话时一并退出） |
| `/procs` | bot 派生的 claude 子进程（PID、已跑时长、各自续的会话）+ 运行中作业 |

普通文字一律当对话内容发给 agent，不作为指令触发。会话监控基于 Claude
Agent SDK 的会话管理 API（`list_sessions` / `get_session_messages` /
`delete_session` 等，读写 `~/.claude` 的 transcript）；子进程监控扫
`/proc` 找 bot 的 claude 后代进程，从命令行的 `--resume=<id>` 关联到会话。
bot 重启会丢内存里的会话指针——用 `/sessions` + `/use` 可找回任意历史会话继续。

首次运行会生成二维码（`qr.png`），**在手机微信里**扫码登录（二维码页依赖
WeixinJSBridge，普通浏览器打不开）。登录后 `token.json` 自动写入，下次复用免扫码。

## 协议要点（踩过的坑，详见代码注释）

- **`sendmessage` 每条必须带全局唯一 `client_id`**（代码用 UUID）。多条共用同一个
  client_id（或都不带）时，微信客户端只渲染第一条——这是"只收到第一条回复"的根因。
- **回复必须原样带回入站消息的 `context_token`**，微信靠它路由到对应对话。
- `get_qrcode_status` / `getupdates` 都是**长轮询**（hold ~30s），调用超时要 ≥ 35s。
- `qrcode` 走查询参数 `?qrcode=`，放 JSON body 服务端读不到。
- 接口路径带 `/ilink/bot/` 前缀；`msg` 里要带 `message_type:2, message_state:2`；
  鉴权头要带 `X-WECHAT-UIN`。

## 安全

> ⚠️ **agent 以运行它的 OS 用户身份执行，Bash/Write 能访问该用户可访问的一切。**
> 真正的边界是 OS 用户权限，不是 `cwd`（Bash 不受 `cwd` 限制）。

默认配置下：

- `WECHAT_ADMIN_USERS` 里的人 = 全工具（等于把 shell 暴露给其微信）。
- 其他人 = 只读（无 Bash/Write/Edit）。
- `WECHAT_AGENT_CWD` 控制 agent 默认工作目录（默认 `/home/<user>`）。
- `WECHAT_AGENT_MAX_TURNS` / `WECHAT_AGENT_TIMEOUT` 限制单轮步数与耗时。

上生产前建议：Docker 容器隔离 + 专用低权限用户 + 严格的 `WECHAT_ADMIN_USERS`。

## 致谢

iLink 协议参考：[hao-ji-xing/openclaw-weixin](https://github.com/hao-ji-xing/openclaw-weixin)、
[epiral/weixin-bot](https://github.com/epiral/weixin-bot)。
