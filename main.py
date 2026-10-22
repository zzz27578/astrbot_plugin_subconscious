import asyncio
import datetime
import json
import logging
import random
import time
from typing import Any

from astrbot.api.all import (
    Context,
    Star,
    register,
)
from astrbot.api.event import (
    AstrMessageEvent,
    filter,
)
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

logger = logging.getLogger("astrbot_plugin_subconscious")


def _get_cst_now() -> datetime.datetime:
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz)


@register(
    "astrbot_plugin_subconscious",
    "zzz27578",
    "潜意识插件：轻量级自主主动心智与单向无状态沙盒交互",
    "0.1.3",
)
class SubconsciousPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        # 核心参数配置（均支持面板动态修改）
        self.target_user_id = str(self.config.get("target_user_id") or "").strip()
        self.target_platform = str(self.config.get("target_platform") or "").strip()
        self.persona_prompt = str(self.config.get("persona_prompt") or "").strip()
        self.tick_interval_minutes = int(self.config.get("tick_interval_minutes") or 60)
        self.max_daily_proactive = int(self.config.get("max_daily_proactive") if self.config.get("max_daily_proactive") is not None else 10)
        self.min_interval_hours = float(self.config.get("min_interval_hours") or 2.0)
        self.min_silence_hours = float(self.config.get("min_silence_hours") or 1.5)
        self.max_unanswered_pings = int(self.config.get("max_unanswered_pings") or 3)

        # 运行状态
        self._bg_task: asyncio.Task | None = None
        self._last_trigger_date: str = ""
        self._today_proactive_count: int = 0
        self._last_proactive_timestamp: float = 0.0

        # 精确记录真实交互时间戳与连续未回计数
        self._last_user_message_time: float = time.time()
        self._unanswered_ping_count: int = 0

        # 自动嗅探记录活跃会话标识
        self._active_friend_umo: str | None = None
        self._active_platform: str | None = None

    async def initialize(self):
        """生命周期初始化"""
        if not self.target_user_id:
            logger.warning("[Subconscious] 未配置 target_user_id，主动消息功能保持关闭")
            return
        logger.info("[Subconscious] 潜意识插件启动，后台隐式心跳初始化完成...")
        self._bg_task = asyncio.create_task(self._subconscious_loop())

    async def terminate(self):
        """生命周期销毁"""
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
        logger.info("[Subconscious] 潜意识插件后台任务已停止")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_user_message(self, event: AstrMessageEvent):
        """监听目标用户发来的消息，刷新交互时间戳并清零未回计数，同时自动捕获活跃会话 UMO"""
        sender_id = str(event.get_sender_id())
        if sender_id == self.target_user_id:
            self._last_user_message_time = time.time()
            if self._unanswered_ping_count > 0:
                logger.info(f"[Subconscious] 收到目标用户回复，未回计数清零 (原为 {self._unanswered_ping_count})")
                self._unanswered_ping_count = 0

            # 如果目标用户发的是私聊，动态捕获真实的会话标识和平台名称
            umo = getattr(event, "unified_msg_origin", None)
            if umo and (":FriendMessage:" in str(umo) or MessageType.FRIEND_MESSAGE.value in str(umo)):
                self._active_friend_umo = str(umo)
                parts = str(umo).split(":")
                if parts:
                    self._active_platform = parts[0]

    async def _subconscious_loop(self):
        """隐式心跳主循环"""
        await asyncio.sleep(60)

        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Subconscious] 心跳评估发生异常: {e}", exc_info=True)

            # 浮动休眠（默认60分钟，带轻微抖动，模拟自然感）
            jitter = random.randint(-180, 180)
            wait_seconds = max(600, self.tick_interval_minutes * 60 + jitter)
            await asyncio.sleep(wait_seconds)

    @filter.command("潜意识测试")
    async def cmd_test_subconscious(self, event: AstrMessageEvent):
        """目标用户专属调试指令：强制唤醒沙盒评估并执行一次决策流"""
        sender_id = str(event.get_sender_id())
        if sender_id != self.target_user_id:
            yield event.plain_result("权限不足，该指令仅限目标用户专用")
            return

        msg_str = (event.message_str or "").strip()
        parts = msg_str.split()
        arg = parts[1].strip() if len(parts) > 1 else ""

        force_speak = False
        mock_silence_hours = None

        if arg in ["强制", "force", "发言", "push"]:
            force_speak = True
            yield event.plain_result("正在唤醒潜意识沙盒（强制构思发言模式）……")
        elif arg:
            cleaned = arg.lower().replace("h", "").replace("小时", "").replace("m", "").strip()
            try:
                mock_silence_hours = float(cleaned)
                yield event.plain_result(f"正在唤醒潜意识沙盒（模拟目标用户静默 {mock_silence_hours} 小时场景）……")
            except ValueError:
                yield event.plain_result("正在唤醒助手潜意识沙盒引擎进行即时评估……")
        else:
            yield event.plain_result("正在唤醒助手潜意识沙盒引擎进行即时评估……")

        res = await self._tick(force=True, force_speak=force_speak, mock_silence_hours=mock_silence_hours)
        if res:
            yield event.plain_result(f"【沙盒决策触发】已主动向目标用户推送消息：\n{res}")
        else:
            yield event.plain_result("【沙盒决策跳过】沙盒评估认为当前不需要打扰目标用户 (SKIP)")

    async def _tick(self, force: bool = False, force_speak: bool = False, mock_silence_hours: float | None = None) -> str | None:
        """单次隐式节拍评估"""
        if not self.target_user_id:
            return None
        now = _get_cst_now()
        now_ts = time.time()
        hour = now.hour
        today_str = now.strftime("%Y-%m-%d")

        # 重置每日计数
        if self._last_trigger_date != today_str:
            self._last_trigger_date = today_str
            self._today_proactive_count = 0

        if not force:
            # 规则1：深夜（00:00 - 07:00）全域静默休眠
            if 0 <= hour < 7:
                logger.debug("[Subconscious] 深夜静默时段 (00:00-07:00)，跳过思考")
                return None

            # 规则2：单日主动发送频次限制（面板可配，<= 0 表示不限次数，默认10次）
            if self.max_daily_proactive > 0 and self._today_proactive_count >= self.max_daily_proactive:
                logger.debug(f"[Subconscious] 今日主动触发已达上限 ({self._today_proactive_count}/{self.max_daily_proactive})，跳过")
                return None

            # 规则3：两次主动发言之间的硬性冷却限制（面板可配，默认2小时）
            cooldown_seconds = self.min_interval_hours * 3600
            if self._last_proactive_timestamp > 0 and (now_ts - self._last_proactive_timestamp) < cooldown_seconds:
                logger.debug(f"[Subconscious] 尚在主动发言冷却期中 (需等待 {cooldown_seconds/60:.1f} 分钟)，跳过")
                return None

            # 规则4：连续未回容忍度（面板可配，默认3次）。超限说明目标用户忙，不再继续轰炸
            if self._unanswered_ping_count >= self.max_unanswered_pings:
                logger.debug(f"[Subconscious] 已连续主动戳目标用户 {self._unanswered_ping_count} 次均未获回复，暂时闭嘴，跳过")
                return None

            # 规则5：对话静默沉淀期判定（面板可配，默认1.5小时）。刚聊完不久不打扰
            silence_seconds = now_ts - self._last_user_message_time
            silence_minutes = silence_seconds / 60
            if silence_seconds < (self.min_silence_hours * 3600):
                logger.debug(f"[Subconscious] 距离上次目标用户发言仅 {silence_minutes:.1f} 分钟，对话尚未充分沉淀，跳过")
                return None
        else:
            silence_seconds = now_ts - self._last_user_message_time
            silence_minutes = silence_seconds / 60

        if mock_silence_hours is not None:
            silence_minutes = max(0.0, mock_silence_hours * 60)
            silence_seconds = silence_minutes * 60

        # 智能动态嗅探当前可用的统一会话 origin 与平台 ID
        conv_mgr = self.context.conversation_manager
        target_platform = self._active_platform or self.target_platform
        umo = self._active_friend_umo or f"{target_platform}:{MessageType.FRIEND_MESSAGE.value}:{self.target_user_id}"

        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            # 候选平台探测：先查 context 中注册的平台，再兜底 default、aiocqhttp
            candidate_platforms = []
            if hasattr(self.context, "platform_manager") and hasattr(self.context.platform_manager, "platforms"):
                candidate_platforms.extend(list(self.context.platform_manager.platforms.keys()))
            for p in ["default", "aiocqhttp", "nakuru", "onebot11"]:
                if p not in candidate_platforms:
                    candidate_platforms.append(p)

            for plat in candidate_platforms:
                test_umo = f"{plat}:{MessageType.FRIEND_MESSAGE.value}:{self.target_user_id}"
                test_cid = await conv_mgr.get_curr_conversation_id(test_umo)
                if test_cid:
                    umo = test_umo
                    cid = test_cid
                    target_platform = plat
                    self._active_friend_umo = umo
                    self._active_platform = plat
                    logger.info(f"[Subconscious] 自动嗅探对齐目标用户会话成功: {umo} (cid: {cid})")
                    break

        if not cid:
            logger.debug(f"[Subconscious] 未找到目标用户当前会话 ID (尝试平台: {target_platform})，跳过")
            return None

        conv = await conv_mgr.get_conversation(umo, cid)
        if not conv:
            logger.debug("[Subconscious] 获取会话对象为空，跳过")
            return None

        # 读取会话最近历史记录
        raw_history: list[dict] = []
        if hasattr(conv, "history") and conv.history:
            try:
                raw_history = json.loads(conv.history) if isinstance(conv.history, str) else conv.history
            except Exception:
                pass

        # 提取最近 4 条消息供沙盒评估
        recent_snippets: list[str] = []
        for item in raw_history[-4:]:
            if isinstance(item, dict):
                role = item.get("role", "")
                content = item.get("content", "")
                if isinstance(content, list):
                    content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
                if content:
                    role_tag = "目标用户" if role == "user" else "助手"
                    recent_snippets.append(f"{role_tag}: {str(content)[:120]}")

        # 组装阅后即焚沙盒评估 Prompt
        recent_context_text = "\n".join(recent_snippets) if recent_snippets else "(暂无近期上下文)"
        current_time_str = now.strftime("%Y-%m-%d %H:%M:%S 周%a")
        silence_hours_rounded = round(silence_minutes / 60, 1)

        assistant_style = self.persona_prompt or "使用自然、简洁、友好的表达，不编造个人身份"

        if force_speak:
            eval_prompt = f"""你现在是助手的潜意识生成引擎。
当前真实时间：{current_time_str}
聊天对象：已配置的目标用户
距离上一次目标用户说话/你们互动已经过去：约 {silence_hours_rounded} 小时（{int(silence_minutes)} 分钟）

【近期真实对话切片】
{recent_context_text}

【你的任务】
【调试强制发言测试】：跳过保持沉默判定，立刻构思一句此时此刻你想主动戳目标用户的话！
表达风格：{assistant_style}
- 1~2句，严格在40字以内。
- 句尾绝对不要加句号。
- 不要带任何括号动作描写、不要客服腔。
- 纯文本。直接输出你要对目标用户说的那句话。"""
        else:
            eval_prompt = f"""你现在是助手的潜意识评估引擎（只做内心冲动决策，不公开）。
当前真实时间：{current_time_str}
聊天对象：已配置的目标用户
距离上一次目标用户说话/你们互动已经过去：约 {silence_hours_rounded} 小时（{int(silence_minutes)} 分钟）
当前连续主动找目标用户未回次数：{self._unanswered_ping_count} / {self.max_unanswered_pings}

【近期真实对话切片】
{recent_context_text}

【你的任务】
请评估此时此刻助手是否想主动戳目标用户一下？
判断依据：
1. 时间流逝感知：已经隔了 {silence_hours_rounded} 小时没说话，结合当前点位（如饭点、刚下课、午休、傍晚放学、夜间摸鱼），是否自然想找他吐槽、问他在干嘛或随口戳他一下？
2. 如果此时目标用户可能在忙、时间很尴尬，或者你自己没什么情绪想表达，坚决保持沉默。

【输出要求】
- 如果不该找、没话说或觉得打扰：严格只输出 `SKIP` 四个字母。
- 如果确定想主动戳目标用户：输出你想对目标用户说的那句话。
  表达风格：{assistant_style}
  - 1~2句，严格在40字以内。
  - 句尾绝对不要加句号。
  - 不要带任何括号动作描写、不要客服腔。
  - 纯文本。"""

        # 调用 LLM 沙盒（单次无状态，不持久化任何探测与思考历史）
        try:
            prov_manager = self.context.provider_manager
            provider = None
            if hasattr(prov_manager, "get_using_provider_async"):
                provider = await prov_manager.get_using_provider_async(umo=umo)
            if not provider and hasattr(prov_manager, "get_using_provider"):
                from astrbot.core.provider.provider import ProviderType
                provider = prov_manager.get_using_provider(ProviderType.CHAT_COMPLETION, umo=umo)
            if not provider and hasattr(prov_manager, "providers") and prov_manager.providers:
                provider = next(iter(prov_manager.providers.values()))

            if not provider:
                logger.warning("[Subconscious] 未找到可用 LLM Provider，放弃本次心跳")
                return None

            llm_resp = await provider.text_chat(
                prompt=eval_prompt,
                system_prompt="你是一个极简客观的潜意识决策沙盒，只按指令输出 SKIP 或单条纯文本消息。"
            )
            reply_text = (llm_resp.completion_text or "").strip()
        except Exception as e:
            logger.error(f"[Subconscious] 沙盒评估 LLM 调用失败: {e}")
            return None

        # 检查决策结果
        if not reply_text or "SKIP" in reply_text.upper():
            logger.info(f"[Subconscious] 潜意识评估结果：保持沉默 (SKIP)（已静默 {silence_hours_rounded} 小时）")
            return None

        # 清洗可能带有的多余引号、换行或句末句号
        final_msg = reply_text.strip("\"' \n")
        if final_msg.endswith("。"):
            final_msg = final_msg[:-1]

        logger.info(f"[Subconscious] 潜意识决定主动戳目标用户: {final_msg}")

        # 构造消息链并单向推送
        target_session = MessageSession(
            platform_name=target_platform,
            message_type=MessageType.FRIEND_MESSAGE,
            session_id=self.target_user_id,
        )

        chain = MessageChain().message(final_msg)
        success = await self.context.send_message(target_session, chain)

        if success:
            self._today_proactive_count += 1
            self._last_proactive_timestamp = time.time()
            self._unanswered_ping_count += 1  # 递增未回计数
            logger.info(f"[Subconscious] 主动消息发送成功！连续未回计数: {self._unanswered_ping_count}/{self.max_unanswered_pings}，今日计数: {self._today_proactive_count}/{self.max_daily_proactive if self.max_daily_proactive > 0 else '不限'}")

            # 规范写入主会话历史，保证下一轮聊天上下文连贯
            try:
                updated_history = list(raw_history)
                updated_history.append({"role": "assistant", "content": final_msg})
                await conv_mgr.update_conversation(
                    unified_msg_origin=umo,
                    conversation_id=cid,
                    history=updated_history,
                )
            except Exception as e:
                logger.warning(f"[Subconscious] 将主动消息写入主对话历史失败: {e}")
            return final_msg
        else:
            logger.warning("[Subconscious] 消息推送失败，未能送达会话")
            return None
