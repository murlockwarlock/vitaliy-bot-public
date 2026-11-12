import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine

from database import (
    AIConfig,
    Base,
    Mailing,
    User,
    _ensure_sqlite_compat_schema,
    async_session_maker,
    init_db,
)
import automation_admin
import automation_events
import handlers
from ai_integration import StructuredAIResponse, get_ai_response_for_topic, get_standalone_ai_response
from automation_engine import AutomationActionExecution, AutomationHandler
from automation_events import dispatch_automation_events, wait_for_pending_automation_events
from crisis_safety import automatic_send_allowed, crisis_contacts_keyboard
from followups import FollowupStep, send_followup_step_to_user
from mailing_service import send_mailing_to_recipient
from time_utils import msk_now


class RecordingBot:
    def __init__(self, failed_ids=None):
        self.sent = []
        self.failed_ids = set(failed_ids or ())

    async def send_message(self, chat_id, message_text, **kwargs):
        if chat_id in self.failed_ids:
            raise RuntimeError(f"recipient unavailable: {chat_id}")
        self.sent.append((chat_id, message_text, kwargs))
        return SimpleNamespace(message_id=len(self.sent), chat=SimpleNamespace(id=chat_id))


def _fsm(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=777, chat_id=user_id, user_id=user_id),
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
    callback.model_copy = lambda update: _callback(user_id, update["data"])
    return callback


async def _ensure_user(user_id: int, **values) -> None:
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None:
            user = User(id=user_id, first_name=f"Client {user_id}", name=f"Client {user_id}")
            session.add(user)
        for key, value in values.items():
            setattr(user, key, value)
        await session.commit()


@pytest.mark.asyncio
async def test_ai_data_crisis_sets_flag_before_response_and_alerts_selected_recipients():
    await init_db()
    user_id = 98130001
    recipient_ids = {98130101, 98130102}
    legacy_recipient_id = 98130105
    topic_slug = f"crisis-alert-topic-{user_id}"
    user_text = "Не вижу смысла <продолжать>."
    await _ensure_user(
        user_id,
        username="crisis_user",
        first_name="Кризис",
        name="Кризис Клиент",
        crisis_detected_at=None,
    )

    async with async_session_maker() as session:
        handler = AutomationHandler(
            name="V3 crisis alert",
            scope="topic",
            topic_slug=topic_slug,
            event_name="crisis_detected",
            action_type="send_admin_message",
            action_payload_json=json.dumps({
                "text": "{event} {user_id} {username} {first_name} {user_text} {practice} {topic} {time}",
                "recipient_ids": [legacy_recipient_id],
            }),
            recipient_ids_json=json.dumps(sorted(recipient_ids)),
            is_active=1,
        )
        session.add(handler)
        await session.commit()
        ai_config = await session.get(AIConfig, 1)

    bot = RecordingBot()
    model_response = StructuredAIResponse(
        text=(
            "Я слышу тебя."
            '<DATA>{"events":["crisis_detected"],"metadata":'
            '{"practice":"Тестовая практика","topic":"Тема поддержки"}}</DATA>'
        ),
        raw_payload={"test": True},
        http_status=None,
        finish_reason="stop",
    )

    async def fake_prepare_clients(session):
        return ai_config, None, object(), "test-model", None

    with patch("ai_integration._prepare_clients", new=AsyncMock(side_effect=fake_prepare_clients)), \
         patch("ai_integration._generate_chat_response", new=AsyncMock(return_value=model_response)):
        answer = await get_ai_response_for_topic(
            user_id,
            user_text,
            "Кризис",
            topic_slug=topic_slug,
            dialogue_id=1,
            bot=bot,
            chat_id=user_id,
            alert_user_text=user_text,
            alert_metadata={"practice": "Практика из запроса"},
        )

    assert answer == "Я слышу тебя."
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        assert user.crisis_detected_at is not None

    await handlers.send_ai_response(user_id, answer, bot, None, crisis_contacts_keyboard())
    await wait_for_pending_automation_events()

    user_messages = [item for item in bot.sent if item[0] == user_id]
    alert_messages = [item for item in bot.sent if item[0] in recipient_ids]
    assert len(user_messages) == 2
    assert user_messages[0][2]["reply_markup"] is None
    contacts_markup = user_messages[1][2]["reply_markup"]
    assert [button.text for row in contacts_markup.inline_keyboard for button in row] == ["Главное меню"]
    assert {item[0] for item in alert_messages} == recipient_ids
    assert legacy_recipient_id not in [item[0] for item in bot.sent]
    assert "&lt;продолжать&gt;" in alert_messages[0][1]
    assert "Тестовая практика" in alert_messages[0][1]
    assert "Тема поддержки" in alert_messages[0][1]


@pytest.mark.asyncio
async def test_ordinary_topic_data_crisis_runs_safety_flow_and_hides_data():
    await init_db()
    user_id = 98130007
    recipient_id = 98130107
    topic_slug = f"ordinary-crisis-topic-{user_id}"
    await _ensure_user(user_id, crisis_detected_at=None, username="ordinary_crisis_user")

    async with async_session_maker() as session:
        handler = AutomationHandler(
            name="Ordinary topic crisis alert",
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
        text='Я рядом.<DATA>{"events":["crisis_detected"]}</DATA>',
        raw_payload={},
        http_status=None,
        finish_reason="stop",
    )

    async def fake_prepare_clients(session):
        return ai_config, None, object(), "ordinary-crisis-model", None

    with patch("ai_integration._prepare_clients", new=AsyncMock(side_effect=fake_prepare_clients)), \
         patch("ai_integration._generate_chat_response", new=AsyncMock(return_value=model_response)):
        answer = await get_ai_response_for_topic(
            user_id,
            "Обычный вопрос по теме",
            "Пользователь",
            topic_slug=topic_slug,
            dialogue_id=17,
            bot=bot,
            chat_id=user_id,
        )

    assert answer == "Я рядом."
    assert "<DATA>" not in answer
    await wait_for_pending_automation_events()

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        assert user.crisis_detected_at is not None
        assert await automatic_send_allowed(session, user_id, "ordinary-topic") is False
        mailing_result = await send_mailing_to_recipient(
            bot,
            user_id,
            Mailing(text="Автоматическое сообщение"),
            session=session,
        )
        followup_result = await send_followup_step_to_user(
            bot,
            session,
            step=FollowupStep(campaign_id=user_id, step_number=1, message_text="Авто follow-up"),
            user=user,
            dialogue_id=17,
        )

    assert mailing_result == (False, "skipped", "crisis_flag")
    assert followup_result == (False, "skipped", "crisis_flag")

    await handlers.send_ai_response(user_id, answer, bot, None, crisis_contacts_keyboard())
    await wait_for_pending_automation_events()

    user_messages = [item for item in bot.sent if item[0] == user_id]
    contact_messages = [
        item for item in user_messages
        if item[2].get("reply_markup") and item[2]["reply_markup"].inline_keyboard
    ]
    alert_messages = [item for item in bot.sent if item[0] == recipient_id]
    assert len(alert_messages) == 1
    assert len(user_messages) == 2
    assert user_messages[0][1] == "Я рядом."
    assert user_messages[0][2].get("reply_markup") is None
    assert len(contact_messages) == 1
    assert [
        button.text
        for row in contact_messages[0][2]["reply_markup"].inline_keyboard
        for button in row
    ] == ["Главное меню"]


@pytest.mark.asyncio
async def test_clinical_data_only_alerts_without_crisis_flag_or_contacts():
    await init_db()
    user_id = 98130008
    clinical_recipient_id = 98130108
    crisis_recipient_id = 98130109
    topic_slug = f"ordinary-clinical-topic-{user_id}"
    await _ensure_user(user_id, crisis_detected_at=None, username="clinical_user")

    async with async_session_maker() as session:
        session.add_all([
            AutomationHandler(
                name="Clinical-only alert",
                scope="global",
                event_name="clinical_detected",
                action_type="send_admin_message",
                action_payload_json=json.dumps({"text": "clinical {user_id}"}),
                recipient_ids_json=json.dumps([clinical_recipient_id]),
                is_active=1,
            ),
            AutomationHandler(
                name="Unrelated crisis alert",
                scope="global",
                event_name="crisis_detected",
                action_type="send_admin_message",
                action_payload_json=json.dumps({"text": "crisis {user_id}"}),
                recipient_ids_json=json.dumps([crisis_recipient_id]),
                is_active=1,
            ),
        ])
        await session.commit()
        ai_config = await session.get(AIConfig, 1)

    bot = RecordingBot()
    model_response = StructuredAIResponse(
        text='Нужна бережная поддержка.<DATA>{"events":["clinical_detected"]}</DATA>',
        raw_payload={},
        http_status=None,
        finish_reason="stop",
    )

    async def fake_prepare_clients(session):
        return ai_config, None, object(), "clinical-model", None

    with patch("ai_integration._prepare_clients", new=AsyncMock(side_effect=fake_prepare_clients)), \
         patch("ai_integration._generate_chat_response", new=AsyncMock(return_value=model_response)):
        answer = await get_ai_response_for_topic(
            user_id,
            "Обычный вопрос без кризисного содержания",
            "Пользователь",
            topic_slug=topic_slug,
            dialogue_id=18,
            bot=bot,
            chat_id=user_id,
        )

    assert answer == "Нужна бережная поддержка."
    assert "<DATA>" not in answer
    await wait_for_pending_automation_events()

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        assert user.crisis_detected_at is None
        assert await automatic_send_allowed(session, user_id, "clinical-topic") is True

    await handlers.send_ai_response(user_id, answer, bot, None)
    clinical_alerts = [item for item in bot.sent if item[0] == clinical_recipient_id]
    crisis_alerts = [item for item in bot.sent if item[0] == crisis_recipient_id]
    user_messages = [item for item in bot.sent if item[0] == user_id]
    assert len(clinical_alerts) == 1
    assert crisis_alerts == []
    assert len(user_messages) == 1
    assert user_messages[0][2].get("reply_markup") is None


@pytest.mark.asyncio
async def test_standalone_data_is_stripped_and_crisis_event_is_processed():
    await init_db()
    user_id = 98130009
    recipient_id = 98130110
    await _ensure_user(user_id, crisis_detected_at=None, username="standalone_crisis_user")

    async with async_session_maker() as session:
        handler = AutomationHandler(
            name="Standalone crisis alert",
            scope="global",
            event_name="crisis_detected",
            action_type="send_admin_message",
            action_payload_json=json.dumps({"text": "standalone crisis {user_id}"}),
            recipient_ids_json=json.dumps([recipient_id]),
            is_active=1,
        )
        session.add(handler)
        await session.commit()
        ai_config = await session.get(AIConfig, 1)

    bot = RecordingBot()
    model_response = StructuredAIResponse(
        text='Интерпретация.<DATA>{"events":["crisis_detected"]}</DATA>',
        raw_payload={},
        http_status=None,
        finish_reason="stop",
    )

    async def fake_prepare_clients(session):
        return ai_config, None, object(), "standalone-model", None

    with patch("ai_integration._prepare_clients", new=AsyncMock(side_effect=fake_prepare_clients)), \
         patch("ai_integration._generate_chat_response", new=AsyncMock(return_value=model_response)):
        answer = await get_standalone_ai_response(
            "Ты бережный помощник.",
            "Интерпретируй результаты.",
            bot=bot,
            chat_id=user_id,
            user_id=user_id,
        )

    assert answer == "Интерпретация."
    assert "<DATA>" not in answer
    await wait_for_pending_automation_events()

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        assert user.crisis_detected_at is not None
    assert len([item for item in bot.sent if item[0] == recipient_id]) == 1


@pytest.mark.asyncio
async def test_safety_alert_delivery_is_partial_and_fails_closed_without_recipients():
    await init_db()
    user_id = 98130002
    good_id = 98130103
    bad_id = 98130104
    legacy_id = 98130105
    await _ensure_user(user_id, username="alert_user")
    bot = RecordingBot(failed_ids={bad_id})

    async with async_session_maker() as session:
        partial = AutomationHandler(
            name="Partial clinical alert",
            scope="global",
            event_name="clinical_detected",
            action_type="send_admin_message",
            action_payload_json=json.dumps({"text": "clinical {user_id}"}),
            recipient_ids_json=json.dumps([good_id, bad_id]),
            is_active=1,
        )
        no_recipients = AutomationHandler(
            name="Unconfigured parse alert",
            scope="global",
            event_name="parse_failed",
            action_type="send_admin_message",
            action_payload_json=json.dumps({"text": "parse {user_id}", "recipient_ids": [legacy_id]}),
            recipient_ids_json=json.dumps([]),
            is_active=1,
        )
        session.add_all([partial, no_recipients])
        await session.commit()

        executed = await dispatch_automation_events(
            bot,
            session,
            user_id=user_id,
            events=["clinical_detected", "parse_failed"],
        )
        assert partial.id in executed
        assert no_recipients.id not in executed

        partial_execution = await session.scalar(
            select(AutomationActionExecution).where(
                AutomationActionExecution.handler_id == partial.id,
                AutomationActionExecution.user_id == user_id,
            )
        )
        failed_execution = await session.scalar(
            select(AutomationActionExecution).where(
                AutomationActionExecution.handler_id == no_recipients.id,
                AutomationActionExecution.user_id == user_id,
            )
        )
        assert partial_execution.status == "delivered"
        assert failed_execution.status == "failed"

    assert [item[0] for item in bot.sent] == [good_id]


@pytest.mark.asyncio
async def test_crisis_flag_blocks_mailing_and_generic_followup():
    await init_db()
    user_id = 98130003
    await _ensure_user(user_id, crisis_detected_at=msk_now())
    bot = RecordingBot()
    mailing = Mailing(text="Запрещенное автоматическое сообщение")
    step = FollowupStep(campaign_id=98130003, step_number=1, message_text="Запрещенный follow-up")

    async with async_session_maker() as session:
        mailing_result = await send_mailing_to_recipient(bot, user_id, mailing, session=session)
        followup_result = await send_followup_step_to_user(
            bot,
            session,
            step=step,
            user=await session.get(User, user_id),
            dialogue_id=1,
        )

    assert mailing_result == (False, "skipped", "crisis_flag")
    assert followup_result == (False, "skipped", "crisis_flag")
    assert bot.sent == []


@pytest.mark.asyncio
async def test_client_list_is_one_column_and_crisis_card_is_admin_only():
    await init_db()
    admin_id = 999999999
    client_id = 98130004
    crisis_client_id = 98130005
    await _ensure_user(client_id, first_name="v3-client-one", name="v3-client-one", created_at=datetime(2026, 1, 1))
    await _ensure_user(
        crisis_client_id,
        first_name="v3-client-two",
        name="v3-client-two",
        created_at=datetime(2026, 1, 2),
        crisis_detected_at=msk_now(),
    )

    state = _fsm(admin_id)
    await state.update_data(client_search_query="v3-client")
    with patch("handlers.send_smart_content", new=AsyncMock()) as send_content:
        await handlers.admin_clients_list(_callback(admin_id, "admin_clients_list_0"), state)
        markup = send_content.call_args.args[3]

    client_buttons = [
        button
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("admin_user_profile_")
    ]
    assert len(client_buttons) == 2
    client_rows = [row for row in markup.inline_keyboard if any(
        button.callback_data and button.callback_data.startswith("admin_user_profile_") for button in row
    )]
    assert all(len(row) == 1 for row in client_rows)
    assert any(button.text.startswith("🚨") for button in client_buttons)

    forbidden_callback = _callback(98130999, "admin_clients_list_0")
    with patch("handlers.send_smart_content", new=AsyncMock()) as send_content:
        await handlers.admin_clients_list(forbidden_callback, state)
        send_content.assert_not_awaited()
    forbidden_callback.answer.assert_awaited_once_with("⛔️ Доступ запрещён.", show_alert=True)

    profile_callback = _callback(admin_id, f"admin_user_profile_{crisis_client_id}")
    with patch("handlers.send_smart_content", new=AsyncMock()) as send_content:
        await handlers.admin_user_profile(profile_callback)
        profile_text = send_content.call_args.args[1]
        profile_markup = send_content.call_args.args[3]
    assert "Кризисный флаг: активен" in profile_text
    assert any(
        button.callback_data == f"admin_crisis_clear_{crisis_client_id}"
        for row in profile_markup.inline_keyboard
        for button in row
    )

    with patch("handlers.admin_user_profile", new=AsyncMock()):
        await handlers.admin_crisis_clear(_callback(admin_id, f"admin_crisis_clear_{crisis_client_id}"))
    async with async_session_maker() as session:
        assert (await session.get(User, crisis_client_id)).crisis_detected_at is None


@pytest.mark.asyncio
async def test_automation_recipient_settings_selects_specific_admin_ids():
    await init_db()
    admin_id = 999999999
    async with async_session_maker() as session:
        handler = AutomationHandler(
            name="Recipient settings test",
            scope="global",
            event_name="signup_clicked",
            action_type="send_admin_message",
            action_payload_json=json.dumps({"text": "signup"}),
            recipient_ids_json=json.dumps([]),
            is_active=1,
        )
        session.add(handler)
        await session.commit()
        handler_id = handler.id

    callback = _callback(admin_id, f"automation_handler_recipients_{handler_id}")
    await automation_admin.callback_automation_handler_router(callback, _fsm(admin_id))
    assert callback.message.edit_text.await_count == 1
    assert "Получатели" in callback.message.edit_text.call_args.args[0]

    toggle_callback = _callback(
        admin_id,
        f"automation_handler_recipient_toggle_{handler_id}_{admin_id}",
    )
    await automation_admin.callback_automation_handler_router(toggle_callback, _fsm(admin_id))
    async with async_session_maker() as session:
        updated = await session.get(AutomationHandler, handler_id)
        assert json.loads(updated.recipient_ids_json) == [admin_id]


@pytest.mark.asyncio
async def test_sqlite_compat_migration_adds_recipient_column(tmp_path):
    legacy_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_alerts.sqlite'}")
    try:
        async with legacy_engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE automation_handlers ("
                "id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, scope VARCHAR NOT NULL, "
                "topic_slug VARCHAR, event_name VARCHAR, trigger_step VARCHAR, "
                "action_type VARCHAR NOT NULL, action_payload_json TEXT NOT NULL, "
                "is_active INTEGER NOT NULL, created_at DATETIME NOT NULL)"
            ))
            await _ensure_sqlite_compat_schema(conn)
            columns = await conn.execute(text("PRAGMA table_info(automation_handlers)"))
            column_names = {row[1] for row in columns.fetchall()}
        assert "recipient_ids_json" in column_names
    finally:
        await legacy_engine.dispose()


@pytest.mark.asyncio
async def test_background_alert_task_keeps_strong_reference_until_completion(monkeypatch):
    await wait_for_pending_automation_events()
    automation_events._pending_test_automation_jobs.clear()
    automation_events._pending_automation_tasks.clear()
    completed = asyncio.Event()

    async def fake_dispatch(bot, kwargs):
        await completed.wait()

    monkeypatch.setattr(automation_events, "IS_TESTING", False)
    monkeypatch.setattr(automation_events, "_dispatch_automation_events_in_new_session", fake_dispatch)

    automation_events.schedule_automation_events(object(), user_id=98130006, events=("crisis_detected",))
    await asyncio.sleep(0)
    assert len(automation_events._pending_automation_tasks) == 1
    assert not next(iter(automation_events._pending_automation_tasks)).done()

    completed.set()
    await wait_for_pending_automation_events()
    await asyncio.sleep(0)
    assert not automation_events._pending_automation_tasks
