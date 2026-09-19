from __future__ import annotations

import abc
import asyncio
import json
import logging
from typing import TYPE_CHECKING, AsyncIterator

from miservice import MiIOService

from xiaogpt.device import STOP_DIRECTIVE, execute_directive

if TYPE_CHECKING:
    from typing import TypeVar

    from miservice import MiNAService

    from xiaogpt.config import Config

    T = TypeVar("T", bound="TTS")

logger = logging.getLogger(__name__)


class TTS(abc.ABC):
    """An abstract base class for Text-to-Speech models."""

    def __init__(
        self, mina_service: MiNAService, device_id: str, config: Config
    ) -> None:
        self.mina_service = mina_service
        self.device_id = device_id
        self.config = config
        # MiTTS.say() 与 stop_playback() 都要用，在这里建一次。
        self.miio_service = MiIOService(mina_service.account)

    async def wait_for_duration(self, duration: float) -> None:
        """Wait for the specified duration."""
        await asyncio.sleep(duration)
        if not self.config.device_profile.status_poll:
            # 该型号播完后 status 永远停在 1，下面的轮询会死循环挂死，
            # 表现是音箱说完第一句就再无反应。只能按预估时长等待，真正的
            # 停止交给 stop_playback()。
            return
        while True:
            if not await self.get_if_xiaoai_is_playing():
                break
            await asyncio.sleep(1)

    async def stop_playback(self) -> bool:
        """显式停掉音箱正在播放的音频。

        返回 False 表示该型号不支持指令停止，调用方按上游方式处理。

        上游对 URL 播放不做任何停止（依赖音箱自己播完），这在播放态不可信的
        型号上表现为最后一句无限重播。
        """
        return await execute_directive(
            self.miio_service,
            self.config.mi_did,
            self.config.device_profile,
            STOP_DIRECTIVE,
        )

    async def get_if_xiaoai_is_playing(self) -> bool:
        playing_info = await self.mina_service.player_get_status(self.device_id)
        # WTF xiaomi api
        is_playing = (
            json.loads(playing_info.get("data", {}).get("info", "{}")).get("status", -1)
            == 1
        )
        return is_playing

    def set_instruction(self, instruction: str) -> bool:
        """设置语音指令（情绪 / 方言 / 语气），返回是否被这个 TTS 接受。

        只有豆包 TTS 的部分音色支持（API 里的 context_texts）；小爱原生 TTS
        与 edge 都没有这个概念，返回 False 让调用方能明确告诉用户"没生效"，
        而不是答应了却什么都不变。
        """
        return False

    def set_style(self, style: dict) -> bool:
        """按场景覆盖这一句的说话方式（语速 / 音量 / 方言 / 语音指令）。

        只有豆包 TTS 实现了它（见 tts/doubao.py 的 STYLE_FIELDS）；其余 TTS
        返回 False，调用方就按配置里的默认值走。
        """
        return False

    def reset_style(self) -> None:
        """清掉上一句的场景覆盖，回到配置里的默认说话方式。"""

    @abc.abstractmethod
    async def synthesize(self, lang: str, text_stream: AsyncIterator[str]) -> None:
        """Synthesize speech from a stream of text."""
        raise NotImplementedError
