"""扫码登录流程。

流程：
    1. 向 iLink 申请二维码
    2. 在终端打印二维码（用手机微信扫码）
    3. 轮询扫码状态，直到 confirmed
    4. 把 bot_token + baseurl 存进 token.json，下次直接复用
"""

import json
import os
import time

import qrcode

from ilink import BASE_URL, ILinkClient

_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(_DIR, "token.json")
QR_PNG = os.path.join(_DIR, "qr.png")    # 干净的 PNG 二维码，手机好扫
QR_URL = os.path.join(_DIR, "qr_url.txt")  # 原始扫码链接（浏览器打开也能扫）
QR_TTL_SEC = 240  # 二维码有效期（秒），超过还没确认就自动换新的一张


def save_token(bot_token: str, baseurl: str) -> None:
    data = {"token": bot_token, "baseurl": baseurl}
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(TOKEN_FILE, 0o600)  # token 是敏感凭据，收紧权限


def load_token() -> dict | None:
    """读取本地 token。没有或为空返回 None。"""
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not data.get("token"):
        return None
    return data


def _print_qrcode(qr_content: str) -> None:
    """在终端用 ASCII 字符渲染二维码。

    qr_content 必须是手机能扫的“二维码内容”——也就是接口返回的
    qrcode_img_content（一个 https://liteapp.weixin.qq.com/... 链接），
    而不是 qrcode（那个是 32 位 token，只用来轮询状态）。
    """
    qr = qrcode.QRCode(border=1)
    qr.add_data(qr_content)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def _save_qr_files(qr_content: str) -> None:
    """把二维码导出成 PNG 和纯文本链接，方便手机扫描。

    终端 ASCII 二维码经常扫不出（字符间距问题），PNG 和浏览器链接更稳。
    """
    # PNG 图片
    img = qrcode.make(qr_content)
    img.save(QR_PNG)
    # 原始链接（浏览器打开会渲染成可扫的二维码页面）
    with open(QR_URL, "w", encoding="utf-8") as f:
        f.write(qr_content + "\n")


def login() -> tuple[str, str]:
    """执行扫码登录，返回 (bot_token, baseurl) 并写入 token.json。

    全程容错：申请二维码失败 / 轮询网络抖动 / 二维码过期，都不会让进程退出，
    而是退避后重试或重新申请一张二维码。
    """
    while True:  # 外层：拿到登录成功才退出，否则一直重试
        client = ILinkClient()

        print("正在申请登录二维码 ...")
        try:
            res = client.get_qrcode()
        except Exception as e:
            print(f"⚠️ 申请二维码失败({e})，5 秒后重试 ...")
            time.sleep(5)
            continue

        # qrcode           : 32 位 token，用来轮询扫码状态
        # qrcode_img_content: 二维码内容链接，手机扫描的就是它
        qrcode_token = res.get("qrcode", "")
        qr_content = res.get("qrcode_img_content", "")
        if not qrcode_token or not qr_content:
            print(f"二维码返回异常: {res}，5 秒后重试 ...")
            time.sleep(5)
            continue

        print("请用微信扫描下面的二维码登录：\n")
        _print_qrcode(qr_content)
        _save_qr_files(qr_content)
        print(f"\n步骤：① 用微信扫 {QR_PNG}（或把 {QR_URL} 里的链接发给微信自己再点开）")
        print("      ② 微信会打开一个页面，找到「确认登录/授权」按钮并点击")
        print("      （该页面依赖微信内置浏览器，普通浏览器打不开）")

        # 轮询这一个二维码。返回 {ret, status}：ret:0=请求成功，status 是状态
        # （wait/scaned/confirmed）。最稳妥的"登录成功"判据是 bot_token 出现
        # 或 status==confirmed。注意 qrcode 必须走查询参数（见 ilink.py）。
        errs = 0
        deadline = time.monotonic() + QR_TTL_SEC
        while True:
            # 二维码有时效：超时还没确认就丢弃，让外层重新申请一张新鲜的
            if time.monotonic() > deadline:
                print("⏰ 当前二维码等候超时（可能已过期），换一张新的 ...")
                break
            try:
                status = client.get_qrcode_status(qrcode_token)
                bot_token = status.get("bot_token", "")
                if bot_token or status.get("status") == "confirmed":
                    baseurl = status.get("baseurl") or BASE_URL
                    save_token(bot_token, baseurl)
                    print(f"\n✅ 登录成功，token 已保存到 {TOKEN_FILE}")
                    return bot_token, baseurl
                errs = 0
                st = status.get("status", "wait")
                print(f"扫码状态：{st}（ret={status.get('ret')}），等待扫码 ...")
            except Exception as e:
                errs += 1
                print(f"⚠️ 轮询出错({e})，第 {errs} 次")
                if errs >= 10:
                    # 连续失败多半是二维码已过期或被限流，丢弃后让外层重新申请一张
                    print("连续失败，丢弃当前二维码，重新申请 ...")
                    break
            # 间隔 5s：get_qrcode_status 对高频调用会限流（亲测 2s 间隔会被挡），
            # 确认状态会保持，5s 足以及时捕获扫码
            time.sleep(5)


if __name__ == "__main__":
    login()
