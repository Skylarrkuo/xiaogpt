"""兜底接管：识别「小爱自己没答上来」的回答。

上游没有对应物。判定用的全是**小爱回答侧**的文本，与用户说了什么无关——
用户正常跟音箱说话，小爱会的她自己答，答不上来的由这里认出来交给 AI。

设计上全部往「宁可漏判、不可误判」偏：把小爱的正常回答抢给 AI 是灾难性的，
漏判只是退回用触发词。因此有三道保守化：

1. 只在回答开头的 PREFIX_WINDOW 个字里找，不做全文匹配——「还没学会」完全
   可能出现在正常回答里（"讲个机器人的故事" → "机器人说：抱歉，我还没学会"）
2. 短语长度闸门，单字短语会把所有回答都判成兜底（见 check_phrases）
3. 回答为空/缺失一律不接管（见 is_fallback_answer 的调用方）

本模块是纯函数 + 常量，不 import config、不做 I/O，这样能一行 REPL 离线校准：

    python -c "from xiaogpt.fallback import is_fallback_answer as f; \\
      p=('还没学会','没听懂'); print(f('抱歉，我还没学会', p), f('今天晴', p))"
"""

from __future__ import annotations

import re
from typing import Any, Iterable

FALLBACK_ANSWER_KEYWORD: tuple[str, ...] = ()
"""小爱答不上来时常见的回答片段。默认空 = 不启用兜底接管（行为与上游一致）。

填的必须是**小爱的回答**里的片段，不是用户说的话。
"""

PREFIX_WINDOW = 12
"""只在归一化后回答的前这么多个字里做匹配。

兜底话术都以道歉/示弱开头，而同样的词出现在后半句往往是正常回答的一部分。
这个窗口是最便宜的一根「用漏判换不误判」的杠杆。
"""

MIN_PHRASE_LEN = 2
"""短于这个长度的短语直接报错。

把「我」写进去会让**每一条**回答都命中，于是所有话都被转给 AI —— 静默且
灾难性，所以宁可直接拒绝启动。
"""

WARN_PHRASE_LEN = 4
"""短于这个长度（但不短于 MIN_PHRASE_LEN）只警告。

「没听懂」只有三个字却是真实可用的词干，不能硬拦。
"""

MAX_FALLBACK_LEN = 40
"""超过这个长度就认为是正常回答，连 AI 都不用问。

实测的兜底话术是「这个我暂时还回答不上诶，我要再学习学习」（19 字），都是短句。
这条闸门既省 API 调用，更重要的是挡住 AI 把一段长回答误判成兜底——那意味着
一个本来答得好好的问题被抢走，是这套机制里最糟的失败模式。
"""

_PUNCTUATION_RE = re.compile(r"[\s，。！？、,.!?；;：:~…—\-「」『』（）()\[\]【】]+")


def normalize_text(text: str) -> str:
    """去掉空白与中英标点，让「抱歉，我还没学会这个技能。」能对上「抱歉我还没学会」。"""
    return _PUNCTUATION_RE.sub("", text)


def normalize_phrases(value: Any) -> tuple[str, ...]:
    """把用户配置归一成可用的短语元组。

    裸字符串要包成列表：Python 会逐字符遍历字符串，写 `"还没学会"` 会变成
    `("还","没","学","会")` —— 和 `keyword` 在 config.read_from_file 里被特判
    是同一个坑。
    """
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    phrases = []
    for item in value:
        text = normalize_text(str(item))
        if text:
            phrases.append(text)
    return tuple(phrases)


def check_phrases(phrases: Iterable[str]) -> None:
    """长度闸门。由 Config.__post_init__ 调用，尽早拦下危险配置。

    报错信息刻意用 ASCII：本机控制台编码会把中文变成乱码，而一个看不懂的
    报错等于没有报错（同 openai_compat_bot.validate 的理由）。
    """
    for phrase in phrases:
        if len(phrase) < MIN_PHRASE_LEN:
            raise ValueError(
                f"fallback_answer_keyword entry {phrase!r} is too short "
                f"(min {MIN_PHRASE_LEN} chars). A short phrase like '我' would "
                "match every answer and hand all of them to the AI. "
                "Remove it or use a longer fragment."
            )
        if len(phrase) < WARN_PHRASE_LEN:
            print(
                f"[warn] fallback_answer_keyword entry {phrase!r} is very short "
                f"(< {WARN_PHRASE_LEN} chars) and may match ordinary answers."
            )


def xiaoai_answers(record: dict | None) -> list[dict]:
    """安全取出 record 的 answers 列表；结构不符一律返回 []。

    answers 的结构没有契约。实测/上游代码里存在 answers 为 None、
    answers[0].tts 为 None 两种情形，而 xiaogpt.py 原先那段 print 只 catch 了
    IndexError，这两种会以 TypeError / AttributeError 冒出 run_forever，
    把整个主循环打断（cli.py 里没有兜底捕获）。
    """
    if not isinstance(record, dict):
        return []
    answers = record.get("answers")
    if not isinstance(answers, list):
        return []
    return [a for a in answers if isinstance(a, dict)]


def xiaoai_answer_text(record: dict | None) -> str:
    """取小爱这轮回答的第一段 TTS 文本。

    没有文本时返回空串——放歌、执行指令（开灯/设闹钟）这类记录就是没有文本，
    而它们与"她没出声"在数据层面无法区分，所以调用方不能把空串当成兜底。

    判定与打印必须共用这一个函数：record 的结构知识只该有一份定义，
    否则迟早分叉。
    """
    for answer in xiaoai_answers(record):
        tts = answer.get("tts")
        if isinstance(tts, dict) and isinstance(tts.get("text"), str):
            return tts["text"]
    return ""


def matched_phrase(text: str, phrases: Iterable[str]) -> str | None:
    """命中则返回命中的短语（便于把判定原因写进日志），未命中返回 None。"""
    if not text:
        return None
    head = normalize_text(text)[:PREFIX_WINDOW]
    for phrase in phrases:
        if phrase in head:
            return phrase
    return None


def is_fallback_answer(text: str, phrases: Iterable[str]) -> bool:
    """小爱这段话是不是「没答上来」。

    注意空文本返回 False —— 由调用方保证不把"没有回答"当成兜底。
    """
    return matched_phrase(text, phrases) is not None
