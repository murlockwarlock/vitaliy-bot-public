import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import background_worker as worker
from database import Base, BotConfig, Habit, JournalSettings, User


class ReminderTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        engine = create_async_engine(f"sqlite+aiosqlite:///{Path(directory.name) / 'test.db'}")
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.enterContext(patch.object(worker, "async_session_maker", self.sessions))
        self.bot = AsyncMock()
        self.night = datetime(2026, 11, 19, 21)
        self.morning = datetime(2026, 11, 20, 7)
        async with self.sessions() as session:
            session.add(User(id=42, habit_timezone="Asia/Yekaterinburg"))
            await session.commit()

    async def test_main_reminder_defers_then_retries_and_counts_one_delivery(self):
        async with self.sessions() as session:
            session.add(BotConfig(id=1, main_reminders_enabled=True))
            user = await session.get(User, 42)
            user.next_main_reminder_at = self.night
            await session.commit()
        await worker.process_main_reminders(self.bot, now=self.night)
        self.bot.send_message.assert_not_awaited()
        async with self.sessions() as session:
            user = await session.get(User, 42)
            self.assertEqual(user.next_main_reminder_at, self.morning)
        self.bot.send_message.side_effect = OSError()
        await worker.process_main_reminders(self.bot, now=self.morning)
        async with self.sessions() as session:
            user = await session.get(User, 42)
            self.assertEqual(user.main_reminder_count, 0)
            retry = user.next_main_reminder_at
            self.assertLess(retry, self.morning + timedelta(hours=1))
        self.bot.send_message.reset_mock(side_effect=True)
        await asyncio.gather(*(worker.process_main_reminders(self.bot, now=retry) for _ in range(2)))
        self.bot.send_message.assert_awaited_once()
        async with self.sessions() as session:
            self.assertEqual((await session.get(User, 42)).main_reminder_count, 1)

    async def test_habit_is_kept_after_failed_morning_delivery(self):
        due = datetime(2026, 11, 19, 18, tzinfo=timezone.utc)
        async with self.sessions() as session:
            session.add(Habit(id=1, user_id=42, name="Чтение", time_target="23:00", next_reminder_at=due.replace(tzinfo=None)))
            await session.commit()
        await worker.process_habit_reminders(self.bot, now=due)
        self.bot.send_message.assert_not_awaited()
        self.bot.send_message.side_effect = OSError()
        morning = datetime(2026, 11, 20, 4, tzinfo=timezone.utc)
        await worker.process_habit_reminders(self.bot, now=morning)
        async with self.sessions() as session:
            habit = await session.get(Habit, 1)
            self.assertIsNone(habit.last_reminder_at)
            retry = habit.next_reminder_at.replace(tzinfo=timezone.utc)
            self.assertLess(retry, morning + timedelta(hours=1))
        self.bot.send_message.reset_mock(side_effect=True)
        await asyncio.gather(*(worker.process_habit_reminders(self.bot, now=retry) for _ in range(2)))
        self.bot.send_message.assert_awaited_once()

    async def test_journal_defers_across_midnight_and_recovers_stale_claim(self):
        async with self.sessions() as session:
            session.add(JournalSettings(user_id=42, reminders_enabled=True, reminder_time="21:00"))
            await session.commit()
        await worker.process_journal_reminders(self.bot, now=self.night)
        self.bot.send_message.assert_not_awaited()
        async with self.sessions() as session:
            settings = await session.get(JournalSettings, 42)
            self.assertEqual(settings.snooze_until, self.morning)
            settings.snooze_claimed_at = self.morning - timedelta(hours=1)
            await session.commit()
        await asyncio.gather(*(worker.process_journal_reminders(self.bot, now=self.morning) for _ in range(2)))
        self.bot.send_message.assert_awaited_once()
        async with self.sessions() as session:
            settings = await session.get(JournalSettings, 42)
            self.assertIsNone(settings.snooze_until)
