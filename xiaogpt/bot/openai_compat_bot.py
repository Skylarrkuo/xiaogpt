"""走 OpenAI Chat Completions 协议的 bot（DeepSeek / MiMo / GLM 通用）。

三家 provider 的差别只有 base_url、默认模型和思考模式语义，全部记在
`xiaogpt/providers.py` 的表里，这里只负责协议本身，所以一个类就够了。
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import httpx
from rich import print

from xiaogpt.bot.base_bot import BaseBot, ChatHistoryMixin
from xiaogpt.providers import PROVIDERS, Provider
from xiaogpt.utils import split_sentences

if TYPE_CHECKING:
    import openai


def fetch_available_models(
    provider: Provider, api_key: str, proxy: str | None = None
) -> list[str] | None:
    """查询 API 支持的模型列表。

    网络不通、或这家根本不提供 `/models`（GLM 就未必有）时返回 None，调用方
    应跳过校验；API key 无效则直接抛 SystemExit——那属于配置错误，早点报出来
    比让音箱一声不吭好。
    """
    kwargs = {"proxy": proxy} if proxy else {}
    try:
        with httpx.Client(trust_env=True, timeout=15, **kwargs) as client:
            resp = client.get(
                f"{provider.base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
        return sorted(m["id"] for m in resp.json().get("data", []))
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            raise SystemExit(
                f"\n{provider.label} API key was rejected "
                f"(HTTP {e.response.status_code}).\n"
                f"Check `{provider.api_key_field}` in config.yaml, "
                f"get a key at {provider.docs_url}\n"
            ) from None
        print(
            f"[warn] {provider.label} has no model list "
            f"(HTTP {e.response.status_code}), skipping validation"
        )
        return None
    except Exception as e:
        print(
            f"[warn] could not reach {provider.label} to validate the model name "
            f"({type(e).__name__}), skipping validation"
        )
        return None


@dataclasses.dataclass
class OpenAICompatBot(ChatHistoryMixin, BaseBot):
    """三家 provider 共用的实现。

    模型名优先级（高 → 低）：`gpt_options.model` → `<provider>_model` 配置 →
    内置默认值（见 providers.py）。

    思考模式默认值同样在 providers.py：DeepSeek / MiMo 默认**关闭**（语音场景
    要的是低延迟），GLM 的 glm-5.3 系列关不掉，默认用 `reasoning_effort=low`
    把推理压到最轻。
    """

    provider: Provider
    api_key: str
    model: str = ""
    base_url: str = ""
    proxy: str | None = None
    #: 把模型回复打到终端吗。主对话要（终端就是给人看的），
    #: 内部用途（如「配音参谋」）不要——它会把 JSON 混进正常输出里。
    quiet: bool = False
    #: 提示词与更早对话的摘要（见 ChatHistoryMixin）
    system_prompt: str = ""
    summary: str = ""
    #: 覆盖 provider 的 stream_usage（None = 按 provider 的实测结论）
    stream_usage: bool | None = None
    history: list[tuple[str, str]] = dataclasses.field(default_factory=list, init=False)
    # GLM 关不掉思考，用户如果写了 thinking.type=disabled，我们只能纠正过来。
    # 纠正本身要留痕，否则用户以为配置生效了；但每次提问都刷屏也没必要。
    _thinking_warned: bool = dataclasses.field(default=False, init=False)

    @property
    def name(self) -> str:
        return self.provider.label

    @property
    def actual_model(self) -> str:
        """配置 → 默认值。gpt_options.model 的覆盖在 _build_kwargs 里生效。"""
        return self.model or self.provider.default_model

    @property
    def want_stream_usage(self) -> bool:
        """流式请求要不要带 include_usage（拿不到用量就统计不了缓存命中）。"""
        if self.stream_usage is None:
            return self.provider.stream_usage
        return self.stream_usage

    @classmethod
    def from_config(cls, config) -> OpenAICompatBot:
        provider = PROVIDERS[config.bot]
        return cls(
            provider=provider,
            api_key=getattr(config, provider.api_key_field),
            model=getattr(config, provider.model_field),
            # api_base 是给代理 / 自建网关用的逃生舱，配了就覆盖官方地址
            base_url=config.api_base or provider.base_url,
            proxy=config.proxy,
            stream_usage=config.stream_usage,
        )

    def validate(self) -> None:
        """启动时向 API 核对模型名。

        ask/ask_stream 会把异常吞掉并返回空字符串，模型名写错的表现是
        音箱一声不吭。这里提前拦下来，给出可操作的报错。
        """
        models = fetch_available_models(self.provider, self.api_key, self.proxy)
        if models is None:
            return  # 取不到列表，不阻断启动
        if self.actual_model in models:
            return
        # 报错信息用 ASCII：本机控制台对中文编码有问题，中文报错会变成乱码，
        # 一个看不懂的报错等于没有报错。
        raise SystemExit(
            f"\n{self.provider.label} model {self.actual_model!r} is not supported "
            f"by the API.\n"
            f"Available models: {', '.join(models)}\n"
            f"Fix `{self.provider.model_field}` in config.yaml "
            f"(leave it empty to use {self.provider.default_model}).\n"
        )

    def _make_openai_client(self, sess: httpx.AsyncClient) -> openai.AsyncOpenAI:
        import openai

        return openai.AsyncOpenAI(
            api_key=self.api_key,
            http_client=sess,
            base_url=self.base_url,
        )

    def _log_cache(self, usage) -> None:
        """把这次请求的**前缀缓存命中**打出来。

        字段各家叫法不同：DeepSeek 是 `prompt_cache_hit_tokens`（还有
        `prompt_cache_miss_tokens`），OpenAI 兼容的写法是
        `prompt_tokens_details.cached_tokens`（GLM/MiMo 走这套）。两种都认，
        认不出就什么都不打——这只是观测，绝不影响回答。
        """
        if usage is None or self.quiet:
            return
        prompt = getattr(usage, "prompt_tokens", None)
        hit = getattr(usage, "prompt_cache_hit_tokens", None)
        if hit is None:
            details = getattr(usage, "prompt_tokens_details", None)
            hit = getattr(details, "cached_tokens", None) if details else None
        if not prompt or hit is None:
            return
        miss = getattr(usage, "prompt_cache_miss_tokens", None)
        if miss is None:
            miss = max(prompt - hit, 0)
        print(
            # 方括号要转义：本模块的 print 是 rich 的，会把 [cache] 当样式标记吃掉
            f"\\[cache] 输入 {prompt} tokens：命中 {hit}（{hit / prompt:.0%}），"
            f"未命中 {miss}"
        )

    def _build_kwargs(self, options: dict) -> dict:
        """构建请求参数，把三家不一致的思考模式抹平。

        `gpt_options` 里可以传两个逃生舱：

        - `thinking`：原样透传给厂商（写厂商私有字段时需要，如 clear_thinking）
        - `reasoning_effort`：表示「开思考」，值由厂商解释；厂商不支持就只开思考

        都没传时用 provider 的默认值，见 ThinkingPolicy 的注释。
        """
        kwargs = {"model": self.actual_model, **options}
        policy = self.provider.thinking
        user_thinking = kwargs.pop("thinking", None)
        effort = kwargs.pop("reasoning_effort", None)
        thinking = user_thinking

        if thinking is not None and not isinstance(thinking, dict):
            # 写错的配置不该把整次提问打崩，退回默认策略并说明原因
            print(f"[warn] gpt_options.thinking 应是字典，已忽略：{thinking!r}")
            thinking = None

        if thinking is None:
            # 传了 reasoning_effort 就等于「我要开思考」，先记下意图再处理
            # 参数本身——不同厂商对这个参数的接受度不一样。
            wants_thinking = effort is not None
            if wants_thinking and not policy.supports_effort:
                # MiMo 没有 reasoning_effort 这个参数，直接发会被 API 拒掉，
                # 但「用户想开思考」这层意图是明确的，退化成只开思考。
                effort = None
            enabled = wants_thinking or policy.default_on
            thinking = {"type": "enabled" if enabled else "disabled"}

        if thinking.get("type") == "disabled" and not policy.can_disable:
            # 与其让 API 报错回一个空字符串（表现是音箱一声不吭），不如
            # 就地改成开启，并明确告诉用户配置没生效。
            if not self._thinking_warned:
                print(
                    f"[warn] {self.provider.label} 不支持关闭思考模式，"
                    "已按开启处理（低延迟请用 gpt_options.reasoning_effort: low）"
                )
                self._thinking_warned = True
            thinking = {"type": "enabled"}

        kwargs["extra_body"] = {"thinking": thinking}
        if effort is not None:
            kwargs["reasoning_effort"] = effort
        elif (
            user_thinking is None
            and thinking.get("type") == "enabled"
            and policy.default_effort
        ):
            # GLM 默认开启思考且默认强度是 max（旗舰档），语音场景必须压低
            # 只在用户没自己指定 thinking 时兜底，别覆盖用户的显式选择
            kwargs["reasoning_effort"] = policy.default_effort

        if thinking.get("type") == "enabled" and policy.locks_sampling:
            for key in (
                "temperature",
                "top_p",
                "presence_penalty",
                "frequency_penalty",
            ):
                kwargs.pop(key, None)

        return kwargs

    async def ask(self, query, **options):
        ms = self.get_messages()
        ms.append({"role": "user", "content": query})
        kwargs = self._build_kwargs(options)
        httpx_kwargs = {}
        if self.proxy:
            httpx_kwargs["proxy"] = self.proxy
        async with httpx.AsyncClient(trust_env=True, **httpx_kwargs) as sess:
            client = self._make_openai_client(sess)
            try:
                completion = await client.chat.completions.create(messages=ms, **kwargs)
            except Exception as e:
                print(str(e))
                return ""

            message = completion.choices[0].message.content
            self.add_message(query, message)
            self._log_cache(getattr(completion, "usage", None))
            if not self.quiet:
                print(message)
            return message

    async def ask_stream(self, query, **options):
        ms = self.get_messages()
        ms.append({"role": "user", "content": query})
        kwargs = self._build_kwargs(options)
        if self.want_stream_usage:
            # 有了它，流式也能在最后一个 chunk 拿到 usage，进而统计缓存命中
            kwargs["stream_options"] = {"include_usage": True}
        httpx_kwargs = {}
        if self.proxy:
            httpx_kwargs["proxy"] = self.proxy
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
                    # 最后一个 chunk 只有 usage（choices 为空），用来统计缓存命中
                    if getattr(event, "usage", None) is not None:
                        self._log_cache(event.usage)
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
