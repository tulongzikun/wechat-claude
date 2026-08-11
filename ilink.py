"""iLink WeChat Bot API 客户端。

iLink 是微信官方提供的 Bot 协议，本身只负责消息收发，不做 AI。
本模块封装了四个核心能力：
    1. 获取登录二维码   get_qrcode()
    2. 轮询扫码状态     get_qrcode_status()
    3. 长轮询拉取消息   get_updates()
    4. 发送消息         send_message()

⚠️ 关键：context_token 必须原样保存，回复时必须带回。
   微信靠它判断"这条回复要发回哪条会话"，丢了就不知道回复给谁。
"""

import base64
import os
import uuid

import requests

# 未登录时的接口前缀（取二维码 / 查状态走这里）
BASE_URL = "https://ilinkai.weixin.qq.com"


class ILinkError(Exception):
    """iLink 接口返回异常时抛出。"""


class ILinkClient:
    def __init__(self, bot_token: str = "", baseurl: str = ""):
        """
        bot_token: 登录成功后拿到的 bot token
        baseurl:   登录成功后返回的接口域名（getupdates/sendmessage 走这里）
        """
        self.bot_token = bot_token
        self.baseurl = baseurl or BASE_URL

    def _headers(self) -> dict:
        """鉴权接口（getupdates / sendmessage）通用请求头。

        X-WECHAT-UIN: base64(随机 uint32)，协议要求每次带一个。
        """
        return {
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json",
            "X-WECHAT-UIN": base64.b64encode(os.urandom(4)).decode(),
        }

    # ---------- 登录相关（无需 token） ----------

    def get_qrcode(self) -> dict:
        """申请登录二维码。返回 {qrcode, qrcode_img_content, ret}。

        qrcode            : 32 位 token，用来轮询扫码状态（get_qrcode_status）
        qrcode_img_content: 二维码内容链接（手机扫描的就是它），形如
                            https://liteapp.weixin.qq.com/q/xxx?qrcode=...
        ret               : 0 表示请求成功
        """
        url = f"{BASE_URL}/ilink/bot/get_bot_qrcode?bot_type=3"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_qrcode_status(self, qrcode: str) -> dict:
        """查询扫码状态（长轮询）。状态流转：status: wait -> scaned -> confirmed。

        ⚠️ 这是个长轮询接口：对一张新鲜未扫的码，服务端会 **hold 住连接 ~30s**
        等扫码事件，超时才返回 status:wait；扫码/确认后会立即返回。
        所以调用方超时必须 ≥35s，否则每次都在服务端响应前主动断开（之前
        用 10s 超时导致一直"超时"的根因）。qrcode 走查询参数，放 JSON body
        服务端读不到。

        confirmed 时返回 {status:"confirmed", bot_token, baseurl}。
        """
        url = f"{BASE_URL}/ilink/bot/get_qrcode_status"
        r = requests.get(
            url,
            params={"qrcode": qrcode},
            headers={"Content-Type": "application/json"},
            timeout=40,
        )
        r.raise_for_status()
        return r.json()

    # ---------- 消息收发（需要 token） ----------

    def get_updates(self, get_updates_buf: str = "") -> dict:
        """长轮询拉取新消息。阻塞最多 ~35s，有消息立即返回。

        返回 {msgs: [...], get_updates_buf}。
        每条 msg 关键字段：
            context_token : 回复时必须带回
            from_user_id  : 发送者
            item_list[0].text_item.text : 文本内容
        """
        url = f"{self.baseurl}/ilink/bot/getupdates"
        payload = {
            "get_updates_buf": get_updates_buf,
            "base_info": {"channel_version": "1.0.2"},
        }
        r = requests.post(url, json=payload, headers=self._headers(), timeout=40)
        r.raise_for_status()
        return r.json()

    def send_message(self, to_user_id: str, context_token: str, text: str) -> dict:
        """给指定用户/会话发送文本消息。

        to_user_id    : 接收者（入站消息的 from_user_id）
        context_token : 必须来自对方那条消息，原样带回
        text          : 回复正文

        ⚠️ client_id 必须每条消息全局唯一（这里用 UUID4）。
        服务端按 client_id 做幂等/去重——如果多条消息共用同一个 client_id
        （或都不带、等于同一个空值），微信客户端**只会渲染第一条**，后续
        全部不显示成新气泡。这就是之前"只收到第一条回复、之后一条都收不到"
        的根因。message_state 用 2（FINISH，完成态）；GENERATING(1) 在普通
        bot 会话里不可靠，不要用。
        """
        url = f"{self.baseurl}/ilink/bot/sendmessage"
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": uuid.uuid4().hex,
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            }
        }
        r = requests.post(url, json=payload, headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()
