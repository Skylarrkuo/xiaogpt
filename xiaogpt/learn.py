"""兜底话术的学习机制。

静态的 `fallback_answer_keyword` 只能覆盖人工枚举过的话术，而小爱的兜底话术
是个黑盒——实测拿到的是「这个我暂时还回答不上诶，我要再学习学习」，跟凭经验
猜的「还没学会」「没听懂」**完全不一样**。所以与其手工枚举，不如让它自己在
运行中认：学习模式下把小爱的回答交给 LLM 判断是否属于"答不上来"，判定结果
落盘，下次同样的回答直接查表，不再调 LLM。

三层递进，越靠前越便宜：

    1. 静态词干表（配置，人工维护）—— 子串 + 前缀窗口匹配
    2. 学习缓存（本模块的数据文件）—— 整句精确匹配，正负都记
    3. LLM 分类 —— 只在学习模式、且前两层都没命中时调一次

第 2 层按**归一化后的整句**精确匹配，而不是像第 1 层那样做子串：AI 从一句话
里"提炼"出的短语如果太短，会把正常回答也匹配掉，而这个失败模式的代价很大。
整句匹配最坏情况只是同义改写要多分类一次，有界且安全。
"""

from __future__ import annotations

import json
from pathlib import Path

from xiaogpt.fallback import MAX_FALLBACK_LEN, normalize_text

LEARNED_PATH = Path("learned_fallbacks.json")
"""学习结果落盘位置。相对当前工作目录，已加进 .gitignore。

它属于运行时数据而不是配置：人工维护的词干表留在 config.yaml，机器学到的东西
单独放，这样随时可以审阅、清空、或整个删掉重学。
"""

MAX_ENTRIES = 200
"""缓存上限。超出后按插入顺序丢弃最早的那些。

小爱的兜底话术来自有限几套模板，正常回答则千变万化——上限主要防的是后者
把文件撑大。
"""

CLASSIFY_PROMPT = """你是小爱音箱的对话分析器。下面是小爱音箱对用户提问的回答文本。

请判断这个回答是否属于「小爱回答不上来」的兜底话术——即她表示自己不会、
无法回答、不知道、需要再学习之类，没有真正回答用户的问题。

是 → 只回一个字：是
不是 → 只回一个字：否
（正常回答问题、闲聊、报时、报天气、讲笑话、执行指令都算「不是」）

不要解释，不要标点，只回一个字。

小爱的回答：
{answer}"""


def parse_verdict(reply: str) -> bool | None:
    """把模型的回答解析成布尔；认不出来返回 None。

    返回 None 时调用方应"不接管"——宁可漏判也不能把正常回答抢走。
    """
    if not isinstance(reply, str):
        return None
    head = reply.strip()[:4]
    # 先判否定：「不是」以「不」开头，若先判「是」会被误判成肯定
    if head.startswith(("否", "不", "no", "No", "NO")):
        return False
    if head.startswith(("是", "yes", "Yes", "YES")):
        return True
    return None


class FallbackLearner:
    """学习缓存的读写与 LLM 判定。

    bot 应当是一个**独立于主对话的实例**（`get_bot(config)` 再建一个），
    否则分类的问答会混进用户的对话历史里污染上下文。
    """

    def __init__(self, bot, path: Path | str = LEARNED_PATH) -> None:
        self.bot = bot
        self.path = Path(path)
        # 归一化后的整句 -> 是否兜底。dict 保序，便于按插入顺序淘汰。
        self.answers: dict[str, bool] = {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.answers = {
                    str(k): bool(v) for k, v in data.items() if isinstance(k, str)
                }
        except Exception as e:
            # 文件损坏不该阻断启动：丢掉重新学就是了
            print(f"[warn] could not read {self.path} ({e}), starting empty")

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self.answers, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[warn] could not write {self.path} ({e})")

    def lookup(self, text: str) -> bool | None:
        """已经判定过的直接返回；没判定过返回 None。"""
        return self.answers.get(normalize_text(text))

    def record(self, text: str, verdict: bool) -> None:
        key = normalize_text(text)
        if not key:
            return
        self.answers[key] = verdict
        while len(self.answers) > MAX_ENTRIES:
            self.answers.pop(next(iter(self.answers)))
        self.save()

    async def check(self, text: str) -> bool:
        """判定小爱这段话是不是「答不上来」。学习模式下才会调 LLM。"""
        known = self.lookup(text)
        if known is not None:
            return known
        if len(normalize_text(text)) > MAX_FALLBACK_LEN:
            # 长回答几乎不可能是兜底话术。这条闸门是算出来的，不是学到的，
            # 所以不落盘——否则每条长回答都会在文件里留一条。
            return False
        # 分类是"一次一句"的无状态任务：历史留着只会让每次请求越来越长，
        # 还会把上一次的判定当成上下文（前缀缓存也帮不上这种一次性提示词）
        self.bot.clear_history()
        reply = await self.bot.ask(CLASSIFY_PROMPT.format(answer=text))
        verdict = parse_verdict(reply)
        if verdict is None:
            print(f"[learn] could not parse classifier reply: {reply!r}")
            return False
        self.record(text, verdict)
        tag = "兜底" if verdict else "正常"
        short = text if len(text) <= 30 else text[:30] + "…"
        print(f"[learn] 判定为{tag}：{short!r}")
        if verdict:
            # 没命中静态词干表才会走到分类这里，而分类要 1~2 秒——接管是"越晚越
            # 听得到小爱念完"，所以顺手提示怎么把它固化下来（固化后零延迟）
            print(
                "[learn] 这次接管为此慢了一步；把这句话开头的辨识部分加进 "
                "fallback_answer_keyword 就能省掉它"
            )
        return verdict
