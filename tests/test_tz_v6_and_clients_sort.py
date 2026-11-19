import io
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Audio, BufferedInputFile, CallbackQuery, Chat, Message, Update, User as TgUser
from sqlalchemy import delete, select

import ai_integration
import background_worker
import funnel
import handlers
from config import OWNER_IDS
from database import (
    AIConfig,
    AILog,
    Book,
    BotConfig,
    Content,
    DEFAULT_HABIT_REMINDER_TEXT,
    DEFAULT_JOURNAL_REMINDER_TEXT,
    FunnelMeditation,
    FunnelState,
    Habit,
    JournalEntry,
    JournalSettings,
    Message as DBMessage,
    PaymentConfig,
    QuickNote,
    Topic,
    TopicDialogueConfig,
    User,
    async_session_maker,
    init_db,
)
from followups import FollowupCampaign, FollowupStep


class TzV6AndClientsSortTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        async with async_session_maker() as session:
            await session.execute(delete(AILog))
            await session.execute(delete(QuickNote))
            await session.execute(delete(JournalEntry))
            await session.execute(delete(JournalSettings))
            await session.execute(delete(Habit).where(Habit.user_id.is_not(None)))
            await session.execute(delete(FunnelState))
            await session.execute(delete(DBMessage))
            await session.execute(delete(User))
            cfg = await session.get(AIConfig, 1)
            cfg.provider = "OpenAI"
            cfg.openai_api_key = "sk-test-dummy"
            cfg.openai_model = "gpt-4o-mini"
            cfg.allow_fallback = False
            p_cfg = await session.get(PaymentConfig, 1)
            p_cfg.mode = "free"
            await session.commit()

    def _extract_id3v23_frames(self, audio_bytes: bytes) -> dict[str, str]:
        self.assertEqual(audio_bytes[:3], b"ID3")
        tag_size = (
            ((audio_bytes[6] & 0x7F) << 21)
            | ((audio_bytes[7] & 0x7F) << 14)
            | ((audio_bytes[8] & 0x7F) << 7)
            | (audio_bytes[9] & 0x7F)
        )
        pos = 10
        end = 10 + tag_size
        frames: dict[str, str] = {}
        while pos + 10 <= end:
            fid = audio_bytes[pos : pos + 4]
            if fid == b"\x00\x00\x00\x00":
                break
            fsize = int.from_bytes(audio_bytes[pos + 4 : pos + 8], "big")
            payload = audio_bytes[pos + 10 : pos + 10 + fsize]
            if payload and payload[0] == 1:
                frames[fid.decode("ascii")] = payload[1:].decode("utf-16")
            elif payload and payload[0] == 3:
                frames[fid.decode("ascii")] = payload[1:].decode("utf-8")
            pos += 10 + fsize
        return frames

    async def test_item1_meditation_audio_retags_title_and_caches_new_file_id(self):
        # Build legacy MP3 bytes with an ugly hash ID3v2 header + ID3v1 footer
        old_header = funnel._build_id3v23_header("29eaf525-3211-hash", "Unknown")
        mp3_body = b"\xff\xfb\x90\x64" + (b"\x11" * 256)
        id3v1_footer = b"TAG" + b"OldHashTitle".ljust(125, b"\x00")
        legacy_mp3 = old_header + mp3_body + id3v1_footer

        with patch("funnel.shutil.which", return_value=None):
            retagged, ext = funnel.retag_audio_bytes_with_title(
                legacy_mp3,
                "Снятие созависимости 🔗✨",
                "Виталий Наумов",
            )
        self.assertEqual(ext, ".mp3")
        parsed_frames = self._extract_id3v23_frames(retagged)
        self.assertEqual(parsed_frames.get("TIT2"), "Снятие созависимости 🔗✨")
        self.assertEqual(parsed_frames.get("TPE1"), "Виталий Наумов")
        self.assertNotIn(b"29eaf525-3211-hash", retagged)
        self.assertNotIn(b"OldHashTitle", retagged)

        async with async_session_maker() as session:
            user = User(id=701001, first_name="Client", username="client_audio")
            med = FunnelMeditation(
                sort_order=99,
                duration_minutes=15,
                title="Снятие созависимости 🔗✨",
                file_type="audio",
                file_id="OLD_HASH_FILE_ID",
                is_active=True,
            )
            session.add_all([user, med])
            await session.commit()
            med_id = med.id

        bot = AsyncMock()
        bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="music/29eaf525_3211.mp3"))
        bot.download_file = AsyncMock(return_value=io.BytesIO(legacy_mp3))
        bot.send_audio = AsyncMock(
            return_value=SimpleNamespace(
                message_id=501,
                audio=SimpleNamespace(file_id="NEW_TAGGED_FILE_ID"),
            )
        )

        target = SimpleNamespace(
            from_user=SimpleNamespace(id=701001),
            chat=SimpleNamespace(id=701001),
            bot=bot,
        )

        with patch("funnel.shutil.which", return_value=None):
            await funnel.send_meditation(target, med_id, bot=bot)

        self.assertEqual(bot.send_audio.await_count, 1)
        first_call = bot.send_audio.await_args
        uploaded_arg = first_call.args[1]
        self.assertIsInstance(uploaded_arg, BufferedInputFile)
        self.assertEqual(uploaded_arg.filename, "Снятие созависимости 🔗✨.mp3")
        self.assertEqual(first_call.kwargs.get("title"), "Снятие созависимости 🔗✨")
        self.assertEqual(first_call.kwargs.get("performer"), "Виталий Наумов")

        async with async_session_maker() as session:
            refreshed = await session.get(FunnelMeditation, med_id)
            self.assertEqual(refreshed.file_id, "NEW_TAGGED_FILE_ID")
            self.assertEqual(refreshed.audio_title_synced_for, "Снятие созависимости 🔗✨")

        # Second send reuses cached NEW_TAGGED_FILE_ID without downloading again
        bot.get_file.reset_mock()
        bot.download_file.reset_mock()
        bot.send_audio.reset_mock()
        await funnel.send_meditation(target, med_id, bot=bot)
        bot.get_file.assert_not_awaited()
        bot.download_file.assert_not_awaited()
        self.assertEqual(bot.send_audio.await_args.args[1], "NEW_TAGGED_FILE_ID")

    async def test_item2_dynamics_insufficient_entries_uses_full_days_word(self):
        async with async_session_maker() as session:
            user = User(id=702001, first_name="Анна", name="Анна", gender="Женский")
            entry = JournalEntry(
                user_id=user.id,
                reflection_date=date.today(),
                status="completed",
                questions_snapshot_json="[]",
            )
            session.add_all([user, entry])
            await session.commit()

        ok7, msg7 = await ai_integration.generate_journal_dynamics_analysis(
            user_id=702001,
            days=7,
            chat_id=702001,
            token8="t7days01",
        )
        self.assertFalse(ok7)
        self.assertIn("за 7 дней", msg7)
        self.assertNotIn("7 дн.", msg7)

        ok21, msg21 = await ai_integration.generate_journal_dynamics_analysis(
            user_id=702001,
            days=21,
            chat_id=702001,
            token8="t21day01",
        )
        self.assertFalse(ok21)
        self.assertIn("за 21 день", msg21)
        self.assertNotIn("21 дн.", msg21)

    async def test_item3_habit_and_journal_reminders_default_and_admin_override(self):
        self.assertIn("Пора уделить время себе. Получилось сегодня?", DEFAULT_HABIT_REMINDER_TEXT)
        self.assertNotIn("(а)", DEFAULT_HABIT_REMINDER_TEXT)
        self.assertNotIn("(а)", DEFAULT_JOURNAL_REMINDER_TEXT)
        self.assertIn("habit_reminder_text", handlers.CATEGORIES["habits"])
        self.assertIn("journal_reminder_text", handlers.CATEGORIES["journal"])

        due_utc = datetime(2026, 11, 19, 10, 0)
        msk_day = datetime(2026, 11, 19, 19, 0)
        bot = AsyncMock()

        async with async_session_maker() as session:
            user = User(id=703001, first_name="Мария", habit_timezone="Europe/Moscow")
            habit = Habit(
                user_id=703001,
                name="Медитация <вечер>",
                time_target="13:00",
                is_active=True,
                next_reminder_at=due_utc - timedelta(minutes=1),
            )
            j_set = JournalSettings(
                user_id=703001,
                reminders_enabled=True,
                reminder_time="18:30",
            )
            # Ensure defaults are used initially
            c_hab = await session.get(Content, "habit_reminder_text")
            if c_hab:
                c_hab.text_content = DEFAULT_HABIT_REMINDER_TEXT
            c_jrn = await session.get(Content, "journal_reminder_text")
            if c_jrn:
                c_jrn.text_content = DEFAULT_JOURNAL_REMINDER_TEXT
            session.add_all([user, habit, j_set])
            await session.commit()
            habit_id = habit.id

        await background_worker.process_habit_reminders(bot, now=due_utc)
        self.assertEqual(bot.send_message.await_count, 1)
        sent_habit_text = bot.send_message.await_args.args[1]
        self.assertIn("Пора уделить время себе. Получилось сегодня?", sent_habit_text)
        self.assertIn("Медитация &lt;вечер&gt;", sent_habit_text)
        self.assertNotIn("(а)", sent_habit_text)

        # Admin customizes both reminder texts in Content
        bot.send_message.reset_mock()
        async with async_session_maker() as session:
            h_row = await session.get(Habit, habit_id)
            h_row.last_reminder_at = None
            h_row.next_reminder_at = due_utc - timedelta(minutes=1)
            c_hab = await session.get(Content, "habit_reminder_text")
            c_hab.text_content = "🌿 Время для привычки «{habit_name}». Получилось сегодня?"
            c_jrn = await session.get(Content, "journal_reminder_text")
            c_jrn.text_content = "📔 Вечерний дневник ждёт твоих заметок!"
            await session.commit()

        await background_worker.process_habit_reminders(bot, now=due_utc)
        self.assertEqual(
            bot.send_message.await_args.args[1],
            "🌿 Время для привычки «Медитация &lt;вечер&gt;». Получилось сегодня?",
        )

        bot.send_message.reset_mock()
        await background_worker.process_journal_reminders(bot, now=msk_day)
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertEqual(
            bot.send_message.await_args.kwargs.get("text"),
            "📔 Вечерний дневник ждёт твоих заметок!",
        )

    async def test_item5_journal_summary_and_dynamics_include_user_profile(self):
        today = date(2026, 11, 19)
        async with async_session_maker() as session:
            user = User(
                id=705001,
                first_name="Olya",
                name="Ольга",
                gender="Женский",
                age="34",
            )
            e1 = JournalEntry(
                user_id=user.id,
                reflection_date=today - timedelta(days=1),
                status="completed",
                questions_snapshot_json="[]",
                summary_text="Спокойный день.",
            )
            e2 = JournalEntry(
                user_id=user.id,
                reflection_date=today,
                status="completed",
                questions_snapshot_json="[]",
            )
            session.add_all([user, e1, e2])
            await session.commit()
            entry_id = e2.id

        captured_messages: list[list[dict]] = []

        async def fake_llm(cfg, client, model, messages):
            captured_messages.append(messages)
            return SimpleNamespace(
                text="[[SUMMARY]]\nТы справилась отлично.\n[[ENCOURAGING]]\nТы не одна.\n[[FOCUS]]\nОтдых.\n[[METADATA_JSON]]\n{}",
                raw_payload={"id": "mock-j"},
                http_status=200,
                finish_reason="stop",
                diagnostics={},
            )

        with patch("ai_integration._generate_chat_response", side_effect=fake_llm), \
             patch("time_utils.msk_now", return_value=datetime(2026, 11, 19, 20, 0)), \
             patch("journal_service.msk_now", return_value=datetime(2026, 11, 19, 20, 0)):
            res = await ai_integration.generate_journal_daily_summary(entry_id, 705001, 705001)
            self.assertEqual(res, "succeeded")

            ok, _ = await ai_integration.generate_journal_dynamics_analysis(705001, 7, 705001, "profchk1")
            self.assertTrue(ok)

        self.assertEqual(len(captured_messages), 2)
        for idx, msg_list in enumerate(captured_messages):
            sys_msg = msg_list[0]["content"]
            self.assertIn("Профиль пользователя:", sys_msg, f"Missing profile block in call #{idx + 1}")
            self.assertIn("Имя пользователя: Ольга", sys_msg)
            self.assertIn("Пол: Женский", sys_msg)

    async def test_item4_em_dash_normalized_in_content_but_preserved_in_system_prompts(self):
        async with async_session_maker() as session:
            bot_conf = await session.get(BotConfig, 1)
            bot_conf.content_em_dash_normalized = False

            c_item = await session.get(Content, "vitaly_intro")
            c_item.text_content = "Виталий Наумов — автор метода Транскоучинг."

            topic = await session.scalar(select(Topic).where(Topic.slug == "free_session"))
            topic.system_prompt = "Ты — системный промпт, здесь длинное тире остаётся."

            tdc = await session.scalar(
                select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "free_session")
            )
            tdc.welcome_text = "Добро пожаловать — начнём разговор."
            tdc.prerecorded_first_message = "Привет — расскажи, что важно сейчас."
            tdc.summary_prompt = "Сводка — служебный промпт не трогать."

            med = FunnelMeditation(
                sort_order=101,
                duration_minutes=12,
                title="Практика — спокойствие",
                short_description="Коротко — про опору",
                full_description="Подробно — глубокое расслабление",
                is_active=True,
            )
            book = Book(title="Книга — Путь", description="Описание — полезное чтение", url="https://example.com")
            camp = FollowupCampaign(name="TestCamp", scope="global", is_active=0)
            session.add_all([med, book, camp])
            await session.flush()
            step = FollowupStep(
                campaign_id=camp.id,
                step_number=1,
                delay_minutes=60,
                step_type="static",
                message_text="Напоминание — загляни в бот",
                ai_instruction="ИИ-инструкция — не трогать",
            )
            session.add(step)
            await session.commit()
            med_id, book_id, step_id = med.id, book.id, step.id

        await init_db()

        async with async_session_maker() as session:
            self.assertEqual(
                (await session.get(Content, "vitaly_intro")).text_content,
                "Виталий Наумов – автор метода Транскоучинг.",
            )
            tdc = await session.scalar(
                select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "free_session")
            )
            self.assertEqual(tdc.welcome_text, "Добро пожаловать – начнём разговор.")
            self.assertEqual(tdc.prerecorded_first_message, "Привет – расскажи, что важно сейчас.")
            self.assertEqual(tdc.summary_prompt, "Сводка — служебный промпт не трогать.")

            topic = await session.scalar(select(Topic).where(Topic.slug == "free_session"))
            self.assertEqual(topic.system_prompt, "Ты — системный промпт, здесь длинное тире остаётся.")

            med = await session.get(FunnelMeditation, med_id)
            self.assertEqual(med.title, "Практика – спокойствие")
            self.assertEqual(med.short_description, "Коротко – про опору")
            self.assertEqual(med.full_description, "Подробно – глубокое расслабление")

            book = await session.get(Book, book_id)
            self.assertEqual(book.title, "Книга – Путь")
            self.assertEqual(book.description, "Описание – полезное чтение")

            step = await session.get(FollowupStep, step_id)
            self.assertEqual(step.message_text, "Напоминание – загляни в бот")
            self.assertEqual(step.ai_instruction, "ИИ-инструкция — не трогать")

            self.assertTrue((await session.get(BotConfig, 1)).content_em_dash_normalized)

    async def test_clients_list_puts_crisis_users_on_top_and_sorts_by_last_action(self):
        base = datetime(2026, 11, 10, 12, 0)
        async with async_session_maker() as session:
            # 1. Older crisis user (registered earliest, active 5 days ago) -> MUST still beat non-crisis users!
            u_crisis_older = User(
                id=801,
                username="crisis_older",
                created_at=base - timedelta(days=60),
                last_activity_at=base + timedelta(days=1),
                crisis_detected_at=base + timedelta(days=1),
            )
            # 2. Newer crisis user (active most recently among crisis users) -> #1 overall!
            u_crisis_newer = User(
                id=802,
                username="freshback_sochi",
                created_at=base - timedelta(days=120),
                last_activity_at=base + timedelta(days=8),
                crisis_detected_at=base + timedelta(days=8),
            )
            # 3. Recently registered idle user (created_at = base + 5d, no later actions)
            u_recent_reg = User(
                id=803,
                username="darimiru_support",
                created_at=base + timedelta(days=5),
                last_activity_at=base + timedelta(days=5),
            )
            # 4. Old account, but sent a user message today (base + 9d) -> #1 among non-crisis!
            u_active_msg = User(
                id=804,
                username="active_chatter",
                created_at=base - timedelta(days=90),
                last_activity_at=base - timedelta(days=90),
            )
            # 5. Old account, acted in funnel at base + 7d -> #2 among non-crisis!
            u_active_funnel = User(
                id=805,
                username="funnel_actor",
                created_at=base - timedelta(days=80),
                last_activity_at=base - timedelta(days=80),
            )
            session.add_all([u_crisis_older, u_crisis_newer, u_recent_reg, u_active_msg, u_active_funnel])
            await session.flush()
            session.add(
                DBMessage(
                    user_id=804,
                    dialogue_id=1,
                    role="user",
                    content="Привет",
                    timestamp=base + timedelta(days=9),
                )
            )
            session.add(
                FunnelState(
                    user_id=805,
                    stage="listening",
                    is_active=True,
                    last_user_action_at=base + timedelta(days=7),
                )
            )
            await session.commit()

        admin_id = OWNER_IDS[0]
        storage = MemoryStorage()
        state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=admin_id, user_id=admin_id))
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            data="admin_clients_list_0",
            message=SimpleNamespace(chat=SimpleNamespace(id=admin_id), message_id=900),
            answer=AsyncMock(),
        )

        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_render:
            mock_render.return_value = SimpleNamespace(chat=SimpleNamespace(id=admin_id), message_id=900)
            await handlers.admin_clients_list(cb, state)

        markup = mock_render.call_args.args[3]
        labels = [
            btn.text
            for row in markup.inline_keyboard
            for btn in row
            if btn.callback_data and btn.callback_data.startswith("admin_user_profile_")
        ]
        self.assertEqual(
            labels,
            [
                "🚨 @freshback_sochi",
                "🚨 @crisis_older",
                "@active_chatter",
                "@funnel_actor",
                "@darimiru_support",
            ],
        )

    async def test_user_activity_middleware_updates_user_actions_and_skips_admin_callbacks(self):
        old_ts = datetime(2026, 1, 1, 10, 0)
        async with async_session_maker() as session:
            session.add(User(id=909001, username="mw_user", created_at=old_ts, last_activity_at=old_ts))
            await session.commit()

        dp = Dispatcher(storage=MemoryStorage())
        mw = handlers.UserActivityMiddleware()
        dp.callback_query.outer_middleware(mw)

        @dp.callback_query()
        async def _noop_cb(callback: CallbackQuery):
            return None

        bot = AsyncMock()
        msg_payload = Message(
            message_id=10,
            date=datetime.now(),
            chat=Chat(id=909001, type="private"),
            from_user=TgUser(id=1, is_bot=True, first_name="Bot"),
            text="menu",
        )

        # 1. Admin callback should NOT bump client activity
        admin_upd = Update(
            update_id=1,
            callback_query=CallbackQuery(
                id="cb1",
                from_user=TgUser(id=909001, is_bot=False, first_name="U"),
                chat_instance="ci1",
                message=msg_payload,
                data="admin_clients_list_0",
            ),
        )
        await dp.feed_update(bot, admin_upd)
        async with async_session_maker() as session:
            self.assertEqual((await session.get(User, 909001)).last_activity_at, old_ts)

        # 2. Regular user callback MUST bump last_activity_at
        user_upd = Update(
            update_id=2,
            callback_query=CallbackQuery(
                id="cb2",
                from_user=TgUser(id=909001, is_bot=False, first_name="U"),
                chat_instance="ci1",
                message=msg_payload,
                data="mode_journal",
            ),
        )
        await dp.feed_update(bot, user_upd)
        async with async_session_maker() as session:
            self.assertGreater((await session.get(User, 909001)).last_activity_at, old_ts)
