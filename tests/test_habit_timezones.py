from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, select

import handlers
from background_worker import process_habit_reminders
from config import OWNER_IDS
from database import Habit, User, async_session_maker, init_db
from time_utils import (
    calculate_next_habit_reminder,
    utc_naive,
)


def utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class HabitTimezoneTests(IsolatedAsyncioTestCase):
    user_id = 983000001

    async def asyncSetUp(self):
        await init_db()
        async with async_session_maker() as session:
            await session.execute(delete(Habit).where(Habit.user_id == self.user_id))
            user = await session.get(User, self.user_id)
            if user is None:
                user = User(id=self.user_id, first_name="Habit timezone test")
                session.add(user)
            user.habit_timezone = None
            user.crisis_detected_at = None
            await session.commit()

    async def asyncTearDown(self):
        async with async_session_maker() as session:
            await session.execute(delete(Habit).where(Habit.user_id == self.user_id))
            await session.commit()

    def test_moscow_default_and_almaty_use_local_wall_time(self):
        now = utc(2026, 1, 15, 4, 30)
        expected_moscow = utc(2026, 1, 15, 5)
        expected_almaty = utc(2026, 1, 16, 4)

        self.assertEqual(calculate_next_habit_reminder("08:00", None, now), expected_moscow)
        self.assertEqual(
            calculate_next_habit_reminder("09:00", "Europe/Moscow", now),
            utc(2026, 1, 15, 6),
        )
        self.assertEqual(calculate_next_habit_reminder("09:00", "Asia/Almaty", now), expected_almaty)

    def test_dst_changes_are_applied_by_iana_zone(self):
        spring_now = utc(2026, 3, 28, 12)
        autumn_now = utc(2026, 10, 24, 12)

        self.assertEqual(
            calculate_next_habit_reminder("09:00", "Europe/Berlin", spring_now),
            utc(2026, 3, 29, 7),
        )
        self.assertEqual(
            calculate_next_habit_reminder("09:00", "Europe/Berlin", autumn_now),
            utc(2026, 10, 25, 8),
        )

    async def test_existing_habit_defaults_to_moscow_and_is_sent_once(self):
        scheduled_at = utc_naive(utc(2026, 1, 15, 6))
        async with async_session_maker() as session:
            session.add(Habit(
                user_id=self.user_id,
                name="Старая привычка",
                time_target="09:00",
                next_reminder_at=scheduled_at,
            ))
            await session.commit()

        class MockBot:
            def __init__(self):
                self.messages = []

            async def send_message(self, chat_id, text, **kwargs):
                self.messages.append((chat_id, text, kwargs))

        bot = MockBot()
        now = utc(2026, 1, 15, 6)
        await process_habit_reminders(bot, now=now)
        await process_habit_reminders(bot, now=now)

        self.assertEqual(sum(message[0] == self.user_id for message in bot.messages), 1)
        async with async_session_maker() as session:
            habit = (await session.execute(
                select(Habit).where(Habit.user_id == self.user_id)
            )).scalar_one()
        self.assertEqual(habit.next_reminder_at, utc_naive(utc(2026, 1, 16, 6)))
        self.assertEqual(habit.last_reminder_at, utc_naive(now))

    async def test_invalid_habit_time_does_not_stop_other_reminders(self):
        scheduled_at = utc_naive(utc(2026, 1, 15, 6))
        async with async_session_maker() as session:
            session.add_all([
                Habit(
                    user_id=self.user_id,
                    name="Некорректная привычка",
                    time_target="not-a-time",
                ),
                Habit(
                    user_id=self.user_id,
                    name="Рабочая привычка",
                    time_target="09:00",
                    next_reminder_at=scheduled_at,
                ),
            ])
            await session.commit()

        class MockBot:
            def __init__(self):
                self.messages = []

            async def send_message(self, chat_id, text, **kwargs):
                self.messages.append((chat_id, text, kwargs))

        bot = MockBot()
        await process_habit_reminders(bot, now=utc(2026, 1, 15, 6))

        user_messages = [message for message in bot.messages if message[0] == self.user_id]
        self.assertEqual(len(user_messages), 1)
        self.assertIn("Рабочая привычка", user_messages[0][1])

    async def test_timezone_change_reschedules_existing_habits(self):
        fixed_now = utc(2026, 1, 15, 5, 30)
        async with async_session_maker() as session:
            session.add(Habit(
                user_id=self.user_id,
                name="Переезд",
                time_target="09:00",
                next_reminder_at=utc_naive(utc(2026, 1, 15, 6)),
            ))
            await session.commit()

        callback = SimpleNamespace(
            data="habit_set_timezone_Asia/Almaty",
            from_user=SimpleNamespace(id=self.user_id),
            answer=AsyncMock(),
        )
        with patch("handlers.as_utc", return_value=fixed_now), patch(
            "handlers.send_smart_content", new_callable=AsyncMock
        ):
            await handlers.habit_set_timezone(callback)

        async with async_session_maker() as session:
            user = await session.get(User, self.user_id)
            habit = (await session.execute(
                select(Habit).where(Habit.user_id == self.user_id)
            )).scalar_one()
        self.assertEqual(user.habit_timezone, "Asia/Almaty")
        self.assertEqual(
            habit.next_reminder_at,
            utc_naive(calculate_next_habit_reminder("09:00", "Asia/Almaty", fixed_now)),
        )

    async def test_timezone_change_skips_invalid_habit_and_reschedules_valid_one(self):
        fixed_now = utc(2026, 1, 15, 5, 30)
        async with async_session_maker() as session:
            session.add_all([
                Habit(
                    user_id=self.user_id,
                    name="Старая некорректная",
                    time_target="25:99",
                ),
                Habit(
                    user_id=self.user_id,
                    name="Корректная",
                    time_target="09:00",
                ),
            ])
            await session.commit()

        callback = SimpleNamespace(
            data="habit_set_timezone_Asia/Almaty",
            from_user=SimpleNamespace(id=self.user_id),
            answer=AsyncMock(),
        )
        with patch("handlers.as_utc", return_value=fixed_now), patch(
            "handlers.send_smart_content", new_callable=AsyncMock
        ):
            await handlers.habit_set_timezone(callback)

        async with async_session_maker() as session:
            habits = (await session.execute(
                select(Habit).where(Habit.user_id == self.user_id).order_by(Habit.id)
            )).scalars().all()
            user = await session.get(User, self.user_id)
        self.assertEqual(user.habit_timezone, "Asia/Almaty")
        self.assertIsNone(habits[0].next_reminder_at)
        self.assertEqual(
            habits[1].next_reminder_at,
            utc_naive(calculate_next_habit_reminder("09:00", "Asia/Almaty", fixed_now)),
        )

    async def test_timezone_change_after_send_does_not_repeat_same_local_day(self):
        fixed_now = utc(2026, 1, 15, 6, 5)
        last_reminder_at = utc_naive(utc(2026, 1, 15, 6))
        async with async_session_maker() as session:
            user = await session.get(User, self.user_id)
            user.habit_timezone = "Europe/Moscow"
            session.add(Habit(
                user_id=self.user_id,
                name="Без повтора",
                time_target="09:00",
                next_reminder_at=last_reminder_at,
                last_reminder_at=last_reminder_at,
            ))
            await session.commit()

        callback = SimpleNamespace(
            data="habit_set_timezone_Europe/Berlin",
            from_user=SimpleNamespace(id=self.user_id),
            answer=AsyncMock(),
        )
        with patch("handlers.as_utc", return_value=fixed_now), patch(
            "handlers.send_smart_content", new_callable=AsyncMock
        ):
            await handlers.habit_set_timezone(callback)

        async with async_session_maker() as session:
            habit = (await session.execute(
                select(Habit).where(Habit.user_id == self.user_id)
            )).scalar_one()
        self.assertEqual(
            habit.next_reminder_at,
            utc_naive(calculate_next_habit_reminder(
                "09:00",
                "Europe/Berlin",
                fixed_now,
                last_reminder_at=last_reminder_at,
            )),
        )

        class MockBot:
            def __init__(self):
                self.messages = []

            async def send_message(self, *args, **kwargs):
                self.messages.append((args, kwargs))

        bot = MockBot()
        await process_habit_reminders(bot, now=fixed_now)
        self.assertEqual(bot.messages, [])

    async def test_autumn_dst_fold_sends_once_for_the_local_day(self):
        first_fold = utc(2026, 10, 25, 0, 30)
        second_fold = utc(2026, 10, 25, 1, 30)
        async with async_session_maker() as session:
            user = await session.get(User, self.user_id)
            user.habit_timezone = "Europe/Berlin"
            session.add(Habit(
                user_id=self.user_id,
                name="Переход DST",
                time_target="02:30",
                next_reminder_at=utc_naive(first_fold),
            ))
            await session.commit()

        class MockBot:
            def __init__(self):
                self.messages = []

            async def send_message(self, chat_id, text, **kwargs):
                self.messages.append((chat_id, text, kwargs))

        bot = MockBot()
        await process_habit_reminders(bot, now=first_fold)
        await process_habit_reminders(bot, now=second_fold)

        self.assertEqual(sum(message[0] == self.user_id for message in bot.messages), 1)
        async with async_session_maker() as session:
            habit = (await session.execute(
                select(Habit).where(Habit.user_id == self.user_id)
            )).scalar_one()
        self.assertEqual(habit.last_reminder_at, utc_naive(first_fold))
        self.assertEqual(habit.next_reminder_at, utc_naive(utc(2026, 10, 26, 1, 30)))

    async def test_time_change_reschedules_next_reminder(self):
        fixed_now = utc(2026, 1, 15, 3)
        async with async_session_maker() as session:
            user = await session.get(User, self.user_id)
            user.habit_timezone = "Asia/Almaty"
            habit = Habit(
                user_id=self.user_id,
                name="Новое время",
                time_target="09:00",
            )
            session.add(habit)
            await session.commit()
            habit_id = habit.id

        name = await handlers._set_habit_time(self.user_id, habit_id, "10:00", now=fixed_now)
        self.assertEqual(name, "Новое время")
        async with async_session_maker() as session:
            habit = await session.get(Habit, habit_id)
        self.assertEqual(habit.time_target, "10:00")
        self.assertEqual(
            habit.next_reminder_at,
            utc_naive(calculate_next_habit_reminder("10:00", "Asia/Almaty", fixed_now)),
        )

    async def test_crisis_lock_keeps_habit_reminder_unsent(self):
        scheduled_at = utc_naive(utc(2026, 1, 15, 6))
        async with async_session_maker() as session:
            user = await session.get(User, self.user_id)
            user.crisis_detected_at = scheduled_at
            session.add(Habit(
                user_id=self.user_id,
                name="Кризисная блокировка",
                time_target="09:00",
                next_reminder_at=scheduled_at,
            ))
            await session.commit()

        class MockBot:
            def __init__(self):
                self.messages = []

            async def send_message(self, *args, **kwargs):
                self.messages.append((args, kwargs))

        bot = MockBot()
        await process_habit_reminders(bot, now=utc(2026, 1, 15, 6))
        self.assertEqual(bot.messages, [])

    async def test_admin_screen_links_reminder_texts_to_content_editor(self):
        event = SimpleNamespace(from_user=SimpleNamespace(id=OWNER_IDS[0]))
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as send:
            await handlers.admin_reminders_settings(event)

        markup = send.call_args.args[3]
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("edit_cont_main:main_reminder_first", callbacks)
        self.assertIn("edit_cont_main:main_reminder_common", callbacks)
        self.assertIn("Контент» → «Главное меню / Старт", send.call_args.args[1])
        self.assertIn("main_reminder_first", handlers.CATEGORIES["main"])
        self.assertIn("main_reminder_common", handlers.CATEGORIES["main"])
