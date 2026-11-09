import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import automation_admin
import automation_events
import funnel
import handlers
from ai_integration import StructuredAIResponse, get_ai_response_for_topic
from automation_engine import AutomationHandler
from crisis_safety import (
    automatic_send_allowed,
    check_crisis_markers,
    crisis_contacts_keyboard,
    find_crisis_marker,
    serialize_crisis_markers,
)
from database import AIConfig, BotConfig, User, _ensure_sqlite_compat_schema, async_session_maker, init_db


class RecordingBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, message_text, **kwargs):
        self.sent.append((chat_id, message_text, kwargs))
        return SimpleNamespace(message_id=len(self.sent), chat=SimpleNamespace(id=chat_id))


def _fsm(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=779, chat_id=user_id, user_id=user_id),
    )


def _callback(user_id: int, data: str):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=user_id),
        message_id=1,
        edit_text=AsyncMock(),
        answer=AsyncMock(),
    )
    callback = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, first_name="Admin"),
        message=message,
        answer=AsyncMock(),
    )
    return callback


def _message(user_id: int, text_value: str):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, first_name="Admin"),
        chat=SimpleNamespace(id=user_id),
        text=text_value,
        answer=AsyncMock(),
    )


async def _ensure_user(user_id: int, **values):
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None:
            user = User(id=user_id, first_name=f"Marker user {user_id}", name=f"Marker user {user_id}")
            session.add(user)
        for key, value in values.items():
            setattr(user, key, value)
        await session.commit()


async def _set_marker_config(enabled: bool, markers: list[str]):
    async with async_session_maker() as session:
        config = await session.get(BotConfig, 1)
        config.crisis_marker_protection_enabled = enabled
        config.crisis_markers_json = serialize_crisis_markers(markers)
        await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def preserve_crisis_marker_config():
    await init_db()
    async with async_session_maker() as session:
        config = await session.get(BotConfig, 1)
        previous_enabled = config.crisis_marker_protection_enabled
        previous_markers = config.crisis_markers_json
    yield
    async with async_session_maker() as session:
        config = await session.get(BotConfig, 1)
        config.crisis_marker_protection_enabled = previous_enabled
        config.crisis_markers_json = previous_markers
        await session.commit()


@pytest.mark.asyncio
async def test_crisis_marker_matcher_uses_phrases_context_and_negation():
    assert find_crisis_marker("Я ХОЧУ, покончить с собой!", ["хочу покончить с собой"]) == "хочу покончить с собой"
    assert find_crisis_marker("Я не хочу покончить с собой", ["хочу покончить с собой"]) is None
    assert find_crisis_marker("В статье обсуждается суицидальный риск", ["суицид"]) is None
    assert find_crisis_marker("Я планирую суицид", ["суицид"]) == "суицид"
    assert find_crisis_marker("Не вижу смысла продолжать. Сил больше нет", ["другой маркер"]) == (
        "не вижу смысла продолжать сил больше нет"
    )
    assert find_crisis_marker("Не вижу смысла продолжать разговор о проекте") is None
    assert find_crisis_marker("Не вижу смысла продолжать разговор. Сил больше нет") is None
    assert find_crisis_marker("Сил больше нет после тренировки") is None


@pytest.mark.asyncio
async def test_crisis_marker_admin_screen_requires_system_permission_and_persists_edits():
    await init_db()
    admin_id = 999999999
    editor_id = 98261001
    await _ensure_user(editor_id, is_admin=True, admin_role="content_editor")
    await _set_marker_config(True, ["первый маркер"])

    forbidden = _callback(editor_id, "automation_crisis_markers")
    await automation_admin.automation_crisis_markers(forbidden, _fsm(editor_id))
    forbidden.answer.assert_awaited_once_with("⛔️ Доступ запрещён.", show_alert=True)
    forbidden.message.edit_text.assert_not_awaited()

    screen = _callback(admin_id, "automation_crisis_markers")
    await automation_admin.automation_crisis_markers(screen, _fsm(admin_id))
    screen.message.edit_text.assert_awaited_once()
    screen_text = screen.message.edit_text.call_args.args[0]
    screen_markup = screen.message.edit_text.call_args.kwargs["reply_markup"]
    assert "Кризисная защита по маркерам" in screen_text
    callbacks = [button.callback_data for row in screen_markup.inline_keyboard for button in row]
    assert "automation_crisis_add" in callbacks
    assert "automation_crisis_edit_0" in callbacks
    assert "automation_crisis_delete_0" in callbacks

    toggle = _callback(admin_id, "automation_crisis_toggle")
    await automation_admin.automation_crisis_toggle(toggle)
    async with async_session_maker() as session:
        config = await session.get(BotConfig, 1)
        assert config.crisis_marker_protection_enabled is False
        assert await check_crisis_markers(session, editor_id, "первый маркер") is None

    add_callback = _callback(admin_id, "automation_crisis_add")
    add_state = _fsm(admin_id)
    await automation_admin.automation_crisis_add(add_callback, add_state)
    await automation_admin.process_crisis_marker_add_input(_message(admin_id, "добавленный маркер"), add_state)

    edit_callback = _callback(admin_id, "automation_crisis_edit_0")
    edit_state = _fsm(admin_id)
    await automation_admin.automation_crisis_edit(edit_callback, edit_state)
    await automation_admin.process_crisis_marker_edit_input(_message(admin_id, "изменённый маркер"), edit_state)

    delete_callback = _callback(admin_id, "automation_crisis_delete_1")
    await automation_admin.automation_crisis_delete(delete_callback)
    async with async_session_maker() as session:
        config = await session.get(BotConfig, 1)
        assert config.crisis_marker_protection_enabled is False
        assert json.loads(config.crisis_markers_json) == ["измененный маркер"]


@pytest.mark.asyncio
async def test_crisis_marker_detection_is_model_independent_and_idempotent():
    await init_db()
    user_id = 98261002
    recipient_id = 98261003
    await _ensure_user(user_id, crisis_detected_at=None, username="marker_user")
    await _set_marker_config(True, ["хочу покончить с собой"])

    async with async_session_maker() as session:
        handler = AutomationHandler(
            name="Crisis marker regression alert",
            scope="global",
            event_name="crisis_detected",
            action_type="send_admin_message",
            action_payload_json=json.dumps({"text": "marker {user_id} {crisis_marker}"}),
            recipient_ids_json=json.dumps([recipient_id]),
            is_active=1,
        )
        session.add(handler)
        await session.commit()
        ai_config = await session.get(AIConfig, 1)

    bot = RecordingBot()
    model_response = StructuredAIResponse(
        text="Модель не добавила кризисный тег.",
        raw_payload={},
        http_status=None,
        finish_reason="stop",
    )

    async def fake_prepare_clients(session):
        return ai_config, None, object(), "marker-test-model", None

    with patch("ai_integration._prepare_clients", new=AsyncMock(side_effect=fake_prepare_clients)), \
         patch("ai_integration._generate_chat_response", new=AsyncMock(return_value=model_response)):
        answer = await get_ai_response_for_topic(
            user_id,
            "Я хочу покончить с собой",
            "Пользователь",
            topic_slug="marker-regression",
            dialogue_id=1,
            bot=bot,
            chat_id=user_id,
        )

    assert answer == "Модель не добавила кризисный тег."
    await handlers.send_ai_response(user_id, answer, bot, None, crisis_contacts_keyboard())
    await automation_events.wait_for_pending_automation_events()

    with patch("ai_integration._prepare_clients", new=AsyncMock(side_effect=fake_prepare_clients)), \
         patch("ai_integration._generate_chat_response", new=AsyncMock(return_value=model_response)):
        answer_again = await get_ai_response_for_topic(
            user_id,
            "Я хочу покончить с собой",
            "Пользователь",
            topic_slug="marker-regression",
            dialogue_id=1,
            bot=bot,
            chat_id=user_id,
        )

    await handlers.send_ai_response(user_id, answer_again, bot, None, crisis_contacts_keyboard())
    await automation_events.wait_for_pending_automation_events()

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        assert user.crisis_detected_at is not None
        assert await automatic_send_allowed(session, user_id, "marker-regression") is False

    user_messages = [item for item in bot.sent if item[0] == user_id]
    contact_messages = [
        item for item in user_messages
        if item[2].get("reply_markup") and item[2]["reply_markup"].inline_keyboard
    ]
    alert_messages = [item for item in bot.sent if item[0] == recipient_id]
    assert len(contact_messages) == 1
    assert len(alert_messages) == 1
    assert all(item[2].get("reply_markup") is None for item in user_messages if item not in contact_messages)


@pytest.mark.asyncio
async def test_funnel_marker_reaches_funnel_handler_for_later_dialogue():
    await init_db()
    user_id = 98261011
    recipient_id = 98261012
    await _ensure_user(user_id, crisis_detected_at=None, username="funnel_marker_user")
    await _set_marker_config(True, ["хочу покончить с собой"])

    async with async_session_maker() as session:
        handler = AutomationHandler(
            name="Funnel marker routing regression",
            scope="funnel",
            topic_slug="funnel_demo",
            event_name="crisis_detected",
            action_type="send_admin_message",
            action_payload_json=json.dumps({"text": "funnel marker {user_id}"}),
            recipient_ids_json=json.dumps([recipient_id]),
            is_active=1,
        )
        session.add(handler)
        await session.commit()
        ai_config = await session.get(AIConfig, 1)

    bot = RecordingBot()
    model_response = StructuredAIResponse(
        text="Модель не определила кризис.",
        raw_payload={},
        http_status=None,
        finish_reason="stop",
    )

    async def fake_prepare_clients(session):
        return ai_config, None, object(), "funnel-marker-model", None

    with patch("ai_integration._prepare_clients", new=AsyncMock(side_effect=fake_prepare_clients)), \
         patch("ai_integration._generate_chat_response", new=AsyncMock(return_value=model_response)):
        answer = await get_ai_response_for_topic(
            user_id,
            "Я хочу покончить с собой",
            "Пользователь",
            topic_slug="funnel_demo",
            dialogue_id=7,
            bot=bot,
            chat_id=user_id,
        )

    await automation_events.wait_for_pending_automation_events()

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        assert user.crisis_detected_at is not None
        assert await automatic_send_allowed(session, user_id, "funnel_demo") is False
    assert answer == "Модель не определила кризис."
    assert len([item for item in bot.sent if item[0] == recipient_id]) == 1


@pytest.mark.asyncio
async def test_funnel_marker_and_model_events_share_one_alert_and_keep_contacts():
    await init_db()
    user_id = 98261013
    recipient_id = 98261014
    await _ensure_user(user_id, crisis_detected_at=None, username="funnel_duplicate_user")
    await _set_marker_config(True, ["хочу покончить с собой"])

    async with async_session_maker() as session:
        handler = AutomationHandler(
            name="Funnel crisis deduplication regression",
            scope="global",
            event_name="crisis_detected",
            action_type="send_admin_message",
            action_payload_json=json.dumps({"text": "crisis {user_id}"}),
            recipient_ids_json=json.dumps([recipient_id]),
            is_active=1,
        )
        session.add(handler)
        await session.commit()
        ai_config = await session.get(AIConfig, 1)

    bot = RecordingBot()
    model_response = StructuredAIResponse(
        text=(
            '{"mode":"crisis","has_experience":false,"has_question":false,'
            '"is_skeptic":false,"is_empty":false,"show_offer":false,'
            '"reply":"Я рядом.","bridge":""}'
            '<DATA>{"events":["crisis_detected"]}</DATA>'
        ),
        raw_payload={},
        http_status=None,
        finish_reason="stop",
    )

    async def fake_prepare_clients(session):
        return ai_config, None, object(), "funnel-dedup-model", None

    with patch("ai_integration._prepare_clients", new=AsyncMock(side_effect=fake_prepare_clients)), \
         patch("ai_integration._generate_chat_response", new=AsyncMock(return_value=model_response)):
        answer = await get_ai_response_for_topic(
            user_id,
            "Я хочу покончить с собой",
            "Пользователь",
            topic_slug="funnel_demo",
            dialogue_id=9,
            bot=bot,
            chat_id=user_id,
        )

    await funnel.render_funnel_analysis(
        bot=bot,
        user_id=user_id,
        chat_id=user_id,
        raw_response=answer,
        reflection_text="Я хочу покончить с собой",
        meditation_id=None,
    )
    await automation_events.wait_for_pending_automation_events()

    alert_messages = [item for item in bot.sent if item[0] == recipient_id]
    user_messages = [item for item in bot.sent if item[0] == user_id]
    assert len(alert_messages) == 1
    assert len(user_messages) == 2
    assert user_messages[0][2].get("reply_markup") is None
    assert user_messages[1][2].get("reply_markup") is not None

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        assert user.crisis_detected_at is not None
        assert await automatic_send_allowed(session, user_id, "funnel_demo") is False


@pytest.mark.asyncio
async def test_funnel_crisis_after_manual_clear_gets_new_alert():
    await init_db()
    user_id = 98261015
    recipient_id = 98261016
    await _ensure_user(user_id, crisis_detected_at=None, username="funnel_repeat_user")
    await _set_marker_config(True, ["хочу покончить с собой"])

    async with async_session_maker() as session:
        handler = AutomationHandler(
            name="Funnel repeated crisis regression",
            scope="funnel",
            topic_slug="funnel_demo",
            event_name="crisis_detected",
            action_type="send_admin_message",
            action_payload_json=json.dumps({"text": "crisis {user_id}"}),
            recipient_ids_json=json.dumps([recipient_id]),
            is_active=1,
        )
        session.add(handler)
        await session.commit()
        ai_config = await session.get(AIConfig, 1)

    bot = RecordingBot()
    model_response = StructuredAIResponse(
        text="Модель не определила кризис.",
        raw_payload={},
        http_status=None,
        finish_reason="stop",
    )

    async def fake_prepare_clients(session):
        return ai_config, None, object(), "funnel-repeat-model", None

    async def run_crisis_turn(dialogue_id: int):
        with patch("ai_integration._prepare_clients", new=AsyncMock(side_effect=fake_prepare_clients)), \
             patch("ai_integration._generate_chat_response", new=AsyncMock(return_value=model_response)):
            return await get_ai_response_for_topic(
                user_id,
                "Я хочу покончить с собой",
                "Пользователь",
                topic_slug="funnel_demo",
                dialogue_id=dialogue_id,
                bot=bot,
                chat_id=user_id,
            )

    await run_crisis_turn(11)
    await automation_events.wait_for_pending_automation_events()

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        first_episode = user.crisis_detected_at
        assert first_episode is not None
        user.crisis_detected_at = None
        await session.commit()

    await run_crisis_turn(12)
    await automation_events.wait_for_pending_automation_events()

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        assert user.crisis_detected_at is not None
        assert user.crisis_detected_at != first_episode
        assert await automatic_send_allowed(session, user_id, "funnel_demo") is False

    assert len([item for item in bot.sent if item[0] == recipient_id]) == 2


@pytest.mark.asyncio
async def test_sqlite_compat_migration_preserves_bot_config_and_adds_marker_columns(tmp_path):
    legacy_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_crisis_markers.sqlite'}")
    try:
        async with legacy_engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE bot_config ("
                "id INTEGER PRIMARY KEY, main_first_reminder_hours INTEGER NOT NULL, "
                "main_next_reminder_hours INTEGER NOT NULL, main_reminders_enabled BOOLEAN NOT NULL)"
            ))
            await conn.execute(text(
                "INSERT INTO bot_config (id, main_first_reminder_hours, main_next_reminder_hours, main_reminders_enabled) "
                "VALUES (1, 24, 48, 1)"
            ))
            await _ensure_sqlite_compat_schema(conn)
            columns = await conn.execute(text("PRAGMA table_info(bot_config)"))
            column_names = {row[1] for row in columns.fetchall()}
            row = (await conn.execute(text(
                "SELECT main_first_reminder_hours, main_next_reminder_hours, "
                "crisis_marker_protection_enabled, crisis_markers_json FROM bot_config WHERE id = 1"
            ))).one()
        assert "crisis_marker_protection_enabled" in column_names
        assert "crisis_markers_json" in column_names
        assert row[0:2] == (24, 48)
        assert row[2] == 1
        assert "хочу покончить с собой" in row[3]
    finally:
        await legacy_engine.dispose()
