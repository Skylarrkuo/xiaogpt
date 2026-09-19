"""LLM provider 元数据表。

本 fork 只保留三家 provider，它们接入协议完全一致（OpenAI Chat
Completions），差别只有三处：base_url、默认模型、思考模式的语义。把这三处
集中在这张表里，`config.py`（配了 key 没配）、`cli.py`（`--bot` 取值）、
`bot/openai_compat_bot.py`（真正发请求）都从这里取，避免同一件事被硬编码
三遍、改一处漏两处。

这里只放静态元数据，**不 import 本项目任何模块**：`config.py` 在 import 期
就要读这张表，任何反向依赖都会绕成循环 import。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThinkingPolicy:
    """思考模式开关的厂商差异。

    三家的默认值和可关性都不一样，不能用一套开关硬套（实测语义）：

    - DeepSeek：默认关，可关；开的时候支持 `reasoning_effort`
    - MiMo：官方默认**开**（不传就是 enabled），可关；只有 `thinking.type`
      一个开关，没有 `reasoning_effort`
    - GLM：官方默认开，且 glm-5.3 系列**不允许关**（传 disabled 直接报错），
      只能靠 `reasoning_effort=low` 把推理压到最轻来控延迟

    locks_sampling 表示「开启思考后 temperature/top_p 是否失效」，失效的厂商
    必须在发请求前把这两个参数摘掉：DeepSeek 会直接报错，MiMo 会静默忽略，
    两种情况下留着都等于让 `gpt_options` 里的设置骗人。
    """

    default_on: bool
    can_disable: bool
    supports_effort: bool = False
    default_effort: str | None = None
    locks_sampling: bool = True


@dataclass(frozen=True)
class Provider:
    """一家 provider 的静态信息。"""

    key: str
    #: 出现在「正在问 X 请耐心等待」里的名字
    label: str
    #: Config 里存 API key / 模型名的字段名，由 from_config 反射读取
    api_key_field: str
    model_field: str
    default_model: str
    base_url: str
    #: 报错时告诉用户去哪申请 key
    docs_url: str
    thinking: ThinkingPolicy
    #: 流式请求能不能带 stream_options.include_usage 拿用量（拿得到才能统计
    #: 前缀缓存命中）。只有实测过的 provider 才打开：这个参数不在每个网关的
    #: 兼容范围内，带上可能被拒。
    stream_usage: bool = False


PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        key="deepseek",
        label="DeepSeek",
        api_key_field="deepseek_api_key",
        model_field="deepseek_model",
        default_model="deepseek-flash",
        base_url="https://api.deepseek.com",
        docs_url="https://platform.deepseek.com",
        thinking=ThinkingPolicy(
            default_on=False, can_disable=True, supports_effort=True
        ),
        stream_usage=True,
    ),
    "mimo": Provider(
        key="mimo",
        label="MiMo",
        api_key_field="mimo_api_key",
        model_field="mimo_model",
        # 非 pro 的那只更快更便宜；要更强就配 mimo-v2.5-pro
        default_model="mimo-v2.5",
        base_url="https://api.xiaomimimo.com/v1",
        docs_url="https://mimo.mi.com/docs",
        thinking=ThinkingPolicy(default_on=False, can_disable=True),
    ),
    "glm": Provider(
        key="glm",
        label="GLM",
        api_key_field="glm_api_key",
        model_field="glm_model",
        # flash 是低价快速档；要旗舰就配 glm-5.3
        default_model="glm-5.3-flash",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        docs_url="https://docs.bigmodel.cn",
        thinking=ThinkingPolicy(
            default_on=True,
            can_disable=False,
            supports_effort=True,
            default_effort="low",
            locks_sampling=False,
        ),
    ),
}

DEFAULT_PROVIDER = "deepseek"
