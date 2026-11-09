import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/vitaliy_v7_test.db")
sys.modules.setdefault("vector_store", SimpleNamespace(get_collection=lambda: None, get_active_path=lambda: None))

from database import (
    Base,
    User,
    async_session_maker,
    engine,
    init_db,
)
from content_service import (
    get_content,
    get_text,
    invalidate_content_cache,
    save_content_item,
)
from admin_html_preview import truncate_html_preview
from error_reporting import (
    KeyedAlertCooldown,
    notify_admins_about_error,
    sanitize_secret_values,
)
from ai_logs import AILog, record_ai_attempt_log
from referral_service import (
    ReferralRewardGrant,
    grant_payment_bonus_days_reward,
    grant_payment_points_reward,
    grant_registration_reward,
)
from mailing_service import (
    MailingDeliveryLog,
    get_delivered_user_ids,
    record_delivery_attempt,
    send_mailing_to_recipient,
)
from export_service import (
    export_multiple_users_metadata,
    export_user_dialogue_history,
    metadata_export_entry,
    serialize_human_dialogue_history,
)
from automation_engine import (
    AutomationActionExecution,
    AutomationConversationState,
    AutomationHandler,
    AutomationStepTransition,
    apply_automation_data,
    extract_service_data,
    get_stage_analytics,
)
from automation_events import dispatch_automation_events
from followups import (
    FollowupCampaign,
    FollowupDelivery,
    FollowupStep,
    is_in_quiet_hours,
    is_step_already_delivered,
    record_followup_delivery,
    send_followup_step_to_user,
)


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

    async def send_photo(self, chat_id, photo, **kwargs):
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=len(self.sent_messages) + 1,
            photo=photo,
            kwargs=kwargs,
        )
        self.sent_messages.append(msg)
        return msg

    async def send_video(self, chat_id, video, **kwargs):
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=len(self.sent_messages) + 1,
            video=video,
            kwargs=kwargs,
        )
        self.sent_messages.append(msg)
        return msg


class PlatformArchitectureV7Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        async with engine.begin() as conn:
            for tbl in reversed(Base.metadata.sorted_tables):
                await conn.execute(tbl.delete())
        invalidate_content_cache()

    # =========================================================================
    # 1. Content Runtime & HTML Preview
    # =========================================================================
    async def test_content_db_priority_over_fallback(self):
        key = "test_custom_content_key"
        # 1. First read with fallback
        item = await get_content(key, fallback_text="Fallback Text", fallback_btn="Fallback Btn")
        self.assertEqual(item.text_content, "Fallback Text")

        # 2. Save custom text in DB
        await save_content_item(key, text_content="Custom DB Value")

        # 3. Read again: DB must win over fallback!
        item2 = await get_content(key, fallback_text="Fallback Text")
        self.assertEqual(item2.text_content, "Custom DB Value")

        # 4. Helper get_text
        text_val = await get_text(key, fallback="Fallback Text")
        self.assertEqual(text_val, "Custom DB Value")

    def test_truncate_html_preview(self):
        html_input = "<b>Жирный текст</b> и <i>курсив с <a href='https://example.com'>ссылкой</a></i>"
        preview = truncate_html_preview(html_input, 20)
        self.assertTrue(preview.endswith("…"))
        # Must not contain unclosed tags
        self.assertNotIn("<a", preview)

        # Unbalanced tags auto-escaping for safe rendering
        broken_html = "<b>Незакрытый жирный текст"
        fixed = truncate_html_preview(broken_html, 15)
        self.assertTrue(fixed.startswith("&lt;b&gt;"))

    # =========================================================================
    # 2. AI Structured Logs & Error Reporting
    # =========================================================================
    async def test_ai_logs_sanitizes_secrets(self):
        user_id = 99901
        secret_prompt = "Hello sk-1234567890abcdef12345678 and token: supersecretvalue"
        log_id = await record_ai_attempt_log(
            user_id=user_id,
            request_type="chat",
            provider="OpenAI",
            model="gpt-4o",
            status="success",
            latency_ms=120,
            prompt_summary=secret_prompt,
            clean_text="Clean answer",
            raw_response="Raw answer with Bearer eyJhbGciOi...",
        )
        self.assertIsNotNone(log_id)

        async with async_session_maker() as session:
            entry = await session.get(AILog, log_id)
            self.assertIsNotNone(entry)
            self.assertNotIn("sk-1234567890abcdef12345678", entry.prompt_summary)
            self.assertIn("[REDACTED]", entry.prompt_summary)
            self.assertNotIn("eyJhbGciOi...", entry.raw_response)

    def test_sanitize_secret_values(self):
        text = "key=AIzaSyA_testkey1234567890123&api_key: secret_api_key_123456"
        clean = sanitize_secret_values(text)
        self.assertNotIn("AIzaSyA_", clean)
        self.assertNotIn("secret_api_key_123456", clean)
        self.assertIn("[REDACTED]", clean)

    async def test_error_reporting_cooldown_dedup(self):
        bot = MockBot()
        cooldown = KeyedAlertCooldown(timedelta(minutes=10))

        # First alert sent
        sent1 = await notify_admins_about_error(
            bot,
            title="Тестовая ошибка",
            details="Ошибка соединения",
            stage="test_stage",
            provider="test_provider",
            cooldown=cooldown,
            cooldown_key="test_err_key",
        )
        self.assertTrue(sent1)

        # Immediate duplicate should be suppressed by cooldown
        sent2 = await notify_admins_about_error(
            bot,
            title="Тестовая ошибка",
            details="Ошибка соединения",
            stage="test_stage",
            provider="test_provider",
            cooldown=cooldown,
            cooldown_key="test_err_key",
        )
        self.assertFalse(sent2)

    # =========================================================================
    # 3. Referral Idempotency
    # =========================================================================
    async def test_referral_registration_reward_idempotency(self):
        referrer_id = 99910
        referee_id = 99911

        async with async_session_maker() as session:
            session.add(User(id=referrer_id, first_name="Boss", referral_balance=100))
            session.add(User(id=referee_id, first_name="Invited", referrer_id=referrer_id))
            await session.commit()

        # 1. First grant should succeed
        async with async_session_maker() as session:
            ok1 = await grant_registration_reward(
                session,
                referrer_id=referrer_id,
                referred_user_id=referee_id,
                points=50,
            )
            await session.commit()
        self.assertTrue(ok1)

        async with async_session_maker() as session:
            ref = await session.get(User, referrer_id)
            self.assertEqual(ref.referral_balance, 150)

        # 2. Second grant with SAME referrer and referee MUST be blocked!
        async with async_session_maker() as session:
            ok2 = await grant_registration_reward(
                session,
                referrer_id=referrer_id,
                referred_user_id=referee_id,
                points=50,
            )
            await session.commit()
        self.assertFalse(ok2)

        # Balance must remain 150, NOT 200
        async with async_session_maker() as session:
            ref = await session.get(User, referrer_id)
            self.assertEqual(ref.referral_balance, 150)

    async def test_referral_payment_reward_idempotency(self):
        referrer_id = 99920
        referee_id = 99921
        payment_key = "prod_inv_test_9999"

        async with async_session_maker() as session:
            session.add(User(id=referrer_id, first_name="RefBoss", referral_balance=0))
            session.add(User(id=referee_id, first_name="Client", referrer_id=referrer_id))
            await session.commit()

        # 1. First payment grant
        async with async_session_maker() as session:
            ok1 = await grant_payment_points_reward(
                session,
                referrer_id=referrer_id,
                referred_user_id=referee_id,
                payment_key=payment_key,
                points=300,
            )
            await session.commit()
        self.assertTrue(ok1)

        # 2. Duplicate webhook with same payment_key
        async with async_session_maker() as session:
            ok2 = await grant_payment_points_reward(
                session,
                referrer_id=referrer_id,
                referred_user_id=referee_id,
                payment_key=payment_key,
                points=300,
            )
            await session.commit()
        self.assertFalse(ok2)

        async with async_session_maker() as session:
            ref = await session.get(User, referrer_id)
            self.assertEqual(ref.referral_balance, 300)

    # =========================================================================
    # 4. Hardened Mailing
    # =========================================================================
    async def test_mailing_delivery_log_and_deduplication(self):
        mailing_id = 701
        user_id = 99930

        async with async_session_maker() as session:
            # First delivery attempt
            ok1 = await record_delivery_attempt(session, mailing_id, user_id, "delivered")
            await session.commit()
            self.assertTrue(ok1)

            # Check delivered IDs
            delivered = await get_delivered_user_ids(session, mailing_id)
            self.assertIn(user_id, delivered)

            # Second attempt for same mailing + user must be rejected by unique constraint
            ok2 = await record_delivery_attempt(session, mailing_id, user_id, "delivered")
            await session.commit()
            self.assertFalse(ok2)

    # =========================================================================
    # 5. Export Services
    # =========================================================================
    def test_serialize_human_dialogue_history(self):
        now = datetime(2026, 10, 5, 12, 0, 0)
        records = [
            (1, "user", "Привет, бот!", now),
            (1, "assistant", "Привет! Чем могу помочь?", now + timedelta(seconds=2)),
            (2, "user", "Начали новую тему.", now + timedelta(minutes=10)),
        ]
        text = serialize_human_dialogue_history(records)
        self.assertIn("--- Начало нового диалога №1 ---", text)
        self.assertIn("--- Начало нового диалога №2 ---", text)
        self.assertIn("[05.10.2026 12:00:00] USER: Привет, бот!", text)
        self.assertIn("[05.10.2026 12:00:02] ASSISTANT: Привет! Чем могу помочь?", text)

    def test_metadata_export_anonymization(self):
        user = User(
            id=12345678,
            username="real_username",
            first_name="Ivan",
            name="Тестовый Пользователь",
            is_admin=False,
            created_at=datetime(2026, 1, 1),
            last_activity_at=datetime(2026, 10, 5),
            referral_balance=50,
            referrer_id=999,
        )
        # 1. Non-anonymized
        entry = metadata_export_entry(user, anonymize=False)
        self.assertEqual(entry["user_info"]["username"], "real_username")
        self.assertEqual(entry["referral"]["referrer_id"], 999)

        # 2. Anonymized
        anon_entry = metadata_export_entry(user, anonymize=True, anonymous_index=5)
        self.assertEqual(anon_entry["user_info"]["label"], "user_5")
        self.assertNotIn("real_username", str(anon_entry))
        self.assertIsNone(anon_entry["referral"]["referrer_id"])

    # =========================================================================
    # 6. Automation Engine (<DATA>, Step Transitions, Events)
    # =========================================================================
    def test_extract_service_data(self):
        raw_msg = (
            "Спасибо за ответ!\n\n"
            "<DATA>\n"
            "{\n"
            '  "current_state": {"current_step": "MEDITATION_3_COMPLETED"},\n'
            '  "events": ["FUNNEL_STEP_FINISHED"],\n'
            '  "metadata": {"stress_level": 4}\n'
            "}\n"
            "</DATA>"
        )
        visible, blocks, invalid = extract_service_data(raw_msg)
        self.assertEqual(visible, "Спасибо за ответ!")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(invalid, 0)
        self.assertEqual(blocks[0]["current_state"]["current_step"], "MEDITATION_3_COMPLETED")
        self.assertEqual(blocks[0]["events"], ["FUNNEL_STEP_FINISHED"])

        # Corrupted JSON inside <DATA> must not crash and must be stripped from visible text
        corrupted = "Текст ответа.<DATA>{broken json content</DATA>"
        vis_c, blocks_c, inv_c = extract_service_data(corrupted)
        self.assertEqual(vis_c, "Текст ответа.")
        self.assertEqual(len(blocks_c), 0)
        self.assertEqual(inv_c, 1)

    async def test_apply_automation_data_and_stage_analytics(self):
        user_id = 99940
        async with async_session_maker() as session:
            # Step 1
            payload1 = {
                "current_state": {"current_step": "STEP_1_WELCOME"},
                "metadata": {"funnel_choice": "stress"},
            }
            res1 = await apply_automation_data(
                session,
                user_id=user_id,
                dialogue_id=1,
                scope="funnel",
                payload=payload1,
            )
            await session.commit()
            self.assertTrue(res1.state_changed)
            self.assertEqual(res1.current_step, "STEP_1_WELCOME")

            # Step 2
            payload2 = {
                "current_state": {"current_step": "STEP_2_PRACTICE"},
                "events": ["PRACTICE_STARTED"],
            }
            res2 = await apply_automation_data(
                session,
                user_id=user_id,
                dialogue_id=1,
                scope="funnel",
                payload=payload2,
            )
            await session.commit()
            self.assertTrue(res2.state_changed)
            self.assertEqual(res2.previous_step, "STEP_1_WELCOME")
            self.assertEqual(res2.current_step, "STEP_2_PRACTICE")
            self.assertEqual(res2.events, ("PRACTICE_STARTED",))
            # Metadata from step 1 preserved!
            self.assertEqual(res2.metadata.get("funnel_choice"), "stress")

            # Analytics check
            analytics = await get_stage_analytics(session, scope="funnel")
            self.assertGreaterEqual(analytics["total_active_stages"], 2)
            step_names = [s["step"] for s in analytics["stages"]]
            self.assertIn("STEP_1_WELCOME", step_names)
            self.assertIn("STEP_2_PRACTICE", step_names)

    async def test_dispatch_automation_events_idempotency(self):
        bot = MockBot()
        user_id = 99950

        async with async_session_maker() as session:
            handler = AutomationHandler(
                name="Onboarding Admin Alert",
                scope="global",
                event_name="ONBOARDING_DONE",
                action_type="send_admin_message",
                action_payload_json=json.dumps({"text": "User {user_id} completed onboarding!"}),
                is_active=1,
            )
            session.add(handler)
            await session.commit()

            # First dispatch: must execute action
            executed_1 = await dispatch_automation_events(
                bot,
                session,
                user_id=user_id,
                dialogue_id=1,
                scope="main",
                events=["ONBOARDING_DONE"],
            )
            await session.commit()
            self.assertIn(handler.id, executed_1)

            # Second duplicate dispatch (e.g. worker retry): must be idempotent!
            executed_2 = await dispatch_automation_events(
                bot,
                session,
                user_id=user_id,
                dialogue_id=1,
                scope="main",
                events=["ONBOARDING_DONE"],
            )
            await session.commit()
            self.assertEqual(executed_2, [])

    # =========================================================================
    # 7. Follow-up Campaigns
    # =========================================================================
    def test_quiet_hours(self):
        # 23:00 to 09:00 MSK
        dt_night = datetime(2026, 10, 5, 23, 30, 0)
        dt_morning = datetime(2026, 10, 5, 7, 0, 0)
        dt_day = datetime(2026, 10, 5, 14, 0, 0)

        self.assertTrue(is_in_quiet_hours(dt_night, 23, 9))
        self.assertTrue(is_in_quiet_hours(dt_morning, 23, 9))
        self.assertFalse(is_in_quiet_hours(dt_day, 23, 9))

    async def test_followup_delivery_idempotency(self):
        user_id = 99960
        campaign_id = 801
        step_id = 802
        dialogue_id = 1

        async with async_session_maker() as session:
            # Check not yet delivered
            self.assertFalse(
                await is_step_already_delivered(
                    session,
                    step_id=step_id,
                    user_id=user_id,
                    dialogue_id=dialogue_id,
                )
            )

            # Record delivery
            ok1 = await record_followup_delivery(
                session,
                campaign_id=campaign_id,
                step_id=step_id,
                user_id=user_id,
                dialogue_id=dialogue_id,
                status="delivered",
            )
            await session.commit()
            self.assertTrue(ok1)

            # Now must be reported as delivered
            self.assertTrue(
                await is_step_already_delivered(
                    session,
                    step_id=step_id,
                    user_id=user_id,
                    dialogue_id=dialogue_id,
                )
            )

            # Re-recording must fail due to unique constraint
            ok2 = await record_followup_delivery(
                session,
                campaign_id=campaign_id,
                step_id=step_id,
                user_id=user_id,
                dialogue_id=dialogue_id,
                status="delivered",
            )
            await session.commit()
            self.assertFalse(ok2)
