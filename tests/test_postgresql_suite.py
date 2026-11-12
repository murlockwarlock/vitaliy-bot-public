"""PostgreSQL Concurrency & Idempotency Integration Suite.

Validates concurrency primitives under real PostgreSQL MVCC, row-level locking (SELECT FOR UPDATE),
and database unique constraints without mocks.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from aiogram import Bot
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, Mailing, Topic, User, _ensure_postgresql_compat_schema
from automation_engine import (
    AutomationActionExecution,
    AutomationConversationState,
    AutomationHandler,
)
from automation_events import dispatch_automation_events
from followups import (
    FollowupCampaign,
    FollowupDelivery,
    FollowupRun,
    FollowupStep,
    is_in_quiet_hours,
    process_generic_followups,
)
from mailing_service import (
    MailingDeliveryLog,
    claim_mailing_recipient,
    record_delivery_attempt,
    send_mailing_to_recipient,
    update_delivery_status,
)
from referral_service import (
    ReferralRewardGrant,
    grant_payment_points_reward,
    grant_registration_reward,
)


pytestmark = pytest.mark.postgres_integration

POSTGRES_URL = os.getenv(
    "TEST_POSTGRES_DATABASE_URL",
    "postgresql+asyncpg://ivankorakin@localhost:5432/vitaliy_test_db",
)


class PostgreSQLConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """Concurrency verification under real PostgreSQL 18 MVCC."""

    async def asyncSetUp(self):
        self.engine = None
        try:
            self.engine = create_async_engine(
                POSTGRES_URL,
                connect_args={"timeout": 5},
                pool_size=30,
                max_overflow=20,
            )
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                # Ensure added columns and partial indexes exist in case tables were pre-created in test DB
                await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS journal_points INTEGER NOT NULL DEFAULT 0;"))
                await conn.execute(text("ALTER TABLE automation_action_executions ADD COLUMN IF NOT EXISTS error_message TEXT;"))
                await conn.execute(text("ALTER TABLE automation_action_executions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE;"))
                await conn.execute(text("ALTER TABLE automation_action_executions ADD COLUMN IF NOT EXISTS claim_token VARCHAR(64);"))
                await conn.execute(text("ALTER TABLE followup_deliveries ADD COLUMN IF NOT EXISTS claim_token VARCHAR(64);"))
                await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_active_followup_run ON followup_runs (campaign_id, user_id, dialogue_id) WHERE status = 'active';"))
            self.session_maker = async_sessionmaker(
                bind=self.engine, class_=AsyncSession, expire_on_commit=False
            )
        except Exception as exc:
            self.skipTest(
                f"PostgreSQL is NOT reachable at {POSTGRES_URL} ({exc}). "
                "Marked as UNVERIFIED PRODUCTION RISK: real PostgreSQL is required."
            )

    async def asyncTearDown(self):
        if self.engine:
            await self.engine.dispose()

    def _unique_id(self, base: int = 100000) -> int:
        return base + (int(uuid.uuid4().hex[:7], 16) % 800000)

    async def test_00_v3_postgresql_compat_schema_has_crisis_and_recipient_columns(self):
        async with self.engine.begin() as conn:
            await _ensure_postgresql_compat_schema(conn)
            result = await conn.execute(text(
                "SELECT table_name, column_name "
                "FROM information_schema.columns "
                "WHERE (table_name = 'users' AND column_name = 'crisis_detected_at') "
                "   OR (table_name = 'automation_handlers' AND column_name = 'recipient_ids_json')"
                "   OR (table_name = 'test_questions' AND column_name IN ('question_type', 'is_active'))"
                "   OR (table_name = 'test_sets' AND column_name = 'expected_question_count')"
                "   OR (table_name = 'journal_questions' AND column_name IN ('question_type', 'allow_custom_answer', 'answer_options_json'))"
                "   OR (table_name = 'journal_answers' AND column_name = 'is_skipped')"
            ))
            columns = {(row[0], row[1]) for row in result.fetchall()}

        self.assertIn(("users", "crisis_detected_at"), columns)
        self.assertIn(("automation_handlers", "recipient_ids_json"), columns)
        self.assertIn(("test_questions", "question_type"), columns)
        self.assertIn(("test_questions", "is_active"), columns)
        self.assertIn(("test_sets", "expected_question_count"), columns)
        self.assertIn(("journal_questions", "question_type"), columns)
        self.assertIn(("journal_questions", "allow_custom_answer"), columns)
        self.assertIn(("journal_questions", "answer_options_json"), columns)
        self.assertIn(("journal_answers", "is_skipped"), columns)

    async def _create_test_user(self, user_id: int, username: str) -> None:
        async with self.session_maker() as session:
            existing = await session.get(User, user_id)
            if not existing:
                u = User(
                    id=user_id,
                    username=username,
                    name=username,
                    referral_balance=0,
                    created_at=datetime.utcnow(),
                )
                session.add(u)
                await session.commit()

    async def test_01_concurrent_registration_grants_20_workers(self):
        """Scenario 1: 20 concurrent workers try to grant referral registration reward simultaneously.
        Guarantees that exactly 1 grant is committed and balance is credited only once.
        """
        ref_id = self._unique_id(1000000)
        new_user_id = self._unique_id(2000000)
        await self._create_test_user(ref_id, f"pg_ref_{ref_id}")
        await self._create_test_user(new_user_id, f"pg_new_{new_user_id}")

        async def worker():
            async with self.session_maker() as session:
                res = await grant_registration_reward(
                    session,
                    referrer_id=ref_id,
                    referred_user_id=new_user_id,
                    points=50,
                )
                if res:
                    await session.commit()
                else:
                    await session.rollback()
                return res

        results = await asyncio.gather(*[worker() for _ in range(20)])
        successful_grants = sum(1 for r in results if r is True)
        self.assertEqual(
            successful_grants,
            1,
            f"Expected exactly 1 grant out of 20 concurrent attempts, got {successful_grants}",
        )

        async with self.session_maker() as session:
            referrer = await session.get(User, ref_id)
            self.assertEqual(referrer.referral_balance, 50)
            grants_count = await session.scalar(
                select(func.count(ReferralRewardGrant.id)).where(
                    ReferralRewardGrant.referrer_id == ref_id,
                    ReferralRewardGrant.referred_user_id == new_user_id,
                )
            )
            self.assertEqual(grants_count, 1)

    async def test_02_concurrent_payment_settlement_20_workers(self):
        """Scenario 2: 20 concurrent tasks attempt to settle the same payment reward.
        Guarantees idempotency and exactly-once crediting.
        """
        ref_id = self._unique_id(3000000)
        buyer_id = self._unique_id(4000000)
        pay_key = f"pay_{uuid.uuid4().hex[:12]}"
        await self._create_test_user(ref_id, f"pg_ref_{ref_id}")
        await self._create_test_user(buyer_id, f"pg_buyer_{buyer_id}")

        async def worker():
            async with self.session_maker() as session:
                res = await grant_payment_points_reward(
                    session,
                    referrer_id=ref_id,
                    referred_user_id=buyer_id,
                    payment_key=pay_key,
                    points=150,
                )
                if res:
                    await session.commit()
                else:
                    await session.rollback()
                return res

        results = await asyncio.gather(*[worker() for _ in range(20)])
        successful_settlements = sum(1 for r in results if r is True)
        self.assertEqual(
            successful_settlements,
            1,
            f"Expected exactly 1 settlement out of 20 concurrent attempts, got {successful_settlements}",
        )

        async with self.session_maker() as session:
            referrer = await session.get(User, ref_id)
            self.assertEqual(referrer.referral_balance, 150)

    async def test_03_concurrent_mailing_recipient_claim_20_workers(self):
        """Scenario 3: 20 concurrent workers try to deliver/record the same mailing recipient.
        Guarantees that MailingDeliveryLog unique constraint deduplicates concurrent attempts.
        """
        user_id = self._unique_id(5000000)
        await self._create_test_user(user_id, f"pg_mail_{user_id}")

        async with self.session_maker() as session:
            m = Mailing(
                text="PostgreSQL Test Mailing",
                target_audience="all",
                status="in_progress",
                created_at=datetime.utcnow(),
            )
            session.add(m)
            await session.commit()
            mailing_id = m.id

        async def worker():
            async with self.session_maker() as session:
                res = await record_delivery_attempt(
                    session,
                    mailing_id=mailing_id,
                    user_id=user_id,
                    status="delivered",
                )
                if res:
                    await session.commit()
                else:
                    await session.rollback()
                return res

        results = await asyncio.gather(*[worker() for _ in range(20)])
        successful_records = sum(1 for r in results if r is True)
        self.assertEqual(
            successful_records,
            1,
            f"Expected exactly 1 delivery record out of 20 concurrent attempts, got {successful_records}",
        )

        async with self.session_maker() as session:
            count = await session.scalar(
                select(func.count(MailingDeliveryLog.id)).where(
                    MailingDeliveryLog.mailing_id == mailing_id,
                    MailingDeliveryLog.user_id == user_id,
                )
            )
            self.assertEqual(count, 1)

    async def test_04_concurrent_followup_run_delivery_claim_20_workers(self):
        """Scenario 4: 20 concurrent scheduler runs try to record follow-up step delivery.
        Guarantees that FollowupDelivery unique constraint (run_id, step_id) prevents duplicate sends.
        """
        user_id = self._unique_id(6000000)
        await self._create_test_user(user_id, f"pg_fu_{user_id}")

        async with self.session_maker() as session:
            c = FollowupCampaign(
                name="PostgreSQL Followup Campaign",
                scope="global",
                is_active=1,
            )
            session.add(c)
            await session.flush()

            s = FollowupStep(
                campaign_id=c.id,
                step_number=1,
                delay_minutes=30,
                message_text="Hello from followup!",
            )
            session.add(s)
            await session.flush()

            r = FollowupRun(
                campaign_id=c.id,
                user_id=user_id,
                run_number=1,
                status="active",
            )
            session.add(r)
            await session.commit()
            campaign_id, step_id, run_id = c.id, s.id, r.id

        async def worker():
            async with self.session_maker() as session:
                try:
                    delivery = FollowupDelivery(
                        run_id=run_id,
                        campaign_id=campaign_id,
                        step_id=step_id,
                        user_id=user_id,
                        status="delivered",
                    )
                    session.add(delivery)
                    await session.commit()
                    return True
                except IntegrityError:
                    await session.rollback()
                    return False

        results = await asyncio.gather(*[worker() for _ in range(20)])
        successful_claims = sum(1 for r in results if r is True)
        self.assertEqual(
            successful_claims,
            1,
            f"Expected exactly 1 followup delivery claim out of 20 concurrent attempts, got {successful_claims}",
        )

        async with self.session_maker() as session:
            count = await session.scalar(
                select(func.count(FollowupDelivery.id)).where(
                    FollowupDelivery.run_id == run_id,
                    FollowupDelivery.step_id == step_id,
                )
            )
            self.assertEqual(count, 1)

    async def test_05_concurrent_automation_execution_deduplication_20_workers(self):
        """Scenario 5: 20 concurrent event triggers try to execute the same automation action.
        Guarantees that AutomationActionExecution unique execution_key enforces deduplication.
        """
        user_id = self._unique_id(7000000)
        await self._create_test_user(user_id, f"pg_auto_{user_id}")

        async with self.session_maker() as session:
            h = AutomationHandler(
                name="Auto Handler PG",
                scope="global",
                action_type="send_user_message",
            )
            session.add(h)
            await session.commit()
            handler_id = h.id

        exec_key = f"exec_auto_{uuid.uuid4().hex[:12]}"

        async def worker():
            async with self.session_maker() as session:
                try:
                    execution = AutomationActionExecution(
                        execution_key=exec_key,
                        user_id=user_id,
                        handler_id=handler_id,
                        event_name="send_user_message",
                        status="executed",
                    )
                    session.add(execution)
                    await session.commit()
                    return True
                except IntegrityError:
                    await session.rollback()
                    return False

        results = await asyncio.gather(*[worker() for _ in range(20)])
        successful_executions = sum(1 for r in results if r is True)
        self.assertEqual(
            successful_executions,
            1,
            f"Expected exactly 1 automation execution out of 20 attempts, got {successful_executions}",
        )

        async with self.session_maker() as session:
            count = await session.scalar(
                select(func.count(AutomationActionExecution.id)).where(
                    AutomationActionExecution.execution_key == exec_key
                )
            )
            self.assertEqual(count, 1)

    async def test_06_concurrent_conversation_state_parallel_writes_20_workers(self):
        """Scenario 6: 20 concurrent tasks updating conversation state under PostgreSQL MVCC.
        Guarantees that concurrent writes across distinct topic/user partitions execute cleanly
        without deadlocks or data corruption.
        """
        base_user_id = self._unique_id(8000000)

        async def worker(idx: int):
            user_id = base_user_id + idx
            await self._create_test_user(user_id, f"pg_user_state_{idx}")
            async with self.session_maker() as session:
                state = AutomationConversationState(
                    user_id=user_id,
                    dialogue_id=1,
                    scope="topic",
                    topic_slug=f"topic_{idx}",
                    current_step=f"step_{idx}",
                    metadata_json=f'{{"worker": {idx}, "status": "active"}}',
                )
                session.add(state)
                await session.commit()

                # Verify read immediately
                saved = await session.scalar(
                    select(AutomationConversationState).where(
                        AutomationConversationState.user_id == user_id,
                        AutomationConversationState.topic_slug == f"topic_{idx}",
                    )
                )
                return saved.current_step == f"step_{idx}"

        results = await asyncio.gather(*[worker(i) for i in range(20)])
        self.assertTrue(all(results), "All 20 parallel state writes must succeed without deadlocks")

    async def test_07_concurrent_referral_balance_sum_20_different_referrals(self):
        """Scenario 7: 20 DIFFERENT referrals register under 1 referrer simultaneously.
        Guarantees that atomic SQL increment prevents lost updates and final balance == 200.
        """
        ref_id = self._unique_id(11000000)
        await self._create_test_user(ref_id, f"pg_ref_sum_{ref_id}")

        async def worker(idx: int):
            referred_id = self._unique_id(12000000 + idx * 50000)
            await self._create_test_user(referred_id, f"pg_new_sum_{referred_id}")
            async with self.session_maker() as session:
                res = await grant_registration_reward(
                    session,
                    referrer_id=ref_id,
                    referred_user_id=referred_id,
                    points=10,
                )
                if res:
                    await session.commit()
                else:
                    await session.rollback()
                return res

        results = await asyncio.gather(*[worker(i) for i in range(20)])
        self.assertEqual(sum(1 for r in results if r is True), 20)

        async with self.session_maker() as session:
            referrer = await session.get(User, ref_id)
            self.assertEqual(
                referrer.referral_balance,
                200,
                f"Expected exactly 200 balance from 20 referrals (10 points each), got {referrer.referral_balance}",
            )

    async def test_08_concurrent_generic_followups_fake_telegram_count_20_workers(self):
        """Scenario 8: 20 concurrent workers process generic followups.
        Guarantees that Fake Telegram mock counts total messages sent == 1.
        """
        user_id = self._unique_id(13000000)
        from time_utils import msk_now
        async with self.session_maker() as session:
            user = User(
                id=user_id,
                username=f"pg_fu_mock_{user_id}",
                name=f"pg_fu_mock_{user_id}",
                referral_balance=0,
                last_activity_at=msk_now() - timedelta(minutes=30),
                created_at=datetime.utcnow(),
            )
            session.add(user)
            await session.commit()

        scope = f"scope_{uuid.uuid4().hex[:8]}"
        async with self.session_maker() as session:
            campaign = FollowupCampaign(
                name="PG Concurrency Campaign",
                scope=scope,
                is_active=1,
                # This concurrency test must not depend on the wall clock.
                # Quiet-hour boundary behavior is covered separately below.
                quiet_hours_start=0,
                quiet_hours_end=0,
            )
            session.add(campaign)
            await session.flush()

            step = FollowupStep(
                campaign_id=campaign.id,
                step_number=1,
                delay_minutes=0,
                message_text="Hello from followup!",
            )
            session.add(step)
            await session.commit()

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()

        async def worker():
            await process_generic_followups(fake_bot, session_maker=self.session_maker, scope=scope, user_id=user_id)

        await asyncio.gather(*[worker() for _ in range(20)])

        self.assertEqual(
            fake_bot.send_message.call_count,
            1,
            f"Expected Fake Telegram to receive exactly 1 send_message call, got {fake_bot.send_message.call_count}",
        )

    async def test_followup_scope_global_sends_to_main_and_topic_users(self):
        """Global campaigns include inactive users in both main and topic dialogs."""
        from time_utils import msk_now

        main_user_id = self._unique_id(18100000)
        topic_user_id = self._unique_id(18200000)
        topic_slug = f"scope_global_{uuid.uuid4().hex[:8]}"
        now = msk_now()

        async with self.session_maker() as session:
            session.add(Topic(slug=topic_slug, name="Global scope test topic"))
            session.add_all([
                User(
                    id=main_user_id,
                    username=f"scope_global_main_{main_user_id}",
                    name="Global main user",
                    current_dialogue_id=1,
                    current_topic_slug=None,
                    last_activity_at=now - timedelta(hours=1),
                    created_at=now,
                ),
                User(
                    id=topic_user_id,
                    username=f"scope_global_topic_{topic_user_id}",
                    name="Global topic user",
                    current_dialogue_id=1,
                    current_topic_slug=topic_slug,
                    last_activity_at=now - timedelta(hours=1),
                    created_at=now,
                ),
            ])
            campaign = FollowupCampaign(
                name="Global scope regression",
                scope="global",
                is_active=1,
                quiet_hours_start=0,
                quiet_hours_end=0,
            )
            session.add(campaign)
            await session.flush()
            session.add(FollowupStep(
                campaign_id=campaign.id,
                step_number=1,
                delay_minutes=0,
                message_text="Global scope message",
            ))
            await session.commit()

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()
        await process_generic_followups(
            fake_bot,
            session_maker=self.session_maker,
            scope="global",
            user_id=main_user_id,
        )
        await process_generic_followups(
            fake_bot,
            session_maker=self.session_maker,
            scope="global",
            user_id=topic_user_id,
        )

        sent_ids = [call.args[0] for call in fake_bot.send_message.call_args_list]
        self.assertCountEqual(sent_ids, [main_user_id, topic_user_id])

    async def test_followup_scope_main_excludes_topic_users(self):
        """Main campaigns send only to users whose current topic is empty."""
        from time_utils import msk_now

        main_user_id = self._unique_id(18300000)
        topic_user_id = self._unique_id(18400000)
        topic_slug = f"scope_main_{uuid.uuid4().hex[:8]}"
        now = msk_now()

        async with self.session_maker() as session:
            session.add(Topic(slug=topic_slug, name="Main scope test topic"))
            session.add_all([
                User(
                    id=main_user_id,
                    username=f"scope_main_main_{main_user_id}",
                    name="Main dialog user",
                    current_dialogue_id=1,
                    current_topic_slug=None,
                    last_activity_at=now - timedelta(hours=1),
                    created_at=now,
                ),
                User(
                    id=topic_user_id,
                    username=f"scope_main_topic_{topic_user_id}",
                    name="Topic dialog user",
                    current_dialogue_id=1,
                    current_topic_slug=topic_slug,
                    last_activity_at=now - timedelta(hours=1),
                    created_at=now,
                ),
            ])
            campaign = FollowupCampaign(
                name="Main scope regression",
                scope="main",
                is_active=1,
                quiet_hours_start=0,
                quiet_hours_end=0,
            )
            session.add(campaign)
            await session.flush()
            session.add(FollowupStep(
                campaign_id=campaign.id,
                step_number=1,
                delay_minutes=0,
                message_text="Main scope message",
            ))
            await session.commit()

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()
        await process_generic_followups(
            fake_bot,
            session_maker=self.session_maker,
            scope="main",
            user_id=main_user_id,
        )
        await process_generic_followups(
            fake_bot,
            session_maker=self.session_maker,
            scope="main",
            user_id=topic_user_id,
        )

        sent_ids = [call.args[0] for call in fake_bot.send_message.call_args_list]
        self.assertEqual(sent_ids, [main_user_id])

    async def test_followup_scope_topic_sends_only_to_selected_topic(self):
        """Topic campaigns send only to users currently in the configured topic."""
        from time_utils import msk_now

        selected_user_id = self._unique_id(18500000)
        other_user_id = self._unique_id(18600000)
        selected_slug = f"scope_topic_selected_{uuid.uuid4().hex[:8]}"
        other_slug = f"scope_topic_other_{uuid.uuid4().hex[:8]}"
        now = msk_now()

        async with self.session_maker() as session:
            session.add_all([
                Topic(slug=selected_slug, name="Selected topic"),
                Topic(slug=other_slug, name="Other topic"),
                User(
                    id=selected_user_id,
                    username=f"scope_topic_selected_{selected_user_id}",
                    name="Selected topic user",
                    current_dialogue_id=1,
                    current_topic_slug=selected_slug,
                    last_activity_at=now - timedelta(hours=1),
                    created_at=now,
                ),
                User(
                    id=other_user_id,
                    username=f"scope_topic_other_{other_user_id}",
                    name="Other topic user",
                    current_dialogue_id=1,
                    current_topic_slug=other_slug,
                    last_activity_at=now - timedelta(hours=1),
                    created_at=now,
                ),
            ])
            campaign = FollowupCampaign(
                name="Topic scope regression",
                scope="topic",
                topic_slug=selected_slug,
                is_active=1,
                quiet_hours_start=0,
                quiet_hours_end=0,
            )
            session.add(campaign)
            await session.flush()
            session.add(FollowupStep(
                campaign_id=campaign.id,
                step_number=1,
                delay_minutes=0,
                message_text="Topic scope message",
            ))
            await session.commit()

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()
        await process_generic_followups(
            fake_bot,
            session_maker=self.session_maker,
            scope="topic",
            user_id=selected_user_id,
        )
        await process_generic_followups(
            fake_bot,
            session_maker=self.session_maker,
            scope="topic",
            user_id=other_user_id,
        )

        sent_ids = [call.args[0] for call in fake_bot.send_message.call_args_list]
        self.assertEqual(sent_ids, [selected_user_id])

    async def test_followup_scope_rechecks_topic_before_send_after_switch(self):
        """A user switching topics after selection is skipped before Telegram send."""
        from time_utils import msk_now
        import followups as followups_module

        user_id = self._unique_id(18700000)
        selected_slug = f"scope_switch_selected_{uuid.uuid4().hex[:8]}"
        switched_slug = f"scope_switch_other_{uuid.uuid4().hex[:8]}"
        now = msk_now()

        async with self.session_maker() as session:
            session.add_all([
                Topic(slug=selected_slug, name="Switch selected topic"),
                Topic(slug=switched_slug, name="Switch other topic"),
                User(
                    id=user_id,
                    username=f"scope_switch_{user_id}",
                    name="Switching topic user",
                    current_dialogue_id=1,
                    current_topic_slug=selected_slug,
                    last_activity_at=now - timedelta(hours=1),
                    created_at=now,
                ),
            ])
            campaign = FollowupCampaign(
                name="Topic switch regression",
                scope="topic",
                topic_slug=selected_slug,
                is_active=1,
                quiet_hours_start=0,
                quiet_hours_end=0,
            )
            session.add(campaign)
            await session.flush()
            session.add(FollowupStep(
                campaign_id=campaign.id,
                step_number=1,
                delay_minutes=0,
                message_text="Must not send after topic switch",
            ))
            await session.commit()

        original_get_or_create = followups_module.get_or_create_active_followup_run
        switched = False

        async def switch_topic_then_create_run(session, **kwargs):
            nonlocal switched
            if not switched:
                async with self.session_maker() as switch_session:
                    switched_user = await switch_session.get(User, user_id)
                    switched_user.current_topic_slug = switched_slug
                    await switch_session.commit()
                switched = True
            return await original_get_or_create(session, **kwargs)

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()
        with patch.object(
            followups_module,
            "get_or_create_active_followup_run",
            side_effect=switch_topic_then_create_run,
        ):
            delivered = await process_generic_followups(
                fake_bot,
                session_maker=self.session_maker,
                scope="topic",
                user_id=user_id,
            )

        self.assertEqual(delivered, 0)
        fake_bot.send_message.assert_not_awaited()

    async def test_09_concurrent_automation_event_dispatch_fake_telegram_count_20_workers(self):
        """Scenario 9: 20 concurrent event dispatches execute simultaneously.
        Guarantees that Fake Telegram mock counts total messages sent == 1.
        """
        user_id = self._unique_id(14000000)
        await self._create_test_user(user_id, f"pg_auto_mock_{user_id}")

        event_name = f"auto_event_{uuid.uuid4().hex[:8]}"
        async with self.session_maker() as session:
            handler = AutomationHandler(
                name="PG Concurrency Handler",
                scope="global",
                event_name=event_name,
                action_type="send_user_message",
                action_payload_json=json.dumps({"text": "Automation triggered!"}),
                is_active=1,
            )
            session.add(handler)
            await session.commit()

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()

        async def worker():
            async with self.session_maker() as session:
                await dispatch_automation_events(
                    fake_bot,
                    session,
                    user_id=user_id,
                    dialogue_id=1,
                    scope="global",
                    events=[event_name],
                )
                await session.commit()

        await asyncio.gather(*[worker() for _ in range(20)])

        self.assertEqual(
            fake_bot.send_message.call_count,
            1,
            f"Expected Fake Telegram to receive exactly 1 send_message call, got {fake_bot.send_message.call_count}",
        )

    async def test_10_concurrent_mailing_delivery_fake_telegram_count_20_workers(self):
        """Scenario 10: 20 concurrent workers try to deliver a mailing to the same recipient.
        Guarantees that Fake Telegram mock counts total messages sent == 1.
        """
        user_id = self._unique_id(15000000)
        await self._create_test_user(user_id, f"pg_mail_mock_{user_id}")

        async with self.session_maker() as session:
            mailing = Mailing(
                text="PG Concurrency Mailing",
                target_audience="all",
                status="sending",
                created_at=datetime.utcnow(),
            )
            session.add(mailing)
            await session.commit()
            mailing_id = mailing.id

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()

        async def worker():
            async with self.session_maker() as session:
                claimed = await claim_mailing_recipient(session, mailing_id, user_id)
                if not claimed:
                    return False
                await session.commit()

                ok, status_code, err_msg = await send_mailing_to_recipient(
                    fake_bot, user_id, mailing, session=session
                )
                await update_delivery_status(session, mailing_id, user_id, status_code, error=err_msg)
                await session.commit()
                return True

        results = await asyncio.gather(*[worker() for _ in range(20)])
        self.assertEqual(sum(1 for r in results if r is True), 1)
        self.assertEqual(
            fake_bot.send_message.call_count,
            1,
            f"Expected Fake Telegram to receive exactly 1 send_message call, got {fake_bot.send_message.call_count}",
        )

    async def test_11_concurrent_failed_followup_retry_fake_telegram_count_20_workers(self):
        """Scenario 11: 20 concurrent workers try to retry a failed delivery.
        Guarantees that CAS transitions status from 'failed' to 'delivering' with a unique claim_token,
        and Fake Telegram mock receives exactly 1 send_message call.
        """
        from time_utils import msk_now
        user_id = self._unique_id(16000000)
        async with self.session_maker() as session:
            user = User(
                id=user_id,
                username=f"pg_fu_failed_{user_id}",
                name=f"pg_fu_failed_{user_id}",
                referral_balance=0,
                last_activity_at=msk_now() - timedelta(minutes=30),
                created_at=datetime.utcnow(),
            )
            session.add(user)
            await session.commit()

        scope = f"scope_{uuid.uuid4().hex[:8]}"
        async with self.session_maker() as session:
            campaign = FollowupCampaign(
                name="PG Failed Retry Campaign",
                scope=scope,
                is_active=1,
                quiet_hours_start=0,
                quiet_hours_end=0,
            )
            session.add(campaign)
            await session.flush()

            step = FollowupStep(
                campaign_id=campaign.id,
                step_number=1,
                delay_minutes=0,
                message_text="Retry message!",
            )
            session.add(step)
            await session.flush()

            run = FollowupRun(
                campaign_id=campaign.id,
                user_id=user_id,
                dialogue_id=1,
                run_number=1,
                status="active",
            )
            session.add(run)
            await session.flush()

            # Pre-insert delivery in 'failed' status
            delivery = FollowupDelivery(
                campaign_id=campaign.id,
                step_id=step.id,
                user_id=user_id,
                dialogue_id=1,
                run_id=run.id,
                status="failed",
                delivered_at=msk_now() - timedelta(minutes=10),
            )
            session.add(delivery)
            await session.commit()

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()

        async def worker():
            await process_generic_followups(fake_bot, session_maker=self.session_maker, scope=scope, user_id=user_id)

        await asyncio.gather(*[worker() for _ in range(20)])

        self.assertEqual(
            fake_bot.send_message.call_count,
            1,
            f"Expected Fake Telegram to receive exactly 1 send_message call on failed retry, got {fake_bot.send_message.call_count}",
        )

        async with self.session_maker() as session:
            final_deliv = await session.scalar(
                select(FollowupDelivery).where(
                    FollowupDelivery.step_id == step.id,
                    FollowupDelivery.user_id == user_id,
                    FollowupDelivery.run_id == run.id,
                )
            )
            self.assertEqual(final_deliv.status, "delivered")

    async def test_12_concurrent_stale_delivering_followup_retry_20_workers(self):
        """Scenario 12: 20 concurrent workers encounter a delivery stuck in 'delivering' status for > 300s.
        Guarantees that CAS reclaims the stale lease and Fake Telegram receives exactly 1 send_message call.
        """
        from time_utils import msk_now
        user_id = self._unique_id(17000000)
        async with self.session_maker() as session:
            user = User(
                id=user_id,
                username=f"pg_fu_stale_{user_id}",
                name=f"pg_fu_stale_{user_id}",
                referral_balance=0,
                last_activity_at=msk_now() - timedelta(minutes=30),
                created_at=datetime.utcnow(),
            )
            session.add(user)
            await session.commit()

        scope = f"scope_{uuid.uuid4().hex[:8]}"
        async with self.session_maker() as session:
            campaign = FollowupCampaign(
                name="PG Stale Delivering Campaign",
                scope=scope,
                is_active=1,
                quiet_hours_start=0,
                quiet_hours_end=0,
            )
            session.add(campaign)
            await session.flush()

            step = FollowupStep(
                campaign_id=campaign.id,
                step_number=1,
                delay_minutes=0,
                message_text="Stale recovery message!",
            )
            session.add(step)
            await session.flush()

            run = FollowupRun(
                campaign_id=campaign.id,
                user_id=user_id,
                dialogue_id=1,
                run_number=1,
                status="active",
            )
            session.add(run)
            await session.flush()

            # Pre-insert delivery in 'delivering' status from 10 minutes ago with old token
            delivery = FollowupDelivery(
                campaign_id=campaign.id,
                step_id=step.id,
                user_id=user_id,
                dialogue_id=1,
                run_id=run.id,
                status="delivering",
                claim_token="old_crashed_token_123",
                delivered_at=msk_now() - timedelta(minutes=10),
            )
            session.add(delivery)
            await session.commit()

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()

        async def worker():
            await process_generic_followups(fake_bot, session_maker=self.session_maker, scope=scope, user_id=user_id)

        await asyncio.gather(*[worker() for _ in range(20)])

        self.assertEqual(
            fake_bot.send_message.call_count,
            1,
            f"Expected Fake Telegram to receive exactly 1 send_message call on stale delivering recovery, got {fake_bot.send_message.call_count}",
        )

        async with self.session_maker() as session:
            final_deliv = await session.scalar(
                select(FollowupDelivery).where(
                    FollowupDelivery.step_id == step.id,
                    FollowupDelivery.user_id == user_id,
                    FollowupDelivery.run_id == run.id,
                )
            )
            self.assertEqual(final_deliv.status, "delivered")
            self.assertNotEqual(final_deliv.claim_token, "old_crashed_token_123")

    async def test_13_quiet_hours_boundaries_block_and_release_followups(self):
        """Quiet hours block at 23:00 and release exactly at 09:00 MSK."""
        user_id = self._unique_id(17500000)
        at_0859 = datetime(2026, 10, 5, 8, 59, 0)
        at_0900 = datetime(2026, 10, 5, 9, 0, 0)
        at_2300 = datetime(2026, 10, 5, 23, 0, 0)

        self.assertTrue(is_in_quiet_hours(at_2300, 23, 9))
        self.assertTrue(is_in_quiet_hours(at_0859, 23, 9))
        self.assertFalse(is_in_quiet_hours(at_0900, 23, 9))

        async with self.session_maker() as session:
            session.add(User(
                id=user_id,
                username=f"pg_quiet_boundary_{user_id}",
                name=f"pg_quiet_boundary_{user_id}",
                referral_balance=0,
                last_activity_at=at_0900 - timedelta(minutes=30),
                created_at=datetime.utcnow(),
            ))
            await session.commit()

        scope = f"scope_{uuid.uuid4().hex[:8]}"
        async with self.session_maker() as session:
            campaign = FollowupCampaign(
                name="PG Quiet Boundary Campaign",
                scope=scope,
                is_active=1,
                quiet_hours_start=23,
                quiet_hours_end=9,
            )
            session.add(campaign)
            await session.flush()
            session.add(FollowupStep(
                campaign_id=campaign.id,
                step_number=1,
                delay_minutes=0,
                message_text="Quiet boundary message!",
            ))
            await session.commit()

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()

        with patch("followups.msk_now", return_value=at_0859):
            await process_generic_followups(
                fake_bot,
                session_maker=self.session_maker,
                scope=scope,
                user_id=user_id,
            )
        self.assertEqual(fake_bot.send_message.call_count, 0)

        with patch("followups.msk_now", return_value=at_2300):
            await process_generic_followups(
                fake_bot,
                session_maker=self.session_maker,
                scope=scope,
                user_id=user_id,
            )
        self.assertEqual(fake_bot.send_message.call_count, 0)

        with patch("followups.msk_now", return_value=at_0900):
            delivered = await process_generic_followups(
                fake_bot,
                session_maker=self.session_maker,
                scope=scope,
                user_id=user_id,
            )
        self.assertEqual(delivered, 1)
        self.assertEqual(fake_bot.send_message.call_count, 1)

    async def test_13_mailing_stale_delivering_recovery(self):
        """Scenario 13: A mailing recipient stuck in 'delivering' status for > 300s is reclaimed and delivered."""
        user_id = self._unique_id(18000000)
        await self._create_test_user(user_id, f"pg_mail_stale_{user_id}")

        async with self.session_maker() as session:
            mailing = Mailing(
                text="Stale Mailing Recovery",
                target_audience="all",
                status="sending",
                created_at=datetime.utcnow(),
            )
            session.add(mailing)
            await session.flush()
            mailing_id = mailing.id

            # Pre-insert recipient in 'delivering' status from 10 minutes ago
            stale_log = MailingDeliveryLog(
                mailing_id=mailing_id,
                user_id=user_id,
                status="delivering",
                sent_at=datetime.utcnow() - timedelta(seconds=600),
            )
            session.add(stale_log)
            await session.commit()

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()

        # Reclaim stale recipient
        async with self.session_maker() as session:
            claimed = await claim_mailing_recipient(session, mailing_id, user_id)
            self.assertTrue(claimed, "Stale delivering recipient should be reclaimed after lease expiry")
            await session.commit()

            ok, status_code, err_msg = await send_mailing_to_recipient(
                fake_bot, user_id, mailing, session=session
            )
            await update_delivery_status(session, mailing_id, user_id, status_code, error=err_msg)
            await session.commit()

        self.assertEqual(fake_bot.send_message.call_count, 1)

        async with self.session_maker() as session:
            final_log = await session.scalar(
                select(MailingDeliveryLog).where(
                    MailingDeliveryLog.mailing_id == mailing_id,
                    MailingDeliveryLog.user_id == user_id,
                )
            )
            self.assertEqual(final_log.status, "delivered")

    async def test_14_automation_crash_window_and_stale_recovery(self):
        """Scenario 14: An automation event action stuck in 'delivering' status for > 300s is recovered and delivered."""
        user_id = self._unique_id(19000000)
        await self._create_test_user(user_id, f"pg_auto_stale_{user_id}")

        event_name = f"stale_event_{uuid.uuid4().hex[:6]}"
        async with self.session_maker() as session:
            handler = AutomationHandler(
                name="Stale Recovery Handler",
                scope="global",
                event_name=event_name,
                action_type="send_user_message",
                action_payload_json=json.dumps({"text": "Recovered action message"}),
                is_active=1,
            )
            session.add(handler)
            await session.flush()
            handler_id = handler.id

            # Pre-insert execution in 'delivering' status from 10 minutes ago
            exec_key = f"auto-exec:{handler_id}:{user_id}:1:{event_name}"
            stale_exec = AutomationActionExecution(
                handler_id=handler_id,
                user_id=user_id,
                event_name=event_name,
                execution_key=exec_key,
                status="delivering",
                created_at=datetime.utcnow() - timedelta(seconds=600),
                updated_at=datetime.utcnow() - timedelta(seconds=600),
            )
            session.add(stale_exec)
            await session.commit()

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()

        async with self.session_maker() as session:
            await dispatch_automation_events(
                fake_bot,
                session,
                user_id=user_id,
                dialogue_id=1,
                scope="global",
                events=[event_name],
            )
            await session.commit()

        self.assertEqual(fake_bot.send_message.call_count, 1)

        async with self.session_maker() as session:
            final_exec = await session.scalar(
                select(AutomationActionExecution).where(
                    AutomationActionExecution.handler_id == handler_id,
                    AutomationActionExecution.user_id == user_id,
                )
            )
            self.assertEqual(final_exec.status, "delivered")

    async def test_15_concurrent_stale_automation_recovery(self):
        """Pre-inserts a stale 'delivering' execution (> 300s) and runs 20 concurrent workers.

        Verifies:
        - Exactly one worker wins the CAS lease recovery.
        - Telegram send_message is invoked exactly once (call_count == 1).
        - Status ends in 'delivered'.
        """
        user_id = 888000000 + random.randint(100000, 999999)
        event_name = f"concurrent_stale_{user_id}"
        async with self.session_maker() as session:
            user = User(
                id=user_id,
                username="stale_concurrent_user",
                first_name="Stale",
            )
            session.add(user)
            handler = AutomationHandler(
                name="Stale Concurrent Recovery Handler",
                event_name=event_name,
                scope="global",
                action_type="send_admin_message",
                action_payload_json=json.dumps({"text": "Alert: {user_id}"}),
                is_active=True,
            )
            session.add(handler)
            await session.flush()
            handler_id = handler.id

            exec_key = f"auto-exec:{handler_id}:{user_id}:1:{event_name}"
            stale_exec = AutomationActionExecution(
                handler_id=handler_id,
                user_id=user_id,
                event_name=event_name,
                execution_key=exec_key,
                status="delivering",
                created_at=datetime.utcnow() - timedelta(seconds=600),
                updated_at=datetime.utcnow() - timedelta(seconds=600),
            )
            session.add(stale_exec)
            await session.commit()

        fake_bot = AsyncMock(spec=Bot)
        fake_bot.send_message = AsyncMock()

        from unittest.mock import patch
        with patch("automation_events.get_all_admin_ids", new=AsyncMock(return_value=[999999999])):
            async def worker():
                async with self.session_maker() as session:
                    await dispatch_automation_events(
                        fake_bot,
                        session,
                        user_id=user_id,
                        dialogue_id=1,
                        scope="global",
                        events=[event_name],
                    )
                    await session.commit()

            await asyncio.gather(*(worker() for _ in range(20)))

        # Assert exactly one worker sent the message
        self.assertEqual(fake_bot.send_message.call_count, 1)

        async with self.session_maker() as session:
            final_exec = await session.scalar(
                select(AutomationActionExecution).where(
                    AutomationActionExecution.handler_id == handler_id,
                    AutomationActionExecution.user_id == user_id,
                )
            )
            self.assertEqual(final_exec.status, "delivered")


class PostgreSQLJournalConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """PostgreSQL 18 MVCC Concurrency & Constraint Verification for Journal Feature."""

    async def asyncSetUp(self):
        self.engine = None
        try:
            self.engine = create_async_engine(
                POSTGRES_URL,
                connect_args={"timeout": 5},
                pool_size=30,
                max_overflow=20,
            )
            async with self.engine.begin() as conn:
                from database import (
                    JournalConfig,
                    JournalSettings,
                    JournalQuestion,
                    JournalEntry,
                    JournalAnswer,
                    JournalReward,
                    JournalAiOperation,
                    User,
                )
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS journal_points INTEGER NOT NULL DEFAULT 0;"))
            self.session_maker = async_sessionmaker(
                bind=self.engine, class_=AsyncSession, expire_on_commit=False
            )
        except Exception as exc:
            self.skipTest(f"PostgreSQL is NOT reachable at {POSTGRES_URL} ({exc}).")

    async def asyncTearDown(self):
        if self.engine:
            await self.engine.dispose()

    def _unique_id(self, base: int) -> int:
        return base + random.randint(1, 900000000)

    async def _create_test_user(self, user_id: int, username: str = "pg_j_user") -> User:
        async with self.session_maker() as session:
            existing = await session.get(User, user_id)
            if existing:
                return existing
            u = User(
                id=user_id,
                username=username,
                journal_points=0,
                balance=100.0,
                created_at=datetime.utcnow(),
            )
            session.add(u)
            await session.commit()
            return u

    async def test_journal_01_schema_creation_and_unique_entry_date_20_workers(self):
        """20 workers concurrently attempt to create JournalEntry for same user and date."""
        from database import JournalEntry
        user_id = self._unique_id(3000000)
        await self._create_test_user(user_id)
        today = datetime.utcnow().date()

        inserted_count = 0

        async def worker():
            nonlocal inserted_count
            async with self.session_maker() as session:
                try:
                    async with session.begin_nested():
                        entry = JournalEntry(
                            user_id=user_id,
                            reflection_date=today,
                            status="in_progress",
                            questions_snapshot_json="[]",
                            started_at=datetime.utcnow(),
                        )
                        session.add(entry)
                        await session.flush()
                    await session.commit()
                    inserted_count += 1
                except IntegrityError:
                    await session.rollback()

        await asyncio.gather(*(worker() for _ in range(20)))

        # Exactly 1 winner
        self.assertEqual(inserted_count, 1)

        # Database contains exactly 1 row
        async with self.session_maker() as session:
            rows = (await session.scalars(
                select(JournalEntry).where(
                    JournalEntry.user_id == user_id,
                    JournalEntry.reflection_date == today,
                )
            )).all()
            self.assertEqual(len(rows), 1)

    async def test_journal_02_answer_fk_behavior_after_question_deletion(self):
        """JournalAnswer FK integrity when JournalQuestion is deleted in PostgreSQL."""
        from database import JournalEntry, JournalQuestion, JournalAnswer
        user_id = self._unique_id(3100000)
        await self._create_test_user(user_id)
        today = datetime.utcnow().date()

        async with self.session_maker() as session:
            q = JournalQuestion(text="Test question for FK", order_num=1, is_active=True)
            session.add(q)
            entry = JournalEntry(
                user_id=user_id,
                reflection_date=today,
                status="completed",
                questions_snapshot_json="[]",
                started_at=datetime.utcnow(),
            )
            session.add(entry)
            await session.flush()

            ans = JournalAnswer(
                entry_id=entry.id,
                question_id=q.id,
                position=1,
                question_text_snapshot="Test question for FK",
                answer_text="Answer text",
            )
            session.add(ans)
            await session.commit()

            q_id = q.id
            ans_id = ans.id

        # Delete question via approved pattern: UPDATE answers SET question_id=NULL then DELETE
        async with self.session_maker() as session:
            await session.execute(
                update(JournalAnswer).where(JournalAnswer.question_id == q_id).values(question_id=None)
            )
            await session.execute(
                delete(JournalQuestion).where(JournalQuestion.id == q_id)
            )
            await session.commit()

        # Verify answer remains with question_id = NULL and snapshot preserved
        async with self.session_maker() as session:
            ans_refreshed = await session.get(JournalAnswer, ans_id)
            self.assertIsNotNone(ans_refreshed)
            self.assertIsNone(ans_refreshed.question_id)
            self.assertEqual(ans_refreshed.question_text_snapshot, "Test question for FK")
            self.assertEqual(ans_refreshed.answer_text, "Answer text")

    async def test_journal_03_point_claim_concurrency_20_workers(self):
        """20 workers attempt atomic points claim via CAS RETURNING points_awarded."""
        from database import JournalEntry, User
        user_id = self._unique_id(3200000)
        await self._create_test_user(user_id)
        today = datetime.utcnow().date()

        async with self.session_maker() as session:
            entry = JournalEntry(
                user_id=user_id,
                reflection_date=today,
                status="completed",
                questions_snapshot_json="[]",
                started_at=datetime.utcnow(),
            )
            session.add(entry)
            await session.commit()
            entry_id = entry.id

        claim_wins = 0

        async def worker():
            nonlocal claim_wins
            now = datetime.utcnow()
            async with self.session_maker() as session:
                claim_stmt = (
                    update(JournalEntry)
                    .where(
                        JournalEntry.id == entry_id,
                        JournalEntry.user_id == user_id,
                        JournalEntry.points_awarded_at == None,
                    )
                    .values(
                        points_awarded=10,
                        points_awarded_at=now,
                    )
                    .returning(JournalEntry.points_awarded)
                )
                awarded = (await session.execute(claim_stmt)).scalar()
                if awarded is not None and awarded > 0:
                    claim_wins += 1
                    await session.execute(
                        update(User)
                        .where(User.id == user_id)
                        .values(journal_points=User.journal_points + awarded)
                    )
                await session.commit()

        await asyncio.gather(*(worker() for _ in range(20)))

        # Exactly 1 worker claimed points
        self.assertEqual(claim_wins, 1)

        async with self.session_maker() as session:
            u = await session.get(User, user_id)
            self.assertEqual(u.journal_points, 10)
            e = await session.get(JournalEntry, entry_id)
            self.assertEqual(e.points_awarded, 10)
            self.assertIsNotNone(e.points_awarded_at)

    async def test_journal_04_reward_unique_concurrent_claim_20_workers(self):
        """20 workers attempt to claim JournalReward cycle 1 concurrently."""
        from database import JournalReward
        user_id = self._unique_id(3300000)
        await self._create_test_user(user_id)

        reward_wins = 0

        async def worker():
            nonlocal reward_wins
            async with self.session_maker() as session:
                try:
                    async with session.begin_nested():
                        reward = JournalReward(
                            user_id=user_id,
                            cycle_number=1,
                            target_days=21,
                            days_granted=7,
                            granted_at=datetime.utcnow(),
                        )
                        session.add(reward)
                        await session.flush()
                    await session.commit()
                    reward_wins += 1
                except IntegrityError:
                    await session.rollback()

        await asyncio.gather(*(worker() for _ in range(20)))

        # Exactly 1 winner
        self.assertEqual(reward_wins, 1)

        # In DB, exactly 1 row with cycle_number=1
        async with self.session_maker() as session:
            rewards = (await session.scalars(
                select(JournalReward).where(JournalReward.user_id == user_id)
            )).all()
            self.assertEqual(len(rewards), 1)
            self.assertEqual(rewards[0].cycle_number, 1)

    async def test_journal_05_ai_operation_key_uniqueness_and_concurrency_20_workers(self):
        """20 workers concurrently attempt initiate_journal_ai_operation with same key."""
        from journal_service import initiate_journal_ai_operation
        user_id = self._unique_id(3400000)
        await self._create_test_user(user_id)
        op_key = f"pg_test_op:{user_id}:{uuid.uuid4().hex[:8]}"

        wins = 0
        loses = 0

        async def worker():
            nonlocal wins, loses
            async with self.session_maker() as session:
                op, won = await initiate_journal_ai_operation(
                    session=session,
                    operation_key=op_key,
                    user_id=user_id,
                    operation_type="daily_summary",
                )
                if won:
                    wins += 1
                else:
                    loses += 1

        await asyncio.gather(*(worker() for _ in range(20)))

        # Exactly 1 winner and 19 losers
        self.assertEqual(wins, 1)
        self.assertEqual(loses, 19)
