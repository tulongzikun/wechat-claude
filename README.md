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
├── main.py            # 主循环：iLink 收消息 → agent → 发回复
├── claude.py          # Claude Agent SDK 后端（per-user agent 会话 + 工具分层）
├── ilink.py           # iLink API 客户端（扫码 / 收消息 / 发消息）
├── login.py           # 扫码登录 + token 持久化
├── start.sh / stop.sh # 后台常驻启停
├── .env.example       # 本地配置模板（复制为 .env）
├── requirements.txt
└── README.md
```

## 安装

```bash
cd wechat
pip install -r requirements.txt      # 含 claude-agent-sdk（捆绑原生 claude 二进制）
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
bash start.sh     # 后台常驻（setsid + nohup，脱离终端）
tail -f bot.log   # 看日志
bash stop.sh      # 停止
```

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
