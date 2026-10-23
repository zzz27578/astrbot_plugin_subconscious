"""Pure code tests with in-memory AstrBot API doubles; no server or model access."""

import asyncio
import copy
import datetime
import enum
import importlib.util
import json
import sys
import time
import types
import unittest
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


class MessageType(enum.Enum):
    FRIEND_MESSAGE = "FriendMessage"
    GROUP_MESSAGE = "GroupMessage"


class MessageSession:
    def __init__(self, platform_name, message_type, session_id):
        self.platform_name = platform_name
        self.message_type = message_type
        self.session_id = session_id

    @classmethod
    def from_str(cls, value):
        platform, message_type, session_id = value.split(":", 2)
        return cls(platform, MessageType(message_type), session_id)


class MessageChain:
    def message(self, text):
        self.text = text
        return self


class Star:
    def __init__(self, context):
        self.context = context

    async def get_kv_data(self, key, default):
        return copy.deepcopy(self.context.kv.get(key, default))

    async def put_kv_data(self, key, value):
        if self.context.kv_error:
            raise RuntimeError("storage failed")
        self.context.kv[key] = copy.deepcopy(value)

    async def delete_kv_data(self, key):
        if self.context.kv_error:
            raise RuntimeError("storage failed")
        self.context.kv.pop(key, None)


class LockManager:
    @asynccontextmanager
    async def acquire_lock(self, session_id):
        yield


def decorator(*args, **kwargs):
    return lambda function: function


def load_plugin():
    modules = {
        name: types.ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.all",
            "astrbot.api.event",
            "astrbot.core",
            "astrbot.core.message",
            "astrbot.core.message.message_event_result",
            "astrbot.core.platform",
            "astrbot.core.platform.message_session",
            "astrbot.core.platform.message_type",
            "astrbot.core.utils",
            "astrbot.core.utils.session_lock",
        )
    }
    modules["astrbot.api"].logger = Mock()
    modules["astrbot.api.all"].Context = object
    modules["astrbot.api.all"].Star = Star
    modules["astrbot.api.all"].register = decorator
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=decorator,
        event_message_type=decorator,
        after_message_sent=decorator,
        EventMessageType=types.SimpleNamespace(ALL="ALL"),
    )
    modules["astrbot.core.message.message_event_result"].MessageChain = MessageChain
    modules["astrbot.core.platform.message_session"].MessageSession = MessageSession
    modules["astrbot.core.platform.message_type"].MessageType = MessageType
    modules["astrbot.core.utils.session_lock"].session_lock_manager = LockManager()
    spec = importlib.util.spec_from_file_location(
        "subconscious_under_test", Path(__file__).resolve().parents[1] / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**modules, spec.name: module}):
        spec.loader.exec_module(module)
    return module


plugin = load_plugin()
UMO = "custom-instance:FriendMessage:101"
DAY = datetime.datetime(
    2026, 10, 22, 15, tzinfo=datetime.timezone(datetime.timedelta(hours=8))
)


class Manager:
    def __init__(self):
        self.current_id = "conversation-1"
        self.conversations = {
            "conversation-1": {
                "history": [
                    {"role": "user", "content": "先去忙了，拜拜"},
                    {"role": "assistant", "content": "好，晚点聊"},
                ],
                "persona_id": "selected-persona",
            }
        }
        self.missing = False
        self.update_error = False
        self.updated = []
        self.created = []

    @property
    def history(self):
        return self.conversations[self.current_id]["history"]

    @history.setter
    def history(self, history):
        self.conversations[self.current_id]["history"] = history

    async def get_curr_conversation_id(self, unified_msg_origin):
        return self.current_id

    async def get_conversation(self, unified_msg_origin, conversation_id):
        if self.missing or conversation_id not in self.conversations:
            return None
        conversation = self.conversations[conversation_id]
        return types.SimpleNamespace(
            history=json.dumps(conversation["history"]),
            persona_id=conversation["persona_id"],
        )

    async def new_conversation(self, unified_msg_origin):
        self.created.append(unified_msg_origin)
        self.current_id = "new-conversation"
        self.conversations[self.current_id] = {"history": [], "persona_id": None}
        return self.current_id

    async def update_conversation(
        self,
        unified_msg_origin,
        conversation_id=None,
        history=None,
        title=None,
        persona_id=None,
        token_usage=None,
    ):
        if self.update_error:
            raise RuntimeError("history write failed")
        self.updated.append(
            (unified_msg_origin, conversation_id, copy.deepcopy(history))
        )
        self.conversations[conversation_id]["history"] = copy.deepcopy(history)


class Response:
    def __init__(self, text):
        self.completion_text = text
        self.reasoning_content = "PRIVATE_REASONING"


class Context:
    def __init__(self):
        self.conversation_manager = Manager()
        self.kv = {}
        self.kv_error = False
        self.calls = []
        self.sent = []
        self.send_success = True
        self.send_error = False
        self.hook = None
        self.send_hook = None
        self.response_text = '{"action":"send","message":"忙完了吗，要不要聊会儿"}'
        self.platform_available = True
        self.model_id = "session-main-model"
        self.persona_calls = []
        self.persona = {
            "prompt": "CURRENT_PERSONA_PROMPT",
            "_begin_dialogs_processed": [
                {"role": "assistant", "content": "PERSONA_EXAMPLE"}
            ],
        }
        self.persona_manager = types.SimpleNamespace(
            resolve_selected_persona=self.resolve_persona
        )
        self.request_context_ref = None
        self.response_ref = None

    def get_config(self, umo=None):
        return {"agent_runner": {"runner_type": "local"}}

    def get_platform_inst(self, platform_id):
        if not self.platform_available:
            return None
        return types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(name="aiocqhttp")
        )

    async def resolve_persona(
        self, *, umo, conversation_persona_id, platform_name, provider_settings=None
    ):
        self.persona_calls.append((umo, conversation_persona_id, platform_name))
        return "selected-persona", self.persona, None, False

    async def get_current_chat_provider_id(self, umo):
        self.selected_umo = umo
        return self.model_id

    async def llm_generate(
        self,
        *,
        chat_provider_id,
        prompt=None,
        image_urls=None,
        audio_urls=None,
        tools=None,
        system_prompt=None,
        contexts=None,
        **kwargs,
    ):
        self.calls.append(
            copy.deepcopy(
                {
                    "provider": chat_provider_id,
                    "prompt": prompt,
                    "system_prompt": system_prompt,
                    "contexts": contexts,
                    "tools": tools,
                    "kwargs": kwargs,
                }
            )
        )
        self.request_context_ref = contexts
        if self.hook:
            await self.hook()
        response = Response(self.response_text)
        self.response_ref = weakref.ref(response)
        return response

    async def send_message(self, session, message_chain):
        if self.send_error:
            raise RuntimeError("send failed")
        if self.send_hook:
            await self.send_hook()
        if self.send_success:
            self.sent.append((session, message_chain.text))
        return self.send_success


class Event:
    def __init__(
        self,
        message="潜意识测试 3",
        sender="101",
        umo=UMO,
        admin=False,
        private=True,
        components=None,
        raw=None,
        self_id="bot-account",
    ):
        self.message_str = message
        self.sender = sender
        self.unified_msg_origin = umo
        self.admin = admin
        self.private = private
        self.stopped = False
        self.message_obj = types.SimpleNamespace(
            self_id=self_id,
            raw_message=raw if raw is not None else {"post_type": "message"},
            message=components if components is not None else [],
        )

    def get_sender_id(self):
        return self.sender

    def is_private_chat(self):
        return self.private

    def get_messages(self):
        return self.message_obj.message

    def is_admin(self):
        return self.admin

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


class ParsingTests(unittest.TestCase):
    def test_history_snapshot_does_not_alias_normal_conversation(self):
        history = [{"role": "user", "content": "bye"}]
        snapshot = plugin._read_history(types.SimpleNamespace(history=history))
        history[0]["content"] = "new activity"
        self.assertEqual(snapshot[0]["content"], "bye")

    def test_decision_envelope(self):
        self.assertEqual(
            plugin._parse_decision('{"action":"skip","message":""}'), ("skip", "")
        )
        self.assertEqual(
            plugin._parse_decision(
                '```json\n{"action":"send","message":"聊聊 SKIP？"}\n```'
            ),
            ("send", "聊聊 SKIP？"),
        )

    def test_invalid_output_is_never_a_message(self):
        outputs = [
            "",
            "SKIP",
            "我想了一下，可以发消息",
            "<think>private</think>{}",
            "[]",
            '{"action":"send","message":""}',
            '{"action":"skip","message":"private"}',
            '{"action":"send","message":"hello","reason":"private"}',
            '{"action":"send","message":123}',
            '{"action":"other","message":"hello"}',
            '{"action":"send","message":"<think>private</think>hello"}',
            json.dumps({"action": "send", "message": "a" * 301}),
        ]
        for output in outputs:
            with self.subTest(output=output), self.assertRaises(ValueError):
                plugin._parse_decision(output)

    def test_test_arguments(self):
        for message, expected in [
            ("潜意识测试", (False, False, None)),
            ("/潜意识测试 3", (False, False, 3)),
            ("潜意识测试 force 30m 发送", (True, True, 0.5)),
            ("潜意识测试 强制", (True, True, None)),
            ("潜意识测试 force 3", (True, True, 3)),
            ("潜意识测试 force 3 预览", (True, False, 3)),
            ("潜意识测试 预览 force 3", (True, False, 3)),
            ("潜意识测试 3小时 强制 预览", (True, False, 3)),
        ]:
            self.assertEqual(plugin._parse_test_args(message), expected)
        for arg in [
            "nan",
            "inf",
            "-3",
            "garbage",
            "3 4",
            "force force",
            "发送 预览",
            "999999",
        ]:
            with self.subTest(arg=arg), self.assertRaises(ValueError):
                plugin._parse_test_args("潜意识测试 " + arg)

    def test_history_only_copies_visible_text_and_respects_budget(self):
        history = [
            {"role": "system", "content": "ignored"},
            {"role": "tool", "content": "ignored"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "visible"},
                    {"type": "think", "think": "secret"},
                    {"type": "image_url", "image_url": "secret"},
                ],
            },
        ]
        before = copy.deepcopy(history)
        result = plugin._dialogue_context(history, 20)
        result[0]["content"] = "changed"
        self.assertEqual(history, before)
        long_history = [{"role": "user", "content": "a" * 5000}] * 50
        result = plugin._dialogue_context(long_history, 100)
        self.assertLessEqual(sum(len(item["content"]) for item in result), 24000)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = Context()
        self.bot = plugin.SubconsciousPlugin(self.context, {"target_user_id": "101"})
        self.bot._binding = plugin.Binding(UMO, "101")
        self.bot._last_activity_time = time.time() - 10800
        self.clock = patch.object(plugin, "_get_cst_now", return_value=DAY)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def command(self, method, event):
        responses = []
        async for response in method(event):
            # The real scheduler refuses to send yielded results after stop_event().
            self.assertFalse(event.stopped)
            responses.append(response)
        self.assertTrue(event.stopped)
        return responses

    async def test_main_model_persona_and_history_are_used_then_discarded(self):
        history = copy.deepcopy(self.context.conversation_manager.history)
        result = await self.bot._tick(bypass_rules=True, deliver=False)
        self.assertEqual(result.status, "APPROVED")
        request = self.context.calls[0]
        self.assertEqual(request["provider"], "session-main-model")
        self.assertEqual(self.context.selected_umo, UMO)
        self.assertIn("CURRENT_PERSONA_PROMPT", request["system_prompt"])
        self.assertEqual(request["contexts"][1:], history)
        self.assertIsNone(request["tools"])
        self.assertFalse(request["kwargs"])
        self.assertEqual(
            self.context.persona_calls, [(UMO, "selected-persona", "aiocqhttp")]
        )
        self.assertEqual(self.context.request_context_ref, [])
        self.assertIsNone(self.context.response_ref())
        self.assertEqual(self.context.kv, {})
        self.assertEqual(self.context.conversation_manager.history, history)
        self.assertEqual(self.context.sent, [])
        self.assertNotIn("忙完", repr(result))
        self.assertEqual(self.bot._today_proactive_count, 0)

    async def test_only_delivered_message_is_appended_to_main_history(self):
        history = copy.deepcopy(self.context.conversation_manager.history)
        result = await self.bot._tick()
        self.assertEqual(result.status, "SENT")
        self.assertEqual(self.context.sent, [(UMO, "忙完了吗，要不要聊会儿")])
        self.assertEqual(
            self.context.conversation_manager.history,
            history + [{"role": "assistant", "content": "忙完了吗，要不要聊会儿"}],
        )
        self.assertNotIn(
            "PRIVATE_REASONING", json.dumps(self.context.conversation_manager.history)
        )
        self.assertEqual(self.bot._today_proactive_count, 1)
        self.assertEqual(self.bot._unanswered_ping_count, 1)
        self.assertEqual(self.context.kv, {})

    async def test_model_skip_is_distinct_from_failure(self):
        self.context.response_text = '{"action":"skip","message":""}'
        result = await self.bot._tick(bypass_rules=True)
        self.assertEqual(result.status, "MODEL_SKIP")
        self.assertEqual(len(self.context.calls), 1)
        self.assertFalse(self.context.sent)
        self.assertFalse(self.context.conversation_manager.updated)

    async def test_rules_skip_without_calling_model(self):
        for condition in ("night", "silence", "cooldown", "daily", "unanswered"):
            with self.subTest(condition=condition):
                self.bot._last_activity_time = time.time() - 10800
                self.bot._last_proactive_timestamp = 0
                self.bot._unanswered_ping_count = 0
                self.bot.config["max_daily_proactive"] = 0
                if condition == "night":
                    now = DAY.replace(hour=2)
                else:
                    now = DAY
                if condition == "silence":
                    self.bot._last_activity_time = time.time()
                if condition == "cooldown":
                    self.bot._last_proactive_timestamp = time.time()
                if condition == "daily":
                    self.bot.config["max_daily_proactive"] = 1
                    self.bot._last_trigger_date = DAY.date().isoformat()
                    self.bot._today_proactive_count = 1
                if condition == "unanswered":
                    self.bot._unanswered_ping_count = 3
                with patch.object(plugin, "_get_cst_now", return_value=now):
                    self.assertEqual((await self.bot._tick()).status, "RULE_SKIP")
        self.assertFalse(self.context.calls)

    async def test_test_bypasses_all_rules_even_at_night(self):
        self.bot._last_activity_time = time.time()
        self.bot._last_proactive_timestamp = time.time()
        self.bot._unanswered_ping_count = 3
        with patch.object(plugin, "_get_cst_now", return_value=DAY.replace(hour=2)):
            responses = await self.command(self.bot.cmd_test_subconscious, Event())
        self.assertIn("APPROVED", responses[-1])
        self.assertIn("3.00 小时", self.context.calls[0]["prompt"])
        self.assertFalse(self.context.sent)

    async def test_explicit_send_test_and_force_prompt(self):
        responses = await self.command(
            self.bot.cmd_test_subconscious, Event("潜意识测试 force 3 发送")
        )
        self.assertIn("SENT", responses[-1])
        self.assertIn("强制构思", self.context.calls[0]["prompt"])
        self.assertEqual(len(self.context.sent), 1)

    async def test_notice_events_during_force_test_do_not_prevent_send(self):
        async def notices():
            for post_type in ("notice", "notice", "request", "message_sent"):
                event = Event("", raw={"post_type": post_type, "notice_type": "notify"})
                await self.bot.on_user_message(event)
                await self.bot.on_assistant_message_sent(event)

        self.context.hook = notices
        responses = await self.command(
            self.bot.cmd_test_subconscious, Event("潜意识测试 强制")
        )
        self.assertIn("SENT", responses[-1])
        self.assertNotIn("STALE", responses[-1])
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.bot._unanswered_ping_count, 1)

    async def test_blank_messages_during_numeric_preview_do_not_discard_result(self):
        async def blanks():
            for text in ("", " ", "\t\n", "\xa0"):
                await self.bot.on_user_message(Event(text))

        self.context.hook = blanks
        responses = await self.command(
            self.bot.cmd_test_subconscious, Event("潜意识测试 3")
        )
        self.assertIn("APPROVED", responses[-1])
        self.assertFalse(self.context.sent)

    async def test_force_with_explicit_preview_does_not_send_or_save_draft(self):
        responses = await self.command(
            self.bot.cmd_test_subconscious, Event("潜意识测试 force 3 预览")
        )
        self.assertIn("APPROVED", responses[-1])
        self.assertFalse(self.context.sent)
        self.assertFalse(self.context.conversation_manager.updated)

    async def test_empty_events_do_not_change_activity_silence_or_unanswered_count(
        self,
    ):
        before = self.bot._activity_revision, self.bot._last_activity_time
        self.bot._unanswered_ping_count = 2
        events = [
            Event(""),
            Event(" \n"),
            Event("", raw={"post_type": "notice"}),
            Event("transport text", raw={"post_type": "notice"}),
            Event("outgoing echo", self_id="101"),
            Event("", components=[types.SimpleNamespace(type="Plain", text=" ")]),
            Event(
                "",
                components=[
                    types.SimpleNamespace(type="At"),
                    types.SimpleNamespace(type="Reply"),
                ],
            ),
        ]
        for event in events:
            await self.bot.on_user_message(event)
            await self.bot.on_assistant_message_sent(event)
        self.assertEqual(
            (self.bot._activity_revision, self.bot._last_activity_time), before
        )
        self.assertEqual(self.bot._unanswered_ping_count, 2)

    async def test_real_media_without_text_still_discards_candidate(self):
        for kind in ("Image", "Record", "Video", "File", "Face"):
            with self.subTest(kind=kind):

                async def incoming_media(kind=kind):
                    await self.bot.on_user_message(
                        Event(
                            "",
                            components=[
                                types.SimpleNamespace(
                                    type=types.SimpleNamespace(value=kind)
                                )
                            ],
                        )
                    )

                self.context.hook = incoming_media
                result = await self.bot._tick(bypass_rules=True, force_speak=True)
                self.assertEqual(result.status, "STALE")
        self.assertFalse(self.context.sent)

    async def test_genuine_text_in_message_chain_counts_as_activity(self):
        before = self.bot._activity_revision
        await self.bot.on_user_message(
            Event("", components=[types.SimpleNamespace(type="Plain", text="hello")])
        )
        self.assertGreater(self.bot._activity_revision, before)

    async def test_status_control_during_evaluation_does_not_discard_candidate(self):
        async def status():
            event = Event("潜意识状态")
            await self.bot.on_user_message(event)
            await self.bot.on_assistant_message_sent(event)

        self.context.hook = status
        self.assertEqual(
            (await self.bot._tick(bypass_rules=True, deliver=False)).status, "APPROVED"
        )

    async def test_invalid_model_output_and_exception_are_errors(self):
        self.context.response_text = "raw private thought"
        self.assertEqual((await self.bot._tick(bypass_rules=True)).status, "ERROR")

        async def fail():
            raise TypeError("private provider error")

        self.context.hook = fail
        self.assertEqual((await self.bot._tick(bypass_rules=True)).status, "ERROR")
        self.assertFalse(self.context.sent)
        self.assertFalse(self.context.conversation_manager.updated)

    async def test_timeout_discards_request_and_releases_evaluation_lock(self):
        started = asyncio.Event()

        async def stall():
            started.set()
            await asyncio.Event().wait()

        self.context.hook = stall
        self.bot.config["llm_timeout_seconds"] = 0.01
        result = await self.bot._tick(bypass_rules=True)
        self.assertEqual(result.status, "LLM_TIMEOUT")
        self.assertTrue(started.is_set())
        self.assertEqual(self.context.request_context_ref, [])
        self.assertFalse(self.bot._evaluation_lock.locked())
        self.assertFalse(self.context.sent)

    async def test_new_user_activity_discards_candidate(self):
        async def incoming():
            await self.bot.on_user_message(Event("我回来了"))

        self.context.hook = incoming
        self.assertEqual((await self.bot._tick(bypass_rules=True)).status, "STALE")
        self.assertFalse(self.context.sent)

    async def test_history_change_without_listener_discards_candidate(self):
        async def change_history():
            self.context.conversation_manager.history.append(
                {"role": "user", "content": "new"}
            )

        self.context.hook = change_history
        self.assertEqual((await self.bot._tick(bypass_rules=True)).status, "STALE")
        self.assertFalse(self.context.sent)

    async def test_missing_dialogue_keeps_private_session_binding(self):
        binding = self.bot._binding
        self.context.conversation_manager.current_id = None
        self.assertEqual(
            (await self.bot._tick(bypass_rules=True)).status, "WAITING_CONVERSATION"
        )
        self.assertIs(self.bot._binding, binding)
        self.context.conversation_manager.current_id = "conversation-1"
        self.context.conversation_manager.missing = True
        self.assertEqual(
            (await self.bot._tick(bypass_rules=True)).status, "CONVERSATION_MISSING"
        )
        self.assertFalse(self.context.calls)
        self.assertIs(self.bot._binding, binding)
        self.context.conversation_manager.missing = False
        self.assertEqual(
            (await self.bot._tick(bypass_rules=True, deliver=False)).status, "APPROVED"
        )

    async def test_switch_before_tick_uses_current_history_and_persona(self):
        manager = self.context.conversation_manager
        old_history = copy.deepcopy(manager.history)
        new_history = [{"role": "user", "content": "这是新对话里的话题"}]
        manager.conversations["conversation-2"] = {
            "history": new_history,
            "persona_id": "new-persona",
        }
        manager.current_id = "conversation-2"
        binding = self.bot._binding
        result = await self.bot._tick(bypass_rules=True)
        self.assertEqual(result.status, "SENT")
        self.assertIs(self.bot._binding, binding)
        self.assertEqual(self.context.calls[0]["contexts"][1:], new_history)
        self.assertEqual(
            self.context.persona_calls[0], (UMO, "new-persona", "aiocqhttp")
        )
        self.assertEqual(manager.updated[0][1], "conversation-2")
        self.assertEqual(
            manager.conversations["conversation-1"]["history"], old_history
        )
        self.assertEqual(self.context.sent[0][0], UMO)

    async def test_switch_during_evaluation_discards_only_current_attempt_then_follows_new_dialogue(
        self,
    ):
        manager = self.context.conversation_manager
        manager.conversations["conversation-2"] = {
            "history": [],
            "persona_id": "new-persona",
        }
        binding = self.bot._binding

        async def switch_dialogue():
            manager.current_id = "conversation-2"

        self.context.hook = switch_dialogue
        for deliver in (False, True):
            manager.current_id = "conversation-1"
            result = await self.bot._tick(bypass_rules=True, deliver=deliver)
            self.assertEqual(result.status, "STALE")
            self.assertIs(self.bot._binding, binding)
            self.assertFalse(self.context.sent)
        self.context.hook = None
        self.assertEqual((await self.bot._tick(bypass_rules=True)).status, "SENT")
        self.assertEqual(manager.updated[0][1], "conversation-2")

    async def test_status_remains_active_after_switch_and_without_selected_dialogue(
        self,
    ):
        manager = self.context.conversation_manager
        for cid in ("different-conversation", None):
            manager.current_id = cid
            responses = await self.command(self.bot.cmd_status, Event("潜意识状态"))
            self.assertIn("绑定状态：生效（整个私聊会话）", responses[-1])
            self.assertIn(UMO, responses[-1])
            self.assertNotIn("重新绑定", responses[-1])

    async def test_status_after_forward_reports_precise_silence_without_resetting_it(
        self,
    ):
        last_reply = datetime.datetime(2026, 10, 23, 13, 10, 49, 430000, plugin.CST)
        forwarded_at = last_reply.replace(
            hour=15, minute=32, second=50, microsecond=479000
        )
        queried_at = last_reply.replace(
            hour=15, minute=34, second=21, microsecond=422000
        )
        with patch.object(plugin.time, "time", return_value=last_reply.timestamp()):
            await self.bot.on_assistant_message_sent(Event("还剩二十分钟"))
        with patch.object(plugin.time, "time", return_value=forwarded_at.timestamp()):
            await self.bot.on_user_message(
                Event("[转发消息]", components=[types.SimpleNamespace(type="Forward")])
            )
        before = self.bot._last_activity_time, self.bot._activity_revision
        with patch.object(plugin.time, "time", return_value=queried_at.timestamp()):
            event = Event("潜意识状态")
            await self.bot.on_user_message(event)
            responses = await self.command(self.bot.cmd_status, event)
            await self.bot.on_assistant_message_sent(event)
        self.assertIn("静默时长：0 小时 1 分 30 秒", responses[-1])
        self.assertIn("最近计时更新：2026-10-23 15:32:50（北京时间）", responses[-1])
        self.assertIn("更新原因：用户消息", responses[-1])
        self.assertEqual(
            (self.bot._last_activity_time, self.bot._activity_revision), before
        )
        self.assertFalse(self.context.calls)

    async def test_status_without_forward_retains_two_hours_despite_blank_and_control(
        self,
    ):
        last_reply = datetime.datetime(2026, 10, 23, 13, 10, 49, 430000, plugin.CST)
        queried_at = last_reply.replace(
            hour=15, minute=34, second=21, microsecond=422000
        )
        with patch.object(plugin.time, "time", return_value=last_reply.timestamp()):
            await self.bot.on_assistant_message_sent(Event("还剩二十分钟"))
        self.bot._unanswered_ping_count = 2
        with patch.object(plugin.time, "time", return_value=queried_at.timestamp()):
            await self.bot.on_user_message(Event(" \n"))
            event = Event("/潜意识状态")
            await self.bot.on_user_message(event)
            responses = await self.command(self.bot.cmd_status, event)
            await self.bot.on_assistant_message_sent(event)
        self.assertIn("静默时长：2 小时 23 分 31 秒", responses[-1])
        self.assertIn("最近计时更新：2026-10-23 13:10:49（北京时间）", responses[-1])
        self.assertIn("更新原因：助手回复", responses[-1])
        self.assertEqual(self.bot._unanswered_ping_count, 2)

    async def test_heartbeat_logs_actual_rule_reason_and_elapsed_silence(self):
        now = DAY.timestamp()
        self.bot._last_activity_time = now - 90
        with (
            patch.object(plugin.time, "time", return_value=now),
            patch.object(
                plugin.asyncio,
                "sleep",
                new=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
            ),
            patch.object(plugin.logger, "info") as log_info,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.bot._subconscious_loop()
        log_info.assert_called_once_with(
            "[Subconscious] Heartbeat result: %s; %s",
            "RULE_SKIP",
            "对话尚未达到最小静默时长"
            "（当前 0 小时 1 分 30 秒 / 要求 1 小时 30 分 0 秒），未调用模型",
        )
        self.assertFalse(self.context.calls)

    async def test_cooldown_reason_reports_remaining_time_without_calling_model(self):
        now = DAY.timestamp()
        self.bot._last_proactive_timestamp = now - 10
        with patch.object(plugin.time, "time", return_value=now):
            result = await self.bot._tick()
        self.assertEqual(result.status, "RULE_SKIP")
        self.assertIn("冷却期（剩余 1 小时 59 分 50 秒）", result.detail)
        self.assertFalse(self.context.calls)

    async def test_switch_while_acquiring_send_lock_discards_only_current_attempt(self):
        manager = self.context.conversation_manager
        manager.conversations["conversation-2"] = {
            "history": [],
            "persona_id": "new-persona",
        }
        binding = self.bot._binding

        class SwitchingLock:
            @asynccontextmanager
            async def acquire_lock(self, session_id):
                manager.current_id = "conversation-2"
                yield

        with patch.object(plugin, "session_lock_manager", SwitchingLock()):
            result = await self.bot._tick(bypass_rules=True)
        self.assertEqual(result.status, "STALE")
        self.assertIs(self.bot._binding, binding)
        self.assertFalse(self.context.sent)
        self.assertFalse(manager.updated)
        self.assertEqual((await self.bot._tick(bypass_rules=True)).status, "SENT")
        self.assertEqual(manager.updated[0][1], "conversation-2")

    async def test_switch_during_delivery_writes_only_to_evaluated_dialogue(self):
        manager = self.context.conversation_manager
        old_history = copy.deepcopy(manager.history)
        new_history = [{"role": "user", "content": "新对话的话题"}]
        manager.conversations["conversation-2"] = {
            "history": copy.deepcopy(new_history),
            "persona_id": "new-persona",
        }

        async def switch_dialogue():
            manager.current_id = "conversation-2"

        self.context.send_hook = switch_dialogue
        result = await self.bot._tick(bypass_rules=True)
        self.assertEqual(result.status, "SENT")
        self.assertEqual(self.context.sent[0][0], UMO)
        self.assertEqual(manager.updated[0][1], "conversation-1")
        self.assertEqual(
            manager.conversations["conversation-1"]["history"],
            old_history + [{"role": "assistant", "content": self.context.sent[0][1]}],
        )
        self.assertEqual(
            manager.conversations["conversation-2"]["history"], new_history
        )
        self.context.send_hook = None
        self.assertEqual((await self.bot._tick(bypass_rules=True)).status, "SENT")
        self.assertEqual(manager.updated[-1][1], "conversation-2")

    async def test_binding_change_discards_candidate(self):
        async def unbind():
            await self.command(self.bot.cmd_unbind, Event("潜意识解绑"))

        self.context.hook = unbind
        self.assertEqual((await self.bot._tick(bypass_rules=True)).status, "STALE")
        self.assertFalse(self.context.sent)

    async def test_overlapping_ticks_make_only_one_request(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def stall():
            started.set()
            await finish.wait()

        self.context.hook = stall
        first = asyncio.create_task(self.bot._tick(bypass_rules=True, deliver=False))
        await started.wait()
        self.assertEqual((await self.bot._tick(bypass_rules=True)).status, "BUSY")
        finish.set()
        self.assertEqual((await first).status, "APPROVED")
        self.assertEqual(len(self.context.calls), 1)

    async def test_cancelled_sandbox_clears_context_and_releases_lock(self):
        started = asyncio.Event()

        async def stall():
            started.set()
            await asyncio.Event().wait()

        self.context.hook = stall
        task = asyncio.create_task(self.bot._tick(bypass_rules=True))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.context.request_context_ref, [])
        self.assertFalse(self.bot._evaluation_lock.locked())
        self.assertFalse(self.context.sent)
        self.assertFalse(self.context.conversation_manager.updated)

    async def test_activity_while_acquiring_main_conversation_lock_discards_result(
        self,
    ):
        bot = self.bot

        class IncomingLock:
            @asynccontextmanager
            async def acquire_lock(self, session_id):
                await bot.on_user_message(Event("new activity"))
                yield

        with patch.object(plugin, "session_lock_manager", IncomingLock()):
            self.assertEqual((await self.bot._tick(bypass_rules=True)).status, "STALE")
        self.assertFalse(self.context.sent)

    async def test_current_main_model_is_resolved_on_each_request(self):
        await self.bot._tick(bypass_rules=True, deliver=False)
        self.context.model_id = "new-session-model"
        await self.bot._tick(bypass_rules=True, deliver=False)
        self.assertEqual(
            [call["provider"] for call in self.context.calls],
            ["session-main-model", "new-session-model"],
        )

    async def test_daily_limit_resets_when_date_changes(self):
        self.bot.config["max_daily_proactive"] = 1
        self.bot._last_trigger_date = (
            (DAY - datetime.timedelta(days=1)).date().isoformat()
        )
        self.bot._today_proactive_count = 1
        self.assertEqual((await self.bot._tick()).status, "SENT")
        self.assertEqual(self.bot._last_trigger_date, DAY.date().isoformat())
        self.assertEqual(self.bot._today_proactive_count, 1)

    async def test_unavailable_platform_is_an_error_before_model_call(self):
        self.context.platform_available = False
        self.assertEqual((await self.bot._tick(bypass_rules=True)).status, "ERROR")
        self.assertFalse(self.context.calls)
        self.assertFalse(self.context.sent)

    async def test_send_failure_does_not_write_history_or_increment_counts(self):
        for exception in (False, True):
            self.context.send_success = False
            self.context.send_error = exception
            self.assertEqual(
                (await self.bot._tick(bypass_rules=True)).status, "SEND_FAILED"
            )
        self.assertFalse(self.context.conversation_manager.updated)
        self.assertEqual(self.bot._today_proactive_count, 0)
        self.assertEqual(self.bot._unanswered_ping_count, 0)

    async def test_history_failure_is_reported_after_successful_send(self):
        self.context.conversation_manager.update_error = True
        self.assertEqual(
            (await self.bot._tick(bypass_rules=True)).status, "SENT_HISTORY_FAILED"
        )
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.bot._today_proactive_count, 1)

    async def test_reply_during_delivery_is_not_counted_as_unanswered(self):
        async def incoming():
            await self.bot.on_user_message(Event("回来了"))

        self.context.send_hook = incoming
        self.assertEqual((await self.bot._tick(bypass_rules=True)).status, "SENT")
        self.assertEqual(self.bot._unanswered_ping_count, 0)

    async def test_binding_persists_only_routing_metadata_and_restores(self):
        await self.command(self.bot.cmd_bind, Event("潜意识绑定"))
        self.assertEqual(
            self.context.kv,
            {
                plugin.BINDING_KEY: {
                    "umo": UMO,
                    "user_id": "101",
                }
            },
        )
        fresh = plugin.SubconsciousPlugin(self.context)
        await fresh.initialize()
        self.assertEqual(fresh._binding, self.bot._binding)
        await fresh.terminate()
        await self.command(self.bot.cmd_unbind, Event("潜意识解绑"))
        self.assertIsNone(self.bot._binding)
        self.assertFalse(self.context.kv)

    async def test_bind_does_not_create_or_pin_a_normal_dialogue(self):
        self.context.conversation_manager.current_id = None
        await self.command(self.bot.cmd_bind, Event("潜意识绑定"))
        self.assertEqual(self.bot._binding, plugin.Binding(UMO, "101"))
        self.assertEqual(self.context.conversation_manager.created, [])
        self.assertEqual(
            (await self.bot._tick(bypass_rules=True)).status, "WAITING_CONVERSATION"
        )
        self.assertFalse(self.context.calls)
        await self.context.conversation_manager.new_conversation(UMO)
        self.assertEqual(
            (await self.bot._tick(bypass_rules=True, deliver=False)).status, "APPROVED"
        )

    async def test_legacy_binding_migrates_without_rebind_or_old_dialogue(self):
        self.context.kv[plugin.BINDING_KEY] = {
            "umo": UMO,
            "user_id": "101",
            "conversation_id": "deleted-old-dialogue",
        }
        fresh = plugin.SubconsciousPlugin(self.context)
        await fresh.initialize()
        try:
            self.assertEqual(fresh._binding, plugin.Binding(UMO, "101"))
            self.assertEqual(
                self.context.kv[plugin.BINDING_KEY], {"umo": UMO, "user_id": "101"}
            )
            self.assertEqual(
                (await fresh._tick(bypass_rules=True, deliver=False)).status, "APPROVED"
            )
        finally:
            await fresh.terminate()

    async def test_legacy_migration_write_failure_still_uses_session_binding(self):
        self.context.kv[plugin.BINDING_KEY] = {
            "umo": UMO,
            "user_id": "101",
            "conversation_id": "obsolete-dialogue",
        }
        self.context.kv_error = True
        fresh = plugin.SubconsciousPlugin(self.context)
        await fresh.initialize()
        try:
            self.assertEqual(fresh._binding, plugin.Binding(UMO, "101"))
            self.assertEqual(
                (await fresh._tick(bypass_rules=True, deliver=False)).status, "APPROVED"
            )
        finally:
            await fresh.terminate()

    async def test_invalid_saved_binding_is_not_used(self):
        self.context.kv[plugin.BINDING_KEY] = {
            "umo": "custom-instance:GroupMessage:101",
            "user_id": "101",
            "conversation_id": "conversation-1",
        }
        fresh = plugin.SubconsciousPlugin(self.context)
        await fresh.initialize()
        self.assertIsNone(fresh._binding)
        self.assertEqual((await fresh._tick()).status, "UNBOUND")
        await fresh.terminate()

    async def test_failed_binding_storage_preserves_previous_binding(self):
        before = self.bot._binding
        self.context.kv_error = True
        await self.command(self.bot.cmd_bind, Event("潜意识绑定"))
        self.assertIs(self.bot._binding, before)
        await self.command(self.bot.cmd_unbind, Event("潜意识解绑"))
        self.assertIs(self.bot._binding, before)

    async def test_commands_require_authorized_private_user(self):
        for event in [Event(sender="stranger"), Event(private=False, admin=True)]:
            responses = await self.command(self.bot.cmd_bind, event)
            self.assertEqual(len(responses), 1)
        self.assertFalse(self.context.kv)
        self.bot._binding = None
        self.bot.config.clear()
        await self.command(self.bot.cmd_bind, Event("潜意识绑定", admin=True))
        self.assertIsNotNone(self.bot._binding)

    async def test_control_commands_do_not_reset_silence_or_unanswered_count(self):
        before = self.bot._last_activity_time
        revision = self.bot._activity_revision
        self.bot._unanswered_ping_count = 2
        await self.bot.on_user_message(Event("潜意识测试 3"))
        await self.bot.on_assistant_message_sent(Event("潜意识测试 3"))
        self.assertEqual(self.bot._last_activity_time, before)
        self.assertEqual(self.bot._unanswered_ping_count, 2)
        self.assertEqual(self.bot._activity_revision, revision)
        await self.bot.on_user_message(Event("正常回复"))
        self.assertGreater(self.bot._last_activity_time, before)
        self.assertEqual(self.bot._unanswered_ping_count, 0)

    async def test_other_session_activity_has_no_effect(self):
        before = self.bot._activity_revision
        await self.bot.on_user_message(Event("hi", umo="other:FriendMessage:101"))
        self.assertEqual(self.bot._activity_revision, before)


if __name__ == "__main__":
    unittest.main()
