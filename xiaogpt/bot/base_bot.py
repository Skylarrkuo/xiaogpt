from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, TypeVar

from xiaogpt.config import Config

T = TypeVar("T", bound="BaseBot")


class BaseBot(ABC):
    name: str

    @abstractmethod
    async def ask(self, query: str, **options: Any) -> str:
        pass

    @abstractmethod
    async def ask_stream(self, query: str, **options: Any) -> AsyncGenerator[str, None]:
        pass

    @classmethod
    @abstractmethod
    def from_config(cls: type[T], config: Config) -> T:
        pass

    @abstractmethod
    def has_history(self) -> bool:
        pass

    @abstractmethod
    def change_prompt(self, new_prompt: str) -> None:
        pass

    def validate(self) -> None:
        """启动自检钩子，默认不做任何事。

        子类可覆盖以在开始轮询前核对配置（如模型名、凭据）。
        校验不通过应抛出异常以终止启动，不要静默降级——否则用户
        对着音箱说话时只会得到一片沉默，无从判断问题出在哪。
        """


class ChatHistoryMixin:
    """对话历史：**只在尾部追加**，太长了才把最老的一半压成摘要。

    为什么死守"只追加"：三家 provider（DeepSeek / MiMo / GLM）都有**前缀缓存**，
    命中的前提是这次请求的 messages 前缀与上次**逐字节一致**。所以这里绝不做
    "只留最近 N 轮"的滑动窗口——那等于每轮都换一个新前缀，缓存全废（实测
    DeepSeek 命中与不命中是十倍价差）。要控长度，就把最老的一半压成摘要塞进
    系统提示：一次性失效，之后继续只追加。

    system 提示（含摘要）放最前、对话追加在后，是为了让稳定前缀尽量长：只要没改
    提示词、没触发压缩，每次请求的前缀就完全一样。
    """

    history: list[tuple[str, str]]
    #: 用户提示词。放 system 而不是塞进第一条用户消息：塞进去等于把提示词混进
    #: 历史，改一次提示词就改写历史，前缀也稳不住。
    system_prompt: str
    #: 被压掉的更早对话
    summary: str

    def has_history(self) -> bool:
        return bool(self.history or self.summary)

    def change_prompt(self, new_prompt: str) -> None:
        self.system_prompt = new_prompt

    def get_messages(self) -> list[dict]:
        ms: list[dict] = []
        system = self.system_prompt
        if self.summary:
            system = (
                f"{system}\n\n【更早的对话摘要】\n{self.summary}"
                if system
                else f"【更早的对话摘要】\n{self.summary}"
            )
        if system:
            ms.append({"role": "system", "content": system})
        for query, answer in self.history:
            ms.append({"role": "user", "content": query})
            ms.append({"role": "assistant", "content": answer})
        return ms

    def clear_history(self) -> None:
        """显式重置（追问窗口过期不再清历史，见 xiaogpt.py 的会话标记）。"""
        self.history.clear()
        self.summary = ""

    def add_message(self, query: str, message: str) -> None:
        self.history.append((query, message))

    # ---- 压缩：长对话不丢上下文，又不让前缀无限膨胀 ----

    def history_chars(self) -> int:
        """历史的字符数（中文场景下 1 字与 1 token 同量级）。"""
        return sum(len(query) + len(answer) for query, answer in self.history)

    def oldest_turns(self, keep_turns: int) -> list[tuple[str, str]]:
        """返回"最老、可以压掉"的那批，保留最后 keep_turns 轮。"""
        if len(self.history) <= keep_turns:
            return []
        return self.history[:-keep_turns]

    def apply_compaction(self, drop_count: int, summary: str) -> None:
        """把最老的 drop_count 轮换成一段摘要。"""
        del self.history[:drop_count]
        self.summary = summary.strip()
