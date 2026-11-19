import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import funnel
from database import Base, FunnelConfig, FunnelEvent, FunnelState, User


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "event"), [
    ("listening", "stage_timeout_sent"),
    ("post_analysis", "followup_sent"),
    ("remarketing_7d", "remarketing_7d_sent"),
])
async def test_due_reminder_survives_quiet_hours_and_concurrent_workers(tmp_path, monkeypatch, stage, event):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(funnel, "async_session_maker", sessions)
    for keyboard in ("configured_meditation_listen_keyboard", "configured_reminder_keyboard", "configured_no_strategy_keyboard"):
        monkeypatch.setattr(funnel, keyboard, AsyncMock(return_value=None))
    now = datetime(2026, 11, 19, 21)
    monkeypatch.setattr(funnel, "msk_now", lambda: now)
    timeout = stage == "listening"
    due_field = "stage_timeout_at" if timeout else "next_followup_at"
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add_all([
                FunnelConfig(id=1), User(id=123, habit_timezone="Asia/Almaty"),
                FunnelState(user_id=123, stage=stage, is_active=True, reminder_count=0,
                           followup_sequence_anchor=now, **{due_field: now}),
            ])
        bot = AsyncMock()
        await funnel.process_funnel_followups(bot, single_iteration=True)
        bot.send_message.assert_not_awaited()
        async with sessions() as session:
            state = await session.get(FunnelState, 123)
            assert getattr(state, due_field) == datetime(2026, 11, 20, 7)
        now += timedelta(hours=10)
        bot.send_message.side_effect = RuntimeError("Telegram unavailable")
        await funnel.process_funnel_followups(bot, single_iteration=True)
        bot.send_message.assert_awaited_once()
        bot.send_message.reset_mock(side_effect=True)
        await asyncio.gather(*(funnel._process_due_funnel_reminders(bot, now, timeout=timeout) for _ in range(3)))
        bot.send_message.assert_awaited_once()
        async with sessions() as session:
            assert (await session.scalars(select(FunnelEvent.event_type))).all() == [event]
    finally:
        await engine.dispose()
