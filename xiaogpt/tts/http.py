"""播给音箱听的本地 HTTP 服务所用的请求处理器。

这里的 Range 支持不是优化，是必需品。音箱用 ffmpeg（请求头里的
`User-Agent: Lavf/...`）取音频，一次播放会发三个请求：

    1. Range: bytes=0-            探测
    2. Range: bytes=<接近文件尾>-   跳到末尾读 MP3 的标签帧
    3. Range: bytes=0-            真正开始播

而 stdlib 的 `SimpleHTTPRequestHandler` **完全不支持 Range**——源码里连
`Range` / `206` / `Content-Range` 这几个字符串都没有，对上面三类请求一律回
`200` + 整个文件。ffmpeg 因此完成不了探测，会以每秒十几次的频率疯狂重试，
而音箱始终不进入播放态。用户听到的是**一片寂静**，日志里却只看到一堆正常的
200 响应，极具迷惑性。

2026-09-18 在 L05C 上实测：补上 206 之后，播放态立刻从 2 变 1，声音正常。
这个坑与型号无关，任何走第三方 TTS 的设备都会踩到。
"""

from __future__ import annotations

import os
import re
from http.server import SimpleHTTPRequestHandler
from typing import Callable

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class _LimitedReader:
    """把读取限制在 [start, end] 区间内，配合 copyfile 使用。"""

    def __init__(self, fp, remaining: int) -> None:
        self.fp = fp
        self.remaining = remaining

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size is None or size < 0 or size > self.remaining:
            size = self.remaining
        data = self.fp.read(size)
        self.remaining -= len(data)
        return data

    def close(self) -> None:
        self.fp.close()


class RangeRequestHandler(SimpleHTTPRequestHandler):
    """支持 Range(206) 的静态文件处理器。

    可选传入 on_fetch 回调，每次收到取流请求时用文件名调用一次。
    音箱开始取流就等于它开始播放，调用方可以据此判断播放时机，
    不必去猜一个固定的缓冲延迟。
    """

    def __init__(
        self, *args, on_fetch: Callable[[str], None] | None = None, **kwargs
    ) -> None:
        self._on_fetch = on_fetch
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self._on_fetch is not None:
            self._on_fetch(self.path.rsplit("/", 1)[-1])
        super().do_GET()

    def send_head(self):
        rng = self.headers.get("Range")
        if not rng:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        size = os.fstat(f.fileno()).st_size
        match = _RANGE_RE.match(rng)
        if not match:
            f.close()
            return super().send_head()
        start = int(match.group(1)) if match.group(1) else 0
        end = int(match.group(2)) if match.group(2) else size - 1
        end = min(end, size - 1)

        if start >= size or start > end:
            f.close()
            self.send_response(416)  # Range Not Satisfiable
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None

        self.send_response(206)
        self.send_header("Content-type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        f.seek(start)
        return _LimitedReader(f, end - start + 1)
