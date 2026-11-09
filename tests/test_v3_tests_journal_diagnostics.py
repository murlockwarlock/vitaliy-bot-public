import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database import (
    Base,
    JournalAnswer,
    JournalConfig,
    JournalEntry,
    JournalQuestion,
    TestAttempt,
    TestQuestion,
    TestSession,
    TestSet,
    Topic,
    TopicDialogueConfig,
    User,
    run_data_migrations,
)
from journal_handlers import JournalStates, _persist_journal_answer, cb_journal_skip
from journal_keyboards import journal_question_keyboard
from journal_service import build_journal_daily_summary_context, build_questions_snapshot
from keyboards import universal_test_question_keyboard
from journal_admin_handlers import (
    JournalAdminStates,
    admin_journal_q_custom_answer_handler,
    admin_journal_q_guided_options_submit,
    admin_journal_q_set_type_handler,
    admin_journal_q_type_handler,
    reorder_questions as reorder_journal_questions,
)
from topic_admin_handlers import set_test_set_selection, toggle_pre_test
from test_set_admin_handlers import (
    TestSetAdminStates,
    _question_edit_payload,
    guided_question_type,
    handle_questions_upload,
    open_test_set_details,
    receive_guided_question_options,
    receive_guided_question_text,
    save_guided_question_custom_answer,
    start_guided_question_add,
)
from test_set_admin_handlers import reorder_questions as reorder_testset_questions
from test_system_handlers import _finish_test, _questions, _send_question, exit_universal_test, start_universal_test
from universal_tests import (
    QUESTION_TYPE_CHOICE,
    QUESTION_TYPE_SCALE,
    QUESTION_TYPE_TEXT,
    build_question_text,
    expected_test_set_question_count,
    get_answer_options,
    is_esoteric_question,
    make_text_answer_record,
    question_accepts_text,
    reorder_questions,
    is_test_set_ready,
)


async def _local_database(tmp_path):
    local_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'v3_tests.sqlite'}")
    local_session_maker = async_sessionmaker(local_engine, expire_on_commit=False)
    async with local_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return local_engine, local_session_maker


def _callback_buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_question_types_progress_free_text_and_exit_controls():
    text_question = SimpleNamespace(
        text="Расскажи о состоянии",
        question_type=QUESTION_TYPE_TEXT,
        allow_custom_answer=False,
        answer_options_json=None,
    )
    choice_question = SimpleNamespace(
        text="Выбери состояние",
        question_type=QUESTION_TYPE_CHOICE,
        allow_custom_answer=True,
        answer_options_json=json.dumps([{"text": "Спокойно", "value": 1}]),
    )
    scale_question = SimpleNamespace(
        text="Оцени состояние",
        question_type=QUESTION_TYPE_SCALE,
        allow_custom_answer=False,
        answer_options_json=None,
    )

    assert question_accepts_text(text_question)
    assert question_accepts_text(choice_question)
    assert not question_accepts_text(scale_question)
    assert [option.text for option in get_answer_options(scale_question)] == [str(i) for i in range(1, 11)]
    assert make_text_answer_record(choice_question, 0, "Свой вариант")["answer"] == "Свой вариант"
    assert "выбери" in str(_raises_value_error(make_text_answer_record, scale_question, 0, "текст"))

    rendered = build_question_text(choice_question, 1, 5)
    assert "Вопрос 2 из 5" in rendered
    assert "░" not in rendered and "█" not in rendered
    assert "выбери" in rendered

    universal_markup = universal_test_question_keyboard(get_answer_options(choice_question), question_index=1)
    assert "test_exit" in {button.callback_data for button in _callback_buttons(universal_markup)}
    assert "test_exit" in {
        button.callback_data
        for button in _callback_buttons(universal_test_question_keyboard([], question_index=1))
    }

    journal_markup = journal_question_keyboard(
        42,
        2,
        {"options": [{"text": "Да", "value": 1}], "question_type": QUESTION_TYPE_CHOICE},
    )
    journal_callbacks = {button.callback_data for button in _callback_buttons(journal_markup)}
    assert "j_skip:42:2" in journal_callbacks
    assert "j_exit" in journal_callbacks


def _raises_value_error(function, *args):
    try:
        function(*args)
    except ValueError as exc:
        return exc
    raise AssertionError("ValueError was not raised")


def _admin_callback(data: str, user_id: int):
    message = SimpleNamespace(
        edit_text=AsyncMock(),
        answer=AsyncMock(),
        chat=SimpleNamespace(id=user_id),
        message_id=1,
    )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=message,
        answer=AsyncMock(),
    )


def _admin_message(text: str, user_id: int):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_testset_attempt_keeps_question_and_interpretation_snapshot_after_admin_edit(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    user_id = 992001
    try:
        async with local_session_maker() as session:
            session.add(User(id=user_id, first_name="Тест"))
            test_set = TestSet(
                id=101,
                title="Профиль",
                slug="profile_snapshot",
                show_progress=True,
                calculation_enabled=True,
                formulas_json=json.dumps([{"name": "score", "formula": "answer_01"}]),
                separate_interpretation_prompt=True,
                interpretation_prompt="Старый промпт",
                interpretation_input_mode="all",
            )
            session.add(test_set)
            session.add_all([
                TestQuestion(
                    test_set_id=test_set.id,
                    text="Старый вопрос 1",
                    question_type=QUESTION_TYPE_SCALE,
                    order_num=1,
                ),
                TestQuestion(
                    test_set_id=test_set.id,
                    text="Старый вопрос 2",
                    question_type=QUESTION_TYPE_TEXT,
                    order_num=2,
                ),
            ])
            await session.commit()

        bot = AsyncMock()
        state = AsyncMock()
        with patch("database.async_session_maker", local_session_maker), \
             patch("test_system_handlers.async_session_maker", local_session_maker):
            assert await start_universal_test(bot, user_id, state, test_set_id=test_set.id)

            async with local_session_maker() as session:
                attempt = await session.scalar(select(TestAttempt).where(TestAttempt.user_id == user_id))
                assert attempt.configuration_snapshot_json
                test_set_db = await session.get(TestSet, test_set.id)
                test_set_db.show_progress = False
                test_set_db.interpretation_prompt = "Новый промпт"
                test_set_db.formulas_json = json.dumps([{"name": "score", "formula": "answer_02"}])
                questions = list((await session.scalars(
                    select(TestQuestion).where(TestQuestion.test_set_id == test_set.id)
                )).all())
                questions[0].text = "Новый вопрос 1"
                questions[1].text = "Новый вопрос 2"
                await session.commit()

            await _send_question(bot, user_id, 1, state=state)

        assert "Старый вопрос 2" in bot.send_message.await_args_list[-1].args[1]
        assert "Новый вопрос 2" not in bot.send_message.await_args_list[-1].args[1]
    finally:
        await local_engine.dispose()


@pytest.mark.asyncio
async def test_testset_import_rejects_invalid_future_definition_without_replacing_questions(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    admin_id = 992002
    try:
        async with local_session_maker() as session:
            session.add(User(id=admin_id, first_name="Админ", is_admin=True, admin_role="content_editor"))
            test_set = TestSet(
                id=102,
                title="Импорт",
                slug="import_validation",
                formulas_json=json.dumps([{"name": "broken", "formula": "missing_score + 1"}]),
            )
            session.add(test_set)
            session.add(TestQuestion(test_set_id=test_set.id, text="Старый вопрос", order_num=1))
            await session.commit()

        payload = json.dumps({"questions": [{"text": "Новый вопрос", "type": "text"}]})
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"target_ts_id": 102})
        message = _admin_message("", admin_id)
        message.document = SimpleNamespace(file_name="questions.json")
        bot = AsyncMock()
        bot.download = AsyncMock(side_effect=lambda document, destination: destination.write(payload.encode()))
        with patch("database.async_session_maker", local_session_maker), \
             patch("test_set_admin_handlers.async_session_maker", local_session_maker), \
             patch("test_set_admin_handlers.render_test_set_details", new=AsyncMock()):
            await handle_questions_upload(message, state, bot)

        assert any("не сохранены" in str(call.args[0]).lower() for call in message.answer.await_args_list)
        async with local_session_maker() as session:
            question = await session.scalar(select(TestQuestion).where(TestQuestion.test_set_id == 102))
            test_set = await session.get(TestSet, 102)
            assert question.text == "Старый вопрос"
            assert json.loads(test_set.formulas_json)[0]["name"] == "broken"
    finally:
        await local_engine.dispose()


@pytest.mark.asyncio
async def test_guided_testset_and_journal_question_editors_save_types_options_and_cancel(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    admin_id = 992003
    try:
        async with local_session_maker() as session:
            session.add(User(id=admin_id, first_name="Админ", is_admin=True, admin_role="content_editor"))
            session.add(TestSet(id=103, title="Редактор", slug="guided_editor"))
            session.add(JournalQuestion(id=1031, text="Как сегодня?", order_num=1, is_active=True))
            await session.commit()

        storage = MemoryStorage()
        state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=admin_id, user_id=admin_id))
        patches = [
            patch("database.async_session_maker", local_session_maker),
            patch("test_set_admin_handlers.async_session_maker", local_session_maker),
            patch("journal_admin_handlers.async_session_maker", local_session_maker),
            patch("test_system_handlers.async_session_maker", local_session_maker),
        ]
        for item in patches:
            item.start()
        try:
            ts_callback = _admin_callback("ts_q_add_103", admin_id)
            await start_guided_question_add(ts_callback, state)
            await receive_guided_question_text(_admin_message("Выбери состояние", admin_id), state)
            await guided_question_type(_admin_callback("ts_q_type_103_0_choice", admin_id), state)
            await receive_guided_question_options(_admin_message("Спокойно\nТревожно", admin_id), state)
            await save_guided_question_custom_answer(_admin_callback("ts_q_custom_103_0_yes", admin_id), state)

            async with local_session_maker() as session:
                question = await session.scalar(select(TestQuestion).where(
                    TestQuestion.test_set_id == 103,
                    TestQuestion.text == "Выбери состояние",
                ))
                assert question.question_type == QUESTION_TYPE_CHOICE
                assert question.allow_custom_answer is True
                assert [item["text"] for item in json.loads(question.answer_options_json)] == ["Спокойно", "Тревожно"]

            await admin_journal_q_type_handler(_admin_callback("j_adm_q_type:1031:0", admin_id), state)
            await admin_journal_q_set_type_handler(_admin_callback("j_adm_q_settype:1031:0:choice", admin_id), state)
            await admin_journal_q_guided_options_submit(_admin_message("Да\nНет", admin_id), state)
            await admin_journal_q_custom_answer_handler(_admin_callback("j_adm_q_custom:1031:0:no", admin_id), state)

            async with local_session_maker() as session:
                journal_question = await session.get(JournalQuestion, 1031)
                assert journal_question.question_type == QUESTION_TYPE_CHOICE
                assert journal_question.allow_custom_answer is False
                assert json.loads(journal_question.answer_options_json)[0]["text"] == "Да"

            await start_guided_question_add(_admin_callback("ts_q_add_103", admin_id), state)
            await receive_guided_question_text(_admin_message("Отменяемый вопрос", admin_id), state)
            await open_test_set_details(_admin_callback("edit_testset_103", admin_id), state)
            async with local_session_maker() as session:
                cancelled = await session.scalar(select(TestQuestion).where(
                    TestQuestion.test_set_id == 103,
                    TestQuestion.text == "Отменяемый вопрос",
                ))
                assert cancelled is None

            bot = AsyncMock()
            await start_universal_test(bot, admin_id, state, test_set_id=103)
            assert "Выбери состояние" in bot.send_message.await_args.args[1]
        finally:
            for item in reversed(patches):
                item.stop()
    finally:
        await local_engine.dispose()


def test_diagnostic_and_profile_sets_require_their_full_active_question_count():
    diagnostics = SimpleNamespace(slug="diagnostics", expected_question_count=7)
    profile = SimpleNamespace(slug="express_psychological_profile", expected_question_count=12)

    assert expected_test_set_question_count(diagnostics) == 7
    assert expected_test_set_question_count(profile) == 12
    assert not is_test_set_ready(diagnostics, 0)
    assert not is_test_set_ready(diagnostics, 6)
    assert is_test_set_ready(diagnostics, 7)
    assert not is_test_set_ready(profile, 11)
    assert is_test_set_ready(profile, 12)


@pytest.mark.asyncio
async def test_empty_and_partial_diagnostic_set_show_setup_screen_and_full_set_starts(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    user_id = 991004
    try:
        async with local_session_maker() as session:
            session.add(TestSet(id=90, title="Диагностика", slug="diagnostics", expected_question_count=7))
            await session.commit()

        bot = AsyncMock()
        state = AsyncMock()
        with patch("test_system_handlers.async_session_maker", local_session_maker):
            assert not await start_universal_test(bot, user_id, state, test_set_id=90, topic_slug="diagnostics")
            assert "Анкета настраивается" in bot.send_message.await_args.args[1]
            state.clear.assert_awaited_once()

            async with local_session_maker() as session:
                session.add_all([
                    TestQuestion(test_set_id=90, text=f"Вопрос {index}", order_num=index)
                    for index in range(1, 7)
                ])
                await session.commit()

            state.reset_mock()
            bot.reset_mock()
            assert not await start_universal_test(bot, user_id, state, test_set_id=90, topic_slug="diagnostics")
            assert "6 из 7" in bot.send_message.await_args.args[1]
            state.clear.assert_awaited_once()

            async with local_session_maker() as session:
                session.add(TestQuestion(test_set_id=90, text="Вопрос 7", order_num=7))
                await session.commit()

            state.reset_mock()
            bot.reset_mock()
            assert await start_universal_test(bot, user_id, state, test_set_id=90, topic_slug="diagnostics")
            state.set_state.assert_awaited_once()
            assert "Вопрос 1 из 7" in bot.send_message.await_args.args[1]
    finally:
        await local_engine.dispose()


@pytest.mark.asyncio
async def test_incomplete_diagnostic_finish_shows_setup_without_generating_profile(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    user_id = 991006
    try:
        async with local_session_maker() as session:
            session.add(User(id=user_id, first_name="Тест"))
            test_set = TestSet(id=93, title="Диагностика", slug="diagnostics", expected_question_count=7)
            session.add(test_set)
            await session.flush()
            attempt = TestAttempt(
                user_id=user_id,
                test_set_id=test_set.id,
                test_set_name=test_set.title,
                test_set_slug=test_set.slug,
                topic_slug="diagnostics",
                purpose="pre_dialogue",
                status="in_progress",
                answers="[]",
            )
            session.add(attempt)
            await session.flush()
            session.add(TestSession(
                user_id=user_id,
                test_set_id=test_set.id,
                attempt_id=attempt.id,
                invocation_topic_slug="diagnostics",
                answers="[]",
                current_question_index=6,
                is_finished=False,
            ))
            session.add_all([
                TestQuestion(test_set_id=test_set.id, text=f"Вопрос {index}", order_num=index)
                for index in range(1, 7)
            ])
            await session.commit()

        bot = AsyncMock()
        state = AsyncMock()
        ai_response = AsyncMock()
        with patch("test_system_handlers.async_session_maker", local_session_maker), \
             patch("test_system_handlers.get_ai_response_for_topic", new=ai_response):
            await _finish_test(bot, user_id, state, [])

        ai_response.assert_not_awaited()
        assert any(
            call.args and "Анкета настраивается" in call.args[1]
            for call in bot.send_message.await_args_list
        )
        state.clear.assert_awaited_once()
        async with local_session_maker() as session:
            saved_attempt = await session.get(TestAttempt, attempt.id)
            saved_session = await session.get(TestSession, user_id)
            assert saved_attempt.status == "in_progress"
            assert saved_session.is_finished is False
    finally:
        await local_engine.dispose()


@pytest.mark.asyncio
async def test_migration_disables_existing_incomplete_express_binding_and_preserves_history(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    user_id = 991005
    try:
        async with local_session_maker() as session:
            session.add_all([
                User(id=user_id, first_name="Тест"),
                Topic(slug="diagnostics", name="Диагностика"),
                Topic(slug="express", name="Экспресс"),
                TestSet(id=1, title="Диагностика", slug="diagnostics", expected_question_count=7),
                TestSet(id=2, title="Старый express", slug="legacy_express", expected_question_count=12),
                TopicDialogueConfig(topic_slug="express", test_set_id=2, pre_dialogue_test_enabled=True),
                TestAttempt(user_id=user_id, test_set_id=2, test_set_name="Старый express", answers='[{"answer":"история"}]'),
            ])
            session.add_all([
                TestQuestion(test_set_id=2, text="Когда ты появился на свет?", variable_name="birth_year", order_num=1),
                TestQuestion(test_set_id=2, text="Какой у тебя тип?", category="human_design", order_num=2),
                TestQuestion(test_set_id=2, text="Число жизненного пути?", category="numerology", order_num=3),
                TestQuestion(test_set_id=2, text="Что важно тебе сегодня?", variable_name="daily_focus", order_num=4),
                TestQuestion(test_set_id=2, text="Ф. И. О.?", order_num=5),
                TestQuestion(test_set_id=2, text="В каком городе ты родился?", order_num=6),
                TestQuestion(test_set_id=2, text="Время твоего рождения?", order_num=7),
                TestQuestion(text="Дата рождения в старом формате?", order_num=8),
            ])
            await session.commit()

            await run_data_migrations(session)

            cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "express"))
            assert cfg.test_set_id == 2
            assert cfg.pre_dialogue_test_enabled is False
            assert (await session.get(TestSet, 2)).is_enabled is True

            legacy_question = await session.scalar(select(TestQuestion).where(TestQuestion.text == "Дата рождения в старом формате?"))
            assert legacy_question.test_set_id == 1
            assert legacy_question.is_active is False

            express_questions = await _questions(session, 2, topic_slug="express")
            assert [question.text for question in express_questions] == ["Что важно тебе сегодня?"]
            diagnostic_questions = await _questions(session, 1, topic_slug="diagnostics")
            assert diagnostic_questions == []

            questions = list((await session.scalars(
                select(TestQuestion).where(TestQuestion.test_set_id == 2).order_by(TestQuestion.order_num)
            )).all())
            assert [question.is_active for question in questions] == [False, False, False, True, False, False, False]
            assert await session.scalar(select(func.count(TestAttempt.id)).where(TestAttempt.test_set_id == 2)) == 1
    finally:
        await local_engine.dispose()


@pytest.mark.asyncio
async def test_migration_keeps_shared_theme_set_enabled_and_blocks_diagnostic_topics(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    user_id = 991007
    try:
        async with local_session_maker() as session:
            session.add_all([
                User(id=user_id, first_name="Тест", current_dialogue_id=1),
                Topic(slug="diagnostics", name="Диагностика"),
                Topic(slug="express", name="Экспресс"),
                Topic(slug="stress", name="Стресс"),
                TestSet(id=101, title="Общий набор", slug="shared_theme_set", is_enabled=True),
                TopicDialogueConfig(topic_slug="diagnostics", test_set_id=101, pre_dialogue_test_enabled=True),
                TopicDialogueConfig(topic_slug="express", test_set_id=101, pre_dialogue_test_enabled=True),
                TopicDialogueConfig(topic_slug="stress", test_set_id=101, pre_dialogue_test_enabled=True),
                TestQuestion(test_set_id=101, text="Что ты чувствуешь сейчас?", variable_name="state", order_num=1),
                TestQuestion(test_set_id=101, text="Что поможет тебе сегодня?", variable_name="support", order_num=2),
            ])
            await session.commit()

            await run_data_migrations(session)
            await run_data_migrations(session)

            shared_set = await session.get(TestSet, 101)
            assert shared_set.is_enabled is True
            assert shared_set.is_archived is False
            diagnostic_cfg = await session.scalar(
                select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "diagnostics")
            )
            express_cfg = await session.scalar(
                select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "express")
            )
            stress_cfg = await session.scalar(
                select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "stress")
            )
            assert diagnostic_cfg.pre_dialogue_test_enabled is False
            assert express_cfg.pre_dialogue_test_enabled is False
            assert stress_cfg.test_set_id == 101
            assert stress_cfg.pre_dialogue_test_enabled is True

        bot = AsyncMock()
        state = AsyncMock()
        with patch("test_system_handlers.async_session_maker", local_session_maker):
            assert await start_universal_test(
                bot,
                user_id,
                state,
                test_set_id=101,
                topic_slug="stress",
                purpose="pre_dialogue",
            )

            state.reset_mock()
            bot.reset_mock()
            assert not await start_universal_test(
                bot,
                user_id,
                state,
                test_set_id=101,
                topic_slug="diagnostics",
            )
            assert "7" in bot.send_message.await_args.args[1]

            state.reset_mock()
            bot.reset_mock()
            assert not await start_universal_test(
                bot,
                user_id,
                state,
                test_set_id=101,
                topic_slug="express",
            )
            assert "12" in bot.send_message.await_args.args[1]
    finally:
        await local_engine.dispose()


@pytest.mark.asyncio
async def test_admin_cannot_enable_or_replace_active_pretest_with_disabled_or_archived_set(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    admin_id = 991008
    try:
        async with local_session_maker() as session:
            session.add_all([
                User(id=admin_id, first_name="Админ", is_admin=True),
                TestSet(id=110, title="Выключенный", slug="disabled_set", is_enabled=False),
                TestSet(id=111, title="Архивный", slug="archived_set", is_enabled=True, is_archived=True),
                TestSet(id=112, title="Текущий", slug="current_set", is_enabled=True),
                TopicDialogueConfig(topic_slug="stress_disabled", test_set_id=110),
                TopicDialogueConfig(topic_slug="stress_archived", test_set_id=111),
                TopicDialogueConfig(topic_slug="stress", test_set_id=112, pre_dialogue_test_enabled=True),
            ])
            await session.commit()

        for slug, expected_text in (
            ("stress_disabled", "выключен"),
            ("stress_archived", "архивирован"),
        ):
            callback = SimpleNamespace(
                from_user=SimpleNamespace(id=admin_id),
                data=f"cfg_toggle_pretest_{slug}",
                answer=AsyncMock(),
                message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            )
            with patch("topic_admin_handlers.async_session_maker", local_session_maker), \
                 patch("topic_admin_handlers._is_admin", new=AsyncMock(return_value=True)):
                await toggle_pre_test(callback)
            assert expected_text in callback.answer.await_args.args[0]
            assert callback.answer.await_args.kwargs["show_alert"] is True

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            data="cfg_set_testset_stress_110",
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        )
        with patch("topic_admin_handlers.async_session_maker", local_session_maker), \
             patch("topic_admin_handlers._is_admin", new=AsyncMock(return_value=True)):
            await set_test_set_selection(callback)
        assert "выключенный" in callback.answer.await_args.args[0]

        async with local_session_maker() as session:
            cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "stress"))
            assert cfg.test_set_id == 112
            assert cfg.pre_dialogue_test_enabled is True
    finally:
        await local_engine.dispose()


@pytest.mark.asyncio
async def test_admin_reordering_is_explicit_and_survives_reload_for_both_editors(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    try:
        async with local_session_maker() as session:
            set_a = TestSet(id=91, title="A", slug="reorder_a")
            set_b = TestSet(id=92, title="B", slug="reorder_b")
            a_questions = [
                TestQuestion(id=910 + index, test_set_id=91, text=f"A{index}", order_num=index)
                for index in range(1, 4)
            ]
            b_question = TestQuestion(id=920, test_set_id=92, text="B1", order_num=1)
            journal_questions = [
                JournalQuestion(id=930 + index, text=f"J{index}", order_num=index)
                for index in range(1, 4)
            ]
            session.add_all([set_a, set_b, b_question, *a_questions, *journal_questions])
            await session.commit()

            ordered_a = reorder_testset_questions(a_questions, 913, 1)
            assert [question.text for question in ordered_a] == ["A3", "A1", "A2"]
            ordered_a = reorder_testset_questions(ordered_a, 911, 3)
            assert [question.text for question in ordered_a] == ["A3", "A2", "A1"]
            journal_questions = reorder_journal_questions(journal_questions, 933, 1)
            assert [question.text for question in journal_questions] == ["J3", "J1", "J2"]
            journal_questions = reorder_journal_questions(journal_questions, 931, 3)
            assert [question.text for question in journal_questions] == ["J3", "J2", "J1"]
            await session.commit()

        async with local_session_maker() as session:
            reloaded_a = list((await session.scalars(
                select(TestQuestion).where(TestQuestion.test_set_id == 91).order_by(TestQuestion.order_num)
            )).all())
            reloaded_b = list((await session.scalars(
                select(TestQuestion).where(TestQuestion.test_set_id == 92).order_by(TestQuestion.order_num)
            )).all())
            reloaded_journal = list((await session.scalars(
                select(JournalQuestion).order_by(JournalQuestion.order_num)
            )).all())
            assert [question.text for question in reloaded_a] == ["A3", "A2", "A1"]
            assert [question.text for question in reloaded_b] == ["B1"]
            assert [question.text for question in reloaded_journal] == ["J3", "J2", "J1"]
            assert [question.order_num for question in reloaded_a] == [1, 2, 3]
            assert [question.order_num for question in reloaded_journal] == [1, 2, 3]
    finally:
        await local_engine.dispose()


def test_esoteric_filter_covers_text_variants_and_stable_metadata():
    questions = [
        SimpleNamespace(text="Ф. И. О.?", variable_name=None, category="general", comment=None),
        SimpleNamespace(text="Когда ты появился на свет?", variable_name=None, category="general", comment=None),
        SimpleNamespace(text="Какой у тебя тип?", variable_name=None, category="human_design", comment=None),
        SimpleNamespace(text="Что говорит число твоего жизненного пути?", variable_name=None, category="general", comment=None),
        SimpleNamespace(text="What is your date of birth?", variable_name=None, category="general", comment=None),
        SimpleNamespace(text="What is your life path number?", variable_name=None, category="general", comment=None),
        SimpleNamespace(text="Что тебе важно сейчас?", variable_name="daily_focus", category="general", comment=None),
    ]
    assert [is_esoteric_question(question) for question in questions] == [True, True, True, True, True, True, False]


@pytest.mark.asyncio
async def test_journal_types_snapshot_skip_and_summary_boundary(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    user_id = 991001
    try:
        async with local_session_maker() as session:
            session.add_all([
                User(id=user_id, first_name="Тест", name="Тест"),
                JournalConfig(id=1, points_per_reflection=10),
                JournalQuestion(id=1, text="Текстовый вопрос", order_num=1, question_type=QUESTION_TYPE_TEXT),
                JournalQuestion(id=2, text="Шкала", order_num=2, question_type=QUESTION_TYPE_SCALE),
                JournalQuestion(
                    id=3,
                    text="Варианты",
                    order_num=3,
                    question_type=QUESTION_TYPE_CHOICE,
                    allow_custom_answer=True,
                    answer_options_json=json.dumps([{"text": "Да", "value": 1}, {"text": "Нет", "value": 0}]),
                ),
            ])
            await session.flush()
            snapshot = await build_questions_snapshot(session)
            entry = JournalEntry(
                user_id=user_id,
                reflection_date=date(2026, 11, 7),
                status="in_progress",
                questions_snapshot_json=json.dumps(snapshot, ensure_ascii=False),
            )
            session.add(entry)
            await session.commit()
            entry_id = entry.id

        assert [item["question_type"] for item in snapshot] == [QUESTION_TYPE_TEXT, QUESTION_TYPE_SCALE, QUESTION_TYPE_CHOICE]
        assert [option["text"] for option in snapshot[1]["options"]] == [str(i) for i in range(1, 11)]
        assert snapshot[2]["allow_custom_answer"] is True

        with patch("journal_handlers.async_session_maker", local_session_maker):
            completed, next_question, _ = await _persist_journal_answer(
                entry_id, user_id, 1, "Сегодня был спокойный день"
            )
            assert not completed and next_question["position"] == 2

            completed, next_question, _ = await _persist_journal_answer(
                entry_id, user_id, 2, is_skipped=True
            )
            assert not completed and next_question["position"] == 3

            completed, next_question, result = await _persist_journal_answer(
                entry_id, user_id, 3, "Да"
            )
            assert completed and next_question is None and result["points_awarded"] == 10

        async with local_session_maker() as session:
            answers = list((await session.scalars(
                select(JournalAnswer).where(JournalAnswer.entry_id == entry_id).order_by(JournalAnswer.position)
            )).all())
            assert answers[1].is_skipped is True
            assert answers[1].answer_text == ""
            prompt, _ = await build_journal_daily_summary_context(session, entry_id, user_id)

        assert "Ответ: Сегодня был спокойный день" in prompt
        assert "Ответ: Да" in prompt
        assert "Пропущенные вопросы (это не ответы пользователя): 2" in prompt
        assert "Ответ: " not in prompt.split("Пропущенные вопросы", 1)[1].split("Не приписывай", 1)[0]
        assert "не заполняй их догадками" in prompt
    finally:
        await local_engine.dispose()


@pytest.mark.asyncio
async def test_journal_skip_callback_persists_state_and_advances(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    user_id = 991002
    try:
        async with local_session_maker() as session:
            session.add(User(id=user_id, first_name="Тест", name="Тест"))
            session.add_all([
                JournalQuestion(id=11, text="Первый", order_num=1, question_type=QUESTION_TYPE_TEXT),
                JournalQuestion(id=12, text="Второй", order_num=2, question_type=QUESTION_TYPE_TEXT),
            ])
            await session.flush()
            snapshot = await build_questions_snapshot(session)
            entry = JournalEntry(
                user_id=user_id,
                reflection_date=date(2026, 11, 6),
                status="in_progress",
                questions_snapshot_json=json.dumps(snapshot, ensure_ascii=False),
            )
            session.add(entry)
            await session.commit()
            entry_id = entry.id

        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
        await state.set_state(JournalStates.answering_question)
        await state.update_data(entry_id=entry_id, position=1, total=2)
        callback = SimpleNamespace(
            data=f"j_skip:{entry_id}:1",
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=user_id),
                edit_reply_markup=AsyncMock(),
                answer=AsyncMock(),
            ),
        )

        with patch("journal_handlers.async_session_maker", local_session_maker):
            await cb_journal_skip(callback, state, AsyncMock())

        callback.answer.assert_awaited_once()
        data = await state.get_data()
        assert data["position"] == 2
        async with local_session_maker() as session:
            answer = await session.scalar(select(JournalAnswer).where(JournalAnswer.entry_id == entry_id))
            assert answer.is_skipped is True
    finally:
        await local_engine.dispose()


@pytest.mark.asyncio
async def test_universal_exit_keeps_resumable_session_and_attempt(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    user_id = 991003
    try:
        async with local_session_maker() as session:
            session.add(User(id=user_id, first_name="Тест", name="Тест"))
            test_set = TestSet(id=71, title="Набор", slug="v3_exit_set")
            session.add(test_set)
            await session.flush()
            attempt = TestAttempt(
                user_id=user_id,
                test_set_id=test_set.id,
                test_set_name=test_set.title,
                test_set_slug=test_set.slug,
                answers='[{"answer": "первый"}]',
                status="in_progress",
            )
            session.add(attempt)
            await session.flush()
            session.add(TestSession(
                user_id=user_id,
                test_set_id=test_set.id,
                attempt_id=attempt.id,
                answers='[{"answer": "первый"}]',
                current_question_index=1,
                is_finished=False,
            ))
            await session.commit()

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id)),
        )
        state = AsyncMock()
        bot = AsyncMock()
        with patch("test_system_handlers.async_session_maker", local_session_maker), \
             patch("handlers.send_main_menu", new=AsyncMock()) as send_main_menu:
            await exit_universal_test(callback, state, bot)

        state.clear.assert_awaited_once()
        send_main_menu.assert_awaited_once()
        async with local_session_maker() as session:
            saved_session = await session.get(TestSession, user_id)
            saved_attempt = await session.get(TestAttempt, 1)
            assert saved_session.is_finished is False
            assert saved_session.current_question_index == 1
            assert json.loads(saved_attempt.answers)[0]["answer"] == "первый"
    finally:
        await local_engine.dispose()


@pytest.mark.asyncio
async def test_testset_editor_and_runtime_are_isolated_by_set(tmp_path):
    local_engine, local_session_maker = await _local_database(tmp_path)
    try:
        async with local_session_maker() as session:
            set_a = TestSet(id=81, title="A", slug="v3_set_a")
            set_b = TestSet(id=82, title="B", slug="v3_set_b")
            q_a = TestQuestion(
                id=811,
                test_set_id=81,
                text="Старый вопрос A",
                order_num=1,
                question_type=QUESTION_TYPE_CHOICE,
                answer_options_json=json.dumps([{"text": "A", "value": 1}]),
            )
            q_b = TestQuestion(
                id=821,
                test_set_id=82,
                text="Вопрос B",
                order_num=1,
                question_type=QUESTION_TYPE_TEXT,
                is_active=True,
            )
            session.add_all([set_a, set_b, q_a, q_b])
            await session.commit()

            payload = _question_edit_payload(
                json.dumps({
                    "text": "Обновлённый вопрос A",
                    "type": "choice",
                    "options": [{"text": "Новый вариант", "value": 2}],
                    "allow_custom_answer": True,
                    "order": 1,
                }, ensure_ascii=False),
                q_a,
            )
            q_a.text = payload["text"]
            q_a.question_type = payload["question_type"]
            q_a.answer_options_json = payload["answer_options_json"]
            q_a.allow_custom_answer = payload["allow_custom_answer"]
            await session.commit()

            questions_a = await _questions(session, set_a.id)
            questions_b = await _questions(session, set_b.id)

        assert [question.text for question in questions_a] == ["Обновлённый вопрос A"]
        assert json.loads(questions_a[0].answer_options_json)[0]["text"] == "Новый вариант"
        assert questions_a[0].allow_custom_answer is True
        assert [question.text for question in questions_b] == ["Вопрос B"]
    finally:
        await local_engine.dispose()
