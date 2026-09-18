from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, ClassVar

import httpx
from rich import print

from xiaogpt.bot.base_bot import BaseBot, ChatHistoryMixin
from xiaogpt.utils import split_sentences

if TYPE_CHECKING:
    import openai


@dataclasses.dataclass
class DeepseekBot(ChatHistoryMixin, BaseBot):
    """Deepseek API bot.

    默认关闭思考模式以获得最快响应速度（适合语音助手场景）。
    如需开启思考模式，在 gpt_options 中设置 reasoning_effort，例如：
        gpt_options:
          reasoning_effort: high
    """

    name: ClassVar[str] = "Deepseek"
    default_options: ClassVar[dict[str, str]] = {"model": "deepseek-flash"}
    deepseek_api_key: str
    api_base: str = "https://api.deepseek.com"
    proxy: str | None = None
    history: list[tuple[str, str]] = dataclasses.field(default_factory=list, init=False)

    def _make_openai_client(self, sess: httpx.AsyncClient) -> openai.AsyncOpenAI:
        import openai

        return openai.AsyncOpenAI(
            api_key=self.deepseek_api_key,
            http_client=sess,
            base_url=self.api_base,
        )

    @classmethod
    def from_config(cls, config):
        return cls(
            deepseek_api_key=config.deepseek_api_key,
            api_base="https://api.deepseek.com",
            proxy=config.proxy,
        )

    def _build_kwargs(self, options: dict) -> dict:
        """构建请求参数，自动处理思考模式。

        - 默认关闭思考模式（type=disabled），以获得最低延迟。
        - 如果用户通过 gpt_options 传入 reasoning_effort，则自动开启思考模式。
        - 思考模式下不发送 temperature/top_p 等不兼容参数。
        """
        kwargs = {**self.default_options, **options}
        reasoning_effort = kwargs.pop("reasoning_effort", None)

        if reasoning_effort:
            # 用户指定了 reasoning_effort -> 开启思考模式
            kwargs["reasoning_effort"] = reasoning_effort
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            # 思考模式不支持 temperature/top_p 等参数，移除以避免无效设置
            for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
                kwargs.pop(key, None)
        else:
            # 默认关闭思考模式，获得最快响应
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        return kwargs

    async def ask(self, query, **options):
        ms = self.get_messages()
        ms.append({"role": "user", "content": query})
        kwargs = self._build_kwargs(options)
        httpx_kwargs = {}
        if self.proxy:
            httpx_kwargs["proxies"] = self.proxy
        async with httpx.AsyncClient(trust_env=True, **httpx_kwargs) as sess:
            client = self._make_openai_client(sess)
            try:
                completion = await client.chat.completions.create(
                    messages=ms, **kwargs
                )
            except Exception as e:
                print(str(e))
                return ""

            message = completion.choices[0].message.content
            self.add_message(query, message)
            print(message)
            return message

    async def ask_stream(self, query, **options):
        ms = self.get_messages()
        ms.append({"role": "user", "content": query})
        kwargs = self._build_kwargs(options)
        httpx_kwargs = {}
        if self.proxy:
            httpx_kwargs["proxies"] = self.proxy
        async with httpx.AsyncClient(trust_env=True, **httpx_kwargs) as sess:
            client = self._make_openai_client(sess)
            try:
                completion = await client.chat.completions.create(
                    messages=ms, stream=True, **kwargs
                )
            except Exception as e:
                print(str(e))
                return

            async def text_gen():
                async for event in completion:
                    if not event.choices:
                        continue
                    chunk_message = event.choices[0].delta
                    # 跳过思考链内容，只输出最终回答
                    if chunk_message.content is None:
                        continue
                    print(chunk_message.content, end="")
                    yield chunk_message.content

            message = ""
            try:
                async for sentence in split_sentences(text_gen()):
                    message += sentence
                    yield sentence
            finally:
                print()
                self.add_message(query, message)
