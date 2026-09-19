"""型号相关的硬件行为差异。

本 fork 唯一的「某型号有什么特殊行为」集中点。上游代码里没有任何型号行为
分支，真实差异藏在 miservice 里：`_USE_PLAY_MUSIC_API` 名单内的型号
（LX04 / LX05 / L05B / L05C / L06 / L06A / X08A / X10A）调 play_by_url 时走的是
ubus `player_play_music`，而不是通用的 `player_play_url`。那条路径的行为与
其它型号不同，依赖改不了，只能在这里补。

设计不变量：**表里只登记与默认行为不同的型号**。未登记的型号拿到
DEFAULT_PROFILE，行为与上游一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from miservice import miio_command

if TYPE_CHECKING:
    from miservice import MiIOService


STOP_DIRECTIVE = "停止播放"
"""停止播放用的文本指令，必须是音箱认识的自然语言。"""

STOP_EARLY_SECONDS = 0.4
"""音频还差这么多就发停止指令，而不是等它放完。

**必须提前，而且要提前够多。** 该型号播完立刻重播，中间几乎不留空隙（实测
5.5 秒的 mp3 每 5.4~5.7 秒起一轮，4.0 秒的音频每 4.0 秒起一轮），等满了再停
第二遍的开头已经出来了；反过来，在**播放中途**发停止指令能取消排队中的下一
轮，所以只要赶在音频结束前把指令发出去就行。

提前 0.4 秒为什么听不出来：edge-tts 生成的 mp3 结尾自带约 0.5 秒静音（实测
最后 11 个 50ms 窗格峰值全为 0），所以切掉的只是静音。换用别的 TTS 后端时
这个前提可能不成立（结尾静音长度未必相同），届时需要重新校准这个值。

代价是零：这是提前，不是延后，不会让用户多等。
"""

BURST_QUIET_SECONDS = 0.25
"""判定"音箱的第一波取流结束了"所需的静默时长。

一次播放会连发三个请求（探测 / 定位到文件尾读标签 / 真正开始播），它们挤在
0.1 秒内。等这一波静下来，最后一个请求的时刻才是真正的播放起点——比
play_by_url 返回的时刻准得多，后者到出声之间有几秒缓冲。
"""

PLAYBACK_START_TIMEOUT = 15.0
"""等音箱开始取流的超时。

超时不是错误——取不到流的原因可能只是设备没连上，此时按"没开始播放"处理，
让流程继续走完，不要卡死。
"""

REPEAT_WATCH_SECONDS = 1.5
"""发完停止指令后，再盯这么久的取流（看设备是不是已经开始重播）。

停止指令要绕云端一圈，落地比音频结束晚一点时，设备已经把头重播了，用户听到
的就是"最后一句的开头又冒出来一下"。设备重播必然重新取流，所以这个窗口用来
接住那种情况：一旦又取流就补发一次停止。窗口本身是等待成本，别设大。
"""

REPEAT_POLL_SECONDS = 0.1
"""盯重播的轮询间隔。"""


@dataclass(frozen=True)
class DeviceProfile:
    """某型号小爱的行为差异。字段默认值即上游行为。"""

    status_poll: bool = True
    """player_get_status 报告的播放态是否可信。

    True  = 上游行为：按预估时长睡完后，轮询它直到 status 不再是 1。
    False = 该型号播完后 status 永远停在 1，上面那个轮询会死循环挂死，
            只能按预估时长等待，真正的停止交给 stop_playback()。
    """

    fallback_phrases: tuple[str, ...] = ()
    """该型号实测到的"小爱答不上来"话术词干。

    与 `config.fallback_answer_keyword` 合并使用（而不是二选一）：这里是实测
    得到的已知模板，用户配置用来补自己遇到的新模板。合到配置字段里、而不是
    在每个调用点各合一次，是为了避免某个调用点漏合。

    每个词干都必须落在 `fallback.PREFIX_WINDOW` 个字以内——实测的话术在
    「看来要更努力学习了」这种共用尾巴上各不相同，而尾巴超出了前缀窗口，
    所以取的是每套模板**开头**的辨识部分。
    """

    directive_command: str | None = None
    """执行文本指令的 MIoT 动作（"siid-aiid"）。

    None = 该型号不支持，停止回退到上游的 player_pause。

    这与 config.HARDWARE_COMMAND_DICT 里的 wakeup_command 是两回事：L05C 上
    两者恰好都是 "5-4"，但语义不同（唤醒 vs 执行指令），入参个数也不同
    （"5-3" Play Text 收 1 个文本参数，"5-4" Execute Text Directive 收
    文本 + 是否静默 两个）。合成一个字段就没法表达「某型号能唤醒但不支持
    执行文本指令」。
    """


DEFAULT_PROFILE = DeviceProfile()

DEVICE_PROFILES: dict[str, DeviceProfile] = {
    # 实测 2026-09-18，小爱音箱 Play 增强版（L05C，mi_did=502970287）：
    #   1. 走 player_play_music 拉 URL 后，按文件时长无限重播
    #      （5.5 秒的 mp3 约每 5.4 秒重新拉一轮）
    #   2. 播完后 status 永远停在 1（1=在放，2=没在放），从不回落，
    #      所以 wait_for_duration 的轮询会死循环
    #   3. player_pause / player_stop / miio 3-3 / miio 3-4 全部停不住
    #   4. 只有 MIoT siid5-aiid4（执行文本指令）能真正停下
    #
    # 注意第 1、4 条只在 tts/http.py 补上 Range 支持之后才观察得到——在那之前
    # 音箱压根进不了播放态，也就无从谈起"重播"与"停止"。
    #
    # 另外 7 个 _USE_PLAY_MUSIC_API 型号走同一条 player_play_music 路径，
    # 很可能同病，但**未实测**，因此不预先登记。
    #
    # fallback_phrases 是 2026-09-19 真机采到的 3 套兜底模板。取的是每套模板
    # 开头的辨识部分而不是共用的尾巴（「看来要更努力学习了」出现在其中两套里，
    # 但它超出了 PREFIX_WINDOW 个字）。固化下来是为了不依赖学习模式与分类器：
    # 删掉 learned_fallbacks.json 也不会退化，已知模板零 API 调用即刻生效。
    "L05C": DeviceProfile(
        status_poll=False,
        directive_command="5-4",
        fallback_phrases=(
            "被你问住了",
            "这可把我难住了",
            # 这条是 2026-09-19 第二次实测采到的："这个问题我暂时还回答不上，
            # 需要再学习一下"。**必须命中词干表**：没命中就要走一次 LLM 分类
            # （1~2 秒），等判定出来小爱早就把整句念完了——用户投诉的"接管了
            # 但她还是念完"有一半是这个原因。取"我暂时还回答不上"这种更短的
            # 辨识部分，"这个…"与"这个问题…"两种变体都能覆盖。
            "我暂时还回答不上",
        ),
    ),
}


_SENT_DIRECTIVES: set[str] = set()
"""我们通过"执行文本指令"发出去过的文本。

**这些指令会被小米的对话接口当成用户说的话记下来。** 于是它们会变成一条
新的 record，如果此时正处在持续对话或追问窗口里，就会被再转发给 AI ——
AI 答完又触发一次停止指令，又生成一条 record，**自我维持停不下来**。
所以发过的文本要登记下来，判定时把它们排除掉。
"""


def remember_directive(text: str) -> None:
    """登记一条自己发出的指令。凡是不走 execute_directive 的发送点都要调它。"""
    _SENT_DIRECTIVES.add(text.strip())


def is_own_directive(query: str) -> bool:
    """这个 query 是不是我们自己发出去的指令的回声。"""
    return query.strip() in _SENT_DIRECTIVES


def get_device_profile(hardware: str) -> DeviceProfile:
    """取某型号的行为差异。未登记的型号返回默认值，即上游行为。"""
    return DEVICE_PROFILES.get(hardware, DEFAULT_PROFILE)


async def execute_directive(
    miio_service: MiIOService, did: str, profile: DeviceProfile, text: str
) -> bool:
    """让音箱执行一句小爱文本指令。

    返回 False 表示该型号不支持这条通道，调用方应回退到上游做法。

    末尾的 "#0" 是 MIoT 动作签名的一部分（siid5-aiid4 的第二个入参：是否
    静默执行），不是用户偏好。**必须带 `#`**：miservice 的 string_or_value()
    只对 `#` 前缀的参数做类型转换，写成 ` 0` 传过去的是字符串 "0" 而不是
    整数 0。
    """
    if profile.directive_command is None:
        return False
    if " " in text:
        # miio_command 按空格切分入参，含空格的文本会被当成多个参数，在设备端
        # 静默变成参数个数错误。在这里早失败，别让它悄悄错。
        raise ValueError(f"指令文本不能含空格: {text!r}")
    # 先登记再发送：设备可能很快就把这条指令回报进对话记录
    remember_directive(text)
    await miio_command(miio_service, did, f"{profile.directive_command} {text} #0")
    return True
