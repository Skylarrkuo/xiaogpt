import asyncio
import contextlib
import functools
import os
import random
import socket
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import AsyncIterator

from miservice import MiNAService

from xiaogpt.config import Config
from xiaogpt.device import (
    BURST_QUIET_SECONDS,
    PLAYBACK_START_TIMEOUT,
    REPEAT_POLL_SECONDS,
    REPEAT_WATCH_SECONDS,
    STOP_EARLY_SECONDS,
)
from xiaogpt.tts.base import TTS, logger
from xiaogpt.tts.http import RangeRequestHandler
from xiaogpt.utils import get_hostname

_SYNTHESIS_DONE = object()
DOUBAO_STOP_EARLY_SECONDS = 2.5


class _SynthesisFailure:
    def __init__(self, error: Exception) -> None:
        self.error = error


class HTTPRequestHandler(RangeRequestHandler):
    def log_message(self, format, *args):
        logger.debug(f"{self.address_string()} - {format}", *args)

    def log_error(self, format, *args):
        logger.error(f"{self.address_string()} - {format}", *args)

    def copyfile(self, source, outputfile):
        try:
            super().copyfile(source, outputfile)
        except (socket.error, ConnectionResetError, BrokenPipeError):
            # ignore this or TODO find out why the error later
            pass


def make_speaker(config: Config):
    """按 config.tts 造一个「一句话 → 一个音频文件」的 speaker。

    edge 与 doubao 都走文件模式，区别只在谁合成：edge 用 tetos 的薄封装
    （音色 / 语速 / 音调 / 音量都可以在 tts_options 里配），doubao 用
    `tts/doubao.py` 里自己实现的火山 WebSocket 协议。
    """
    if config.tts == "doubao":
        from xiaogpt.tts.doubao import DoubaoSpeaker

        return DoubaoSpeaker.from_config(config)

    from tetos import get_speaker

    speaker_cls = get_speaker(config.tts)
    try:
        return speaker_cls(**config.tts_options)
    except TypeError as e:
        raise ValueError(f"{e}. Please add them via `tts_options` config") from e


class FileTTS(TTS):
    """先本地合成音频文件，再让音箱来拉的 TTS（edge / doubao 共用）。

    L05C 的播放态不可信，靠的是「等音频时长 + 显式停止」这套逻辑，见下面
    _wait_for_duration_from_playback 的注释。流式边播边合成的路子（上游给 fish
    用的那种）在这个型号上没有验证过，所以不在这里。
    """

    def __init__(
        self, mina_service: MiNAService, device_id: str, config: Config
    ) -> None:
        super().__init__(mina_service, device_id, config)
        self.dirname = tempfile.TemporaryDirectory(prefix="xiaogpt-tts-")
        # 音箱每次来取流都记一笔，用来判断"它真的开始播了"。由 HTTP 服务线程
        # 写入、事件循环线程读取，append 在 GIL 下是安全的。
        self.fetches: list[tuple[str, float]] = []
        #: 最近一次交给音箱播放的文件名，收尾时用来盯"它是不是开始重播了"
        self.last_played: str | None = None
        self._start_http_server()

        assert config.tts and config.tts != "mi"
        self.speaker = make_speaker(config)

    def set_instruction(self, instruction: str) -> bool:
        """把语音指令（情绪 / 方言 / 语气）转给 speaker，它不认识就返回 False。"""
        setter = getattr(self.speaker, "set_instruction", None)
        if setter is None:
            return False
        setter(instruction)
        return True

    def set_style(self, style: dict) -> bool:
        setter = getattr(self.speaker, "set_style", None)
        if setter is None:
            return False
        setter(style)
        return True

    def reset_style(self) -> None:
        resetter = getattr(self.speaker, "reset_style", None)
        if resetter is not None:
            resetter()

    async def make_audio_file(self, lang: str, text: str) -> tuple[Path, float]:
        with tempfile.NamedTemporaryFile(
            suffix=".mp3", mode="wb", delete=False, dir=self.dirname.name
        ) as output_file:
            path = Path(output_file.name)
        try:
            duration = await self.speaker.synthesize(text, path, lang=lang)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path, duration

    async def synthesize(self, lang: str, text_stream: AsyncIterator[str]) -> None:
        # 两个待播文件足够覆盖网络与播放的流水线，同时给合成端施加背压，避免
        # 长回答一次生成几十个文件。错误与结束也走同一条队列，消费者不会再
        # 因 worker 提前退出而永久轮询空队列。
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        created_files: list[Path] = []
        self.last_played = None

        async def worker():
            try:
                async for text in text_stream:
                    path, duration = await self.make_audio_file(lang, text)
                    created_files.append(path)
                    url = f"http://{self.hostname}:{self.port}/{path.name}"
                    await queue.put((url, duration, path.name))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # 把原始异常交给消费者原样抛出
                await queue.put(_SynthesisFailure(error))
            else:
                await queue.put(_SYNTHESIS_DONE)

        task = asyncio.create_task(worker())
        primary_error: BaseException | None = None
        try:
            while True:
                item = await queue.get()
                if item is _SYNTHESIS_DONE:
                    break
                if isinstance(item, _SynthesisFailure):
                    raise item.error
                url, duration, filename = item
                logger.debug("Playing URL %s (%s seconds)", url, duration)
                if self.config.device_profile.status_poll:
                    await asyncio.gather(
                        self.mina_service.play_by_url(self.device_id, url, _type=1),
                        self.wait_for_duration(duration),
                    )
                else:
                    # 播放态不可信的型号：播完不会回落，只能按音频时长等待，
                    # 并靠显式停止收尾。
                    self.fetches.clear()
                    self.last_played = filename
                    await self.mina_service.play_by_url(self.device_id, url, _type=1)
                    await self._wait_for_duration_from_playback(filename, duration)
            await task
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            try:
                # 音箱把 URL 当音乐播，正常结束、模型报错或 Ctrl-C 都要收尾。
                await self._stop_after_playback()
            except Exception:
                if primary_error is None:
                    raise
                logger.exception("TTS 异常后停止音箱也失败")
            finally:
                self.last_played = None
                for path in created_files:
                    with contextlib.suppress(OSError):
                        path.unlink()

    async def _wait_for_duration_from_playback(
        self, filename: str, duration: float
    ) -> None:
        """从"音箱真正开始播"算起，等满音频时长。

        起点取第一波取流的**最后一个**请求，而不是 play_by_url 返回的时刻：
        后者到出声之间有几秒缓冲，直接按时长等会在还没出声时就发停止指令
        （实测停止指令会落空）。
        """
        started = await self._wait_for_playback_start(filename)
        if started is None:
            # 音箱可能只是在排队，稍后才开始取流。若从超时时刻再睡一个完整
            # duration，下一段会在上一段尚未播完时覆盖它，造成确定性漏播。
            raise TimeoutError(f"音箱未开始取流: {filename}")
        # 只在后端明确提供的句尾静音内提前。edge 已实测约 0.5 秒；豆包则
        # 根据 silence_duration 计算，用户设为 0 时绝不切正文。
        deadline = started + duration - self._stop_early_seconds()
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _wait_for_playback_start(self, filename: str) -> float | None:
        """等第一波取流结束，返回真正的播放起点（time.monotonic 时刻）。

        超时返回 None，由调用方终止本轮；继续排下一段会覆盖迟到的上一段。
        """
        deadline = time.monotonic() + PLAYBACK_START_TIMEOUT
        last: float | None = None
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            stamps = [t for name, t in self.fetches if name == filename]
            if stamps and stamps[-1] != last:
                last = stamps[-1]
                last_change = time.monotonic()
            elif last is not None and time.monotonic() - last_change >= (
                BURST_QUIET_SECONDS
            ):
                return last
            await asyncio.sleep(0.05)
        if last is not None:
            return last
        # 音箱没来取流 = 它没听到任何东西。这里把"它被要求访问的地址"打出来，
        # 因为最常见的原因就是这个地址它够不着（虚拟网卡 / 网段不同 / 防火墙）。
        logger.warning(
            "音箱 %.0f 秒内没有来取流（%s://%s:%d/%s），终止本轮播放。"
            "请确认电脑与音箱同网段、防火墙放行 8050-8089、"
            "必要时用 XIAOGPT_HOSTNAME 指定局域网地址",
            PLAYBACK_START_TIMEOUT,
            "http",
            self.hostname,
            self.port,
            filename,
        )
        return None

    async def _stop_after_playback(self) -> None:
        """收尾时显式停止。没有指令通道的型号直接返回，行为与上游一致。

        时机由 _wait_for_duration_from_playback 在等待阶段就掐好了，这里不再
        额外延时——晚一步设备就已经开始重播下一轮了。
        """
        if self.config.device_profile.directive_command is None:
            return
        # 第二道保险：停止指令要绕云端一圈，慢半拍设备就已经把文件从头重播了
        # （用户听到的就是"最后一句的开头又冒出来一下"）。设备重播必然**重新来
        # 取流**，所以盯着取流列表比再猜一个延时靠谱：一出现就立刻再停一次。
        filename = self.last_played
        if not filename:
            return
        await self.stop_playback()
        seen = sum(1 for name, _ in self.fetches if name == filename)
        deadline = time.monotonic() + REPEAT_WATCH_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(REPEAT_POLL_SECONDS)
            count = sum(1 for name, _ in self.fetches if name == filename)
            if count > seen:
                logger.debug("设备开始重播 %s，补一次停止", filename)
                await self.stop_playback()
                return

    def _stop_early_seconds(self) -> float:
        if self.config.tts != "doubao":
            return STOP_EARLY_SECONDS
        trailing_ms = getattr(self.speaker, "silence_duration", 0) or 0
        try:
            trailing_seconds = max(0.0, float(trailing_ms) / 1000)
        except (TypeError, ValueError):
            return 0.0
        # L05C 真机测得停止指令约 2 秒才落到设备。豆包由 silence_duration
        # 明确生成尾静音，因此可以在这段静音内更早发送，不会切掉正文。
        return min(DOUBAO_STOP_EARLY_SECONDS, trailing_seconds)

    def _note_fetch(self, filename: str) -> None:
        self.fetches.append((filename, time.monotonic()))
        # 只留最近的一小段，长时间运行不会无限涨
        del self.fetches[:-64]

    def _start_http_server(self):
        # set the port range
        port_range = range(8050, 8090)
        # get a random port from the range
        self.port = int(os.getenv("XIAOGPT_PORT", random.choice(port_range)))
        # create the server
        handler = functools.partial(
            HTTPRequestHandler, directory=self.dirname.name, on_fetch=self._note_fetch
        )
        httpd = ThreadingHTTPServer(("", self.port), handler)
        # start the server in a new thread
        server_thread = threading.Thread(target=httpd.serve_forever)
        server_thread.daemon = True
        server_thread.start()

        self.hostname = get_hostname()
        logger.info(f"Serving on {self.hostname}:{self.port}")
