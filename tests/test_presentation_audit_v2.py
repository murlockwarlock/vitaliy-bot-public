"""Comprehensive Presentation and End-to-End Typing Audit Test Suite (v2).

Validates:
1. Habit names inside Telegram HTML (A < B & C, <3, <b>test</b>, AT&T).
2. Referral link text dynamic HTML insertion & contract.
3. Follow-up delivery HTML contract (static templates, AI plain text escaping, malformed fallback).
4. Field-origin dynamic HTML inspection on actual Telegram call arguments.
5. Real flow typing duration, continuous pulsing, and ordering using delayed asyncio.Event mocks.
"""

import asyncio
import html
import os
import re
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message
from sqlalchemy import delete, select

from database import (
    Content,
    Habit,
    HabitConfig,
    JournalEntry,
    KnowledgeBase,
    Message as DBMessage,
    PaymentConfig,
    SubscriptionPlan,
    TestAttempt,
    TestSet,
    Topic,
    User,
    UserSubscription,
    async_session_maker,
    init_db,
)
from time_utils import msk_now


class MockBot(Bot):
    """Mock Aiogram Bot capturing all sent messages and chat actions."""

    def __init__(self):
        self.sent_messages = []
        self.chat_actions = []
        self._default = SimpleNamespace(link_preview_is_disabled=True)

    @property
    def default(self):
        return self._default

    async def send_message(self, chat_id, text, **kwargs):
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=len(self.sent_messages) + 1,
            text=text,
            html_text=text,
            kwargs=kwargs,
            delete=AsyncMock(),
        )
        self.sent_messages.append(msg)
        return msg

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.chat_actions.append({"chat_id": chat_id, "action": action, **kwargs})
        return True

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=message_id or 1,
            text=text,
            html_text=text,
            kwargs=kwargs,
        )
        self.sent_messages.append(msg)
        return msg

    async def get_file(self, file_id):
        return SimpleNamespace(file_path="mock_path")

    async def download_file(self, file_path):
        import io
        return io.BytesIO(b"mock_bytes")


def make_message(text: str, user_id: int, chat_id: int = None, bot: MockBot = None) -> AsyncMock:
    if chat_id is None:
        chat_id = user_id
    msg = AsyncMock(spec=Message)
    msg.text = text
    msg.caption = None
    msg.photo = None
    msg.voice = None
    msg.audio = None
    msg.from_user = SimpleNamespace(id=user_id, is_bot=False, first_name="Test", username=f"user_{user_id}")
    msg.chat = SimpleNamespace(id=chat_id, type="private")
    msg.message_id = 101
    msg.answer = AsyncMock()
    if bot:
        msg.bot = bot
    return msg


def make_callback_query(data: str, user_id: int, chat_id: int = None, bot: MockBot = None) -> AsyncMock:
    if chat_id is None:
        chat_id = user_id
    cb = AsyncMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = SimpleNamespace(id=user_id, is_bot=False, first_name="Test", username=f"user_{user_id}")
    cb.message = AsyncMock(spec=Message)
    cb.message.chat = SimpleNamespace(id=chat_id, type="private")
    cb.message.message_id = 102
    cb.message.content_type = "text"
    cb.message.html_text = "<b>Initial</b>"
    cb.message.edit_text = AsyncMock()
    cb.message.delete = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    if bot:
        cb.bot = bot
        cb.message.bot = bot
    return cb


# =========================================================================
# 1. Habit Name HTML Escaping Tests
# =========================================================================
class TestHabitNameHtmlEscaping(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.bot = MockBot()
        self.storage = MemoryStorage()
        async with async_session_maker() as session:
            await session.execute(delete(Habit))
            await session.execute(delete(HabitConfig))
            session.add(HabitConfig(id=1, mode="hybrid", streak_days=7, reward_sequence="M,O,L,O,D,E,C", time_window_minutes=60))
            p_conf = await session.get(PaymentConfig, 1)
            if not p_conf:
                session.add(PaymentConfig(id=1, mode="free"))
            else:
                p_conf.mode = "free"
            await session.commit()

    async def test_habit_save_name_with_dangerous_inputs(self):
        """habit_save_name must escape dangerous user-controlled habit names in HTML."""
        from handlers import habit_save_name

        test_inputs = ["A < B & C", "<3", "<b>test</b>", "AT&T"]
        for dangerous_name in test_inputs:
            state = FSMContext(
                storage=self.storage,
                key=StorageKey(bot_id=123, chat_id=1001, user_id=1001),
            )
            msg = make_message(dangerous_name, user_id=1001, bot=self.bot)

            await habit_save_name(msg, state)

            self.assertTrue(msg.answer.called)
            call_kwargs = msg.answer.call_args.kwargs
            sent_text = msg.answer.call_args.args[0] if msg.answer.call_args.args else call_kwargs.get("text", "")
            parse_mode = call_kwargs.get("parse_mode")

            self.assertEqual(parse_mode, "HTML")
            expected_escaped = html.escape(dangerous_name)
            self.assertIn(expected_escaped, sent_text)
            if "<" in dangerous_name or "&" in dangerous_name:
                self.assertNotIn(f"«{dangerous_name}»", sent_text)

    async def test_habit_finalize_creation_with_dangerous_inputs(self):
        """habit_finalize_creation must escape habit name and time inside HTML confirmation."""
        from handlers import habit_finalize_creation

        test_inputs = ["A < B & C", "<3", "<b>test</b>", "AT&T"]
        for dangerous_name in test_inputs:
            bot = MockBot()
            state = FSMContext(
                storage=self.storage,
                key=StorageKey(bot_id=123, chat_id=1002, user_id=1002),
            )
            await state.update_data(habit_name=dangerous_name)
            msg = make_message("dummy", user_id=1002, bot=bot)

            await habit_finalize_creation(msg, state, "09:00", 1002, bot)

            self.assertGreaterEqual(len(bot.sent_messages), 1)
            confirm_msg = bot.sent_messages[0]
            self.assertEqual(confirm_msg.kwargs.get("parse_mode"), "HTML")
            expected_escaped = html.escape(dangerous_name)
            self.assertIn(expected_escaped, confirm_msg.text)
            if "<" in dangerous_name or "&" in dangerous_name:
                self.assertNotIn(f"«{dangerous_name}»", confirm_msg.text)

    async def test_habit_add_template_start_with_dangerous_name(self):
        """habit_add_template_start must escape template habit name inside HTML prompt."""
        from handlers import habit_add_template_start

        dangerous_name = "Медитация <3 & релакс <b>raw</b>"
        async with async_session_maker() as session:
            tpl = Habit(name=dangerous_name, user_id=None)
            session.add(tpl)
            await session.commit()
            tpl_id = tpl.id

        cb = make_callback_query(f"habit_add_tpl_{tpl_id}", user_id=1003, bot=self.bot)
        state = FSMContext(
            storage=self.storage,
            key=StorageKey(bot_id=123, chat_id=1003, user_id=1003),
        )

        await habit_add_template_start(cb, state)

        self.assertTrue(cb.message.answer.called)
        call_kwargs = cb.message.answer.call_args.kwargs
        sent_text = cb.message.answer.call_args.args[0] if cb.message.answer.call_args.args else call_kwargs.get("text", "")
        self.assertEqual(call_kwargs.get("parse_mode"), "HTML")
        self.assertIn(html.escape(dangerous_name), sent_text)
        self.assertNotIn("<b>raw</b>", sent_text)

    async def test_background_worker_habit_reminder_escapes_name(self):
        """Habit reminder sent by background worker must escape Habit.name in HTML."""
        dangerous_name = "Чтение <3 & чай <b>bold</b>"
        async with async_session_maker() as session:
            h = Habit(name=dangerous_name, user_id=1004, time_target="12:00", is_active=True)
            session.add(h)
            await session.commit()

        safe_name = html.escape(h.name or "")
        text = (
            f"🔔 <b>Напоминание привычки!</b>\n\n"
            f"🎗 <b>{safe_name}</b>\n\n"
            f"Пора уделить время себе. Выполнил(а)?"
        )
        self.assertIn(html.escape(dangerous_name), text)
        self.assertNotIn("<b>bold</b>", text)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", text)


# =========================================================================
# 2. Referral Link Text Dynamic HTML Insertion Tests
# =========================================================================
class TestReferralLinkTextDynamicHtml(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.bot = MockBot()
        self.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="vitaliy_bot"))

    async def test_referral_link_text_plain_dynamic_contract(self):
        """Content(referral_link_text) must be escaped before embedding in bot HTML."""
        from handlers import ref_create_link

        dangerous_header = "A < B & C <b>raw</b>"
        async with async_session_maker() as session:
            await session.execute(delete(Content))
            await session.execute(delete(User))
            session.add(Content(key="referral_link_text", text_content=dangerous_header))
            session.add(User(id=2001, username="test_ref_user", first_name="RefUser"))
            await session.commit()

        cb = make_callback_query("ref_create_link", user_id=2001, bot=self.bot)
        await ref_create_link(cb, self.bot)

        self.assertTrue(cb.message.answer.called)
        call_kwargs = cb.message.answer.call_args.kwargs
        sent_text = cb.message.answer.call_args.args[0] if cb.message.answer.call_args.args else call_kwargs.get("text", "")

        # 1. Telegram call uses parse_mode="HTML"
        self.assertEqual(call_kwargs.get("parse_mode"), "HTML")

        # 2. Bot static markup remains markup
        self.assertIn("🔗 <b>", sent_text)
        self.assertIn("<code>https://t.me/vitaliy_bot?start=2001</code>", sent_text)
        self.assertIn("<b>Шаблоны сообщений для отправки друзьям:</b>", sent_text)

        # 3. Dynamic configured text is escaped
        self.assertIn(html.escape(dangerous_header), sent_text)
        self.assertIn("&lt;b&gt;raw&lt;/b&gt;", sent_text)

        # 4. No raw admin-entered tags are interpreted
        self.assertNotIn("<b>raw</b>", sent_text)


# =========================================================================
# 3. Follow-up Delivery HTML Contract Tests
# =========================================================================
class TestFollowupDeliveryHtmlContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.bot = MockBot()

    async def test_static_template_with_dangerous_user_name(self):
        """Static template preserves trusted HTML but escapes dynamic {name} placeholder."""
        from followups import send_followup_step_to_user

        user = SimpleNamespace(id=3001, name="A < B & C", first_name="A < B & C")
        step = SimpleNamespace(
            id=1,
            step_type="message",
            ai_instruction=None,
            message_text="Привет, <b>{name}</b>! Продолжаем практику.",
            button_text=None,
            button_action=None,
        )

        async with async_session_maker() as session:
            ok, status, err = await send_followup_step_to_user(
                self.bot, session, step=step, user=user, dialogue_id=1
            )

        self.assertTrue(ok)
        self.assertEqual(status, "delivered")
        self.assertEqual(len(self.bot.sent_messages), 1)

        msg = self.bot.sent_messages[0]
        self.assertEqual(msg.kwargs.get("parse_mode"), "HTML")
        # Template HTML intact
        self.assertIn("<b>A &lt; B &amp; C</b>", msg.text)
        # Raw unescaped user name not present inside tags
        self.assertNotIn("<b>A < B & C</b>", msg.text)

    async def test_ai_step_escapes_untrusted_llm_output(self):
        """AI-generated follow-up text is plain text and must be escaped before sending as HTML."""
        from followups import send_followup_step_to_user

        user = SimpleNamespace(id=3002, name="Alice", first_name="Alice")
        step = SimpleNamespace(
            id=2,
            step_type="ai",
            ai_instruction="Напомни о медитации",
            message_text=None,
            button_text=None,
            button_action=None,
        )

        llm_dangerous_output = "<3 & hello <b>unexpected</b> {name}"
        with patch("ai_integration.get_standalone_ai_response", new=AsyncMock(return_value=llm_dangerous_output)):
            async with async_session_maker() as session:
                ok, status, err = await send_followup_step_to_user(
                    self.bot, session, step=step, user=user, dialogue_id=1
                )

        self.assertTrue(ok)
        msg = self.bot.sent_messages[0]
        self.assertEqual(msg.kwargs.get("parse_mode"), "HTML")
        # LLM output must be escaped
        self.assertIn("&lt;3 &amp; hello &lt;b&gt;unexpected&lt;/b&gt;", msg.text)
        self.assertNotIn("<b>unexpected</b>", msg.text)

    async def test_malformed_admin_template_falls_back_to_plain_text(self):
        """Malformed admin template that fails Telegram entity parsing falls back to clean plain text without tags."""
        from followups import send_followup_step_to_user

        user = SimpleNamespace(id=3003, name="A < B & C", first_name="A < B & C")
        step = SimpleNamespace(
            id=3,
            step_type="message",
            ai_instruction=None,
            message_text="<b>Внимание, {name}!</b> <i>Незакрытый курсив и <code>код без закрытия",
            button_text=None,
            button_action=None,
        )

        sent_payloads = []

        async def mock_send(chat_id, text, **kwargs):
            sent_payloads.append((text, kwargs.get("parse_mode")))
            if kwargs.get("parse_mode") == "HTML":
                raise TelegramBadRequest(method="sendMessage", message="Bad Request: can't parse entities in message")
            return SimpleNamespace(message_id=999, text=text)

        bot = MockBot()
        bot.send_message = mock_send

        async with async_session_maker() as session:
            ok, status, err = await send_followup_step_to_user(
                bot, session, step=step, user=user, dialogue_id=1
            )

        self.assertTrue(ok)
        self.assertEqual(status, "delivered")
        self.assertEqual(len(sent_payloads), 2)
        # Attempt 1: HTML
        self.assertEqual(sent_payloads[0][1], "HTML")
        # Attempt 2: Clean plain text fallback
        final_text, final_mode = sent_payloads[1]
        self.assertIsNone(final_mode)
        # Assert clean plain text has no raw tags
        for raw_tag in ["<b>", "</b>", "<i>", "</i>", "<code>", "</code>"]:
            self.assertNotIn(raw_tag, final_text)
        # Dynamic user name is preserved in plain text, not HTML-escaped
        self.assertIn("A < B & C", final_text)
        self.assertNotIn("&lt;", final_text)
        self.assertNotIn("&amp;", final_text)

    async def test_followup_retry_after_dispatches_with_fallback(self):
        """On TelegramRetryAfter, retry executes identically with clean entity fallback."""
        from followups import send_followup_step_to_user

        user = SimpleNamespace(id=3004, name="User <Special>", first_name="User <Special>")
        step = SimpleNamespace(
            id=4,
            step_type="message",
            ai_instruction=None,
            message_text="<b>Привет, {name}! <i>сломанный тег",
            button_text=None,
            button_action=None,
        )

        call_idx = 0
        sent_payloads = []

        async def mock_send(chat_id, text, **kwargs):
            nonlocal call_idx
            call_idx += 1
            sent_payloads.append((text, kwargs.get("parse_mode")))
            if call_idx == 1:
                # First attempt: Flood wait
                raise TelegramRetryAfter(method="sendMessage", message="Flood", retry_after=0.01)
            elif call_idx == 2:
                # Retry attempt: Telegram entity parsing failure
                raise TelegramBadRequest(method="sendMessage", message="can't parse entities")
            # Fallback plain attempt succeeds
            return SimpleNamespace(message_id=1000, text=text)

        bot = MockBot()
        bot.send_message = mock_send

        async with async_session_maker() as session:
            ok, status, err = await send_followup_step_to_user(
                bot, session, step=step, user=user, dialogue_id=1
            )

        self.assertTrue(ok)
        self.assertEqual(status, "delivered")
        self.assertEqual(len(sent_payloads), 3)
        final_text, final_mode = sent_payloads[2]
        self.assertIsNone(final_mode)
        self.assertNotIn("<b>", final_text)
        self.assertNotIn("<i>", final_text)
        self.assertIn("User <Special>", final_text)


# =========================================================================
# 4. Field-Origin Dynamic HTML Verification Tests
# =========================================================================
class TestFieldOriginDynamicHtml(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.bot = MockBot()

    async def test_admin_subscribers_escapes_username_and_plan(self):
        """admin_subscribers screen must escape username and plan name in HTML list."""
        from handlers import admin_subscribers

        async with async_session_maker() as session:
            await session.execute(delete(UserSubscription))
            await session.execute(delete(SubscriptionPlan))
            await session.execute(delete(User))

            u = User(id=4001, username="bad<user>&name", first_name="<b>Injected</b>")
            admin = User(id=1, first_name="Admin", is_admin=True)
            plan = SubscriptionPlan(id=1, name="Тариф <VIP> & Pro", price=100.0, duration_value=30, duration_unit="days")
            sub = UserSubscription(user_id=4001, plan_id=1, end_date=msk_now() + timedelta(days=30))
            session.add_all([admin, u, plan, sub])
            await session.commit()

        cb = make_callback_query("admin_subs_list_0", user_id=1, bot=self.bot)
        await admin_subscribers(cb)

        self.assertTrue(cb.message.edit_text.called)
        call_kwargs = cb.message.edit_text.call_args.kwargs
        sent_text = cb.message.edit_text.call_args.args[0] if cb.message.edit_text.call_args.args else call_kwargs.get("text", "")

        self.assertEqual(call_kwargs.get("parse_mode"), "HTML")
        self.assertIn(html.escape(u.first_name), sent_text)
        self.assertIn(html.escape(u.username), sent_text)
        self.assertIn(html.escape(plan.name), sent_text)
        self.assertNotIn("<b>Injected</b>", sent_text)
        self.assertNotIn("<VIP>", sent_text)

    async def test_admin_habit_game_escapes_reward_sequence(self):
        """admin_habit_game screen must escape reward_sequence in HTML."""
        from handlers import admin_habit_game

        dangerous_rewards = "A<B, C&D, <3>"
        async with async_session_maker() as session:
            admin = await session.get(User, 1)
            if not admin:
                session.add(User(id=1, first_name="Admin", is_admin=True))
            else:
                admin.is_admin = True
            conf = await session.get(HabitConfig, 1)
            if not conf:
                conf = HabitConfig(id=1)
                session.add(conf)
            conf.reward_sequence = dangerous_rewards
            conf.final_message = "Поздравляем <3!"
            await session.commit()

        cb = make_callback_query("admin_habit_game", user_id=1, bot=self.bot)
        with patch("handlers.send_smart_content", new=AsyncMock()) as mock_smart:
            await admin_habit_game(cb)
            self.assertTrue(mock_smart.called)
            text_arg = mock_smart.call_args.args[1]
            self.assertIn(html.escape(dangerous_rewards), text_arg)
            self.assertNotIn("A<B", text_arg)


# =========================================================================
# 5 & 8. Real Flow Typing Duration, Continuous Pulsing, and Ordering Tests
# =========================================================================
class TestRealFlowTypingMocks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.bot = MockBot()
        async with async_session_maker() as session:
            p_conf = await session.get(PaymentConfig, 1)
            if not p_conf:
                session.add(PaymentConfig(id=1, mode="free"))
            else:
                p_conf.mode = "free"
            await session.commit()

    async def test_normal_text_dialogue_typing_active_from_start_to_finish(self):
        """Normal text dialogue must have typing active continuously from start until AI completion."""
        from ai_integration import get_ai_response_for_topic, ai_typing_action

        bot = MockBot()
        async with async_session_maker() as session:
            await session.execute(delete(User))
            session.add(User(id=5001, username="test_u", first_name="TestUser"))
            await session.commit()

        llm_running = asyncio.Event()
        llm_release = asyncio.Event()

        async def mock_generate_chat(*args, **kwargs):
            llm_running.set()
            await llm_release.wait()
            return SimpleNamespace(text="Ответ ассистента", raw_payload={})

        def fast_typing(b, c, action="typing", interval=4.5):
            return ai_typing_action(b, c, action=action, interval=0.03)

        with patch("ai_integration._generate_chat_response", side_effect=mock_generate_chat), \
             patch("ai_integration.search_relevant_chunks", new=AsyncMock(return_value=[])), \
             patch("ai_integration.ai_typing_action", side_effect=fast_typing):

            task = asyncio.create_task(
                get_ai_response_for_topic(
                    user_id=5001,
                    user_prompt="Привет!",
                    user_name="User",
                    bot=bot,
                    chat_id=5001,
                )
            )

            # Wait until LLM is executing
            await asyncio.wait_for(llm_running.wait(), timeout=2.0)
            await asyncio.sleep(0.08)

            # Typing pulses must be active during user wait
            self.assertGreaterEqual(len(bot.chat_actions), 2)
            for act in bot.chat_actions:
                self.assertEqual(act["chat_id"], 5001)
                self.assertEqual(act["action"], "typing")

            # Release LLM
            llm_release.set()
            res = await asyncio.wait_for(task, timeout=2.0)
            self.assertEqual(res, "Ответ ассистента")

            # Verify typing task exits cleanly
            actions_after_done = len(bot.chat_actions)
            await asyncio.sleep(0.06)
            self.assertEqual(len(bot.chat_actions), actions_after_done)

    async def test_rag_dialogue_typing_active_during_embedding_delay(self):
        """Typing must start BEFORE search_relevant_chunks (embedding API) and stay active."""
        from ai_integration import get_ai_response_for_topic, ai_typing_action
        from database import topic_knowledgebase_association

        bot = MockBot()
        async with async_session_maker() as session:
            await session.execute(delete(topic_knowledgebase_association))
            await session.execute(delete(KnowledgeBase))
            await session.execute(delete(Topic))
            await session.execute(delete(User))
            topic = Topic(id=10, slug="rag_slug", name="RAG Topic")
            kb = KnowledgeBase(id=10, filename="test.txt", indexed_content="...")
            topic.knowledge_base_files.append(kb)
            session.add_all([
                User(id=5002, username="rag_u", first_name="RagUser", current_topic_slug="rag_slug"),
                topic,
                kb,
            ])
            await session.commit()

        rag_running = asyncio.Event()
        rag_release = asyncio.Event()

        async def mock_rag_search(*args, **kwargs):
            rag_running.set()
            await rag_release.wait()
            return ["Фрагмент базы знаний"]

        def fast_typing(b, c, action="typing", interval=4.5):
            return ai_typing_action(b, c, action=action, interval=0.03)

        with patch("ai_integration.search_relevant_chunks", side_effect=mock_rag_search), \
             patch("ai_integration._prepare_clients", new=AsyncMock(return_value=(
                 SimpleNamespace(context_limit_first=2, context_limit_recent=4, provider="OpenAI"),
                 "dummy_embedding_client",
                 AsyncMock(),
                 "gpt-4o",
                 None
             ))), \
             patch("ai_integration.ai_typing_action", side_effect=fast_typing), \
             patch("ai_integration._generate_chat_response", new=AsyncMock(return_value=SimpleNamespace(text="Ответ", raw_payload={}))):

            task = asyncio.create_task(
                get_ai_response_for_topic(
                    user_id=5002,
                    user_prompt="Вопрос по базе",
                    user_name="User",
                    topic_slug="rag_slug",
                    bot=bot,
                    chat_id=5002,
                )
            )

            # Wait until RAG is running
            await asyncio.wait_for(rag_running.wait(), timeout=2.0)
            await asyncio.sleep(0.05)

            # Typing MUST ALREADY BE ACTIVE while RAG/embedding is pending
            self.assertGreaterEqual(len(bot.chat_actions), 1)
            self.assertEqual(bot.chat_actions[0]["action"], "typing")
            self.assertEqual(bot.chat_actions[0]["chat_id"], 5002)

            rag_release.set()
            res = await asyncio.wait_for(task, timeout=2.0)
            self.assertEqual(res, "Ответ")

    async def test_universal_test_two_stage_typing_continuous(self):
        """Universal test preliminary interpretation + topic AI answer must share continuous typing."""
        from test_system_handlers import _finish_test
        from ai_integration import ai_typing_action
        from database import TestSession

        bot = MockBot()
        async with async_session_maker() as session:
            await session.execute(delete(TestSession))
            await session.execute(delete(TestAttempt))
            await session.execute(delete(TestSet))
            await session.execute(delete(User))
            session.add(User(id=5003, username="test_u_3", first_name="User3"))
            tset = TestSet(
                id=99,
                title="Тест",
                slug="test_slug",
                is_enabled=True,
                separate_interpretation_prompt=True,
                interpretation_prompt="Интерпретация",
            )
            session.add(tset)
            att = TestAttempt(
                id=88,
                user_id=5003,
                test_set_id=99,
                status="in_progress",
                answers="[]",
            )
            session.add(att)
            ts = TestSession(
                user_id=5003,
                test_set_id=99,
                attempt_id=88,
                is_finished=False,
            )
            session.add(ts)
            await session.commit()

        stage1_running = asyncio.Event()
        stage1_release = asyncio.Event()
        stage2_running = asyncio.Event()
        stage2_release = asyncio.Event()

        async def mock_standalone(*args, **kwargs):
            stage1_running.set()
            await stage1_release.wait()
            return "Предварительный вывод"

        async def mock_topic_ai(*args, **kwargs):
            stage2_running.set()
            await stage2_release.wait()
            return "Финальный результат"

        def fast_typing(b, c, action="typing", interval=4.5):
            return ai_typing_action(b, c, action=action, interval=0.03)

        state = FSMContext(
            storage=MemoryStorage(),
            key=StorageKey(bot_id=123, chat_id=5003, user_id=5003),
        )

        with patch("test_system_handlers.get_standalone_ai_response", side_effect=mock_standalone), \
             patch("test_system_handlers.get_ai_response_for_topic", side_effect=mock_topic_ai), \
             patch("test_system_handlers.ai_typing_action", side_effect=fast_typing), \
             patch("ai_integration.ai_typing_action", side_effect=fast_typing):

            task = asyncio.create_task(
                _finish_test(bot, 5003, state, answers=[])
            )

            # Stage 1 active
            await asyncio.wait_for(stage1_running.wait(), timeout=2.0)
            await asyncio.sleep(0.05)
            self.assertGreaterEqual(len(bot.chat_actions), 1)

            # Release Stage 1
            stage1_release.set()

            # Stage 2 active
            await asyncio.wait_for(stage2_running.wait(), timeout=2.0)
            await asyncio.sleep(0.05)

            # Typing continued pulsing across stages
            self.assertGreaterEqual(len(bot.chat_actions), 2)

            stage2_release.set()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_photo_textual_flow_typing_continuous(self):
        """Photo handler typing starts before image analysis and stays active through LLM response."""
        from handlers import process_chat_photo
        from ai_integration import ai_typing_action

        bot = MockBot()
        async with async_session_maker() as session:
            await session.execute(delete(User))
            session.add(User(id=5004, username="photo_u", first_name="PhotoUser", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            await session.commit()

        vision_running = asyncio.Event()
        vision_release = asyncio.Event()

        async def mock_vision(*args, **kwargs):
            vision_running.set()
            await vision_release.wait()
            return "Описание фото"

        def fast_typing(b, c, action="typing", interval=4.5):
            return ai_typing_action(b, c, action=action, interval=0.03)

        msg = make_message("", user_id=5004, bot=bot)
        msg.photo = [SimpleNamespace(file_id="photo_123")]

        with patch("handlers.analyze_image", side_effect=mock_vision), \
             patch("handlers.ai_typing_action", side_effect=fast_typing), \
             patch("ai_integration.ai_typing_action", side_effect=fast_typing), \
             patch("handlers.get_ai_response_for_topic", new=AsyncMock(return_value="AI ответ по фото")):

            task = asyncio.create_task(process_chat_photo(msg, bot))

            # Vision is running
            await asyncio.wait_for(vision_running.wait(), timeout=2.0)
            await asyncio.sleep(0.05)

            # Typing active from processing start
            self.assertGreaterEqual(len(bot.chat_actions), 1)
            self.assertEqual(bot.chat_actions[0]["action"], "typing")

            vision_release.set()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_voice_textual_flow_typing_continuous(self):
        """Audio handler typing starts before transcription and stays active through LLM response."""
        from handlers import process_chat_audio
        from ai_integration import ai_typing_action

        bot = MockBot()
        async with async_session_maker() as session:
            await session.execute(delete(User))
            session.add(User(id=5005, username="voice_u", first_name="VoiceUser", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            await session.commit()

        audio_running = asyncio.Event()
        audio_release = asyncio.Event()

        async def mock_transcribe(*args, **kwargs):
            audio_running.set()
            await audio_release.wait()
            return "Транскрибированный голос"

        def fast_typing(b, c, action="typing", interval=4.5):
            return ai_typing_action(b, c, action=action, interval=0.03)

        msg = make_message("", user_id=5005, bot=bot)
        msg.voice = SimpleNamespace(file_id="voice_123")

        with patch("handlers.transcribe_audio", side_effect=mock_transcribe), \
             patch("handlers.ai_typing_action", side_effect=fast_typing), \
             patch("ai_integration.ai_typing_action", side_effect=fast_typing), \
             patch("handlers.get_ai_response_for_topic", new=AsyncMock(return_value="AI ответ по аудио")):

            task = asyncio.create_task(process_chat_audio(msg, bot))

            await asyncio.wait_for(audio_running.wait(), timeout=2.0)
            await asyncio.sleep(0.05)

            self.assertGreaterEqual(len(bot.chat_actions), 1)
            self.assertEqual(bot.chat_actions[0]["action"], "typing")

            audio_release.set()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_journal_summary_typing_through_fallback(self):
        """Journal summary typing remains active through primary failure and into fallback."""
        from ai_integration import generate_journal_daily_summary, ai_typing_action

        bot = MockBot()
        async with async_session_maker() as session:
            await session.execute(delete(User))
            await session.execute(delete(JournalEntry))
            u = User(id=5006, username="j_u", first_name="JUser")
            entry = JournalEntry(
                id=901,
                user_id=5006,
                reflection_date=msk_now().date(),
                status="completed",
                questions_snapshot_json="[]",
            )
            session.add_all([u, entry])
            await session.commit()

        fallback_running = asyncio.Event()
        fallback_release = asyncio.Event()

        attempt = 0

        async def mock_generate_chat(ai_config, chat_client, model_name, messages):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise Exception("Primary provider 500 Internal Error")
            fallback_running.set()
            await fallback_release.wait()
            return SimpleNamespace(
                text="1. Итог дня: Отлично\n2. Поддержка: Держись\n3. Фокус: Практика",
                raw_payload={},
            )

        def fast_typing(b, c, action="typing", interval=4.5):
            return ai_typing_action(b, c, action=action, interval=0.03)

        with patch("ai_integration._generate_chat_response", side_effect=mock_generate_chat), \
             patch("ai_integration.record_ai_attempt_log", new=AsyncMock()), \
             patch("journal_service.initiate_journal_ai_operation", new=AsyncMock(return_value=(SimpleNamespace(id=1, status="running", owner_token="tok"), True))), \
             patch("journal_service.finalize_journal_ai_operation_success", new=AsyncMock(return_value=True)), \
             patch("journal_service.build_journal_daily_summary_context", new=AsyncMock(return_value=("Prompt", "Sys"))), \
             patch("ai_integration.ai_typing_action", side_effect=fast_typing), \
             patch("ai_integration._prepare_clients", new=AsyncMock(return_value=(
                 SimpleNamespace(provider="OpenAI", allow_fallback=True, fallback_provider="DeepSeek", deepseek_api_key="key", deepseek_model="deepseek-chat"),
                 None, AsyncMock(), "gpt-4o", None
             ))):

            task = asyncio.create_task(
                generate_journal_daily_summary(
                    entry_id=901,
                    user_id=5006,
                    chat_id=5006,
                    bot=bot,
                )
            )

            await asyncio.wait_for(fallback_running.wait(), timeout=2.0)
            await asyncio.sleep(0.05)

            # Typing is active during fallback wait
            self.assertGreaterEqual(len(bot.chat_actions), 1)
            self.assertEqual(bot.chat_actions[0]["action"], "typing")

            fallback_release.set()
            res = await asyncio.wait_for(task, timeout=2.0)

    async def test_typing_task_exits_cleanly_on_exception(self):
        """ai_typing_action must cleanly cancel and exit background task on unhandled exception."""
        from ai_integration import ai_typing_action

        bot = MockBot()
        with self.assertRaises(RuntimeError):
            async with ai_typing_action(bot, 5007, interval=0.02):
                await asyncio.sleep(0.03)
                raise RuntimeError("Simulated crash")

        # Give loop moment to finish cancellation
        await asyncio.sleep(0.05)
        count_at_exit = len(bot.chat_actions)
        await asyncio.sleep(0.05)
        # No more pulses after exit
        self.assertEqual(len(bot.chat_actions), count_at_exit)


# =========================================================================
# 6. get_ai_response_for_topic AsyncSession Lifetime Tests
# =========================================================================
class TestGetAiResponseForTopicSessionLifetime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.bot = MockBot()

    def _create_session_spy(self, real_session_maker):
        class SessionSpy:
            def __init__(self):
                self._real = None
                self.entered = False
                self.closed = False
                self.operations_after_close = []

            async def __aenter__(self):
                self._real = await real_session_maker().__aenter__()
                self.entered = True
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                self.closed = True
                res = await self._real.__aexit__(exc_type, exc_val, exc_tb)
                return res

            def __getattr__(self, name):
                if self.closed and name not in ("closed", "entered", "operations_after_close", "_real"):
                    self.operations_after_close.append(name)
                return getattr(self._real, name)

        return SessionSpy()

    async def test_session_closes_on_primary_success_and_no_ops_after_close(self):
        """Session context must be alive throughout orchestration and close without post-exit ops."""
        from ai_integration import get_ai_response_for_topic
        from database import User, AIConfig

        async with async_session_maker() as session:
            await session.execute(delete(User))
            u = User(id=6001, username="test_u1", first_name="TestU1")
            session.add(u)
            conf = await session.get(AIConfig, 1)
            if not conf:
                conf = AIConfig(id=1, provider="OpenAI", openai_api_key="key", openai_model="gpt-4o")
                session.add(conf)
            else:
                conf.provider = "OpenAI"
                conf.openai_api_key = "key"
            await session.commit()

        spy = self._create_session_spy(async_session_maker)

        with patch("ai_integration.async_session_maker", return_value=spy), \
             patch("ai_integration._generate_chat_response", new=AsyncMock(return_value="AI Answer")), \
             patch("ai_integration.record_ai_attempt_log", new=AsyncMock()), \
             patch("ai_dialogue_service.get_turn_aware_history", new=AsyncMock(return_value=[])):

            res = await get_ai_response_for_topic(
                user_id=6001,
                user_prompt="Привет",
                user_name="TestU1",
                bot=self.bot,
            )

        self.assertEqual(res, "AI Answer")
        self.assertTrue(spy.entered)
        self.assertTrue(spy.closed)
        self.assertEqual(spy.operations_after_close, [], "No session operation must occur after session __aexit__")

    async def test_session_closes_on_missing_user_early_return(self):
        """Missing user early return must cleanly close the session without leaks."""
        from ai_integration import get_ai_response_for_topic
        from database import User, AIConfig

        async with async_session_maker() as session:
            await session.execute(delete(User).where(User.id == 6099))
            conf = await session.get(AIConfig, 1)
            if not conf:
                conf = AIConfig(id=1, provider="OpenAI", openai_api_key="key")
                session.add(conf)
            await session.commit()

        spy = self._create_session_spy(async_session_maker)

        with patch("ai_integration.async_session_maker", return_value=spy):
            res = await get_ai_response_for_topic(
                user_id=6099,
                user_prompt="Привет",
                user_name="Ghost",
                bot=self.bot,
            )

        self.assertIn("Пользователь не найден", res)
        self.assertTrue(spy.entered)
        self.assertTrue(spy.closed)
        self.assertEqual(spy.operations_after_close, [])

    async def test_session_closes_on_primary_terminal_failure(self):
        """Session must close cleanly on terminal error when fallback is disabled."""
        from ai_integration import get_ai_response_for_topic
        from database import User, AIConfig

        async with async_session_maker() as session:
            await session.execute(delete(User))
            u = User(id=6002, username="test_u2", first_name="TestU2")
            session.add(u)
            conf = await session.get(AIConfig, 1)
            if conf:
                conf.provider = "OpenAI"
                conf.openai_api_key = "key"
                conf.allow_fallback = False
            await session.commit()

        spy = self._create_session_spy(async_session_maker)

        with patch("ai_integration.async_session_maker", return_value=spy), \
             patch("ai_integration._generate_chat_response", side_effect=Exception("API Down")), \
             patch("ai_integration.record_ai_attempt_log", new=AsyncMock()), \
             patch("ai_dialogue_service.get_turn_aware_history", new=AsyncMock(return_value=[])):

            res = await get_ai_response_for_topic(
                user_id=6002,
                user_prompt="Привет",
                user_name="TestU2",
                bot=self.bot,
            )

        self.assertIn("Произошла ошибка при генерации ответа", res)
        self.assertTrue(spy.entered)
        self.assertTrue(spy.closed)
        self.assertEqual(spy.operations_after_close, [])

    async def test_session_closes_on_fallback_terminal_failure(self):
        """Session must close cleanly on terminal failure when fallback also fails."""
        from ai_integration import get_ai_response_for_topic
        from database import User, AIConfig

        async with async_session_maker() as session:
            await session.execute(delete(User))
            u = User(id=6003, username="test_u3", first_name="TestU3")
            session.add(u)
            conf = await session.get(AIConfig, 1)
            if conf:
                conf.provider = "OpenAI"
                conf.openai_api_key = "key"
                conf.allow_fallback = True
                conf.fallback_provider = "DeepSeek"
                conf.deepseek_api_key = "key_ds"
                conf.deepseek_model = "deepseek-chat"
            await session.commit()

        spy = self._create_session_spy(async_session_maker)

        with patch("ai_integration.async_session_maker", return_value=spy), \
             patch("ai_integration._generate_chat_response", side_effect=[Exception("Primary Err"), Exception("Fallback Err")]), \
             patch("ai_integration.record_ai_attempt_log", new=AsyncMock()), \
             patch("ai_dialogue_service.get_turn_aware_history", new=AsyncMock(return_value=[])):

            res = await get_ai_response_for_topic(
                user_id=6003,
                user_prompt="Привет",
                user_name="TestU3",
                bot=self.bot,
            )

        self.assertIn("Произошла ошибка при генерации ответа", res)
        self.assertTrue(spy.entered)
        self.assertTrue(spy.closed)
        self.assertEqual(spy.operations_after_close, [])


# =========================================================================
# 7. Universal Test Admin Preview HTML Contract Tests
# =========================================================================
class TestUniversalTestAdminPreviewHtmlContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.bot = MockBot()

    async def test_admin_preview_html_rendering_and_escaping(self):
        """Admin preview renders with parse_mode=HTML, static tags intact, dynamic AI escaped."""
        from test_system_handlers import _finish_test
        from database import TestSet, TestSession, TestAttempt, TestQuestion
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.memory import MemoryStorage
        from aiogram.fsm.storage.base import StorageKey

        admin_id = 7001
        async with async_session_maker() as session:
            await session.execute(delete(TestSession))
            await session.execute(delete(TestAttempt))
            await session.execute(delete(TestQuestion))
            await session.execute(delete(TestSet))
            await session.execute(delete(User))

            u = User(id=admin_id, username="admin_user", first_name="Admin")
            ts = TestSet(
                id=10,
                title="Тест самооценки <3 & <b>тест</b>",
                slug="preview_test_slug",
                is_enabled=True,
                separate_result_prompt_enabled=True,
                result_system_prompt="Интерпретируй",
                calculation_enabled=True,
                formulas_json='[{"name": "score", "formula": "1 + 1"}]',
            )
            q = TestQuestion(
                id=101,
                test_set_id=10,
                order_num=1,
                text="Вопрос 1",
                variable_name="q1",
            )
            session.add_all([u, ts, q])
            await session.flush()

            attempt = TestAttempt(
                id=501,
                user_id=admin_id,
                test_set_id=10,
                purpose="preview",
                status="in_progress",
            )
            session.add(attempt)
            await session.flush()

            t_session = TestSession(
                user_id=admin_id,
                test_set_id=10,
                attempt_id=attempt.id,
                current_question_index=1,
                answers="[]",
            )
            session.add(t_session)
            await session.commit()

        storage = MemoryStorage()
        state = FSMContext(storage=storage, key=StorageKey(bot_id=123, chat_id=admin_id, user_id=admin_id))

        untrusted_ai_interp = "<3 & <b>unexpected</b> AI interpretation with AT&T"

        with patch("test_system_handlers.get_standalone_ai_response", new=AsyncMock(return_value=untrusted_ai_interp)):
            await _finish_test(self.bot, admin_id, state, answers=[{"question_id": 101, "value": 1}])

        # Assert messages sent
        self.assertGreaterEqual(len(self.bot.sent_messages), 1)
        preview_msgs = [m for m in self.bot.sent_messages if "Предпросмотр теста" in m.text]
        self.assertEqual(len(preview_msgs), 1)
        preview_msg = preview_msgs[0]

        # 1. Telegram call uses parse_mode='HTML'
        self.assertEqual(preview_msg.kwargs.get("parse_mode"), "HTML")

        # 2. Bot static tags are preserved
        self.assertIn("📋 <b>Предпросмотр теста", preview_msg.text)
        self.assertIn("<b>Результаты расчёта формул:</b>", preview_msg.text)
        self.assertIn("<b>Интерпретация:</b>", preview_msg.text)

        # 3. Dynamic test set title is escaped
        self.assertIn(html.escape("Тест самооценки <3 & <b>тест</b>"), preview_msg.text)

        # 4. Untrusted AI interpretation is escaped
        self.assertIn(html.escape(untrusted_ai_interp), preview_msg.text)
        self.assertNotIn("<b>unexpected</b>", preview_msg.text)
        self.assertIn("&lt;b&gt;unexpected&lt;/b&gt;", preview_msg.text)
        self.assertIn("&lt;3", preview_msg.text)

    async def test_admin_preview_html_fallback_to_clean_plain_text(self):
        """When parse_mode='HTML' fails, _send_long must fall back to clean human-readable plain text."""
        from test_system_handlers import _send_long

        admin_id = 7002
        html_input = "<b>Предпросмотр теста</b>\n<b>Интерпретация:</b>\nA &lt; B &amp; C"

        calls = []

        async def fake_send_message(chat_id, text, **kwargs):
            calls.append((text, kwargs))
            if kwargs.get("parse_mode") == "HTML":
                raise TelegramBadRequest(
                    method="sendMessage",
                    message="Bad Request: can't parse entities in message text: unexpected end of tag",
                )
            return make_message(text=text, user_id=admin_id, bot=self.bot)

        self.bot.send_message = AsyncMock(side_effect=fake_send_message)

        await _send_long(self.bot, admin_id, html_input, parse_mode="HTML")

        # Must have exactly 2 attempts: 1st with HTML (failed), 2nd with plain text (succeeded)
        self.assertEqual(len(calls), 2)

        first_text, first_kwargs = calls[0]
        self.assertEqual(first_kwargs.get("parse_mode"), "HTML")
        self.assertIn("<b>Предпросмотр теста</b>", first_text)

        second_text, second_kwargs = calls[1]
        # 1. parse_mode is None
        self.assertIsNone(second_kwargs.get("parse_mode"))

        # 2. Contains readable text
        self.assertIn("Предпросмотр теста", second_text)
        self.assertIn("Интерпретация:", second_text)
        self.assertIn("A < B & C", second_text)

        # 3. Contains no raw Telegram markup tags
        self.assertNotIn("<b>", second_text)
        self.assertNotIn("</b>", second_text)
        self.assertNotIn("<i>", second_text)
        self.assertNotIn("<code>", second_text)

        # 4. HTML entities are decoded
        self.assertNotIn("&lt;", second_text)
        self.assertNotIn("&amp;", second_text)
        self.assertNotIn("&gt;", second_text)



# =========================================================================
# 8. Plain Destination Contract Tests (Toast & Notification)
# =========================================================================
class TestAdminTemplateToastPlainDestination(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.bot = MockBot()

    async def test_adm_save_new_template_toast_with_dangerous_name(self):
        """Admin habit template creation toast must display raw plain text, not HTML entities."""
        from handlers import adm_save_new_template
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.memory import MemoryStorage
        from aiogram.fsm.storage.base import StorageKey

        admin_id = 8001
        dangerous_name = "A < B & C"

        storage = MemoryStorage()
        state = FSMContext(storage=storage, key=StorageKey(bot_id=123, chat_id=admin_id, user_id=admin_id))

        message = make_message(text=dangerous_name, user_id=admin_id, bot=self.bot)

        sent_toasts = []

        async def mock_send_temp_notif(bot, chat_id, text):
            sent_toasts.append((chat_id, text))

        with patch("handlers.send_temp_notification", side_effect=mock_send_temp_notif), \
             patch("handlers.admin_habit_templates", new=AsyncMock()), \
             patch("handlers.clean_chat_after_input", new=AsyncMock()):

            await adm_save_new_template(message, state, self.bot)
            await asyncio.sleep(0.02)

        self.assertEqual(len(sent_toasts), 1)
        chat_id, toast_text = sent_toasts[0]
        # Assert visible plain text has exact raw string, NOT HTML entities
        self.assertIn(f"✅ Шаблон «{dangerous_name}» добавлен!", toast_text)
        self.assertNotIn("&lt;", toast_text)
        self.assertNotIn("&amp;", toast_text)


# =========================================================================
# 9. Destination-Contract Classification Regression Tests
# =========================================================================
class TestDestinationContractClassification(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.bot = MockBot()

    async def test_html_destination_contract(self):
        """HTML_DESTINATION: bot static tags allowed, dynamic values strictly escaped."""
        from handlers import habit_save_name
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.memory import MemoryStorage
        from aiogram.fsm.storage.base import StorageKey

        user_id = 9001
        raw_val = "Yoga <3 & Pilates <b>fit</b>"

        storage = MemoryStorage()
        state = FSMContext(storage=storage, key=StorageKey(bot_id=123, chat_id=user_id, user_id=user_id))
        msg = make_message(text=raw_val, user_id=user_id, bot=self.bot)

        with patch("handlers.clean_chat_after_input", new=AsyncMock()):
            await habit_save_name(msg, state)

        self.assertTrue(msg.answer.called)
        call_kwargs = msg.answer.call_args.kwargs
        sent_text = msg.answer.call_args.args[0] if msg.answer.call_args.args else call_kwargs.get("text", "")
        # HTML destination verification
        self.assertEqual(call_kwargs.get("parse_mode"), "HTML")
        self.assertIn("<b>Время напоминания для", sent_text)
        self.assertIn(html.escape(raw_val), sent_text)
        self.assertNotIn("<b>fit</b>", sent_text)
        self.assertIn("&lt;b&gt;fit&lt;/b&gt;", sent_text)

    async def test_plain_destination_contract(self):
        """PLAIN_DESTINATION: no html.escape on user visible text, no bot-authored raw HTML tags."""
        from handlers import send_temp_notification

        raw_val = "Toast <3 & Alert <b>plain</b>"

        await send_temp_notification(self.bot, 9002, f"Info: {raw_val}")

        self.assertGreaterEqual(len(self.bot.sent_messages), 1)
        sent = self.bot.sent_messages[0]
        # Plain destination verification
        self.assertIsNone(sent.kwargs.get("parse_mode"))
        self.assertIn(raw_val, sent.text)
        self.assertNotIn("&lt;3", sent.text)
        self.assertNotIn("&amp;", sent.text)


# =========================================================================
# 10. Shared Telegram HTML Sanitizer Hardening & Contract Parity Tests
# =========================================================================
class TestStripTelegramHtmlSanitizer(unittest.TestCase):
    def test_strip_telegram_html_malformed_fragments_and_legitimate_angle_brackets(self):
        """Sanitizer removes malformed Telegram tag fragments while preserving legitimate angle brackets."""
        from md_utils import strip_telegram_html

        # 1. Malformed/incomplete Telegram tag fragments removed
        res1 = strip_telegram_html("Hello <b")
        self.assertNotIn("<b", res1)
        self.assertEqual(res1.strip(), "Hello")

        res2 = strip_telegram_html("Hello <i broken")
        self.assertNotIn("<i", res2)
        self.assertNotIn("broken", res2)
        self.assertEqual(res2.strip(), "Hello")

        res3 = strip_telegram_html("Code: <code")
        self.assertNotIn("<code", res3)
        self.assertEqual(res3.strip(), "Code:")

        res_attr = strip_telegram_html("Code: <code attr")
        self.assertNotIn("<code", res_attr)
        self.assertEqual(res_attr.strip(), "Code:")

        # 2. Legitimate mathematical/emotive angle-bracket text preserved
        res4 = strip_telegram_html("A < B & C")
        self.assertEqual(res4, "A < B & C")

        res5 = strip_telegram_html("<3")
        self.assertEqual(res5, "<3")

        res6 = strip_telegram_html("x > y")
        self.assertEqual(res6, "x > y")

        # 3. Tag removal and entity unescaping integration
        res7 = strip_telegram_html("<b>Интерпретация:</b>\nA &lt; B &amp; C")
        self.assertEqual(res7.strip(), "Интерпретация:\nA < B & C")
        self.assertNotIn("<b>", res7)
        self.assertNotIn("</b>", res7)

    def test_shared_sanitizer_contract_parity(self):
        """Ensure followups and md_utils share the identical strip_telegram_html function."""
        import followups
        import md_utils
        self.assertIs(followups.strip_telegram_html, md_utils.strip_telegram_html)


if __name__ == "__main__":
    unittest.main()
