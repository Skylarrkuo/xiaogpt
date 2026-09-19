"""豆包语音合成大模型（火山引擎）—— 单向流式 WebSocket。

协议照着官方《单向流式语音合成WebSocket》文档与配套示例
（`websocket unidirectional.zip` 里的 `protocols.py`）实现：
4 字节二进制头 + JSON 载荷，音频以二进制帧流式返回。

官方这个接口没有 SDK 封装，只有示例代码；协议本身一百来行，自己写还能把
报错翻成人话（示例里是直接抛原始异常）。

**只在文件模式用**：本 fork 里它和 edge 一样走 `FileTTS`——先落地 mp3，再让
音箱来拉。不接流式播放：L05C 的取流/停止逻辑是在文件模式下调通的（见
`tts/file.py` 与 CHANGELOG），拿一条没验证过的播放链路去换几百毫秒首字延迟
不划算。
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import logging
import struct
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from xiaogpt.utils import calculate_tts_elapse

if TYPE_CHECKING:
    from xiaogpt.config import Config

logger = logging.getLogger(__name__)

ENDPOINT = "wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream"
#: seed-tts-2.0 = 豆包语音合成大模型 2.0（音色列表里的 *_bigtts）；
#: 声音复刻用 seed-icl-2.0，但它不支持语音指令 context_texts
RESOURCE_ID = "seed-tts-2.0"

# ---- 协议常量，取值全部来自官方 protocols.py ----
VERSION_1 = 0b0001
HEADER_SIZE_4 = 0b0001
MSG_FULL_CLIENT_REQUEST = 0b0001
MSG_FULL_SERVER_RESPONSE = 0b1001
MSG_AUDIO_ONLY_SERVER = 0b1011
MSG_ERROR = 0b1111
FLAG_NO_SEQ = 0b0000
FLAG_POSITIVE_SEQ = 0b0001
FLAG_NEGATIVE_SEQ = 0b0011
FLAG_WITH_EVENT = 0b0100
SER_JSON = 0b0001
COMPRESSION_NONE = 0b0000
COMPRESSION_GZIP = 0b0001

EVENT_SESSION_FINISHED = 152
EVENT_USAGE_RESPONSE = 154
EVENT_TTS_SENTENCE_START = 350
EVENT_TTS_SENTENCE_END = 351
EVENT_TTS_SUBTITLE = 364

#: 官方 unmarshal 里写死的名单：这几个事件后面没有 sessionId
_NO_SESSION_EVENTS = frozenset({1, 2, 50, 51, 52})
#: 只有这三个事件后面跟着 connectId
_CONNECT_ID_EVENTS = frozenset({50, 51, 52})

#: 官方 explicit_dialect 的全部取值
DIALECTS = (
    "beijing",
    "dongbei",
    "henan",
    "shaanxi",
    "shanghai",
    "sichuan",
    "tianjin",
    "yue",
)

#: 可以逐句覆盖（按场景自动调整）的字段，以及取值范围
STYLE_FIELDS: dict[str, tuple[int, int] | tuple[str, ...] | None] = {
    "instruction": None,
    "dialect": DIALECTS,
    "speech_rate": (-50, 100),
    "loudness_rate": (-50, 100),
    "pitch": (-12, 12),
}

#: 让 AI 参谋「这句话该怎么念」的提示词。要求只回一行 JSON，解析失败就退回默认。
STYLE_PROMPT = """你是小爱音箱的配音导演。用户刚刚对音箱说了这句话：
「{query}」

请决定这句回答该怎么说，只输出一行 JSON，不要任何解释、不要代码块：
{{"instruction": "给语音合成的中文语气指令（20 字以内，如：用温柔轻声的语气说）",
 "speech_rate": 0, "loudness_rate": 0, "pitch": 0, "dialect": ""}}

取值规则：speech_rate / loudness_rate 为 -50~100 的整数（100 = 2 倍速 / 2 倍音量，
-50 = 0.5 倍），pitch 为 -12~12 的整数；dialect 只在用户明确用方言说话或要求用
方言回答时才填，可选 beijing/dongbei/henan/shaanxi/shanghai/sichuan/tianjin/yue，
否则留空字符串。参考：讲恐怖或悬疑的，语速调慢、音量略低；哄睡、安慰、道歉的，
语速慢、音量轻；兴奋、催促、讲笑话的，语速快、音量正常；严肃、正式的回答，
语速略慢。拿不准就把三个数字都填 0、dialect 留空。"""


def parse_style(reply: str) -> dict | None:
    """从 AI 的回复里抠出风格方案。

    模型偶尔会加解释或代码块，所以取第一个花括号块而不是整段 json.loads；
    解析不出来就返回 None，调用方退回配置里的默认值——配音失败不能影响回答。
    """
    start = reply.find("{")
    end = reply.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(reply[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    style = {}
    for key in STYLE_FIELDS:
        value = data.get(key)
        if value in (None, ""):
            continue
        style[key] = value
    return style or None


def build_request(payload: dict[str, Any]) -> bytes:
    """把 JSON 载荷打包成一个 FULL_CLIENT_REQUEST 帧（单向流式只发这一帧）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = bytes(
        [
            (VERSION_1 << 4) | HEADER_SIZE_4,
            (MSG_FULL_CLIENT_REQUEST << 4) | FLAG_NO_SEQ,
            (SER_JSON << 4) | COMPRESSION_NONE,
            0,  # 头补齐到 4 字节
        ]
    )
    return header + struct.pack(">I", len(body)) + body


@dataclass
class ServerMessage:
    type: int = 0
    flag: int = 0
    event: int = 0
    session_id: str = ""
    error_code: int = 0
    payload: bytes = b""

    @property
    def audio(self) -> bytes:
        """这一帧带的音频数据（非音频帧返回空）。"""
        return self.payload if self.type == MSG_AUDIO_ONLY_SERVER else b""

    @property
    def text(self) -> str:
        return self.payload.decode("utf-8", "ignore")


def _parse_error_tail(data: bytes, pos: int) -> tuple[int, bytes]:
    """读错误帧的（错误码, 说明）。

    两个官方实现对错误帧的字段顺序不一致：Java 是 事件→错误码→载荷，而官方
    Python 的 marshal 会多写一个 sessionId（事件→sessionId→错误码→载荷）。
    这里用「载荷长度必须正好顶到帧尾」这条硬约束来分辨，两种都能读；都对不上
    就按 Java 顺序硬读——说明文案难看点没关系，别把错误码丢了。
    """
    for extra in (8, 4):
        length_pos = pos + extra
        if length_pos + 4 > len(data):
            continue
        size = struct.unpack_from(">I", data, length_pos)[0]
        if length_pos + 4 + size != len(data):
            continue
        code = struct.unpack_from(">I", data, length_pos - 4)[0]
        return code, data[length_pos + 4 :]
    if pos + 4 <= len(data):
        return struct.unpack_from(">I", data, pos)[0], data[pos + 4 :]
    return 0, b""


def parse_message(data: bytes) -> ServerMessage:
    """解析服务端的一帧。

    字段顺序照抄官方 `unmarshal`：先序号/错误码，再（有 WITH_EVENT 时）事件号、
    sessionId、connectId，最后是长度前缀 + 载荷。顺序错一位就会把音频当长度读，
    所以这里按官方实现逐步走，不做任何"优化"。
    """
    if len(data) < 4:
        raise ValueError(f"响应帧太短：{len(data)} 字节")
    header_size = (data[0] & 0x0F) * 4
    msg = ServerMessage(type=(data[1] >> 4) & 0x0F, flag=data[1] & 0x0F)
    compression = data[2] & 0x0F
    pos = max(header_size, 4)

    def read_u32() -> int:
        nonlocal pos
        if pos + 4 > len(data):
            # 帧被截断时不要抛 IndexError：宁可少读一个字段，也不要让一句
            # "读越界"盖掉真正的原因（对面到底发了什么，看日志里的 hex 更直接）
            logger.warning("响应帧长度不足，字段读取提前结束（%d 字节）", len(data))
            pos = len(data)
            return 0
        value = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        return value

    def read_bytes(size: int) -> bytes:
        nonlocal pos
        chunk = data[pos : pos + size]
        pos += size
        return chunk

    if msg.flag in (FLAG_POSITIVE_SEQ, FLAG_NEGATIVE_SEQ):
        read_u32()  # 序号：单向流式用不上
    if msg.flag == FLAG_WITH_EVENT:
        msg.event = read_u32()
        if msg.type != MSG_ERROR and msg.event not in _NO_SESSION_EVENTS:
            size = read_u32()
            if size:
                msg.session_id = read_bytes(size).decode("utf-8", "ignore")
        if msg.event in _CONNECT_ID_EVENTS:
            size = read_u32()
            if size:
                read_bytes(size)  # connectId，只在排错时要，先丢掉
    if msg.type == MSG_ERROR:
        msg.error_code, msg.payload = _parse_error_tail(data, pos)
        return msg
    if pos + 4 <= len(data):
        size = read_u32()
        msg.payload = read_bytes(size)
        if compression == COMPRESSION_GZIP and msg.payload:
            msg.payload = gzip.decompress(msg.payload)
    return msg


@dataclass
class DoubaoSpeaker:
    """文字 → mp3 文件 + 时长（接口与 tetos 的 Speaker 对齐，FileTTS 直接复用）。

    语音指令（情绪 / 方言 / 语气 / 语速）从哪进：

    - 情绪、语气、风格 → `instruction`，对应 API 的 `context_texts`，
      例如「你可以用特别特别痛心的语气说话吗?」。**只有豆包语音合成模型 2.0
      的音色支持**，声音复刻（seed-icl-2.0）不支持
    - 方言 → `dialect`，取值见 DIALECTS（beijing/dongbei/henan/shaanxi/
      shanghai/sichuan/tianjin/yue），需要音色本身支持方言
    - 语速 → `speech_rate`（-50 ~ 100，100 是 2 倍速），音量 → `loudness_rate`
    - 音调 → `pitch`（-12 ~ 12，post_process.pitch）
    - 语种 → `language`（zh-cn/en/ja/es-mx/...，见 README）

    没列到的参数用 `additions` / `post_process` 原样透传（发音词典
    pronunciation_dict 这类就写在那里）。
    """

    api_key: str
    speaker: str
    resource_id: str = RESOURCE_ID
    instruction: str | list[str] | None = None
    dialect: str | None = None
    language: str | None = None
    speech_rate: int | None = None
    loudness_rate: int | None = None
    #: 音量补偿。豆包出来的声音明显比小爱本嗓轻（实测 RMS -22 dBFS），
    #: 这里固定加一档；实测 +50 约 +3.6 dB、峰值仍有 -4 dBFS 余量，
    #: 再往上会被服务端压限（+100 的 RMS 反而不涨）。
    #: 0 = 用上游默认音量。它**不在 STYLE_FIELDS 里**：场景配音调的是
    #: "这句相对轻/响"，补偿是设备侧固定落差，不该被逐句覆盖掉。
    loudness_boost: int = 50
    pitch: int | None = None
    format: str = "mp3"
    sample_rate: int = 24000
    bit_rate: int | None = None
    # L05C 真机测得“停止播放”约 2 秒才落地。生成 3 秒句尾静音，让停止命令
    # 在静音区内提前发送，而不是切掉回答正文或等设备已经开始重播。
    silence_duration: int | None = 3000
    max_length_to_filter_parenthesis: int | None = None
    # 官方默认 false = 保留原始字符（"**你好**" 会读成"星星你好星星"），
    # 语言助手的场景要的是去掉标记，所以默认打开过滤。
    disable_markdown_filter: bool = True
    disable_emoji_filter: bool = True
    timeout: float = 30.0
    proxy: str | None = None
    additions: dict[str, Any] = field(default_factory=dict)
    post_process: dict[str, Any] = field(default_factory=dict)
    #: 配置里的原始风格值，逐句覆盖后靠它回滚（init=False，不进配置）
    _default_style: dict[str, Any] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._default_style = {key: getattr(self, key) for key in STYLE_FIELDS}

    @classmethod
    def from_config(cls, config: Config) -> DoubaoSpeaker:
        options = dict(config.tts_options)
        options.setdefault("proxy", config.proxy)
        known = {f.name for f in dataclasses.fields(cls)}
        if unknown := sorted(set(options) - known):
            # 拼错的键静默忽略 = 用户以为配了方言结果没生效，不如直接报出来
            raise ValueError(
                f"tts_options 里有豆包不认识的键 {unknown}，可用：{sorted(known)}"
            )
        return cls(**options)

    def set_instruction(self, instruction: str) -> None:
        """运行时改语音指令（由「语音指令 xxx」这条命令调用）。"""
        self.instruction = instruction.strip()
        # 手动设过之后它就是这个会话的基准，自动风格别再盖掉它
        self._default_style["instruction"] = self.instruction

    def set_style(self, style: dict) -> dict:
        """按场景覆盖这一句的说话方式，返回真正生效的值。

        非法值（超范围、方言名写错、类型不对）直接丢弃而不是报错：配音参数是
        锦上添花，绝不能因为它把整句话卡住。范围外的数字会被夹到边界。
        """
        applied: dict[str, Any] = {}
        for key, allowed in STYLE_FIELDS.items():
            if key not in style or style[key] in (None, ""):
                continue
            value = style[key]
            if allowed is None:  # instruction：只要字符串
                if not isinstance(value, str):
                    continue
                value = value.strip()[:60]
            elif isinstance(allowed, tuple) and allowed and isinstance(allowed[0], int):
                if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                    continue
                try:
                    value = int(float(value))
                except ValueError:
                    continue
                low, high = allowed  # type: ignore[misc]
                value = max(low, min(high, value))
            else:  # dialect：枚举
                if value not in allowed:
                    continue
            setattr(self, key, value)
            applied[key] = value
        return applied

    def reset_style(self) -> None:
        """回到配置里的默认说话方式（上一轮生成了风格、这一轮没有时用）。"""
        for key, value in self._default_style.items():
            setattr(self, key, value)

    def _context_texts(self) -> list[str]:
        if not self.instruction:
            return []
        if isinstance(self.instruction, str):
            return [self.instruction]
        return list(self.instruction)

    def _payload(self, text: str) -> dict[str, Any]:
        additions: dict[str, Any] = {
            "disable_markdown_filter": self.disable_markdown_filter,
            "disable_emoji_filter": self.disable_emoji_filter,
        }
        if self.dialect:
            additions["explicit_dialect"] = self.dialect
        if self.language:
            additions["explicit_language"] = self.language
        if self.silence_duration is not None:
            # V3 实测该 additions 字段会精确增加句尾静音。不要混入 V1 请求层的
            # enable_trailing_silence_audio：它在 V3 additions 中没有得到验证。
            additions["silence_duration"] = self.silence_duration
        if self.max_length_to_filter_parenthesis is not None:
            additions["max_length_to_filter_parenthesis"] = (
                self.max_length_to_filter_parenthesis
            )
        additions.update(self.additions)

        audio_params: dict[str, Any] = {
            "format": self.format,
            "sample_rate": self.sample_rate,
        }
        if self.speech_rate is not None:
            audio_params["speech_rate"] = self.speech_rate
        if self.loudness_rate is not None or self.loudness_boost:
            audio_params["loudness_rate"] = max(
                -50, min(100, (self.loudness_rate or 0) + self.loudness_boost)
            )
        if self.bit_rate is not None:
            audio_params["bit_rate"] = self.bit_rate

        req: dict[str, Any] = {
            "text": text,
            "speaker": self.speaker,
            "audio_params": audio_params,
            # ⚠️ additions 在协议里是 **JSON 字符串**，不是嵌套对象。官方参数表
            # 把它标成 string，发音词典的示例也是 "additions": "{\"tone\":[...]}"。
            # 写成对象服务端会解析失败，而报错信息未必指向这里。
            "additions": json.dumps(additions, ensure_ascii=False),
        }
        if context_texts := self._context_texts():
            req["context_texts"] = context_texts
        post_process = dict(self.post_process)
        if self.pitch is not None:
            post_process["pitch"] = self.pitch
        if post_process:
            req["post_process"] = post_process
        return {"req_params": req}

    def _duration(self, path: Path, text: str) -> float:
        """音频时长（秒）。

        文件模式靠它决定"等多久再发停止指令"，所以宁可估一个也不能不返回。
        """
        if self.format == "mp3":
            try:
                from mutagen.mp3 import MP3

                return float(MP3(path).info.length)
            except Exception as e:  # 损坏/不完整的 mp3 都可能解析失败
                logger.warning("读 mp3 时长失败（%s），改用字数估算", e)
        elif self.format == "pcm":
            # 16bit 单声道，采样率已知，直接按字节数算
            return path.stat().st_size / (self.sample_rate * 2)
        return calculate_tts_elapse(text)

    async def synthesize(
        self, text: str, out_file: str | Path, lang: str | None = None
    ) -> float:
        """合成一句话到文件，返回时长。

        `lang` 用不上：语种由 `language` 参数和文本自己决定，传进来的
        `zh-` 只是本仓库内部的语种标签。
        """
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            # 文档的参数表写 X-Api-Request-Id，配套 Java 示例写的是
            # X-Api-Connect-Id——两个都带上，省得赌哪个。
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Connect-Id": str(uuid.uuid4()),
            # 带上计费字符数，日志里能看到这次请求花了多少
            "X-Control-Require-Usage-Tokens-Return": "*",
        }
        out_file = Path(out_file)
        written = 0
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                ws = await session.ws_connect(
                    ENDPOINT, headers=headers, proxy=self.proxy, max_msg_size=0
                )
            except aiohttp.WSServerHandshakeError as e:
                # 握手就被拒（实测假 key 返回 401）：这条信息比 aiohttp 的
                # "Invalid response status" 有用得多——用户要的就是知道该去改哪
                if e.status in (401, 403):
                    raise RuntimeError(
                        f"豆包 TTS 鉴权失败（HTTP {e.status}）：检查 tts_options.api_key，"
                        "并确认控制台里已开通「语音合成大模型」"
                    ) from e
                raise RuntimeError(f"豆包 TTS 连接失败（HTTP {e.status}）") from e
            async with ws:
                await ws.send_bytes(build_request(self._payload(text)))
                with open(out_file, "wb") as f:
                    while True:
                        frame = await ws.receive(timeout=self.timeout)
                        if frame.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                        ):
                            break
                        if frame.type == aiohttp.WSMsgType.ERROR:
                            raise RuntimeError(f"豆包 TTS 连接异常：{ws.exception()}")
                        if frame.type != aiohttp.WSMsgType.BINARY:
                            continue
                        msg = parse_message(frame.data)
                        if msg.type == MSG_ERROR:
                            raise RuntimeError(
                                f"豆包 TTS 报错 {msg.error_code}: {msg.text}"
                            )
                        if msg.audio:
                            f.write(msg.audio)
                            written += len(msg.audio)
                        elif (
                            msg.type == MSG_FULL_SERVER_RESPONSE
                            and msg.event == EVENT_SESSION_FINISHED
                        ):
                            break
                        elif (
                            msg.type == MSG_FULL_SERVER_RESPONSE
                            and msg.event == EVENT_TTS_SENTENCE_START
                        ):
                            logger.debug("豆包开始合成：%s", msg.text[:80])
                        elif (
                            msg.type == MSG_FULL_SERVER_RESPONSE
                            and msg.event == EVENT_TTS_SUBTITLE
                        ):
                            logger.debug("豆包字幕：%s", msg.text[:80])
                        elif (
                            msg.type == MSG_FULL_SERVER_RESPONSE
                            and msg.event == EVENT_USAGE_RESPONSE
                        ):
                            logger.debug("豆包 TTS 用量：%s", msg.text)
        if not written:
            raise RuntimeError("豆包 TTS 没有返回音频（检查 speaker / api_key）")
        duration = self._duration(out_file, text)
        logger.debug(
            "豆包合成 %d 字节，%.2f 秒，音色 %s，指令 %r",
            written,
            duration,
            self.speaker,
            self.instruction,
        )
        return duration
