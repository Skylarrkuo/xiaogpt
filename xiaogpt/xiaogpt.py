#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import time
from pathlib import Path
from typing import AsyncIterator

from aiohttp import ClientSession, ClientTimeout
from miservice import MiAccount, MiIOService, MiNAService, miio_command
from rich import print
from rich.logging import RichHandler

from xiaogpt.bot import get_bot
from xiaogpt.config import (
    COOKIE_TEMPLATE,
    LATEST_ASK_API,
    WAKEUP_KEYWORD,
    Config,
)
from xiaogpt.device import (
    STOP_DIRECTIVE,
    execute_directive,
    is_own_directive,
    remember_directive,
)
from xiaogpt.fallback import is_fallback_answer, xiaoai_answer_text, xiaoai_answers
from xiaogpt.learn import FallbackLearner
from xiaogpt.tts import TTS, FileTTS, MiTTS
from xiaogpt.tts.doubao import STYLE_PROMPT, parse_style
from xiaogpt.utils import (
    calculate_tts_elapse,
    detect_language,
    parse_cookie_string,
)

EOF = object()

#: 等「这句话该怎么念」的结果最多等多久。超时就用配置里的默认值，
#: 不能让配音参谋拖慢回答——回答本身早就合成好了。
STYLE_WAIT_SECONDS = 2.0

#: 接管期间盯小爱的时间上限。回答的第一句通常 3~6 秒内就出声，那时我们会
#: 主动收手，所以这个值只是上限。
MUTE_WINDOW_SECONDS = 12.0
#: 内部循环节奏（纯本地 sleep，不发请求）
MUTE_POLL_SECONDS = 0.15
#: "她在不在播"是云端调用，前几秒密查、之后放慢
MUTE_FAST_SECONDS = 3.0
MUTE_CHECK_FAST = 0.3
MUTE_CHECK_SLOW = 1.0
#: 两次发停止指令之间的最小间隔
MUTE_RESEND_SECONDS = 0.5
#: L05C 真机上，停止请求返回后仍约需 1~2 秒才在设备执行。播放 AI 前必须
#: 等这条已进入云端的命令落地，否则本地取消任务也挡不住它迟到。
MUTE_COMMAND_SETTLE_SECONDS = 2.5

#: 压缩对话历史的提示词。产物会当"更早的对话摘要"长期留在 system 里，
#: 所以要求写事实、不写评论、不编造。
COMPACT_PROMPT = """请把下面这段对话压缩成一份简短的事实清单，供后续对话当背景。

要求：
- 只保留对后续有用的信息：用户是谁、偏好、正在做的事、未完成的事项、约定
- 用第三人称短句，一行一条，不要复述问答原文，不要加评论、不要编造
- 总长度不超过 {max_chars} 字
- 只输出清单本身，不要任何前言后语

【已有的摘要】（如果有，合并进来）
{previous}

【本次要压缩的对话】
{transcript}"""


class MiGPT:
    def __init__(self, config: Config):
        self.config = config

        self.mi_token_home = Path.home() / ".mi.token"
        self.last_timestamp = int(time.time() * 1000)  # timestamp last call mi speaker
        self.cookie_jar = None
        self.device_id = ""
        self.mina_service = None
        self.miio_service = None
        self.in_conversation = False
        # 追问窗口的截止时刻（time.monotonic）。0 表示没有窗口。
        # 与 in_conversation 走同一个路由分支，只是布尔来源不同：
        # in_conversation 是用户显式开关且永久，这个是隐式且会自动过期。
        self.ai_session_until = 0.0
        # 本轮的「按场景配音」任务（见 _plan_speech_style），以及用户是否用说的
        # 手动设过语音指令——手动设过就整场不再自动覆盖
        self._style_task: asyncio.Task | None = None
        # 接管期间持续掐小爱的任务（见 _keep_muting_xiaoai）
        self._mute_task: asyncio.Task | None = None
        # 每轮 AI 回答都有独立编号。静音任务只允许停止自己所属轮次、且必须在
        # 该轮取得播放权之前发送指令，防止上一轮迟到的停止命令误杀下一轮音频。
        self._turn_seq = 0
        self._mute_owner: int | None = None
        self._playback_owner: int | None = None
        self._manual_instruction = False
        # 本轮实际生效的场景配音（只用来在回答结束后打一行摘要，
        # 不能中途打印：那会把回答正文切断，看着像格式乱了）
        self._applied_style = ""
        # 上一轮对话已过期（窗口关了）时置位：下一句带一条"新一轮对话"标记，
        # 提醒模型别把跨小时的两件事硬接起来
        self._new_session = False
        # 后台压缩任务（一次压缩要一次模型调用，别卡住追问窗口）
        self._compact_task: asyncio.Task | None = None
        self.auth_failed = False
        self.polling_event = asyncio.Event()
        self.last_record = asyncio.Queue(1)
        # setup logger
        self.log = logging.getLogger("xiaogpt")
        self.log.setLevel(logging.DEBUG if config.verbose else logging.INFO)
        self.log.addHandler(RichHandler())
        self.log.debug(config)
        self.mi_session = ClientSession()

    async def close(self):
        await self.mi_session.close()

    async def poll_latest_ask(self):
        async with ClientSession() as session:
            session._cookie_jar = self.cookie_jar
            log_polling = int(self.config.verbose) > 1
            while True:
                if log_polling:
                    self.log.debug(
                        "Listening new message, timestamp: %s", self.last_timestamp
                    )
                new_record = await self.get_latest_ask_from_xiaoai(session)
                start = time.perf_counter()
                if log_polling:
                    self.log.debug(
                        "Polling_event, timestamp: %s %s",
                        self.last_timestamp,
                        new_record,
                    )
                await self.polling_event.wait()
                # 停止播放只能由主循环分配了 turn_id 后执行。轮询任务在这里抢先
                # 发命令没有播放所有权，云端迟到时会误杀随后开始的 AI 音频。
                if (d := time.perf_counter() - start) < 1:
                    # sleep to avoid too many request
                    if log_polling:
                        self.log.debug(
                            "Sleep %f, timestamp: %s", d, self.last_timestamp
                        )
                    # if you want force mute xiaoai, comment this line below.
                    await asyncio.sleep(1 - d)

    async def init_all_data(self):
        await self.login_miboy()
        await self._init_data_hardware()
        self.mi_session.cookie_jar.update_cookies(self.get_cookie())
        self.cookie_jar = self.mi_session.cookie_jar
        self.tts  # init tts

    async def login_miboy(self):
        account = MiAccount(
            self.mi_session,
            self.config.account,
            self.config.password,
            str(self.mi_token_home),
        )
        # Logs in with the passToken in ~/.mi_token (written by login_qr.py),
        # which mints a fresh serviceToken each time and needs no user input.
        await account.login("micoapi")
        self.mina_service = MiNAService(account)
        self.miio_service = MiIOService(account)

    async def _init_data_hardware(self):
        hardware_data = await self.mina_service.device_list()
        # fix multi xiaoai problems we check did first
        # why we use this way to fix?
        # some videos and articles already in the Internet
        # we do not want to change old way, so we check if miotDID in `env` first
        # to set device id

        for h in hardware_data:
            if did := self.config.mi_did:
                if h.get("miotDID", "") == str(did):
                    self.device_id = h.get("deviceID")
                    break
                else:
                    continue
            if h.get("hardware", "") == self.config.hardware:
                self.device_id = h.get("deviceID")
                break
        else:
            raise Exception(
                f"we have no hardware: {self.config.hardware} please use `micli mina` to check"
            )
        if not self.config.mi_did:
            devices = await self.miio_service.device_list()
            try:
                self.config.mi_did = next(
                    d["did"]
                    for d in devices
                    if d["model"].endswith(self.config.hardware.lower())
                )
            except StopIteration:
                raise Exception(
                    f"cannot find did for hardware: {self.config.hardware} "
                    "please set it via MI_DID env"
                )

    def get_cookie(self):
        # Built from ~/.mi_token, which the account login above has just refreshed.
        with open(self.mi_token_home) as f:
            user_data = json.loads(f.read())
        user_id = user_data.get("userId")
        service_token = user_data.get("micoapi")[1]
        cookie_string = COOKIE_TEMPLATE.format(
            device_id=self.device_id, service_token=service_token, user_id=user_id
        )
        return parse_cookie_string(cookie_string)

    @functools.cached_property
    def chatbot(self):
        return get_bot(self.config)

    @functools.cached_property
    def style_bot(self):
        """给「配音参谋」单独一个实例。

        不能复用主对话：让它带着「你是配音导演…」这种提示词进主历史，下一轮
        模型会把这套导演指令当上下文，回答就跑偏了。

        还必须静音：`ask()` 默认会把模型回复打到终端，而参谋回的是 JSON，
        混在回答流里看起来就像那段 JSON 被念出来了（其实只是打印）。
        """
        bot = get_bot(self.config)
        bot.quiet = True
        return bot

    @functools.cached_property
    def summarizer(self):
        """压缩对话历史用的实例：同样单独一份、静音、无状态。"""
        bot = get_bot(self.config)
        bot.quiet = True
        return bot

    async def _maybe_compact_history(self) -> None:
        """历史太长时把最老的一半压成摘要。

        为什么"攒着一次压掉"而不是每轮重写摘要：摘要放在 system 里，改写一次
        前缀就变一次，缓存全失效。所以只在超过预算时压缩，把最老的整批换掉，
        之后继续只追加。
        """
        budget = self.config.history_budget_chars
        bot = self.chatbot
        if budget <= 0 or bot.history_chars() <= budget:
            return
        old_turns = bot.oldest_turns(self.config.history_keep_turns)
        if not old_turns:
            return
        transcript = "\n".join(
            f"用户：{query}\nAI：{answer}" for query, answer in old_turns
        )
        prompt = COMPACT_PROMPT.format(
            max_chars=self.config.summary_max_chars,
            previous=bot.summary or "（无）",
            transcript=transcript,
        )
        try:
            summarizer = self.summarizer
            summarizer.clear_history()  # 压缩本身也必须无状态
            summary = await summarizer.ask(prompt)
        except Exception as e:  # noqa: BLE001  压缩失败不影响对话，下轮再试
            self.log.warning("压缩对话历史失败（%s），下轮再试", type(e).__name__)
            return
        if not summary or not summary.strip():
            return
        bot.apply_compaction(len(old_turns), summary)
        print(
            f"对话记忆：最老 {len(old_turns)} 轮压成 {len(summary)} 字摘要，"
            f"当前历史 {bot.history_chars()} 字"
        )

    def _schedule_compaction(self) -> None:
        """后台压缩：一次压缩要一次模型调用，别卡在"回答完毕"这里。"""
        if self.config.history_budget_chars <= 0:
            return
        if self._compact_task is not None and not self._compact_task.done():
            return
        self._compact_task = asyncio.create_task(self._maybe_compact_history())

    @property
    def in_follow_up(self) -> bool:
        """AI 刚答完，还处在"可以接着追问"的窗口里。"""
        return time.monotonic() < self.ai_session_until

    def _match_trigger(self, query: str) -> str | None:
        """返回命中的触发词（原样大小写），未命中返回 None。"""
        lowered = query.lower()
        for word in self.config.keyword:
            if lowered.startswith(word.lower()):
                return word
        return None

    def _strip_trigger_word(self, query: str) -> str:
        """剥掉开头的触发词。

        与 _match_trigger 共用同一套比较：原先判定用 .lower() 而剥词用的正则
        大小写敏感，`HEY` 之类能触发却剥不掉，会把触发词原样喂给 AI。
        按长度切片也顺带免掉了 re.escape —— 用户把 `C++` 写进 keyword 时，
        未转义的正则会让 re.sub 直接抛异常。
        """
        word = self._match_trigger(query)
        return query[len(word) :] if word else query

    def _is_forced_ai_route(self, record) -> bool:
        """无需查看小爱回答就能确定要交给 AI 的路径。"""
        if not record:
            return False
        query = record.get("query", "")
        if is_own_directive(query):
            return False
        if self._match_trigger(query) is not None:
            return True
        if query.startswith(WAKEUP_KEYWORD) and not self.in_follow_up:
            return False
        return self.in_conversation or self.in_follow_up

    async def _resolve_ai_route(self, record) -> tuple[bool, bool]:
        """返回（是否交给 AI，是否由兜底话术触发）。

        触发词、持续对话和追问本来就确定走 AI，不能再等一次兜底分类；否则
        白白多出 1~2 秒，小爱已经把自己的回答念出来了。
        """
        if not record or is_own_directive(record.get("query", "")):
            return False, False
        if self._is_forced_ai_route(record):
            return True, False
        query = record.get("query", "")
        if query.startswith(WAKEUP_KEYWORD) and not self.in_follow_up:
            return False, False
        fallback_takeover = await self.resolve_fallback_verdict(record)
        return fallback_takeover, fallback_takeover

    @property
    def fallback_enabled(self) -> bool:
        return bool(self.config.fallback_answer_keyword) or self.config.learn_fallback

    @functools.cached_property
    def learner(self) -> FallbackLearner | None:
        """学习模式的判定器。没开学习模式就是 None，不会产生任何 API 调用。

        bot 用**独立实例**（get_bot 再建一个），这样分类的问答不会混进用户的
        对话历史里污染上下文。
        """
        if not self.config.learn_fallback:
            return None
        return FallbackLearner(get_bot(self.config))

    async def resolve_fallback_verdict(self, record) -> bool:
        """判定这条记录是不是“小爱答不上来”。

        空回答一律不算兜底 —— 实测执行类指令（“停止播放”）的 answers 就是
        空的，把它当兜底会把开灯、放歌、设闹钟
        全都抢走。
        """
        if not self.fallback_enabled:
            return False
        text = xiaoai_answer_text(record)
        if not text.strip():
            return False
        if is_fallback_answer(text, self.config.fallback_answer_keyword):
            self.log.debug("小爱答不上来（命中词干表）: %r", text)
            verdict = True
        elif self.learner is not None:
            verdict = await self.learner.check(text)
        else:
            verdict = False
        return verdict

    def need_change_prompt(self, record):
        query = record.get("query", "")
        return query.startswith(tuple(self.config.change_prompt_keyword))

    def _change_prompt(self, new_prompt):
        new_prompt = re.sub(
            rf"^({'|'.join(self.config.change_prompt_keyword)})", "", new_prompt
        )
        new_prompt = "以下都" + new_prompt
        print(f"Prompt from {self.config.prompt} change to {new_prompt}")
        self.config.prompt = new_prompt
        self.chatbot.change_prompt(new_prompt)

    def need_change_tts_instruction(self, record) -> bool:
        """这句是不是「语音指令 xxx」。"""
        if not self.config.change_tts_instruction_keyword:
            return False
        return record.get("query", "").startswith(
            tuple(self.config.change_tts_instruction_keyword)
        )

    async def _change_tts_instruction(self, query: str) -> None:
        """用说话的方式改 TTS 的语音指令（情绪 / 方言 / 语气）。

        只对支持的 TTS 生效（现在只有豆包的部分音色），不支持的型号明确说
        "不支持"——答应了却什么都不变比不答应更糟。指令只存在内存里，重启后
        回到配置文件里的值。
        """
        word = next(
            (
                w
                for w in self.config.change_tts_instruction_keyword
                if query.startswith(w)
            ),
            "",
        )
        instruction = query[len(word) :].strip()
        if not instruction:
            self.log.warning("语音指令后面没内容，忽略")
            return
        if self.config.mute_xiaoai:
            # 与提问路径一致：先把小爱自己的回答掐掉，再用音箱说确认语
            await self.stop_if_xiaoai_is_playing()
        if self.tts.set_instruction(instruction):
            # 手动设过就是这个会话的基准，之后的自动配音不再覆盖它
            self._manual_instruction = True
            print(f"语音指令改为：{instruction}")
            await self.do_tts("好的")
        else:
            self.log.warning("当前 TTS（%s）不支持语音指令", self.config.tts)
            await self.do_tts("当前音色还不支持语音指令")

    async def get_latest_ask_from_xiaoai(self, session: ClientSession) -> dict | None:
        retries = 3
        for i in range(retries):
            try:
                timeout = ClientTimeout(total=15)
                r = await session.get(
                    LATEST_ASK_API.format(
                        hardware=self.config.hardware,
                        timestamp=str(int(time.time() * 1000)),
                    ),
                    timeout=timeout,
                )
            except Exception as e:
                self.log.warning(
                    "Execption when get latest ask from xiaoai: %s", str(e)
                )
                continue
            if r.status != 200:
                body = await r.text()
                if r.status in (401, 403):
                    # Re-initing re-runs the account login, which mints a fresh
                    # serviceToken from the passToken in ~/.mi_token.
                    if not self.auth_failed:
                        self.auth_failed = True
                        self.log.warning(
                            "Auth error (HTTP %s), refreshing the serviceToken "
                            "via %s...",
                            r.status,
                            self.mi_token_home,
                        )
                    try:
                        await self._retry()
                    except Exception as e:
                        self.log.error(
                            "Refresh failed: %s. Run `python login_qr.py` and "
                            "scan with the Mi Home app to re-authenticate.",
                            e,
                        )
                    # Back off instead of hammering the login endpoint.
                    await asyncio.sleep(30)
                    return None
                self.log.warning(
                    "get latest ask from xiaoai error: HTTP %s %s",
                    r.status,
                    body[:200],
                )
                if i == 1:
                    # tricky way to fix #282 #272 # if it is the third time we re init all data
                    print("Maybe outof date trying to re init it")
                    await self._retry()
                continue
            try:
                data = await r.json()
            except Exception:
                self.log.warning("get latest ask from xiaoai error, retry")
                if i == 1:
                    # tricky way to fix #282 #272 # if it is the third time we re init all data
                    print("Maybe outof date trying to re init it")
                    await self._retry()
            else:
                self.auth_failed = False
                return self._get_last_query(data)
        return None

    async def _retry(self):
        await self.init_all_data()

    def _get_last_query(self, data: dict) -> dict | None:
        if d := data.get("data"):
            records = json.loads(d).get("records")
            if not records:
                return None
            last_record = records[0]
            timestamp = last_record.get("time")
            if timestamp > self.last_timestamp:
                if is_own_directive(last_record.get("query", "")):
                    # ask_gpt 把“last_record 队列非空”当作用户插话并立即中断，
                    # 因此内部指令必须在入队之前过滤，主循环里再过滤已经太晚。
                    self.last_timestamp = timestamp
                    self.log.debug(
                        "入队前忽略自己发出的指令回声: %r",
                        last_record.get("query", ""),
                    )
                    return None
                try:
                    self.last_record.put_nowait(last_record)
                    self.last_timestamp = timestamp
                    return last_record
                except asyncio.QueueFull:
                    pass
        return None

    async def do_tts(self, value):
        if not self.config.use_command:
            try:
                await self.mina_service.text_to_speech(self.device_id, value)
            except Exception:
                pass
        else:
            await miio_command(
                self.miio_service,
                self.config.mi_did,
                f"{self.config.tts_command} {value}",
            )

    def _style_enabled(self) -> bool:
        """要不要为这句话自动决定「怎么说」。"""
        return (
            self.config.tts_auto_instruction
            and self.config.tts == "doubao"
            and not self._manual_instruction
        )

    async def _plan_speech_style(self, query: str) -> dict | None:
        """让 AI 判断这句话该怎么念（语气 / 语速 / 音量 / 方言）。

        失败一律返回 None：配音是锦上添花，不能因为它把回答卡住。
        """
        try:
            style_bot = self.style_bot
            # 参谋是无状态的：上一句的场景不该影响这一句
            style_bot.clear_history()
            reply = await style_bot.ask(STYLE_PROMPT.format(query=query))
        except Exception as e:  # noqa: BLE001
            self.log.warning("生成说话方式失败（%s），用默认值", type(e).__name__)
            return None
        style = parse_style(reply or "")
        if style is None:
            self.log.warning("说话方式解析失败，用默认值：%r", (reply or "")[:80])
            return None
        return style

    async def _apply_pending_style(self) -> None:
        """把本轮的说话方式交给 TTS；没有就回到配置里的默认值。"""
        task, self._style_task = self._style_task, None
        if task is None:
            return
        try:
            style = await asyncio.wait_for(task, timeout=STYLE_WAIT_SECONDS)
        except Exception:  # noqa: BLE001  超时/报错都按"没有风格"处理
            style = None
        if style and self.tts.set_style(style):
            numbers = "，".join(
                f"{key}={value}" for key, value in style.items() if key != "instruction"
            )
            self._applied_style = f"{style.get('instruction', '')}（{numbers}）"
        else:
            self.tts.reset_style()
            self._applied_style = ""

    def _discard_style_task(self) -> None:
        """这一轮没走到 TTS（没回答 / 报错），别把任务留在后台。"""
        if self._style_task is not None and not self._style_task.done():
            self._style_task.cancel()
        self._style_task = None

    @functools.cached_property
    def tts(self) -> TTS:
        if self.config.tts == "mi":
            return MiTTS(self.mina_service, self.device_id, self.config)
        # edge 与 doubao 都走文件模式：先落地音频再让音箱来拉，L05C 上验证过
        return FileTTS(self.mina_service, self.device_id, self.config)

    @staticmethod
    def _normalize(message: str) -> str:
        message = message.strip().replace(" ", "--")
        message = message.replace("\n", "，")
        message = message.replace('"', "，")
        message = message.replace("*", "")
        return message

    async def ask_gpt(self, query: str) -> AsyncIterator[str]:
        if not self.config.stream:
            answer = await self.chatbot.ask(query, **self.config.gpt_options)
            message = self._normalize(answer) if answer else ""
            yield message
            return

        async def collect_stream(queue):
            async for message in self.chatbot.ask_stream(
                query, **self.config.gpt_options
            ):
                await queue.put(message)

        def done_callback(future):
            queue.put_nowait(EOF)
            # 必须先判 cancelled：任务被 cancel 后再调 exception() 会抛
            # CancelledError，从回调里冒出来变成一大段 "Exception in callback"
            # 噪音（ask_gpt 结尾正常会 cancel 它）。
            if not future.cancelled() and future.exception():
                self.log.error(future.exception())

        self.polling_event.set()
        queue = asyncio.Queue()
        is_eof = False
        task = asyncio.create_task(collect_stream(queue))
        task.add_done_callback(done_callback)
        try:
            while True:
                if is_eof or not self.last_record.empty():
                    break
                message = await queue.get()
                if message is EOF:
                    break
                while not queue.empty():
                    if (next_msg := queue.get_nowait()) is EOF:
                        is_eof = True
                        break
                    message += next_msg
                if message:
                    yield self._normalize(message)
        finally:
            # FileTTS 被取消或消费者提前结束时，也必须回收上游流任务；否则上一轮
            # 仍可能在后台继续收 token，并与下一轮的状态/日志交错。
            self.polling_event.clear()
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # done_callback 已记录原始错误；这里的职责只是取回任务异常。
                pass

    async def get_if_xiaoai_is_playing(self):
        playing_info = await self.mina_service.player_get_status(self.device_id)
        # WTF xiaomi api
        is_playing = (
            json.loads(playing_info.get("data", {}).get("info", "{}")).get("status", -1)
            == 1
        )
        return is_playing

    async def _wait_for_xiaoai_fallback(self, record) -> None:
        """接管时等小爱把兜底话术说完。

        mute_xiaoai 关闭时上游写死 sleep(8)。但接管路径上我们**已经拿到了**
        兜底话术的原文，按它自己的时长等就够——用户听到的是"道歉 → 安静一两秒
        → 回答"，而不是"道歉 → 沉默 8 秒 → 回答"。上限仍取 8 秒兜底。
        """
        await asyncio.sleep(
            min(8.0, calculate_tts_elapse(xiaoai_answer_text(record)) + 0.5)
        )

    def _open_follow_up(self) -> None:
        seconds = self.config.follow_up_seconds
        if seconds > 0:
            self.ai_session_until = time.monotonic() + seconds
            # 措辞要准：窗口管的是"这句要不要给 AI"，管不了麦克风——音箱不唤醒
            # 就不记录，除非设备自己开着连续对话。写"不用唤醒词"会让人以为说完
            # 音箱就会听，实际什么都不会发生。
            print(
                f"追问窗口开启：{seconds} 秒内接着问就行（不用触发词；"
                "但还是要先叫「小爱同学」唤醒设备）"
            )

    def _maybe_expire_follow_up(self) -> None:
        """窗口过期只关掉"追问路由"，**不动历史**。

        历史要留着（用户要的就是"别忘上文"），但跨小时的两段对话确实没关系，
        模型容易自己脑补连续性。所以改成给下一句**追加**一条"新一轮对话"的标记：
        标记加在用户消息上、不动前面的前缀，前缀缓存照样命中。
        """
        if self.ai_session_until and not self.in_follow_up:
            self.ai_session_until = 0.0
            self._new_session = True

    def _apply_session_marker(self, query: str) -> str:
        """上一轮对话已过期时，给这一句加上「新一轮对话」的标记。

        标记**追加**在用户消息里，不动前面的 messages 前缀，所以缓存照样命中；
        模型看到它就知道别把跨小时的两件事硬接起来。
        """
        if not self._new_session:
            return query
        self._new_session = False
        print("（上一轮已过去，这句带上「新一轮对话」标记）")
        return f"（新一轮对话，之前的内容只当背景）{query}"

    async def send_directive(self, text: str) -> bool:
        """让音箱执行一句文本指令。返回 False 表示该型号不支持这条通道。"""
        return await execute_directive(
            self.miio_service, self.config.mi_did, self.config.device_profile, text
        )

    async def stop_if_xiaoai_is_playing(self):
        is_playing = await self.get_if_xiaoai_is_playing()
        if not is_playing:
            return
        self.log.debug("Muting xiaoai")
        # player_pause 在 L05C 上实测无效，而且会触发音频重新拉流，所以登记了
        # 指令通道的型号改走 MIoT 文本指令。
        if await self.send_directive(STOP_DIRECTIVE):
            return
        # stop it
        await self.mina_service.player_pause(self.device_id)

    def _next_turn_id(self) -> int:
        self._turn_seq += 1
        return self._turn_seq

    async def _start_muting_xiaoai(self, turn_id: int) -> None:
        """把静音权交给本轮，并等上一轮静音任务完全退出。"""
        await self._stop_muting_xiaoai()
        self._mute_owner = turn_id
        self._playback_owner = None
        self._mute_task = asyncio.create_task(self._keep_muting_xiaoai(turn_id))

    async def _keep_muting_xiaoai(self, turn_id: int) -> None:
        """接管期间尽快掐掉小爱，直到我们要开始播自己的音频为止。

        为什么要盯着而不是查一次：**记录比声音先到**。对话记录里 answers 已经
        填好（我们据此判定"她答不上来"）时，她往往还没开口，单次检查必然扑空；
        等她真的开口，人已经听完了。

        为了"瞬间"而不是"过一会儿"，这里做三件事：

        1. **开火不等**：一上来先发一次停止指令。此刻她多半还没开口，指令的
           云端往返正好落在她刚出声那一刻——这是最要紧的一枪。
        2. **前 3 秒密查**（0.3 秒一次，之后放慢到 1 秒）：她一在播就立刻补一枪。
        3. 两次发送之间留 0.5 秒，别把 MIoT 打成刷屏。
        """
        if not self.config.device_profile.status_poll:
            # L05C 不会让新 URL 抢占原生回答，只会排到它后面。唯一有效的停止
            # 又要经云端约 1~2 秒。只发一条停止并等它彻底落地；重复发送只会
            # 制造更多可能在 AI 开播后才到达的迟到命令。
            try:
                await self._mute_once(turn_id)
                await asyncio.sleep(MUTE_COMMAND_SETTLE_SECONDS)
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001  失败才退回旧循环
                self.log.debug("单次停止接管失败，回退停止循环：%s", error)

        start = time.monotonic()
        deadline = start + MUTE_WINDOW_SECONDS
        try:
            await self._mute_once(turn_id)  # 1. 不等检查，先开一枪
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  第一枪失败也要继续检查
            self.log.debug("首次掐断小爱时出错：%s", e)
        last_sent = time.monotonic()
        last_check = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            interval = (
                MUTE_CHECK_FAST if now - start < MUTE_FAST_SECONDS else MUTE_CHECK_SLOW
            )
            if now - last_check >= interval:
                last_check = now
                try:
                    # L05C 的播放态播完不回落（device.py 里 status_poll=False），
                    # 所以这里只做"要不要补枪"的门槛，别指望它变 False 就收手
                    if (
                        await self.get_if_xiaoai_is_playing()
                        and now - last_sent >= MUTE_RESEND_SECONDS
                    ):
                        await self._mute_once(turn_id)
                        last_sent = time.monotonic()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001  掐不断也不能影响回答
                    self.log.debug("掐断小爱时出错：%s", e)
            await asyncio.sleep(MUTE_POLL_SECONDS)

    async def _mute_once(self, turn_id: int) -> None:
        """发一次"停止播放"。指令通道不可用的型号回退到 player_pause。"""
        if self._mute_owner != turn_id or self._playback_owner == turn_id:
            return
        if await self.send_directive(STOP_DIRECTIVE):
            self.log.debug("掐断小爱的回答")
            return
        await self.mina_service.player_pause(self.device_id)

    async def _stop_muting_xiaoai(
        self, turn_id: int | None = None, *, claim_playback: bool = False
    ) -> None:
        """撤销静音权并等待任务真正退出，不留下迟到的本地发送。"""
        if turn_id is not None and self._mute_owner not in (None, turn_id):
            return
        task = self._mute_task
        self._mute_task = None
        if task is not None:
            # L05C 的任务包含“等待云端停止命令落地 + 恢复音量”。准备播放 AI
            # 时必须等它自然结束，不能 cancel；其它型号仍立即取消轮询。
            wait_for_settle = (
                claim_playback and not self.config.device_profile.status_poll
            )
            if not task.done() and not wait_for_settle:
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except (
                Exception
            ) as error:  # noqa: BLE001  取回后台异常，避免 never retrieved
                self.log.debug("静音任务结束时出错：%s", error)
        self._mute_owner = None
        if claim_playback and turn_id is not None:
            self._playback_owner = turn_id

    def _release_playback(self, turn_id: int) -> None:
        if self._playback_owner == turn_id:
            self._playback_owner = None

    async def wakeup_xiaoai(self):
        # 末尾必须是 "#0" 而不是 "0"：miservice 只对 `#` 前缀的参数做类型转换，
        # 写成 " 0" 传过去的是字符串，而这个入参是整数（是否静默执行）。
        #
        # 这里走的也是"执行文本指令"，所以同样会出现在对话记录里；不登记的话，
        # 持续对话叠加追问窗口时「小爱同学」会被当成新提问转给 AI，形成自激。
        remember_directive(WAKEUP_KEYWORD)
        return await miio_command(
            self.miio_service,
            self.config.mi_did,
            f"{self.config.wakeup_command} {WAKEUP_KEYWORD} #0",
        )

    async def run_forever(self):
        await self.init_all_data()
        # 启动自检（如模型名、凭据），失败则直接退出而不是让音箱沉默
        self.chatbot.validate()
        # 提示词走 system（不再拼进第一条用户消息），前缀才稳得住、缓存才命中
        self.chatbot.change_prompt(self.config.prompt)
        task = asyncio.create_task(self.poll_latest_ask())
        assert task is not None  # to keep the reference to task, do not remove this
        print(
            f"Running xiaogpt now, 用 [green]{'/'.join(self.config.keyword)}[/] 开头来提问"
        )
        print(f"或用 [green]{self.config.start_conversation}[/] 开始持续对话")
        while True:
            self.polling_event.set()
            new_record = await self.last_record.get()
            self.polling_event.clear()  # stop polling when processing the question
            query = new_record.get("query", "").strip()
            if self.config.verbose:
                # 校准兜底话术用：每条记录无条件 dump，包括不会被接管的那些。
                # 放在任何判定之前，才能看到"小爱答得好"的负样本长什么样。
                answers = xiaoai_answers(new_record)
                # 方括号要转义：本模块的 print 是 rich 的，会把 [record] 当成
                # 样式标记吃掉，日志里只剩 query=... 看不出这是哪来的
                print(
                    f"\\[record] query={query!r} "
                    f"answer={xiaoai_answer_text(new_record)!r} "
                    f"n_answers={len(answers)} "
                    f"types={[a.get('type') for a in answers]}"
                )
            if query == self.config.start_conversation:
                if not self.in_conversation:
                    print("开始对话")
                    self.in_conversation = True
                    await self.wakeup_xiaoai()
                await self.stop_if_xiaoai_is_playing()
                continue
            elif query == self.config.end_conversation:
                if self.in_conversation:
                    print("结束对话")
                    self.in_conversation = False
                await self.stop_if_xiaoai_is_playing()
                continue

            # we can change prompt
            if self.need_change_prompt(new_record):
                print(new_record)
                self._change_prompt(new_record.get("query", ""))

            # 改说话方式（情绪 / 方言 / 语气）：这是给 TTS 的指令，不是问题，
            # 所以处理完直接跳过，不送给 AI
            if self.need_change_tts_instruction(new_record):
                await self._change_tts_instruction(query)
                continue

            # “交给 AI”与“命中兜底话术”是两个概念。触发词/持续对话/追问
            # 已经确定走 AI，不再为它们额外等待一次兜底分类。
            route_to_ai, fallback_takeover = await self._resolve_ai_route(new_record)
            if not route_to_ai:
                self.log.debug("No new xiao ai record")
                continue

            if self.in_follow_up and self._match_trigger(query) is None:
                # 让"窗口到底有没有生效"看得见：能看到这行说明设备把话记下来了，
                # 看不到就是它根本没听（没唤醒 / 连续对话没开）
                print("追问窗口内：这句直接交给 AI")

            # drop key words
            query = self._strip_trigger_word(query).strip()

            self._maybe_expire_follow_up()

            print("-" * 20)
            print("问题：" + query + "？")
            query = self._apply_session_marker(query)
            # 配音参谋只看用户原话：下面会把 prompt 拼进 query，那是给回答用的
            # 约束（"严禁 Markdown…"），喂给参谋会把它带偏
            style_query = query

            turn_id = self._next_turn_id()
            if self.config.mute_xiaoai:
                # 所有 AI 路径都持续截断。过去只有兜底接管走这里，触发词与追问
                # 仍只查一次播放状态，记录早于声音时必然扑空。
                await self._start_muting_xiaoai(turn_id)
            elif fallback_takeover:
                await self._wait_for_xiaoai_fallback(new_record)
            else:
                # waiting for xiaoai speaker done
                await asyncio.sleep(8)
            if not fallback_takeover and not self.config.mute_xiaoai:
                # 接管时用户刚听完小爱说"我暂时还回答不上"，再听一句
                # "正在问 XX 请耐心等待"是第二次打断，跳过。
                # 持续静音开启时也不能播放这句，否则静音任务会把它一起掐掉。
                await self.do_tts(f"正在问{self.chatbot.name}请耐心等待")
            print(
                "以下是小爱的回答：",
                xiaoai_answer_text(new_record) or "（没有回答文本）",
            )
            print(f"以下是 {self.chatbot.name} 的回答：", end="")
            # 配音参谋和回答并行跑：等回答的第一句出来时它多半已经好了，
            # 于是既不影响首句延迟，又能按这句话的场景调整语速语气
            self._applied_style = ""
            if self._style_enabled():
                self._style_task = asyncio.create_task(
                    self._plan_speech_style(style_query)
                )
            try:
                await self.speak(self.ask_gpt(query), turn_id)
            except StopAsyncIteration:
                # 流里一个字都没有。常见原因：模型没返回内容，或流式过程中
                # 又来了一条新记录导致提前 break。原先这里只会打出
                # "回答出错 "（str(StopAsyncIteration()) 是空串），看不出原因。
                print(
                    f"{self.chatbot.name} 没有返回任何内容"
                    "（可能被新提问打断，或上游返回为空）"
                )
            except Exception as e:
                print(f"{self.chatbot.name} 回答出错 {type(e).__name__}: {e}")
            else:
                # 场景配音放在这里说：中途打印会把回答正文切断，看着像格式乱了
                if self._applied_style:
                    print(f"回答完毕（按场景配音：{self._applied_style}）")
                else:
                    print("回答完毕")
                # 窗口必须在 speak() 返回之后才开，不能在提问时开：300 字的回答
                # 要播一分钟以上，按提问时刻起算窗口必然提前过期。
                self._open_follow_up()
                # 历史超预算就后台压缩（不影响这一轮的收尾）
                self._schedule_compaction()
            finally:
                # 走到 TTS 的路径上已经在 speak() 里消费掉了，这里是兜底：
                # 没回答 / 报错时别把这个任务留在后台
                self._discard_style_task()
                await self._stop_muting_xiaoai(turn_id)
            if self.in_conversation:
                print(f"继续对话，或用 `{self.config.end_conversation}` 结束对话")
                await self.wakeup_xiaoai()

    async def speak(self, text_stream: AsyncIterator[str], turn_id: int) -> None:
        first_chunk = await text_stream.__anext__()
        # Detect the language from the first chunk
        # Add suffix '-' because tetos expects it to exist when selecting voices
        # however, the nation code is never used.
        lang = detect_language(first_chunk) + "-"

        async def gen():  # reconstruct the generator
            yield first_chunk
            async for text in text_stream:
                yield text

        # 已经拿到第一句了，这里再等一小会儿配音参谋的结果（见 _plan_speech_style）
        await self._apply_pending_style()
        # 要出声了：先取得本轮播放权，再等待静音任务完全退出。旧轮次即使晚醒，
        # 也会因所有权不匹配而无法再发停止命令。
        await self._stop_muting_xiaoai(turn_id, claim_playback=True)
        try:
            await self.tts.synthesize(lang, gen())
        finally:
            self._release_playback(turn_id)
