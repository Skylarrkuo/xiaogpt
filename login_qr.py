"""小米账号扫码登录，把 token 写入 ~/.mi.token 供 miservice / xiaogpt 使用。

用米家 App 扫一次码即可，之后 xiaogpt 会用 passToken 自动换取新的 serviceToken，
不需要再手工填 cookie，也不会因为 serviceToken 过期而失效。

用法:
    python login_qr.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import string
import sys
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin

import yaml
from aiohttp import ClientSession, ClientTimeout

BASE = "https://account.xiaomi.com"
USER_AGENT = "APP/com.xiaomi.mihome APPV/6.0.103 iosPassportSDK/3.9.0 iOS/14.4 miHSTS"
SID = "micoapi"
TOKEN_PATH = Path.home() / ".mi.token"
CONFIG_PATH = Path(__file__).with_name("config.yaml")


def parse_jsonp(text: str) -> dict:
    """小米的接口会带 &&&START&&& 前缀，或者用 JSONP 包一层。"""
    t = text.strip()
    if t.startswith("&&&START&&&"):
        t = t[len("&&&START&&&") :]
    m = re.match(r"^[A-Za-z_$][\w$]*\((.*)\)[;\s]*$", t, re.S)
    return json.loads(m.group(1) if m else t)


def new_device_id(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length)).upper()


def show_qr(url: str) -> None:
    try:
        import qrcode
    except ImportError:
        # 没装 qrcode 也能用：在浏览器里打开这个链接扫码
        print("=" * 60)
        print("请在浏览器打开下面的链接，然后用米家 App 扫屏幕上的二维码：")
        print()
        print(url)
        print("=" * 60)
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def summarize(payload: dict) -> str:
    """只报告字段是否存在，不打印任何凭据内容。"""
    return json.dumps(
        {k: ("<有值>" if v else "<空>") for k, v in payload.items()}, ensure_ascii=False
    )


async def get_service_token(session: ClientSession, result: dict) -> str | None:
    """用 location + clientSign 换取 serviceToken。"""
    location = urljoin(BASE, result["location"])
    ssecurity, nonce = result.get("ssecurity"), result.get("nonce")
    if ssecurity and nonce:
        nsec = f"nonce={nonce}&{ssecurity}"
        client_sign = base64.b64encode(hashlib.sha1(nsec.encode()).digest()).decode()
        url = f"{location}&clientSign={quote(client_sign)}"
    else:
        url = location
    async with session.get(url) as r:
        await r.text()
    for cookie in session.cookie_jar:
        if cookie.key == "serviceToken":
            return cookie.value
    return None


async def qr_login() -> dict:
    async with ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        params = {
            "_qrsize": "240",
            "sid": SID,
            "_json": "true",
            "callback": "callback",
            "serviceParam": '{"checkSafeAddress":false,"lsrp_score":0.0}',
        }
        async with session.get(f"{BASE}/longPolling/loginUrl?{urlencode(params)}") as r:
            start = parse_jsonp(await r.text())
        if not start.get("qr"):
            raise SystemExit(f"获取二维码失败: {summarize(start)}")

        qr_url = urljoin(BASE, start["qr"])
        lp_url = urljoin(BASE, start["lp"])
        timeout = int(start.get("timeout", 300))

        print(f"\n请用米家 App 扫描二维码（约 {timeout} 秒内有效）：\n")
        show_qr(qr_url)
        print("\n等待扫码...")

        async with session.get(lp_url, timeout=ClientTimeout(total=timeout + 30)) as r:
            result = parse_jsonp(await r.text())

        if not all(result.get(k) for k in ("userId", "passToken", "location")):
            raise SystemExit(
                f"扫码未完成或返回异常（可能超时/取消）: {summarize(result)}"
            )

        service_token = await get_service_token(session, result)
        if not service_token:
            raise SystemExit("没有拿到 serviceToken，请重试")

        return {
            "deviceId": new_device_id(),
            "userId": str(result["userId"]),
            "passToken": result["passToken"],
            SID: [result.get("ssecurity", ""), service_token],
        }


async def verify(token: dict) -> bool:
    """用对话接口确认 token 真的可用——这正是之前 401 的那个接口。"""
    try:
        hardware = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))["hardware"]
    except Exception:
        print("(跳过验证：读不到 config.yaml 里的 hardware)")
        return True
    import time

    from xiaogpt.config import LATEST_ASK_API

    url = LATEST_ASK_API.format(
        hardware=hardware, timestamp=str(int(time.time() * 1000))
    )
    cookies = {
        "deviceId": token["deviceId"],
        "serviceToken": token[SID][1],
        "userId": token["userId"],
    }
    async with ClientSession(cookies=cookies) as session:
        async with session.get(url, timeout=ClientTimeout(total=15)) as r:
            print(f"验证请求返回 HTTP {r.status}")
            return r.status == 200


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    token = asyncio.run(qr_login())

    TOKEN_PATH.write_text(json.dumps(token, indent=2), encoding="utf-8")
    try:
        TOKEN_PATH.chmod(0o600)
    except Exception:
        pass
    print(f"\n[OK] token 已写入 {TOKEN_PATH}")
    print(f"     userId={token['userId']}")

    if asyncio.run(verify(token)):
        print("\n[OK] 验证通过，可以运行 .\\one_click.ps1 了")
        print("     记得把 config.yaml 里的 cookie 那一行删掉或留空。")
    else:
        print("\n[!] 验证未通过，token 可能无效，请重试")


if __name__ == "__main__":
    main()
