import asyncio
import json
import os
import sys
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramBadRequest

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/vitaliy_v6_test.db")
sys.modules.setdefault("vector_store", SimpleNamespace(get_collection=lambda: None, get_active_path=lambda: None))

import test_set_admin_handlers
import topic_admin_handlers
import test_system_handlers
import universal_tests

from ai_dialogue_service import (
    acquire_pre_test_claim,
    allocate_global_dialogue_id,
    allocate_next_dialogue_id,
    build_ai_user_profile_context,
    claim_summary_generation,
    clear_pending_ai_flow,
    complete_pre_test_claim,
    deliver_prerecorded_first_message,
    finish_summary_claim,
    generate_session_summary,
    get_active_topic_dialogue_id,
    get_pending_ai_flow,
    get_turn_aware_history,
    renew_summary_claim_lease,
    renew_pre_test_claim_lease,
    set_active_topic_dialogue_id,
    store_pending_ai_flow,
    consume_pending_ai_flow,
    get_pre_test_claim,
    TopicContextSnapshot,
)
from sqlalchemy import select, func, update, delete
from ai_integration import get_ai_response_for_topic
from database import (
    AIConfig,
    Base,
    Content,
    ContentMedia,
    FunnelState,
    FunnelLanding,
    Message as DBMessage,
    PaymentConfig,
    PendingAiFlow,
    PreTestClaim,
    SessionSummary,
    SummaryClaim,
    TestAttempt,
    TestQuestion,
    TestSession,
    TestSet,
    Topic,
    TopicDialogueConfig,
    TopicPrerecordedDelivery,
    MenuButton,
    User,
    UserTopicSession,
    async_session_maker,
    engine,
    init_db,
)
import database
import handlers
import funnel
import keyboards as kb
from time_utils import msk_now


class MockBot:
    def __init__(self):
        self.sent_messages = []
        self.sent_photos = []
        self.edited_messages = []
        self.edited_reply_markups = []
        self.deleted_messages = []

    async def send_message(self, chat_id, text, **kwargs):
        msg = SimpleNamespace(
            chat_id=chat_id,
            chat=SimpleNamespace(id=chat_id),
            message_id=len(self.sent_messages) + 1,
            text=text,
            kwargs=kwargs,
            reply_markup=kwargs.get("reply_markup"),
            delete=AsyncMock(),
        )
        self.sent_messages.append(msg)
        return msg

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        msg = SimpleNamespace(
            chat_id=chat_id,
            chat=SimpleNamespace(id=chat_id),
            message_id=len(self.sent_messages) + 1,
            text=caption,
            caption=caption,
            photo=photo,
            kwargs=kwargs,
            reply_markup=kwargs.get("reply_markup"),
            delete=AsyncMock(),
        )
        self.sent_photos.append(msg)
        self.sent_messages.append(msg)
        return msg

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        entry = SimpleNamespace(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            kwargs=kwargs,
            reply_markup=kwargs.get("reply_markup"),
        )
        self.edited_messages.append(entry)
        return entry

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, **kwargs):
        if getattr(self, "fail_reply_markup_edit", False):
            raise TelegramBadRequest(method="editMessageReplyMarkup", message="message to edit not found")
        self.edited_reply_markups.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": kwargs.get("reply_markup"),
        })

    async def get_me(self):
        return SimpleNamespace(username="test_vitaliy_bot")


class StateStore:
    def __init__(self):
        self.data = {}
        self.current_state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **values):
        self.data.update(values)
        return dict(self.data)

    async def set_state(self, state):
        self.current_state = getattr(state, "state", state)

    async def get_state(self):
        return self.current_state

    async def clear(self):
        self.data.clear()
        self.current_state = None


class AiDialoguePlatformV6Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await init_db()
        self.bot = MockBot()

    # =========================================================================
    # GUARD 1: Disclaimer Acceptance Must Resume Original Flow
    # =========================================================================

    async def test_guard1_disclaimer_continuation_topic(self):
        user_id = 20001
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="User1", ai_disclaimer_accepted_at=None))
            await session.commit()

        # Step 1: User tries to enter "stress" topic without disclaimer
        state = AsyncMock()
        event = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="User1"),
            chat=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
        )
        await handlers.activate_ai_mode(event, "stress", state, self.bot)

        # Assert: Disclaimer prompt sent with consent keyboard
        self.assertTrue(len(self.bot.sent_messages) >= 1)
        last_msg = self.bot.sent_messages[-1]
        self.assertIsNotNone(last_msg.reply_markup)

        # Assert: Pending flow recorded in DB
        pending = await get_pending_ai_flow(user_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.flow_type, "thematic")
        self.assertEqual(pending.payload.get("topic_slug"), "stress")

        # Step 2: User clicks "✅ Принимаю" (cb_accept_disclaimer)
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="User1"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock(), answer=AsyncMock()),
            data="cb_accept_disclaimer",
            answer=AsyncMock(),
        )
        await handlers.cb_accept_disclaimer(cb, state, self.bot)

        # Assert: User in DB now has disclaimer accepted
        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertIsNotNone(u.ai_disclaimer_accepted_at)
            self.assertEqual(u.ai_disclaimer_version, 1)

        # Assert: Pending flow cleared
        pending_after = await get_pending_ai_flow(user_id)
        self.assertIsNone(pending_after)

        # Assert: User entered state chatting with current_topic_slug = "stress"
        state.set_state.assert_called_with(handlers.UserStates.chatting)

    async def test_guard1_disclaimer_continuation_diagnostics(self):
        user_id = 20002
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="User2", ai_disclaimer_accepted_at=None))
            await session.commit()

        state = AsyncMock()
        event = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="User2"),
            chat=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
        )
        await handlers.activate_ai_mode(event, "diagnostics", state, self.bot)

        pending = await get_pending_ai_flow(user_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.flow_type, "diagnostics")

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="User2"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock(), answer=AsyncMock()),
            data="cb_accept_disclaimer",
            answer=AsyncMock(),
        )
        with patch("handlers.start_universal_test", new=AsyncMock()) as mock_start_test:
            await handlers.cb_accept_disclaimer(cb, state, self.bot)
            mock_start_test.assert_not_called()
            setup_messages = [
                message for message in self.bot.sent_messages
                if message.text and "Анкета настраивается" in message.text
            ]
            self.assertTrue(setup_messages)

    async def test_guard1_disclaimer_continuation_funnel_reflection_preserves_text(self):
        user_id = 20003
        reflection_text = "Я почувствовал невероятную ясность ума и спокойствие."
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="User3", ai_disclaimer_accepted_at=None, current_dialogue_id=1))
            st = FunnelState(user_id=user_id, stage="waiting_reflection", is_active=True, meditation_id=1)
            session.add(st)
            await session.commit()

        state = AsyncMock()
        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="User3"),
            chat=SimpleNamespace(id=user_id),
            text=reflection_text,
            bot=self.bot,
        )

        # User submits reflection message
        await funnel.funnel_reflection_message(msg, state)

        # Assert: Disclaimer requested, reflection text preserved in PendingAiFlow
        pending = await get_pending_ai_flow(user_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.flow_type, "funnel_reflection")
        self.assertEqual(pending.payload.get("reflection_text"), reflection_text)

        # User clicks "✅ Принимаю"
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="User3"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock(), answer=AsyncMock()),
            data="cb_accept_disclaimer",
            answer=AsyncMock(),
        )
        with patch("funnel.get_ai_response_for_topic", new=AsyncMock(return_value="Глубокий опыт.")) as mock_ai:
            await handlers.cb_accept_disclaimer(cb, state, self.bot)
            mock_ai.assert_called_once()
            # Assert prompt passed to AI contains the preserved reflection text
            called_prompt = mock_ai.call_args[0][1]
            self.assertIn(reflection_text, called_prompt)

        # Assert pending flow cleared
        self.assertIsNone(await get_pending_ai_flow(user_id))

    async def test_guard1_funnel_strategy_requires_disclaimer_preserves_message(self):
        """P0-1: Direct menu strategy session without disclaimer -> zero LLMs, pending flow recorded, consent UI sent."""
        user_id = 20005
        strategy_msg_text = "Хочу увеличить выручку компании в 3 раза."
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="UserStrat", ai_disclaimer_accepted_at=None, current_dialogue_id=1))
            await session.commit()

        class MockState:
            def __init__(self):
                self._state = None
                self._data = {}
            async def set_state(self, s):
                self._state = s
            async def get_state(self):
                return self._state
            async def get_data(self):
                return self._data
            async def update_data(self, **kw):
                self._data.update(kw)
            async def clear(self):
                self._state = None
                self._data.clear()

        state = MockState()

        # User starts strategy session from menu
        menu_cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="UserStrat"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), answer=AsyncMock(), edit_text=AsyncMock()),
            data="menu_strategy_session",
            answer=AsyncMock(),
            bot=self.bot,
        )
        await funnel.menu_strategy_session(menu_cb, state)
        self.assertEqual(await state.get_state(), funnel.FunnelStates.strategy_chat)
        did = (await state.get_data()).get("strategy_dialogue_id")
        self.assertIsNotNone(did)

        # User sends message in strategy_chat without having accepted disclaimer
        user_msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="UserStrat"),
            chat=SimpleNamespace(id=user_id),
            text=strategy_msg_text,
            bot=self.bot,
            answer=AsyncMock(),
        )
        with patch("funnel.get_ai_response_for_topic", new=AsyncMock()) as mock_ai:
            await funnel.strategy_chat_message(user_msg, state)
            # Assert: ZERO LLM calls made
            mock_ai.assert_not_called()

        # Assert: No user DBMessage persisted yet
        async with async_session_maker() as session:
            msgs = (await session.execute(
                select(DBMessage).where(DBMessage.user_id == user_id, DBMessage.role == "user")
            )).scalars().all()
            self.assertEqual(len(msgs), 0)

        # Assert: PendingAiFlow contains exact message and dialogue identity
        pending = await get_pending_ai_flow(user_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.flow_type, "funnel_strategy")
        self.assertEqual(pending.payload.get("strategy_message"), strategy_msg_text)
        self.assertEqual(pending.payload.get("strategy_dialogue_id"), did)

        # Assert: consent UI sent
        self.assertTrue(any(m.chat_id == user_id and m.reply_markup is not None for m in self.bot.sent_messages))

    async def test_guard1_funnel_strategy_accept_resumes_message_exactly_once(self):
        """P0-1: Accept consent resumes exact strategy message, creates DBMessages, exactly one LLM call."""
        user_id = 20006
        strategy_msg_text = "Хочу увеличить выручку компании в 3 раза."
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="UserStrat2", ai_disclaimer_accepted_at=None, current_dialogue_id=1))
            await session.commit()

        class MockState:
            def __init__(self):
                self._state = None
                self._data = {}
            async def set_state(self, s):
                self._state = s
            async def get_state(self):
                return self._state
            async def get_data(self):
                return self._data
            async def update_data(self, **kw):
                self._data.update(kw)
            async def clear(self):
                self._state = None
                self._data.clear()

        state = MockState()

        await store_pending_ai_flow(user_id, "funnel_strategy", {
            "strategy_message": strategy_msg_text,
            "strategy_dialogue_id": 5,
        })

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="UserStrat2"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock(), answer=AsyncMock()),
            data="cb_accept_disclaimer",
            answer=AsyncMock(),
        )

        with patch("funnel.get_ai_response_for_topic", new=AsyncMock(return_value="Стратегия принята.")) as mock_ai:
            await handlers.cb_accept_disclaimer(cb, state, self.bot)
            # Assert: exactly one LLM call
            mock_ai.assert_called_once()
            called_text = mock_ai.call_args[0][1]
            self.assertEqual(called_text, strategy_msg_text)
            called_did = mock_ai.call_args[1].get("dialogue_id")
            self.assertEqual(called_did, 5)

        # Assert: User message and Assistant response persisted in DBMessage
        async with async_session_maker() as session:
            msgs = (await session.execute(
                select(DBMessage).where(DBMessage.user_id == user_id).order_by(DBMessage.id.asc())
            )).scalars().all()
            self.assertTrue(any(m.role == "user" and m.content == strategy_msg_text for m in msgs))
            self.assertTrue(any(m.role == "assistant" and m.content == "Стратегия принята." for m in msgs))

        # Assert: PendingAiFlow cleared
        self.assertIsNone(await get_pending_ai_flow(user_id))

    async def test_guard1_funnel_objection_requires_disclaimer_preserves_message(self):
        """P0-1: Objection message without disclaimer -> zero LLMs, pending flow recorded, consent UI sent."""
        user_id = 20007
        objection_text = "Мне кажется, программа слишком сложная для меня."
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="UserObj", ai_disclaimer_accepted_at=None, current_dialogue_id=1))
            await session.commit()

        class MockState:
            def __init__(self):
                self._state = None
                self._data = {}
            async def set_state(self, s):
                self._state = s
            async def get_state(self):
                return self._state
            async def get_data(self):
                return self._data
            async def update_data(self, **kw):
                self._data.update(kw)
            async def clear(self):
                self._state = None
                self._data.clear()

        state = MockState()

        # Start objection dialogue
        start_cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="UserObj"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), answer=AsyncMock(), edit_text=AsyncMock()),
            data="start_objection",
            answer=AsyncMock(),
        )
        await funnel.start_objection_dialogue(start_cb, state, bot=self.bot)
        self.assertEqual(await state.get_state(), funnel.FunnelStates.objection_chat)
        did = (await state.get_data()).get("objection_dialogue_id")
        self.assertIsNotNone(did)

        # User sends objection message without having accepted disclaimer
        user_msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="UserObj"),
            chat=SimpleNamespace(id=user_id),
            text=objection_text,
            bot=self.bot,
            answer=AsyncMock(),
        )
        with patch("funnel.get_ai_response_for_topic", new=AsyncMock()) as mock_ai:
            await funnel.objection_chat_message(user_msg, state)
            mock_ai.assert_not_called()

        # Assert: No user DBMessage persisted yet
        async with async_session_maker() as session:
            msgs = (await session.execute(
                select(DBMessage).where(DBMessage.user_id == user_id, DBMessage.role == "user")
            )).scalars().all()
            self.assertEqual(len(msgs), 0)

        # Assert: PendingAiFlow contains exact message and dialogue identity
        pending = await get_pending_ai_flow(user_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.flow_type, "funnel_objection")
        self.assertEqual(pending.payload.get("objection_message"), objection_text)
        self.assertEqual(pending.payload.get("objection_dialogue_id"), did)

        # Assert: consent UI sent
        self.assertTrue(any(m.chat_id == user_id and m.reply_markup is not None for m in self.bot.sent_messages))

    async def test_guard1_funnel_objection_accept_resumes_message_exactly_once(self):
        """P0-1: Accept consent resumes exact objection message, creates DBMessages, exactly one LLM call."""
        user_id = 20008
        objection_text = "Мне кажется, программа слишком сложная для меня."
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="UserObj2", ai_disclaimer_accepted_at=None, current_dialogue_id=1))
            await session.commit()

        class MockState:
            def __init__(self):
                self._state = None
                self._data = {}
            async def set_state(self, s):
                self._state = s
            async def get_state(self):
                return self._state
            async def get_data(self):
                return self._data
            async def update_data(self, **kw):
                self._data.update(kw)
            async def clear(self):
                self._state = None
                self._data.clear()

        state = MockState()

        await store_pending_ai_flow(user_id, "funnel_objection", {
            "objection_message": objection_text,
            "objection_dialogue_id": 7,
        })

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="UserObj2"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock(), answer=AsyncMock()),
            data="cb_accept_disclaimer",
            answer=AsyncMock(),
        )

        with patch("funnel.get_ai_response_for_topic", new=AsyncMock(return_value="Мы поможем шаг за шагом.")) as mock_ai:
            await handlers.cb_accept_disclaimer(cb, state, self.bot)
            mock_ai.assert_called_once()
            called_text = mock_ai.call_args[0][1]
            self.assertEqual(called_text, objection_text)
            called_did = mock_ai.call_args[1].get("dialogue_id")
            self.assertEqual(called_did, 7)

        # Assert: User message and Assistant response persisted in DBMessage
        async with async_session_maker() as session:
            msgs = (await session.execute(
                select(DBMessage).where(DBMessage.user_id == user_id).order_by(DBMessage.id.asc())
            )).scalars().all()
            self.assertTrue(any(m.role == "user" and m.content == objection_text for m in msgs))
            self.assertTrue(any(m.role == "assistant" and m.content == "Мы поможем шаг за шагом." for m in msgs))

        # Assert: PendingAiFlow cleared
        self.assertIsNone(await get_pending_ai_flow(user_id))

    async def test_guard1_concurrent_accept_resumes_funnel_pending_flow_at_most_once(self):
        """P0-1: Concurrent / double accept on funnel flow resumes at most once."""
        user_id = 20009
        strategy_msg = "Мой тестовый стратегический запрос."
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ConcUser", ai_disclaimer_accepted_at=None, current_dialogue_id=1))
            await session.commit()

        class MockState:
            def __init__(self):
                self._state = None
                self._data = {}
            async def set_state(self, s):
                self._state = s
            async def get_state(self):
                return self._state
            async def get_data(self):
                return self._data
            async def update_data(self, **kw):
                self._data.update(kw)
            async def clear(self):
                self._state = None
                self._data.clear()

        await store_pending_ai_flow(user_id, "funnel_strategy", {
            "strategy_message": strategy_msg,
            "strategy_dialogue_id": 12,
        })

        barrier = asyncio.Barrier(2)
        llm_calls = []

        async def click_worker():
            cb = SimpleNamespace(
                from_user=SimpleNamespace(id=user_id, first_name="ConcUser"),
                message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock(), answer=AsyncMock()),
                data="cb_accept_disclaimer",
                answer=AsyncMock(),
            )
            state = MockState()
            await barrier.wait()
            await handlers.cb_accept_disclaimer(cb, state, self.bot)

        with patch("funnel.get_ai_response_for_topic", new=AsyncMock(side_effect=lambda *args, **kw: llm_calls.append(args) or "AI answer")):
            await asyncio.gather(click_worker(), click_worker())

        # Assert: LLM called exactly once despite 2 concurrent accept clicks
        self.assertEqual(len(llm_calls), 1)
        self.assertIsNone(await get_pending_ai_flow(user_id))

    async def test_p0_2_concurrent_topic_reset_and_strategy_session_atomic_dialogue_allocation(self):
        """P0-2: Real concurrency test A: same user counter=20, Topic Reset vs Strategy Session -> distinct IDs {21, 22}, final high-water == max."""
        user_id = 20010
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="AtomicUser", current_dialogue_id=20))
            await session.commit()

        barrier = asyncio.Barrier(2)

        async def topic_reset_alloc():
            async with async_session_maker() as session:
                await barrier.wait()
                did = await allocate_next_dialogue_id(session, user_id, "stress")
                await session.commit()
                return did

        async def strategy_alloc():
            async with async_session_maker() as session:
                await barrier.wait()
                did = await allocate_global_dialogue_id(session, user_id)
                await session.commit()
                return did

        id1, id2 = await asyncio.gather(topic_reset_alloc(), strategy_alloc())

        # Assert: two distinct IDs, e.g. {21, 22}
        self.assertNotEqual(id1, id2)
        self.assertEqual({id1, id2}, {21, 22})

        # Assert: final high-water == max(ids) == 22
        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertEqual(u.current_dialogue_id, max(id1, id2))
            self.assertEqual(u.current_dialogue_id, 22)

    async def test_p0_2_concurrent_funnel_dialogue_allocations_monotonic_counter(self):
        """P0-2: Real concurrency test B: two concurrent funnel dialogue allocations -> distinct IDs, monotonic final counter."""
        user_id = 20011
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="AtomicFunnelUser", current_dialogue_id=10))
            await session.commit()

        barrier = asyncio.Barrier(2)

        async def alloc_task():
            async with async_session_maker() as session:
                await barrier.wait()
                did = await allocate_global_dialogue_id(session, user_id)
                await session.commit()
                return did

        id1, id2 = await asyncio.gather(alloc_task(), alloc_task())

        # Assert: distinct IDs
        self.assertNotEqual(id1, id2)
        self.assertEqual({id1, id2}, {11, 12})

        # Assert: monotonic final counter
        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertEqual(u.current_dialogue_id, 12)

    async def test_guard1_disclaimer_cancellation_clears_flow(self):
        user_id = 20004
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="User4", ai_disclaimer_accepted_at=None))
            await session.commit()

        await store_pending_ai_flow(user_id, "thematic", {"topic_slug": "stress"})
        self.assertIsNotNone(await get_pending_ai_flow(user_id))

        state = AsyncMock()
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="User4"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
            data="cb_cancel_disclaimer",
            answer=AsyncMock(),
        )
        await handlers.cb_cancel_disclaimer(cb, state, self.bot)

        self.assertIsNone(await get_pending_ai_flow(user_id))
        state.clear.assert_called_once()

    # =========================================================================
    # GUARD 2: Prerecorded First Message DB-Level Exactly-Once Concurrency
    # =========================================================================

    async def test_guard2_prerecorded_first_message_concurrency_barrier(self):
        user_id = 20010
        topic_slug = "stress"
        dialogue_id = 1
        prerecorded_text = "Здравствуй! С чем сегодня пришел(ла) поработать?"

        # 5 concurrent workers attempt to deliver the prerecorded first message simultaneously
        async def worker():
            async with async_session_maker() as session:
                return await deliver_prerecorded_first_message(
                    self.bot,
                    session,
                    user_id,
                    topic_slug,
                    dialogue_id,
                    prerecorded_text,
                )

        results = await asyncio.gather(*(worker() for _ in range(5)))

        # Assert exactly ONE worker succeeded and delivered
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 4)

        # Assert exactly ONE TopicPrerecordedDelivery row in DB
        async with async_session_maker() as session:
            deliveries = (await session.execute(
                select(TopicPrerecordedDelivery).where(
                    TopicPrerecordedDelivery.user_id == user_id,
                    TopicPrerecordedDelivery.topic_slug == topic_slug,
                    TopicPrerecordedDelivery.dialogue_id == dialogue_id,
                )
            )).scalars().all()
            self.assertEqual(len(deliveries), 1)

            # Assert exactly ONE Message row in DB with message_kind='topic_prerecorded_first'
            msgs = (await session.execute(
                select(DBMessage).where(
                    DBMessage.user_id == user_id,
                    DBMessage.topic_slug == topic_slug,
                    DBMessage.dialogue_id == dialogue_id,
                    DBMessage.message_kind == "topic_prerecorded_first",
                )
            )).scalars().all()
            self.assertEqual(len(msgs), 1)

    # =========================================================================
    # GUARD 3: Pre-Test Concurrency-Safe Claim
    # =========================================================================

    async def test_guard3_pre_test_claim_concurrency_barrier(self):
        user_id = 20020
        topic_slug = "self_esteem"
        dialogue_id = 1
        test_set_id = 1

        async def worker():
            async with async_session_maker() as session:
                acquired, claim = await acquire_pre_test_claim(
                    session,
                    user_id,
                    topic_slug,
                    dialogue_id,
                    test_set_id,
                )
                await session.commit()
                return acquired

        results = await asyncio.gather(*(worker() for _ in range(5)))

        # Assert exactly ONE worker acquired ownership
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 4)

        # Assert exactly ONE PreTestClaim row in DB
        async with async_session_maker() as session:
            claims = (await session.execute(
                select(PreTestClaim).where(
                    PreTestClaim.user_id == user_id,
                    PreTestClaim.topic_slug == topic_slug,
                    PreTestClaim.dialogue_id == dialogue_id,
                )
            )).scalars().all()
            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0].status, "in_progress")

            # Complete claim
            await complete_pre_test_claim(session, user_id, topic_slug, dialogue_id, test_set_id, attempt_id=999)
            completed_claim = await session.get(PreTestClaim, claims[0].id)
            self.assertEqual(completed_claim.status, "completed")
            self.assertEqual(completed_claim.attempt_id, 999)

    # =========================================================================
    # GUARD 4: Renewable Lease Summarization & Map-Reduce
    # =========================================================================

    async def test_guard4_summary_lease_renewal_and_takeover(self):
        user_id = 20030
        topic_slug = "relationships"
        dialogue_id = 1

        # Worker A acquires lease
        token_a = "token_worker_A"
        claimed_a = await claim_summary_generation(user_id, topic_slug, dialogue_id, token_a, lease_seconds=30)
        self.assertTrue(claimed_a)

        # Worker B tries to claim concurrently -> rejected
        token_b = "token_worker_B"
        claimed_b = await claim_summary_generation(user_id, topic_slug, dialogue_id, token_b, lease_seconds=30)
        self.assertFalse(claimed_b)

        # Worker A renews lease
        renewed_a = await renew_summary_claim_lease(user_id, topic_slug, dialogue_id, token_a, extend_seconds=60)
        self.assertTrue(renewed_a)

        # Worker B tries to renew Worker A's lease -> rejected
        renewed_b = await renew_summary_claim_lease(user_id, topic_slug, dialogue_id, token_b, extend_seconds=60)
        self.assertFalse(renewed_b)

        # Finish claim
        await finish_summary_claim(user_id, topic_slug, dialogue_id, token_a, summary_id=123)

        async with async_session_maker() as session:
            claim_row = (await session.execute(
                select(SummaryClaim).where(
                    SummaryClaim.user_id == user_id,
                    SummaryClaim.topic_slug == topic_slug,
                    SummaryClaim.dialogue_id == dialogue_id,
                )
            )).scalar_one()
            self.assertEqual(claim_row.status, "completed")
            self.assertEqual(claim_row.summary_id, 123)

    async def test_guard4_map_reduce_session_summary_generation(self):
        user_id = 20035
        topic_slug = "stress"
        dialogue_id = 2

        # Seed 35 messages to trigger multi-chunk Map-Reduce (chunk size = 15)
        async with async_session_maker() as session:
            for i in range(35):
                role = "user" if i % 2 == 0 else "assistant"
                session.add(DBMessage(
                    user_id=user_id,
                    topic_slug=topic_slug,
                    dialogue_id=dialogue_id,
                    role=role,
                    content=f"Сообщение {i + 1} в диалоге про стресс и отдых.",
                ))
            await session.commit()

        with patch("ai_dialogue_service.get_ai_response", new=AsyncMock(return_value="Ключевые инсайты: снизить нагрузку и наладить сон.")):
            summary = await generate_session_summary(user_id, topic_slug, dialogue_id)

        self.assertIsNotNone(summary)
        self.assertIn("Ключевые инсайты", summary.summary_text)

        # Second call returns existing summary without re-computation
        summary_again = await generate_session_summary(user_id, topic_slug, dialogue_id)
        self.assertEqual(summary.id, summary_again.id)

    # =========================================================================
    # SUITE A: TestSet Management & Migration
    # =========================================================================

    async def test_suite_a_testset_crud_and_modes(self):
        async with async_session_maker() as session:
            ts = TestSet(
                title="Тест на эмоциональное выгорание",
                slug="burnout_test",
                description="Оценка трех шкал Maslach Burnout Inventory",
                is_enabled=True,
                show_progress=True,
                calculation_enabled=False,
                preliminary_results_mode="simple",
                handoff_mode="prompt",
            )
            session.add(ts)
            await session.commit()
            ts_id = ts.id

        async with async_session_maker() as session:
            loaded = await session.get(TestSet, ts_id)
            self.assertEqual(loaded.title, "Тест на эмоциональное выгорание")
            self.assertEqual(loaded.slug, "burnout_test")
            self.assertEqual(loaded.preliminary_results_mode, "simple")

    async def test_suite_a_historical_attempt_archive_policy(self):
        async with async_session_maker() as session:
            session.add(User(id=9999, first_name="AuditUser"))
            ts = TestSet(title="Архивный тест", slug="audit_test", is_enabled=True)
            session.add(ts)
            await session.flush()
            ts_id = ts.id

            # Add completed attempt
            attempt = TestAttempt(
                user_id=9999,
                test_set_id=ts_id,
                test_set_name_snapshot="Архивный тест",
                test_set_slug_snapshot="audit_test",
                is_completed=True,
            )
            session.add(attempt)
            await session.commit()

        # Admin tries to delete test set with historical attempts
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=1),  # mock admin
            data=f"ts_delete_{ts_id}",
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        )
        with patch("test_set_admin_handlers._is_admin", new=AsyncMock(return_value=True)):
            await test_set_admin_handlers.delete_or_archive_test_set(cb)

        # Assert: TestSet is NOT hard-deleted; it is archived
        async with async_session_maker() as session:
            ts_after = await session.get(TestSet, ts_id)
            self.assertIsNotNone(ts_after)
            self.assertTrue(ts_after.is_archived)

            # Historical attempt remains intact
            attempt_after = await session.get(TestAttempt, attempt.id)
            self.assertIsNotNone(attempt_after)
            self.assertEqual(attempt_after.test_set_id, ts_id)

    # =========================================================================
    # SUITE B: Turn-Aware Prompt Payload Deduplication
    # =========================================================================

    async def test_suite_b_turn_aware_history_excludes_current_message(self):
        user_id = 20040
        topic_slug = "motivation_goals"
        dialogue_id = 1

        async with async_session_maker() as session:
            # 1. Past user & assistant turn
            msg1 = DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="user", content="Привет")
            msg2 = DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="assistant", content="Здравствуйте!")
            session.add_all([msg1, msg2])
            await session.flush()

            # 2. Current user turn persisted before LLM call
            msg3 = DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="user", content="Хочу поставить цель на год")
            session.add(msg3)
            await session.commit()
            curr_id = msg3.id

            # Turn-aware history excluding curr_id
            history = await get_turn_aware_history(session, user_id, topic_slug, dialogue_id, exclude_message_id=curr_id)

        # Assert history contains ONLY msg1 and msg2, NOT msg3
        history_ids = [m.id for m in history]
        self.assertIn(msg1.id, history_ids)
        self.assertIn(msg2.id, history_ids)
        self.assertNotIn(curr_id, history_ids)

    async def test_suite_b_get_ai_response_for_topic_payload_no_duplicate_user_role(self):
        user_id = 20041
        topic_slug = "motivation_goals"
        dialogue_id = 1

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="UserB", name="Пользователь"))
            ai_cfg = await session.get(AIConfig, 1)
            if ai_cfg:
                ai_cfg.provider = "KIE"
                ai_cfg.kie_api_key = "test_kie_api_key"
            curr_user_msg = DBMessage(
                user_id=user_id,
                topic_slug=topic_slug,
                dialogue_id=dialogue_id,
                role="user",
                content="Мой текущий запрос",
            )
            session.add(curr_user_msg)
            await session.commit()
            curr_msg_id = curr_user_msg.id

        captured_messages = []

        async def fake_kie_post(*args, **kwargs):
            nonlocal captured_messages
            payload = kwargs.get("json", {})
            captured_messages = payload.get("messages", [])
            resp = SimpleNamespace(
                status_code=200,
                json=lambda: {"code": 200, "data": {"choices": [{"message": {"content": "Ответ AI"}}]}},
            )
            return resp

        with patch("httpx.AsyncClient.post", side_effect=fake_kie_post):
            await get_ai_response_for_topic(
                user_id=user_id,
                user_prompt="Мой текущий запрос",
                user_name="Пользователь",
                topic_slug=topic_slug,
                dialogue_id=dialogue_id,
                current_message_id=curr_msg_id,
            )

        # In captured LLM payload, assert user prompt appears exactly once at the end
        user_turns = [m for m in captured_messages if m["role"] == "user"]
        self.assertEqual(len(user_turns), 1)
        self.assertIn("Мой текущий запрос", user_turns[0]["content"])

    # =========================================================================
    # SUITE C: User Name Priority in Prompts
    # =========================================================================

    async def test_suite_c_user_name_precedence_over_first_name(self):
        user = User(
            id=20050,
            first_name="tg_alex",
            name="Алексей Владимирович",
        )
        profile_str = build_ai_user_profile_context(user)
        self.assertIn("Алексей Владимирович", profile_str)
        self.assertNotIn("tg_alex", profile_str)

        user_no_name = User(
            id=20051,
            first_name="Мария",
            name=None,
        )
        profile_str2 = build_ai_user_profile_context(user_no_name)
        self.assertIn("Мария", profile_str2)

    # =========================================================================
    # SUITE D: Monotonic UserTopicSession Concurrency
    # =========================================================================

    async def test_suite_d_monotonic_dialogue_id_under_concurrency(self):
        user_id = 20060
        topic_slug = "sleep_recovery"

        async with async_session_maker() as session:
            session.add(User(id=user_id, username="conc_user", current_dialogue_id=5))
            await session.commit()

        barrier = asyncio.Barrier(2)

        async def worker():
            async with async_session_maker() as session:
                await barrier.wait()
                did = await allocate_next_dialogue_id(session, user_id, topic_slug)
                await set_active_topic_dialogue_id(session, user_id, topic_slug, did)
                await session.commit()
                return did

        res1, res2 = await asyncio.gather(worker(), worker())

        # Assert: allocated ids are different
        self.assertNotEqual(res1, res2)
        expected_max = max(res1, res2)

        # Assert: exactly one UserTopicSession row exists and active_dialogue_id == max
        async with async_session_maker() as session:
            rows = (await session.execute(
                select(UserTopicSession).where(
                    UserTopicSession.user_id == user_id,
                    UserTopicSession.topic_slug == topic_slug,
                )
            )).scalars().all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].active_dialogue_id, expected_max)

    # =========================================================================
    # SUITE E: 9-Step Entry Gate Pipeline
    # =========================================================================

    async def test_suite_e_topic_dialogue_pipeline_start_button_and_pretest(self):
        user_id = 20070
        topic_slug = "emotional_intelligence"

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="EQUser", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == topic_slug))
            if not cfg:
                cfg = TopicDialogueConfig(topic_slug=topic_slug)
                session.add(cfg)
            cfg.start_dialogue_button_enabled = True
            cfg.start_dialogue_button_text = "Погрузиться в эмоции"
            cfg.welcome_text = "Добро пожаловать в EQ практику."
            await session.commit()

        state = AsyncMock()
        event = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="EQUser"),
            chat=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
        )

        # 1. Entry when start button enabled -> renders welcome and Start button keyboard
        await handlers.activate_ai_mode(event, topic_slug, state, self.bot)
        self.assertTrue(len(self.bot.sent_messages) >= 1)
        start_msg = self.bot.sent_messages[-1]
        self.assertIn("Добро пожаловать в EQ практику.", start_msg.text)
        self.assertIsNotNone(start_msg.reply_markup)

        # 2. User clicks Start button
        start_btn_cb = start_msg.reply_markup.inline_keyboard[0][0].callback_data
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="EQUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
            data=start_btn_cb,
            answer=AsyncMock(),
        )
        await handlers.cb_ai_start_dialogue(cb, state, self.bot)

        # User transitioned into chatting state
        state.set_state.assert_called_with(handlers.UserStates.chatting)

    # =========================================================================
    # SUITE F: Admin FSM Cancel & Back Navigation
    # =========================================================================

    async def test_suite_f_topic_admin_cancel_clears_fsm(self):
        import topic_admin_handlers
        user_id = 1  # admin
        state = AsyncMock()
        await state.set_state(topic_admin_handlers.TopicAdminStates.waiting_welcome_text)

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data="admin_topic_dialogue_cfg_stress",
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        with patch("topic_admin_handlers._is_admin", new=AsyncMock(return_value=True)):
            await topic_admin_handlers.open_topic_dialogue_cfg(cb, state)

        state.clear.assert_called_once()

    async def test_suite_f_test_set_admin_cancel_clears_fsm(self):
        import test_set_admin_handlers
        user_id = 1
        state = AsyncMock()
        await state.set_state(test_set_admin_handlers.TestSetAdminStates.waiting_new_title)

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data="admin_test_sets_list",
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        with patch("test_set_admin_handlers._is_admin", new=AsyncMock(return_value=True)):
            await test_set_admin_handlers.list_test_sets(cb, state)

        state.clear.assert_called_once()

    # =========================================================================
    # SUITE G: Thematic Sessions Menu & Dispatch
    # =========================================================================

    async def test_suite_g_thematic_sessions_menu_and_routing(self):
        user_id = 20080
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ThematicUser", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            thematic_button = await session.scalar(
                select(MenuButton).where(
                    MenuButton.section_key == "thematic",
                    MenuButton.legacy_callback == "thematic_sleep_recovery",
                )
            )
            thematic_button.topic_slug = "sleep_recovery"
            await session.commit()

        # 1. Main menu button triggers menu_thematic_sessions
        cb_menu = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
            data="menu_thematic_sessions",
            answer=AsyncMock(),
        )
        await handlers.menu_thematic_sessions_callback(cb_menu, self.bot)
        self.assertTrue(len(self.bot.sent_messages) >= 1)
        thematic_menu = self.bot.sent_messages[-1]
        self.assertIn("Выбери тему", thematic_menu.text)

        # 2. Clicking a thematic topic triggers activate_ai_mode for that topic
        state = AsyncMock()
        cb_choice = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="ThematicUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
            data="thematic_sleep_recovery",
            answer=AsyncMock(),
        )
        await handlers.thematic_session_choice(cb_choice, state, self.bot)
        state.set_state.assert_called_with(handlers.UserStates.chatting)

    # =========================================================================
    # SENIOR REVIEW TESTS: P0-1 through P1-13
    # =========================================================================

    async def test_pre_dialogue_lifecycle_gate_order_and_reentry(self):
        """P0-2: Gate order: pre-test -> welcome/Start button -> prerecorded -> chatting.

        Pre-test does not rerun upon completed claim. Abandoned/failed test does not brick topic.
        """
        user_id = 20101
        slug = "gate_order_topic"

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="GateUser", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            session.add(Topic(slug=slug, name="Gate Topic"))
            session.add(Content(key="gate_topic_welcome_media"))
            session.add(ContentMedia(content_key="gate_topic_welcome_media", file_type="photo", file_id="gate-photo"))
            await session.flush()
            welcome_media = (await session.scalars(select(ContentMedia).where(ContentMedia.content_key == "gate_topic_welcome_media"))).first()
            session.add(TopicDialogueConfig(
                topic_slug=slug,
                welcome_text="Добро пожаловать в гейт-тест",
                welcome_media_id=str(welcome_media.id),
                start_dialogue_button_enabled=True,
                start_dialogue_button_text="Старт диалога",
                pre_dialogue_test_enabled=True,
                test_set_id=1,
                prerecorded_first_message="Записанное сообщение ассистента",
            ))
            await session.commit()

        state = AsyncMock()
        event = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="GateUser"),
            chat=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
        )

        async def fake_start_universal_test(bot, uid, st, purpose=None, test_set_id=None, topic_slug=None, dialogue_id=None):
            async with async_session_maker() as sess:
                target_slug = topic_slug or slug
                target_did = dialogue_id or await get_active_topic_dialogue_id(sess, uid, target_slug)
                await acquire_pre_test_claim(sess, uid, target_slug, target_did, test_set_id)
                await sess.commit()
            return True

        # 1. First entry: pre-test is required and not completed -> pre-test appears FIRST
        with patch("handlers.start_universal_test", side_effect=fake_start_universal_test) as mock_test:
            await handlers.activate_ai_mode(event, slug, state, self.bot)
            mock_test.assert_called_once()
            _, kwargs = mock_test.call_args
            self.assertEqual(kwargs.get("purpose"), "pre_dialogue")
            self.assertEqual(kwargs.get("test_set_id"), 1)

        # 2. Simulate pre-test completion via _finish_test
        import test_system_handlers
        async with async_session_maker() as session:
            did = await get_active_topic_dialogue_id(session, user_id, slug)
            claim = await get_pre_test_claim(session, user_id, slug, did)
            self.assertIsNotNone(claim)
            attempt = TestAttempt(
                user_id=user_id,
                test_set_id=1,
                purpose="pre_dialogue",
                topic_slug=slug,
                dialogue_id=did,
                status="in_progress",
                answers="[]",
            )
            session.add(attempt)
            await session.flush()
            claim.attempt_id = attempt.id
            test_session = database.TestSession(
                user_id=user_id,
                test_set_id=1,
                attempt_id=attempt.id,
                invocation_topic_slug=slug,
                invocation_dialogue_id=did,
                answers="[]",
            )
            session.add(test_session)
            await session.commit()
            attempt_id = attempt.id

        finish_state = AsyncMock()
        with patch("test_system_handlers.get_ai_response_for_topic", new=AsyncMock(return_value="Результат теста")), \
             patch("test_system_handlers._questions", new=AsyncMock(return_value=[])):
            await test_system_handlers._finish_test(self.bot, user_id, finish_state, [])

        # Claim must now be completed with attempt_id
        async with async_session_maker() as session:
            claim = await get_pre_test_claim(session, user_id, slug, did)
            self.assertEqual(claim.status, "completed")
            self.assertEqual(claim.attempt_id, attempt_id)

        # After completion: the configured topic welcome is shown with the
        # Start Dialogue button, without a technical invitation.
        start_btns = [m for m in self.bot.sent_messages if m.text and "Добро пожаловать в гейт-тест" in m.text]
        self.assertTrue(len(start_btns) >= 1)
        self.assertFalse(any(m.text and "Тестирование пройдено" in m.text for m in self.bot.sent_messages))
        self.assertEqual(len(self.bot.sent_photos), 1)
        self.assertEqual(self.bot.sent_photos[0].caption, "Добро пожаловать в гейт-тест")
        self.assertEqual(
            start_btns[-1].reply_markup.inline_keyboard[0][0].callback_data,
            f"ai_start_dialogue:{slug}:{did}",
        )

        # 3. Click Start button: activate_ai_mode(from_start_button=True)
        cb_start = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="GateUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
            data=f"ai_start_dialogue:{slug}:{did}",
            answer=AsyncMock(),
        )
        with patch("handlers.start_universal_test", new=AsyncMock()) as mock_test_rerun:
            await handlers.activate_ai_mode(cb_start, slug, state, self.bot, from_start_button=True)
            # Pre-test does NOT rerun
            mock_test_rerun.assert_not_called()

        # Prerecorded message delivered and state is chatting
        prerec = [m for m in self.bot.sent_messages if m.text and "Записанное сообщение ассистента" in m.text]
        self.assertTrue(len(prerec) >= 1)
        state.set_state.assert_called_with(handlers.UserStates.chatting)

        # 4. Recoverable: failed/abandoned pre-test does not permanently brick topic
        async with async_session_maker() as session:
            c = await get_pre_test_claim(session, user_id, slug, did)
            c.status = "failed"
            await session.commit()
            reacquired, _ = await acquire_pre_test_claim(session, user_id, slug, did, 1)
            self.assertTrue(reacquired)

    async def test_resume_checks_current_topic_access_before_restoring_fsm(self):
        user_id = 20106
        slug = "paid_resume_review_topic"
        async with async_session_maker() as session:
            user = User(
                id=user_id,
                first_name="ExpiredUser",
                current_dialogue_id=1,
                subscription_end=msk_now() - timedelta(days=1),
            )
            session.add_all([
                user,
                Topic(slug=slug, name="Платная тема", system_prompt="Ответь кратко.", is_paid=True),
                UserTopicSession(user_id=user_id, topic_slug=slug, active_dialogue_id=1),
                DBMessage(user_id=user_id, topic_slug=slug, dialogue_id=1, role="user", content="Старый вопрос"),
            ])
            payment = await session.get(PaymentConfig, 1)
            payment.mode = "subscription"
            await session.commit()

        state = StateStore()
        target = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="ExpiredUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id)),
            answer=AsyncMock(),
        )
        resumed = await handlers._resume_ai_dialogue(
            target,
            state,
            self.bot,
            slug,
            1,
            explicit=True,
        )

        self.assertFalse(resumed)
        self.assertIsNone(await state.get_state())
        self.assertIn("доступ", target.answer.await_args.args[0])
        self.assertFalse(any(message.text and message.text.startswith("Продолжаем") for message in self.bot.sent_messages))

    async def test_immutable_topic_context_snapshot_race_barrier_chat(self):
        """P0-3: TopicContextSnapshot frozen before await; switching topic mid-generation stores response in original topic."""
        user_id = 20102
        topic_a = "topic_snapshot_a"
        topic_b = "topic_snapshot_b"

        async with async_session_maker() as session:
            session.add(User(id=user_id, username="snap_user", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1,
                             current_topic_slug=topic_a, current_dialogue_id=10))
            await session.commit()

        state = AsyncMock()
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="SnapUser"),
            chat=SimpleNamespace(id=user_id),
            text="Вопрос в топике А",
            answer=AsyncMock(),
        )

        barrier = asyncio.Barrier(2)

        async def fake_llm(*args, **kwargs):
            # User switches to topic B during LLM generation
            async with async_session_maker() as s:
                u = await s.get(User, user_id)
                u.current_topic_slug = topic_b
                u.current_dialogue_id = 20
                await s.commit()
            await barrier.wait()
            return "Ответ из модели для топика А"

        with patch("handlers.get_ai_response_for_topic", side_effect=fake_llm):
            chat_task = asyncio.create_task(handlers.process_chat(message, state, self.bot))
            await barrier.wait()
            await chat_task

        # Assert: assistant Message stored in Topic A / dialogue 10 ONLY!
        async with async_session_maker() as session:
            msgs_a = (await session.execute(
                select(DBMessage).where(DBMessage.user_id == user_id, DBMessage.topic_slug == topic_a, DBMessage.dialogue_id == 10)
            )).scalars().all()
            msgs_b = (await session.execute(
                select(DBMessage).where(DBMessage.user_id == user_id, DBMessage.topic_slug == topic_b)
            )).scalars().all()
            self.assertEqual(len(msgs_b), 0)
            self.assertTrue(any(m.role == "assistant" and "Ответ из модели" in m.content for m in msgs_a))

    async def test_immutable_topic_context_snapshot_race_barrier_photo(self):
        """P0-3 / P0-6: Photo processing uses immutable snapshot; topic switch stores response in original topic."""
        user_id = 20103
        topic_a = "photo_topic_a"
        topic_b = "photo_topic_b"

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="PhotoSnap", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1,
                             current_topic_slug=topic_a, current_dialogue_id=5))
            await session.commit()

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="PhotoSnap"),
            chat=SimpleNamespace(id=user_id),
            photo=[SimpleNamespace(file_id="photo_123")],
            caption="Подпись к фото",
            answer=AsyncMock(),
        )

        self.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="p.jpg"))
        self.bot.download_file = AsyncMock(return_value=SimpleNamespace(read=lambda: b"fake_image_bytes"))

        barrier = asyncio.Barrier(2)

        async def fake_photo_llm(*args, **kwargs):
            # Switch to topic B during LLM generation
            async with async_session_maker() as s:
                u = await s.get(User, user_id)
                u.current_topic_slug = topic_b
                u.current_dialogue_id = 99
                await s.commit()
            await barrier.wait()
            return "Ответ на фото для топика А"

        with patch("handlers.analyze_image", new=AsyncMock(return_value="Фото заката")), \
             patch("handlers.get_ai_response_for_topic", side_effect=fake_photo_llm):
            photo_task = asyncio.create_task(handlers.process_chat_photo(message, self.bot))
            await barrier.wait()
            await photo_task

        async with async_session_maker() as session:
            msgs_a = (await session.execute(
                select(DBMessage).where(DBMessage.user_id == user_id, DBMessage.topic_slug == topic_a, DBMessage.dialogue_id == 5)
            )).scalars().all()
            msgs_b = (await session.execute(
                select(DBMessage).where(DBMessage.user_id == user_id, DBMessage.topic_slug == topic_b)
            )).scalars().all()
            self.assertEqual(len(msgs_b), 0)
            self.assertTrue(any(m.role == "user" and "[Фото: Фото заката]" in m.content for m in msgs_a))
            self.assertTrue(any(m.role == "assistant" and "Ответ на фото" in m.content for m in msgs_a))

    async def test_turn_aware_memory_payload_deduplication_and_no_half_turns(self):
        """P0-4: Complete turns selected, no half-turns, overlap deduplicated."""
        user_id = 20104
        topic_slug = "memory_turn_topic"
        dialogue_id = 1

        async with async_session_maker() as session:
            # 8 complete turns (16 messages)
            for i in range(1, 9):
                session.add(DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="user", content=f"Q{i}"))
                session.add(DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="assistant", content=f"A{i}"))
            await session.commit()

        async with async_session_maker() as session:
            # First 2 turns (Q1,A1, Q2,A2) and recent 2 turns (Q7,A7, Q8,A8) -> exactly 8 messages
            history = await get_turn_aware_history(session, user_id, topic_slug, dialogue_id, limit_first=2, limit_recent=2)
            self.assertEqual(len(history), 8)
            contents = [m.content for m in history]
            self.assertEqual(contents, ["Q1", "A1", "Q2", "A2", "Q7", "A7", "Q8", "A8"])

            # Overlap test: first 5 and recent 5 on 8 total turns -> exactly 8 complete turns (16 messages), no duplicates
            overlap_hist = await get_turn_aware_history(session, user_id, topic_slug, dialogue_id, limit_first=5, limit_recent=5)
            self.assertEqual(len(overlap_hist), 16)
            self.assertEqual(len(set(m.id for m in overlap_hist)), 16)

    async def test_user_profile_context_canonical_fields_and_no_billing(self):
        """P0-5: Canonical profile: name, gender, age, response_length. NO billing metadata."""
        user = User(
            id=20105,
            name="Тестовый Тестовый",
            gender="мужской",
            age=30,
            response_length="лаконичный",
            balance=1000,
            subscription_end=msk_now() + timedelta(days=10),
            username="ivan_tg",
        )
        ctx = build_ai_user_profile_context(user)
        self.assertIn("Тестовый Тестовый", ctx)
        self.assertIn("мужской", ctx)
        self.assertIn("30", ctx)
        self.assertIn("лаконичный", ctx)
        self.assertNotIn("1000", ctx)
        self.assertNotIn("баланс", ctx.lower())
        self.assertNotIn("подписк", ctx.lower())
        self.assertNotIn("ivan_tg", ctx)

    async def test_two_stage_photo_pipeline_payload_and_snapshot(self):
        """P0-6: Two-stage vision: Stage 1 factual description -> Stage 2 topic dialogue pipeline with prompt & tone."""
        user_id = 20106
        topic_slug = "vision_pipeline_topic"

        async with async_session_maker() as session:
            session.add(User(id=user_id, name="VisionUser", current_topic_slug=topic_slug, current_dialogue_id=1,
                             ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            session.add(Topic(slug=topic_slug, name="Vision Topic", system_prompt="Системный промпт топика"))
            await session.commit()

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="VisionUser"),
            chat=SimpleNamespace(id=user_id),
            photo=[SimpleNamespace(file_id="p1")],
            caption="Помоги понять это фото",
            answer=AsyncMock(),
        )

        self.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="photo.jpg"))
        self.bot.download_file = AsyncMock(return_value=SimpleNamespace(read=lambda: b"bytes"))

        captured_payload = {}

        async def mock_generate(ai_cfg, chat_client, model_name, messages):
            captured_payload["messages"] = messages
            return "Ответ ассистента на фото"

        with patch("handlers.analyze_image", new=AsyncMock(return_value="Картина с изображением гор")), \
             patch("ai_integration._prepare_clients", new=AsyncMock(return_value=(SimpleNamespace(provider="OPENAI", context_limit_first=5, context_limit_recent=5), None, AsyncMock(), "gpt-4o", None))), \
             patch("ai_integration._generate_chat_response", side_effect=mock_generate):
            await handlers.process_chat_photo(message, self.bot)

        # Inspect captured model payload
        msgs = captured_payload.get("messages", [])
        self.assertTrue(len(msgs) >= 2)
        system_msg = msgs[0]["content"]
        self.assertIn("Системный промпт топика", system_msg)
        self.assertIn("ВАЖНОЕ ПРАВИЛО ОБЩЕНИЯ", system_msg)  # GLOBAL_TONE_INSTRUCTION
        user_msg = msgs[-1]["content"]
        self.assertIn("[Фото: Картина с изображением гор]", user_msg)
        self.assertIn("Помоги понять это фото", user_msg)

    async def test_finish_summary_ux_actions_and_version_regeneration(self):
        """P1-7: Finish UX shows summary/new/booking actions and regeneration creates v2."""
        user_id = 20107
        topic_slug = "summary_ux_topic"
        dialogue_id = 7

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="SumUser", current_topic_slug=topic_slug, current_dialogue_id=dialogue_id))
            session.add(TopicDialogueConfig(topic_slug=topic_slug, summary_generation_enabled=True))
            session.add(DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="user", content="Привет"))
            session.add(DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="assistant", content="Привет, чем могу помочь?"))
            await session.commit()

        # 1. Finish callback shows actions
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ai_finish_dialogue:{topic_slug}:{dialogue_id}",
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )
        await handlers.cb_ai_finish_dialogue(cb, AsyncMock(), self.bot)
        _, kwargs = cb.message.answer.call_args
        markup = kwargs.get("reply_markup")
        cb_data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn(f"ai_gen_summary:{topic_slug}:{dialogue_id}", cb_data)
        self.assertIn(f"ai_new_dialogue:{topic_slug}", cb_data)
        self.assertIn("funnel_open_landing", cb_data)

        # 2. Continue dialogue keeps exact dialogue_id
        cb_cont = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ai_continue_dialogue:{topic_slug}:{dialogue_id}",
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )
        cont_state = AsyncMock()
        await handlers.cb_ai_continue_dialogue(cb_cont, cont_state, self.bot)
        cont_state.update_data.assert_called_with(current_topic_slug=topic_slug, dialogue_id=dialogue_id)

        # 3. Summary generation creates version 1
        with patch("ai_dialogue_service._generate_summary_chunk", new=AsyncMock(return_value="Итог v1")):
            summary1 = await generate_session_summary(user_id, topic_slug, dialogue_id, version=1)
            self.assertEqual(summary1.version, 1)

        # 4. Summary regeneration creates version 2
        with patch("ai_dialogue_service._generate_summary_chunk", new=AsyncMock(return_value="Итог v2")):
            summary2 = await generate_session_summary(user_id, topic_slug, dialogue_id, version=2)
            self.assertEqual(summary2.version, 2)

    async def test_summary_chunking_middle_turn_fact_survives(self):
        """P1-7: Map-Reduce 10-turn chunks with 1-turn overlap preserves middle-turn fact."""
        user_id = 20108
        topic_slug = "chunk_topic"
        dialogue_id = 3

        async with async_session_maker() as session:
            for i in range(1, 16):
                fact = "СЕКРЕТНЫЙ_ФАКТ_7" if i == 7 else f"обычный_факт_{i}"
                session.add(DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="user", content=f"Q{i}: {fact}"))
                session.add(DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="assistant", content=f"A{i}"))
            await session.commit()

        async def echo_chunk(system, prompt):
            return f"Processed chunk: {prompt}"

        with patch("ai_dialogue_service._generate_summary_chunk", side_effect=echo_chunk):
            summary = await generate_session_summary(user_id, topic_slug, dialogue_id, version=1)
            self.assertIn("СЕКРЕТНЫЙ_ФАКТ_7", summary.summary_text)

    async def test_ai_response_keyboard_has_only_configurable_finish_action(self):
        """P1-8: AI responses expose only the configurable finish action."""
        topic_slug = "booking_topic"
        markup = kb.ai_response_keyboard(topic_slug, 1)
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].text, "Завершить диалог")
        self.assertEqual(buttons[0].callback_data, f"ai_finish_dialogue:{topic_slug}:1")

        custom = kb.ai_response_keyboard(topic_slug, 1, "Закрыть тему")
        custom_button = custom.inline_keyboard[0][0]
        self.assertEqual(custom_button.text, "Закрыть тему")
        self.assertEqual(custom_button.callback_data, f"ai_finish_dialogue:{topic_slug}:1")

    async def test_chat_handlers_keep_only_latest_ai_keyboard_and_survive_edit_error(self):
        user_id = 20120
        topic_slug = "latest_keyboard_topic"
        async with async_session_maker() as session:
            session.add(User(
                id=user_id,
                first_name="LatestKeyboard",
                current_topic_slug=topic_slug,
                current_dialogue_id=1,
                ai_disclaimer_accepted_at=msk_now(),
                ai_disclaimer_version=1,
            ))
            session.add(Topic(slug=topic_slug, name="Latest keyboard", system_prompt="Answer briefly."))
            session.add(TopicDialogueConfig(topic_slug=topic_slug, welcome_text="Приветствие темы"))
            finish_content = await session.get(Content, "btn_ai_finish_dialogue")
            finish_content.text_content = "Закрыть диалог"
            finish_content.button_text = "Закрыть диалог"
            await session.commit()

        state = StateStore()
        event = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="LatestKeyboard"),
            chat=SimpleNamespace(id=user_id),
        )
        await handlers.activate_ai_mode(event, topic_slug, state, self.bot)

        async def message(text):
            return SimpleNamespace(
                from_user=SimpleNamespace(id=user_id, first_name="LatestKeyboard"),
                chat=SimpleNamespace(id=user_id),
                text=text,
                answer=AsyncMock(),
            )

        with patch(
            "handlers.get_ai_response_for_topic",
            new=AsyncMock(side_effect=["Ответ 1", "Ответ 2", "Ответ 3"]),
        ):
            await handlers.process_chat(await message("Вопрос 1"), state, self.bot)
            first_response_id = state.data["ai_last_response_message_id"]
            await handlers.process_chat(await message("Вопрос 2"), state, self.bot)
            second_response_id = state.data["ai_last_response_message_id"]

            self.bot.fail_reply_markup_edit = True
            await handlers.process_chat(await message("Вопрос 3"), state, self.bot)

        response_messages = [
            item for item in self.bot.sent_messages if item.text in {"Ответ 1", "Ответ 2", "Ответ 3"}
        ]
        self.assertEqual(len(response_messages), 3)
        for item in response_messages:
            buttons = [button for row in item.reply_markup.inline_keyboard for button in row]
            self.assertEqual(len(buttons), 1)
            self.assertEqual(buttons[0].text, "Закрыть диалог")
            self.assertNotEqual(buttons[0].callback_data, "funnel_open_landing")

        self.assertEqual(
            [entry["message_id"] for entry in self.bot.edited_reply_markups],
            [self.bot.sent_messages[0].message_id, first_response_id],
        )
        self.assertNotEqual(first_response_id, second_response_id)
        self.assertEqual(
            sum("Режим активирован" in (item.text or "") for item in self.bot.sent_messages),
            0,
        )
        self.assertEqual(
            sum("Приветствие темы" in (item.text or "") for item in self.bot.sent_messages),
            1,
        )

    async def test_finish_button_is_editable_through_existing_content_admin(self):
        user_id = 20121
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="Admin", is_admin=True))
            await session.commit()

        state = StateStore()
        await state.update_data(key="btn_ai_finish_dialogue", category="ai")
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(id=user_id),
            text="Закрыть текущий диалог",
            html_text="Закрыть текущий диалог",
            caption=None,
            photo=None,
            video=None,
            delete=AsyncMock(),
            answer=AsyncMock(return_value=SimpleNamespace(message_id=9001)),
            bot=self.bot,
        )
        await handlers.save_content(message, state, self.bot)

        async with async_session_maker() as session:
            content = await session.get(Content, "btn_ai_finish_dialogue")
            self.assertEqual(content.button_text, "Закрыть текущий диалог")
        markup = await kb.ai_response_keyboard_for_topic("editable_topic", 3)
        self.assertEqual(markup.inline_keyboard[0][0].text, "Закрыть текущий диалог")

    async def test_finished_ai_booking_returns_to_completion_not_hypno_funnel(self):
        user_id = 20122
        topic_slug = "landing_return_topic"
        dialogue_id = 4
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="LandingReturn", current_topic_slug=topic_slug, current_dialogue_id=dialogue_id))
            session.add(Topic(slug=topic_slug, name="Landing return", system_prompt="Answer."))
            session.add(TopicDialogueConfig(topic_slug=topic_slug, summary_generation_enabled=False))
            session.add(FunnelState(user_id=user_id, stage="strategy_offer", is_active=True))
            await session.commit()

        state = StateStore()
        finish_callback = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ai_finish_dialogue:{topic_slug}:{dialogue_id}",
            answer=AsyncMock(),
            message=SimpleNamespace(
                answer=AsyncMock(),
                edit_reply_markup=AsyncMock(),
            ),
        )
        await handlers.cb_ai_finish_dialogue(finish_callback, state, self.bot)
        self.assertNotIn(
            "ai_gen_summary:landing_return_topic:4",
            [
                button.callback_data
                for row in finish_callback.message.answer.call_args.kwargs["reply_markup"].inline_keyboard
                for button in row
            ],
        )

        landing_callback = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data="funnel_open_landing",
            answer=AsyncMock(),
            bot=self.bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=501, delete=AsyncMock()),
        )
        await funnel.funnel_open_landing(landing_callback, state)
        landing_markup = self.bot.sent_messages[-1].reply_markup
        back_callback = landing_markup.inline_keyboard[-1][0].callback_data
        self.assertEqual(back_callback, f"ai_session_completed:{topic_slug}:{dialogue_id}")

        async with async_session_maker() as session:
            landing = (await session.execute(
                select(FunnelLanding).where(FunnelLanding.user_id == user_id)
            )).scalars().all()[-1]
            self.assertEqual(landing.origin_stage, "ai_session_completed")

        unknown_markup = await funnel.build_landing_keyboard_for_user(user_id, origin_stage="unknown")
        self.assertEqual(unknown_markup.inline_keyboard[-1][0].callback_data, "funnel_to_main")
        self.assertFalse(hasattr(TopicDialogueConfig, "booking_button_url"))

    async def test_finished_ai_session_exits_chatting_and_keeps_resume_actions(self):
        user_id = 20123
        topic_slug = "finished_session_topic"
        dialogue_id = 4
        async with async_session_maker() as session:
            session.add(User(
                id=user_id,
                first_name="FinishedSession",
                current_topic_slug=topic_slug,
                current_dialogue_id=dialogue_id,
                ai_disclaimer_accepted_at=msk_now(),
                ai_disclaimer_version=1,
            ))
            session.add(Topic(slug=topic_slug, name="Finished session", system_prompt="Answer."))
            session.add(TopicDialogueConfig(topic_slug=topic_slug, summary_generation_enabled=True))
            session.add(DBMessage(
                user_id=user_id,
                topic_slug=topic_slug,
                dialogue_id=dialogue_id,
                role="user",
                content="Сохрани этот контекст",
            ))
            await session.commit()

        state = StateStore()
        await state.set_state(handlers.UserStates.chatting)
        await state.update_data(
            current_topic_slug=topic_slug,
            dialogue_id=dialogue_id,
            preserved_context="keep",
        )
        finish_callback = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ai_finish_dialogue:{topic_slug}:{dialogue_id}",
            answer=AsyncMock(),
            message=SimpleNamespace(
                answer=AsyncMock(),
                edit_reply_markup=AsyncMock(),
            ),
        )

        await handlers.cb_ai_finish_dialogue(finish_callback, state, self.bot)

        self.assertIsNone(await state.get_state())
        state_data = await state.get_data()
        self.assertEqual(state_data["preserved_context"], "keep")
        self.assertEqual(
            state_data["ai_landing_return_callback"],
            f"ai_session_completed:{topic_slug}:{dialogue_id}",
        )

        async with async_session_maker() as session:
            messages = (await session.execute(
                select(DBMessage).where(
                    DBMessage.user_id == user_id,
                    DBMessage.topic_slug == topic_slug,
                    DBMessage.dialogue_id == dialogue_id,
                )
            )).scalars().all()
            self.assertEqual(len(messages), 1)

        with patch("handlers.get_ai_response_for_topic", new=AsyncMock()) as mocked_ai:
            incoming = SimpleNamespace(
                from_user=SimpleNamespace(id=user_id),
                chat=SimpleNamespace(id=user_id),
                text="Сообщение после завершения",
            )
            await handlers.router.message.trigger(
                incoming,
                raw_state=await state.get_state(),
                state=state,
                bot=self.bot,
            )
            mocked_ai.assert_not_awaited()

        completion_markup = finish_callback.message.answer.call_args.kwargs["reply_markup"]
        completion_callbacks = [
            button.callback_data
            for row in completion_markup.inline_keyboard
            for button in row
        ]
        self.assertIn(f"ai_gen_summary:{topic_slug}:{dialogue_id}", completion_callbacks)
        self.assertIn(f"ai_new_dialogue:{topic_slug}", completion_callbacks)
        self.assertIn("funnel_open_landing", completion_callbacks)
        self.assertIn("main_menu_action", completion_callbacks)

        continue_callback = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ai_continue_dialogue:{topic_slug}:{dialogue_id}",
            answer=AsyncMock(),
            message=SimpleNamespace(
                answer=AsyncMock(return_value=SimpleNamespace(message_id=7001)),
            ),
        )
        await handlers.cb_ai_continue_dialogue(continue_callback, state, self.bot)
        self.assertEqual(await state.get_state(), handlers.UserStates.chatting.state)

        new_callback = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ai_new_dialogue:{topic_slug}",
            answer=AsyncMock(),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=user_id),
                answer=AsyncMock(return_value=SimpleNamespace(message_id=7002)),
                delete=AsyncMock(),
            ),
        )
        await handlers.cb_ai_new_dialogue(new_callback, state, self.bot)
        self.assertEqual(await state.get_state(), handlers.UserStates.chatting.state)
        async with async_session_maker() as session:
            user = await session.get(User, user_id)
            self.assertGreater(user.current_dialogue_id, dialogue_id)

    async def test_testset_admin_import_xlsx_csv_txt_scoped_to_b(self):
        """P1-9: Importing into TestSet B does not mutate TestSet A; formulas remain scoped to B."""
        import io
        async with async_session_maker() as session:
            ts_a = TestSet(name="Set A", slug="set_a")
            ts_b = TestSet(name="Set B", slug="set_b")
            session.add_all([ts_a, ts_b])
            await session.flush()
            session.add(TestQuestion(test_set_id=ts_a.id, text="Q_A1"))
            session.add(TestQuestion(test_set_id=ts_a.id, text="Q_A2"))
            admin_user = await session.get(User, 10001)
            if not admin_user:
                session.add(User(id=10001, first_name="Admin", is_admin=True))
            await session.commit()
            id_a, id_b = ts_a.id, ts_b.id

        csv_content = "Вопрос;Категория;Реверс\nНовый вопрос B;general;False\n"
        file_io = io.BytesIO(csv_content.encode("utf-8"))

        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"target_ts_id": id_b})
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=10001),
            document=SimpleNamespace(file_name="questions.csv"),
            text=None,
            answer=AsyncMock(),
        )
        mock_bot = AsyncMock()
        mock_bot.download = AsyncMock(side_effect=lambda doc, destination: destination.write(csv_content.encode("utf-8")))

        with patch("test_set_admin_handlers.render_test_set_details", new=AsyncMock()):
            await test_set_admin_handlers.handle_questions_upload(message, state, mock_bot)


        async with async_session_maker() as session:
            qa = (await session.execute(select(TestQuestion).where(TestQuestion.test_set_id == id_a))).scalars().all()
            qb = (await session.execute(select(TestQuestion).where(TestQuestion.test_set_id == id_b))).scalars().all()
            # Set A untouched!
            self.assertEqual(len(qa), 2)
            # Set B has 1 question!
            self.assertEqual(len(qb), 1)

    async def test_testconfig_elimination_and_diagnostics_seed_and_failsafe(self):
        """P1-10: Diagnostics config seeded with test_set_id=1; fail-safe triggers if test_set_id is None."""
        user_id = 20110
        slug = "failsafe_topic"

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="FSUser", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            session.add(TopicDialogueConfig(
                topic_slug=slug,
                pre_dialogue_test_enabled=True,
                test_set_id=None,  # Bad config: test enabled but no test set
            ))
            await session.commit()

        state = AsyncMock()
        event = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="FSUser"),
            chat=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
        )

        with patch("handlers.start_universal_test", new=AsyncMock()) as mock_start:
            await handlers.activate_ai_mode(event, slug, state, self.bot)
            mock_start.assert_not_called()
            setup_msgs = [m for m in self.bot.sent_messages if m.text and "Анкета настраивается" in m.text]
            self.assertTrue(len(setup_msgs) >= 1)

    async def test_prerecorded_delivery_status_and_telegram_retry(self):
        """P1-11: Prerecorded delivery status claimed -> failed on error -> delivered on retry."""
        user_id = 20111
        slug = "prerec_retry_topic"
        did = 1
        text = "Тестовое приветствие"

        # 1. Telegram fails -> delivery marked failed, returns False
        failing_bot = MockBot()
        failing_bot.send_message = AsyncMock(side_effect=Exception("Telegram Network Error"))

        async with async_session_maker() as session:
            delivered = await deliver_prerecorded_first_message(failing_bot, session, user_id, slug, did, text)
            self.assertFalse(delivered)

        async with async_session_maker() as session:
            deliv = (await session.execute(
                select(TopicPrerecordedDelivery).where(
                    TopicPrerecordedDelivery.user_id == user_id,
                    TopicPrerecordedDelivery.topic_slug == slug,
                    TopicPrerecordedDelivery.dialogue_id == did,
                )
            )).scalar_one_or_none()
            self.assertIsNotNone(deliv)
            self.assertEqual(deliv.status, "failed")

        # 2. Retry succeeds -> delivery marked delivered, returns True
        working_bot = MockBot()
        async with async_session_maker() as session:
            delivered_retry = await deliver_prerecorded_first_message(working_bot, session, user_id, slug, did, text)
            self.assertTrue(delivered_retry)

        async with async_session_maker() as session:
            deliv2 = (await session.execute(
                select(TopicPrerecordedDelivery).where(
                    TopicPrerecordedDelivery.user_id == user_id,
                    TopicPrerecordedDelivery.topic_slug == slug,
                    TopicPrerecordedDelivery.dialogue_id == did,
                )
            )).scalar_one_or_none()
            self.assertEqual(deliv2.status, "delivered")

    async def test_disclaimer_acceptance_atomic_consume_concurrency(self):
        """P0-2: Atomic consume with barrier: two concurrent tasks consume same flow -> exactly one winner, continuation runs once."""
        user_id = 20112
        await store_pending_ai_flow(user_id, "thematic", {"topic_slug": "stress"})

        barrier = asyncio.Barrier(2)
        results = []

        async def worker():
            await barrier.wait()
            consumed = await consume_pending_ai_flow(user_id)
            results.append(consumed)

        await asyncio.gather(worker(), worker())

        non_none = [r for r in results if r is not None]
        none_results = [r for r in results if r is None]
        self.assertEqual(len(non_none), 1)
        self.assertEqual(len(none_results), 1)
        self.assertEqual(non_none[0].flow_type, "thematic")
        self.assertEqual(non_none[0].payload.get("topic_slug"), "stress")

        # Also verify via cb_accept_disclaimer with barrier that continuation runs exactly once
        await store_pending_ai_flow(user_id, "thematic", {"topic_slug": "stress"})
        cb_barrier = asyncio.Barrier(2)
        dispatch_calls = []

        async def cb_worker():
            cb = SimpleNamespace(
                from_user=SimpleNamespace(id=user_id, first_name="User1"),
                message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock(), answer=AsyncMock()),
                data="cb_accept_disclaimer",
                answer=AsyncMock(),
            )
            state = AsyncMock()
            await cb_barrier.wait()
            await handlers.cb_accept_disclaimer(cb, state, self.bot)

        with patch("handlers.activate_ai_mode", new=AsyncMock(side_effect=lambda *args, **kwargs: dispatch_calls.append(args))):
            await asyncio.gather(cb_worker(), cb_worker())

        self.assertEqual(len(dispatch_calls), 1)

    async def test_summary_claim_failed_retryable(self):
        """P1-11: Failed summary claim is immediately retryable without waiting for lease."""
        user_id = 20113
        slug = "failed_claim_topic"
        did = 1

        async with async_session_maker() as session:
            session.add(SummaryClaim(
                user_id=user_id,
                topic_slug=slug,
                dialogue_id=did,
                version=1,
                owner_token="old_owner",
                status="failed",
                lease_until=msk_now() + timedelta(hours=1),  # far in future
            ))
            await session.commit()

        async with async_session_maker() as session:
            acquired, claim, token = await claim_summary_generation(session, user_id, slug, did)
            self.assertTrue(acquired)
            self.assertNotEqual(token, "old_owner")

    async def test_token_access_cta_and_alert_keyboard(self):
        """P1-12: Canonical rule balance < cost_per_message routes to token_alert_keyboard."""
        user_id = 20114
        async with async_session_maker() as session:
            pconf = await session.get(PaymentConfig, 1)
            if not pconf:
                pconf = PaymentConfig(id=1, mode="token", cost_per_message=10)
                session.add(pconf)
            else:
                pconf.mode = "token"
                pconf.cost_per_message = 10
            session.add(User(id=user_id, username="token_user", balance=0, is_admin=False))
            session.add(Topic(slug="paid_t", name="Paid Topic", is_paid=True))
            await session.commit()

        event = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(answer=AsyncMock()),
        )

        # 1. Balance == 0 -> Denied
        denied1 = await handlers.check_paid_access(event, self.bot, topic_slug="paid_t")
        self.assertFalse(denied1)
        _, kwargs1 = event.message.answer.call_args
        self.assertIn("pay_topup_balance", [btn.callback_data for row in kwargs1["reply_markup"].inline_keyboard for btn in row])

        # 2. 0 < Balance < cost_per_message (balance=5, cost=10) -> Denied
        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            u.balance = 5
            await session.commit()

        denied2 = await handlers.check_paid_access(event, self.bot, topic_slug="paid_t")
        self.assertFalse(denied2)

        # 3. Balance >= cost_per_message (balance=15, cost=10) -> Allowed
        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            u.balance = 15
            await session.commit()

        allowed = await handlers.check_paid_access(event, self.bot, topic_slug="paid_t")
        self.assertTrue(allowed)

    async def test_handoff_linkage_both_messages(self):
        """P1-13: Both user and assistant handoff messages have test_attempt_id == attempt.id."""
        user_id = 20115
        import test_system_handlers
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="HandoffUser", current_topic_slug="stress", current_dialogue_id=1))
            attempt = TestAttempt(
                user_id=user_id,
                test_set_id=1,
                status="in_progress",
                purpose="general",
                answers="[]",
            )
            session.add(attempt)
            await session.flush()
            session.add(database.TestSession(
                user_id=user_id,
                test_set_id=1,
                attempt_id=attempt.id,
                answers="[]",
            ))
            await session.commit()
            att_id = attempt.id

        finish_state = AsyncMock()
        with patch("test_system_handlers.get_ai_response_for_topic", new=AsyncMock(return_value="Результат интерпретации")), \
             patch("test_system_handlers._questions", new=AsyncMock(return_value=[])):
            await test_system_handlers._finish_test(self.bot, user_id, finish_state, [])

        async with async_session_maker() as session:
            handoff_msgs = (await session.execute(
                select(DBMessage).where(DBMessage.user_id == user_id, DBMessage.message_kind == "test_handoff")
            )).scalars().all()
            self.assertEqual(len(handoff_msgs), 2)
            user_h = next(m for m in handoff_msgs if m.role == "user")
            asst_h = next(m for m in handoff_msgs if m.role == "assistant")
            self.assertEqual(user_h.test_attempt_id, att_id)
            self.assertEqual(asst_h.test_attempt_id, att_id)

    # =========================================================================
    # Substantive Regression Tests: P0-1 through P1-11
    # =========================================================================

    async def test_p0_1_monotonic_counter_separation_from_active_topic_dialogue(self):
        """P0-1: User.current_dialogue_id is monotonic high-water counter, active dialogue resolved from UserTopicSession."""
        user_id = 20201
        async with async_session_maker() as session:
            session.add(User(
                id=user_id,
                first_name="P01User",
                current_dialogue_id=20,
                current_topic_slug="stress",
                ai_disclaimer_accepted_at=msk_now(),
                ai_disclaimer_version=1,
            ))
            session.add(UserTopicSession(
                user_id=user_id,
                topic_slug="stress",
                active_dialogue_id=5,
            ))
            session.add(UserTopicSession(
                user_id=user_id,
                topic_slug="relationships",
                active_dialogue_id=12,
            ))
            await session.commit()

        # Step 1: User continues Topic A via cb_ai_continue_dialogue
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data="ai_continue_dialogue:stress:5",
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )
        state = AsyncMock()
        await handlers.cb_ai_continue_dialogue(cb, state, self.bot)

        # Assert counter remains 20 (does NOT decrease to 5!)
        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertEqual(u.current_dialogue_id, 20)
            did = await get_active_topic_dialogue_id(session, user_id, "stress")
            self.assertEqual(did, 5)

        # Step 2: Also test activate_ai_mode on Topic A (preserve topic)
        event = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="P01User"),
            chat=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
        )
        await handlers.activate_ai_mode(event, "stress", state, self.bot)
        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertEqual(u.current_dialogue_id, 20)

        # Step 3: Reset Topic B ("free_session", reset_context_on_entry=True)
        await handlers.activate_ai_mode(event, "free_session", state, self.bot)

        # Assert allocated dialogue is 21 (20 + 1) and user.current_dialogue_id is 21
        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertEqual(u.current_dialogue_id, 21)
            did_b = await get_active_topic_dialogue_id(session, user_id, "free_session")
            self.assertEqual(did_b, 21)

        # Step 4: Switch to Topic C (relationships, active=12)
        await handlers.activate_ai_mode(event, "relationships", state, self.bot)
        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertEqual(u.current_dialogue_id, 21)
            did_c = await get_active_topic_dialogue_id(session, user_id, "relationships")
            self.assertEqual(did_c, 12)

        # Step 5: Switch back to Topic A (stress, active=5)
        await handlers.activate_ai_mode(event, "stress", state, self.bot)
        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertEqual(u.current_dialogue_id, 21)
            did_a = await get_active_topic_dialogue_id(session, user_id, "stress")
            self.assertEqual(did_a, 5)

    async def test_p0_3_pre_test_first_question_failure_and_subsequent_retry(self):
        """P0-3 Test A: First-question Telegram send failure transitions claim & attempt to 'failed'; later entry retries successfully."""
        user_id = 20203
        topic_slug = "pretest_fail_topic"
        dialogue_id = 1
        test_set_id = 1

        async with async_session_maker() as session:
            diagnostics_set = await session.get(TestSet, test_set_id)
            if diagnostics_set:
                diagnostics_set.is_enabled = True
            session.add(User(
                id=user_id,
                first_name="PreFailUser",
                ai_disclaimer_accepted_at=msk_now(),
                ai_disclaimer_version=1,
            ))
            session.add(TopicDialogueConfig(
                topic_slug=topic_slug,
                pre_dialogue_test_enabled=True,
                test_set_id=test_set_id,
            ))
            session.add(TestQuestion(test_set_id=test_set_id, text="Q1 for pretest fail"))
            await session.commit()

        state = AsyncMock()

        # Step 1: Simulate _send_question raising exception
        with patch("test_system_handlers._send_question", new=AsyncMock(side_effect=RuntimeError("Telegram send error"))):
            with self.assertRaises(RuntimeError):
                await test_system_handlers.start_universal_test(
                    self.bot,
                    user_id,
                    state,
                    purpose="pre_dialogue",
                    topic_slug=topic_slug,
                    dialogue_id=dialogue_id,
                    test_set_id=test_set_id,
                )

        # Assert claim and attempt are marked 'failed'
        async with async_session_maker() as session:
            claim = (await session.execute(
                select(PreTestClaim).where(
                    PreTestClaim.user_id == user_id,
                    PreTestClaim.topic_slug == topic_slug,
                    PreTestClaim.dialogue_id == dialogue_id,
                    PreTestClaim.test_set_id == test_set_id,
                )
            )).scalar_one_or_none()
            self.assertIsNotNone(claim)
            self.assertEqual(claim.status, "failed")

            attempt = (await session.execute(
                select(TestAttempt).where(
                    TestAttempt.user_id == user_id,
                    TestAttempt.topic_slug == topic_slug,
                    TestAttempt.dialogue_id == dialogue_id,
                )
            )).scalar_one_or_none()
            self.assertIsNotNone(attempt)
            self.assertEqual(attempt.status, "failed")

        # Step 2: Later entry retries with _send_question succeeding
        with patch("test_system_handlers._send_question", new=AsyncMock()) as mock_send:
            await test_system_handlers.start_universal_test(
                self.bot,
                user_id,
                state,
                purpose="pre_dialogue",
                topic_slug=topic_slug,
                dialogue_id=dialogue_id,
                test_set_id=test_set_id,
            )
            mock_send.assert_called_once()

        # Assert claim is re-acquired and attempt is in_progress
        async with async_session_maker() as session:
            claim_after = (await session.execute(
                select(PreTestClaim).where(
                    PreTestClaim.user_id == user_id,
                    PreTestClaim.topic_slug == topic_slug,
                    PreTestClaim.dialogue_id == dialogue_id,
                    PreTestClaim.test_set_id == test_set_id,
                )
            )).scalar_one_or_none()
            self.assertEqual(claim_after.status, "in_progress")
            self.assertIsNotNone(claim_after.attempt_id)

            attempts = (await session.execute(
                select(TestAttempt).where(
                    TestAttempt.user_id == user_id,
                    TestAttempt.topic_slug == topic_slug,
                    TestAttempt.dialogue_id == dialogue_id,
                ).order_by(TestAttempt.id.asc())
            )).scalars().all()
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0].status, "failed")
            self.assertEqual(attempts[1].status, "in_progress")

    async def test_p0_3_pre_test_concurrent_retry_single_winner(self):
        """P0-3 Test B: Two workers retry one failed claim concurrently -> exactly one acquires ownership via atomic CAS."""
        user_id = 20204
        topic_slug = "concurrent_retry_topic"
        dialogue_id = 1
        test_set_id = 1

        async with async_session_maker() as session:
            session.add(PreTestClaim(
                user_id=user_id,
                topic_slug=topic_slug,
                dialogue_id=dialogue_id,
                test_set_id=test_set_id,
                status="failed",
                claimed_at=msk_now() - timedelta(minutes=5),
            ))
            await session.commit()

        barrier = asyncio.Barrier(2)
        results = []

        async def retry_worker():
            await barrier.wait()
            async with async_session_maker() as s:
                acquired, claim = await acquire_pre_test_claim(
                    s, user_id, topic_slug, dialogue_id, test_set_id
                )
                await s.commit()
                results.append(acquired)

        await asyncio.gather(retry_worker(), retry_worker())

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)

    async def test_p0_3_pre_test_stale_claim_recovery(self):
        """P0-3 Test C: Stale abandoned in_progress claim (expired lease) is recovered automatically by new caller."""
        user_id = 20205
        topic_slug = "stale_claim_topic"
        dialogue_id = 1
        test_set_id = 1
        stale_time = msk_now() - timedelta(seconds=200)

        async with async_session_maker() as session:
            session.add(PreTestClaim(
                user_id=user_id,
                topic_slug=topic_slug,
                dialogue_id=dialogue_id,
                test_set_id=test_set_id,
                status="in_progress",
                claimed_at=stale_time,
                lease_until=stale_time + timedelta(seconds=120),
            ))
            await session.commit()

        async with async_session_maker() as session:
            acquired, claim = await acquire_pre_test_claim(
                session, user_id, topic_slug, dialogue_id, test_set_id
            )
            await session.commit()
            self.assertTrue(acquired)
            self.assertIsNotNone(claim)
            self.assertEqual(claim.status, "in_progress")
            self.assertGreater(claim.lease_until, msk_now())

    async def test_p1_4_legacy_testconfig_mutation_does_not_affect_runtime(self):
        """P1-4: Mutating legacy TestConfig has zero effect on runtime; TestSet(id=1) is single source of truth."""
        from database import TestConfig

        async with async_session_maker() as session:
            tc = await session.get(TestConfig, 1)
            if not tc:
                tc = TestConfig(id=1)
                session.add(tc)
            tc.show_progress = False
            tc.interpretation_mode = "mutated_legacy_mode"
            tc.prompt = "MUTATED_LEGACY_PROMPT_NEVER_USED"

            ts = await session.get(TestSet, 1)
            if not ts:
                ts = TestSet(id=1, slug="diagnostics", name="Authoritative Set")
                session.add(ts)
            ts.show_progress = True
            ts.interpretation_prompt = "AUTHORITATIVE_TESTSET_PROMPT"
            await session.commit()

        async with async_session_maker() as session:
            cfg = await test_system_handlers._config(session)
            self.assertIsInstance(cfg, TestSet)
            self.assertEqual(cfg.id, 1)
            self.assertEqual(cfg.interpretation_prompt, "AUTHORITATIVE_TESTSET_PROMPT")
            self.assertTrue(cfg.show_progress)

            missing_cfg = await test_system_handlers._get_test_set(session, test_set_id=99999)
            self.assertIsNone(missing_cfg)

    async def test_p1_5_import_xlsx_questions_into_b_a_unchanged(self):
        """P1-5: Import XLSX questions into TestSet B leaves TestSet A questions unchanged."""
        import io
        import openpyxl

        admin_id = 99912
        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            ts_a = TestSet(name="Set A", slug="set_a_xlsx")
            ts_b = TestSet(name="Set B", slug="set_b_xlsx")
            session.add_all([ts_a, ts_b])
            await session.flush()
            session.add(TestQuestion(test_set_id=ts_a.id, text="Q_A_original_1"))
            session.add(TestQuestion(test_set_id=ts_a.id, text="Q_A_original_2"))
            await session.commit()
            id_a, id_b = ts_a.id, ts_b.id

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Вопросы"
        ws.append(["Вопрос", "Категория", "Реверс"])
        ws.append(["Как ваше самочувствие?", "general", False])
        buf = io.BytesIO()
        wb.save(buf)
        xlsx_bytes = buf.getvalue()

        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"target_ts_id": id_b})
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            document=SimpleNamespace(file_name="questions.xlsx"),
            text=None,
            answer=AsyncMock(),
        )
        mock_bot = AsyncMock()
        mock_bot.download = AsyncMock(side_effect=lambda doc, destination: destination.write(xlsx_bytes))

        with patch("test_set_admin_handlers.render_test_set_details", new=AsyncMock()):
            await test_set_admin_handlers.handle_questions_upload(message, state, mock_bot)

        async with async_session_maker() as session:
            qa = (await session.execute(select(TestQuestion).where(TestQuestion.test_set_id == id_a))).scalars().all()
            qb = (await session.execute(select(TestQuestion).where(TestQuestion.test_set_id == id_b))).scalars().all()
            self.assertEqual(len(qa), 2)
            self.assertEqual(len(qb), 1)
            self.assertEqual(qb[0].text, "Как ваше самочувствие?")

    async def test_p1_5_import_csv_questions_into_b_a_unchanged(self):
        """P1-5: Import CSV questions into TestSet B leaves TestSet A questions unchanged."""
        admin_id = 99913
        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            ts_a = TestSet(name="Set A", slug="set_a_csv")
            ts_b = TestSet(name="Set B", slug="set_b_csv")
            session.add_all([ts_a, ts_b])
            await session.flush()
            session.add(TestQuestion(test_set_id=ts_a.id, text="Q_A1"))
            session.add(TestQuestion(test_set_id=ts_a.id, text="Q_A2"))
            await session.commit()
            id_a, id_b = ts_a.id, ts_b.id

        csv_content = "Вопрос;Категория;Реверс\nКаково твое самочувствие?;general;False\n"

        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"target_ts_id": id_b})
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            document=SimpleNamespace(file_name="questions.csv"),
            text=None,
            answer=AsyncMock(),
        )
        mock_bot = AsyncMock()
        mock_bot.download = AsyncMock(side_effect=lambda doc, destination: destination.write(csv_content.encode("utf-8")))

        with patch("test_set_admin_handlers.render_test_set_details", new=AsyncMock()):
            await test_set_admin_handlers.handle_questions_upload(message, state, mock_bot)

        async with async_session_maker() as session:
            qa = (await session.execute(select(TestQuestion).where(TestQuestion.test_set_id == id_a))).scalars().all()
            qb = (await session.execute(select(TestQuestion).where(TestQuestion.test_set_id == id_b))).scalars().all()
            self.assertEqual(len(qa), 2)
            self.assertEqual(len(qb), 1)
            self.assertEqual(qb[0].text, "Каково твое самочувствие?")

    async def test_p1_5_import_block_txt_questions_into_b_a_unchanged(self):
        """P1-5: Import block TXT questions into TestSet B leaves TestSet A questions unchanged."""
        admin_id = 99914
        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            ts_a = TestSet(name="Set A", slug="set_a_txt")
            ts_b = TestSet(name="Set B", slug="set_b_txt")
            session.add_all([ts_a, ts_b])
            await session.flush()
            session.add(TestQuestion(test_set_id=ts_a.id, text="Q_A1"))
            session.add(TestQuestion(test_set_id=ts_a.id, text="Q_A2"))
            await session.commit()
            id_a, id_b = ts_a.id, ts_b.id

        txt_content = (
            "Вопрос 1: Как ты оцениваешь уровень тревожности?\n"
            "Категория: anxiety\n"
            "Варианты:\n"
            "1. Низкий = 1\n"
            "2. Средний = 2\n"
            "3. Высокий = 3\n"
        )

        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"target_ts_id": id_b})
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            document=SimpleNamespace(file_name="questions.txt"),
            text=None,
            answer=AsyncMock(),
        )
        mock_bot = AsyncMock()
        mock_bot.download = AsyncMock(side_effect=lambda doc, destination: destination.write(txt_content.encode("utf-8")))

        with patch("test_set_admin_handlers.render_test_set_details", new=AsyncMock()):
            await test_set_admin_handlers.handle_questions_upload(message, state, mock_bot)

        async with async_session_maker() as session:
            qa = (await session.execute(select(TestQuestion).where(TestQuestion.test_set_id == id_a))).scalars().all()
            qb = (await session.execute(select(TestQuestion).where(TestQuestion.test_set_id == id_b))).scalars().all()
            self.assertEqual(len(qa), 2)
            self.assertEqual(len(qb), 1)
            self.assertEqual(qb[0].text, "Как ты оцениваешь уровень тревожности?")

    async def test_p1_5_import_json_formulas_into_b_a_unchanged(self):
        """P1-5: Import JSON formulas into TestSet B parses correctly; TestSet A formulas unchanged."""
        admin_id = 99915
        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            ts_a = TestSet(name="Set A", slug="set_a_fjson", formulas_json=json.dumps([{"name": "f_a", "expression": "1+1"}]))
            ts_b = TestSet(name="Set B", slug="set_b_fjson")
            session.add_all([ts_a, ts_b])
            await session.commit()
            id_a, id_b = ts_a.id, ts_b.id

        json_formulas = [
            {"name": "total_score", "expression": "q1 + q2", "description": "Сумма баллов"}
        ]
        json_str = json.dumps(json_formulas)

        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"target_ts_id": id_b})
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            document=None,
            text=json_str,
            answer=AsyncMock(),
        )
        mock_bot = AsyncMock()

        with patch("test_set_admin_handlers.render_test_set_details", new=AsyncMock()):
            await test_set_admin_handlers.handle_formulas_upload(message, state, mock_bot)

        async with async_session_maker() as session:
            res_a = await session.get(TestSet, id_a)
            res_b = await session.get(TestSet, id_b)
            self.assertEqual(json.loads(res_a.formulas_json)[0]["name"], "f_a")
            b_formulas = json.loads(res_b.formulas_json)
            self.assertEqual(len(b_formulas), 1)
            self.assertEqual(b_formulas[0]["name"], "total_score")

    async def test_p1_5_import_xlsx_formulas_into_b_a_unchanged(self):
        """P1-5: Import XLSX formulas into TestSet B via file upload; TestSet A formulas unchanged."""
        import io
        import openpyxl

        admin_id = 99916
        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            ts_a = TestSet(name="Set A", slug="set_a_fxlsx", formulas_json=json.dumps([{"name": "f_a", "expression": "2+2"}]))
            ts_b = TestSet(name="Set B", slug="set_b_fxlsx")
            session.add_all([ts_a, ts_b])
            await session.commit()
            id_a, id_b = ts_a.id, ts_b.id

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Формулы"
        ws.append(["Название", "Выражение", "Описание"])
        ws.append(["index_res", "ans_01 * 2", "Индекс ресурса"])
        buf = io.BytesIO()
        wb.save(buf)
        xlsx_bytes = buf.getvalue()

        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"target_ts_id": id_b})
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            document=SimpleNamespace(file_name="formulas.xlsx"),
            text=None,
            answer=AsyncMock(),
        )
        mock_bot = AsyncMock()
        mock_bot.download = AsyncMock(side_effect=lambda doc, destination: destination.write(xlsx_bytes))

        with patch("test_set_admin_handlers.render_test_set_details", new=AsyncMock()):
            await test_set_admin_handlers.handle_formulas_upload(message, state, mock_bot)

        async with async_session_maker() as session:
            res_a = await session.get(TestSet, id_a)
            res_b = await session.get(TestSet, id_b)
            self.assertEqual(json.loads(res_a.formulas_json)[0]["name"], "f_a")
            b_formulas = json.loads(res_b.formulas_json)
            self.assertEqual(len(b_formulas), 1)
            self.assertEqual(b_formulas[0]["name"], "index_res")

    async def test_p1_5_import_csv_txt_formulas_into_b_a_unchanged(self):
        """P1-5: Import CSV/TXT formulas into TestSet B; TestSet A formulas unchanged."""
        admin_id = 99917
        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            ts_a = TestSet(name="Set A", slug="set_a_fcsv", formulas_json=json.dumps([{"name": "f_a", "expression": "3+3"}]))
            ts_b = TestSet(name="Set B", slug="set_b_fcsv")
            session.add_all([ts_a, ts_b])
            await session.commit()
            id_a, id_b = ts_a.id, ts_b.id

        csv_content = "Название;Формула;Описание\nstress_metric;q_01 + 10;Метрика стресса\n"

        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"target_ts_id": id_b})
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            document=SimpleNamespace(file_name="formulas.csv"),
            text=None,
            answer=AsyncMock(),
        )
        mock_bot = AsyncMock()
        mock_bot.download = AsyncMock(side_effect=lambda doc, destination: destination.write(csv_content.encode("utf-8")))

        with patch("test_set_admin_handlers.render_test_set_details", new=AsyncMock()):
            await test_set_admin_handlers.handle_formulas_upload(message, state, mock_bot)


        async with async_session_maker() as session:
            res_a = await session.get(TestSet, id_a)
            res_b = await session.get(TestSet, id_b)
            self.assertEqual(json.loads(res_a.formulas_json)[0]["name"], "f_a")
            b_formulas = json.loads(res_b.formulas_json)
            self.assertEqual(len(b_formulas), 1)
            self.assertEqual(b_formulas[0]["name"], "stress_metric")

    async def test_p1_6_prerecorded_failed_concurrent_retry_single_telegram_send(self):
        """P1-6: One failed prerecorded delivery retried concurrently by two workers -> exactly one Telegram send occurs."""
        user_id = 20206
        slug = "prerec_concurrent_topic"
        did = 1
        text = "Приветственное сообщение"

        async with async_session_maker() as session:
            session.add(TopicPrerecordedDelivery(
                user_id=user_id,
                topic_slug=slug,
                dialogue_id=did,
                status="failed",
                claimed_at=msk_now() - timedelta(minutes=2),
            ))
            await session.commit()

        mock_bot = MockBot()
        barrier = asyncio.Barrier(2)
        results = []

        async def worker():
            await barrier.wait()
            async with async_session_maker() as session:
                res = await deliver_prerecorded_first_message(
                    mock_bot, session, user_id, slug, did, text
                )
                results.append(res)

        await asyncio.gather(worker(), worker())

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)
        self.assertEqual(len(mock_bot.sent_messages), 1)
        self.assertEqual(mock_bot.sent_messages[0].text, text)

    async def test_p1_7_summary_claim_fencing_lost_owner_cannot_persist(self):
        """P1-7: Stale worker whose lease expired cannot persist SessionSummary; new winner persists."""
        user_id = 20207
        slug = "fencing_topic"
        did = 1

        async with async_session_maker() as session:
            session.add(DBMessage(user_id=user_id, topic_slug=slug, dialogue_id=did, role="user", content="Вопрос 1"))
            session.add(DBMessage(user_id=user_id, topic_slug=slug, dialogue_id=did, role="assistant", content="Ответ 1"))
            await session.commit()

        # Worker A acquires claim
        async with async_session_maker() as session:
            ok, claim_a, token_a = await claim_summary_generation(session, user_id, slug, did, version=1, lease_seconds=120)
            self.assertTrue(ok)
            claim_id = claim_a.id

        # Worker A starts LLM... meanwhile lease expires and Worker B reclaims it
        async with async_session_maker() as session:
            await session.execute(
                update(SummaryClaim)
                .where(SummaryClaim.id == claim_id)
                .values(
                    owner_token="token_b_winner",
                    claimed_at=msk_now(),
                    lease_until=msk_now() + timedelta(seconds=120),
                )
            )
            await session.commit()

        # Worker A attempts to finish/persist with its stale token
        async with async_session_maker() as session:
            finished = await finish_summary_claim(session, claim_id, token_a, "completed")
            self.assertFalse(finished)

        # Worker B finishes and persists
        async with async_session_maker() as session:
            finished_b = await finish_summary_claim(session, claim_id, "token_b_winner", "completed")
            self.assertTrue(finished_b)
            session.add(SessionSummary(
                user_id=user_id,
                topic_slug=slug,
                topic_slug_snapshot=slug,
                topic_name_snapshot="Fencing Topic",
                dialogue_id=did,
                version=1,
                summary_text="Резюме от воркера B",
                created_at=msk_now(),
            ))
            await session.commit()

        # Assert SessionSummary contains Worker B's summary and only 1 version exists
        async with async_session_maker() as session:
            summaries = (await session.execute(
                select(SessionSummary).where(
                    SessionSummary.user_id == user_id,
                    SessionSummary.topic_slug_snapshot == slug,
                    SessionSummary.dialogue_id == did,
                    SessionSummary.version == 1,
                )
            )).scalars().all()
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].summary_text, "Резюме от воркера B")

    async def test_p1_7_summary_claim_live_worker_heartbeat_prevents_takeover(self):
        """P1-7: Live worker renewing lease prevents second worker from reclaiming."""
        user_id = 20208
        slug = "heartbeat_topic"
        did = 1

        async with async_session_maker() as session:
            ok, claim, token = await claim_summary_generation(session, user_id, slug, did, version=1, lease_seconds=120)
            self.assertTrue(ok)
            claim_id = claim.id

        # Worker renews lease (heartbeat)
        async with async_session_maker() as session:
            renewed = await renew_summary_claim_lease(session, claim_id, token, lease_seconds=120)
            self.assertTrue(renewed)

        # Worker B tries to claim while lease is live
        async with async_session_maker() as session:
            claimed_b, claim_b, token_b = await claim_summary_generation(session, user_id, slug, did, version=1, lease_seconds=120)
            self.assertFalse(claimed_b)
            self.assertEqual(claim_b.owner_token, token)

    async def test_p1_8_two_generate_summary_clicks_in_progress_no_failure(self):
        """P1-8: Two Generate Summary clicks -> one generator, second caller gets already_generating without failure."""
        user_id = 20209
        slug = "summary_twoclick_topic"
        did = 1

        async with async_session_maker() as session:
            session.add(TopicDialogueConfig(topic_slug=slug, summary_generation_enabled=True))
            session.add(DBMessage(user_id=user_id, topic_slug=slug, dialogue_id=did, role="user", content="Вопрос"))
            session.add(DBMessage(user_id=user_id, topic_slug=slug, dialogue_id=did, role="assistant", content="Ответ"))
            await session.commit()

        # First caller acquires claim and is in progress
        async with async_session_maker() as session:
            claimed, claim, token = await claim_summary_generation(session, user_id, slug, did, version=1)
            self.assertTrue(claimed)

        # Second caller calls generate_session_summary
        res2 = await generate_session_summary(user_id, slug, did, version=1)
        self.assertIsNotNone(res2)
        self.assertTrue(getattr(res2, "is_generating", False))
        self.assertEqual(getattr(res2, "status", None), "already_generating")

    async def test_p1_8_two_simultaneous_regenerate_clicks_single_version(self):
        """P1-8: Two simultaneous Regenerate clicks allocate the same next version cleanly with atomic reservation."""
        user_id = 20210
        slug = "summary_tworegen_topic"
        did = 1

        async with async_session_maker() as session:
            session.add(TopicDialogueConfig(topic_slug=slug, summary_generation_enabled=True))
            session.add(DBMessage(user_id=user_id, topic_slug=slug, dialogue_id=did, role="user", content="Вопрос"))
            session.add(DBMessage(user_id=user_id, topic_slug=slug, dialogue_id=did, role="assistant", content="Ответ"))
            session.add(SessionSummary(
                user_id=user_id,
                topic_slug=slug,
                topic_slug_snapshot=slug,
                dialogue_id=did,
                version=1,
                summary_text="Итог v1",
            ))
            await session.commit()

        cb1 = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ai_regen_summary:{slug}:{did}",
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )
        cb2 = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ai_regen_summary:{slug}:{did}",
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )

        with patch("ai_dialogue_service._generate_summary_chunk", new=AsyncMock(return_value="Итог v2")):
            await asyncio.gather(
                handlers.cb_ai_regen_summary(cb1, AsyncMock(), self.bot),
                handlers.cb_ai_regen_summary(cb2, AsyncMock(), self.bot),
            )

        async with async_session_maker() as session:
            v2 = (await session.execute(
                select(SessionSummary).where(
                    SessionSummary.user_id == user_id,
                    SessionSummary.topic_slug_snapshot == slug,
                    SessionSummary.dialogue_id == did,
                    SessionSummary.version == 2,
                )
            )).scalars().all()
            self.assertEqual(len(v2), 1)

    async def test_p1_9_finish_main_menu_does_not_generate_summary(self):
        """P1-9: Finish -> Main menu click uses main_menu_action and does NOT generate summary."""
        topic_slug = "stress"
        dialogue_id = 5

        # 1. Keyboard contains main_menu_action
        markup = kb.topic_finish_actions_keyboard(topic_slug, dialogue_id, has_summary=True)
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("main_menu_action", callbacks)
        self.assertNotIn("exit_ai_mode", callbacks)

        # 2. Even if exit_ai_mode_callback is called, generate_session_summary is NOT called
        user_id = 20211
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data="exit_ai_mode",
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock(), chat=SimpleNamespace(id=user_id)),
        )
        state = AsyncMock()
        with patch("handlers.generate_session_summary", new=AsyncMock()) as mock_gen, \
             patch("handlers.cmd_menu", new=AsyncMock()):
            await handlers.exit_ai_mode_callback(cb, state, self.bot)
            mock_gen.assert_not_called()

    async def test_p1_10_canonical_profile_in_standalone_interp_handoff_and_summary_payloads(self):
        """P1-10: Outbound LLM payloads contain canonical profile fields and no billing metadata across interp, handoff, and summary."""
        user_id = 20212
        topic_slug = "stress"
        did = 1

        async with async_session_maker() as session:
            session.add(User(
                id=user_id,
                name="Алексей",
                first_name="Alex",
                gender="мужской",
                age=32,
                response_length="short",
                balance=150,
                subscription_end=msk_now() + timedelta(days=30),
            ))
            cfg = (await session.execute(
                select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == topic_slug)
            )).scalar_one_or_none()
            if not cfg:
                cfg = TopicDialogueConfig(topic_slug=topic_slug)
                session.add(cfg)
            cfg.summary_generation_enabled = True

            ts = await session.get(TestSet, 1)
            if not ts:
                ts = TestSet(id=1, slug="diagnostics", name="Diagnostics")
                session.add(ts)
            ts.separate_result_prompt_enabled = True
            ts.result_system_prompt = "Промпт интерпретации"

            attempt = TestAttempt(
                user_id=user_id,
                test_set_id=1,
                topic_slug=topic_slug,
                dialogue_id=did,
                status="in_progress",
                purpose="pre_dialogue",
                answers="[]",
            )
            session.add(attempt)
            await session.flush()
            session.add(database.TestSession(
                user_id=user_id,
                test_set_id=1,
                topic_slug=topic_slug,
                invocation_topic_slug=topic_slug,
                invocation_dialogue_id=did,
                attempt_id=attempt.id,
                answers="[]",
            ))
            await session.commit()

        captured_prompts = []
        async def mock_standalone(system_prompt, user_prompt, **kwargs):
            captured_prompts.append(("standalone", system_prompt, user_prompt))
            return "Предварительная интерпретация"

        async def mock_topic_ai(uid, prompt, uname, topic_slug=None, dialogue_id=None, **kwargs):
            captured_prompts.append(("handoff_topic", "", prompt))
            return "Итоговый ответ"

        finish_state = AsyncMock()
        with patch("test_system_handlers.get_standalone_ai_response", side_effect=mock_standalone), \
             patch("test_system_handlers.get_ai_response_for_topic", side_effect=mock_topic_ai), \
             patch("test_system_handlers._questions", new=AsyncMock(return_value=[])):
            await test_system_handlers._finish_test(self.bot, user_id, finish_state, [])

        # 1. Verify standalone interpretation system prompt contains canonical profile
        standalone_calls = [p for p in captured_prompts if p[0] == "standalone"]
        self.assertEqual(len(standalone_calls), 1)
        _, sys_p, _ = standalone_calls[0]
        self.assertIn("Алексей", sys_p)
        self.assertIn("мужской", sys_p)
        self.assertIn("32", sys_p)
        self.assertIn("кратк", sys_p.lower())
        self.assertNotIn("150", sys_p)
        self.assertNotIn("balance", sys_p.lower())
        self.assertNotIn("subscription", sys_p.lower())

        # 2. Verify test handoff prompt includes canonical profile context
        handoff_calls = [p for p in captured_prompts if p[0] == "handoff_topic"]
        self.assertEqual(len(handoff_calls), 1)
        _, _, handoff_p = handoff_calls[0]
        self.assertIn("Алексей", handoff_p)
        self.assertIn("мужской", handoff_p)
        self.assertIn("32", handoff_p)
        self.assertNotIn("150", handoff_p)
        self.assertNotIn("balance", handoff_p.lower())

        # 3. Verify SessionSummary Map/Reduce system prompts contain profile context
        summary_systems = []
        async def mock_summary_chunk(system, prompt):
            summary_systems.append(system)
            return "Частичный итог"

        with patch("ai_dialogue_service._generate_summary_chunk", side_effect=mock_summary_chunk):
            await generate_session_summary(user_id, topic_slug, did, version=1)

        self.assertTrue(len(summary_systems) >= 1)
        for s in summary_systems:
            self.assertIn("Алексей", s)
            self.assertIn("мужской", s)
            self.assertIn("32", s)
            self.assertNotIn("150", s)
            self.assertNotIn("balance", s.lower())

    async def test_p1_11_start_button_rejects_stale_dialogue_id(self):
        """P1-11: Start button with stale dialogue_id is rejected with alert and does not hijack active dialogue."""
        user_id = 20213
        topic_slug = "stress"

        async with async_session_maker() as session:
            session.add(User(
                id=user_id,
                first_name="StaleBtnUser",
                ai_disclaimer_accepted_at=msk_now(),
                ai_disclaimer_version=1,
            ))
            session.add(UserTopicSession(
                user_id=user_id,
                topic_slug=topic_slug,
                active_dialogue_id=2,
            ))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ai_start_dialogue:{topic_slug}:1",
            answer=AsyncMock(),
            message=SimpleNamespace(delete=AsyncMock()),
        )
        state = AsyncMock()

        await handlers.cb_ai_start_dialogue(cb, state, self.bot)
        cb.answer.assert_any_call("⚠️ Эта кнопка устарела. Начни диалог заново из меню темы.", show_alert=True)

    # =========================================================================
    # SENIOR REVIEW THIRD FIX PASS TESTS (P0-1 through P1-12)
    # =========================================================================

    async def test_p0_1_answers_continue_over_120s_and_reentry_preserves_attempt(self):
        """P0-1: Activity (>120s) renews PreTestClaim lease; re-entry preserves attempt and restores answering state."""
        user_id = 31001
        slug = "stress"
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="UserP0_1", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            diagnostics_set = await session.get(TestSet, 1)
            if diagnostics_set:
                diagnostics_set.is_enabled = True
            cfg = (await session.execute(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == slug))).scalar_one_or_none()
            if not cfg:
                cfg = TopicDialogueConfig(topic_slug=slug)
                session.add(cfg)
            cfg.pre_dialogue_test_enabled = True
            cfg.test_set_id = 1
            cfg.reset_dialogue_on_entry = True
            session.add_all([
                TestQuestion(test_set_id=1, text="Вопрос 1", variable_name="q1", sort_order=0, answer_options_json='[{"text":"Да","value":1},{"text":"Нет","value":0}]'),
                TestQuestion(test_set_id=1, text="Вопрос 2", variable_name="q2", sort_order=1, answer_options_json='[{"text":"Да","value":1},{"text":"Нет","value":0}]'),
            ])
            await session.commit()

        state = AsyncMock()
        event = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="UserP0_1"),
            chat=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
        )
        await handlers.activate_ai_mode(event, slug, state, self.bot)
        state.set_state.assert_called_with(test_system_handlers.UniversalTestStates.answering)

        async with async_session_maker() as session:
            did = await get_active_topic_dialogue_id(session, user_id, slug)
            claim = await get_pre_test_claim(session, user_id, slug, did, test_set_id=1)
            self.assertIsNotNone(claim)
            self.assertEqual(claim.status, "in_progress")
            initial_lease = claim.lease_until
            attempt = await session.get(TestAttempt, claim.attempt_id)
            self.assertIsNotNone(attempt)
            attempt_id = attempt.id

        await test_system_handlers._save_answer(user_id, "test_opt_0_0", is_text=False)
        async with async_session_maker() as session:
            claim_after = await get_pre_test_claim(session, user_id, slug, did, test_set_id=1)
            self.assertGreater(claim_after.lease_until, initial_lease)

        state.reset_mock()
        await handlers.activate_ai_mode(event, slug, state, self.bot)

        state.set_state.assert_called_with(test_system_handlers.UniversalTestStates.answering)
        async with async_session_maker() as session:
            tsess = await session.get(TestSession, user_id)
            self.assertIsNotNone(tsess)
            self.assertEqual(tsess.attempt_id, attempt_id)
            self.assertEqual(tsess.current_question_index, 1)

    async def test_p0_1_reenter_while_live_claim_exists_keeps_fsm_and_session(self):
        """P0-1: Re-entering topic when a live pre-dialogue claim exists does not break FSM or replace TestSession."""
        user_id = 31002
        slug = "stress"
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="UserP0_1B", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            diagnostics_set = await session.get(TestSet, 1)
            if diagnostics_set:
                diagnostics_set.is_enabled = True
            cfg = (await session.execute(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == slug))).scalar_one_or_none()
            if not cfg:
                cfg = TopicDialogueConfig(topic_slug=slug)
                session.add(cfg)
            cfg.pre_dialogue_test_enabled = True
            cfg.test_set_id = 1
            session.add_all([
                TestQuestion(test_set_id=1, text="Вопрос 1", variable_name="q1", sort_order=0, answer_options_json='[{"text":"Да","value":1}]'),
                TestQuestion(test_set_id=1, text="Вопрос 2", variable_name="q2", sort_order=1, answer_options_json='[{"text":"Да","value":1}]'),
            ])
            await session.commit()

        state = AsyncMock()
        event = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="UserP0_1B"),
            chat=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), delete=AsyncMock()),
        )
        await handlers.activate_ai_mode(event, slug, state, self.bot)
        await test_system_handlers._save_answer(user_id, "test_opt_0_0", is_text=False)

        state.reset_mock()
        await handlers.activate_ai_mode(event, slug, state, self.bot)

        state.set_state.assert_called_with(test_system_handlers.UniversalTestStates.answering)
        next_idx, total, answers = await test_system_handlers._save_answer(user_id, "test_opt_1_0", is_text=False)
        self.assertEqual(next_idx, 2)
        self.assertEqual(total, 2)

    async def test_p0_1_abandoned_stale_pretest_claim_is_recoverable(self):
        """P0-1: Truly abandoned stale pre-test claim (lease_until < now) can be acquired by a new attempt."""
        user_id = 31003
        slug = "stress"
        stale_time = msk_now() - timedelta(minutes=10)
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="StaleUser", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            session.add(PreTestClaim(
                user_id=user_id,
                topic_slug=slug,
                dialogue_id=1,
                test_set_id=1,
                status="in_progress",
                claimed_at=stale_time,
                lease_until=stale_time + timedelta(seconds=120),
            ))
            await session.commit()

        async with async_session_maker() as session:
            ok, new_claim = await acquire_pre_test_claim(session, user_id, slug, 1, 1)
            self.assertTrue(ok)
            self.assertIsNotNone(new_claim)
            self.assertEqual(new_claim.status, "in_progress")
            self.assertGreater(new_claim.lease_until, msk_now())

    async def test_p0_2_directive_start_test_uses_topic_and_dialogue_snapshot(self):
        """P0-2: [START_TEST] directive scopes TestSession and TestAttempt to snapshot.topic_slug and dialogue_id."""
        user_id = 31004
        topic_slug = "relationships"
        async with async_session_maker() as session:
            session.add(User(
                id=user_id,
                first_name="UserP0_2",
                current_dialogue_id=20,
                current_topic_slug=topic_slug,
                ai_disclaimer_accepted_at=msk_now(),
                ai_disclaimer_version=1,
            ))
            diagnostics_set = await session.get(TestSet, 1)
            if diagnostics_set:
                diagnostics_set.is_enabled = True
            session.add(UserTopicSession(
                user_id=user_id,
                topic_slug=topic_slug,
                active_dialogue_id=5,
            ))
            session.add_all([
                TestQuestion(test_set_id=1, text="Вопрос 1", variable_name="q1", sort_order=0, answer_options_json='[{"text":"Да","value":1}]'),
            ])
            await session.commit()

        snapshot = TopicContextSnapshot(
            user_id=user_id,
            topic_slug=topic_slug,
            dialogue_id=5,
        )

        state = AsyncMock()
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=user_id),
            from_user=SimpleNamespace(id=user_id, first_name="UserP0_2"),
            text="Пройти тест",
            answer=AsyncMock(),
        )

        with patch("handlers.get_ai_response_for_topic", new=AsyncMock(return_value="[START_TEST] Начинаем диагностику.")):
            await handlers.process_chat(msg, state, self.bot)

        async with async_session_maker() as session:
            tsess = await session.get(TestSession, user_id)
            self.assertIsNotNone(tsess)
            self.assertEqual(tsess.invocation_dialogue_id, 5)
            self.assertEqual(tsess.invocation_topic_slug, topic_slug)

            attempt = (await session.execute(
                select(TestAttempt).where(TestAttempt.user_id == user_id).order_by(TestAttempt.id.desc())
            )).scalar_one_or_none()
            self.assertIsNotNone(attempt)
            self.assertEqual(attempt.dialogue_id, 5)
            self.assertEqual(attempt.topic_slug, topic_slug)

            user = await session.get(User, user_id)
            self.assertEqual(user.current_dialogue_id, 20)

    async def test_p0_3_ureset_dialog_clears_db_and_sessions_without_lowering_counter(self):
        """P0-3: /ureset_dialog deletes messages, UserTopicSession, TestSession, claims; preserves counter and TestAttempt."""
        user_id = 31005
        topic_slug = "stress"
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ResetUser", is_admin=True, current_dialogue_id=15, current_topic_slug=topic_slug))
            session.add(UserTopicSession(user_id=user_id, topic_slug=topic_slug, active_dialogue_id=15))
            session.add(DBMessage(user_id=user_id, dialogue_id=15, role="user", content="Hi", topic_slug=topic_slug))
            session.add(TestSession(user_id=user_id, invocation_dialogue_id=15, answers="[]"))
            session.add(TestAttempt(user_id=user_id, dialogue_id=15, status="completed", answers="[]"))
            session.add(PreTestClaim(user_id=user_id, topic_slug=topic_slug, dialogue_id=15, test_set_id=1, status="in_progress", claimed_at=msk_now(), lease_until=msk_now() + timedelta(seconds=60)))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ureset_dialog_{user_id}",
            answer=AsyncMock(),
            model_copy=lambda **kw: SimpleNamespace(data=kw.get("update", {}).get("data", f"admin_user_resets_{user_id}"), from_user=SimpleNamespace(id=user_id), answer=AsyncMock()),
        )
        with patch("handlers.admin_user_resets", new=AsyncMock()):
            await handlers.ureset_dialog(cb)

        async with async_session_maker() as session:
            user = await session.get(User, user_id)
            self.assertEqual(user.current_dialogue_id, 15)
            self.assertIsNone(user.current_topic_slug)

            msgs = (await session.execute(select(DBMessage).where(DBMessage.user_id == user_id, DBMessage.dialogue_id == 15))).scalars().all()
            self.assertEqual(len(msgs), 0)

            uts = (await session.execute(select(UserTopicSession).where(UserTopicSession.user_id == user_id))).scalars().all()
            self.assertEqual(len(uts), 0)

            self.assertIsNone(await session.get(TestSession, user_id))

            claims = (await session.execute(select(PreTestClaim).where(PreTestClaim.user_id == user_id, PreTestClaim.status == "in_progress"))).scalars().all()
            self.assertEqual(len(claims), 0)

            attempt = (await session.execute(select(TestAttempt).where(TestAttempt.user_id == user_id))).scalar_one_or_none()
            self.assertIsNotNone(attempt)

    async def test_p0_3_ureset_full_clears_all_dialogue_records_and_resets_counter(self):
        """P0-3: /ureset_full deletes all dialogue-scoped records and resets current_dialogue_id = 1."""
        user_id = 31006
        topic_slug = "stress"
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="FullResetUser", is_admin=True, current_dialogue_id=12, current_topic_slug=topic_slug))
            session.add(UserTopicSession(user_id=user_id, topic_slug=topic_slug, active_dialogue_id=12))
            session.add(DBMessage(user_id=user_id, dialogue_id=12, role="user", content="Hi", topic_slug=topic_slug))
            session.add(TestSession(user_id=user_id, invocation_dialogue_id=12, answers="[]"))
            session.add(TestAttempt(user_id=user_id, dialogue_id=12, status="completed", answers="[]"))
            session.add(SessionSummary(user_id=user_id, dialogue_id=12, topic_slug=topic_slug, topic_slug_snapshot=topic_slug, summary_text="Sum", version=1))
            session.add(SummaryClaim(user_id=user_id, dialogue_id=12, topic_slug=topic_slug, version=1, status="completed", claimed_at=msk_now(), lease_until=msk_now(), owner_token="t"))
            session.add(TopicPrerecordedDelivery(user_id=user_id, topic_slug=topic_slug, dialogue_id=12, status="delivered"))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ureset_full_{user_id}",
            answer=AsyncMock(),
            model_copy=lambda **kw: SimpleNamespace(data=kw.get("update", {}).get("data", f"admin_user_resets_{user_id}"), from_user=SimpleNamespace(id=user_id), answer=AsyncMock()),
        )
        with patch("handlers.admin_user_resets", new=AsyncMock()):
            await handlers.ureset_full(cb)

        async with async_session_maker() as session:
            user = await session.get(User, user_id)
            self.assertEqual(user.current_dialogue_id, 1)
            self.assertIsNone(user.current_topic_slug)

            self.assertEqual(len((await session.execute(select(DBMessage).where(DBMessage.user_id == user_id))).scalars().all()), 0)
            self.assertEqual(len((await session.execute(select(UserTopicSession).where(UserTopicSession.user_id == user_id))).scalars().all()), 0)
            self.assertEqual(len((await session.execute(select(TestAttempt).where(TestAttempt.user_id == user_id))).scalars().all()), 0)
            self.assertEqual(len((await session.execute(select(SessionSummary).where(SessionSummary.user_id == user_id))).scalars().all()), 0)
            self.assertEqual(len((await session.execute(select(SummaryClaim).where(SummaryClaim.user_id == user_id))).scalars().all()), 0)
            self.assertEqual(len((await session.execute(select(TopicPrerecordedDelivery).where(TopicPrerecordedDelivery.user_id == user_id))).scalars().all()), 0)

    async def test_p1_4_start_button_atomic_validation_rejects_after_concurrent_reset(self):
        """P0-2: Deterministic barrier test: active=5, Start task blocked before validation, Reset commits active=6, Start is rejected."""
        user_id = 31007
        topic_slug = "stress"
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="AtomicUser", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            session.add(UserTopicSession(user_id=user_id, topic_slug=topic_slug, active_dialogue_id=5))
            cfg = (await session.execute(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == topic_slug))).scalar_one_or_none()
            if not cfg:
                cfg = TopicDialogueConfig(topic_slug=topic_slug)
                session.add(cfg)
            cfg.start_dialogue_button_enabled = True
            cfg.prerecorded_first_message = "Prerecorded message 5"
            await session.commit()

        start_at_barrier = asyncio.Event()
        reset_committed = asyncio.Event()

        orig_touch = handlers.touch_user_activity

        async def barrier_touch(uid):
            await orig_touch(uid)
            start_at_barrier.set()
            await reset_committed.wait()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ai_start_dialogue:{topic_slug}:5",
            answer=AsyncMock(),
            message=SimpleNamespace(delete=AsyncMock()),
        )
        state = AsyncMock()

        with patch("handlers.touch_user_activity", side_effect=barrier_touch):
            start_task = asyncio.create_task(handlers.cb_ai_start_dialogue(cb, state, self.bot))
            await start_at_barrier.wait()

            # Concurrent Reset task runs while Start is paused right before atomic validation
            async with async_session_maker() as session:
                await set_active_topic_dialogue_id(session, user_id, topic_slug, 6)
                user = await session.get(User, user_id)
                user.current_dialogue_id = 6
                await session.commit()

            # Release Start task
            reset_committed.set()
            await start_task

        # Assert: Start is rejected with alert
        cb.answer.assert_any_call("⚠️ Эта кнопка устарела. Начни диалог заново из меню темы.", show_alert=True)

        async with async_session_maker() as session:
            # Assert: active remains 6
            uts = (await session.execute(
                select(UserTopicSession).where(
                    UserTopicSession.user_id == user_id,
                    UserTopicSession.topic_slug == topic_slug,
                )
            )).scalar_one_or_none()
            self.assertIsNotNone(uts)
            self.assertEqual(uts.active_dialogue_id, 6)

            # Assert: no prerecorded delivery for dialogue 5
            deliv = (await session.execute(
                select(TopicPrerecordedDelivery).where(
                    TopicPrerecordedDelivery.user_id == user_id,
                    TopicPrerecordedDelivery.topic_slug == topic_slug,
                    TopicPrerecordedDelivery.dialogue_id == 5,
                )
            )).scalar_one_or_none()
            self.assertIsNone(deliv)

        # Assert: FSM does not enter chat for dialogue 5
        state.set_state.assert_not_called()


    async def test_p1_5_config_strict_no_testconfig_fallback(self):
        """P1-5: _config returns None when TestSet missing; does NOT reconstruct from TestConfig."""
        async with async_session_maker() as session:
            await session.execute(delete(TestSet))
            tc = await session.get(database.TestConfig, 1)
            if not tc:
                tc = database.TestConfig(id=1)
                session.add(tc)
            tc.formulas_enabled = True
            tc.formulas_json = '[{"name":"foo"}]'
            await session.commit()

        async with async_session_maker() as session:
            res = await test_system_handlers._config(session)
            self.assertIsNone(res)
            res_get = await test_system_handlers._get_test_set(session, 1)
            self.assertIsNone(res_get)
            count = await session.scalar(select(func.count(TestSet.id)))
            self.assertEqual(count, 0)

    async def test_p1_6_diagnostics_test_set_deletion_and_archival_rejected(self):
        """P1-6: Deletion or archival of diagnostic test set (id=1 / slug=diagnostics) is rejected with warning."""
        admin_id = 31008
        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="AdminUser", is_admin=True))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            data="ts_delete_1",
            answer=AsyncMock(),
        )
        await test_set_admin_handlers.delete_or_archive_test_set(cb)

        cb.answer.assert_called_with("⚠️ Нельзя удалить или архивировать системный диагностический тест.", show_alert=True)
        async with async_session_maker() as session:
            ts = await session.get(TestSet, 1)
            self.assertIsNotNone(ts)
            self.assertFalse(ts.is_archived)

    async def test_p1_7_non_admin_test_set_callback_rejected(self):
        """P1-7: Non-admin callback to test set admin handler is rejected and DB is unchanged."""
        user_id = 31009
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="NonAdmin", is_admin=False))
            ts = await session.get(TestSet, 1)
            ts.is_enabled = True
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data="ts_toggle_enabled_1",
            answer=AsyncMock(),
        )
        await test_set_admin_handlers.toggle_ts_enabled(cb)

        cb.answer.assert_called_with("Доступ запрещен.", show_alert=True)
        async with async_session_maker() as session:
            ts = await session.get(TestSet, 1)
            self.assertTrue(ts.is_enabled)

    async def test_p1_7_non_admin_topic_callback_rejected(self):
        """P1-7: Non-admin callback to topic dialogue admin handler is rejected and DB is unchanged."""
        user_id = 31010
        slug = "stress"
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="NonAdmin2", is_admin=False))
            cfg = (await session.execute(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == slug))).scalar_one_or_none()
            if not cfg:
                cfg = TopicDialogueConfig(topic_slug=slug)
                session.add(cfg)
            cfg.reset_dialogue_on_entry = True
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"cfg_toggle_reset_{slug}",
            answer=AsyncMock(),
        )
        await topic_admin_handlers.toggle_reset_dialogue(cb)

        cb.answer.assert_called_with("Доступ запрещен.", show_alert=True)
        async with async_session_maker() as session:
            cfg = (await session.execute(
                select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == slug)
            )).scalar_one_or_none()
            self.assertIsNotNone(cfg)
            self.assertTrue(cfg.reset_dialogue_on_entry)

    async def test_p1_8_deterministic_slug_collision_increments_suffix(self):
        """P1-8: Successive test sets with identical title generate deterministic suffixes (slug, slug_2, slug_3)."""
        admin_id = 31011
        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="Admin3", is_admin=True))
            await session.commit()

        async def _create_set(title):
            state = AsyncMock()
            state.get_data = AsyncMock(return_value={"new_title": title, "new_slug": test_set_admin_handlers._slugify(title)})
            msg = SimpleNamespace(
                from_user=SimpleNamespace(id=admin_id),
                text="-",
                answer=AsyncMock(),
            )
            with patch("test_set_admin_handlers.render_test_set_details", new=AsyncMock()):
                await test_set_admin_handlers.handle_new_description(msg, state)

        await _create_set("Тревожность")
        await _create_set("Тревожность")
        await _create_set("Тревожность")

        async with async_session_maker() as session:
            slugs = (await session.execute(
                select(TestSet.slug).where(TestSet.title == "Тревожность").order_by(TestSet.id.asc())
            )).scalars().all()
            self.assertEqual(slugs, ["тревожность", "тревожность_2", "тревожность_3"])

    async def test_p1_9_test_set_rename_and_edit_description(self):
        """P1-9: Admin can rename test set and edit its description."""
        admin_id = 31012
        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="Admin4", is_admin=True))
            ts = TestSet(id=50, title="Старое Название", slug="ts_50", is_enabled=True)
            session.add(ts)
            await session.commit()

        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"target_ts_id": 50})
        msg1 = SimpleNamespace(from_user=SimpleNamespace(id=admin_id), text="Новое Название", answer=AsyncMock())
        with patch("test_set_admin_handlers.render_test_set_details", new=AsyncMock()):
            await test_set_admin_handlers.handle_rename_test_set(msg1, state)

        msg2 = SimpleNamespace(from_user=SimpleNamespace(id=admin_id), text="Новое Описание", answer=AsyncMock())
        with patch("test_set_admin_handlers.render_test_set_details", new=AsyncMock()):
            await test_set_admin_handlers.handle_edit_description(msg2, state)

        async with async_session_maker() as session:
            updated = await session.get(TestSet, 50)
            self.assertEqual(updated.title, "Новое Название")
            self.assertEqual(updated.description, "Новое Описание")

    async def test_p1_9_test_set_preview_launches_exact_test_set(self):
        """P1-4: Preview runs exact TestSet, completes without DBMessage handoff, clears FSM, and allows hard delete."""
        admin_id = 31013
        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="Admin5", is_admin=True))
            ts = TestSet(
                id=60,
                title="Preview Test",
                slug="ts_60",
                is_enabled=True,
                calculation_enabled=True,
                formulas_json=json.dumps([{"name": "score", "formula": "q1 * 2"}]),
            )
            session.add(ts)
            session.add(TestQuestion(
                text="Preview Question 1",
                category="general",
                variable_name="q1",
                order_num=1,
                test_set_id=60,
                answer_options_json=json.dumps([{"text": "Option 1", "value": 5}]),
            ))
            await session.commit()

        class MockState:
            def __init__(self):
                self._state = None
                self._data = {}
            async def set_state(self, s):
                self._state = s
            async def get_state(self):
                return self._state
            async def clear(self):
                self._state = None
                self._data.clear()
            async def get_data(self):
                return self._data
            async def update_data(self, **kw):
                self._data.update(kw)

        state = MockState()

        # 1. Start preview
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            data="ts_preview_60",
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
        )
        await test_set_admin_handlers.start_test_set_preview(cb, state, self.bot)

        # 2. Answer question to complete preview test
        answer_cb = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            data="test_opt_0",
            answer=AsyncMock(),
            message=SimpleNamespace(edit_reply_markup=AsyncMock()),
        )
        await test_system_handlers.answer_test_option(answer_cb, state, self.bot)

        # 3. Assert: no production DBMessage handoff is written
        async with async_session_maker() as session:
            db_messages = (await session.execute(
                select(DBMessage).where(DBMessage.user_id == admin_id)
            )).scalars().all()
            self.assertEqual(len(db_messages), 0)

            # Assert: preview TestSession is cleaned up upon completion (P1-3)
            preview_sessions = (await session.execute(
                select(TestSession).where(TestSession.user_id == admin_id)
            )).scalars().all()
            self.assertEqual(len(preview_sessions), 0)

        # Assert: admin FSM does not become chatting (cleared back to admin mode)
        current_state = await state.get_state()
        self.assertIsNone(current_state)

        # Assert: preview alone does not force archive-on-delete
        del_cb = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            data="ts_delete_60",
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        )
        await test_set_admin_handlers.delete_or_archive_test_set(del_cb)
        del_cb.answer.assert_any_call("Набор тестов полностью удален.", show_alert=True)

        async with async_session_maker() as session:
            deleted_ts = await session.get(TestSet, 60)
            self.assertIsNone(deleted_ts)
            # Assert: no TestSession referencing deleted test set remains (P1-3)
            remaining_ts_sessions = (await session.execute(
                select(TestSession).where(TestSession.test_set_id == 60)
            )).scalars().all()
            self.assertEqual(len(remaining_ts_sessions), 0)

    async def test_p1_10_topic_welcome_media_supports_photo_video_animation_document(self):
        """P1-10: Topic welcome media handler accepts photo, video, animation, and document."""
        admin_id = 31014
        slug = "stress"
        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="Admin6", is_admin=True))
            cfg = (await session.execute(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == slug))).scalar_one_or_none()
            if not cfg:
                cfg = TopicDialogueConfig(topic_slug=slug)
                session.add(cfg)
            await session.commit()

        types_and_payloads = [
            ("photo", SimpleNamespace(photo=[SimpleNamespace(file_id="p_123")], video=None, animation=None, document=None)),
            ("video", SimpleNamespace(photo=None, video=SimpleNamespace(file_id="v_123"), animation=None, document=None)),
            ("animation", SimpleNamespace(photo=None, video=None, animation=SimpleNamespace(file_id="a_123"), document=None)),
            ("document", SimpleNamespace(photo=None, video=None, animation=None, document=SimpleNamespace(file_id="d_123"))),
        ]

        for expected_type, media_payload in types_and_payloads:
            state = AsyncMock()
            state.get_data = AsyncMock(return_value={"target_slug": slug})
            msg = SimpleNamespace(
                from_user=SimpleNamespace(id=admin_id),
                answer=AsyncMock(),
                **{k: getattr(media_payload, k) for k in ["photo", "video", "animation", "document"]},
            )
            await topic_admin_handlers.save_welcome_media(msg, state)

            async with async_session_maker() as session:
                cfg = (await session.execute(
                    select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == slug)
                )).scalar_one_or_none()
                self.assertIsNotNone(cfg)
                media = await session.get(ContentMedia, cfg.welcome_media_id)
                self.assertEqual(media.file_type, expected_type)
                self.assertEqual(cfg.welcome_media_type, expected_type)


    async def test_p1_11_summary_regeneration_atomic_version_barrier(self):
        """P1-3: Deterministic barrier test: A claims v2 and blocks in LLM; B calculates v2 and gets already_generating; no v3 claim created."""
        user_id = 31015
        slug = "stress"
        did = 1

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="RegenUser", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            session.add(SessionSummary(
                user_id=user_id,
                topic_slug=slug,
                topic_slug_snapshot=slug,
                dialogue_id=did,
                version=1,
                summary_text="Version 1 Summary",
            ))
            session.add(DBMessage(user_id=user_id, dialogue_id=did, topic_slug=slug, role="user", content="Вопрос"))
            session.add(DBMessage(user_id=user_id, dialogue_id=did, topic_slug=slug, role="assistant", content="Ответ"))
            await session.commit()

        llm_calls = 0
        a_started_llm = asyncio.Event()
        b_finished_call = asyncio.Event()

        async def mock_slow_llm(system, prompt):
            nonlocal llm_calls
            llm_calls += 1
            a_started_llm.set()
            await b_finished_call.wait()
            return "Version 2 Summary"

        with patch("ai_dialogue_service._generate_summary_chunk", side_effect=mock_slow_llm):
            task_a = asyncio.create_task(generate_session_summary(user_id, slug, did, is_regeneration=True))
            await a_started_llm.wait()

            # Caller B calls regenerate while A is actively inside LLM generating version 2
            res_b = await generate_session_summary(user_id, slug, did, is_regeneration=True)

            # Assert while generation is active:
            # 1. Caller B received already_generating
            self.assertIsNotNone(res_b)
            self.assertEqual(getattr(res_b, "status", None), "already_generating")

            # 2. Only version 2 claim exists in DB while generation is active; no version 3 claim
            async with async_session_maker() as session:
                active_claims = (await session.execute(
                    select(SummaryClaim).where(SummaryClaim.user_id == user_id)
                )).scalars().all()
                self.assertEqual([c.version for c in active_claims], [2])
                self.assertFalse(any(c.version == 3 for c in active_claims))

            # Release caller A's LLM computation
            b_finished_call.set()
            res_a = await task_a

        # Assert: exactly one LLM generation occurred
        self.assertEqual(llm_calls, 1)
        self.assertIsNotNone(res_a)
        self.assertEqual(res_a.version, 2)

        async with async_session_maker() as session:
            all_summaries = (await session.execute(
                select(SessionSummary).where(SessionSummary.user_id == user_id).order_by(SessionSummary.version.asc())
            )).scalars().all()
            self.assertEqual([s.version for s in all_summaries], [1, 2])


    async def test_p1_12_pre_v6_sqlite_fixture_migration_preserves_data_and_seeds_diagnostics(self):
        """P1-12: Migrating a pre-V6 SQLite fixture preserves sort_order values into order_num, test_sessions, and constraints."""
        import tempfile
        import sqlite3
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.ext.asyncio import AsyncSession

        fd, temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            conn = sqlite3.connect(temp_db_path)
            cur = conn.cursor()
            cur.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, first_name TEXT, is_admin BOOLEAN DEFAULT 0, current_dialogue_id INTEGER DEFAULT 1)")
            cur.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, role TEXT, content TEXT, created_at TEXT, topic_slug TEXT, dialogue_id INTEGER)")
            cur.execute("CREATE TABLE test_config (id INTEGER PRIMARY KEY, is_enabled BOOLEAN DEFAULT 1, show_progress BOOLEAN DEFAULT 1, formulas_enabled BOOLEAN DEFAULT 0, formulas_json TEXT, result_system_prompt TEXT, separate_result_prompt_enabled BOOLEAN DEFAULT 0, interpretation_input_mode TEXT DEFAULT 'all')")
            # Actual relevant schema from 30bec03: test_questions contains sort_order, NOT order_num
            cur.execute("CREATE TABLE test_questions (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general', is_reverse BOOLEAN NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0, comment TEXT, variable_name TEXT, allow_custom_answer BOOLEAN NOT NULL DEFAULT 0, buttons_layout TEXT NOT NULL DEFAULT 'vertical', answer_options_json TEXT)")
            # Actual relevant schema from 30bec03: test_sessions
            cur.execute("CREATE TABLE test_sessions (user_id INTEGER PRIMARY KEY, current_question_index INTEGER NOT NULL DEFAULT 0, answers TEXT NOT NULL DEFAULT '[]', formula_results TEXT, invocation_topic_slug TEXT, invocation_dialogue_id INTEGER, is_finished BOOLEAN NOT NULL DEFAULT 0, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(id))")
            cur.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE, name TEXT, is_active BOOLEAN DEFAULT 1)")

            cur.execute("INSERT INTO users (id, first_name, current_dialogue_id) VALUES (999, 'OldUser', 5)")
            cur.execute("INSERT INTO messages (id, user_id, role, content, created_at, topic_slug, dialogue_id) VALUES (1, 999, 'user', 'Old message', '2026-10-01 12:00:00', 'stress', 5)")
            cur.execute("INSERT INTO test_config (id, is_enabled, formulas_enabled, formulas_json) VALUES (1, 1, 1, '[{\"name\": \"total\"}]')")
            # Insert questions whose order differs from ID order:
            # id 1 sort_order 20
            # id 2 sort_order 5
            # id 3 sort_order 10
            cur.execute("INSERT INTO test_questions (id, text, variable_name, sort_order) VALUES (1, 'Question 1', 'q1', 20)")
            cur.execute("INSERT INTO test_questions (id, text, variable_name, sort_order) VALUES (2, 'Question 2', 'q2', 5)")
            cur.execute("INSERT INTO test_questions (id, text, variable_name, sort_order) VALUES (3, 'Question 3', 'q3', 10)")
            cur.execute("INSERT INTO test_questions (id, text, variable_name, sort_order) VALUES (4, 'Дата рождения', 'birth_date', 30)")
            # Insert real legacy TestSession row
            cur.execute("INSERT INTO test_sessions (user_id, current_question_index, answers, invocation_topic_slug, invocation_dialogue_id) VALUES (999, 1, '[\"A\"]', 'stress', 5)")
            cur.execute("INSERT INTO topics (slug, name) VALUES ('stress', 'Стресс')")
            conn.commit()
            conn.close()

            temp_db_url = f"sqlite+aiosqlite:///{temp_db_path}"
            custom_engine = create_async_engine(temp_db_url)
            custom_session_maker = sessionmaker(custom_engine, class_=AsyncSession, expire_on_commit=False)

            with patch("database.engine", custom_engine), patch("database.async_session_maker", custom_session_maker):
                # Run real init_db migration twice to assert idempotence
                await init_db()
                await init_db()

            conn = sqlite3.connect(temp_db_path)
            cur = conn.cursor()

            # Assert users/messages preserved
            cur.execute("SELECT id, first_name, current_dialogue_id FROM users WHERE id = 999")
            user_row = cur.fetchone()
            self.assertEqual(user_row, (999, "OldUser", 5))

            cur.execute("SELECT id, user_id, role, content, timestamp, topic_slug, dialogue_id, test_attempt_id FROM messages WHERE user_id = 999")
            msg_row = cur.fetchone()
            self.assertEqual(msg_row[0], 1)
            self.assertEqual(msg_row[1], 999)
            self.assertEqual(msg_row[2], "user")
            self.assertEqual(msg_row[3], "Old message")
            self.assertEqual(msg_row[4], "2026-10-01 12:00:00")
            self.assertEqual(msg_row[5], "stress")
            self.assertEqual(msg_row[6], 5)
            self.assertIsNone(msg_row[7])

            # Verify messages table has test_attempt_id column
            cur.execute("PRAGMA table_info(messages)")
            msg_cols = [c[1] for c in cur.fetchall()]
            self.assertIn("test_attempt_id", msg_cols)

            # Verify PRAGMA foreign_key_list(messages) contains the expected FK to test_attempts(id) ON DELETE SET NULL
            cur.execute("PRAGMA foreign_key_list(messages)")
            msg_fks = cur.fetchall()
            self.assertTrue(any(
                fk[2] == "test_attempts" and fk[3] == "test_attempt_id" and fk[4] == "id" and fk[6] == "SET NULL"
                for fk in msg_fks
            ))

            # Assert test_sets id=1 created
            cur.execute("SELECT id, slug, title, is_enabled FROM test_sets WHERE id = 1")
            ts_row = cur.fetchone()
            self.assertIsNotNone(ts_row)
            self.assertEqual(ts_row[1], "diagnostics")

            # Assert legacy order_num values are preserved exactly.
            cur.execute("SELECT id, order_num, test_set_id FROM test_questions ORDER BY id ASC")
            q_rows = cur.fetchall()
            self.assertEqual(len(q_rows), 4)
            self.assertEqual(q_rows[0], (1, 20, 1))
            self.assertEqual(q_rows[1], (2, 5, 1))
            self.assertEqual(q_rows[2], (3, 10, 1))
            self.assertEqual(q_rows[3], (4, 30, 1))

            # Assert runtime ordering becomes: id2, id3, id1
            cur.execute("SELECT id FROM test_questions ORDER BY order_num ASC")
            ordered_ids = [row[0] for row in cur.fetchall()]
            self.assertEqual(ordered_ids, [2, 3, 1, 4])

            cur.execute("SELECT is_active FROM test_questions WHERE id = 4")
            self.assertEqual(cur.fetchone()[0], 0)

            cur.execute("SELECT slug, expected_question_count FROM test_sets ORDER BY slug")
            test_sets = dict(cur.fetchall())
            self.assertEqual(test_sets["diagnostics"], 7)
            self.assertEqual(test_sets["express_psychological_profile"], 12)

            cur.execute(
                "SELECT topic_slug, test_set_id, pre_dialogue_test_enabled "
                "FROM topic_dialogue_configs WHERE topic_slug = 'express'"
            )
            express_cfg = cur.fetchone()
            self.assertIsNotNone(express_cfg)
            cur.execute("SELECT id FROM test_sets WHERE slug = 'express_psychological_profile'")
            profile_set_id = cur.fetchone()[0]
            self.assertEqual(express_cfg[1], profile_set_id)
            self.assertFalse(express_cfg[2])

            # Verify migrated schema constraints/indexes required by V6 model
            cur.execute("PRAGMA foreign_key_list(test_questions)")
            fk_list = cur.fetchall()
            self.assertTrue(any(fk[2] == "test_sets" and fk[3] == "test_set_id" and fk[4] == "id" for fk in fk_list))

            cur.execute("PRAGMA index_list(test_questions)")
            idx_list = cur.fetchall()
            self.assertTrue(any("test_set_id" in idx[1] for idx in idx_list))

            # Verify test_sessions backfill
            cur.execute("SELECT user_id, current_question_index, test_set_id FROM test_sessions WHERE user_id = 999")
            sess_row = cur.fetchone()
            self.assertIsNotNone(sess_row)
            self.assertEqual(sess_row, (999, 1, 1))

            conn.close()

            # Verify continued usability via ORM
            async with custom_session_maker() as session:
                loaded_sess = await session.get(TestSession, 999)
                self.assertIsNotNone(loaded_sess)
                self.assertEqual(loaded_sess.current_question_index, 1)
                self.assertEqual(loaded_sess.test_set_id, 1)
                # Verify ORM question ordering matches the legacy sort_order values.
                questions_orm = (await session.execute(
                    select(TestQuestion).where(TestQuestion.test_set_id == 1).order_by(TestQuestion.order_num.asc())
                )).scalars().all()
                self.assertEqual([q.id for q in questions_orm], [2, 3, 1, 4])

                # Verify can update and commit session without errors
                loaded_sess.current_question_index = 2
                await session.commit()

            await custom_engine.dispose()
        finally:
            if os.path.exists(temp_db_path):
                os.remove(temp_db_path)

    async def test_summary_generation_lost_ownership_fencing_barrier(self):
        """Test Quality: Lost ownership during LLM computation prevents persisting summary (CAS fencing)."""
        user_id = 31016
        slug = "stress"
        did = 1

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="FencedUser", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            session.add(DBMessage(user_id=user_id, dialogue_id=did, topic_slug=slug, role="user", content="My anxiety"))
            session.add(DBMessage(user_id=user_id, dialogue_id=did, topic_slug=slug, role="assistant", content="Tell me more"))
            await session.commit()

        async def hijack_claim_during_llm(system, prompt):
            async with async_session_maker() as session:
                await session.execute(
                    update(SummaryClaim)
                    .where(SummaryClaim.user_id == user_id, SummaryClaim.topic_slug == slug, SummaryClaim.dialogue_id == did)
                    .values(owner_token="competing_worker_token")
                )
                await session.commit()
            return "Summary from original worker"

        with patch("ai_dialogue_service._generate_summary_chunk", side_effect=hijack_claim_during_llm):
            result = await generate_session_summary(user_id, slug, did, version=1)

        self.assertIsNone(result)

        async with async_session_maker() as session:
            summaries = (await session.execute(
                select(SessionSummary).where(SessionSummary.user_id == user_id)
            )).scalars().all()
            self.assertEqual(len(summaries), 0)

    async def test_summary_generation_heartbeat_renews_lease_during_blocked_llm(self):
        """Test Quality: Heartbeat background task actively renews claim lease during slow LLM call."""
        user_id = 31017
        slug = "stress"
        did = 1

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="HeartbeatUser", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            session.add(DBMessage(user_id=user_id, dialogue_id=did, topic_slug=slug, role="user", content="Hello"))
            session.add(DBMessage(user_id=user_id, dialogue_id=did, topic_slug=slug, role="assistant", content="Hi"))
            await session.commit()

        heartbeat_renewals_during_llm = []
        real_renew = renew_summary_claim_lease
        llm_active = False

        async def tracked_renew(*args, **kwargs):
            ok = await real_renew(*args, **kwargs)
            if llm_active:
                heartbeat_renewals_during_llm.append(ok)
            return ok

        async def slow_llm(system, prompt):
            nonlocal llm_active
            llm_active = True
            try:
                # Sleep long enough for multiple 0.05s heartbeat ticks
                await asyncio.sleep(0.25)
                return "Summary result"
            finally:
                llm_active = False

        with (
            patch("ai_dialogue_service.renew_summary_claim_lease", side_effect=tracked_renew),
            patch("ai_dialogue_service._generate_summary_chunk", side_effect=slow_llm),
        ):
            # Pass injectable heartbeat_interval_seconds=0.05
            res = await generate_session_summary(user_id, slug, did, version=1, heartbeat_interval_seconds=0.05)

        self.assertIsNotNone(res)
        # Verify renewals occurred WHILE LLM was blocked
        self.assertGreaterEqual(len(heartbeat_renewals_during_llm), 3)
        self.assertTrue(all(heartbeat_renewals_during_llm))

    async def test_p1_5_stale_pretest_claim_takeover_marks_old_attempt_abandoned(self):
        """P1-5: Stale pre-test claim takeover marks previous in-progress TestAttempt abandoned."""
        user_id = 31020
        topic_slug = "stress"
        did = 1
        set_id = 1

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="StaleTakeoverUser", ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            old_attempt = TestAttempt(
                user_id=user_id,
                test_set_id=set_id,
                topic_slug=topic_slug,
                dialogue_id=did,
                purpose="pre_dialogue",
                status="in_progress",
                answers="[]",
                started_at=msk_now() - timedelta(minutes=10),
            )
            session.add(old_attempt)
            await session.flush()
            old_attempt_id = old_attempt.id

            claim = PreTestClaim(
                user_id=user_id,
                topic_slug=topic_slug,
                dialogue_id=did,
                test_set_id=set_id,
                attempt_id=old_attempt_id,
                status="in_progress",
                claimed_at=msk_now() - timedelta(minutes=10),
                lease_until=msk_now() - timedelta(minutes=5),  # Stale lease!
            )
            session.add(claim)
            await session.commit()

        # New worker acquires claim via stale takeover
        async with async_session_maker() as session:
            ok, new_claim = await acquire_pre_test_claim(
                session, user_id, topic_slug, did, set_id, lease_seconds=120
            )
            await session.commit()

        self.assertTrue(ok)
        self.assertIsNotNone(new_claim)
        self.assertIsNone(new_claim.attempt_id)  # Cleared for new attempt binding

        # Assert old attempt was marked abandoned
        async with async_session_maker() as session:
            refreshed_old_attempt = await session.get(TestAttempt, old_attempt_id)
            self.assertIsNotNone(refreshed_old_attempt)
            self.assertEqual(refreshed_old_attempt.status, "abandoned")
            self.assertIsNotNone(refreshed_old_attempt.completed_at)

    async def test_p1_5_dialog_reset_marks_active_pretest_attempt_abandoned(self):
        """P1-5: Dialog reset preserves active pre-test TestAttempt row and marks it abandoned."""
        user_id = 31021
        topic_slug = "stress"
        did = 1
        set_id = 1

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ResetAuditUser", is_admin=True, current_dialogue_id=did, current_topic_slug=topic_slug))
            active_attempt = TestAttempt(
                user_id=user_id,
                test_set_id=set_id,
                topic_slug=topic_slug,
                dialogue_id=did,
                purpose="pre_dialogue",
                status="in_progress",
                answers="[]",
                started_at=msk_now(),
            )
            session.add(active_attempt)
            await session.flush()
            attempt_id = active_attempt.id

            session.add(PreTestClaim(
                user_id=user_id,
                topic_slug=topic_slug,
                dialogue_id=did,
                test_set_id=set_id,
                attempt_id=attempt_id,
                status="in_progress",
                claimed_at=msk_now(),
                lease_until=msk_now() + timedelta(minutes=5),
            ))
            session.add(TestSession(user_id=user_id, invocation_dialogue_id=did, attempt_id=attempt_id, answers="[]"))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            data=f"ureset_dialog_{user_id}",
            answer=AsyncMock(),
            model_copy=lambda **kw: SimpleNamespace(data=kw.get("update", {}).get("data", f"admin_user_resets_{user_id}"), from_user=SimpleNamespace(id=user_id), answer=AsyncMock()),
        )
        with patch("handlers.admin_user_resets", new=AsyncMock()):
            await handlers.ureset_dialog(cb)

        async with async_session_maker() as session:
            refreshed_attempt = await session.get(TestAttempt, attempt_id)
            self.assertIsNotNone(refreshed_attempt)
            self.assertEqual(refreshed_attempt.status, "abandoned")
            self.assertIsNotNone(refreshed_attempt.completed_at)
            # Transient session and claim are cleaned up
            self.assertIsNone(await session.get(TestSession, user_id))
            self.assertEqual(len((await session.execute(
                select(PreTestClaim).where(PreTestClaim.user_id == user_id)
            )).scalars().all()), 0)

    async def test_p1_6_null_test_set_question_does_not_leak_into_diagnostics(self):
        """P1-6: Questions with test_set_id=NULL are never loaded into diagnostics runtime."""
        async with async_session_maker() as session:
            await session.execute(delete(TestQuestion))
            session.add(TestQuestion(
                id=801,
                text="Diagnostics Q1",
                category="general",
                variable_name="dq1",
                order_num=1,
                test_set_id=1,
                answer_options_json='[{"text":"Option 1","score":1}]',
            ))
            session.add(TestQuestion(
                id=802,
                text="Diagnostics Q2",
                category="general",
                variable_name="dq2",
                order_num=2,
                test_set_id=1,
                answer_options_json='[{"text":"Option 2","score":2}]',
            ))
            null_q = TestQuestion(
                id=803,
                text="Orphaned Null Question",
                category="general",
                variable_name="nq",
                order_num=0,
                test_set_id=None,
                answer_options_json='[{"text":"Option 0","score":0}]',
            )
            session.add(null_q)
            await session.commit()

        async with async_session_maker() as session:
            diag_questions = await test_system_handlers._questions(session, test_set_id=1)
            q_ids = [q.id for q in diag_questions]
            self.assertIn(801, q_ids)
            self.assertIn(802, q_ids)
            self.assertNotIn(803, q_ids)

            default_questions = await test_system_handlers._questions(session)
            default_q_ids = [q.id for q in default_questions]
            self.assertEqual(default_q_ids, [801, 802])
            self.assertNotIn(803, default_q_ids)
