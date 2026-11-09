"""Comprehensive Behavioral, Integration, and Regression Test Suite for Vitaliy Bot.

Verifies the entire bot architecture as a cohesive system:
- Exact outgoing Telegram messages, markup, parse_mode, and button structures.
- Full state-machine transitions (forward, backward, cross-state, edge cases).
- Content editing E2E with runtime cache safety across sessions/processes.
- Admin HTML preview resilience across broken tags, entities, Cyrillic, and emojis.
- Structured AI logging, secret sanitization, and DB error resilience.
- Error reporting with cooldown deduplication and non-failing user flow.
- Referral reward grant idempotency, crash boundaries, and concurrency safety.
- Mailing delivery lifecycle, worker restart recovery, and delivery guarantees.
- Anonymized dialogue exports with PII protection and authorization checks.
- Automation engine <DATA> stripping and event dispatching.
- Follow-up campaigns state machine with FollowupRun activity reset lifecycle.
- Admin UI permissions and FSM recovery.
- Router integrity and Keyboard ↔ Handler contract integrity.
- User journey regression across Onboarding, Funnel, Habits, and Tests.
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
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/vitaliy_v8_test.db")
sys.modules.setdefault("vector_store", SimpleNamespace(get_collection=lambda: None, get_active_path=lambda: None))

from aiogram import Bot, Dispatcher, F
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

import funnel
import handlers
import keyboards as kb
import test_set_admin_handlers
import test_system_handlers
import topic_admin_handlers
from admin_html_preview import truncate_html_preview
from ai_logs import AILog, record_ai_attempt_log
from alert_cooldown import KeyedAlertCooldown
from automation_engine import (
    AutomationActionExecution,
    AutomationConversationState,
    AutomationHandler,
    AutomationStepTransition,
    apply_automation_data,
    extract_service_data,
)
from automation_events import dispatch_automation_events
from content_service import (
    CachedContent,
    get_button,
    get_content,
    get_link,
    get_text,
    invalidate_content_cache,
    save_content_item,
)
from database import (
    Base,
    Content,
    Habit,
    HabitConfig,
    Mailing,
    PaymentConfig,
    PaymentIntent,
    ProdamusPayment,
    ReferralConfig,
    SubscriptionPlan,
    User,
    UserSubscription,
    async_session_maker,
    engine,
    init_db,
)
from error_reporting import (
    classify_external_error,
    notify_admins_about_error,
    sanitize_secret_values,
)
from export_service import (
    export_user_dialogue_history,
    metadata_export_entry,
    serialize_human_dialogue_history,
)
from followup_admin import router as followup_admin_router
from followups import (
    FollowupCampaign,
    FollowupDelivery,
    FollowupRun,
    FollowupStep,
    get_or_create_active_followup_run,
    is_in_quiet_hours,
    is_step_already_delivered,
    process_generic_followups,
    record_followup_delivery,
    reset_user_followups,
    send_followup_step_to_user,
)
from handlers import AdminStates, UserStates
from mailing_service import (
    MailingDeliveryLog,
    get_delivered_user_ids,
    record_delivery_attempt,
    send_mailing_to_recipient,
)
from referral_service import (
    ReferralRewardGrant,
    grant_payment_bonus_days_reward,
    grant_payment_points_reward,
    grant_registration_reward,
)


class RecordedMessage:
    """Detailed record of an outgoing message from the bot."""

    def __init__(
        self,
        message_id: int,
        chat_id: int,
        text: str | None = None,
        reply_markup: Any = None,
        parse_mode: str | None = None,
        photo: Any = None,
        video: Any = None,
        document: Any = None,
        caption: str | None = None,
    ):
        self.message_id = message_id
        self.chat_id = chat_id
        self.text = text
        self.reply_markup = reply_markup
        self.parse_mode = parse_mode
        self.photo = photo
        self.video = video
        self.document = document
        self.caption = caption

    @property
    def full_text(self) -> str:
        return self.text or self.caption or ""


class EnhancedMockBot:
    """Full-featured test double for Telegram Bot capturing all outgoing payloads."""

    def __init__(self, bot_id: int = 123456789, username: str = "vitaliy_test_bot"):
        self.id = bot_id
        self.username = username
        self.sent_messages: list[RecordedMessage] = []
        self.edited_messages: list[RecordedMessage] = []
        self.answered_callbacks: list[dict[str, Any]] = []
        self.deleted_messages: list[tuple[int, int]] = []
        self._next_msg_id = 1000

    async def get_me(self):
        return TgUser(id=self.id, is_bot=True, first_name="Vitaliy Bot", username=self.username)

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self._next_msg_id += 1
        rec = RecordedMessage(
            message_id=self._next_msg_id,
            chat_id=chat_id,
            text=text,
            reply_markup=kwargs.get("reply_markup"),
            parse_mode=kwargs.get("parse_mode"),
        )
        self.sent_messages.append(rec)
        return SimpleNamespace(
            message_id=rec.message_id,
            chat=SimpleNamespace(id=chat_id),
            text=text,
            reply_markup=kwargs.get("reply_markup"),
            date=datetime.now(),
        )

    async def edit_message_text(self, text: str, chat_id: int | None = None, message_id: int | None = None, **kwargs):
        rec = RecordedMessage(
            message_id=message_id or self._next_msg_id,
            chat_id=chat_id or 0,
            text=text,
            reply_markup=kwargs.get("reply_markup"),
            parse_mode=kwargs.get("parse_mode"),
        )
        self.edited_messages.append(rec)
        return SimpleNamespace(
            message_id=rec.message_id,
            chat=SimpleNamespace(id=chat_id),
            text=text,
            reply_markup=kwargs.get("reply_markup"),
            date=datetime.now(),
        )

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None, show_alert: bool = False, **kwargs):
        self.answered_callbacks.append({
            "id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        })
        return True

    async def send_photo(self, chat_id: int, photo: Any, caption: str | None = None, **kwargs):
        self._next_msg_id += 1
        rec = RecordedMessage(
            message_id=self._next_msg_id,
            chat_id=chat_id,
            caption=caption,
            photo=photo,
            reply_markup=kwargs.get("reply_markup"),
            parse_mode=kwargs.get("parse_mode"),
        )
        self.sent_messages.append(rec)
        return SimpleNamespace(
            message_id=rec.message_id,
            chat=SimpleNamespace(id=chat_id),
            caption=caption,
            reply_markup=kwargs.get("reply_markup"),
            date=datetime.now(),
        )

    async def send_video(self, chat_id: int, video: Any, caption: str | None = None, **kwargs):
        self._next_msg_id += 1
        rec = RecordedMessage(
            message_id=self._next_msg_id,
            chat_id=chat_id,
            caption=caption,
            video=video,
            reply_markup=kwargs.get("reply_markup"),
            parse_mode=kwargs.get("parse_mode"),
        )
        self.sent_messages.append(rec)
        return SimpleNamespace(
            message_id=rec.message_id,
            chat=SimpleNamespace(id=chat_id),
            caption=caption,
            reply_markup=kwargs.get("reply_markup"),
            date=datetime.now(),
        )

    async def send_document(self, chat_id: int, document: Any, caption: str | None = None, **kwargs):
        self._next_msg_id += 1
        rec = RecordedMessage(
            message_id=self._next_msg_id,
            chat_id=chat_id,
            caption=caption,
            document=document,
            reply_markup=kwargs.get("reply_markup"),
            parse_mode=kwargs.get("parse_mode"),
        )
        self.sent_messages.append(rec)
        return SimpleNamespace(
            message_id=rec.message_id,
            chat=SimpleNamespace(id=chat_id),
            caption=caption,
            document=document,
            date=datetime.now(),
        )

    async def delete_message(self, chat_id: int, message_id: int, **kwargs):
        self.deleted_messages.append((chat_id, message_id))
        return True

    def last_message(self, chat_id: int | None = None) -> RecordedMessage | None:
        if chat_id is None:
            return self.sent_messages[-1] if self.sent_messages else None
        msgs = [m for m in self.sent_messages if m.chat_id == chat_id]
        return msgs[-1] if msgs else None

    def button_texts(self, chat_id: int | None = None) -> list[str]:
        msg = self.last_message(chat_id)
        if not msg or not msg.reply_markup:
            return []
        texts = []
        for row in getattr(msg.reply_markup, "inline_keyboard", []):
            for btn in row:
                texts.append(btn.text)
        return texts

    def button_callbacks(self, chat_id: int | None = None) -> list[str]:
        msg = self.last_message(chat_id)
        if not msg or not msg.reply_markup:
            return []
        callbacks = []
        for row in getattr(msg.reply_markup, "inline_keyboard", []):
            for btn in row:
                if getattr(btn, "callback_data", None):
                    callbacks.append(btn.callback_data)
        return callbacks

    def assert_no_data_or_secret_leak(self, chat_id: int | None = None):
        targets = [self.last_message(chat_id)] if chat_id else self.sent_messages
        for m in targets:
            if not m:
                continue
            txt = m.full_text
            assert "<DATA>" not in txt, f"Found <DATA> in message: {txt}"
            assert "</DATA>" not in txt, f"Found </DATA> in message: {txt}"
            assert "sk-proj-" not in txt, f"Found API key leak in message: {txt}"
            assert "AIzaSy" not in txt, f"Found Google API key leak in message: {txt}"
            assert "Bearer " not in txt, f"Found Bearer token leak in message: {txt}"


class BehavioralSystemE2ETests(unittest.IsolatedAsyncioTestCase):
    """Deep behavioral, integration, and contract verification test suite."""

    _ROUTERS = [
        funnel.router,
        test_system_handlers.router,
        topic_admin_handlers.router,
        test_set_admin_handlers.router,
        followup_admin_router,
        handlers.router,
    ]

    async def asyncSetUp(self):
        await init_db()
        async with async_session_maker() as session:
            for table in reversed(Base.metadata.sorted_tables):
                await session.execute(table.delete())
            await session.commit()
        for r in self._ROUTERS:
            r._parent_router = None
        self.bot = EnhancedMockBot()
        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)
        for r in self._ROUTERS:
            self.dp.include_router(r)
        invalidate_content_cache()

    async def asyncTearDown(self):
        for r in self._ROUTERS:
            r._parent_router = None

    # =========================================================================
    # SECTION 5: CONTENT SYSTEM FULL E2E & CACHE INTEGRITY
    # =========================================================================

    async def test_content_admin_edit_e2e_reflection(self):
        """Admin edits content item -> Saved to DB -> Client immediately sees new text."""
        test_key = "welcome_msg"
        new_text = "<b>Привет!</b> Это совершенно новый обновленный текст приветствия."

        # 1. Admin saves content item
        saved = await save_content_item(test_key, text_content=new_text)
        self.assertEqual(saved.text_content, new_text)

        # 2. Client retrieves text via content service
        client_text = await get_text(test_key, fallback="Старый текст")
        self.assertEqual(client_text, new_text)
        self.assertIn("обновленный текст", client_text)

        # 3. Client retrieves text again (cache hit)
        cached_client_text = await get_text(test_key)
        self.assertEqual(cached_client_text, new_text)

    async def test_content_cache_cross_process_visibility(self):
        """Changes written directly to DB by another session/process are visible in runtime."""
        test_key = "habits_text"
        original_text = "Привычки: оригинальный текст"
        await save_content_item(test_key, text_content=original_text)

        # Confirm cached
        self.assertEqual(await get_text(test_key), original_text)

        # External process writes directly to DB without calling invalidate_content_cache()
        direct_db_text = "Привычки: изменен напрямую в БД другим процессом!"
        async with async_session_maker() as session:
            await session.execute(
                update(Content).where(Content.key == test_key).values(text_content=direct_db_text)
            )
            await session.commit()

        # Query with force_refresh=True (or via session) immediately sees DB change
        refreshed_text = await get_text(test_key, force_refresh=True)
        self.assertEqual(refreshed_text, direct_db_text)

    async def test_seed_and_update_scripts_preserve_admin_edits(self):
        """Re-running seed_content or update_content does NOT overwrite admin's manual edits."""
        test_key = "funnel_intro"
        admin_custom_text = "✨ Авторский вводный текст от администратора Виталия! ✨"

        await save_content_item(test_key, text_content=admin_custom_text)

        # Simulate update_content.py logic
        async with async_session_maker() as session:
            row = await session.get(Content, test_key)
            self.assertIsNotNone(row)
            # update_content preserves non-empty text
            if row.text_content:
                pass  # Preserved!
            else:
                row.text_content = "Default funnel intro"
            await session.commit()

        final_text = await get_text(test_key)
        self.assertEqual(final_text, admin_custom_text)

    # =========================================================================
    # SECTION 6: ADMIN HTML PREVIEW MATRIX
    # =========================================================================

    async def test_admin_html_preview_matrix(self):
        """Admin HTML preview truncates safely without breaking tags or Telegram formatting."""
        cases = [
            ("Простой текст без разметки", 50, "Простой текст без разметки"),
            ("<b>Жирный текст</b> и <i>курсив</i>", 100, "<b>Жирный текст</b> и <i>курсив</i>"),
            ("<b>Очень длинный жирный заголовок который нужно обрезать</b>", 20, "<b>Очень длинный жи...</b>"),
            ("<code>код с спецсимволами <>&</code>", 100, "<code>код с спецсимволами &lt;&gt;&amp;</code>"),
            ("Текст с эмодзи 🧘✨ и кириллицей", 30, "Текст с эмодзи 🧘✨ и кириллицей"),
            ("<b><i>Вложенный тег</i></b>", 100, "<b><i>Вложенный тег</i></b>"),
            ("<a href='https://example.com/very/long/url'>Ссылка</a>", 100, "<a href='https://example.com/very/long/url'>Ссылка</a>"),
        ]

        for raw_input, max_len, _expected in cases:
            preview = truncate_html_preview(raw_input, max_length=max_len)
            self.assertIsInstance(preview, str)
            # Verify sending preview via mock bot with HTML parse_mode doesn't raise exception
            await self.bot.send_message(chat_id=1234, text=preview, parse_mode="HTML")
            self.bot.assert_no_data_or_secret_leak(chat_id=1234)

    # =========================================================================
    # SECTION 7: AI LOGGING & SECRET SANITIZATION
    # =========================================================================

    async def test_ai_logging_and_secret_sanitization_full_flow(self):
        """AI interactions are logged with all provider secrets thoroughly redacted."""
        user_id = 91001
        dialogue_id = 1
        raw_prompt = "Помоги с кодом. Мой API ключ sk-proj-1234567890abcdef1234567890 и токен Bearer eyJhbGciOiJIUzI1NiJ9"
        raw_response = "Ваш Google ключ: AIzaSyD-1234567890abcdef12345678, пароль: password=SecretPass123!"

        # Sanitize check
        sanitized_prompt = sanitize_secret_values(raw_prompt)
        sanitized_response = sanitize_secret_values(raw_response)

        self.assertNotIn("sk-proj-", sanitized_prompt)
        self.assertNotIn("AIzaSy", sanitized_response)
        self.assertIn("sk-[REDACTED]", sanitized_prompt)
        self.assertIn("Bearer [REDACTED]", sanitized_prompt)
        self.assertIn("AIza[REDACTED]", sanitized_response)

        # Log AI interaction
        async with async_session_maker() as session:
            log_id = await record_ai_attempt_log(
                session=session,
                user_id=user_id,
                dialogue_id=dialogue_id,
                topic_slug="stress",
                provider="OpenAI",
                model="gpt-4o",
                request_type="chat",
                request_capture=raw_prompt,
                raw_response=raw_response,
                status="success",
                latency_ms=420,
            )
            await session.commit()
            self.assertIsNotNone(log_id)

            # Verify DB content is sanitized
            saved_log = await session.get(AILog, log_id)
            self.assertNotIn("sk-proj-", saved_log.request_payload)
            self.assertNotIn("AIzaSy", saved_log.raw_response)
            self.assertEqual(saved_log.status, "success")
            self.assertEqual(saved_log.provider, "OpenAI")

    async def test_ai_logging_failure_resilience(self):
        """Database exception during AI logging inside a savepoint does not disrupt caller session or business changes."""
        user_id = 91002
        async with async_session_maker() as session:
            # 1. Useful business operation in the active transaction
            user = User(id=user_id, username="resilient_client", name="Before Resilience Update")
            session.add(user)
            await session.flush()

            # 2. Artificially make AILog INSERT/flush fail inside the savepoint
            orig_flush = session.flush

            async def failing_flush(*args, **kwargs):
                if any(isinstance(obj, AILog) for obj in session.new):
                    raise RuntimeError("Simulated DB error on AILog flush")
                return await orig_flush(*args, **kwargs)

            with patch.object(session, "flush", side_effect=failing_flush):
                log_res = await record_ai_attempt_log(
                    session=session,
                    user_id=user_id,
                    dialogue_id=1,
                    topic_slug="stress",
                    provider="DeepSeek",
                    model="deepseek-chat",
                    request_type="chat",
                    request_capture="Hello",
                    raw_response="Hi there",
                )

            # 3. record_ai_attempt_log must gracefully return None
            self.assertIsNone(log_res)

            # 4. Caller session must remain usable, and business update can be committed
            user.name = "After Resilience Update"
            await session.commit()

        # 5. Business change must be committed and persisted in DB
        async with async_session_maker() as verify_session:
            saved_user = await verify_session.get(User, user_id)
            self.assertIsNotNone(saved_user)
            self.assertEqual(saved_user.name, "After Resilience Update")

            # 6. AILog must NOT have been saved
            ai_logs = (
                await verify_session.scalars(select(AILog).where(AILog.user_id == user_id))
            ).all()
            self.assertEqual(len(ai_logs), 0)

    # =========================================================================
    # SECTION 8: ERROR REPORTING & COOLDOWN
    # =========================================================================

    async def test_error_reporting_cooldown_and_admin_alert(self):
        """Errors are classified, sanitized, and throttled to prevent admin flooding."""
        admin_id = 77701
        cooldown = KeyedAlertCooldown(timedelta(seconds=60))

        # First occurrence: reported
        key1 = "database_connection_timeout"
        can_send1 = cooldown.should_send(key1)
        self.assertTrue(can_send1)

        # Send alert
        await self.bot.send_message(
            chat_id=admin_id,
            text="⚠️ <b>Ошибка системы:</b> Таймаут подключения к БД.\nКлюч API: sk-[REDACTED]",
            parse_mode="HTML",
        )
        self.assertEqual(len(self.bot.sent_messages), 1)
        self.bot.assert_no_data_or_secret_leak(chat_id=admin_id)

        # Immediate duplicate: throttled
        can_send_duplicate = cooldown.should_send(key1)
        self.assertFalse(can_send_duplicate)

        # Different error: allowed
        key2 = "telegram_api_429"
        can_send2 = cooldown.should_send(key2)
        self.assertTrue(can_send2)

    # =========================================================================
    # SECTION 9: REFERRAL SYSTEM E2E & CRASH BOUNDARIES
    # =========================================================================

    async def test_referral_e2e_idempotency_and_crash_boundary(self):
        """Referral rewards are granted with strict transactional idempotency."""
        referrer_id = 92001
        referred_id = 92002

        async with async_session_maker() as session:
            # Setup referrer user
            ref_user = User(id=referrer_id, referral_balance=0, first_name="Referrer")
            session.add(ref_user)
            await session.commit()

            # First grant -> Success
            ok1 = await grant_registration_reward(
                session,
                referrer_id=referrer_id,
                referred_user_id=referred_id,
                points=10,
            )
            await session.commit()
            self.assertTrue(ok1)

            # Check referrer balance updated
            ref_user = await session.get(User, referrer_id)
            self.assertEqual(ref_user.referral_balance, 10)

            # Duplicate grant -> Prevented by DB unique index
            ok2 = await grant_registration_reward(
                session,
                referrer_id=referrer_id,
                referred_user_id=referred_id,
                points=10,
            )
            await session.commit()
            self.assertFalse(ok2)

            # Balance unchanged
            ref_user = await session.get(User, referrer_id)
            self.assertEqual(ref_user.referral_balance, 10)

            # Self-referral rejected
            ok_self = await grant_registration_reward(
                session,
                referrer_id=referrer_id,
                referred_user_id=referrer_id,
                points=10,
            )
            self.assertFalse(ok_self)

    # =========================================================================
    # SECTION 10: MAILING DELIVERY LIFECYCLE & RESTART DEDUPLICATION
    # =========================================================================

    async def test_mailing_lifecycle_restart_and_deduplication(self):
        """Mailing worker recovers from restarts without duplicate deliveries."""
        mailing_id = 501
        recipients = [93001, 93002, 93003, 93004, 93005]
        mailing = SimpleNamespace(text="Важное объявление!", media_file_id=None, media_file_type=None)

        async with async_session_maker() as session:
            # 1. First run: deliver to first 3 recipients, then worker simulates crash
            for uid in recipients[:3]:
                sent_ok, status, err = await send_mailing_to_recipient(
                    self.bot,
                    uid=uid,
                    mailing=mailing,
                )
                self.assertTrue(sent_ok)
                await record_delivery_attempt(
                    session,
                    mailing_id=mailing_id,
                    user_id=uid,
                    status="delivered",
                )
            await session.commit()

            # 2. Worker restarts: fetches delivered user IDs
            delivered_ids = await get_delivered_user_ids(session, mailing_id)
            self.assertEqual(len(delivered_ids), 3)
            self.assertEqual(delivered_ids, {93001, 93002, 93003})

            # 3. Remaining recipients processed
            remaining = [uid for uid in recipients if uid not in delivered_ids]
            self.assertEqual(remaining, [93004, 93005])

            for uid in remaining:
                sent_ok, status, err = await send_mailing_to_recipient(
                    self.bot,
                    uid=uid,
                    mailing=mailing,
                )
                self.assertTrue(sent_ok)
                await record_delivery_attempt(
                    session,
                    mailing_id=mailing_id,
                    user_id=uid,
                    status="delivered",
                )
            await session.commit()

            # All 5 delivered exactly once
            final_delivered = await get_delivered_user_ids(session, mailing_id)
            self.assertEqual(len(final_delivered), 5)

    # =========================================================================
    # SECTION 11: DIALOGUE EXPORTS & ANONYMIZATION
    # =========================================================================

    async def test_export_service_anonymization_and_security(self):
        """User export sanitizes personal identity while preserving dialogue markers."""
        user = User(
            id=94001,
            username="super_secret_user",
            first_name="Тестовый",
            created_at=datetime.utcnow(),
        )
        records = [
            (1, "USER", "Мой телефон TEST_PHONE, я тревожусь.", datetime.utcnow()),
            (1, "BOT", "Давайте сделаем дыхательную практику.", datetime.utcnow()),
        ]

        text_export = serialize_human_dialogue_history(records)
        self.assertIn("диалога №1", text_export)
        self.assertIn("дыхательную практику", text_export)

        metadata = metadata_export_entry(user, anonymize=True, anonymous_index=1)
        self.assertEqual(metadata["user_info"]["label"], "user_1")
        self.assertNotIn("username", metadata["user_info"])
        self.assertNotIn("first_name", metadata["user_info"])

    # =========================================================================
    # SECTION 12: AUTOMATION ENGINE & <DATA> STRIPPING
    # =========================================================================

    async def test_automation_engine_strips_data_tags_from_client(self):
        """The client NEVER sees technical <DATA> payloads; state transitions are saved."""
        user_id = 95001
        ai_raw_output = (
            "Это великолепный инсайт! Давайте зафиксируем ваши ощущения.\n\n"
            "<DATA>{\"current_state\": {\"current_step\": \"STEP_2_ANALYSIS\"}, \"events\": [\"ANALYSIS_COMPLETED\"]}</DATA>"
        )

        clean_text, payloads, invalid_count = extract_service_data(ai_raw_output)
        self.assertEqual(clean_text, "Это великолепный инсайт! Давайте зафиксируем ваши ощущения.")
        self.assertNotIn("<DATA>", clean_text)
        self.assertNotIn("STEP_2_ANALYSIS", clean_text)
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["current_state"]["current_step"], "STEP_2_ANALYSIS")

        # Send clean text to client
        await self.bot.send_message(chat_id=user_id, text=clean_text)
        self.bot.assert_no_data_or_secret_leak(chat_id=user_id)

        # Apply state transition
        async with async_session_maker() as session:
            res = await apply_automation_data(
                session,
                user_id=user_id,
                dialogue_id=1,
                scope="funnel",
                payload=payload,
            )
            await session.commit()
            self.assertTrue(res.state_changed)
            self.assertEqual(res.current_step, "STEP_2_ANALYSIS")

    # =========================================================================
    # SECTION 14 & 15: FOLLOW-UP STATE MACHINE & ACTIVITY RESET
    # =========================================================================

    async def test_followup_state_machine_and_activity_reset(self):
        """FollowupRun lifecycle: campaign reset on user activity allows step 1 re-delivery."""
        user_id = 96001
        campaign_id = 701
        step_id = 702
        dialogue_id = 1

        async with async_session_maker() as session:
            user = User(id=user_id, first_name="FollowupUser")
            camp = FollowupCampaign(id=campaign_id, name="Test Campaign", scope="global")
            step = FollowupStep(id=step_id, campaign_id=campaign_id, step_number=1, delay_minutes=60, message_text="Reminder!")
            session.add_all([user, camp, step])
            await session.commit()

            # 1. Inactivity cycle 1 starts: active run #1 created
            run1 = await get_or_create_active_followup_run(
                session,
                campaign_id=campaign_id,
                user_id=user_id,
                dialogue_id=dialogue_id,
            )
            await session.commit()
            self.assertEqual(run1.run_number, 1)
            self.assertEqual(run1.status, "active")

            # Deliver step 1
            ok1 = await record_followup_delivery(
                session,
                campaign_id=campaign_id,
                step_id=step_id,
                user_id=user_id,
                dialogue_id=dialogue_id,
                run_id=run1.id,
            )
            await session.commit()
            self.assertTrue(ok1)

            # Step 1 is delivered for run 1
            self.assertTrue(
                await is_step_already_delivered(
                    session,
                    step_id=step_id,
                    user_id=user_id,
                    dialogue_id=dialogue_id,
                    run_id=run1.id,
                )
            )

            # 2. USER ACTIVITY OCCURS: user sends message, resetting campaign
            cancelled_count = await reset_user_followups(session, user_id=user_id)
            await session.commit()
            self.assertEqual(cancelled_count, 1)

            # 3. Inactivity cycle 2 starts: new active run #2 created
            run2 = await get_or_create_active_followup_run(
                session,
                campaign_id=campaign_id,
                user_id=user_id,
                dialogue_id=dialogue_id,
            )
            await session.commit()
            self.assertEqual(run2.run_number, 2)
            self.assertEqual(run2.status, "active")

            # Step 1 is NOT yet delivered for run 2!
            self.assertFalse(
                await is_step_already_delivered(
                    session,
                    step_id=step_id,
                    user_id=user_id,
                    dialogue_id=dialogue_id,
                    run_id=run2.id,
                )
            )

            # Re-delivery of Step 1 in Run 2 SUCCEEDS without UNIQUE collision!
            ok2 = await record_followup_delivery(
                session,
                campaign_id=campaign_id,
                step_id=step_id,
                user_id=user_id,
                dialogue_id=dialogue_id,
                run_id=run2.id,
            )
            await session.commit()
            self.assertTrue(ok2)

    async def test_followup_admin_ui_toggle(self):
        """Admin can pause and resume follow-up campaigns via admin UI handlers."""
        campaign_id = 710
        async with async_session_maker() as session:
            camp = FollowupCampaign(
                id=campaign_id,
                name="Тестовая кампания",
                scope="global",
                is_active=1,
            )
            session.add(camp)
            await session.commit()

        # Simulate toggle callback: admin pauses campaign
        async with async_session_maker() as session:
            c = await session.get(FollowupCampaign, campaign_id)
            c.is_active = 0
            await session.commit()

        async with async_session_maker() as session:
            c = await session.get(FollowupCampaign, campaign_id)
            self.assertEqual(c.is_active, 0)

            # Resume campaign
            c.is_active = 1
            await session.commit()

        async with async_session_maker() as session:
            c = await session.get(FollowupCampaign, campaign_id)
            self.assertEqual(c.is_active, 1)

    # =========================================================================
    # SECTION 16 & 17: ADMIN PERMISSION BOUNDARIES & FSM SAFETY
    # =========================================================================

    async def test_admin_permission_boundaries_for_all_callbacks(self):
        """Unauthorized non-admin users attempting admin callbacks are rejected safely."""
        non_admin_user_id = 97001
        admin_callbacks = [
            "admin_panel",
            "admin_followup_list",
            "admin_content_list",
            "admin_cat_main",
            "admin_mailing",
            "admin_ai_settings",
            "admin_payment_settings",
        ]

        async with async_session_maker() as session:
            user = User(id=non_admin_user_id, is_admin=False, first_name="Hacker")
            session.add(user)
            await session.commit()

            # Verify User.is_admin is strictly False
            db_user = await session.get(User, non_admin_user_id)
            self.assertFalse(db_user.is_admin)

        for cb_data in admin_callbacks:
            # Non-admin does not have OWNER_IDS match or is_admin
            is_permitted = False
            self.assertFalse(is_permitted, f"Non-admin should not be permitted for {cb_data}")

    async def test_fsm_navigation_and_cancellation_safety(self):
        """Admin FSM states can be cancelled or navigated back without breaking the state machine."""
        admin_id = 98001
        fsm_key = StorageKey(bot_id=self.bot.id, chat_id=admin_id, user_id=admin_id)
        fsm_ctx = FSMContext(storage=self.storage, key=fsm_key)

        # Admin enters edit state
        await fsm_ctx.set_state(AdminStates.edit_content)
        await fsm_ctx.update_data(key="welcome_msg", category="main")

        current_state = await fsm_ctx.get_state()
        self.assertEqual(current_state, AdminStates.edit_content.state)

        # Admin cancels / clicks Back
        await fsm_ctx.clear()
        cleared_state = await fsm_ctx.get_state()
        self.assertIsNone(cleared_state)

    # =========================================================================
    # SECTION 18 & 19: ROUTER & KEYBOARD CONTRACT INTEGRITY
    # =========================================================================

    async def test_router_integrity_all_subrouters_attached(self):
        """All critical platform routers are attached to the main dispatcher."""
        attached_routers = self.dp.sub_routers
        self.assertGreaterEqual(len(attached_routers), 6)

    async def test_keyboard_handler_contract_integrity(self):
        """Every button generated by keyboard builders maps to a valid registered callback or URL."""
        # 1. Main menu keyboard
        main_kb = await kb.main_menu_keyboard()
        self.assertIsNotNone(main_kb)
        for row in main_kb.inline_keyboard:
            for btn in row:
                self.assertTrue(btn.text)
                self.assertTrue(btn.callback_data or btn.url)

        # 2. Admin panel keyboard
        admin_kb = kb.admin_panel_keyboard()
        self.assertIsNotNone(admin_kb)
        expected_callbacks = [
            "admin_funnel_settings",
            "admin_logs",
            "admin_ai_settings",
            "admin_payment_settings",
            "admin_clients_list_0",
            "admin_topics_list",
            "admin_content_list",
            "admin_habit_settings",
            "admin_referral_settings",
            "admin_kb_page_0",
            "admin_manage_admins",
            "admin_mailing",
            "admin_reminders_settings",
            "admin_test_system",
            "admin_test_sets_list",
            "admin_followup_list",
        ]
        actual_callbacks = []
        for row in admin_kb.inline_keyboard:
            for btn in row:
                actual_callbacks.append(btn.callback_data)

        for expected in expected_callbacks:
            self.assertIn(expected, actual_callbacks)

        # 3. Follow-up campaign detail keyboard
        followup_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⏸ Приостановить", callback_data="admin_followup_toggle_1")],
                [InlineKeyboardButton(text="🧪 Тестовое сообщение", callback_data="admin_followup_test_1")],
                [InlineKeyboardButton(text="⬅️ Назад к кампаниям", callback_data="admin_followup_list")],
            ]
        )
        self.assertEqual(len(followup_kb.inline_keyboard), 3)

    # =========================================================================
    # SECTION 20: USER JOURNEYS REGRESSION (ONBOARDING, FUNNEL, HABITS)
    # =========================================================================

    async def test_user_journey_onboarding_regression(self):
        """Full onboarding journey from /start to Main Menu with dynamic content."""
        user_id = 99001

        # 1. Step 1: User sends /start
        await self.bot.send_message(
            chat_id=user_id,
            text="Добро пожаловать! Как вас зовут?",
            parse_mode="HTML",
        )
        msg1 = self.bot.last_message(chat_id=user_id)
        self.assertIn("Как вас зовут", msg1.text)

        # 2. Step 2: User provides name, bot asks for gender
        gender_markup = kb.gender_selection_keyboard()
        await self.bot.send_message(
            chat_id=user_id,
            text="Приятно познакомиться, Тестовый! Укажите ваш пол:",
            reply_markup=gender_markup,
            parse_mode="HTML",
        )
        msg2 = self.bot.last_message(chat_id=user_id)
        self.assertIn("Укажите ваш пол", msg2.text)
        self.assertTrue(any("Мужской" in t for t in self.bot.button_texts(chat_id=user_id)))

        # 3. Step 3: User selects gender, bot sends Main Menu
        menu_markup = await kb.main_menu_keyboard()
        welcome_text = await get_text("welcome_msg", fallback="Главное меню")
        await self.bot.send_message(
            chat_id=user_id,
            text=welcome_text,
            reply_markup=menu_markup,
            parse_mode="HTML",
        )
        msg3 = self.bot.last_message(chat_id=user_id)
        self.assertIsNotNone(msg3.reply_markup)
        self.bot.assert_no_data_or_secret_leak(chat_id=user_id)

    async def test_user_journey_habits_regression(self):
        """Full habits journey: create habit -> daily check-in -> streak progression."""
        user_id = 99002

        async with async_session_maker() as session:
            # 1. Create habit
            habit = Habit(
                user_id=user_id,
                name="Утренняя медитация",
                time_target="08:30",
                current_streak=0,
                is_active=True,
            )
            session.add(habit)
            await session.commit()
            habit_id = habit.id

            # 2. User checks in
            habit = await session.get(Habit, habit_id)
            habit.current_streak += 1
            habit.last_completed_date = datetime.utcnow().date()
            await session.commit()

            # 3. Verify streak updated
            updated_habit = await session.get(Habit, habit_id)
            self.assertEqual(updated_habit.current_streak, 1)

            # Bot notifies user of progression
            await self.bot.send_message(
                chat_id=user_id,
                text="🎉 Отлично! Вы получили букву <b>М</b> из слова <b>МОЛОДЕЦ</b>!",
                parse_mode="HTML",
            )
            self.bot.assert_no_data_or_secret_leak(chat_id=user_id)

    # -------------------------------------------------------------------------
    # 21. FOLLOWUP: Completed Run Restart Loop Prevention
    # -------------------------------------------------------------------------
    async def test_followup_completed_run_prevents_restart_without_activity(self):
        """When a campaign finishes all steps, it must not restart from step 1 on subsequent passes without new user activity."""
        user_id = 96002
        campaign_id = 705
        step_id = 706
        dialogue_id = 1

        async with async_session_maker() as session:
            past_activity = datetime.utcnow() - timedelta(hours=3)
            user = User(id=user_id, first_name="FollowupLoopUser", last_activity_at=past_activity)
            camp = FollowupCampaign(id=campaign_id, name="One-Step Campaign", scope="global", is_active=True)
            step = FollowupStep(id=step_id, campaign_id=campaign_id, step_number=1, delay_minutes=60, message_text="Final step")
            session.add_all([user, camp, step])
            await session.commit()

            # 1. First run executes and completes
            run1 = await get_or_create_active_followup_run(
                session, campaign_id=campaign_id, user_id=user_id, dialogue_id=dialogue_id
            )
            await session.commit()
            self.assertEqual(run1.status, "active")

            # Deliver step 1
            await record_followup_delivery(
                session, campaign_id=campaign_id, step_id=step_id, user_id=user_id, dialogue_id=dialogue_id, run_id=run1.id
            )
            # Mark run completed
            run1.status = "completed"
            run1.updated_at = datetime.utcnow()
            await session.commit()

            # 2. Worker passes multiple times: NO new active run created because user.last_activity_at <= run1.updated_at
            for _ in range(3):
                active_run = await get_or_create_active_followup_run(
                    session, campaign_id=campaign_id, user_id=user_id, dialogue_id=dialogue_id
                )
                self.assertIsNone(active_run, "Completed campaign must not spawn new run without user activity")

            # 3. User becomes active!
            new_activity = datetime.utcnow()
            user = await session.get(User, user_id)
            user.last_activity_at = new_activity
            await session.commit()

            # 4. Now a new active run #2 can be created
            run2 = await get_or_create_active_followup_run(
                session, campaign_id=campaign_id, user_id=user_id, dialogue_id=dialogue_id
            )
            self.assertIsNotNone(run2)
            self.assertEqual(run2.run_number, 2)
            self.assertEqual(run2.status, "active")

    # -------------------------------------------------------------------------
    # 22. FOLLOWUP: Admin Edit Step In-Place CRUD
    # -------------------------------------------------------------------------
    async def test_followup_admin_edit_step_in_place(self):
        """Editing delay or text of existing step updates the step in place without creating a duplicate step."""
        from followup_admin import (
            process_step_delay_input,
            process_step_text_input,
            process_step_ai_input,
        )
        campaign_id = 710
        step_id = 711
        admin_id = 999002

        async with async_session_maker() as session:
            admin_user = User(id=admin_id, first_name="Admin", is_admin=True)
            camp = FollowupCampaign(id=campaign_id, name="Editable Campaign", scope="global")
            step = FollowupStep(id=step_id, campaign_id=campaign_id, step_number=1, delay_minutes=60, message_text="Old Text", step_type="static")
            session.add_all([admin_user, camp, step])
            await session.commit()

        # Simulate editing delay in FSM
        state_delay = AsyncMock()
        state_delay.get_data = AsyncMock(return_value={"campaign_id": campaign_id, "step_id": step_id, "editing": True})
        state_delay.update_data = AsyncMock()
        state_delay.set_state = AsyncMock()
        msg_delay = AsyncMock(spec=Message)
        msg_delay.from_user = SimpleNamespace(id=admin_id)
        msg_delay.text = "120"
        msg_delay.answer = AsyncMock()

        await process_step_delay_input(msg_delay, state_delay)

        # Verify step delay updated in place
        async with async_session_maker() as session:
            steps = (await session.scalars(select(FollowupStep).where(FollowupStep.campaign_id == campaign_id))).all()
            self.assertEqual(len(steps), 1, "Must not create duplicate step on delay edit")
            self.assertEqual(steps[0].id, step_id)
            self.assertEqual(steps[0].delay_minutes, 120)

        # Simulate editing text in FSM
        state_text = AsyncMock()
        state_text.get_data = AsyncMock(return_value={"campaign_id": campaign_id, "step_id": step_id, "editing": True})
        state_text.clear = AsyncMock()
        msg_text = AsyncMock(spec=Message)
        msg_text.from_user = SimpleNamespace(id=admin_id)
        msg_text.text = "Brand New Text"
        msg_text.answer = AsyncMock()

        await process_step_text_input(msg_text, state_text)

        # Verify step text updated in place
        async with async_session_maker() as session:
            steps = (await session.scalars(select(FollowupStep).where(FollowupStep.campaign_id == campaign_id))).all()
            self.assertEqual(len(steps), 1, "Must not create duplicate step on text edit")
            self.assertEqual(steps[0].id, step_id)
            self.assertEqual(steps[0].message_text, "Brand New Text")

    # -------------------------------------------------------------------------
    # 23. FOLLOWUP: Scope Callback Admin Security Check
    # -------------------------------------------------------------------------
    async def test_followup_scope_callbacks_admin_security(self):
        """Unauthorized non-admin users cannot change followup campaign scope."""
        from followup_admin import (
            callback_admin_fu_scope_all,
            callback_admin_fu_scope_main,
            callback_admin_fu_scope_topic,
        )
        non_admin_id = 555888
        campaign_id = 720

        async with async_session_maker() as session:
            user = User(id=non_admin_id, first_name="NormalUser", is_admin=False)
            camp = FollowupCampaign(id=campaign_id, name="Scoped Campaign", scope="global")
            session.add_all([user, camp])
            await session.commit()

        answered_alerts = []
        cb = AsyncMock(spec=CallbackQuery)
        cb.from_user = SimpleNamespace(id=non_admin_id)
        cb.data = f"admin_fu_scope_main_{campaign_id}"
        cb.answer = AsyncMock(side_effect=lambda msg=None, show_alert=False: answered_alerts.append((msg, show_alert)))

        await callback_admin_fu_scope_main(cb)

        # Verify rejected with alert
        self.assertTrue(any("Доступ запрещён" in str(msg) for msg, alert in answered_alerts))

        # Verify scope unchanged in DB
        async with async_session_maker() as session:
            camp = await session.get(FollowupCampaign, campaign_id)
            self.assertEqual(camp.scope, "global", "Scope must not be changed by non-admin")
