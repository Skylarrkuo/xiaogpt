#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import socket
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, AsyncIterator
from urllib.parse import urlparse

from requests.utils import cookiejar_from_dict

if TYPE_CHECKING:
    from lingua import LanguageDetector


### HELP FUNCTION ###
def parse_cookie_string(cookie_string):
    cookie = SimpleCookie()
    cookie.load(cookie_string)
    cookies_dict = {k: m.value for k, m in cookie.items()}
    return cookiejar_from_dict(cookies_dict, cookiejar=None, overwrite=True)


_no_elapse_chars = re.compile(r"([「」『』《》“”'\"()（）]|(?<!-)-(?!-))", re.UNICODE)


def calculate_tts_elapse(text: str) -> float:
    # for simplicity, we use a fixed speed
    speed = 4.5  # this value is picked by trial and error
    # Exclude quotes and brackets that do not affect the total elapsed time
    return len(_no_elapse_chars.sub("", text)) / speed


_ending_punctuations = ("。", "？", "！", "；", "\n", "?", "!", ";")


async def split_sentences(text_stream: AsyncIterator[str]) -> AsyncIterator[str]:
    cur = ""
    async for text in text_stream:
        cur += text
        if cur.endswith(_ending_punctuations):
            yield cur
            cur = ""
    if cur:
        yield cur


def validate_proxy(proxy_str: str) -> bool:
    """Do a simple validation of the http proxy string."""

    parsed = urlparse(proxy_str)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Proxy scheme must be http or https")
    if not (parsed.hostname and parsed.port):
        raise ValueError("Proxy hostname and port must be set")

    return True


#: 这些地址段不可能是音箱能访问到的本机地址：
#: - 198.18.0.0/15 是 RFC2544 的保留段，Clash / Mihomo 这类 TUN 代理拿它当
#:   fake-ip；连着代理时"连 8.8.8.8 看本机地址"问到的就是这个虚拟地址
#: - 169.254.0.0/16 是没拿到 DHCP 时的自分配地址
#: - 127./0. 是回环与未指定
_UNUSABLE_PREFIXES = ("198.18.", "198.19.", "169.254.", "127.", "0.")


def _candidate_ips() -> list[str]:
    """本机所有可用的 IPv4 地址（滤掉虚拟/回环地址，去重保序）。"""
    found: list[str] = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith(_UNUSABLE_PREFIXES):
                found.append(ip)
    except OSError:
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if not ip.startswith(_UNUSABLE_PREFIXES):
                found.append(ip)
        except OSError:
            pass
    return list(dict.fromkeys(found))


def _rank(ip: str) -> int:
    """越小越优先：家庭网几乎都是 192.168.x.x，其次 10.x，
    172.16~31.x 排最后（Docker / Hyper-V 也爱用这段，音箱通常到不了）。"""
    if ip.startswith("192.168."):
        return 0
    if ip.startswith("10."):
        return 1
    if ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31:
        return 2
    return 3


def get_hostname() -> str:
    """本机在局域网里的地址，音箱靠它来拉音频。

    不能直接用"连 8.8.8.8 看本机地址"这一招：开了 Clash / Mihomo 这类 TUN
    代理后，去 8.8.8.8 的流量会走进虚拟网卡，拿到的是 198.18.0.1 这种
    fake-ip——音箱根本连不上。表现是音频明明合成好了，音箱却一声不吭，
    日志里只有一句"等 xxx.mp3 的取流超时"，很难查。
    """
    if "XIAOGPT_HOSTNAME" in os.environ:
        return os.environ["XIAOGPT_HOSTNAME"]

    candidates = _candidate_ips()
    if not candidates:
        # 候选全被滤掉（比如真的只有 VPN 地址）时退回上游那一招
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]

    best = min(candidates, key=_rank)
    if len(candidates) > 1:
        print(
            f"[tts] 本机有多个地址 {candidates}，选 {best} 给音箱取流；"
            "若音箱仍不出声，用 XIAOGPT_HOSTNAME 指定局域网地址"
        )
    return best


def _get_detector() -> LanguageDetector | None:
    try:
        from lingua import LanguageDetectorBuilder
    except ImportError:
        return None
    return LanguageDetectorBuilder.from_all_spoken_languages().build()


_detector = _get_detector()


def detect_language(text: str) -> str:
    if _detector is None:
        return "zh"  # default to Chinese if langdetect module is not available
    lang = _detector.detect_language_of(text)
    return lang.iso_code_639_1.name.lower() if lang is not None else "zh"
