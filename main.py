import asyncio
import copy
import datetime
import json
import math
import random
import re
import time
from dataclasses import asdict, dataclass

from astrbot.api import logger
from astrbot.api.all import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.session_lock import session_lock_manager

BINDING_KEY = "active_binding"
CONTROL_COMMANDS = {"潜意识绑定", "潜意识解绑", "潜意识状态", "潜意识测试"}
MAX_MESSAGE_LENGTH = 300
MAX_CONTEXT_CHARACTERS = 24000


@dataclass(frozen=True)
class Binding:
    umo: str
    user_id: str
    conversation_id: str


@dataclass(frozen=True)
class TickResult:
    status: str
    detail: str
    provider_id: str = ""


def _get_cst_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def _read_history(conversation) -> list[dict]:
    history = conversation.history or []
    if isinstance(history, str):
        history = json.loads(history)
    if not isinstance(history, list) or any(
        not isinstance(item, dict) for item in history
    ):
        raise ValueError("Invalid conversation history")
    return copy.deepcopy(history)


def _dialogue_context(history: list[dict], limit: int) -> list[dict]:
    """Copy visible dialogue into a bounded, text-only request context."""
    messages = []
    remaining = MAX_CONTEXT_CHARACTERS
    for item in reversed(history):
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = item.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                part["text"]
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            )
        if not isinstance(content, str) or not content.strip():
            continue
        content = content[-min(4000, remaining) :]
        messages.append({"role": role, "content": content})
        remaining -= len(content)
        if len(messages) >= limit or remaining <= 0:
            break
    return list(reversed(messages))


def _parse_decision(text: str) -> tuple[str, str]:
    """Accept only a decision envelope; never send raw reasoning."""
    text = text.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if not match:
            raise ValueError("Invalid decision envelope")
        text = match.group(1)
    decision = json.loads(text)
    if not isinstance(decision, dict) or set(decision) != {"action", "message"}:
        raise ValueError("Invalid decision fields")
    action, message = decision["action"], decision["message"]
    if not isinstance(message, str):
        raise ValueError("Invalid message type")
    message = message.strip()
    if action == "skip" and not message:
        return "skip", ""
    if (
        action != "send"
        or not message
        or len(message) > MAX_MESSAGE_LENGTH
        or re.search(r"</?(?:think|analysis|reasoning)\b", message, re.IGNORECASE)
    ):
        raise ValueError("Invalid proactive message")
    return "send", message


def _parse_test_args(message: str) -> tuple[bool, bool, float | None]:
    """Parse a mode, an optional simulated duration, and delivery choice."""
    args = message.strip().split()[1:]
    force_speak = deliver = False
    mock_hours = None
    mode_seen = delivery_seen = False
    for arg in args:
        arg = arg.lower()
        if arg in {"force", "强制", "评估"} and not mode_seen:
            force_speak = arg != "评估"
            mode_seen = True
        elif arg in {"发送", "send", "预览", "preview"} and not delivery_seen:
            deliver = arg in {"发送", "send"}
            delivery_seen = True
        elif mock_hours is None:
            match = re.fullmatch(r"(\d+(?:\.\d+)?)(h|小时|m|分钟)?", arg)
            if not match:
                raise ValueError("Invalid test argument")
            mock_hours = float(match.group(1))
            if match.group(2) in {"m", "分钟"}:
                mock_hours /= 60
            if not math.isfinite(mock_hours) or mock_hours > 8760:
                raise ValueError("Invalid simulated duration")
        else:
            raise ValueError("Unexpected test argument")
    return force_speak, deliver, mock_hours


@register(
    "astrbot_plugin_subconscious",
    "zzz27578",
    "绑定私聊后，由主模型在临时沙盒中决策的主动消息插件",
    "0.2.0",
)
class SubconsciousPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._binding: Binding | None = None
        self._bg_task: asyncio.Task | None = None
        self._evaluation_lock = asyncio.Lock()
        self._binding_lock = asyncio.Lock()
        self._activity_revision = 0
        self._last_activity_time = time.time()
        self._last_trigger_date = ""
        self._today_proactive_count = 0
        self._last_proactive_timestamp = 0.0
        self._unanswered_ping_count = 0

    async def initialize(self):
        """Restore routing metadata only and start the heartbeat."""
        stored = await self.get_kv_data(BINDING_KEY, None)
        if stored is not None:
            try:
                binding = Binding(**stored)
                session = MessageSession.from_str(binding.umo)
                if (
                    session.message_type != MessageType.FRIEND_MESSAGE
                    or not session.platform_name
                    or not session.session_id
                    or any(
                        not isinstance(value, str) or not value.strip()
                        for value in asdict(binding).values()
                    )
                ):
                    raise ValueError("Invalid binding")
                self._binding = binding
            except (TypeError, ValueError, AttributeError):
                logger.warning("[Subconscious] Stored binding is invalid; bind again")
        self._bg_task = asyncio.create_task(self._subconscious_loop())
        logger.info("[Subconscious] Heartbeat started; bound=%s", bool(self._binding))

    async def terminate(self):
        if self._bg_task:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
        self._binding = None

    def _command_error(self, event: AstrMessageEvent) -> str | None:
        if not event.is_private_chat():
            return "请在要接收主动消息的私聊中使用此指令"
        sender = str(event.get_sender_id())
        allowed_user = str(self.config.get("target_user_id") or "").strip()
        if event.is_admin() or sender == allowed_user:
            return None
        if self._binding and sender == self._binding.user_id:
            return None
        return "此指令仅限 AstrBot 管理员、配置的目标用户或已绑定用户使用"

    @filter.command("潜意识绑定")
    async def cmd_bind(self, event: AstrMessageEvent):
        if error := self._command_error(event):
            yield event.plain_result(error)
            event.stop_event()
            return
        async with self._binding_lock:
            try:
                umo = str(event.unified_msg_origin)
                manager = self.context.conversation_manager
                cid = await manager.get_curr_conversation_id(umo)
                if not cid:
                    cid = await manager.new_conversation(umo)
                if not isinstance(cid, str) or not cid:
                    raise ValueError("Conversation ID is unavailable")
                binding = Binding(umo, str(event.get_sender_id()), cid)
                await self.put_kv_data(BINDING_KEY, asdict(binding))
            except Exception as exc:
                logger.error("[Subconscious] Binding failed (%s)", type(exc).__name__)
                yield event.plain_result(
                    "绑定失败：无法读取对话或保存绑定信息，请查看日志"
                )
                event.stop_event()
                return
            self._binding = binding
            self._activity_revision += 1
            self._last_activity_time = time.time()
            self._unanswered_ping_count = 0
        yield event.plain_result(
            "已绑定当前私聊和当前对话。对话静默后将按节拍唤醒主模型，"
            "由模型决定是否主动接话；切换新对话后请重新绑定"
        )
        event.stop_event()

    @filter.command("潜意识解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        if error := self._command_error(event):
            yield event.plain_result(error)
            event.stop_event()
            return
        async with self._binding_lock:
            try:
                await self.delete_kv_data(BINDING_KEY)
            except Exception as exc:
                logger.error("[Subconscious] Unbinding failed (%s)", type(exc).__name__)
                yield event.plain_result("解绑失败：无法删除绑定信息，请查看日志")
                event.stop_event()
                return
            self._binding = None
            self._activity_revision += 1
        yield event.plain_result("已解绑，主动消息已停止")
        event.stop_event()

    @filter.command("潜意识状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if error := self._command_error(event):
            yield event.plain_result(error)
            event.stop_event()
            return
        if not self._binding:
            yield event.plain_result("尚未绑定，请在目标私聊发送 /潜意识绑定")
            event.stop_event()
            return
        binding = self._binding
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(
                binding.umo
            )
        except Exception as exc:
            logger.error("[Subconscious] Status lookup failed (%s)", type(exc).__name__)
            yield event.plain_result("状态查询失败：无法读取当前对话，请查看日志")
            event.stop_event()
            return
        state = "生效" if cid == binding.conversation_id else "对话已切换，需重新绑定"
        silence = max(0.0, (time.time() - self._last_activity_time) / 3600)
        yield event.plain_result(
            f"绑定状态：{state}\n静默时长：{silence:.1f} 小时\n"
            f"连续未回复：{self._unanswered_ping_count}\n"
            f"节拍：约 {self.config.get('tick_interval_minutes', 60)} 分钟"
        )
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_user_message(self, event: AstrMessageEvent):
        binding = self._binding
        if (
            not binding
            or str(event.unified_msg_origin) != binding.umo
            or str(event.get_sender_id()) != binding.user_id
        ):
            return
        self._activity_revision += 1
        command = (event.message_str or "").strip().lstrip("/").split()
        if command and command[0] in CONTROL_COMMANDS:
            return
        self._last_activity_time = time.time()
        self._unanswered_ping_count = 0

    @filter.after_message_sent()
    async def on_assistant_message_sent(self, event: AstrMessageEvent):
        if not self._binding or str(event.unified_msg_origin) != self._binding.umo:
            return
        command = (event.message_str or "").strip().lstrip("/").split()
        if command and command[0] in CONTROL_COMMANDS:
            return
        self._last_activity_time = time.time()
        self._activity_revision += 1

    @filter.command("潜意识测试")
    async def cmd_test_subconscious(self, event: AstrMessageEvent):
        if error := self._command_error(event):
            yield event.plain_result(error)
            event.stop_event()
            return
        if not self._binding or str(event.unified_msg_origin) != self._binding.umo:
            yield event.plain_result("请先在当前私聊发送 /潜意识绑定")
            event.stop_event()
            return
        try:
            force_speak, deliver, mock_hours = _parse_test_args(event.message_str or "")
        except ValueError:
            yield event.plain_result(
                "用法：/潜意识测试 [评估|force] [静默时长] [预览|发送]\n"
                "例如：/潜意识测试 3、/潜意识测试 3 发送、"
                "/潜意识测试 force 3 发送；时长默认小时，也支持 30m"
            )
            event.stop_event()
            return
        yield event.plain_result(
            "正在调用当前对话的主模型进行临时沙盒评估"
            + ("（强制构思）" if force_speak else "")
            + ("，批准后发送……" if deliver else "，仅预览决策……")
        )
        result = await self._tick(
            bypass_rules=True,
            force_speak=force_speak,
            mock_silence_hours=mock_hours,
            deliver=deliver,
        )
        model = f"\n主模型：{result.provider_id}" if result.provider_id else ""
        yield event.plain_result(f"【{result.status}】{result.detail}{model}")
        event.stop_event()

    async def _subconscious_loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                result = await self._tick()
                logger.info("[Subconscious] Heartbeat result: %s", result.status)
            except Exception as exc:
                logger.error("[Subconscious] Heartbeat failed (%s)", type(exc).__name__)
            minutes = float(self.config.get("tick_interval_minutes", 60))
            await asyncio.sleep(max(60, minutes * 60 + random.randint(-180, 180)))

    def _rule_skip(self, now: datetime.datetime) -> str | None:
        now_ts = time.time()
        if 0 <= now.hour < 7:
            return "当前处于北京时间 00:00–07:00 静默时段，未调用模型"
        count = (
            self._today_proactive_count
            if self._last_trigger_date == now.date().isoformat()
            else 0
        )
        daily_limit = int(self.config.get("max_daily_proactive", 0))
        if daily_limit > 0 and count >= daily_limit:
            return "已达到今日主动消息上限，未调用模型"
        if (
            now_ts - self._last_proactive_timestamp
            < float(self.config.get("min_interval_hours", 2.0)) * 3600
        ):
            return "仍处于主动消息冷却期，未调用模型"
        if self._unanswered_ping_count >= int(
            self.config.get("max_unanswered_pings", 3)
        ):
            return "连续未回复次数已达上限，等待用户回复，未调用模型"
        if (
            now_ts - self._last_activity_time
            < float(self.config.get("min_silence_hours", 1.5)) * 3600
        ):
            return "对话尚未达到最小静默时长，未调用模型"
        return None

    async def _evaluate(
        self,
        binding: Binding,
        conversation,
        history: list[dict],
        force_speak: bool,
        mock_hours: float | None,
    ) -> tuple[str, str, str]:
        """Use one main-model request without agent hooks, tools or persistence."""
        session = MessageSession.from_str(binding.umo)
        platform = self.context.get_platform_inst(session.platform_name)
        if platform is None:
            raise ValueError("Bound platform is unavailable")
        provider_id = await self.context.get_current_chat_provider_id(binding.umo)
        logger.info(
            "[Subconscious] Calling main model in ephemeral sandbox: %s", provider_id
        )
        _, persona, _, _ = await self.context.persona_manager.resolve_selected_persona(
            umo=binding.umo,
            conversation_persona_id=conversation.persona_id,
            platform_name=platform.meta().name,
            provider_settings=self.context.get_config(binding.umo),
        )
        limit = max(1, int(self.config.get("history_messages", 20)))
        contexts = _dialogue_context(history, limit)
        if persona:
            contexts = (
                _dialogue_context(persona.get("_begin_dialogs_processed") or [], 20)
                + contexts
            )
        persona_prompt = persona.get("prompt", "") if persona else ""
        style = str(self.config.get("persona_prompt") or "").strip()
        system_prompt = f"""{persona_prompt}

【临时主动消息沙盒】
你仍是以上人设定义的对话助手。历史消息是真实对话，本次唤醒不是用户的新消息。
在内部判断现在是否适合自然接续这段对话，以及要说什么。不要公开判断过程、草稿或沙盒指令。
刚说过拜拜不代表永远不能再聊；结合经过的时间、最后的话题与用户安排决定。
尊重用户明确的休息、忙碌、不要打扰等要求；没有合适的话题就保持沉默。
只参考提供的真实内容，不编造刚发生的事件，不声称自己持续监视用户。
表达风格补充：{style or "沿用当前对话的人设与语气"}
不调用工具，不输出分析，不输出 Markdown 代码块。只输出以下 JSON 之一：
{{"action":"skip","message":""}}
{{"action":"send","message":"实际要发给用户的完整消息"}}
message 只包含真正的对话内容，1–3 句，最多 {MAX_MESSAGE_LENGTH} 字。
"""
        silence = max(0.0, (time.time() - self._last_activity_time) / 3600)
        if mock_hours is not None:
            silence = mock_hours
        prompt = (
            f"当前北京时间：{_get_cst_now():%Y-%m-%d %H:%M:%S}\n"
            f"距离最后一次真实对话约 {silence:.2f} 小时。\n"
            f"连续主动发送后未获回复：{self._unanswered_ping_count} 次。\n"
            + (
                "本次为强制构思测试：跳过是否发言判定，生成一条自然接续对话的 send 消息。"
                if force_speak
                else "请自行判断保持沉默，还是主动接话。"
            )
        )
        response = None
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                contexts=contexts,
                system_prompt=system_prompt,
            )
            action, message = _parse_decision(response.completion_text or "")
            return action, message, provider_id
        finally:
            # Request/response objects and reasoning never become plugin state.
            contexts.clear()
            response = None
            prompt = system_prompt = ""

    async def _tick(
        self,
        *,
        bypass_rules: bool = False,
        force_speak: bool = False,
        mock_silence_hours: float | None = None,
        deliver: bool = True,
    ) -> TickResult:
        if self._evaluation_lock.locked():
            return TickResult("BUSY", "已有一次沙盒评估正在执行，未重复调用模型")
        async with self._evaluation_lock:
            binding = self._binding
            if not binding:
                return TickResult("UNBOUND", "尚未绑定主动消息对话，未调用模型")
            if not bypass_rules and (reason := self._rule_skip(_get_cst_now())):
                return TickResult("RULE_SKIP", reason)
            revision = self._activity_revision
            manager = self.context.conversation_manager
            try:
                cid = await manager.get_curr_conversation_id(binding.umo)
                if cid != binding.conversation_id:
                    return TickResult(
                        "CONVERSATION_CHANGED",
                        "当前对话已切换或删除，请重新绑定；未调用模型",
                    )
                conversation = await manager.get_conversation(binding.umo, cid)
                if conversation is None:
                    return TickResult(
                        "CONVERSATION_MISSING", "绑定对话不存在，请重新绑定；未调用模型"
                    )
                history = _read_history(conversation)
                action, message, provider_id = await asyncio.wait_for(
                    self._evaluate(
                        binding, conversation, history, force_speak, mock_silence_hours
                    ),
                    timeout=float(self.config.get("llm_timeout_seconds", 120)),
                )
            except asyncio.TimeoutError:
                return TickResult("LLM_TIMEOUT", "沙盒模型请求超时，未发送任何消息")
            except Exception as exc:
                logger.error("[Subconscious] Sandbox failed (%s)", type(exc).__name__)
                return TickResult(
                    "ERROR",
                    "沙盒调用或结果校验失败，未发送消息；请查看日志中的错误类型",
                )
            if self._binding is not binding or self._activity_revision != revision:
                return TickResult(
                    "STALE", "评估期间对话有新活动或绑定已变化，已丢弃结果", provider_id
                )
            if action == "skip":
                return TickResult(
                    "MODEL_SKIP", "已调用主模型，模型决定保持沉默", provider_id
                )
            if not deliver:
                return TickResult(
                    "APPROVED",
                    "已调用主模型并批准主动发言；预览完成，草稿已丢弃，未发送或写入历史",
                    provider_id,
                )
            async with session_lock_manager.acquire_lock(binding.umo):
                try:
                    # Recheck after acquiring AstrBot's normal conversation lock.
                    cid = await manager.get_curr_conversation_id(binding.umo)
                    current = await manager.get_conversation(
                        binding.umo, binding.conversation_id
                    )
                    if (
                        self._binding is not binding
                        or self._activity_revision != revision
                        or cid != binding.conversation_id
                        or current is None
                        or _read_history(current) != history
                    ):
                        return TickResult(
                            "STALE", "发送前对话有变化，已丢弃沙盒结果", provider_id
                        )
                    success = await self.context.send_message(
                        binding.umo, MessageChain().message(message)
                    )
                except Exception as exc:
                    logger.error(
                        "[Subconscious] Delivery failed (%s)", type(exc).__name__
                    )
                    return TickResult(
                        "SEND_FAILED", "主动消息发送失败，未写入历史", provider_id
                    )
                if not success:
                    return TickResult(
                        "SEND_FAILED", "未找到可发送的平台，未写入历史", provider_id
                    )
                today = _get_cst_now().date().isoformat()
                if self._last_trigger_date != today:
                    self._last_trigger_date = today
                    self._today_proactive_count = 0
                self._today_proactive_count += 1
                self._last_proactive_timestamp = time.time()
                self._last_activity_time = self._last_proactive_timestamp
                if self._activity_revision == revision:
                    self._unanswered_ping_count += 1
                self._activity_revision += 1
                try:
                    # Only delivered speech becomes part of the normal conversation.
                    current = await manager.get_conversation(
                        binding.umo, binding.conversation_id
                    )
                    if current is None:
                        raise ValueError("Conversation was deleted during delivery")
                    delivered_history = list(_read_history(current))
                    delivered_history.append({"role": "assistant", "content": message})
                    await manager.update_conversation(
                        unified_msg_origin=binding.umo,
                        conversation_id=binding.conversation_id,
                        history=delivered_history,
                    )
                except Exception as exc:
                    logger.warning(
                        "[Subconscious] History update failed (%s)", type(exc).__name__
                    )
                    return TickResult(
                        "SENT_HISTORY_FAILED",
                        "主动消息已发送，但正常对话历史写入失败",
                        provider_id,
                    )
                return TickResult(
                    "SENT",
                    "主模型批准的主动消息已发送，并接续正常对话历史",
                    provider_id,
                )
