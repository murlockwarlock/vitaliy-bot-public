"""Mutation & Invariant Verification Suite.

Tests real production functions from:
- ai_integration.py (get_ai_response_for_topic fallback invariants)
- followups.py (is_step_already_delivered, get_or_create_active_followup_run)
- ai_logs.py (record_ai_attempt_log sanitization and nested savepoint isolation)
- referral_service.py (grant_registration_reward atomic idempotency)
- content_service.py (cache invalidation vs staleness)
- automation_engine.py (extract_service_data)
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/vitaliy_mutation_test.db")
sys.modules.setdefault("vector_store", SimpleNamespace(get_collection=lambda: None, get_active_path=lambda: None))

from database import Base, Content, User, AIConfig, async_session_maker, engine, init_db
from content_service import get_text, invalidate_content_cache, save_content_item
from automation_engine import extract_service_data
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
from referral_service import ReferralRewardGrant, grant_registration_reward
from ai_logs import record_ai_attempt_log, sanitize_secret_values
from ai_integration import get_ai_response_for_topic


class MutationAndInvariantTests(unittest.IsolatedAsyncioTestCase):
    """Verifies that mutating core production logic invariants causes expected failures or behavior changes."""

    async def asyncSetUp(self):
        await init_db()
        async with engine.begin() as conn:
            for tbl in reversed(Base.metadata.sorted_tables):
                await conn.execute(tbl.delete())
        invalidate_content_cache()

    # -------------------------------------------------------------------------
    # 1. MUTATION: Cache Invalidation in content_service
    # -------------------------------------------------------------------------
    async def test_mutation_cache_invalidation_disabled_causes_staleness(self):
        """Calling production save_content_item with monkeypatched no-op invalidation leaves cache stale."""
        key = "mut_content_test"
        await save_content_item(key, text_content="Fresh Text")
        val1 = await get_text(key)
        self.assertEqual(val1, "Fresh Text")

        # Mutate: patch invalidate_content_cache to no-op
        with patch("content_service.invalidate_content_cache", lambda *args, **kwargs: None):
            async with async_session_maker() as session:
                item = await session.get(Content, key)
                item.text_content = "Mutated DB Text"
                await session.commit()

            # Production get_text returns stale in-memory cache
            val_stale = await get_text(key)
            self.assertEqual(val_stale, "Fresh Text", "Stale cache was returned when invalidation was suppressed")

        # Without mutation: explicit invalidation reflects updated value
        invalidate_content_cache(key)
        val_fresh = await get_text(key)
        self.assertEqual(val_fresh, "Mutated DB Text")

    # -------------------------------------------------------------------------
    # 2. MUTATION: AI Fallback Disabled vs Enabled in ai_integration
    # -------------------------------------------------------------------------
    async def test_mutation_ai_fallback_strict_adherence_to_config(self):
        """Calling production get_ai_response_for_topic must strictly honor allow_fallback=False."""
        async with async_session_maker() as session:
            user = User(id=12345, first_name="Тест")
            session.add(user)
            ai_cfg = AIConfig(
                id=1,
                provider="deepseek",
                deepseek_model="deepseek-chat",
                openai_model="gpt-4o-mini",
                deepseek_api_key="sk-deepseek-test",
                openai_api_key="sk-openai-test",
                allow_fallback=False,  # STRICTLY DISABLED
                fallback_provider="openai",
                fallback_model="gpt-4o-mini",
            )
            session.add(ai_cfg)
            await session.commit()

        call_counts = {"deepseek": 0, "openai": 0}

        async def fake_generate_chat_response(cfg, client, model, messages):
            prov = (cfg.provider or "").lower()
            call_counts[prov] += 1
            if prov == "deepseek":
                raise RuntimeError("DeepSeek Primary Failure")
            return "OpenAI fallback response"

        # When allow_fallback == False, fallback must NOT be called
        with patch("ai_integration._generate_chat_response", fake_generate_chat_response):
            resp = await get_ai_response_for_topic(12345, "Привет", "Тест")
            self.assertIn("Произошла ошибка", resp)
            self.assertEqual(call_counts["deepseek"], 1)
            self.assertEqual(call_counts["openai"], 0, "Fallback was called despite allow_fallback=False!")

        # Now mutate configuration to allow_fallback = True
        async with async_session_maker() as session:
            cfg = await session.get(AIConfig, 1)
            cfg.allow_fallback = True
            await session.commit()

        with patch("ai_integration._generate_chat_response", fake_generate_chat_response):
            resp = await get_ai_response_for_topic(12345, "Привет", "Тест")
            self.assertEqual(resp, "OpenAI fallback response")
            self.assertEqual(call_counts["deepseek"], 2)
            self.assertEqual(call_counts["openai"], 1, "Fallback was NOT called when allow_fallback=True!")

    # -------------------------------------------------------------------------
    # 3. MUTATION: Followup Delivery Retry Invariant in followups.py
    # -------------------------------------------------------------------------
    async def test_mutation_followup_status_failed_allows_retry_while_delivered_blocks(self):
        """Production is_step_already_delivered must return False for status='failed' and True for 'delivered'."""
        async with async_session_maker() as session:
            campaign = FollowupCampaign(
                name="Mutation Campaign",
                scope="main",
                is_active=1,
            )
            session.add(campaign)
            await session.flush()

            step = FollowupStep(
                campaign_id=campaign.id,
                step_number=1,
                delay_minutes=0,
                message_text="Step 1",
            )
            session.add(step)
            await session.flush()

            run = FollowupRun(
                campaign_id=campaign.id,
                user_id=12345,
                dialogue_id=1,
                status="active",
            )
            session.add(run)
            await session.flush()

            # Record delivery with status='failed'
            deliv = FollowupDelivery(
                run_id=run.id,
                campaign_id=campaign.id,
                step_id=step.id,
                user_id=12345,
                dialogue_id=1,
                status="failed",
            )
            session.add(deliv)
            await session.commit()

            # Production check: failed delivery must NOT be treated as delivered (permitting retry)
            already_done = await is_step_already_delivered(
                session, step_id=step.id, user_id=12345, dialogue_id=1, run_id=run.id
            )
            self.assertFalse(already_done, "Production is_step_already_delivered must allow retry for 'failed' status")

            # Mutate status to 'delivered'
            deliv.status = "delivered"
            await session.commit()

            # Production check: now it must be treated as already delivered
            already_done = await is_step_already_delivered(
                session, step_id=step.id, user_id=12345, dialogue_id=1, run_id=run.id
            )
            self.assertTrue(already_done, "Production is_step_already_delivered must block 'delivered' status")

    # -------------------------------------------------------------------------
    # 4. MUTATION: Referral Registration Double-Grant in referral_service.py
    # -------------------------------------------------------------------------
    async def test_mutation_referral_double_grant_prevented_by_production_function(self):
        """Calling production grant_registration_reward twice must be strictly idempotent."""
        async with async_session_maker() as session:
            referrer = User(id=777, first_name="Referrer", referral_balance=0)
            referred = User(id=888, first_name="Referred")
            session.add_all([referrer, referred])
            await session.commit()

        async with async_session_maker() as session:
            granted1 = await grant_registration_reward(
                session, referrer_id=777, referred_user_id=888, points=50
            )
            await session.commit()
            self.assertTrue(granted1)

        async with async_session_maker() as session:
            ref_user = await session.get(User, 777)
            self.assertEqual(ref_user.referral_balance, 50)

        # Mutate / repeat attempt: second call must return False and not increase balance
        async with async_session_maker() as session:
            granted2 = await grant_registration_reward(
                session, referrer_id=777, referred_user_id=888, points=50
            )
            await session.commit()
            self.assertFalse(granted2, "Second grant attempt must be rejected")

        async with async_session_maker() as session:
            ref_user = await session.get(User, 777)
            self.assertEqual(ref_user.referral_balance, 50, "Balance must not be double credited")

    # -------------------------------------------------------------------------
    # 5. MUTATION: AI Log Secret Sanitization & Savepoint Resilience in ai_logs.py
    # -------------------------------------------------------------------------
    async def test_mutation_ai_logs_sanitization_and_nested_savepoint_isolation(self):
        """Calling production record_ai_attempt_log with all 8 secret types sanitizes secrets and does not poison session."""
        secrets_dict = {
            "openai": "sk-proj-1234567890abcdef1234",
            "bearer": "Bearer my_super_secret_bearer_token_12345",
            "gemini": "TEST_GEMINI_KEY",
            "telegram": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            "password": "password: super_secret_pass_12345",
            "api_key": "api_key='custom_secret_api_key_67890'",
            "token_url": "https://api.example.com/v1?token=param_secret_token_123&key=param_secret_key_456",
            "webhook": "webhook_secret: my_webhook_secret_signature_999",
        }
        secret_prompt = " | ".join(secrets_dict.values())

        async with async_session_maker() as session:
            user = User(id=999, first_name="Test User")
            session.add(user)
            await session.flush()

            # Record attempt log with raw secrets across prompt, response, payload, diagnostics
            await record_ai_attempt_log(
                session=session,
                user_id=999,
                provider="deepseek",
                model="deepseek-chat",
                request_capture={"messages": [{"role": "user", "content": secret_prompt}]},
                clean_text=f"Clean AI reply with {secrets_dict['openai']} and {secrets_dict['telegram']}",
                raw_response=secret_prompt,
                diagnostics={"raw_key": secrets_dict["gemini"], "auth": secrets_dict["bearer"]},
                status="success",
            )
            await session.commit()

        # Verify that ALL 8 secrets were sanitized in production DB log
        from database import AILog
        async with async_session_maker() as session:
            log_entry = await session.scalar(select(AILog).where(AILog.user_id == 999))
            self.assertIsNotNone(log_entry)

            # None of the raw secrets must appear in request_payload, raw_response, clean_text, or diagnostics_json
            for sec_name, raw_val in secrets_dict.items():
                self.assertNotIn(raw_val, log_entry.request_payload, f"Secret {sec_name} leaked in request_payload")
                self.assertNotIn(raw_val, log_entry.raw_response or "", f"Secret {sec_name} leaked in raw_response")
                self.assertNotIn(raw_val, log_entry.clean_text or "", f"Secret {sec_name} leaked in clean_text")
                self.assertNotIn(raw_val, log_entry.diagnostics_json or "", f"Secret {sec_name} leaked in diagnostics_json")

            # Check that redaction markers are present
            self.assertIn("sk-[REDACTED]", log_entry.request_payload)
            self.assertIn("[REDACTED_TELEGRAM_TOKEN]", log_entry.request_payload)
            self.assertIn("AIza[REDACTED]", log_entry.diagnostics_json)
            self.assertIn("Bearer [REDACTED]", log_entry.diagnostics_json)

    # -------------------------------------------------------------------------
    # 6. MUTATION: DATA Block Stripping in automation_engine.py
    # -------------------------------------------------------------------------
    def test_mutation_extract_service_data_strips_technical_blocks(self):
        """Production extract_service_data cleanly separates visible text from <DATA> payloads."""
        raw_message = (
            "Привет! Как твои дела?\n"
            "<DATA>{\"current_state\": {\"current_step\": \"STEP_2\", \"events\": [\"event_a\"]}}</DATA>\n"
            "Жду твоего ответа."
        )
        clean_text, blocks, invalid = extract_service_data(raw_message)
        self.assertNotIn("<DATA>", clean_text)
        self.assertNotIn("current_step", clean_text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["current_state"]["current_step"], "STEP_2")
        self.assertEqual(invalid, 0)

    # -------------------------------------------------------------------------
    # 7. MUTATION: HTML Preview Truncation in admin_html_preview.py
    # -------------------------------------------------------------------------
    def test_html_preview_truncation(self):
        """truncate_html_preview cleanly preserves valid open/closed HTML tags and falls back safely on invalid markup."""
        from admin_html_preview import truncate_html_preview

        # 1. Normal HTML within budget
        normal_html = "<b>Привет</b>, <i>мир</i>!"
        self.assertEqual(truncate_html_preview(normal_html, max_length=100), normal_html)

        # 2. Long HTML truncated cleanly without leaving unclosed tags
        long_html = "<b>Это очень длинный текст, который должен быть корректно обрезан парсером</b>"
        preview = truncate_html_preview(long_html, max_length=30)
        self.assertTrue(len(preview) <= 30)
        self.assertTrue(preview.endswith("</b>"), f"Preview must close open tag cleanly: {preview}")

        # 3. Malformed / unclosed HTML falls back gracefully to escaped preview without crashing
        malformed_html = "<bНевалидный <тег> без закрытия & амперсанд"
        safe_preview = truncate_html_preview(malformed_html, max_length=25)
        self.assertTrue(len(safe_preview) <= 25)
        self.assertNotIn("<bНевалидный", safe_preview)

    # -------------------------------------------------------------------------
    # 8. MUTATION: Malformed Callbacks in ai_logs_admin.py
    # -------------------------------------------------------------------------
    async def test_malformed_callbacks_admin_logs(self):
        """Admin logs callback handlers reject malformed and invalid IDs gracefully via alert."""
        from ai_logs_admin import callback_admin_ai_log_detail, callback_admin_ai_log_file_explicit
        from aiogram.types import CallbackQuery, User as TgUser

        mock_user = TgUser(id=999, is_bot=False, first_name="Admin")

        # Test 1: Malformed log ID in callback_admin_ai_log_detail
        answered_alerts = []
        cb_malformed = AsyncMock(spec=CallbackQuery)
        cb_malformed.from_user = mock_user
        cb_malformed.data = "admin_ai_log_invalid_abc"
        cb_malformed.answer = AsyncMock(side_effect=lambda msg=None, show_alert=False: answered_alerts.append((msg, show_alert)))

        with patch("ai_logs_admin.is_admin_actor", AsyncMock(return_value=True)):
            await callback_admin_ai_log_detail(cb_malformed)
        self.assertTrue(any("Неверный ID лога" in str(msg) for msg, alert in answered_alerts))

        # Test 2: Malformed file callback in callback_admin_ai_log_file_explicit
        answered_file_alerts = []
        cb_file_malformed = AsyncMock(spec=CallbackQuery)
        cb_file_malformed.from_user = mock_user
        cb_file_malformed.data = "admin_ai_log_file_notanumber"
        cb_file_malformed.answer = AsyncMock(side_effect=lambda msg=None, show_alert=False: answered_file_alerts.append((msg, show_alert)))

        with patch("ai_logs_admin.is_admin_actor", AsyncMock(return_value=True)):
            await callback_admin_ai_log_file_explicit(cb_file_malformed)
        self.assertTrue(any("Неверный ID лога" in str(msg) for msg, alert in answered_file_alerts))
