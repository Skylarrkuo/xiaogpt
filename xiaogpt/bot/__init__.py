from __future__ import annotations

from xiaogpt.bot.base_bot import BaseBot
from xiaogpt.bot.openai_compat_bot import OpenAICompatBot
from xiaogpt.config import Config
from xiaogpt.providers import PROVIDERS

# 三家 provider 共用同一个实现，差异全在 providers.py 的表里，所以这里不是
# 一个类名一个映射，而是按 provider 名字把同一个类注册多份。
BOTS: dict[str, type[BaseBot]] = {key: OpenAICompatBot for key in PROVIDERS}


def get_bot(config: Config) -> BaseBot:
    try:
        bot_cls = BOTS[config.bot]
    except KeyError:
        raise ValueError(f"Unsupported bot {config.bot}, must be one of {list(BOTS)}")
    return bot_cls.from_config(config)


__all__ = ["OpenAICompatBot", "get_bot"]
