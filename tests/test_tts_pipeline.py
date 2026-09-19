import asyncio
import json
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from xiaogpt.device import STOP_DIRECTIVE, remember_directive
from xiaogpt.tts.doubao import DoubaoSpeaker
from xiaogpt.tts.file import FileTTS
from xiaogpt.xiaogpt import MiGPT


class RouteAndDirectiveTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self) -> MiGPT:
        bot = object.__new__(MiGPT)
        bot.config = SimpleNamespace(
            keyword=("请",),
            fallback_answer_keyword=(),
            learn_fallback=True,
        )
        bot.in_conversation = False
        bot.ai_session_until = 0.0
        bot.last_timestamp = 0
        bot.last_record = asyncio.Queue(1)
        bot.log = logging.getLogger("test-route")
        return bot

    async def test_all_forced_ai_routes_skip_fallback_classifier(self):
        classifier_calls = 0

        async def classify(record):
            nonlocal classifier_calls
            classifier_calls += 1
            return True

        cases = (
            ("触发词", "请解释量子纠缠", False, 0.0),
            ("持续对话", "解释量子纠缠", True, 0.0),
            ("追问窗口", "那它有什么用", False, float("inf")),
        )
        for label, query, in_conversation, follow_up_until in cases:
            with self.subTest(label=label):
                bot = self.make_bot()
                bot.in_conversation = in_conversation
                bot.ai_session_until = follow_up_until
                bot.resolve_fallback_verdict = classify
                route_to_ai, fallback_takeover = await bot._resolve_ai_route(
                    {"query": query, "time": 1}
                )
                self.assertTrue(route_to_ai)
                self.assertFalse(fallback_takeover)

        self.assertEqual(classifier_calls, 0)

    async def test_fallback_route_remains_distinct(self):
        bot = self.make_bot()

        async def classify(record):
            return True

        bot.resolve_fallback_verdict = classify
        self.assertEqual(
            await bot._resolve_ai_route({"query": "普通问题", "time": 1}),
            (True, True),
        )

    async def test_internal_directive_is_filtered_before_queue(self):
        bot = self.make_bot()
        directive = "测试内部停止指令"
        remember_directive(directive)
        data = {
            "data": json.dumps(
                {"records": [{"query": directive, "time": 1}]},
                ensure_ascii=False,
            )
        }

        self.assertIsNone(bot._get_last_query(data))
        self.assertTrue(bot.last_record.empty())
        self.assertEqual(bot.last_timestamp, 1)

        real_record = {"query": "真实用户输入", "time": 2}
        data["data"] = json.dumps({"records": [real_record]}, ensure_ascii=False)
        self.assertEqual(bot._get_last_query(data), real_record)
        self.assertEqual(bot.last_record.get_nowait(), real_record)

    async def test_internal_directive_echo_does_not_preempt_answer_stream(self):
        bot = self.make_bot()
        bot.config.stream = True
        bot.config.gpt_options = {}
        bot.polling_event = asyncio.Event()

        class Chatbot:
            async def ask_stream(self, query, **options):
                yield "第一句。"
                yield "第二句。"

        bot.__dict__["chatbot"] = Chatbot()
        directive = "测试流式回答期间的内部停止指令"
        remember_directive(directive)
        data = {
            "data": json.dumps(
                {"records": [{"query": directive, "time": 1}]},
                ensure_ascii=False,
            )
        }
        self.assertIsNone(bot._get_last_query(data))

        chunks = [chunk async for chunk in bot.ask_gpt("测试")]

        self.assertEqual("".join(chunks), "第一句。第二句。")
        self.assertTrue(bot.last_record.empty())

    async def test_closing_answer_stream_cleans_up_upstream_task(self):
        bot = self.make_bot()
        bot.config.stream = True
        bot.config.gpt_options = {}
        bot.polling_event = asyncio.Event()
        upstream_started = asyncio.Event()
        upstream_stopped = asyncio.Event()

        class Chatbot:
            async def ask_stream(self, query, **options):
                upstream_started.set()
                try:
                    yield "第一句。"
                    await asyncio.Future()
                finally:
                    upstream_stopped.set()

        bot.__dict__["chatbot"] = Chatbot()
        stream = bot.ask_gpt("测试")
        self.assertEqual(await asyncio.wait_for(anext(stream), timeout=1), "第一句。")
        await asyncio.wait_for(upstream_started.wait(), timeout=1)

        await stream.aclose()

        await asyncio.wait_for(upstream_stopped.wait(), timeout=1)
        self.assertFalse(bot.polling_event.is_set())


class MuteOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_claiming_playback_waits_for_mute_task_and_blocks_late_send(self):
        bot = object.__new__(MiGPT)
        bot._mute_task = None
        bot._mute_owner = None
        bot._playback_owner = None
        bot.log = logging.getLogger("test-mute")
        bot.config = SimpleNamespace(device_profile=SimpleNamespace(status_poll=True))
        started = asyncio.Event()
        stopped = asyncio.Event()
        calls: list[str] = []

        async def send_directive(text):
            calls.append(text)
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        bot.send_directive = send_directive
        turn_id = 7
        await bot._start_muting_xiaoai(turn_id)
        mute_task = bot._mute_task
        await asyncio.wait_for(started.wait(), timeout=1)

        await bot._stop_muting_xiaoai(turn_id, claim_playback=True)

        self.assertTrue(stopped.is_set())
        self.assertTrue(mute_task.done())
        self.assertIsNone(bot._mute_task)
        self.assertIsNone(bot._mute_owner)
        self.assertEqual(bot._playback_owner, turn_id)
        self.assertEqual(calls, [STOP_DIRECTIVE])

        await bot._mute_once(turn_id)
        self.assertEqual(calls, [STOP_DIRECTIVE])
        bot._release_playback(turn_id)
        self.assertIsNone(bot._playback_owner)

    async def test_l05c_waits_for_single_remote_stop_before_playback(self):
        bot = object.__new__(MiGPT)
        bot._mute_task = None
        bot._mute_owner = None
        bot._playback_owner = None
        bot.log = logging.getLogger("test-l05c-single-stop")
        bot.config = SimpleNamespace(device_profile=SimpleNamespace(status_poll=False))
        calls: list[str] = []

        async def send_directive(text):
            calls.append(text)
            return True

        bot.send_directive = send_directive
        turn_id = 8
        with patch("xiaogpt.xiaogpt.MUTE_COMMAND_SETTLE_SECONDS", 0):
            await bot._start_muting_xiaoai(turn_id)
            await bot._stop_muting_xiaoai(turn_id, claim_playback=True)

        self.assertEqual(calls, [STOP_DIRECTIVE])
        self.assertEqual(bot._playback_owner, turn_id)


class FileTTSPipelineTests(unittest.IsolatedAsyncioTestCase):
    def make_tts(self) -> FileTTS:
        tts = object.__new__(FileTTS)
        tts.last_played = None

        async def no_stop():
            return None

        tts._stop_after_playback = no_stop
        return tts

    async def test_worker_error_is_propagated_instead_of_hanging(self):
        tts = self.make_tts()

        async def fail_make_audio_file(lang, text):
            raise RuntimeError("synthetic TTS failure")

        async def text_stream():
            yield "第一句。"

        tts.make_audio_file = fail_make_audio_file
        with self.assertRaisesRegex(RuntimeError, "synthetic TTS failure"):
            await asyncio.wait_for(
                FileTTS.synthesize(tts, "zh-", text_stream()), timeout=1
            )

    async def test_cancelling_consumer_cleans_up_worker(self):
        tts = self.make_tts()
        started = asyncio.Event()
        worker_stopped = asyncio.Event()

        async def blocked_make_audio_file(lang, text):
            started.set()
            try:
                await asyncio.Future()
            finally:
                worker_stopped.set()

        async def text_stream():
            yield "第一句。"

        tts.make_audio_file = blocked_make_audio_file
        task = asyncio.create_task(FileTTS.synthesize(tts, "zh-", text_stream()))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(worker_stopped.wait(), timeout=1)

    def test_doubao_stop_margin_never_exceeds_explicit_tail_silence(self):
        tts = object.__new__(FileTTS)
        tts.config = SimpleNamespace(tts="doubao")
        tts.speaker = SimpleNamespace(silence_duration=3000)
        self.assertEqual(tts._stop_early_seconds(), 2.5)

        tts.speaker.silence_duration = 100
        self.assertEqual(tts._stop_early_seconds(), 0.1)

        tts.speaker.silence_duration = 0
        self.assertEqual(tts._stop_early_seconds(), 0.0)

        tts.config.tts = "edge"
        self.assertEqual(tts._stop_early_seconds(), 0.4)

    async def test_playback_start_timeout_aborts_instead_of_overlapping_next_chunk(
        self,
    ):
        tts = object.__new__(FileTTS)

        async def no_start(filename):
            return None

        tts._wait_for_playback_start = no_start
        with self.assertRaisesRegex(TimeoutError, "音箱未开始取流"):
            await tts._wait_for_duration_from_playback("late.mp3", 20)

    def test_doubao_requests_known_v3_trailing_silence(self):
        speaker = DoubaoSpeaker(api_key="test", speaker="test-speaker")
        additions = json.loads(speaker._payload("测试")["req_params"]["additions"])
        self.assertEqual(additions["silence_duration"], 3000)
        self.assertNotIn("enable_trailing_silence_audio", additions)


if __name__ == "__main__":
    unittest.main()
