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
import hashlib
import os
import uuid
from urllib.parse import quote

import requests

# 未登录时的接口前缀（取二维码 / 查状态走这里）
BASE_URL = "https://ilinkai.weixin.qq.com"
# 媒体文件 CDN（上传/下载都走这里）
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"


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

    # ---------- 媒体消息（文件/图片/视频） ----------
    # 三步（协议参考 openclaw-weixin 源码，文档没写全的部分都在那里）：
    #   ① getuploadurl：报 filekey/media_type/大小/MD5/aeskey(hex) 换 upload_param
    #   ② POST 密文到 CDN：URL = /upload?encrypted_query_param=<upload_param>&filekey=<filekey>，
    #      body = AES-128-ECB(PKCS7) 加密后的字节；响应 header x-encrypted-param = 下载参数
    #   ③ sendmessage：item 引用 CDN——media:{encrypt_query_param, aes_key(base64),
    #      encrypt_type:1}，按类型加 image_item/video_item/file_item
    # 注意两套枚举别混：getuploadurl 的 media_type（图1/视频2/文件3/语音4）≠
    # item_list 的 type（文本1/图2/语音3/文件4/视频5）。caption 单独发一条文本。

    _IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
    _VIDEO_EXTS = {"mp4", "mov", "m4v", "mkv"}

    @staticmethod
    def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
        """AES-128-ECB + PKCS7 填充（CDN 要求的加密方式）。"""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        padder = PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return enc.update(padded) + enc.finalize()

    def upload_media(self, file_path: str, to_user_id: str, media_type: int) -> dict:
        """上传文件到微信 CDN。返回 {download_param, aes_key_b64, raw_size,
        cipher_size, file_name}；失败抛 ILinkError。"""
        with open(file_path, "rb") as f:
            raw = f.read()
        key = os.urandom(16)
        filekey = os.urandom(16).hex()
        # 密文大小：明文 + 1~16 字节 PKCS7 填充，对齐 16
        cipher_size = ((len(raw) + 16) // 16) * 16
        r = requests.post(
            f"{self.baseurl}/ilink/bot/getuploadurl",
            json={
                "filekey": filekey,
                "media_type": media_type,          # 1图 2视频 3文件 4语音
                "to_user_id": to_user_id,
                "rawsize": len(raw),
                "rawfilemd5": hashlib.md5(raw).hexdigest(),
                "filesize": cipher_size,
                "no_need_thumb": True,
                "aeskey": key.hex(),
                "base_info": {"channel_version": "1.0.2"},
            },
            headers=self._headers(), timeout=15,
        )
        r.raise_for_status()
        upload_param = (r.json() or {}).get("upload_param")
        if not upload_param:
            raise ILinkError(f"getuploadurl 未返回 upload_param：{r.text[:200]}")
        url = (f"{CDN_BASE}/upload?encrypted_query_param={quote(upload_param, safe='')}"
               f"&filekey={quote(filekey, safe='')}")
        res = requests.post(
            url, data=self._aes_ecb_encrypt(raw, key),
            headers={"Content-Type": "application/octet-stream"}, timeout=120,
        )
        if res.status_code >= 400:
            raise ILinkError(f"CDN 上传失败 {res.status_code}："
                             f"{res.headers.get('x-error-message', res.text[:200])}")
        download_param = res.headers.get("x-encrypted-param")
        if not download_param:
            raise ILinkError(f"CDN 响应缺 x-encrypted-param：{res.text[:200]}")
        # ⚠️ aes_key 编码（对齐 openclaw-weixin 参考实现，别改回 base64(原始16字节)）：
        # 服务端在 getuploadurl 登记的是 hex 字符串，客户端按 base64 解出后当 hex 串
        # 再解一次才是真 key。即 base64(key.hex() 的 UTF-8 字节)，44 字符——
        # 直接 base64(原始 key) 会被服务端收下(message_id)但客户端解不开图。
        return {"download_param": download_param,
                "aes_key_b64": base64.b64encode(key.hex().encode()).decode(),
                "raw_size": len(raw), "cipher_size": cipher_size,
                "file_name": os.path.basename(file_path)}

    def send_media(self, to_user_id: str, context_token: str,
                   file_path: str, caption: str = "") -> dict:
        """发送文件/图片/视频（按扩展名自动路由）。caption 有值时先发一条文本。"""
        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        media = None
        if ext in self._IMAGE_EXTS:
            up = self.upload_media(file_path, to_user_id, media_type=1)
            media = {"type": 2, "image_item": {
                "media": self._cdn_ref(up), "mid_size": up["cipher_size"]}}
        elif ext in self._VIDEO_EXTS:
            up = self.upload_media(file_path, to_user_id, media_type=2)
            media = {"type": 5, "video_item": {
                "media": self._cdn_ref(up), "video_size": up["cipher_size"]}}
        else:
            up = self.upload_media(file_path, to_user_id, media_type=3)
            media = {"type": 4, "file_item": {
                "media": self._cdn_ref(up),
                "file_name": up["file_name"], "len": str(up["raw_size"])}}
        if caption:
            self.send_message(to_user_id, context_token, caption)
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": uuid.uuid4().hex,   # 每条唯一，同 send_message
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [media],
            }
        }
        r = requests.post(f"{self.baseurl}/ilink/bot/sendmessage",
                          json=payload, headers=self._headers(), timeout=15)
        r.raise_for_status()
        resp = r.json()
        # 成功时只回 message_id；失败才有 ret（如 token 过期 ret=-2）
        if resp.get("ret", 0) != 0:
            raise ILinkError(f"sendmessage 失败：{resp}")
        return resp

    @staticmethod
    def _cdn_ref(up: dict) -> dict:
        return {"encrypt_query_param": up["download_param"],
                "aes_key": up["aes_key_b64"], "encrypt_type": 1}
