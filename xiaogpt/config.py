from __future__ import annotations

import argparse
import functools
import json
import os
import re
from dataclasses import dataclass, field, fields
from typing import Any, Iterable, Literal

import yaml

from xiaogpt.device import DeviceProfile, get_device_profile
from xiaogpt.fallback import (
    FALLBACK_ANSWER_KEYWORD,
    check_phrases,
    normalize_phrases,
)
from xiaogpt.providers import DEFAULT_PROVIDER, PROVIDERS
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
CHANGE_TTS_INSTRUCTION_KEY_WORD = ("语音指令",)
SUPPORTED_TTS = ("mi", "edge", "doubao")
PROMPT = "以下请用 300 字以内回答，请只回答文字不要带链接"
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
        "deepseek_api_key",
        "mimo_api_key",
        "glm_api_key",
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
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    # 留空则用 providers.py 里的默认模型
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "")
    mimo_api_key: str = os.getenv("MIMO_API_KEY", "")
    mimo_model: str = os.getenv("MIMO_MODEL", "")
    glm_api_key: str = os.getenv("GLM_API_KEY", "")
    glm_model: str = os.getenv("GLM_MODEL", "")
    proxy: str | None = None
    mi_did: str = os.getenv("MI_DID", "")
    keyword: Iterable[str] = KEY_WORD
    change_prompt_keyword: Iterable[str] = CHANGE_PROMPT_KEY_WORD
    # 说话方式也能用说的改：「语音指令 用四川话温柔一点」→ 转给 TTS。
    # 只有豆包 TTS 的部分音色支持（API 的 context_texts），留空即关闭。
    change_tts_instruction_keyword: Iterable[str] = CHANGE_TTS_INSTRUCTION_KEY_WORD
    # 豆包 TTS：每句都让 AI 先判断「这句话该怎么念」（语速 / 音量 / 方言 /
    # 语音指令），再据此合成。代价是每次回答多一次很短的模型调用。
    # 用「语音指令 xxx」手动设过一次之后，本次运行不再自动覆盖。
    tts_auto_instruction: bool = True
    # 对话记忆：历史超过这么多字符就把最老的一半压成摘要（0 = 不压缩）。
    # 之所以只在超限时压缩：压缩会改写前缀，前缀一变缓存全失效，所以要"攒着
    # 一次压掉"，中间让前缀保持不动、让缓存一直命中。
    history_budget_chars: int = 16000
    # 压缩后至少保留最近这么多轮原文（摘要毕竟有损）
    history_keep_turns: int = 4
    # 摘要长度上限（字）
    summary_max_chars: int = 500
    # 流式请求带不带 stream_options.include_usage（拿用量才能统计缓存命中）。
    # 留空 = 按 provider 的实测结论（目前只有 deepseek 开着）；MiMo/GLM 想统计
    # 可以设 true，若报错说明它不支持这个参数。
    stream_usage: bool | None = None
    # 兜底接管：小爱的回答里命中这些片段，就当作"她答不上来"，把提问转给 AI。
    # 填的是**她的回答**，不是你说的话。留空（默认）即关闭，行为与上游一致。
    fallback_answer_keyword: Iterable[str] = FALLBACK_ANSWER_KEYWORD
    # 学习模式：把没命中词干表的回答交给 AI 判断是否属于兜底话术，结果落盘。
    # 只在开启时才会产生额外的 API 调用。
    learn_fallback: bool = False
    # AI 回答完之后，这段时间内的下一句仍然交给 AI。0 = 关闭。
    follow_up_seconds: int = 0
    prompt: str = PROMPT
    mute_xiaoai: bool = False
    bot: str = DEFAULT_PROVIDER
    # 覆盖 provider 的默认接口地址（代理 / 自建网关用），留空即用官方地址
    api_base: str | None = None
    use_command: bool = False
    verbose: int = 0
    start_conversation: str = "开始持续对话"
    end_conversation: str = "结束持续对话"
    stream: bool = False
    # 只留三家：小爱原生 mi、微软 edge、豆包 doubao
    tts: Literal["mi", "edge", "doubao"] = "mi"
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
        # 短语表要在任何判定之前归一化并过长度闸门：把「我」这种单字写进去会让
        # 每一条回答都命中，于是所有话都被转给 AI——静默且灾难性，所以直接拒绝。
        self.fallback_answer_keyword = normalize_phrases(self.fallback_answer_keyword)
        # 型号自带的兜底词干（实测得到的已知模板）与用户配置**合并**而不是
        # 二选一。合成一个字段，调用方读到的就是最终生效的那份，不会漏合。
        if self.device_profile.fallback_phrases:
            self.fallback_answer_keyword = tuple(
                dict.fromkeys(
                    tuple(self.fallback_answer_keyword)
                    + self.device_profile.fallback_phrases
                )
            )
        check_phrases(self.fallback_answer_keyword)
        if self.proxy:
            validate_proxy(self.proxy)
        # TTS 白名单与豆包的必填项都在这里拦下来：音箱不出声是最难查的故障，
        # 配置写错就该在启动时直接报出来。
        if self.tts not in SUPPORTED_TTS:
            raise Exception(
                f"Unsupported tts {self.tts!r}, must be one of {list(SUPPORTED_TTS)}"
            )
        if self.tts == "doubao":
            missing = [
                key
                for key in ("api_key", "speaker")
                if not str(self.tts_options.get(key, "")).strip()
            ]
            if missing:
                raise Exception(
                    "tts: doubao needs `tts_options`: "
                    f"{', '.join(f'`{m}`' for m in missing)}"
                )
        # provider 表是唯一事实来源：bot 名字合法性与「key 配了没」都按它校验，
        # 新增 provider 时不用回来改这里。
        provider = PROVIDERS.get(self.bot)
        if provider is None:
            raise Exception(
                f"Unsupported bot {self.bot!r}, must be one of {list(PROVIDERS)}"
            )
        # 用 strip 后再判空：yaml 里写 `glm_api_key: "   "` 也是没配，
        # 这种错误早报比等到音箱一声不吭、再去翻日志找 401 便宜得多。
        if not getattr(self, provider.api_key_field).strip():
            raise Exception(
                f"Using {provider.label} api needs {provider.api_key_field}, "
                f"get a key at {provider.docs_url}"
            )

    @functools.cached_property
    def device_profile(self) -> DeviceProfile:
        """本型号的硬件行为差异。未登记的型号返回默认值，即上游行为。

        profile 由既有的 hardware 字段驱动，不是用户配置项，所以 cli 与
        yaml 示例都不需要新增参数。
        """
        return get_device_profile(self.hardware)

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
                if key in (
                    "keyword",
                    "change_prompt_keyword",
                    "change_tts_instruction_keyword",
                    "fallback_answer_keyword",
                ):
                    if not isinstance(value, list):
                        value = [value]
                    value = [kw for kw in value if kw]
                elif key == "use_deepseek":
                    key, value = "bot", "deepseek"
                elif key == "use_mimo":
                    key, value = "bot", "mimo"
                elif key == "use_glm":
                    key, value = "bot", "glm"
                elif key == "enable_edge_tts":
                    key, value = "tts", "edge"
                if key in cls.__dataclass_fields__:
                    result[key] = value
        return result
