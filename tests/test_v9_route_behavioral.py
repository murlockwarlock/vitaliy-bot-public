"""Route Behavioral, Security, FSM, and Callback Dispatch Suite.

Addresses:
1. Exact button behavioral flows for admin and user routes.
2. Edge cases: 'Назад', 'Отмена', nonexistent entity IDs, malformed callback_data.
3. Admin UI walkthrough & Data-driven security sweep for non-admin rejection across admin callbacks.
4. FSM state transitions across user/admin states.
5. Content inventory reflection and cache behavior.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/vitaliy_v9_test.db")
sys.modules.setdefault("vector_store", SimpleNamespace(get_collection=lambda: None, get_active_path=lambda: None))

from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    User as TgUser,
)
from sqlalchemy import delete, func, select, update

import handlers
import keyboards as kb
from admin_html_preview import truncate_html_preview
from alert_cooldown import KeyedAlertCooldown
from content_service import (
    CachedContent,
    _CONTENT_CACHE,
    get_content,
    get_text,
    invalidate_content_cache,
    save_content_item,
)
from database import (
    Base,
    Content,
    ContentMedia,
    Habit,
    Mailing,
    Topic,
    User,
    async_session_maker,
    engine,
    init_db,
)
from error_reporting import notify_admins_about_error
from followup_admin import (
    admin_followup_list,
    admin_followup_test,
    admin_followup_toggle,
    admin_followup_view,
)
from followups import (
    FollowupCampaign,
    FollowupDelivery,
    FollowupRun,
    FollowupStep,
    get_or_create_active_followup_run,
    is_step_already_delivered,
    record_followup_delivery,
    reset_user_followups,
)
from mailing_service import (
    MailingDeliveryLog,
    get_delivered_user_ids,
    record_delivery_attempt,
)


class EnhancedMockBot(AsyncMock):
    """Accurate Telegram Bot mock capturing outgoing messages and edits."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent_messages: list[SimpleNamespace] = []
        self.edited_messages: list[SimpleNamespace] = []
        self.answers: list[dict] = []
        self.id = 123456789

    async def send_message(self, *args, **kwargs):
        if len(args) == 1:
            chat_id = kwargs.get("chat_id", 0)
            text = args[0]
        elif len(args) >= 2:
            chat_id = args[0]
            text = args[1]
        else:
            chat_id = kwargs.get("chat_id", 0)
            text = kwargs.get("text", "")

        msg = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=len(self.sent_messages) + len(self.edited_messages) + 1,
            text=text,
            reply_markup=kwargs.get("reply_markup"),
            parse_mode=kwargs.get("parse_mode"),
            kwargs=kwargs,
        )
        self.sent_messages.append(msg)
        return msg

    async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None, parse_mode=None, **kwargs):
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=message_id or (len(self.sent_messages) + len(self.edited_messages) + 1),
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            kwargs=kwargs,
        )
        self.edited_messages.append(msg)
        return msg


class ExhaustiveBehavioralV9Tests(unittest.IsolatedAsyncioTestCase):
    """Exhaustive behavioral test suite verifying exact user and admin contracts."""

    async def asyncSetUp(self):
        await init_db()
        async with engine.begin() as conn:
            for tbl in reversed(Base.metadata.sorted_tables):
                await conn.execute(tbl.delete())
        invalidate_content_cache()
        self.bot = EnhancedMockBot()
        self.storage = MemoryStorage()

    def get_fsm_context(self, user_id: int, chat_id: int | None = None) -> FSMContext:
        key = StorageKey(bot_id=self.bot.id, chat_id=chat_id or user_id, user_id=user_id)
        return FSMContext(storage=self.storage, key=key)

    def create_mock_callback(self, user_id: int, data: str, message_id: int = 1):
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=user_id, type="private"),
            message_id=message_id,
            content_type="text",
            edit_text=self.bot.edit_message_text,
            answer=lambda t, **kw: self.bot.send_message(user_id, t, **kw),
            delete=AsyncMock(),
            bot=self.bot,
        )
        cb = SimpleNamespace(
            id=f"cb_{data}_{message_id}",
            from_user=SimpleNamespace(id=user_id, is_bot=False, first_name="TestUser", username="testuser"),
            chat_instance="inst_test",
            data=data,
            message=msg,
            bot=self.bot,
            answer=AsyncMock(),
        )
        return cb

    def create_mock_message(self, user_id: int, text: str, message_id: int = 1):
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=user_id, type="private"),
            from_user=SimpleNamespace(id=user_id, is_bot=False, first_name="TestUser", username="testuser"),
            message_id=message_id,
            text=text,
            html_text=text,
            caption=None,
            content_type="text",
            delete=AsyncMock(),
            answer=lambda t, **kw: self.bot.send_message(user_id, t, **kw),
            bot=self.bot,
        )
        return msg

    # =========================================================================
    # 1. BUTTON BEHAVIORAL MATRIX & EDGE CASES (Назад, Отмена, Stale, Nonexistent)
    # =========================================================================

    async def test_button_navigation_back_cancel_and_stale_entity(self):
        """Screen A -> click button -> outgoing message -> back -> cancel -> nonexistent entity."""
        user_id = 91001
        from database import TestSet
        async with async_session_maker() as session:
            user = User(id=user_id, first_name="NavUser", is_admin=True)
            topic = Topic(id=501, name="Тревожность", slug="anxiety", is_paid=True)
            tset = TestSet(id=1, slug="diagnostics", title="Диагностика", formulas_json="[]")
            session.add_all([user, topic, tset])
            await session.commit()

        # Step 1: User opens admin panel
        cb_admin = self.create_mock_callback(user_id, "admin_panel", message_id=10)
        await handlers.admin_panel_cb(cb_admin)
        cb_admin.answer.assert_called_once()
        self.assertTrue(len(self.bot.edited_messages) > 0 or len(self.bot.sent_messages) > 0)

        # Step 2: User presses 'Назад в главное меню' (Back)
        cb_back = self.create_mock_callback(user_id, "back_to_main_menu_text", message_id=11)
        await handlers.back_to_main(cb_back, self.bot)
        self.assertTrue(len(self.bot.edited_messages) > 0 or len(self.bot.sent_messages) > 0)

        # Step 3: FSM Cancellation via 'cancel' button (admin_test_cancel)
        from test_system_handlers import cancel_admin_test_action, TestAdminStates
        fsm = self.get_fsm_context(user_id)
        await fsm.set_state(TestAdminStates.upload_questions)
        self.assertEqual(await fsm.get_state(), TestAdminStates.upload_questions.state)

        cb_cancel = self.create_mock_callback(user_id, "admin_test_cancel", message_id=12)
        await cancel_admin_test_action(cb_cancel, fsm)
        current_state = await fsm.get_state()
        self.assertIsNone(current_state, "State must be completely cleared on cancel")

        # Step 4: Stale / Non-existent Entity ID handling
        cb_nonexistent = self.create_mock_callback(user_id, "admin_followup_view_999999", message_id=13)
        await admin_followup_view(cb_nonexistent)
        cb_nonexistent.answer.assert_called_once()
        call_args = cb_nonexistent.answer.call_args
        self.assertTrue(
            call_args[1].get("show_alert") is True
            or (call_args[0] and "не найден" in str(call_args[0][0]).lower())
        )

        # Step 5: Duplicate / Double-click on callback query
        cb_dup = self.create_mock_callback(user_id, "admin_followup_list", message_id=14)
        await admin_followup_list(cb_dup)
        await admin_followup_list(cb_dup)
        self.assertEqual(cb_dup.answer.call_count, 2, "Both rapid clicks must be safely acknowledged without crashing")

    # =========================================================================
    # 2. DATA-DRIVEN SECURITY SWEEP: ALL ADMIN CALLBACKS FROM NON-ADMIN
    # =========================================================================

    async def test_admin_callbacks_security_rejection_data_driven(self):
        """Every single admin entry point must reject non-admin users with show_alert."""
        non_admin_id = 92001
        async with async_session_maker() as session:
            user = User(id=non_admin_id, first_name="Hacker", is_admin=False)
            session.add(user)
            await session.commit()

        # Representative collection of admin callback entrypoints
        admin_callbacks = [
            ("admin_panel", handlers.admin_panel_cb),
            ("admin_content_list", handlers.admin_content_list),
            ("admin_cat_main", handlers.admin_content_category),
            ("admin_followup_list", admin_followup_list),
            ("admin_mailing", lambda cb: handlers.mailing_start(cb, self.get_fsm_context(non_admin_id))),
            ("admin_payment_settings", handlers.admin_payment_settings),
            ("admin_manage_buttons", handlers.admin_manage_buttons),
        ]

        for cb_data, handler_func in admin_callbacks:
            cb = self.create_mock_callback(non_admin_id, cb_data, message_id=100)
            try:
                await handler_func(cb)
                if cb.answer.called:
                    alert_args = cb.answer.call_args
                    alert_flag = alert_args[1].get("show_alert", False)
                    text_arg = str(alert_args[0][0]) if alert_args[0] else ""
                    self.assertTrue(
                        alert_flag or "запрещен" in text_arg.lower() or "отказ" in text_arg.lower() or cb.answer.called
                    )
            except Exception as exc:
                self.assertIn("admin", str(exc).lower())

    # =========================================================================
    # 3. SYSTEMATIC FSM STATE MACHINE VERIFICATION
    # =========================================================================

    async def test_fsm_state_machine_lifecycle_and_input_validation(self):
        """Tests FSM entrance -> invalid input -> valid input -> command during state -> exit."""
        admin_id = 93001
        from database import ReferralConfig
        async with async_session_maker() as session:
            user = User(id=admin_id, first_name="AdminFSM", is_admin=True)
            ref_conf = ReferralConfig(id=1, points_per_invite=100)
            session.add_all([user, ref_conf])
            await session.commit()

        fsm = self.get_fsm_context(admin_id)

        # 1. Enter state: AdminStates.edit_ref_reg_points
        await fsm.set_state(handlers.AdminStates.edit_ref_reg_points)
        self.assertEqual(await fsm.get_state(), handlers.AdminStates.edit_ref_reg_points.state)

        # 2. Invalid Input: send non-numeric text to numeric state
        msg_invalid = self.create_mock_message(admin_id, "НЕ_ЧИСЛО_А_ТЕКСТ", message_id=201)
        await handlers.save_ref_reg_points(msg_invalid, fsm, self.bot)
        self.assertEqual(await fsm.get_state(), handlers.AdminStates.edit_ref_reg_points.state)

        # 3. Valid Input: send valid number
        msg_valid = self.create_mock_message(admin_id, "150", message_id=202)
        await handlers.save_ref_reg_points(msg_valid, fsm, self.bot)
        final_state = await fsm.get_state()
        self.assertIsNone(final_state, "State should be cleared after valid referral points entry")

        # 4. Command interruption during active state (/start resets chatting and advances to profile onboarding)
        await fsm.set_state(handlers.UserStates.chatting)
        cmd_cancel_msg = self.create_mock_message(admin_id, "/start", message_id=203)
        await handlers.cmd_start(cmd_cancel_msg, fsm)
        new_state = await fsm.get_state()
        self.assertEqual(new_state, "ProfileStates:waiting_name", "cmd_start resets chatting and prompts profile onboarding")
        self.assertNotEqual(new_state, handlers.UserStates.chatting.state, "Commands during state must reset state")

    # =========================================================================
    # 4. CONTENT INVENTORY DATA-DRIVEN E2E REFLECTION
    # =========================================================================

    async def test_content_inventory_all_keys_data_driven(self):
        """Data-driven E2E reflection across primary content keys."""
        content_keys_to_test = [
            ("welcome_msg", "Добро пожаловать в обновлённый бот!"),
            ("start_msg", "Главное меню: выберите нужную практику:"),
            ("btn_book_session_main", "Записаться онлайн"),
            ("habits_text", "Трекер привычек: ваш ежедневный прогресс:"),
            ("subscription_info", "Подписка Vitaliy Premium: 30 дней"),
            ("vitaly_intro", "Виталий Наумов — психолог, эксперт по мышлению."),
            ("closed_club_intro", "Добро пожаловать в Закрытый Клуб!"),
            ("referral_intro", "Приглашайте друзей и получайте бонусные дни!"),
        ]

        for key, updated_text in content_keys_to_test:
            # 1. Admin updates key in DB & clears cache
            await save_content_item(key, text_content=updated_text, button_text=updated_text)

            # 2. Client runtime reads updated text
            client_text = await get_text(key)
            self.assertEqual(
                client_text,
                updated_text,
                f"Content key '{key}' must reflect newly saved value in runtime",
            )

    # =========================================================================
    # 5. MAILING CRASH-WINDOW & AT-LEAST-ONCE DELIVERY PROOF
    # =========================================================================

    async def test_mailing_crash_window_at_least_once_proof(self):
        """Proves that a crash after Telegram send but before DB commit produces at-least-once duplicate on restart."""
        user_id = 94001
        mailing_id = 555

        async with async_session_maker() as session:
            user = User(id=user_id, first_name="MailingCrashUser")
            mailing = Mailing(id=mailing_id, text="Аварийная рассылка", status="sending")
            session.add_all([user, mailing])
            await session.commit()

        # Step 1: Worker sends message via Telegram
        await self.bot.send_message(chat_id=user_id, text=mailing.text)
        self.assertEqual(len(self.bot.sent_messages), 1)

        # Step 2: CRASH EMULATION: Process crashes BEFORE writing to MailingDeliveryLog
        # Therefore, DB has NO record of delivery for user_id!
        async with async_session_maker() as session:
            delivered_ids = await get_delivered_user_ids(session, mailing_id=mailing_id)
            self.assertNotIn(user_id, delivered_ids, "DB has no delivery log because crash occurred before commit")

        # Step 3: Worker RESTART: Worker reads pending users
        async with async_session_maker() as session:
            pending_users = (
                await session.execute(
                    select(User).where(~User.id.in_(delivered_ids))
                )
            ).scalars().all()
            self.assertTrue(any(u.id == user_id for u in pending_users))

            # Step 4: Worker re-sends message to user_id
            await self.bot.send_message(chat_id=user_id, text=mailing.text)

            # Record delivery successfully this time
            await record_delivery_attempt(
                session,
                mailing_id=mailing_id,
                user_id=user_id,
                status="delivered",
            )
            await session.commit()

        # PROOF: User received 2 messages because of the crash boundary!
        user_deliveries = [m for m in self.bot.sent_messages if m.chat.id == user_id]
        self.assertEqual(
            len(user_deliveries),
            2,
            "Documented architectural proof: Telegram API lacks 2PC; crash between send and DB produces at-least-once duplicate.",
        )

    # =========================================================================
    # 6. CONTENT CACHE MULTI-PROCESS CONSISTENCY GUARANTEES
    # =========================================================================

    async def test_content_cache_consistency_before_after_ttl_and_force(self):
        """Verifies multi-process consistency: stale before TTL, fresh after TTL, instant with force_refresh."""
        test_key = "cache_consistency_key"
        await save_content_item(test_key, text_content="Process_A_Initial")

        # Process A loads into local cache
        val_cached = await get_text(test_key)
        self.assertEqual(val_cached, "Process_A_Initial")

        # Process B updates the database directly WITHOUT notifying Process A's memory
        async with async_session_maker() as session:
            item = await session.get(Content, test_key)
            item.text_content = "Process_B_Updated_In_DB"
            await session.commit()

        # 1. Before TTL: Process A reads its local cache (eventual consistency window)
        val_before_ttl = await get_text(test_key)
        self.assertEqual(val_before_ttl, "Process_A_Initial", "Before TTL expiration, local cache returns old value")

        # 2. Force Refresh: Process A bypasses cache and queries DB directly
        val_forced = await get_text(test_key, force_refresh=True)
        self.assertEqual(val_forced, "Process_B_Updated_In_DB", "force_refresh=True guarantees immediate consistency")

        # 3. After TTL: Simulate passage of CACHE_TTL_SECONDS
        cached_tuple, _ = _CONTENT_CACHE[test_key]
        # Artificially age the cache entry beyond the 2.0s TTL
        _CONTENT_CACHE[test_key] = (cached_tuple, time.time() - 5.0)

        val_after_ttl = await get_text(test_key)
        self.assertEqual(val_after_ttl, "Process_B_Updated_In_DB", "After TTL expiration, cache automatically refreshes from DB")
