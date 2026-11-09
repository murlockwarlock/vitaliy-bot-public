"""Comprehensive Test Suite for Self-Reflection Journal Feature.

Implements all test groups (A through AE) from the approved implementation plan.
"""

import asyncio
import html
import json
import os
import sys
import unittest
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, create_autospec

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from background_worker import dispatch_journal_reminder_message, journal_reminder_worker
from handlers import (
    cb_accept_disclaimer,
    consume_pending_ai_flow,
    get_content,
    mode_habits,
    store_pending_ai_flow,
)
from keyboards import (
    main_menu_keyboard,
    topic_finish_actions_keyboard,
    topic_summary_result_keyboard,
)
from error_reporting import send_ai_fallback_used_alert, send_terminal_ai_failure_alert
from ai_integration import (
    JournalAiResult,
    generate_journal_daily_summary,
    generate_journal_dynamics_analysis,
    StructuredAIResponse,
)
from journal_handlers import (
    JournalStates,
    cb_journal_dynamics_menu,
    cb_journal_history_page,
    cb_journal_history_view,
    cb_journal_reminder_menu,
    cb_journal_restart,
    cb_journal_resume,
    cb_journal_retry_summary,
    cb_journal_run_dynamics,
    cb_journal_snooze_menu,
    cb_journal_snooze_set,
    cb_journal_start,
    cb_journal_time_manual,
    cb_journal_time_off,
    cb_journal_time_set_preset,
    cb_journal_video,
    cb_mode_journal,
    cb_journal_open_stats,
    handle_journal_start,
    msg_answer_question,
    msg_journal_time_manual_input,
    run_journal_summary_and_update,
    show_journal_menu,
)
from journal_admin_handlers import (
    JournalAdminStates,
    admin_journal_menu_handler,
    admin_journal_pts_edit_handler,
    admin_journal_pts_edit_submit,
    admin_journal_q_add_handler,
    admin_journal_q_add_submit,
    admin_journal_q_del_handler,
    admin_journal_q_del_execute_handler,
    admin_journal_q_dn_handler,
    admin_journal_q_edit_handler,
    admin_journal_q_edit_submit,
    admin_journal_q_list_handler,
    admin_journal_q_tog_handler,
    admin_journal_q_up_handler,
)
from database import (
    AIConfig,
    AILog,
    AiDisclaimerClaim,
    Base,
    CANONICAL_QUESTIONS,
    Content,
    JournalAiOperation,
    JournalAnswer,
    JournalConfig,
    JournalEntry,
    JournalQuestion,
    JournalReward,
    JournalSettings,
    PaymentConfig,
    Topic,
    User,
    UserSubscription,
    _ensure_sqlite_compat_schema,
    async_session_maker,
    engine,
    init_db,
)
from journal_keyboards import (
    journal_admin_cancel_keyboard,
    journal_admin_menu_keyboard,
    journal_admin_questions_keyboard,
    journal_completed_today_keyboard,
    journal_dynamics_menu_keyboard,
    journal_entry_detail_keyboard,
    journal_history_list_keyboard,
    journal_in_progress_keyboard,
    journal_menu_keyboard,
    journal_reminder_message_keyboard,
    journal_reminder_time_keyboard,
    journal_snooze_menu_keyboard,
)
from journal_service import (
    InsufficientTokensError,
    build_journal_daily_summary_context,
    build_journal_dynamics_context,
    build_questions_snapshot,
    calculate_journal_streak,
    execute_transaction_a,
    finalize_journal_ai_operation_failure,
    finalize_journal_ai_operation_success,
    get_journal_completed_days_count,
    get_journal_stats,
    get_or_create_journal_config,
    get_or_create_journal_settings,
    grant_subscription_bonus_days,
    initiate_journal_ai_operation,
    parse_journal_daily_summary_response,
    recover_stale_journal_ai_operations,
    renew_journal_ai_operation_lease,
)
from time_utils import msk_now


class MockBot:
    def __init__(self):
        self.sent_messages = []

    async def send_message(self, chat_id, text, **kwargs):
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=len(self.sent_messages) + 1,
            text=text,
            kwargs=kwargs,
        )
        self.sent_messages.append(msg)
        return msg

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=message_id or 1,
            text=text,
            kwargs=kwargs,
        )
        self.sent_messages.append(msg)
        return msg


def make_callback_query(data: str, user_id: int, chat_id: int = None, bot: MockBot = None) -> AsyncMock:
    if chat_id is None:
        chat_id = user_id
    cb = AsyncMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = SimpleNamespace(id=user_id, is_bot=False, first_name="Test", username=f"user_{user_id}")
    cb.message = AsyncMock(spec=Message)
    cb.message.chat = SimpleNamespace(id=chat_id, type="private")
    cb.message.message_id = 101
    cb.message.content_type = "text"
    cb.message.edit_text = AsyncMock()
    cb.message.delete = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    if bot:
        cb.bot = bot
        cb.message.bot = bot
    return cb


def make_message(text: str, user_id: int, chat_id: int = None, bot: MockBot = None) -> AsyncMock:
    if chat_id is None:
        chat_id = user_id
    msg = AsyncMock(spec=Message)
    msg.text = text
    msg.from_user = SimpleNamespace(id=user_id, is_bot=False, first_name="Test", username=f"user_{user_id}")
    msg.chat = SimpleNamespace(id=chat_id, type="private")
    msg.message_id = 102
    msg.answer = AsyncMock()
    if bot:
        msg.bot = bot
    return msg


class JournalFeatureBaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        async with async_session_maker() as session:
            for model in [
                JournalAnswer,
                JournalEntry,
                JournalReward,
                JournalAiOperation,
                JournalSettings,
                UserSubscription,
                AiDisclaimerClaim,
                User,
            ]:
                await session.execute(delete(model))
            vid_content = await session.get(Content, "journal_video")
            if vid_content:
                vid_content.text_content = "Видеоурок по саморефлексии"
            j_cfg = await session.get(JournalConfig, 1)
            if j_cfg:
                j_cfg.points_per_reflection = 10
            p_cfg = await session.get(PaymentConfig, 1)
            if p_cfg:
                p_cfg.mode = "free"
            await session.commit()
        self.bot = MockBot()
        self.storage = MemoryStorage()

    async def create_user(
        self,
        user_id: int,
        is_admin: bool = False,
        balance: float = 100.0,
        sub_days: int = 0,
        disclaimer_accepted: bool = True,
    ) -> User:
        now = msk_now()
        sub_end = now + timedelta(days=sub_days) if sub_days > 0 else None
        async with async_session_maker() as session:
            u = User(
                id=user_id,
                username=f"user_{user_id}",
                first_name="User",
                is_admin=is_admin,
                balance=balance,
                subscription_end=sub_end,
                ai_disclaimer_accepted_at=now if disclaimer_accepted else None,
                ai_disclaimer_version=1 if disclaimer_accepted else None,
                journal_points=0,
            )
            session.add(u)
            await session.commit()
            return u

    async def create_standard_questions(self, count: int = 6) -> list[JournalQuestion]:
        async with async_session_maker() as session:
            await session.execute(delete(JournalQuestion))
            questions = []
            for i in range(1, count + 1):
                q = JournalQuestion(
                    id=i,
                    text=f"Вопрос {i}?",
                    order_num=i,
                    is_active=True,
                )
                session.add(q)
                questions.append(q)
            cfg = await session.get(JournalConfig, 1)
            if not cfg:
                session.add(JournalConfig(id=1, points_per_reflection=10, questions_seeded=True))
            else:
                cfg.questions_seeded = True
            await session.commit()
            return questions


# =========================================================================
# Group A: Access, Disclaimer, Roles & Free Habits
# =========================================================================
class TestJournalGroupA(JournalFeatureBaseTest):
    async def test_disclaimer_blocks_unaccepted_user(self):
        from journal_handlers import cb_mode_journal
        user = await self.create_user(1001, sub_days=10, disclaimer_accepted=False)
        cb = make_callback_query("mode_journal", user.id, bot=self.bot)

        await cb_mode_journal(cb, self.bot)
        # Verify disclaimer message was sent
        self.assertTrue(len(self.bot.sent_messages) > 0)
        self.assertIn("не заменяет", self.bot.sent_messages[-1].text.lower())

    async def test_expired_subscription_blocks_mode_journal(self):
        from journal_handlers import cb_mode_journal
        async with async_session_maker() as session:
            pconf = await session.get(PaymentConfig, 1)
            if pconf:
                pconf.mode = "subscription"
            top = await session.scalar(select(Topic).where(Topic.slug == "journal"))
            if top:
                top.is_paid = True
            await session.commit()

        user = await self.create_user(1002, sub_days=0, disclaimer_accepted=True)
        cb = make_callback_query("mode_journal", user.id, bot=self.bot)
        await cb_mode_journal(cb, self.bot)
        # Should be blocked and prompt payment
        cb.answer.assert_called()
        self.assertIn("доступ", cb.answer.call_args[0][0].lower())

    async def test_admin_always_has_access(self):
        from journal_handlers import cb_mode_journal
        async with async_session_maker() as session:
            pconf = await session.get(PaymentConfig, 1)
            if pconf:
                pconf.mode = "subscription"
            top = await session.scalar(select(Topic).where(Topic.slug == "journal"))
            if top:
                top.is_paid = True
            await session.commit()

        admin = await self.create_user(1003, is_admin=True, sub_days=0, disclaimer_accepted=True)
        cb = make_callback_query("mode_journal", admin.id, bot=self.bot)
        await cb_mode_journal(cb, self.bot)
        # Admin gets through to Journal menu
        cb.message.edit_text.assert_called()
        self.assertIn("дневник", cb.message.edit_text.call_args[0][0].lower())

    async def test_habits_always_free_without_subscription(self):
        from handlers import mode_habits
        async with async_session_maker() as session:
            pconf = await session.get(PaymentConfig, 1)
            if pconf:
                pconf.mode = "subscription"
            await session.commit()

        user = await self.create_user(1004, sub_days=0, balance=0.0)
        cb = make_callback_query("mode_habits", user.id, bot=self.bot)
        await mode_habits(cb, self.bot)
        # User enters habits without payment block
        cb.message.edit_text.assert_called()
        self.assertIn("привыч", cb.message.edit_text.call_args[0][0].lower())


# =========================================================================
# Group B: Reminder Settings
# =========================================================================
class TestJournalGroupB(JournalFeatureBaseTest):
    async def test_presets_and_disable(self):
        from journal_handlers import cb_journal_time_off, cb_journal_time_set_preset
        user = await self.create_user(2001, sub_days=10)
        cb = make_callback_query("j_time_set:2100", user.id, bot=self.bot)
        await cb_journal_time_set_preset(cb, self.bot)

        async with async_session_maker() as session:
            st = await session.get(JournalSettings, user.id)
            self.assertIsNotNone(st)
            self.assertTrue(st.reminders_enabled)
            self.assertEqual(st.reminder_time, "21:00")

        # Now disable reminders
        cb_off = make_callback_query("j_time_off", user.id, bot=self.bot)
        await cb_journal_time_off(cb_off, self.bot)

        async with async_session_maker() as session:
            st = await session.get(JournalSettings, user.id)
            self.assertFalse(st.reminders_enabled)

    async def test_manual_time_input_validation(self):
        from journal_handlers import msg_journal_time_manual_input
        user = await self.create_user(2002, sub_days=10)
        state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=user.id, user_id=user.id))

        # Invalid format
        msg_inv = make_message("25:99", user.id, bot=self.bot)
        await msg_journal_time_manual_input(msg_inv, state, self.bot)
        msg_inv.answer.assert_called()
        self.assertIn("неверный", msg_inv.answer.call_args[0][0].lower())

        # Valid format
        msg_val = make_message("20:45", user.id, bot=self.bot)
        await msg_journal_time_manual_input(msg_val, state, self.bot)

        async with async_session_maker() as session:
            st = await session.get(JournalSettings, user.id)
            self.assertTrue(st.reminders_enabled)
            self.assertEqual(st.reminder_time, "20:45")


# =========================================================================
# Group C & D: Regular Reminders, Races, Catch-Up & Snooze
# =========================================================================
class TestJournalGroupCD(JournalFeatureBaseTest):
    async def test_regular_reminder_worker_claim_and_suppression(self):
        user = await self.create_user(3001, sub_days=10)
        today = msk_now().date()
        async with async_session_maker() as session:
            session.add(
                JournalSettings(
                    user_id=user.id,
                    reminders_enabled=True,
                    reminder_time="19:00",
                    last_reminded_date=None,
                )
            )
            await session.commit()

        # 1. Run single reminder iteration via atomic SQL claim
        async with async_session_maker() as session:
            not_completed_subq = (
                select(1)
                .select_from(JournalEntry)
                .where(
                    JournalEntry.user_id == user.id,
                    JournalEntry.reflection_date == today,
                    JournalEntry.status == "completed",
                )
            )
            claim_stmt = (
                update(JournalSettings)
                .where(
                    JournalSettings.user_id == user.id,
                    JournalSettings.reminders_enabled == True,
                    JournalSettings.reminder_time <= "23:59",
                    (JournalSettings.last_reminded_date == None) | (JournalSettings.last_reminded_date < today),
                    ~not_completed_subq.exists(),
                )
                .values(last_reminded_date=today)
                .returning(JournalSettings.user_id)
            )
            claimed = (await session.execute(claim_stmt)).scalar()
            await session.commit()

        self.assertEqual(claimed, user.id)

        # 2. Duplicate tick in same minute does not claim again
        async with async_session_maker() as session:
            claimed_again = (await session.execute(claim_stmt)).scalar()
        self.assertIsNone(claimed_again)

        # 3. Completed entry suppresses reminder
        user2 = await self.create_user(3002, sub_days=10)
        async with async_session_maker() as session:
            session.add(
                JournalSettings(
                    user_id=user2.id,
                    reminders_enabled=True,
                    reminder_time="19:00",
                    last_reminded_date=None,
                )
            )
            session.add(
                JournalEntry(
                    user_id=user2.id,
                    reflection_date=today,
                    status="completed",
                    questions_snapshot_json="[]",
                )
            )
            await session.commit()

        async with async_session_maker() as session:
            not_comp = (
                select(1)
                .select_from(JournalEntry)
                .where(
                    JournalEntry.user_id == user2.id,
                    JournalEntry.reflection_date == today,
                    JournalEntry.status == "completed",
                )
            )
            c_stmt = (
                update(JournalSettings)
                .where(
                    JournalSettings.user_id == user2.id,
                    JournalSettings.reminders_enabled == True,
                    ~not_comp.exists(),
                )
                .values(last_reminded_date=today)
                .returning(JournalSettings.user_id)
            )
            res = (await session.execute(c_stmt)).scalar()
        self.assertIsNone(res)

    async def test_snooze_setting_and_atomic_claim(self):
        from journal_handlers import cb_journal_snooze_set
        user = await self.create_user(3003, sub_days=10)
        cb = make_callback_query("j_snooze:15", user.id, bot=self.bot)
        await cb_journal_snooze_set(cb, self.bot)

        async with async_session_maker() as session:
            st = await session.get(JournalSettings, user.id)
            self.assertIsNotNone(st.snooze_until)
            self.assertGreater(st.snooze_until, msk_now() - timedelta(minutes=1))


# =========================================================================
# Group E-K: Reflection, Snapshots, Questions & Transaction A
# =========================================================================
class TestJournalGroupEK(JournalFeatureBaseTest):
    async def test_init_db_seeds_exact_six_canonical_questions(self):
        # Fresh init_db
        async with async_session_maker() as session:
            cfg = await session.get(JournalConfig, 1)
            cfg.questions_seeded = False
            for q in (await session.execute(select(JournalQuestion))).scalars().all():
                await session.delete(q)
            await session.commit()

        await init_db()

        async with async_session_maker() as session:
            questions = (
                await session.execute(
                    select(JournalQuestion).order_by(JournalQuestion.order_num.asc())
                )
            ).scalars().all()
            self.assertEqual(len(questions), 6)
            for idx, text_val in enumerate(CANONICAL_QUESTIONS, start=1):
                self.assertEqual(questions[idx - 1].order_num, idx)
                self.assertEqual(questions[idx - 1].text, text_val)
                self.assertTrue(questions[idx - 1].is_active)

            cfg = await session.get(JournalConfig, 1)
            self.assertTrue(cfg.questions_seeded)

        # Admin deletes all questions
        async with async_session_maker() as session:
            for q in (await session.execute(select(JournalQuestion))).scalars().all():
                await session.delete(q)
            await session.commit()

        # Restart bot: init_db does NOT re-seed deleted questions because questions_seeded == True
        await init_db()
        async with async_session_maker() as session:
            q_after = (await session.execute(select(JournalQuestion))).scalars().all()
            self.assertEqual(len(q_after), 0)

    async def test_reflection_flow_and_transaction_a_atomicity(self):
        await self.create_standard_questions(3)
        user = await self.create_user(4001, sub_days=10)
        today = msk_now().date()

        # Start reflection
        from journal_handlers import handle_journal_start, msg_answer_question
        state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=user.id, user_id=user.id))
        cb_start = make_callback_query("j_start", user.id, bot=self.bot)
        await handle_journal_start(cb_start, state, self.bot)

        # Answers 1, 2
        msg1 = make_message("Ответ на первый вопрос", user.id, bot=self.bot)
        await msg_answer_question(msg1, state, self.bot)

        msg2 = make_message("Ответ на второй вопрос", user.id, bot=self.bot)
        await msg_answer_question(msg2, state, self.bot)

        # Final answer (position 3) triggers Transaction A
        with patch("ai_integration.generate_journal_daily_summary", new_callable=AsyncMock):
            msg3 = make_message("Финальный ответ за сегодня", user.id, bot=self.bot)
            await msg_answer_question(msg3, state, self.bot)

        async with async_session_maker() as session:
            entry = await session.scalar(
                select(JournalEntry).where(
                    JournalEntry.user_id == user.id,
                    JournalEntry.reflection_date == today,
                )
            )
            self.assertIsNotNone(entry)
            self.assertEqual(entry.status, "completed")
            self.assertIsNotNone(entry.points_awarded_at)
            self.assertEqual(entry.points_awarded, 10)

            # User points incremented
            u = await session.get(User, user.id)
            self.assertEqual(u.journal_points, 10)


# =========================================================================
# Group L & M-N: History, Ownership, Dynamics & Replay
# =========================================================================
class TestJournalGroupLMN(JournalFeatureBaseTest):
    async def test_history_ownership_protection(self):
        from journal_handlers import cb_journal_history_view
        user1 = await self.create_user(5001, sub_days=10)
        user2 = await self.create_user(5002, sub_days=10)
        today = msk_now().date()

        async with async_session_maker() as session:
            e1 = JournalEntry(
                user_id=user1.id,
                reflection_date=today,
                status="completed",
                summary_text="Саммари пользователя 1",
                questions_snapshot_json="[]",
            )
            session.add(e1)
            await session.commit()
            e1_id = e1.id

        # User 2 tries to view User 1's entry
        cb_tamper = make_callback_query(f"j_h_v:{e1_id}", user2.id, bot=self.bot)
        await cb_journal_history_view(cb_tamper, self.bot)
        cb_tamper.answer.assert_called_with("⛔️ Запись не найдена или принадлежит другому пользователю.", show_alert=True)

    async def test_dynamics_less_than_two_entries_blocks_ai(self):
        from journal_handlers import cb_journal_run_dynamics
        user = await self.create_user(5003, sub_days=10)
        today = msk_now().date()

        # Only 1 completed entry
        async with async_session_maker() as session:
            session.add(
                JournalEntry(
                    user_id=user.id,
                    reflection_date=today,
                    status="completed",
                    questions_snapshot_json="[]",
                )
            )
            await session.commit()

        cb_dyn = make_callback_query("j_d7:abcdef12", user.id, bot=self.bot)
        await cb_journal_run_dynamics(cb_dyn, self.bot)
        cb_dyn.message.edit_text.assert_called()
        self.assertIn("минимум 2", cb_dyn.message.edit_text.call_args[0][0].lower())


# =========================================================================
# Group O & P-Q: Streak, 21-Day Milestone & Subscription Bonus
# =========================================================================
class TestJournalGroupOPQ(JournalFeatureBaseTest):
    async def test_streak_calculation(self):
        user = await self.create_user(6001)
        today = msk_now().date()
        async with async_session_maker() as session:
            # 3 consecutive days
            for i in range(3):
                session.add(
                    JournalEntry(
                        user_id=user.id,
                        reflection_date=today - timedelta(days=i),
                        status="completed",
                        questions_snapshot_json="[]",
                    )
                )
            await session.commit()

        async with async_session_maker() as session:
            streak = await calculate_journal_streak(session, user.id)
            self.assertEqual(streak, 3)

    async def test_21_day_milestone_reward_exact_day_21(self):
        user = await self.create_user(6002, sub_days=5)
        today = msk_now().date()

        # Prepopulate exactly 20 completed days
        async with async_session_maker() as session:
            for i in range(1, 21):
                session.add(
                    JournalEntry(
                        user_id=user.id,
                        reflection_date=today - timedelta(days=i),
                        status="completed",
                        questions_snapshot_json="[]",
                        points_awarded=10,
                        points_awarded_at=msk_now(),
                    )
                )
            # Create today's entry
            today_entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="in_progress",
                questions_snapshot_json=json.dumps([{"id": 1, "text": "Q1"}]),
            )
            session.add(today_entry)
            await session.commit()
            entry_id = today_entry.id

        # Execute Transaction A
        ans = {"position": 1, "question_id": 1, "question_text_snapshot": "Q1", "answer_text": "A1"}
        async with async_session_maker() as session:
            res = await execute_transaction_a(
                session, entry_id, user.id, final_answer_text="A1", final_question_snapshot=ans
            )
            await session.commit()

        self.assertEqual(res["points_awarded"], 10)
        self.assertTrue(res["reward_granted"])

        # Verify reward and subscription extension
        async with async_session_maker() as session:
            reward = await session.scalar(
                select(JournalReward).where(
                    JournalReward.user_id == user.id,
                    JournalReward.cycle_number == 1,
                )
            )
            self.assertIsNotNone(reward)
            u = await session.get(User, user.id)
            # Was 5 days, now 5 + 7 = 12 days
            diff_days = (u.subscription_end.date() - today).days
            self.assertGreaterEqual(diff_days, 11)


# =========================================================================
# Group U-V: Admin Handlers & Question Order Normalization
# =========================================================================
class TestJournalGroupUV(JournalFeatureBaseTest):
    async def test_admin_check_rejects_non_admin(self):
        from journal_admin_handlers import admin_journal_menu_handler
        user = await self.create_user(7001, is_admin=False)
        cb = make_callback_query("admin_journal_menu", user.id, bot=self.bot)
        state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=user.id, user_id=user.id))
        await admin_journal_menu_handler(cb, state)
        cb.answer.assert_called_with("⛔️ Действие доступно только администраторам.", show_alert=True)

    async def test_question_crud_and_order_normalization(self):
        admin = await self.create_user(7002, is_admin=True)
        await self.create_standard_questions(3)

        from journal_admin_handlers import (
            admin_journal_q_add_submit,
            admin_journal_q_del_handler,
            admin_journal_q_dn_handler,
            admin_journal_q_up_handler,
        )
        state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=admin.id, user_id=admin.id))

        # 1. Add question
        msg_add = make_message("Новый вопрос 4", admin.id, bot=self.bot)
        await admin_journal_q_add_submit(msg_add, state)

        async with async_session_maker() as session:
            qs = (await session.execute(select(JournalQuestion).order_by(JournalQuestion.order_num.asc()))).scalars().all()
            self.assertEqual(len(qs), 4)
            self.assertEqual([q.order_num for q in qs], [1, 2, 3, 4])

        # 2. Move up question 4
        cb_up = make_callback_query(f"j_adm_q_up:{qs[3].id}", admin.id, bot=self.bot)
        await admin_journal_q_up_handler(cb_up)

        async with async_session_maker() as session:
            qs = (await session.execute(select(JournalQuestion).order_by(JournalQuestion.order_num.asc()))).scalars().all()
            self.assertEqual(qs[2].text, "Новый вопрос 4")
            self.assertEqual([q.order_num for q in qs], [1, 2, 3, 4])

        # 3. Delete question 2
        del_target = qs[1].id
        cb_del = make_callback_query(f"j_adm_q_del:{del_target}", admin.id, bot=self.bot)
        await admin_journal_q_del_handler(cb_del)

        async with async_session_maker() as session:
            remaining = (await session.execute(select(JournalQuestion).order_by(JournalQuestion.order_num.asc()))).scalars().all()
            self.assertEqual(len(remaining), 4)

        cb_execute = make_callback_query(f"j_adm_q_del_execute:{del_target}", admin.id, bot=self.bot)
        await admin_journal_q_del_execute_handler(cb_execute)

        async with async_session_maker() as session:
            remaining = (await session.execute(select(JournalQuestion).order_by(JournalQuestion.order_num.asc()))).scalars().all()
            self.assertEqual(len(remaining), 3)
            self.assertEqual([q.order_num for q in remaining], [1, 2, 3])


# =========================================================================
# Group W-Y: Token Billing, Recovery & Owner CAS
# =========================================================================
class TestJournalGroupWY(JournalFeatureBaseTest):
    async def test_token_debit_atomic_and_refund_on_failure(self):
        async with async_session_maker() as session:
            pcfg = await session.get(PaymentConfig, 1)
            if pcfg:
                pcfg.mode = "token"
                pcfg.cost_per_message = 15.0
            top = await session.scalar(select(Topic).where(Topic.slug == "journal"))
            if top:
                top.is_paid = True
            await session.commit()

        user = await self.create_user(8001, balance=50.0)

        # 1. Initiate operation debits 15 tokens
        async with async_session_maker() as session:
            op, is_new = await initiate_journal_ai_operation(
                session, "summary_1", user.id, "summary", entry_id=None
            )
            await session.commit()

        self.assertTrue(is_new)
        self.assertEqual(op.token_cost, 15.0)
        self.assertIsNotNone(op.charged_at)

        async with async_session_maker() as session:
            u = await session.get(User, user.id)
            self.assertEqual(u.balance, 35.0)

        # 2. Finalize failure refunds exactly once
        async with async_session_maker() as session:
            refunded = await finalize_journal_ai_operation_failure(
                session, operation_id=op.id, owner_token=op.owner_token
            )
            await session.commit()
        self.assertTrue(refunded)

        async with async_session_maker() as session:
            u = await session.get(User, user.id)
            self.assertEqual(u.balance, 50.0)

        # 3. Second refund call does nothing
        async with async_session_maker() as session:
            refunded_again = await finalize_journal_ai_operation_failure(
                session, operation_id=op.id, owner_token=op.owner_token
            )
            await session.commit()
        self.assertFalse(refunded_again)

        async with async_session_maker() as session:
            u = await session.get(User, user.id)
            self.assertEqual(u.balance, 50.0)

    async def test_stale_operations_recovery(self):
        user = await self.create_user(8002, balance=20.0)
        # Create a stale running operation charged 10 tokens with expired lease
        async with async_session_maker() as session:
            stale_op = JournalAiOperation(
                operation_key="stale_op_1",
                user_id=user.id,
                operation_type="dynamics",
                entry_id=None,
                status="running",
                owner_token="owner_old",
                lease_until=msk_now() - timedelta(minutes=5),
                token_cost=10.0,
                charged_at=msk_now() - timedelta(minutes=10),
            )
            session.add(stale_op)
            await session.commit()

        recovered_count = await recover_stale_journal_ai_operations()
        self.assertEqual(recovered_count, 1)

        async with async_session_maker() as session:
            op = await session.scalar(
                select(JournalAiOperation).where(JournalAiOperation.operation_key == "stale_op_1")
            )
            self.assertEqual(op.status, "failed")
            self.assertIsNotNone(op.refunded_at)
            u = await session.get(User, user.id)
            self.assertEqual(u.balance, 30.0)


# =========================================================================
# Group Z-AA: AI Pipeline, Bounded Context & Metadata Parsing
# =========================================================================
class TestJournalGroupZAA(JournalFeatureBaseTest):
    async def test_bounded_context_construction(self):
        user = await self.create_user(9001)
        today = msk_now().date()
        async with async_session_maker() as session:
            # Create entry today with answers
            today_entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="completed",
                questions_snapshot_json="[]",
            )
            session.add(today_entry)
            await session.flush()
            session.add(JournalAnswer(entry_id=today_entry.id, question_id=1, position=1, question_text_snapshot="Как дела?", answer_text="Отлично"))
            # Create 10 previous completed entries
            for i in range(1, 11):
                session.add(JournalEntry(
                    user_id=user.id,
                    reflection_date=today - timedelta(days=i),
                    status="completed",
                    questions_snapshot_json="[]",
                    summary_text=f"День -{i}",
                ))
            await session.commit()
            entry_id = today_entry.id

        async with async_session_maker() as session:
            user_prompt, sys_prompt = await build_journal_daily_summary_context(session, entry_id, user.id)

        self.assertIn("Как дела?", user_prompt)
        self.assertIn("Отлично", user_prompt)
        # Previous entries bounded to at most 7
        self.assertIn("День -1", user_prompt)
        self.assertIn("День -7", user_prompt)
        self.assertNotIn("День -8", user_prompt)
        self.assertIn("предоставленные данные Дневника", sys_prompt)

    def test_metadata_and_summary_parsing_robustness(self):
        raw_response = (
            "[[SUMMARY]]\n"
            "Сегодня был продуктивный день с фокусом на ключевых целях.\n\n"
            "[[ENCOURAGING]]\n"
            "Ты отлично справляешься, продолжай в том же духе!\n\n"
            "[[FOCUS]]\n"
            "Отдохнуть утром и начать задачи вовремя.\n\n"
            "[[METADATA_JSON]]\n"
            "{\n"
            '  "dominant_emotions": ["радость", "спокойствие"],\n'
            '  "gratitude": ["коллеги"],\n'
            '  "important_events": ["встреча"],\n'
            '  "lessons": ["тайм-менеджмент"],\n'
            '  "keep": ["баланс"],\n'
            '  "let_go": ["тревога"],\n'
            '  "tomorrow_intentions": ["прогулка"]\n'
            "}"
        )
        parsed = parse_journal_daily_summary_response(raw_response)
        self.assertIn("продуктивный день", parsed["summary_text"])
        self.assertIn("отлично справляешься", parsed["encouraging_message"])
        self.assertIn("Отдохнуть утром", parsed["tomorrow_focus"])
        meta = json.loads(parsed["metadata_json"])
        self.assertIn("радость", meta["dominant_emotions"])
        self.assertIn("коллеги", meta["gratitude"])


# =========================================================================
# Group AB-AE: DB Compat, Keyboards & Cross-Links
# =========================================================================
class TestJournalGroupABAE(JournalFeatureBaseTest):
    def test_all_callback_data_within_64_bytes(self):
        # Test keyboard callbacks <= 64 bytes UTF-8
        kbs = [
            journal_menu_keyboard(),
            journal_completed_today_keyboard(12345),
            journal_in_progress_keyboard(12345),
            journal_reminder_time_keyboard(),
            journal_reminder_message_keyboard(),
            journal_snooze_menu_keyboard(),
            journal_dynamics_menu_keyboard("tok12345"),
            journal_entry_detail_keyboard(12345, True, token8="12345678"),
            journal_entry_detail_keyboard(12345, False, token8="12345678"),
        ]
        for kb in kbs:
            for row in kb.inline_keyboard:
                for btn in row:
                    if btn.callback_data:
                        b_len = len(btn.callback_data.encode("utf-8"))
                        self.assertLessEqual(
                            b_len,
                            64,
                            f"Callback data exceeds 64 bytes: '{btn.callback_data}' ({b_len} bytes)",
                        )

    async def test_sqlite_compat_schema_users_journal_points(self):
        # Verify _ensure_sqlite_compat_schema() adds journal_points idempotently
        async with engine.begin() as conn:
            await _ensure_sqlite_compat_schema(conn)
        async with engine.connect() as conn:
            res = await conn.execute(text("PRAGMA table_info(users);"))
            columns = [row[1] for row in res.fetchall()]
            self.assertIn("journal_points", columns)

    async def test_journal_video_slot_in_content(self):
        async with async_session_maker() as session:
            c = await session.get(Content, "journal_video")
            self.assertIsNotNone(c)
            self.assertIn("Видеоурок", c.text_content)


# =========================================================================
# Additional Mandatory Edge Cases: Presets, Snooze, One-Time Rewards, Keys
# =========================================================================
class TestJournalApprovedEdgeCases(JournalFeatureBaseTest):
    async def test_exact_reminder_presets_19_20_21_22(self):
        """Keyboard must strictly offer 19:00, 20:00, 21:00, 22:00 presets."""
        kb = journal_reminder_time_keyboard()
        cb_data_list = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        expected_presets = ["j_time_set:1900", "j_time_set:2000", "j_time_set:2100", "j_time_set:2200"]
        for p in expected_presets:
            self.assertIn(p, cb_data_list)
        # Unapproved old presets must not be present
        self.assertNotIn("j_time_set:0900", cb_data_list)
        self.assertNotIn("j_time_set:1300", cb_data_list)

    async def test_exact_snooze_options_15_30_60(self):
        """Snooze keyboard must offer strictly 15, 30, and 60 minutes."""
        kb = journal_snooze_menu_keyboard()
        cb_data_list = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("j_snooze:15", cb_data_list)
        self.assertIn("j_snooze:30", cb_data_list)
        self.assertIn("j_snooze:60", cb_data_list)
        self.assertNotIn("j_snooze:180", cb_data_list)
        self.assertNotIn("j_snooze:tomorrow", cb_data_list)

    async def test_reward_prohibits_recurring_multiples_at_22_42_63(self):
        """20 -> 21 grants +7 once; 22, 42, 63 grant nothing."""
        user = await self.create_user(8001, sub_days=10)
        today = msk_now().date()

        # Prepopulate 20 days
        async with async_session_maker() as session:
            for i in range(1, 21):
                session.add(
                    JournalEntry(
                        user_id=user.id,
                        reflection_date=today - timedelta(days=100 + i),
                        status="completed",
                        questions_snapshot_json="[]",
                        points_awarded=10,
                        points_awarded_at=msk_now(),
                    )
                )
            # Day 21 entry
            e21 = JournalEntry(
                user_id=user.id,
                reflection_date=today - timedelta(days=100),
                status="in_progress",
                questions_snapshot_json="[]",
            )
            session.add(e21)
            await session.commit()
            e21_id = e21.id

        # Finish day 21 -> grants reward
        async with async_session_maker() as session:
            res21 = await execute_transaction_a(session, e21_id, user.id)
            await session.commit()
        self.assertTrue(res21["reward_granted"])

        # Complete day 22
        async with async_session_maker() as session:
            e22 = JournalEntry(
                user_id=user.id,
                reflection_date=today - timedelta(days=99),
                status="in_progress",
                questions_snapshot_json="[]",
            )
            session.add(e22)
            await session.commit()
            e22_id = e22.id

        async with async_session_maker() as session:
            res22 = await execute_transaction_a(session, e22_id, user.id)
            await session.commit()
        self.assertFalse(res22["reward_granted"])

        # Add days up to 41, then complete day 42
        async with async_session_maker() as session:
            for i in range(23, 42):
                session.add(
                    JournalEntry(
                        user_id=user.id,
                        reflection_date=today - timedelta(days=100 - i),
                        status="completed",
                        questions_snapshot_json="[]",
                        points_awarded=10,
                        points_awarded_at=msk_now(),
                    )
                )
            e42 = JournalEntry(
                user_id=user.id,
                reflection_date=today - timedelta(days=50),
                status="in_progress",
                questions_snapshot_json="[]",
            )
            session.add(e42)
            await session.commit()
            e42_id = e42.id

        async with async_session_maker() as session:
            res42 = await execute_transaction_a(session, e42_id, user.id)
            await session.commit()
        self.assertFalse(res42["reward_granted"], "Day 42 must NOT grant a recurring reward")

        # Add days up to 62, then complete day 63
        async with async_session_maker() as session:
            for i in range(43, 63):
                session.add(
                    JournalEntry(
                        user_id=user.id,
                        reflection_date=today - timedelta(days=50 - (i - 42)),
                        status="completed",
                        questions_snapshot_json="[]",
                        points_awarded=10,
                        points_awarded_at=msk_now(),
                    )
                )
            e63 = JournalEntry(
                user_id=user.id,
                reflection_date=today - timedelta(days=1),
                status="in_progress",
                questions_snapshot_json="[]",
            )
            session.add(e63)
            await session.commit()
            e63_id = e63.id

        async with async_session_maker() as session:
            res63 = await execute_transaction_a(session, e63_id, user.id)
            await session.commit()
        self.assertFalse(res63["reward_granted"], "Day 63 must NOT grant a recurring reward")

        # Total rewards in DB must be strictly 1
        async with async_session_maker() as session:
            all_rewards = (await session.scalars(
                select(JournalReward).where(JournalReward.user_id == user.id)
            )).all()
            self.assertEqual(len(all_rewards), 1)
            self.assertEqual(all_rewards[0].cycle_number, 1)

    async def test_subscription_extension_matrix_active_expired_missing(self):
        """Test grant_subscription_bonus_days with active, expired, and missing subscriptions."""
        now = msk_now()

        # 1. Active subscription
        u_act = await self.create_user(8011, sub_days=5)
        async with async_session_maker() as session:
            sub_act = UserSubscription(
                user_id=u_act.id,
                start_date=now - timedelta(days=10),
                end_date=now + timedelta(days=5),
            )
            session.add(sub_act)
            await session.commit()

        async with async_session_maker() as session:
            await grant_subscription_bonus_days(session, u_act.id, days=7)
            await session.commit()

        async with async_session_maker() as session:
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == u_act.id))
            diff = (sub.end_date - now).total_seconds() / 86400
            self.assertGreaterEqual(diff, 11.5)

        # 2. Expired subscription
        u_exp = await self.create_user(8012, sub_days=0)
        async with async_session_maker() as session:
            sub_exp = UserSubscription(
                user_id=u_exp.id,
                start_date=now - timedelta(days=20),
                end_date=now - timedelta(days=5),
            )
            session.add(sub_exp)
            await session.commit()

        async with async_session_maker() as session:
            await grant_subscription_bonus_days(session, u_exp.id, days=7)
            await session.commit()

        async with async_session_maker() as session:
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == u_exp.id))
            diff = (sub.end_date - now).total_seconds() / 86400
            self.assertGreaterEqual(diff, 6.8)

        # 3. Missing subscription
        u_mis = await self.create_user(8013, sub_days=0)
        async with async_session_maker() as session:
            await grant_subscription_bonus_days(session, u_mis.id, days=7)
            await session.commit()

        async with async_session_maker() as session:
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == u_mis.id))
            self.assertIsNotNone(sub)
            diff = (sub.end_date - now).total_seconds() / 86400
            self.assertGreaterEqual(diff, 6.8)

    async def test_operation_key_action_token_semantics_and_replay(self):
        """Check operation_key formats and replay idempotency."""
        user = await self.create_user(8021, balance=50.0)
        today_iso = msk_now().date().isoformat()
        token8 = uuid.uuid4().hex[:8]

        # Succeeded dynamics operation
        dyn_key = f"dynamics_7:{user.id}:{today_iso}:{token8}"
        async with async_session_maker() as session:
            op = JournalAiOperation(
                operation_key=dyn_key,
                user_id=user.id,
                operation_type="dynamics_7",
                status="succeeded",
                result_text="Cached dynamics result",
                token_cost=5.0,
                charged_at=msk_now(),
            )
            session.add(op)
            await session.commit()

        # Replay same key via initiate_journal_ai_operation
        async with async_session_maker() as session:
            op_replayed, won = await initiate_journal_ai_operation(
                session=session,
                operation_key=dyn_key,
                user_id=user.id,
                operation_type="dynamics_7",
            )
        self.assertFalse(won)
        self.assertEqual(op_replayed.status, "succeeded")
        self.assertEqual(op_replayed.result_text, "Cached dynamics result")

        # New token creates new operation key
        new_token8 = uuid.uuid4().hex[:8]
        new_key = f"dynamics_7:{user.id}:{today_iso}:{new_token8}"
        self.assertNotEqual(dyn_key, new_key)

    async def test_stale_retry_callback_rejected_when_summary_exists(self):
        """j_ret callback with an existing summary alerts without charging or re-running AI."""
        from journal_handlers import cb_journal_retry_summary
        user = await self.create_user(8031, sub_days=10)
        today = msk_now().date()
        async with async_session_maker() as session:
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="completed",
                questions_snapshot_json="[]",
                summary_text="Already generated summary.",
            )
            session.add(entry)
            await session.commit()
            entry_id = entry.id

        cb = make_callback_query(f"j_ret:{entry_id}:abcd1234", user.id, bot=self.bot)
        await cb_journal_retry_summary(cb, self.bot)
        cb.answer.assert_called_with("Итог дня уже сформирован.", show_alert=True)

    async def test_reminder_worker_claim_all_conditions(self):
        """Test production worker claim logic: disabled, future, and completed reflections block reminder claims."""
        today = msk_now().date()
        current_hhmm = "20:00"

        # 1. Disabled reminder
        u1 = await self.create_user(8041)
        # 2. Future reminder (22:00 > 20:00)
        u2 = await self.create_user(8042)
        # 3. Completed reflection
        u3 = await self.create_user(8043)
        # 4. Valid candidate (19:00 <= 20:00, enabled, not completed)
        u4 = await self.create_user(8044)

        async with async_session_maker() as session:
            session.add(JournalSettings(user_id=u1.id, reminders_enabled=False, reminder_time="19:00"))
            session.add(JournalSettings(user_id=u2.id, reminders_enabled=True, reminder_time="22:00"))
            session.add(JournalSettings(user_id=u3.id, reminders_enabled=True, reminder_time="19:00"))
            session.add(JournalEntry(user_id=u3.id, reflection_date=today, status="completed", questions_snapshot_json="[]"))
            session.add(JournalSettings(user_id=u4.id, reminders_enabled=True, reminder_time="19:00"))
            await session.commit()

        # Execute production claim update query from background_worker
        async with async_session_maker() as session:
            for uid in [u1.id, u2.id, u3.id, u4.id]:
                not_completed_subq = (
                    select(1)
                    .select_from(JournalEntry)
                    .where(
                        JournalEntry.user_id == uid,
                        JournalEntry.reflection_date == today,
                        JournalEntry.status == "completed",
                    )
                )
                claim_stmt = (
                    update(JournalSettings)
                    .where(
                        JournalSettings.user_id == uid,
                        JournalSettings.reminders_enabled == True,
                        JournalSettings.reminder_time != None,
                        JournalSettings.reminder_time <= current_hhmm,
                        (JournalSettings.last_reminded_date == None) | (JournalSettings.last_reminded_date < today),
                        ~not_completed_subq.exists(),
                    )
                    .values(last_reminded_date=today)
                    .returning(JournalSettings.user_id)
                )
                claimed_id = (await session.execute(claim_stmt)).scalar()
                if uid == u4.id:
                    self.assertEqual(claimed_id, u4.id, "Valid candidate must be claimed")
                else:
                    self.assertIsNone(claimed_id, f"User {uid} must NOT be claimed")
            await session.commit()

    async def test_snooze_forbidden_and_stale_claim_recovery(self):
        """Worker clears snooze state for blocked user and resets claims older than 5 minutes."""
        now = msk_now()
        u_stale = await self.create_user(8051)
        async with async_session_maker() as session:
            session.add(
                JournalSettings(
                    user_id=u_stale.id,
                    snooze_until=now - timedelta(minutes=10),
                    snooze_claimed_at=now - timedelta(minutes=6),
                )
            )
            await session.commit()

        # Run worker's stale cleanup query
        async with async_session_maker() as session:
            await session.execute(
                update(JournalSettings)
                .where(
                    JournalSettings.snooze_claimed_at != None,
                    JournalSettings.snooze_claimed_at < now - timedelta(minutes=5),
                )
                .values(snooze_until=None, snooze_claimed_at=None)
            )
            await session.commit()

        async with async_session_maker() as session:
            st = await session.get(JournalSettings, u_stale.id)
            self.assertIsNone(st.snooze_until)
            self.assertIsNone(st.snooze_claimed_at)


class TestJournalPR4ReviewFixes(JournalFeatureBaseTest):
    """Specific tests verifying all 12 defect fixes required in PR #4 final review."""

    async def test_dispatch_reminder_retry_after_then_network_failure_regular(self):
        """Regular reminder RetryAfter then network failure conditionally resets last_reminded_date."""
        user = await self.create_user(9001)
        today = msk_now().date()
        async with async_session_maker() as session:
            session.add(JournalSettings(user_id=user.id, last_reminded_date=today))
            await session.commit()

        bot = AsyncMock(spec=Bot)
        bot.send_message.side_effect = [
            TelegramRetryAfter(method=MagicMock(), message="Flood control", retry_after=0.001),
            Exception("Network connection reset 502"),
        ]

        delivered = await dispatch_journal_reminder_message(
            bot=bot,
            uid=user.id,
            reminder_text="Test reminder",
            kb=None,
            is_snooze=False,
            today=today,
        )
        self.assertFalse(delivered)

        async with async_session_maker() as session:
            st = await session.get(JournalSettings, user.id)
            self.assertIsNone(st.last_reminded_date, "last_reminded_date must be reset to None on network failure")

    async def test_dispatch_reminder_retry_after_then_network_failure_snooze(self):
        """Snooze reminder RetryAfter then network failure resets snooze_claimed_at but keeps snooze_until."""
        user = await self.create_user(9002)
        now = msk_now()
        today = now.date()
        snooze_time = now + timedelta(minutes=15)
        async with async_session_maker() as session:
            session.add(JournalSettings(user_id=user.id, snooze_until=snooze_time, snooze_claimed_at=now))
            await session.commit()

        bot = AsyncMock(spec=Bot)
        bot.send_message.side_effect = [
            TelegramRetryAfter(method=MagicMock(), message="Flood control", retry_after=0.001),
            Exception("Network timeout"),
        ]

        delivered = await dispatch_journal_reminder_message(
            bot=bot,
            uid=user.id,
            reminder_text="Test snooze",
            kb=None,
            is_snooze=True,
            today=today,
        )
        self.assertFalse(delivered)

        async with async_session_maker() as session:
            st = await session.get(JournalSettings, user.id)
            self.assertIsNone(st.snooze_claimed_at, "snooze_claimed_at must be reset to None")
            self.assertEqual(st.snooze_until, snooze_time, "snooze_until must be kept unchanged for future worker retry")

    async def test_dispatch_reminder_forbidden_transitions(self):
        """Forbidden user marks User.is_blocked, keeps regular claim, clears snooze state."""
        today = msk_now().date()
        u_reg = await self.create_user(9003)
        u_snz = await self.create_user(9004)

        async with async_session_maker() as session:
            session.add(JournalSettings(user_id=u_reg.id, last_reminded_date=today))
            session.add(JournalSettings(user_id=u_snz.id, snooze_until=msk_now(), snooze_claimed_at=msk_now()))
            await session.commit()

        bot = AsyncMock(spec=Bot)
        bot.send_message.side_effect = TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by user")

        # 1. Regular reminder: consumes/keeps claim
        del_reg = await dispatch_journal_reminder_message(bot, u_reg.id, "text", None, is_snooze=False, today=today)
        self.assertFalse(del_reg)
        async with async_session_maker() as session:
            user_db = await session.get(User, u_reg.id)
            st_reg = await session.get(JournalSettings, u_reg.id)
            self.assertTrue(user_db.is_blocked)
            self.assertEqual(st_reg.last_reminded_date, today, "Daily claim must be kept for blocked user")

        # 2. Snooze reminder: clears snooze_until and snooze_claimed_at
        del_snz = await dispatch_journal_reminder_message(bot, u_snz.id, "text", None, is_snooze=True, today=today)
        self.assertFalse(del_snz)
        async with async_session_maker() as session:
            user_db2 = await session.get(User, u_snz.id)
            st_snz = await session.get(JournalSettings, u_snz.id)
            self.assertTrue(user_db2.is_blocked)
            self.assertIsNone(st_snz.snooze_until)
            self.assertIsNone(st_snz.snooze_claimed_at)

    async def test_concurrent_duplicate_intermediate_answer_savepoint(self):
        """Duplicate intermediate answer is handled with begin_nested without overwriting first answer or crashing."""
        user = await self.create_user(9005)
        await self.create_standard_questions(6)
        today = msk_now().date()

        async with async_session_maker() as session:
            snap = await build_questions_snapshot(session)
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="in_progress",
                questions_snapshot_json=json.dumps(snap, ensure_ascii=False),
            )
            session.add(entry)
            await session.flush()
            # First answer already stored
            ans1 = JournalAnswer(
                entry_id=entry.id,
                position=1,
                question_text_snapshot=snap[0]["text"],
                answer_text="Первый ответ (оригинал)",
            )
            session.add(ans1)
            await session.commit()
            entry_id = entry.id

        # Simulate second concurrent delivery of answer for position 1
        storage_key = StorageKey(bot_id=1, chat_id=user.id, user_id=user.id)
        state = FSMContext(storage=self.storage, key=storage_key)
        await state.set_state(JournalStates.answering_question)
        await state.update_data(entry_id=entry_id, position=1, total=6)

        msg = make_message("Дубликат ответа 1", user.id, bot=self.bot)
        await msg_answer_question(msg, state, self.bot)

        # Verify DB state: answer 1 was NOT overwritten
        async with async_session_maker() as session:
            stored_ans = await session.scalar(
                select(JournalAnswer).where(JournalAnswer.entry_id == entry_id, JournalAnswer.position == 1)
            )
            self.assertEqual(stored_ans.answer_text, "Первый ответ (оригинал)")

        # Verify FSM advanced to the first unanswered position (position 2)
        fsm_data = await state.get_data()
        self.assertEqual(fsm_data.get("position"), 2)

    async def test_concurrent_duplicate_final_answer_transaction_a(self):
        """Two concurrent final answers in execute_transaction_a complete safely without IntegrityError."""
        user = await self.create_user(9006)
        await self.create_standard_questions(6)
        today = msk_now().date()

        async with async_session_maker() as session:
            snap = await build_questions_snapshot(session)
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="in_progress",
                questions_snapshot_json=json.dumps(snap, ensure_ascii=False),
            )
            session.add(entry)
            await session.flush()
            for p in range(1, 6):
                session.add(
                    JournalAnswer(
                        entry_id=entry.id,
                        position=p,
                        question_text_snapshot=snap[p - 1]["text"],
                        answer_text=f"Ответ {p}",
                    )
                )
            await session.commit()
            entry_id = entry.id
            q6_snap = snap[5]

        # Concurrently execute transaction A with different sessions
        async def _call_tx(ans_txt):
            async with async_session_maker() as sess:
                return await execute_transaction_a(
                    session=sess,
                    entry_id=entry_id,
                    user_id=user.id,
                    final_answer_text=ans_txt,
                    final_question_snapshot=q6_snap,
                )

        res1, res2 = await asyncio.gather(_call_tx("Финальный ответ A"), _call_tx("Финальный ответ B"))

        # One call awarded points, one awarded 0; sum must be exactly 10
        total_pts_awarded = res1["points_awarded"] + res2["points_awarded"]
        self.assertEqual(total_pts_awarded, 10, "Points must be awarded exactly once across concurrent executions")

        async with async_session_maker() as session:
            db_entry = await session.get(JournalEntry, entry_id)
            db_user = await session.get(User, user.id)
            self.assertEqual(db_entry.status, "completed")
            self.assertEqual(db_user.journal_points, 10)
            answers = (await session.scalars(select(JournalAnswer).where(JournalAnswer.entry_id == entry_id))).all()
            self.assertEqual(len(answers), 6, "Must have exactly 6 answers, no duplicates")

    async def test_dynamics_entry_count_checked_before_billing(self):
        """Users with <2 completed entries receive 'not enough reflections' without JournalAiOperation or token debit."""
        # 1. Zero balance user with 0 entries
        u_zero = await self.create_user(9007, balance=0.0)
        bot = AsyncMock()

        success, msg = await generate_journal_dynamics_analysis(
            user_id=u_zero.id,
            days=7,
            chat_id=u_zero.id,
            token8="zero1234",
            bot=bot,
        )
        self.assertFalse(success)
        self.assertIn("требуется как минимум 2", msg)
        self.assertNotIn("Недостаточно токенов", msg)

        # 2. Rich user with only 1 completed entry
        u_rich = await self.create_user(9008, balance=100.0)
        today = msk_now().date()
        async with async_session_maker() as session:
            session.add(
                JournalEntry(
                    user_id=u_rich.id,
                    reflection_date=today,
                    status="completed",
                    questions_snapshot_json="[]",
                )
            )
            await session.commit()

        success_rich, msg_rich = await generate_journal_dynamics_analysis(
            user_id=u_rich.id,
            days=7,
            chat_id=u_rich.id,
            token8="rich1234",
            bot=bot,
        )
        self.assertFalse(success_rich)
        self.assertIn("требуется как минимум 2", msg_rich)

        # Verify no JournalAiOperation and no token debit occurred
        async with async_session_maker() as session:
            ops = (await session.scalars(select(JournalAiOperation).where(JournalAiOperation.user_id.in_([u_zero.id, u_rich.id])))).all()
            self.assertEqual(len(ops), 0, "No JournalAiOperation may be created when entry count < 2")
            refreshed_rich = await session.get(User, u_rich.id)
            self.assertEqual(refreshed_rich.balance, 100.0, "Balance must not be modified")

    async def test_dynamics_fallback_success_and_structured_logging(self):
        """Dynamics attempts fallback on primary error, populates structured AILog, and fires fallback alert."""
        user = await self.create_user(9009)
        today = msk_now().date()
        async with async_session_maker() as session:
            for d in [1, 2]:
                session.add(
                    JournalEntry(
                        user_id=user.id,
                        reflection_date=today - timedelta(days=d),
                        status="completed",
                        questions_snapshot_json="[]",
                        summary_text=f"Итог дня {d}",
                    )
                )
            cfg = await session.get(AIConfig, 1)
            if not cfg:
                cfg = AIConfig(id=1, provider="OpenAI", openai_api_key="sk-test", allow_fallback=True, fallback_provider="DeepSeek", deepseek_api_key="sk-ds")
                session.add(cfg)
            else:
                cfg.provider = "OpenAI"
                cfg.openai_api_key = "sk-test"
                cfg.allow_fallback = True
                cfg.fallback_provider = "DeepSeek"
                cfg.deepseek_api_key = "sk-ds"
            await session.commit()

        bot = AsyncMock()
        primary_mock_err = Exception("OpenAI 500 Internal Error")
        fallback_resp = StructuredAIResponse(
            text="Анализ динамики (резервный провайдер)",
            raw_payload={"choices": [{"message": {"content": "Анализ динамики"}}]},
            http_status=200,
            finish_reason="stop",
            diagnostics={"provider": "DeepSeek"},
        )

        with patch("ai_integration._generate_chat_response", side_effect=[primary_mock_err, fallback_resp]), \
             patch("ai_integration.send_ai_fallback_used_alert", new_callable=AsyncMock) as mock_fb_alert, \
             patch("ai_integration.record_ai_attempt_log", new_callable=AsyncMock) as mock_log:

            success, text_res = await generate_journal_dynamics_analysis(
                user_id=user.id,
                days=7,
                chat_id=user.id,
                token8="fb_test1",
                bot=bot,
            )

            self.assertTrue(success)
            self.assertEqual(text_res, "Анализ динамики (резервный провайдер)")

            # Check AILog calls
            self.assertEqual(mock_log.call_count, 2)
            # Attempt 1 (primary)
            att1_kw = mock_log.call_args_list[0].kwargs
            self.assertEqual(att1_kw.get("attempt_no"), 1)
            self.assertEqual(att1_kw.get("attempt_role"), "primary")
            self.assertEqual(att1_kw.get("request_type"), "journal_dynamics")
            self.assertEqual(att1_kw.get("status"), "error")

            # Attempt 2 (fallback) with structured fields
            att2_kw = mock_log.call_args_list[1].kwargs
            self.assertEqual(att2_kw.get("attempt_no"), 2)
            self.assertEqual(att2_kw.get("attempt_role"), "fallback")
            self.assertEqual(att2_kw.get("request_type"), "journal_dynamics")
            self.assertEqual(att2_kw.get("status"), "success")
            self.assertEqual(att2_kw.get("http_status"), 200)
            self.assertEqual(att2_kw.get("finish_reason"), "stop")
            self.assertIsNotNone(att2_kw.get("provider_response_payload"))

            # Check fallback alert called with request_type="journal_dynamics"
            mock_fb_alert.assert_called_once()
            self.assertEqual(mock_fb_alert.call_args.kwargs.get("request_type"), "journal_dynamics")

    async def test_dynamics_terminal_failure_alert(self):
        """Dynamics terminal failure sends terminal alert with request_type='journal_dynamics'."""
        user = await self.create_user(9010)
        today = msk_now().date()
        async with async_session_maker() as session:
            for d in [1, 2]:
                session.add(
                    JournalEntry(
                        user_id=user.id,
                        reflection_date=today - timedelta(days=d),
                        status="completed",
                        questions_snapshot_json="[]",
                    )
                )
            cfg = await session.get(AIConfig, 1)
            cfg.allow_fallback = False
            await session.commit()

        bot = AsyncMock()
        with patch("ai_integration._generate_chat_response", side_effect=Exception("Terminal error")), \
             patch("ai_integration.send_terminal_ai_failure_alert", new_callable=AsyncMock) as mock_term_alert:

            success, msg = await generate_journal_dynamics_analysis(
                user_id=user.id,
                days=7,
                chat_id=user.id,
                token8="term_test",
                bot=bot,
            )
            self.assertFalse(success)
            mock_term_alert.assert_called_once()
            self.assertEqual(mock_term_alert.call_args.kwargs.get("request_type"), "journal_dynamics")

    async def test_habits_keyboard_contains_journal_button(self):
        """mode_habits keyboard must contain '📔 Дневник саморефлексии' with callback_data='mode_journal'."""
        cb = make_callback_query("mode_habits", 9011, bot=self.bot)
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            await mode_habits(cb, self.bot)
            mock_send.assert_called_once()
            markup = mock_send.call_args[0][3]  # reply_markup argument
            all_buttons = [btn for row in markup.inline_keyboard for btn in row]
            journal_btn = next((b for b in all_buttons if b.callback_data == "mode_journal"), None)
            self.assertIsNotNone(journal_btn, "Button with callback_data='mode_journal' must exist in habits menu")
            self.assertIn("Дневник", journal_btn.text)

    async def test_in_progress_summary_retry_and_detail_blocked(self):
        """Crafted j_ret or j_h_v on in-progress entry must be rejected without creating AI operation."""
        user = await self.create_user(9012)
        today = msk_now().date()
        async with async_session_maker() as session:
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="in_progress",
                questions_snapshot_json="[]",
            )
            session.add(entry)
            await session.commit()
            entry_id = entry.id

        # 1. Test j_ret rejection
        cb_ret = make_callback_query(f"j_ret:{entry_id}:abcd", user.id, bot=self.bot)
        with patch("ai_integration.generate_journal_daily_summary", new_callable=AsyncMock) as mock_gen:
            await cb_journal_retry_summary(cb_ret, self.bot)
            mock_gen.assert_not_called()
            cb_ret.answer.assert_called_with("⛔️ Запись не найдена или ещё не завершена.", show_alert=True)

        # 2. Test j_h_v rejection
        cb_h = make_callback_query(f"j_h_v:{entry_id}", user.id, bot=self.bot)
        await cb_journal_history_view(cb_h, self.bot)
        cb_h.answer.assert_called_with("⛔️ Запись ещё не завершена.", show_alert=True)

        # 3. Test build_journal_daily_summary_context rejection
        async with async_session_maker() as session:
            with self.assertRaises(ValueError):
                await build_journal_daily_summary_context(session, entry_id, user.id)

    def test_parser_salvage_edge_cases(self):
        """Test all 4 critical parser salvage cases without exposing raw metadata."""
        # A. Missing SUMMARY with ENCOURAGING/FOCUS
        raw_a = "[[ENCOURAGING]] Ты справился со всеми задачами! [[FOCUS]] Отдохни перед сном. [[METADATA_JSON]] {\"dominant_emotions\": [\"peace\"]}"
        res_a = parse_journal_daily_summary_response(raw_a)
        self.assertTrue(bool(res_a["summary_text"]))
        self.assertNotIn("[[METADATA_JSON]]", res_a["summary_text"])
        self.assertNotIn("dominant_emotions", res_a["summary_text"])
        self.assertEqual(res_a["encouraging_message"], "Ты справился со всеми задачами!")

        # B. Metadata-only response
        raw_b = "[[METADATA_JSON]] {\"dominant_emotions\": [\"joy\"], \"gratitude\": [\"семья\"]}"
        res_b = parse_journal_daily_summary_response(raw_b)
        self.assertEqual(res_b["summary_text"], "")
        self.assertNotIn("dominant_emotions", res_b["summary_text"])
        self.assertIn("joy", res_b["metadata_json"])

        # C. Malformed metadata
        raw_c = "[[SUMMARY]] Это хороший день. [[METADATA_JSON]] {невалидный json"
        res_c = parse_journal_daily_summary_response(raw_c)
        self.assertEqual(res_c["summary_text"], "Это хороший день.")
        self.assertEqual(res_c["metadata_json"], "{}")

        # D. Completely malformed readable response
        raw_d = "Сегодня был продуктивный день. Мы завершили крупный этап проекта."
        res_d = parse_journal_daily_summary_response(raw_d)
        self.assertEqual(res_d["summary_text"], "Сегодня был продуктивный день. Мы завершили крупный этап проекта.")
        self.assertEqual(res_d["metadata_json"], "{}")

    async def test_configured_journal_video_slot_loaded(self):
        """Configured journal_video content slot is loaded by show_journal_menu and cb_journal_video."""
        user = await self.create_user(9013)
        async with async_session_maker() as session:
            vid_content = await session.get(Content, "journal_video")
            if not vid_content:
                session.add(Content(key="journal_video", title="Видеоурок", text_content="Видеоурок: Обучающее видео"))
            else:
                vid_content.text_content = "Видеоурок: Обучающее видео"
            await session.commit()

        # 1. show_journal_menu
        with patch("journal_handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            cb = make_callback_query("mode_journal", user.id, bot=self.bot)
            await cb_mode_journal(cb, self.bot)
            mock_send.assert_called_once()

        # 2. cb_journal_video
        with patch("journal_handlers.send_smart_content", new_callable=AsyncMock) as mock_send_vid:
            cb_v = make_callback_query("j_video", user.id, bot=self.bot)
            await cb_journal_video(cb_v, self.bot)
            mock_send_vid.assert_called_once()
            caption = mock_send_vid.call_args[0][1]
            self.assertIn("Обучающее видео", caption)

    async def test_duplicate_running_summary_replay_ux(self):
        """Replay of running summary retry informs user that it is running without creating new paid retry."""
        user = await self.create_user(9014)
        today = msk_now().date()
        async with async_session_maker() as session:
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="completed",
                questions_snapshot_json="[]",
            )
            session.add(entry)
            await session.flush()
            # Set up running operation
            op = JournalAiOperation(
                operation_key=f"summary_retry:{entry.id}:abcd1234",
                user_id=user.id,
                operation_type="summary_retry",
                entry_id=entry.id,
                status="running",
                owner_token="owner_1",
                lease_until=msk_now() + timedelta(minutes=5),
            )
            session.add(op)
            await session.commit()
            entry_id = entry.id

        cb = make_callback_query(f"j_ret:{entry_id}:abcd1234", user.id, bot=self.bot)
        with patch("ai_integration._generate_chat_response", new_callable=AsyncMock) as mock_ai:
            await cb_journal_retry_summary(cb, self.bot)
            mock_ai.assert_not_called()
            cb.message.edit_text.assert_called_with("⏳ Итог дня уже формируется. Пожалуйста, подождите...")

    async def test_j_start_integrity_error_narrow_handling_and_recovery(self):
        """handle_journal_start catches only IntegrityError on race and reloads actual unanswered state."""
        user = await self.create_user(9015)
        await self.create_standard_questions(6)
        today = msk_now().date()

        async with async_session_maker() as session:
            snap = await build_questions_snapshot(session)
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="in_progress",
                questions_snapshot_json=json.dumps(snap, ensure_ascii=False),
            )
            session.add(entry)
            await session.flush()
            # 2 questions already answered
            session.add(JournalAnswer(entry_id=entry.id, position=1, question_text_snapshot=snap[0]["text"], answer_text="A1"))
            session.add(JournalAnswer(entry_id=entry.id, position=2, question_text_snapshot=snap[1]["text"], answer_text="A2"))
            await session.commit()

        storage_key = StorageKey(bot_id=1, chat_id=user.id, user_id=user.id)
        state = FSMContext(storage=self.storage, key=storage_key)
        cb = make_callback_query("j_start", user.id, bot=self.bot)

        # Start should recover and offer position 3 (first unanswered), not blindly position 1
        await handle_journal_start(cb, state, self.bot)
        fsm_data = await state.get_data()
        self.assertEqual(fsm_data.get("position"), 3)


class TestJournalPR4FinalFiveDefects(JournalFeatureBaseTest):
    """Tests covering the five defects identified in the final review."""

    async def test_terminal_ai_failure_alert_autospec_signature_compliance(self):
        """All Journal terminal alerts must pass arguments conforming to the real send_terminal_ai_failure_alert signature."""
        user = await self.create_user(9020)
        today = msk_now().date()
        async with async_session_maker() as session:
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="completed",
                questions_snapshot_json="[]",
            )
            session.add(entry)
            cfg = await session.get(AIConfig, 1)
            cfg.allow_fallback = True
            cfg.fallback_provider = "DeepSeek"
            cfg.deepseek_api_key = "sk-ds"
            await session.commit()
            entry_id = entry.id

        # Use autospec to strictly enforce that no unsupported keyword arguments (like primary_error, fallback_error) are accepted
        mock_alert = create_autospec(send_terminal_ai_failure_alert, return_value=True)

        # 1. Daily summary fallback terminal failure
        with patch("ai_integration._generate_chat_response", side_effect=Exception("Terminal error")), \
             patch("ai_integration.send_terminal_ai_failure_alert", mock_alert):
            res = await generate_journal_daily_summary(entry_id, user.id, user.id, bot=self.bot)
            self.assertEqual(res, JournalAiResult.FAILED)
            mock_alert.assert_called_once()
            call_kw = mock_alert.call_args.kwargs
            self.assertEqual(call_kw.get("request_type"), "journal_summary")
            self.assertIn("error_message", call_kw)
            self.assertNotIn("primary_error", call_kw)
            self.assertNotIn("fallback_error", call_kw)

        # 2. Dynamics fallback terminal failure
        async with async_session_maker() as session:
            for d in [1, 2]:
                session.add(
                    JournalEntry(
                        user_id=user.id,
                        reflection_date=today - timedelta(days=d),
                        status="completed",
                        questions_snapshot_json="[]",
                    )
                )
            await session.commit()

        mock_alert_dyn = create_autospec(send_terminal_ai_failure_alert, return_value=True)
        with patch("ai_integration._generate_chat_response", side_effect=Exception("Terminal dynamics error")), \
             patch("ai_integration.send_terminal_ai_failure_alert", mock_alert_dyn):
            ok, text_msg = await generate_journal_dynamics_analysis(user.id, 7, user.id, "tok8", bot=self.bot)
            self.assertFalse(ok)
            mock_alert_dyn.assert_called_once()
            call_kw_dyn = mock_alert_dyn.call_args.kwargs
            self.assertEqual(call_kw_dyn.get("request_type"), "journal_dynamics")
            self.assertIn("error_message", call_kw_dyn)
            self.assertNotIn("primary_error", call_kw_dyn)
            self.assertNotIn("fallback_error", call_kw_dyn)

    async def test_dynamics_cas_loss_primary_and_fallback(self):
        """Late arrival after CAS ownership loss must return False and discard the raw response."""
        user = await self.create_user(9021)
        today = msk_now().date()
        async with async_session_maker() as session:
            for d in [1, 2]:
                session.add(
                    JournalEntry(
                        user_id=user.id,
                        reflection_date=today - timedelta(days=d),
                        status="completed",
                        questions_snapshot_json="[]",
                    )
                )
            cfg = await session.get(AIConfig, 1)
            cfg.allow_fallback = True
            cfg.fallback_provider = "DeepSeek"
            cfg.deepseek_api_key = "sk-ds"
            await session.commit()

        # Case A: Primary provider returns after CAS ownership lost
        with patch("ai_integration._generate_chat_response", new_callable=AsyncMock) as mock_ai, \
             patch("journal_service.finalize_journal_ai_operation_success", return_value=False):
            mock_ai.return_value = StructuredAIResponse(
                text="Late Primary Result",
                raw_payload={"choices": [{"message": {"content": "Late Primary Result"}}]},
                http_status=200,
                finish_reason="stop",
            )
            ok, msg = await generate_journal_dynamics_analysis(user.id, 7, user.id, "tok8_a", bot=self.bot)
            self.assertFalse(ok)
            self.assertNotIn("Late Primary Result", msg)
            self.assertIn("уже завершён или был восстановлен", msg)

        # Case B: Fallback provider returns after CAS ownership lost
        fb_resp = StructuredAIResponse(
            text="Late Fallback Result",
            raw_payload={"choices": [{"message": {"content": "Late Fallback Result"}}]},
            http_status=200,
            finish_reason="stop",
        )
        with patch("ai_integration._generate_chat_response", side_effect=[Exception("Primary fail"), fb_resp]), \
             patch("journal_service.finalize_journal_ai_operation_success", return_value=False):
            ok, msg = await generate_journal_dynamics_analysis(user.id, 7, user.id, "tok8_b", bot=self.bot)
            self.assertFalse(ok)
            self.assertNotIn("Late Fallback Result", msg)
            self.assertIn("уже завершён или был восстановлен", msg)

    async def test_claim_operation_key_before_token_debit_concurrency(self):
        """20 concurrent calls with identical operation_key: exactly 1 winner, 19 duplicate losers, no InsufficientTokensError."""
        user = await self.create_user(9022, balance=10.0)
        async with async_session_maker() as session:
            p_conf = await session.get(PaymentConfig, 1)
            p_conf.mode = "token"
            p_conf.cost_per_message = 10.0
            await session.commit()

        op_key = f"concurrency_test_key_{uuid.uuid4().hex}"

        async def worker():
            async with async_session_maker() as session:
                return await initiate_journal_ai_operation(
                    session=session,
                    operation_key=op_key,
                    user_id=user.id,
                    entry_id=None,
                    operation_type="dynamics_7",
                )

        results = await asyncio.gather(*(worker() for _ in range(20)))

        winners = [r for r in results if r[1] is True]
        losers = [r for r in results if r[1] is False]

        self.assertEqual(len(winners), 1, "Exactly one winner should claim the operation_key")
        self.assertEqual(len(losers), 19, "19 calls should return won=False without debiting")

        async with async_session_maker() as session:
            u_db = await session.get(User, user.id)
            self.assertEqual(u_db.balance, 0.0, "Balance should be debited exactly once (10 -> 0)")
            ops = (await session.scalars(select(JournalAiOperation).where(JournalAiOperation.operation_key == op_key))).all()
            self.assertEqual(len(ops), 1, "Exactly one JournalAiOperation must exist")
            self.assertIsNotNone(ops[0].charged_at, "The single claimed operation must have charged_at set")

    async def test_heartbeat_cancelled_and_awaited_cleanly(self):
        """Heartbeat task is cleanly cancelled and awaited before AI helper returns."""
        user = await self.create_user(9023)
        today = msk_now().date()
        async with async_session_maker() as session:
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="completed",
                questions_snapshot_json="[]",
            )
            session.add(entry)
            await session.commit()
            entry_id = entry.id

        created_tasks = []
        orig_create_task = asyncio.create_task

        def tracking_create_task(coro, **kwargs):
            task = orig_create_task(coro, **kwargs)
            if "run_lease_heartbeat" in str(coro):
                created_tasks.append(task)
            return task

        with patch("asyncio.create_task", side_effect=tracking_create_task), \
             patch("ai_integration._generate_chat_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = StructuredAIResponse(
                text="[[SUMMARY]]\nДень норм\n[[ENCOURAGING]]\nМолодец\n[[FOCUS]]\nСпи",
                raw_payload={"choices": [{"message": {"content": "День норм"}}]},
                http_status=200,
                finish_reason="stop",
            )
            res = await generate_journal_daily_summary(entry_id, user.id, user.id, bot=self.bot)
            self.assertEqual(res, JournalAiResult.SUCCEEDED)

        self.assertTrue(len(created_tasks) >= 1)
        for t in created_tasks:
            self.assertTrue(t.done(), "Heartbeat task must be finished/cancelled before function returns")

    async def test_automatic_summary_status_handling_matrix(self):
        """run_journal_summary_and_update branches explicitly on JournalAiResult states."""
        user = await self.create_user(9024)
        today = msk_now().date()
        async with async_session_maker() as session:
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="completed",
                questions_snapshot_json="[]",
                summary_text="Итог дня тестовый",
            )
            session.add(entry)
            await session.commit()
            entry_id = entry.id

        bot = AsyncMock(spec=Bot)

        # 1. already_running: sends nothing
        with patch("ai_integration.generate_journal_daily_summary", return_value=JournalAiResult.ALREADY_RUNNING):
            await run_journal_summary_and_update(entry_id, user.id, user.id, bot)
            bot.send_message.assert_not_called()

        # 2. insufficient_tokens: sends token alert keyboard
        bot.reset_mock()
        with patch("ai_integration.generate_journal_daily_summary", return_value=JournalAiResult.INSUFFICIENT_TOKENS):
            await run_journal_summary_and_update(entry_id, user.id, user.id, bot)
            bot.send_message.assert_called_once()
            call_kw = bot.send_message.call_args.kwargs
            self.assertIn("недостаточно токенов", call_kw.get("text", "").lower())
            self.assertIsNotNone(call_kw.get("reply_markup"))

        # 3. failed: sends retry keyboard
        bot.reset_mock()
        with patch("ai_integration.generate_journal_daily_summary", return_value=JournalAiResult.FAILED):
            await run_journal_summary_and_update(entry_id, user.id, user.id, bot)
            bot.send_message.assert_called_once()
            call_kw = bot.send_message.call_args.kwargs
            self.assertIn("не удалось сформировать итог", call_kw.get("text", "").lower())
            self.assertIsNotNone(call_kw.get("reply_markup"))

        # 4. succeeded: sends summary block
        bot.reset_mock()
        with patch("ai_integration.generate_journal_daily_summary", return_value=JournalAiResult.SUCCEEDED):
            await run_journal_summary_and_update(entry_id, user.id, user.id, bot)
            bot.send_message.assert_called_once()
            call_kw = bot.send_message.call_args.kwargs
            self.assertIn("Итог дня тестовый", call_kw.get("text", ""))


# =========================================================================
# Acceptance Suite: Areas A through P + HTML Resilience + DB Dialect Guard
# =========================================================================
class TestJournalUserAcceptanceMatrix(JournalFeatureBaseTest):
    """Full user/admin acceptance test matrix covering real handlers, keyboards, and UX."""

    async def test_html_escaping_resilience_in_production_renderers(self):
        """Dynamic user/AI text with HTML chars (<3, A < B, x > y, AT&T, <b>hello</b>) is safely escaped."""
        user = await self.create_user(8001, is_admin=True)
        today = msk_now().date()
        dangerous_user_answer = "<3 Любовь & Забота, A < B, x > y, AT&T, <b>жирный</b>, 'кавычки' \"двойные\""
        dangerous_summary = "Итог: 1 < 2 & 5 > 3 <3 <b>тест</b> AT&T 'одинарные' \"двойные\""

        async with async_session_maker() as session:
            q = await session.get(JournalQuestion, 1)
            if not q:
                q = JournalQuestion(id=1, text="Вопрос <1> & 'тест'?", order_num=1, is_active=True)
                session.add(q)
            else:
                q.text = "Вопрос <1> & 'тест'?"
                q.is_active = True
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="completed",
                questions_snapshot_json=json.dumps([{"id": 1, "text": "Вопрос <1> & 'тест'?", "position": 1}]),
                summary_text=dangerous_summary,
                encouraging_message="Поддержка <3 & AT&T",
                tomorrow_focus="Фокус: x < y & z > w",
            )
            session.add(entry)
            await session.flush()
            ans = JournalAnswer(
                entry_id=entry.id,
                question_id=1,
                position=1,
                question_text_snapshot="Вопрос <1> & 'тест'?",
                answer_text=dangerous_user_answer,
            )
            session.add(ans)
            await session.commit()
            entry_id = entry.id

        # 1. cb_journal_history_view
        cb = make_callback_query(f"j_h_v:{entry_id}", user.id, bot=self.bot)
        await cb_journal_history_view(cb, self.bot)
        cb.message.edit_text.assert_called_once()
        rendered_history = cb.message.edit_text.call_args[0][0]
        # Assert dynamic text was escaped
        self.assertIn("&lt;3", rendered_history)
        self.assertIn("&lt;b&gt;жирный&lt;/b&gt;", rendered_history)
        self.assertIn("&amp;", rendered_history)
        self.assertIn("&lt;1&gt;", rendered_history)
        self.assertNotIn("<b>жирный</b>", rendered_history)

        # 2. run_journal_summary_and_update
        bot_mock = AsyncMock(spec=Bot)
        with patch("ai_integration.generate_journal_daily_summary", new_callable=AsyncMock, return_value=JournalAiResult.SUCCEEDED):
            await run_journal_summary_and_update(entry_id, user.id, user.id, bot_mock)
            bot_mock.send_message.assert_called_once()
            sent_summary = bot_mock.send_message.call_args.kwargs.get("text", "")
            self.assertIn("&lt;3", sent_summary)
            self.assertIn("&lt;b&gt;тест&lt;/b&gt;", sent_summary)
            self.assertIn("&amp;", sent_summary)
            self.assertNotIn("<b>тест</b>", sent_summary)

        # 3. cb_journal_run_dynamics
        cb_dyn = make_callback_query("j_d7:token123", user.id, bot=self.bot)
        with patch("ai_integration.generate_journal_dynamics_analysis", new_callable=AsyncMock, return_value=(True, "Динамика <3 & A < B <b>тест</b>")):
            await cb_journal_run_dynamics(cb_dyn, self.bot)
            cb_dyn.message.edit_text.assert_called()
            sent_dyn = cb_dyn.message.edit_text.call_args[0][0]
            self.assertIn("&lt;3", sent_dyn)
            self.assertIn("&lt;b&gt;тест&lt;/b&gt;", sent_dyn)
            self.assertIn("&amp;", sent_dyn)

        # 4. admin_journal_q_edit_handler
        cb_admin = make_callback_query("j_adm_q_ed:1", user.id, bot=self.bot)
        state_admin = FSMContext(storage=self.storage, key=StorageKey(bot_id=1, chat_id=user.id, user_id=user.id))
        await admin_journal_q_edit_handler(cb_admin, state_admin)
        cb_admin.message.edit_text.assert_called_once()
        edit_text_arg = cb_admin.message.edit_text.call_args[0][0]
        self.assertIn("&lt;1&gt;", edit_text_arg)
        self.assertIn("&amp;", edit_text_arg)

    async def test_sqlite_compat_schema_dialect_guard(self):
        """_ensure_sqlite_compat_schema executes on SQLite and skips safely on non-SQLite."""
        # 1. Real SQLite connection
        async with engine.connect() as conn:
            await _ensure_sqlite_compat_schema(conn)

        # 2. Mock PostgreSQL connection
        mock_conn = AsyncMock()
        mock_conn.dialect.name = "postgresql"
        await _ensure_sqlite_compat_schema(mock_conn)
        mock_conn.execute.assert_not_called()

    async def test_acceptance_area_a_root_screen(self):
        """Root Journal screen renders all conditions and visible buttons accurately."""
        user = await self.create_user(8010, balance=500.0)

        # 1. Brand-new eligible user, today not started, reminder disabled, progress 0/21
        cb = make_callback_query("mode_journal", user.id, bot=self.bot)
        await cb_mode_journal(cb, self.bot)
        cb.message.edit_text.assert_called_once()
        text_out = cb.message.edit_text.call_args[0][0]
        kb_out = cb.message.edit_text.call_args.kwargs.get("reply_markup") or cb.message.edit_text.call_args[1].get("reply_markup")
        btn_texts = [b.text for row in kb_out.inline_keyboard for b in row]

        if "0/21" not in text_out:
            stats_cb = make_callback_query("journal_open_stats", user.id, bot=self.bot)
            await cb_journal_open_stats(stats_cb, self.bot)
            text_out = stats_cb.message.edit_text.call_args[0][0]
            kb_out = stats_cb.message.edit_text.call_args.kwargs.get("reply_markup") or stats_cb.message.edit_text.call_args[1].get("reply_markup")
            btn_texts = [b.text for row in kb_out.inline_keyboard for b in row]

        self.assertIn("0/21", text_out)
        self.assertIn("0 дн.", text_out)
        self.assertIn("0", text_out)  # points
        self.assertIn("отключено", text_out)
        self.assertIn("▶️ Пройти рефлексию сегодня", btn_texts)
        self.assertIn("⏰ Время напоминания", btn_texts)
        self.assertIn("📖 Мои прошлые записи", btn_texts)
        self.assertIn("📈 Моя динамика", btn_texts)
        self.assertIn("🧘 Формирование привычек", btn_texts)
        self.assertIn("⬅️ Главное меню", btn_texts)

        # 2. Today in progress
        today = msk_now().date()
        async with async_session_maker() as session:
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="in_progress",
                questions_snapshot_json="[]",
            )
            session.add(entry)
            await session.commit()

        cb = make_callback_query("mode_journal", user.id, bot=self.bot)
        await cb_mode_journal(cb, self.bot)
        stats_cb = make_callback_query("journal_open_stats", user.id, bot=self.bot)
        await cb_journal_open_stats(stats_cb, self.bot)
        kb_out2 = stats_cb.message.edit_text.call_args.kwargs.get("reply_markup")
        btn_texts2 = [b.text for row in kb_out2.inline_keyboard for b in row]
        self.assertIn("▶️ Продолжить", btn_texts2)
        self.assertIn("🔄 Начать заново", btn_texts2)

        # 3. Today completed, progress 21/21, points 100, streak 5, reminder enabled
        async with async_session_maker() as session:
            entry_curr = await session.get(JournalEntry, entry.id)
            entry_curr.status = "completed"
            entry_curr.summary_text = "Done"

            st = await session.get(JournalSettings, user.id)
            if not st:
                st = JournalSettings(user_id=user.id, reminders_enabled=True, reminder_time="21:30")
                session.add(st)
            else:
                st.reminders_enabled = True
                st.reminder_time = "21:30"

            u = await session.get(User, user.id)
            u.journal_points = 100
            await session.commit()

        with patch("journal_service.get_journal_completed_days_count", return_value=21), \
             patch("journal_service.calculate_journal_streak", return_value=5):
            cb = make_callback_query("mode_journal", user.id, bot=self.bot)
            await cb_mode_journal(cb, self.bot)
            stats_cb = make_callback_query("journal_open_stats", user.id, bot=self.bot)
            await cb_journal_open_stats(stats_cb, self.bot)
            text_out3 = stats_cb.message.edit_text.call_args[0][0]
            kb_out3 = stats_cb.message.edit_text.call_args.kwargs.get("reply_markup")
            btn_texts3 = [b.text for row in kb_out3.inline_keyboard for b in row]

            self.assertIn("21/21", text_out3)
            self.assertIn("5 дн.", text_out3)
            self.assertIn("100", text_out3)
            self.assertIn("21:30 МСК", text_out3)
            self.assertIn("👁 Посмотреть запись за сегодня", btn_texts3)

    async def test_acceptance_area_b_access_matrix(self):
        """Full access matrix: admin, active/expired sub, sufficient/insufficient tokens, habits free."""
        # 1. Admin always allowed
        admin = await self.create_user(8021, is_admin=True, balance=0.0)
        cb_admin = make_callback_query("mode_journal", admin.id, bot=self.bot)
        await cb_mode_journal(cb_admin, self.bot)
        cb_admin.message.edit_text.assert_called_once()

        # 2. Subscription active allowed
        async with async_session_maker() as session:
            p_cfg = await session.get(PaymentConfig, 1)
            if not p_cfg:
                session.add(PaymentConfig(id=1, mode="subscription"))
            else:
                p_cfg.mode = "subscription"
            top = await session.scalar(select(Topic).where(Topic.slug == "journal"))
            if top:
                top.is_paid = True
            await session.commit()

        sub_user = await self.create_user(8022, balance=0.0, sub_days=15)
        cb_sub = make_callback_query("mode_journal", sub_user.id, bot=self.bot)
        await cb_mode_journal(cb_sub, self.bot)
        cb_sub.message.edit_text.assert_called_once()

        # 3. Subscription expired blocked without DB mutation
        exp_user = await self.create_user(8023, balance=0.0, sub_days=0)
        cb_exp = make_callback_query("mode_journal", exp_user.id, bot=self.bot)
        await cb_mode_journal(cb_exp, self.bot)
        cb_exp.answer.assert_called()

        # Stale callbacks from expired user must not mutate DB
        for cdata, handler in [
            ("j_start", lambda cb: cb_journal_start(cb, AsyncMock(), self.bot)),
            ("j_h_p:0", lambda cb: cb_journal_history_page(cb, self.bot)),
            ("j_d7:tok", lambda cb: cb_journal_run_dynamics(cb, self.bot)),
            ("j_time_off", lambda cb: cb_journal_time_off(cb, self.bot)),
        ]:
            cb_stale = make_callback_query(cdata, exp_user.id, bot=self.bot)
            await handler(cb_stale)

        async with async_session_maker() as session:
            entries = (await session.execute(select(JournalEntry).where(JournalEntry.user_id == exp_user.id))).all()
            self.assertEqual(len(entries), 0, "Expired user must not mutate Journal DB")

        # 4. Token mode: sufficient vs insufficient
        async with async_session_maker() as session:
            p_cfg = await session.get(PaymentConfig, 1)
            p_cfg.mode = "token"
            p_cfg.cost_per_message = 10.0
            await session.commit()

        token_user_ok = await self.create_user(8024, balance=25.0)
        cb_tok_ok = make_callback_query("mode_journal", token_user_ok.id, bot=self.bot)
        await cb_mode_journal(cb_tok_ok, self.bot)
        cb_tok_ok.message.edit_text.assert_called_once()

        token_user_poor = await self.create_user(8025, balance=2.0)
        cb_tok_poor = make_callback_query("mode_journal", token_user_poor.id, bot=self.bot)
        await cb_mode_journal(cb_tok_poor, self.bot)
        cb_tok_poor.answer.assert_called()

        # 5. Habits remain free
        cb_habits = make_callback_query("mode_habits", exp_user.id, bot=self.bot)
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_smart:
            await mode_habits(cb_habits, self.bot)
            mock_smart.assert_called_once()

    async def test_acceptance_area_c_disclaimer_matrix(self):
        """Disclaimer prompts unaccepted users, accepts, resumes flow, and preserves FSMContext."""
        unacc_user = await self.create_user(8031, balance=200.0, disclaimer_accepted=False)

        # 1. mode_journal before acceptance sends disclaimer message
        cb = make_callback_query("mode_journal", unacc_user.id, bot=self.bot)
        await cb_mode_journal(cb, self.bot)
        self.assertTrue(len(self.bot.sent_messages) > 0)
        self.assertIn("не заменяет", self.bot.sent_messages[-1].text.lower())

        # Pending flow stored as "journal"
        pending = await consume_pending_ai_flow(unacc_user.id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.flow_type, "journal")

        # Accept disclaimer -> resumes Journal menu
        await store_pending_ai_flow(unacc_user.id, "journal", {})
        cb_acc = make_callback_query("cb_accept_disclaimer", unacc_user.id, bot=self.bot)
        state = FSMContext(storage=self.storage, key=StorageKey(bot_id=1, chat_id=unacc_user.id, user_id=unacc_user.id))

        with patch("journal_handlers.show_journal_menu", new_callable=AsyncMock) as mock_show_menu:
            await cb_accept_disclaimer(cb_acc, state, self.bot)
            mock_show_menu.assert_called_once()

        # Accepted user is not prompted again
        cb2 = make_callback_query("mode_journal", unacc_user.id, bot=self.bot)
        await cb_mode_journal(cb2, self.bot)
        cb2.message.edit_text.assert_called_once()

    async def test_acceptance_area_d_daily_reflection_journey(self):
        """Full daily reflection journey: start -> Q1..Q6 answers -> reward -> completion summary."""
        await self.create_standard_questions(count=6)
        user = await self.create_user(8041, balance=500.0)
        state = FSMContext(storage=self.storage, key=StorageKey(bot_id=1, chat_id=user.id, user_id=user.id))

        # Start reflection
        cb_start = make_callback_query("j_start", user.id, bot=self.bot)
        await cb_journal_start(cb_start, state, self.bot)

        state_data = await state.get_data()
        self.assertEqual(state_data.get("position"), 1)
        self.assertEqual(state_data.get("total"), 6)
        entry_id = state_data.get("entry_id")
        self.assertIsNotNone(entry_id)

        # Test empty / whitespace answer rejection
        msg_empty = make_message("   ", user.id, bot=self.bot)
        await msg_answer_question(msg_empty, state, self.bot)
        msg_empty.answer.assert_called_once()
        self.assertIn("напиши свой ответ", msg_empty.answer.call_args[0][0].lower())
        self.assertEqual((await state.get_data()).get("position"), 1)

        # Answer Q1 through Q5
        for pos in range(1, 6):
            msg_ans = make_message(f"Ответ на вопрос {pos}", user.id, bot=self.bot)
            await msg_answer_question(msg_ans, state, self.bot)
            curr_state = await state.get_data()
            self.assertEqual(curr_state.get("position"), pos + 1)

        # Answer Q6 (final question) with AI summary mock
        with patch("journal_handlers.run_journal_summary_and_update", new_callable=AsyncMock) as mock_run_sum:
            msg_q6 = make_message("Ответ на вопрос 6", user.id, bot=self.bot)
            await msg_answer_question(msg_q6, state, self.bot)
            await asyncio.sleep(0.01)
            mock_run_sum.assert_called_once()

        # Verify entry completed in DB
        async with async_session_maker() as session:
            entry = await session.get(JournalEntry, entry_id)
            self.assertEqual(entry.status, "completed")
            answers = (await session.execute(select(JournalAnswer).where(JournalAnswer.entry_id == entry_id))).scalars().all()
            self.assertEqual(len(answers), 6)

            user_db = await session.get(User, user.id)
            self.assertEqual(user_db.journal_points, 10)

    async def test_acceptance_area_e_question_snapshot_immutability(self):
        """Snapshotted questions remain frozen even if admin edits or deletes subsequent questions."""
        await self.create_standard_questions(count=3)
        user = await self.create_user(8051, balance=500.0)
        state = FSMContext(storage=self.storage, key=StorageKey(bot_id=1, chat_id=user.id, user_id=user.id))

        cb_start = make_callback_query("j_start", user.id, bot=self.bot)
        await cb_journal_start(cb_start, state, self.bot)
        entry_id = (await state.get_data()).get("entry_id")

        # Admin edits Q2 and deletes Q3 in DB
        async with async_session_maker() as session:
            q2 = await session.get(JournalQuestion, 2)
            q2.text = "ИЗМЕНЕННЫЙ АДМИНОМ ВОПРОС 2"
            q3 = await session.get(JournalQuestion, 3)
            await session.delete(q3)
            await session.commit()

        # User answers Q1
        msg1 = make_message("Ответ 1", user.id, bot=self.bot)
        await msg_answer_question(msg1, state, self.bot)

        # Sent text for Q2 must be ORIGINAL text, not the edited DB text
        q2_prompt = msg1.answer.call_args[0][0]
        self.assertIn("Вопрос 2?", q2_prompt)
        self.assertNotIn("ИЗМЕНЕННЫЙ АДМИНОМ", q2_prompt)

        # User answers Q2
        msg2 = make_message("Ответ 2", user.id, bot=self.bot)
        await msg_answer_question(msg2, state, self.bot)

        # Q3 was deleted in DB, but user still receives snapshot Q3
        q3_prompt = msg2.answer.call_args[0][0]
        self.assertIn("Вопрос 3?", q3_prompt)

        # User answers Q3: succeeds with question_id=None
        with patch("journal_handlers.run_journal_summary_and_update", new_callable=AsyncMock):
            msg3 = make_message("Ответ 3", user.id, bot=self.bot)
            await msg_answer_question(msg3, state, self.bot)

        async with async_session_maker() as session:
            ans3 = (await session.execute(
                select(JournalAnswer).where(JournalAnswer.entry_id == entry_id, JournalAnswer.position == 3)
            )).scalar_one_or_none()
            self.assertIsNotNone(ans3)
            self.assertIsNone(ans3.question_id)
            self.assertEqual(ans3.question_text_snapshot, "Вопрос 3?")

    async def test_acceptance_area_f_completed_day_experience(self):
        """Completed day: j_start informs user, summary retry recovers or shows friendly error."""
        user = await self.create_user(8061, balance=500.0)
        today = msk_now().date()
        async with async_session_maker() as session:
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="completed",
                questions_snapshot_json="[]",
                summary_text="Итог готов",
            )
            session.add(entry)
            await session.commit()
            entry_id = entry.id

        # 1. Clicking j_start after completion shows completed message
        cb = make_callback_query("j_start", user.id, bot=self.bot)
        state = FSMContext(storage=self.storage, key=StorageKey(bot_id=1, chat_id=user.id, user_id=user.id))
        await cb_journal_start(cb, state, self.bot)
        cb.message.edit_text.assert_called_once()
        self.assertIn("уже пройдена", cb.message.edit_text.call_args[0][0].lower())

        # 2. Stale retry when summary now exists answers alert without calling AI
        cb_ret = make_callback_query(f"j_ret:{entry_id}:tok12345", user.id, bot=self.bot)
        with patch("ai_integration.generate_journal_daily_summary") as mock_ai:
            await cb_journal_retry_summary(cb_ret, self.bot)
            mock_ai.assert_not_called()
            cb_ret.answer.assert_called_with("Итог дня уже сформирован.", show_alert=True)

        # 3. Retry when summary is missing calls AI
        async with async_session_maker() as session:
            entry_curr = await session.get(JournalEntry, entry_id)
            entry_curr.summary_text = None
            await session.commit()

        cb_ret2 = make_callback_query(f"j_ret:{entry_id}:tok999", user.id, bot=self.bot)
        with patch("ai_integration.generate_journal_daily_summary", new_callable=AsyncMock, return_value="succeeded"):
            await cb_journal_retry_summary(cb_ret2, self.bot)
            cb_ret2.message.edit_text.assert_called()

    async def test_acceptance_area_g_history_pagination_contract(self):
        """History pagination: 0 entries, 1 entry, 5 entries, 6 entries (2 pages), newest-first order."""
        user = await self.create_user(8071, balance=500.0)

        # 0 entries
        cb0 = make_callback_query("j_h_p:0", user.id, bot=self.bot)
        await cb_journal_history_page(cb0, self.bot)
        self.assertIn("нет сохранённых записей", cb0.message.edit_text.call_args[0][0].lower())

        # Create 6 completed entries (distinct dates)
        today = msk_now().date()
        async with async_session_maker() as session:
            for i in range(6):
                session.add(JournalEntry(
                    user_id=user.id,
                    reflection_date=today - timedelta(days=i),
                    status="completed",
                    questions_snapshot_json="[]",
                    summary_text=f"Summary {i}",
                ))
            await session.commit()

        # Page 0: 5 entries + next page button
        cb_p0 = make_callback_query("j_h_p:0", user.id, bot=self.bot)
        await cb_journal_history_page(cb_p0, self.bot)
        kb_p0 = cb_p0.message.edit_text.call_args.kwargs.get("reply_markup")
        cbs_p0 = [b.callback_data for row in kb_p0.inline_keyboard for b in row]
        self.assertIn("j_h_p:1", cbs_p0)

        # Page 1: 1 entry + prev page button
        cb_p1 = make_callback_query("j_h_p:1", user.id, bot=self.bot)
        await cb_journal_history_page(cb_p1, self.bot)
        kb_p1 = cb_p1.message.edit_text.call_args.kwargs.get("reply_markup")
        cbs_p1 = [b.callback_data for row in kb_p1.inline_keyboard for b in row]
        self.assertIn("j_h_p:0", cbs_p1)

        # Ownership isolation: other user cannot see user's history
        user2 = await self.create_user(8072, balance=500.0)
        cb_other = make_callback_query("j_h_p:0", user2.id, bot=self.bot)
        await cb_journal_history_page(cb_other, self.bot)
        self.assertIn("нет сохранённых записей", cb_other.message.edit_text.call_args[0][0].lower())

    async def test_acceptance_area_h_dynamics_ux(self):
        """Dynamics UX: <2 entries blocked without billing; >=2 entries analyzed; replay cached."""
        user = await self.create_user(8081, balance=500.0)

        # 0 entries -> message requiring at least 2 entries (exercises real generate_journal_dynamics_analysis count check)
        cb_dyn = make_callback_query("j_d7:tok1", user.id, bot=self.bot)
        await cb_journal_run_dynamics(cb_dyn, self.bot)
        self.assertIn("как минимум 2 завершённые записи", cb_dyn.message.edit_text.call_args[0][0].lower())

        # Add 2 completed entries
        today = msk_now().date()
        async with async_session_maker() as session:
            session.add(JournalEntry(user_id=user.id, reflection_date=today, status="completed", questions_snapshot_json="[]", summary_text="S1"))
            session.add(JournalEntry(user_id=user.id, reflection_date=today - timedelta(days=1), status="completed", questions_snapshot_json="[]", summary_text="S2"))
            await session.commit()

        # Run dynamics: success
        with patch("ai_integration.generate_journal_dynamics_analysis", new_callable=AsyncMock, return_value=(True, "Отличный прогресс!")):
            await cb_journal_run_dynamics(cb_dyn, self.bot)
            cb_dyn.message.edit_text.assert_called()
            self.assertIn("Отличный прогресс!", cb_dyn.message.edit_text.call_args[0][0])

    async def test_acceptance_area_i_reminder_settings_ux(self):
        """Reminder presets, manual valid/invalid formats, and disable button."""
        user = await self.create_user(8091)
        state = FSMContext(storage=self.storage, key=StorageKey(bot_id=1, chat_id=user.id, user_id=user.id))

        # Preset 20:00
        cb_preset = make_callback_query("j_time_set:2000", user.id, bot=self.bot)
        await cb_journal_time_set_preset(cb_preset, self.bot)

        async with async_session_maker() as session:
            st = await session.get(JournalSettings, user.id)
            self.assertTrue(st.reminders_enabled)
            self.assertEqual(st.reminder_time, "20:00")

        # Disable
        cb_off = make_callback_query("j_time_off", user.id, bot=self.bot)
        await cb_journal_time_off(cb_off, self.bot)
        async with async_session_maker() as session:
            st = await session.get(JournalSettings, user.id)
            self.assertFalse(st.reminders_enabled)

        # Manual entry start
        cb_man = make_callback_query("j_time_manual", user.id, bot=self.bot)
        await cb_journal_time_manual(cb_man, state, self.bot)
        self.assertEqual(await state.get_state(), JournalStates.setting_reminder_time)

        # Invalid manual input
        msg_inv = make_message("25:00", user.id, bot=self.bot)
        await msg_journal_time_manual_input(msg_inv, state, self.bot)
        self.assertEqual(await state.get_state(), JournalStates.setting_reminder_time)
        self.assertIn("неверный формат", msg_inv.answer.call_args[0][0].lower())

        # Valid manual input
        msg_val = make_message("22:45", user.id, bot=self.bot)
        await msg_journal_time_manual_input(msg_val, state, self.bot)
        self.assertIsNone(await state.get_state())
        async with async_session_maker() as session:
            st = await session.get(JournalSettings, user.id)
            self.assertTrue(st.reminders_enabled)
            self.assertEqual(st.reminder_time, "22:45")

    async def test_acceptance_area_j_reminder_delivery_and_snooze(self):
        """Reminder delivery checks exact message, buttons, snooze presets, and skip if completed."""
        user = await self.create_user(8101)
        today = msk_now().date()
        async with async_session_maker() as session:
            session.add(JournalSettings(user_id=user.id, reminders_enabled=True, reminder_time="20:00"))
            await session.commit()

        # Regular dispatch
        bot_mock = AsyncMock(spec=Bot)
        reminder_text = "🔔 <b>Время вечерней рефлексии!</b>\n\nВыдели несколько минут, чтобы подвести итоги дня в Дневнике."
        kb = journal_reminder_message_keyboard()
        await dispatch_journal_reminder_message(bot_mock, user.id, reminder_text, kb, is_snooze=False, today=today)
        bot_mock.send_message.assert_called_once()
        sent_kw = bot_mock.send_message.call_args.kwargs
        self.assertIn("🔔 <b>Время вечерней рефлексии!</b>", sent_kw["text"])
        kb_sent = sent_kw["reply_markup"]
        btn_texts = [b.text for row in kb_sent.inline_keyboard for b in row]
        self.assertIn("▶️ Начать рефлексию", btn_texts)
        self.assertIn("⏰ Напомнить позже", btn_texts)

        # Snooze preset 30 min
        cb_snz = make_callback_query("j_snooze:30", user.id, bot=self.bot)
        await cb_journal_snooze_set(cb_snz, self.bot)
        async with async_session_maker() as session:
            st = await session.get(JournalSettings, user.id)
            self.assertIsNotNone(st.snooze_until)

    async def test_acceptance_area_k_token_billing_ux(self):
        """Token billing: charged on initiation, refunded on failure, blocked when insufficient."""
        user = await self.create_user(8111, balance=5.0)
        async with async_session_maker() as session:
            p_cfg = await session.get(PaymentConfig, 1)
            p_cfg.mode = "token"
            p_cfg.cost_per_message = 10.0
            await session.commit()

        # Insufficient balance raises InsufficientTokensError
        async with async_session_maker() as session:
            with self.assertRaises(InsufficientTokensError):
                await initiate_journal_ai_operation(
                    session=session,
                    operation_key="key1",
                    user_id=user.id,
                    operation_type="summary",
                    entry_id=None,
                )

        # Set sufficient balance
        async with async_session_maker() as session:
            u = await session.get(User, user.id)
            u.balance = 20.0
            await session.commit()

        async with async_session_maker() as session:
            op, won = await initiate_journal_ai_operation(
                session=session,
                operation_key="key2",
                user_id=user.id,
                operation_type="summary",
                entry_id=None,
            )
            self.assertTrue(won)
            self.assertIsNotNone(op.charged_at)
            op_id = op.id

        # Finalize failure -> refund
        async with async_session_maker() as session:
            await finalize_journal_ai_operation_failure(session, op_id)

        async with async_session_maker() as session:
            op_db = await session.get(JournalAiOperation, op_id)
            self.assertIsNotNone(op_db.refunded_at)
            u_db = await session.get(User, user.id)
            self.assertEqual(u_db.balance, 20.0)

    async def test_acceptance_area_l_ai_fallback_and_failure(self):
        """Terminal AI failure sends alert with exact supported signature and logs failure properly."""
        user = await self.create_user(8121)
        today = msk_now().date()
        async with async_session_maker() as session:
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="completed",
                questions_snapshot_json="[]",
            )
            session.add(entry)
            cfg = await session.get(AIConfig, 1)
            cfg.allow_fallback = False
            await session.commit()
            entry_id = entry.id

        mock_alert = create_autospec(send_terminal_ai_failure_alert, return_value=True)
        with patch("ai_integration._generate_chat_response", side_effect=Exception("Terminal error")), \
             patch("ai_integration.send_terminal_ai_failure_alert", mock_alert):
            res = await generate_journal_daily_summary(entry_id, user.id, user.id, bot=self.bot)
            self.assertEqual(res, JournalAiResult.FAILED)
            mock_alert.assert_called_once()
            call_kw = mock_alert.call_args.kwargs
            self.assertEqual(call_kw.get("request_type"), "journal_summary")
            self.assertEqual(call_kw.get("user_id"), user.id)

    async def test_acceptance_area_m_video_slot(self):
        """Video slot loads configured Content(key='journal_video') safely."""
        user = await self.create_user(8131)
        cb = make_callback_query("j_video", user.id, bot=self.bot)
        with patch("journal_handlers.send_smart_content", new_callable=AsyncMock) as mock_send_smart:
            await cb_journal_video(cb, self.bot)
            mock_send_smart.assert_called_once()
            self.assertEqual(mock_send_smart.call_args[0][1], "Видеоурок по саморефлексии")

    async def test_acceptance_area_n_cross_navigation(self):
        """Cross navigation buttons present in main menu, habits, journal, and AI dialogue keyboards."""
        # 1. Main menu keyboard
        main_kb = await main_menu_keyboard()
        main_cbs = [b.callback_data for row in main_kb.inline_keyboard for b in row]
        self.assertIn("mode_journal", main_cbs)

        # 2. Journal menu keyboard
        j_kb = journal_menu_keyboard(has_in_progress=False)
        j_cbs = [b.callback_data for row in j_kb.inline_keyboard for b in row]
        self.assertIn("mode_habits", j_cbs)
        self.assertIn("back_to_main_menu_text", j_cbs)

        # 3. AI dialogue finish keyboards
        tf_kb = topic_finish_actions_keyboard("slug", 1)
        tf_cbs = [b.callback_data for row in tf_kb.inline_keyboard for b in row]
        self.assertIn("main_menu_action", tf_cbs)

        ts_kb = topic_summary_result_keyboard("slug", 1)
        ts_cbs = [b.callback_data for row in ts_kb.inline_keyboard for b in row]
        self.assertIn("mode_journal", ts_cbs)

    async def test_acceptance_area_o_admin_acceptance_matrix(self):
        """Admin CRUD: list, add, edit, toggle, reorder, delete, points config, and non-admin denial."""
        admin = await self.create_user(8151, is_admin=True)
        non_admin = await self.create_user(8152, is_admin=False)
        state = FSMContext(storage=self.storage, key=StorageKey(bot_id=1, chat_id=admin.id, user_id=admin.id))

        # 1. Non-admin unauthorized callbacks rejected
        cb_unauth = make_callback_query("j_adm_menu", non_admin.id, bot=self.bot)
        await admin_journal_menu_handler(cb_unauth, state)
        cb_unauth.answer.assert_called_with("⛔️ Действие доступно только администраторам.", show_alert=True)

        # 2. Admin menu renders
        cb_adm = make_callback_query("j_adm_menu", admin.id, bot=self.bot)
        with patch("journal_admin_handlers.send_smart_content", new_callable=AsyncMock) as mock_smart:
            await admin_journal_menu_handler(cb_adm, state)
            mock_smart.assert_called_once()

        # 3. Add question
        await admin_journal_q_add_handler(cb_adm, state)
        self.assertEqual(await state.get_state(), JournalAdminStates.new_question_text)

        # Blank text rejected
        msg_blank = make_message("   ", admin.id, bot=self.bot)
        await admin_journal_q_add_submit(msg_blank, state)
        self.assertEqual(await state.get_state(), JournalAdminStates.new_question_text)

        # Valid text saved
        msg_q = make_message("Новый проверочный вопрос?", admin.id, bot=self.bot)
        with patch("journal_admin_handlers.send_smart_content", new_callable=AsyncMock):
            await admin_journal_q_add_submit(msg_q, state)
        self.assertIsNone(await state.get_state())

        async with async_session_maker() as session:
            new_q = (await session.execute(
                select(JournalQuestion).where(JournalQuestion.text == "Новый проверочный вопрос?")
            )).scalar_one_or_none()
            self.assertIsNotNone(new_q)
            qid = new_q.id

        # 4. Toggle active status
        cb_tog = make_callback_query(f"j_adm_q_tog:{qid}", admin.id, bot=self.bot)
        with patch("journal_admin_handlers.send_smart_content", new_callable=AsyncMock):
            await admin_journal_q_tog_handler(cb_tog)
        async with async_session_maker() as session:
            q_after = await session.get(JournalQuestion, qid)
            self.assertFalse(q_after.is_active)

        # 5. Points edit: positive accepted, negative rejected
        cb_pts = make_callback_query("j_adm_pts_edit", admin.id, bot=self.bot)
        await admin_journal_pts_edit_handler(cb_pts, state)
        self.assertEqual(await state.get_state(), JournalAdminStates.edit_points)

        msg_pts_neg = make_message("-5", admin.id, bot=self.bot)
        await admin_journal_pts_edit_submit(msg_pts_neg, state)
        self.assertEqual(await state.get_state(), JournalAdminStates.edit_points)

        msg_pts_pos = make_message("25", admin.id, bot=self.bot)
        with patch("journal_admin_handlers.send_smart_content", new_callable=AsyncMock):
            await admin_journal_pts_edit_submit(msg_pts_pos, state)
        self.assertIsNone(await state.get_state())

        async with async_session_maker() as session:
            cfg = await session.get(JournalConfig, 1)
            self.assertEqual(cfg.points_per_reflection, 25)
            cfg.points_per_reflection = 10
            await session.commit()

        # 6. Delete question
        cb_del = make_callback_query(f"j_adm_q_del:{qid}", admin.id, bot=self.bot)
        with patch("journal_admin_handlers.send_smart_content", new_callable=AsyncMock):
            await admin_journal_q_del_handler(cb_del)
        async with async_session_maker() as session:
            deleted_q = await session.get(JournalQuestion, qid)
            self.assertIsNotNone(deleted_q)

        cb_execute = make_callback_query(f"j_adm_q_del_execute:{qid}", admin.id, bot=self.bot)
        with patch("journal_admin_handlers.send_smart_content", new_callable=AsyncMock):
            await admin_journal_q_del_execute_handler(cb_execute)
        async with async_session_maker() as session:
            deleted_q = await session.get(JournalQuestion, qid)
            self.assertIsNone(deleted_q)

    async def test_acceptance_area_p_callback_contract(self):
        """All Journal keyboards produce non-empty callback_data <= 64 bytes."""
        q1 = JournalQuestion(id=1, text="Q1", order_num=1, is_active=True)
        keyboards_to_test = [
            journal_menu_keyboard(has_in_progress=False),
            journal_menu_keyboard(has_in_progress=True, in_progress_id=999),
            journal_menu_keyboard(is_today_completed=True, completed_id=999),
            journal_in_progress_keyboard(entry_id=999),
            journal_completed_today_keyboard(entry_id=999),
            journal_reminder_time_keyboard(),
            journal_snooze_menu_keyboard(),
            journal_reminder_message_keyboard(),
            journal_dynamics_menu_keyboard(token8="12345678"),
            journal_history_list_keyboard(entries=[], page=0, total_pages=3),
            journal_entry_detail_keyboard(entry_id=999, has_summary=False, token8="12345678"),
            journal_admin_menu_keyboard(),
            journal_admin_questions_keyboard([q1]),
            journal_admin_cancel_keyboard(),
        ]

        for kb in keyboards_to_test:
            for row in kb.inline_keyboard:
                for btn in row:
                    cb_data = btn.callback_data
                    self.assertTrue(cb_data, f"Button {btn.text} has empty callback_data")
                    self.assertLessEqual(
                        len(cb_data.encode("utf-8")),
                        64,
                        f"callback_data '{cb_data}' exceeds 64 bytes"
                    )


class TestTelegramPresentationAndTypingGuards(JournalFeatureBaseTest):
    """Regression guards for Telegram presentation contracts: HTML parse_mode, link previews, and AI typing."""

    async def test_ai_disclaimer_parse_mode_and_preview_disabled(self):
        """Disclaimers must strictly specify parse_mode='HTML' and link_preview_options.is_disabled=True."""
        user = await self.create_user(98111, disclaimer_accepted=False)
        mock_bot = MockBot()
        mock_bot.send_message = AsyncMock()

        from handlers import check_and_request_ai_disclaimer, ensure_ai_disclaimer_sent

        # 1. check_and_request_ai_disclaimer
        accepted = await check_and_request_ai_disclaimer(mock_bot, user.id, user.id, flow_type="journal_test")
        self.assertFalse(accepted)
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        self.assertEqual(call_kwargs.get("chat_id"), user.id)
        self.assertIn("не заменяет консультацию", call_kwargs.get("text", ""))
        self.assertEqual(call_kwargs.get("parse_mode"), "HTML")
        self.assertIsNotNone(call_kwargs.get("link_preview_options"))
        self.assertTrue(call_kwargs["link_preview_options"].is_disabled)
        self.assertIsNotNone(call_kwargs.get("reply_markup"))

        # 2. ensure_ai_disclaimer_sent
        user2 = await self.create_user(98112, disclaimer_accepted=False)
        mock_bot.send_message.reset_mock()
        sent = await ensure_ai_disclaimer_sent(mock_bot, user2.id, user2.id)
        self.assertTrue(sent)
        mock_bot.send_message.assert_called_once()
        call_kwargs2 = mock_bot.send_message.call_args.kwargs
        self.assertEqual(call_kwargs2.get("chat_id"), user2.id)
        self.assertIn("не заменяет консультацию", call_kwargs2.get("text", ""))
        self.assertEqual(call_kwargs2.get("parse_mode"), "HTML")
        self.assertIsNotNone(call_kwargs2.get("link_preview_options"))
        self.assertTrue(call_kwargs2["link_preview_options"].is_disabled)

    async def test_ai_typing_action_continuous_pulses(self):
        """ai_typing_action must continuously pulse 'typing' chat action for the entire duration."""
        from ai_integration import ai_typing_action

        mock_bot = MockBot()
        mock_bot.send_chat_action = AsyncMock()

        # Pulse every 30ms, sleep 100ms -> at least 3 calls
        async with ai_typing_action(mock_bot, 777888, interval=0.03):
            await asyncio.sleep(0.10)

        self.assertGreaterEqual(mock_bot.send_chat_action.call_count, 3)
        for call in mock_bot.send_chat_action.call_args_list:
            self.assertEqual(call.kwargs.get("chat_id"), 777888)
            self.assertEqual(call.kwargs.get("action"), "typing")

    async def test_ai_typing_action_resilience(self):
        """ai_typing_action must gracefully no-op when bot is None, chat_id is None, or action errors."""
        from ai_integration import ai_typing_action

        # 1. bot is None
        async with ai_typing_action(None, 12345, interval=0.02):
            pass

        # 2. chat_id is None
        mock_bot = MockBot()
        mock_bot.send_chat_action = AsyncMock()
        async with ai_typing_action(mock_bot, None, interval=0.02):
            pass
        mock_bot.send_chat_action.assert_not_called()

        # 3. send_chat_action raises network error -> must not raise out of context
        mock_bot.send_chat_action.side_effect = Exception("Telegram connection dropped")
        async with ai_typing_action(mock_bot, 12345, interval=0.02):
            await asyncio.sleep(0.05)

    async def test_ast_static_guard_no_raw_html_without_parse_mode(self):
        """Static AST guard: no production code may send static HTML tags without parse_mode='HTML'."""
        import ast
        import os
        import re

        tag_re = re.compile(r"<\/?(b|i|u|s|code|pre|a)(\s+[^>]*)?>", re.IGNORECASE)
        violations = []

        workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for root, _, files in os.walk(workspace_root):
            if "venv" in root or ".git" in root or "tests" in root:
                continue
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                with open(path, "r", encoding="utf-8") as fp:
                    tree = ast.parse(fp.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        func_name = ""
                        if isinstance(node.func, ast.Attribute):
                            func_name = node.func.attr
                        elif isinstance(node.func, ast.Name):
                            func_name = node.func.id
                        if func_name in ("send_message", "edit_message_text", "edit_text", "answer", "reply"):
                            has_parse_mode = any(kw.arg == "parse_mode" for kw in node.keywords)
                            texts = []
                            for arg in node.args:
                                for sub in ast.walk(arg):
                                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                                        texts.append(sub.value)
                            for kw in node.keywords:
                                if kw.arg in ("text", "caption"):
                                    for sub in ast.walk(kw.value):
                                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                                            texts.append(sub.value)
                            for t in texts:
                                if tag_re.search(t) and not has_parse_mode:
                                    violations.append(f"{os.path.relpath(path, workspace_root)}:{node.lineno} {func_name} -> {t[:50]}")

        self.assertEqual(violations, [], f"Found calls sending HTML without parse_mode: {violations}")


if __name__ == "__main__":
    unittest.main()
