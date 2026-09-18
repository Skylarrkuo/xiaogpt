from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field, fields
from typing import Any, Iterable, Literal

import yaml

from xiaogpt.utils import validate_proxy

LATEST_ASK_API = "https://userprofile.mina.mi.com/device_profile/v2/conversation?source=dialogu&hardware={hardware}&timestamp={timestamp}&limit=2"
COOKIE_TEMPLATE = "deviceId={device_id}; serviceToken={service_token}; userId={user_id}"
WAKEUP_KEYWORD = "小爱同学"

HARDWARE_COMMAND_DICT = {
    # hardware: (tts_command, wakeup_command)
    "LX06": ("5-1", "5-5"),
    "L05B": ("5-3", "5-4"),
    "S12": ("5-1", "5-5"),  # 第一代小爱，型号 MDZ-25-DA
    "S12A": ("5-1", "5-5"),
    "LX01": ("5-1", "5-5"),
    "L06A": ("5-1", "5-5"),
    "LX04": ("5-1", "5-4"),
    "L05C": ("5-3", "5-4"),
    "L17A": ("7-3", "7-4"),
    "X08E": ("7-3", "7-4"),
    "LX05A": ("5-1", "5-5"),  # 小爱红外版
    "LX5A": ("5-1", "5-5"),  # 小爱红外版
    "L07A": ("5-1", "5-5"),  # Redmi 小爱音箱 Play(l7a)
    "L15A": ("7-3", "7-4"),
    "X6A": ("7-3", "7-4"),  # 小米智能家庭屏 6
    "X10A": ("7-3", "7-4"),  # 小米智能家庭屏 10
    # add more here
}

DEFAULT_COMMAND = ("5-1", "5-5")

KEY_WORD = ("帮我", "请")
CHANGE_PROMPT_KEY_WORD = ("更改提示词",)
PROMPT = "以下请用 300 字以内回答，请只回答文字不要带链接"
# simulate_xiaoai_question
MI_ASK_SIMULATE_DATA = {
    "code": 0,
    "message": "Success",
    "data": '{"bitSet":[0,1,1],"records":[{"bitSet":[0,1,1,1,1],"answers":[{"bitSet":[0,1,1,1],"type":"TTS","tts":{"bitSet":[0,1],"text":"Fake Answer"}}],"time":1677851434593,"query":"Fake Question","requestId":"fada34f8fa0c3f408ee6761ec7391d85"}],"nextEndTime":1677849207387}',
}


# 用 ASCII 而不是中文：日志经常被重定向到文件，Windows 控制台编码会把
# 中文标记变成乱码，反而看不懂。
MASK = "***"

# 需要打码的顶层字段。这里必须用显式集合而不是按名字正则匹配——
# `keyword` 和 `change_prompt_keyword` 名字里都带 key，但它们是唤醒词，
# 匹配 "key" 会把它们一起打码，反而让日志失去意义。
# account 是手机号，属于个人信息，一并打码，便于把日志贴出去求助。
_MASKED_FIELDS = frozenset(
    {
        "account",
        "password",
        "openai_key",
        "gemini_key",
        "volc_access_key",
        "volc_secret_key",
        "volc_api_key",
        "deepseek_api_key",
    }
)

# tts_options / gpt_options 里的值由 from_options 注入（volc 的
# access_key/secret_key、fish 的 api_key），这里按字典键名判断。
_SECRET_NAME = re.compile(r"key|secret|token|password|passwd|credential", re.I)


def _redact(value: Any) -> Any:
    """递归打码嵌套结构里名字像凭据的字段。"""
    if isinstance(value, dict):
        return {
            k: (MASK if _SECRET_NAME.search(str(k)) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_redact(v) for v in value)
    return value


@dataclass
class Config:
    hardware: str = "LX06"
    account: str = os.getenv("MI_USER", "")
    password: str = os.getenv("MI_PASS", "")
    openai_key: str = os.getenv("OPENAI_API_KEY", "")
    gemini_key: str = os.getenv("GEMINI_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "")
    gemini_api_domain: str = os.getenv(
        "GEMINI_API_DOMAIN", ""
    )  # 自行部署的 Google Gemini 代理
    volc_access_key: str = os.getenv("VOLC_ACCESS_KEY", "")
    volc_secret_key: str = os.getenv("VOLC_SECRET_KEY", "")
    volc_api_key: str = os.getenv("volc_api_key", "")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    proxy: str | None = None
    mi_did: str = os.getenv("MI_DID", "")
    keyword: Iterable[str] = KEY_WORD
    change_prompt_keyword: Iterable[str] = CHANGE_PROMPT_KEY_WORD
    prompt: str = PROMPT
    mute_xiaoai: bool = False
    bot: str = "chatgptapi"
    api_base: str | None = None
    deployment_id: str | None = None
    use_command: bool = False
    verbose: int = 0
    start_conversation: str = "开始持续对话"
    end_conversation: str = "结束持续对话"
    stream: bool = False
    tts: Literal[
        "mi", "edge", "azure", "openai", "baidu", "google", "volc", "minimax", "fish"
    ] = "mi"
    tts_options: dict[str, Any] = field(default_factory=dict)
    gpt_options: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        # dataclass 默认生成的 __repr__ 会把所有字段原样打出，而
        # xiaogpt.py 在 -v/-vv 下会 log.debug(config)，等于把密码和各
        # 家 API key 明文写进日志。这里覆盖掉，让任何打印 config 的地方
        # 都自动安全。
        parts = []
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in _MASKED_FIELDS:
                parts.append(f"{f.name}={MASK!r}")
            else:
                parts.append(f"{f.name}={_redact(value)!r}")
        return f"{type(self).__name__}({', '.join(parts)})"

    def __post_init__(self) -> None:
        if self.proxy:
            validate_proxy(self.proxy)
        if (
            self.api_base
            and self.api_base.endswith(("openai.azure.com", "openai.azure.com/"))
            and not self.deployment_id
        ):
            raise Exception(
                "Using Azure OpenAI needs deployment_id, read this: "
                "https://learn.microsoft.com/en-us/azure/cognitive-services/openai/how-to/chatgpt?pivots=programming-language-chat-completions"
            )
        if self.bot in ["chatgptapi"]:
            if not self.openai_key:
                raise Exception(
                    "Using GPT api needs openai API key, please google how to"
                )
        if self.bot == "deepseek":
            if not self.deepseek_api_key:
                raise Exception(
                    "Using Deepseek api needs Deepseek API key, please visit https://platform.deepseek.com"
                )

    @property
    def tts_command(self) -> str:
        return HARDWARE_COMMAND_DICT.get(self.hardware, DEFAULT_COMMAND)[0]

    @property
    def wakeup_command(self) -> str:
        return HARDWARE_COMMAND_DICT.get(self.hardware, DEFAULT_COMMAND)[1]

    @classmethod
    def from_options(cls, options: argparse.Namespace) -> Config:
        config = {}
        if options.config:
            config = cls.read_from_file(options.config)
        for key, value in vars(options).items():
            if value is not None and key in cls.__dataclass_fields__:
                config[key] = value
        if config.get("tts") == "volc":
            config.setdefault("tts_options", {}).setdefault(
                "access_key", config.get("volc_access_key")
            )
            config.setdefault("tts_options", {}).setdefault(
                "secret_key", config.get("volc_secret_key")
            )
        elif config.get("tts") == "fish":
            config.setdefault("tts_options", {}).setdefault(
                "api_key", config.get("fish_api_key")
            )
            if voice := config.get("fish_voice_key"):
                config.setdefault("tts_options", {}).setdefault("voice", voice)

        return cls(**config)

    @classmethod
    def read_from_file(cls, config_path: str) -> dict:
        result = {}
        with open(config_path, "rb") as f:
            if config_path.endswith(".json"):
                config = json.load(f)
            else:
                config = yaml.safe_load(f)
            for key, value in config.items():
                if value is None:
                    continue
                if key == "keyword":
                    if not isinstance(value, list):
                        value = [value]
                    value = [kw for kw in value if kw]
                elif key == "use_chatgpt_api":
                    key, value = "bot", "chatgptapi"
                elif key == "use_gemini":
                    key, value = "bot", "gemini"
                elif key == "use_doubao":
                    key, value = "bot", "doubao"
                elif key == "use_deepseek":
                    key, value = "bot", "deepseek"
                elif key == "enable_edge_tts":
                    key, value = "tts", "edge"
                if key in cls.__dataclass_fields__:
                    result[key] = value
        return result
