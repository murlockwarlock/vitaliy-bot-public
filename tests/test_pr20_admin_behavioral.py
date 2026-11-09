import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User as TelegramUser
from sqlalchemy import delete, select

import handlers
import funnel
import keyboards as kb
from automation_admin import automation_crisis_markers, automation_handlers_list, automation_menu
from database import (
    AILog,
    AdminNavigationContext,
    FunnelEvent,
    FunnelLanding,
    JournalQuestion,
    TestQuestion,
    TestSet,
    User,
    async_session_maker,
    init_db,
)
from followup_admin import admin_followup_list
from admin_roles import AdminPermission, AdminRole, permission_for_callback
from funnel import funnel_landing_detail_keyboard
from crisis_safety import get_crisis_marker_settings
from journal_admin_handlers import admin_journal_q_list_handler, admin_journal_q_tog_handler
from test_set_admin_handlers import view_test_set_questions


ADMIN_ID = 98273001
ADMIN_ID_2 = 98273002
CLIENT_IDS = tuple(range(98273100, 98273111))


def _callback(user_id: int, data: str, *, chat_id: int | None = None, message_id: int = 1):
    callback = AsyncMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = TelegramUser(id=user_id, is_bot=False, first_name="Менеджер")
    callback.message = AsyncMock(spec=Message)
    callback.message.chat = Chat(id=chat_id or user_id, type="private")
    callback.message.message_id = message_id
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.message.delete = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _real_callback(user_id: int, data: str, *, chat_id: int | None = None, message_id: int = 1):
    telegram_user = TelegramUser(id=user_id, is_bot=False, first_name="Менеджер")
    return CallbackQuery(
        id=f"callback-{user_id}-{message_id}",
        from_user=telegram_user,
        chat_instance=f"chat-{chat_id or user_id}",
        message=Message(
            message_id=message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id or user_id, type="private"),
            from_user=telegram_user,
            text="Список клиентов",
        ),
        data=data,
    )


def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=98273, chat_id=user_id, user_id=user_id),
    )


def _callbacks(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


async def _ensure_admin(user_id: int = ADMIN_ID):
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None:
            user = User(id=user_id, first_name="Менеджер", is_admin=True)
            session.add(user)
        user.is_admin = True
        user.admin_role = AdminRole.FULL_ADMIN.value
        await session.commit()


async def _cleanup_users(*user_ids: int):
    async with async_session_maker() as session:
        await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.execute(
            delete(AdminNavigationContext).where(
                AdminNavigationContext.admin_id.in_(user_ids)
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_standard_pagination_is_one_row_and_has_safe_boundaries():
    for page, total_pages, expected in (
        (0, 1, ["noop", "noop"]),
        (0, 3, ["noop", "admin_clients_list_1"]),
        (2, 3, ["admin_clients_list_1", "noop"]),
        (99, 3, ["admin_clients_list_1", "noop"]),
    ):
        row = kb.standard_pagination_row(
            page,
            total_pages,
            lambda target: f"admin_clients_list_{target}",
        )
        assert [button.text for button in row] == ["⬅️", f"{min(max(page, 0), total_pages - 1) + 1}/{total_pages}", "➡️"]
        assert [button.callback_data for button in row][::2] == expected

    users = [
        SimpleNamespace(id=1, username="a_very_long_manager_name", first_name="Первый", crisis_detected_at=None),
        SimpleNamespace(id=2, username=None, first_name="Второй менеджер", crisis_detected_at=None),
    ]
    markup = kb.admin_clients_keyboard(users, page=0, total_pages=2)
    assert [len(row) for row in markup.inline_keyboard[:2]] == [1, 1]
    navigation = next(row for row in markup.inline_keyboard if [button.text for button in row] == ["⬅️", "1/2", "➡️"])
    assert len(navigation) == 3
    assert [button.callback_data for button in navigation] == ["noop", "noop", "admin_clients_list_1"]


@pytest.mark.asyncio
async def test_real_section_route_clears_fsm_and_returns_to_admin_root():
    await init_db()
    await _ensure_admin()
    state = _state(ADMIN_ID)
    await state.set_state(handlers.AdminStates.search_client)

    callback = _callback(ADMIN_ID, "admin_section_automation")
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as render:
        await handlers.admin_section_menu(callback, state)

    assert await state.get_state() is None
    markup = render.call_args.args[3]
    assert _callbacks(markup)[-1] == "admin_panel"
    assert "automation_menu" in _callbacks(markup)
    await _cleanup_users(ADMIN_ID)


@pytest.mark.asyncio
async def test_journal_question_actions_keep_the_current_page_context():
    await init_db()
    await _ensure_admin()
    marker = f"PR20 question {uuid.uuid4().hex}"
    async with async_session_maker() as session:
        max_order = await session.scalar(select(JournalQuestion.order_num).order_by(JournalQuestion.order_num.desc())) or 0
        questions = [
            JournalQuestion(text=f"{marker} {index}", order_num=max_order + index + 1, is_active=True)
            for index in range(11)
        ]
        session.add_all(questions)
        await session.commit()
        question_ids = [question.id for question in questions]

    try:
        async with async_session_maker() as session:
            ordered = list((await session.scalars(
                select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
            )).all())
            position = next(index for index, question in enumerate(ordered) if question.id == question_ids[-1])
        page = position // 10

        list_callback = _callback(ADMIN_ID, f"j_adm_q_list_page:{page}")
        with patch("journal_admin_handlers.send_smart_content", new_callable=AsyncMock) as render:
            await admin_journal_q_list_handler(list_callback, _state(ADMIN_ID))
        markup = render.call_args.args[3]
        pagination = next(row for row in markup.inline_keyboard if len(row) == 3 and "/" in row[1].text)
        assert pagination[1].text == f"{page + 1}/{max(1, (len(ordered) + 9) // 10)}"
        target_button = next(
            button
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data == f"j_adm_q_tog:{question_ids[-1]}:{page}"
        )
        assert target_button.text.startswith("🟢")

        toggle_callback = _callback(ADMIN_ID, target_button.callback_data)
        with patch("journal_admin_handlers.send_smart_content", new_callable=AsyncMock) as rerender:
            await admin_journal_q_tog_handler(toggle_callback, _state(ADMIN_ID))
        rerender_markup = rerender.call_args.args[3]
        rerender_pagination = next(
            row for row in rerender_markup.inline_keyboard if len(row) == 3 and "/" in row[1].text
        )
        assert rerender_pagination[1].text == pagination[1].text
    finally:
        async with async_session_maker() as session:
            await session.execute(delete(JournalQuestion).where(JournalQuestion.id.in_(question_ids)))
            await session.commit()
        await _cleanup_users(ADMIN_ID)


@pytest.mark.asyncio
async def test_payment_statistics_back_route_uses_each_real_entry_point():
    await init_db()
    await _ensure_admin()
    subscriptions_state = _state(ADMIN_ID)
    subscriptions_callback = _callback(ADMIN_ID, "admin_subscriptions")
    await handlers.admin_subscriptions_menu(subscriptions_callback, subscriptions_state)
    subscriptions_markup = subscriptions_callback.message.edit_text.call_args.kwargs["reply_markup"]
    assert "admin_payment_stats:admin_payment_settings" in _callbacks(subscriptions_markup)
    assert (await subscriptions_state.get_data()).get("admin_payment_stats_return") is None

    prodamus_state = _state(ADMIN_ID)
    await prodamus_state.update_data(admin_payment_stats_return="admin_subscriptions")
    prodamus_callback = _callback(ADMIN_ID, "admin_prodamus_settings")
    await handlers.admin_prodamus_settings(prodamus_callback, prodamus_state)
    prodamus_markup = prodamus_callback.message.edit_text.call_args.kwargs["reply_markup"]
    assert prodamus_markup.inline_keyboard[-1][0].callback_data == "admin_payment_settings"

    legacy_from_subscriptions = _callback(ADMIN_ID, "admin_payment_stats")
    await handlers.admin_payment_stats(legacy_from_subscriptions, subscriptions_state)
    legacy_subscriptions_markup = legacy_from_subscriptions.message.edit_text.call_args.kwargs["reply_markup"]
    assert legacy_subscriptions_markup.inline_keyboard[-1][0].callback_data == "admin_payment_settings"

    legacy_subscribers = _callback(ADMIN_ID, "admin_subscribers")
    await handlers.admin_subscribers(legacy_subscribers, _state(ADMIN_ID))
    assert legacy_subscribers.message.edit_text.await_count == 1

    for source in (
        "admin_payment_settings",
        "admin_subscriptions",
        "admin_prodamus_settings",
    ):
        callback = _callback(ADMIN_ID, f"admin_payment_stats:{source}")
        await handlers.admin_payment_stats(callback)
        markup = callback.message.edit_text.call_args.kwargs["reply_markup"]
        expected = "admin_payment_settings" if source == "admin_subscriptions" else source
        assert markup.inline_keyboard[-1][0].callback_data == expected

    legacy = _callback(ADMIN_ID, "admin_payment_stats")
    await handlers.admin_payment_stats(legacy)
    legacy_markup = legacy.message.edit_text.call_args.kwargs["reply_markup"]
    assert legacy_markup.inline_keyboard[-1][0].callback_data == "admin_payment_settings"
    await _cleanup_users(ADMIN_ID)


@pytest.mark.asyncio
async def test_reminder_editor_real_cycle_returns_to_automation_section():
    await init_db()
    await _ensure_admin()
    state = _state(ADMIN_ID)

    settings_callback = _callback(ADMIN_ID, "admin_reminders_settings")
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as render:
        await handlers.admin_reminders_settings(settings_callback, state)
    settings_markup = render.call_args.args[3]
    editor_callback_data = "edit_cont_main:main_reminder_first"
    assert editor_callback_data in _callbacks(settings_markup)

    editor_callback = _callback(ADMIN_ID, editor_callback_data)
    await handlers.edit_content_start(editor_callback, state)
    editor_data = await state.get_data()
    assert editor_data["return_callback"] == "admin_reminders_settings"
    editor_markup = editor_callback.message.answer.call_args.kwargs["reply_markup"]
    assert "admin_reminders_settings" in _callbacks(editor_markup)

    cancel_callback = _callback(ADMIN_ID, "admin_reminders_settings")
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as render_after_cancel:
        await handlers.admin_reminders_settings(cancel_callback, state)
    assert await state.get_state() is None
    assert "admin_section_automation" in _callbacks(render_after_cancel.call_args.args[3])
    await _cleanup_users(ADMIN_ID)


@pytest.mark.asyncio
async def test_automation_and_followup_handlers_use_real_parent_route_and_pagination_row():
    await init_db()
    await _ensure_admin()

    menu_callback = _callback(ADMIN_ID, "automation_menu")
    await automation_menu(menu_callback, _state(ADMIN_ID))
    menu_markup = menu_callback.message.edit_text.call_args.kwargs["reply_markup"]
    assert _callbacks(menu_markup)[-1] == "admin_section_automation"

    automation_callback = _callback(ADMIN_ID, "automation_handlers_page_999")
    await automation_handlers_list(automation_callback, _state(ADMIN_ID))
    automation_markup = automation_callback.message.edit_text.call_args.kwargs["reply_markup"]
    automation_pagination = next(
        row for row in automation_markup.inline_keyboard if len(row) == 3 and row[1].text.count("/") == 1
    )
    assert [button.text for button in automation_pagination][0:3] == ["⬅️", automation_pagination[1].text, "➡️"]
    assert _callbacks(automation_markup)[-1] == "automation_menu"

    followup_callback = _callback(ADMIN_ID, "admin_fu_list_999")
    await admin_followup_list(followup_callback, _state(ADMIN_ID))
    followup_markup = followup_callback.message.edit_text.call_args.kwargs["reply_markup"]
    followup_pagination = next(
        row for row in followup_markup.inline_keyboard if len(row) == 3 and row[1].text.count("/") == 1
    )
    assert [button.text for button in followup_pagination][0] == "⬅️"
    assert _callbacks(followup_markup)[-1] == "admin_section_automation"
    await _cleanup_users(ADMIN_ID)


@pytest.mark.asyncio
async def test_admin_manual_rows_survive_builder_rendering():
    await init_db()
    await _ensure_admin()

    async with async_session_maker() as session:
        config, _, markers = await get_crisis_marker_settings(session)
        previous_markers = list(markers)
        config.crisis_markers_json = '["маркер один", "маркер два"]'
        test_set = TestSet(slug=f"pr20-layout-{uuid.uuid4().hex}", title="PR20 layout")
        session.add(test_set)
        await session.flush()
        session.add_all([
            TestQuestion(test_set_id=test_set.id, text="Вопрос 1", order_num=1),
            TestQuestion(test_set_id=test_set.id, text="Вопрос 2", order_num=2),
        ])
        await session.commit()
        test_set_id = test_set.id

    try:
        crisis_callback = _callback(ADMIN_ID, "automation_crisis_markers")
        await automation_crisis_markers(crisis_callback, _state(ADMIN_ID))
        crisis_rows = crisis_callback.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        marker_rows = [row for row in crisis_rows if len(row) == 2]
        assert len(marker_rows) == 2

        questions_callback = _callback(ADMIN_ID, f"ts_view_q_{test_set_id}")
        await view_test_set_questions(questions_callback, _state(ADMIN_ID))
        question_rows = questions_callback.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        assert all(len(row) == 1 for row in question_rows[:4:2])
        assert all(len(row) == 2 for row in question_rows[1:4:2])
        assert question_rows[-1][0].callback_data == f"edit_testset_{test_set_id}"
    finally:
        async with async_session_maker() as session:
            config, _, _ = await get_crisis_marker_settings(session)
            config.crisis_markers_json = json.dumps(previous_markers, ensure_ascii=False)
            await session.execute(delete(TestQuestion).where(TestQuestion.test_set_id == test_set_id))
            await session.execute(delete(TestSet).where(TestSet.id == test_set_id))
            await session.commit()
        await _cleanup_users(ADMIN_ID)


@pytest.mark.asyncio
async def test_legacy_ai_log_profile_back_route_targets_a_real_profile_handler():
    markup = kb.admin_ai_logs_keyboard(
        [],
        page=0,
        total_pages=1,
        filter_user_id=98273120,
    )
    assert markup.inline_keyboard[-1][0].callback_data == "admin_user_profile_98273120"


@pytest.mark.asyncio
async def test_client_profile_history_keeps_page_and_search_context_through_real_handlers():
    await init_db()
    await _ensure_admin()
    await _cleanup_users(*CLIENT_IDS)
    async with async_session_maker() as session:
        session.add_all([
            User(id=user_id, first_name=f"Контекст {index}")
            for index, user_id in enumerate(CLIENT_IDS)
        ])
        await session.commit()

    state = _state(ADMIN_ID)
    await state.update_data(client_search_query="Контекст")
    list_callback = _callback(ADMIN_ID, "admin_clients_list_1")
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as list_render:
        await handlers.admin_clients_list(list_callback, state)
    list_markup = list_render.call_args.args[3]
    profile_callback_data = list_markup.inline_keyboard[0][0].callback_data
    assert profile_callback_data.startswith("admin_user_profile_")
    assert (await state.get_data())["admin_client_return_callback"] == "admin_clients_list_1"

    profile_callback = _callback(ADMIN_ID, profile_callback_data)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as profile_render:
        await handlers.admin_user_profile(profile_callback, state)
    profile_markup = profile_render.call_args.args[3]
    assert any(
        callback.startswith("admin_clients_list_1")
        for callback in _callbacks(profile_markup)
    )

    client_id = int(profile_callback_data.split(":", 1)[0].rsplit("_", 1)[1])
    history_callback = _callback(ADMIN_ID, f"admin_history_view_{client_id}_0")
    with patch("handlers.send_smart_content", new_callable=AsyncMock):
        await handlers.admin_history_view(history_callback, state)
    preserved = await state.get_data()
    assert preserved["admin_client_return_callback"] == "admin_clients_list_1"
    assert preserved["client_search_query"] == "Контекст"

    profile_again = _callback(ADMIN_ID, f"admin_user_profile_{client_id}")
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as profile_again_render:
        await handlers.admin_user_profile(profile_again, state)
    assert "admin_clients_list_1" in _callbacks(profile_again_render.call_args.args[3])
    await _cleanup_users(*CLIENT_IDS, ADMIN_ID)


@pytest.mark.asyncio
async def test_client_search_reset_returns_to_the_source_page_through_real_handlers():
    await init_db()
    await _ensure_admin()
    client_ids = tuple(range(98273200, 98273230))
    await _cleanup_users(*client_ids)
    async with async_session_maker() as session:
        session.add_all([
            User(id=user_id, first_name=f"Search candidate {index}")
            for index, user_id in enumerate(client_ids)
        ])
        await session.commit()

    state = _state(ADMIN_ID)
    await state.update_data(
        client_search_query="candidate",
        admin_client_search_reset_callback="admin_clients_list_2",
        admin_client_search_reset_query=None,
    )
    search_list_callback = _callback(ADMIN_ID, "admin_clients_list_0")
    search_list_callback.message.message_id = 601
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as search_render:
        await handlers.admin_clients_list(search_list_callback, state)

    markup = search_render.call_args.args[3]
    reset_data = next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.text == "❌ Сбросить поиск"
    )
    reset_callback = _callback(ADMIN_ID, reset_data)
    reset_callback.message.message_id = 601
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as reset_render:
        await handlers.reset_client_search(reset_callback, state)

    assert "Страница 3" in reset_render.call_args.args[1]
    data = await state.get_data()
    assert data["client_search_query"] is None
    assert data["admin_client_return_callback"] == "admin_clients_list_2"
    await _cleanup_users(*client_ids)


@pytest.mark.asyncio
async def test_client_search_cancel_keeps_page_three_after_real_handler_flow():
    await init_db()
    await _ensure_admin()
    client_ids = tuple(range(98273230, 98273260))
    await _cleanup_users(*client_ids)
    async with async_session_maker() as session:
        session.add_all([
            User(id=user_id, first_name=f"Cancel candidate {index}")
            for index, user_id in enumerate(client_ids)
        ])
        await session.commit()

    state = _state(ADMIN_ID)
    list_callback = _callback(ADMIN_ID, "admin_clients_list_2")
    list_callback.message.message_id = 602
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as list_render:
        await handlers.admin_clients_list(list_callback, state)
    search_data = next(
        button.callback_data
        for row in list_render.call_args.args[3].inline_keyboard
        for button in row
        if button.text == "🔍 Поиск"
    )

    start_callback = _callback(ADMIN_ID, search_data)
    start_callback.message.message_id = 602
    await handlers.start_client_search(start_callback, state)
    cancel_markup = start_callback.message.edit_text.call_args.kwargs["reply_markup"]
    cancel_data = cancel_markup.inline_keyboard[0][0].callback_data

    cancel_callback = _callback(ADMIN_ID, cancel_data)
    cancel_callback.message.message_id = 602
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as cancel_render:
        await handlers.cancel_client_search(cancel_callback, state)

    assert "Страница 3" in cancel_render.call_args.args[1]
    assert await state.get_state() is None
    assert (await state.get_data())["client_search_query"] is None
    await _cleanup_users(*client_ids)


@pytest.mark.asyncio
async def test_client_profile_callbacks_keep_two_open_list_contexts_separate():
    await init_db()
    await _ensure_admin()
    alpha_ids = tuple(range(98273260, 98273290))
    beta_ids = tuple(range(98273290, 98273320))
    await _cleanup_users(*alpha_ids, *beta_ids)
    async with async_session_maker() as session:
        session.add_all([
            User(id=user_id, first_name=f"alpha client {index}")
            for index, user_id in enumerate(alpha_ids)
        ])
        session.add_all([
            User(id=user_id, first_name=f"beta client {index}")
            for index, user_id in enumerate(beta_ids)
        ])
        await session.commit()

    state = _state(ADMIN_ID)
    await state.update_data(
        client_search_query="alpha",
        admin_client_search_reset_callback="admin_clients_list_2",
        admin_client_search_reset_query=None,
    )
    list_a = _callback(ADMIN_ID, "admin_clients_list_2")
    list_a.message.message_id = 603
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as render_a:
        await handlers.admin_clients_list(list_a, state)
    profile_a_data = render_a.call_args.args[3].inline_keyboard[0][0].callback_data
    assert ":clientctx_" in profile_a_data

    await state.update_data(
        client_search_query="beta",
        admin_client_search_reset_callback="admin_clients_list_1",
        admin_client_search_reset_query=None,
    )
    list_b = _callback(ADMIN_ID, "admin_clients_list_1")
    list_b.message.message_id = 604
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as render_b:
        await handlers.admin_clients_list(list_b, state)
    profile_b_data = render_b.call_args.args[3].inline_keyboard[0][0].callback_data
    assert profile_a_data != profile_b_data

    profile_a = _callback(ADMIN_ID, profile_a_data)
    profile_a.message.message_id = 603
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as profile_a_render:
        await handlers.admin_user_profile(profile_a, state)
    assert any(
        callback.startswith("admin_clients_list_2")
        for callback in _callbacks(profile_a_render.call_args.args[3])
    )
    assert (await state.get_data())["client_search_query"] == "alpha"

    stale_profile = _callback(ADMIN_ID, profile_a_data)
    stale_profile.message.message_id = 604
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as stale_render:
        await handlers.admin_user_profile(stale_profile, state)
    stale_render.assert_not_awaited()
    stale_profile.answer.assert_awaited_once_with(
        "Экран списка устарел. Откройте список клиентов заново.",
        show_alert=True,
    )

    profile_b = _callback(ADMIN_ID, profile_b_data)
    profile_b.message.message_id = 604
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as profile_b_render:
        await handlers.admin_user_profile(profile_b, state)
    assert any(
        callback.startswith("admin_clients_list_1")
        for callback in _callbacks(profile_b_render.call_args.args[3])
    )
    assert (await state.get_data())["client_search_query"] == "beta"
    await _cleanup_users(*alpha_ids, *beta_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "callback_data"),
    (
        ("reset_client_search", "admin_clients_reset_search"),
        ("cancel_client_search", "admin_clients_cancel_search"),
    ),
)
async def test_client_search_actions_do_not_mutate_real_callback_query(
    handler_name,
    callback_data,
):
    await init_db()
    await _ensure_admin()
    client_ids = tuple(range(98273400, 98273430))
    await _cleanup_users(*client_ids)
    async with async_session_maker() as session:
        session.add_all([
            User(id=user_id, first_name=f"Immutable candidate {index}")
            for index, user_id in enumerate(client_ids)
        ])
        await session.commit()

    state = _state(ADMIN_ID)
    if handler_name == "reset_client_search":
        await state.update_data(
            client_search_query="candidate",
            admin_client_search_reset_callback="admin_clients_list_2",
            admin_client_search_reset_query=None,
        )
    else:
        await state.update_data(
            client_search_query=None,
            admin_client_search_input_return_callback="admin_clients_list_2",
            admin_client_search_input_return_query=None,
        )

    callback = _real_callback(ADMIN_ID, callback_data, message_id=705)
    original_data = callback.data
    with (
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
        patch("handlers.send_smart_content", new_callable=AsyncMock) as render,
    ):
        await getattr(handlers, handler_name)(callback, state)

    assert callback.data == original_data
    assert "Страница 3" in render.call_args.args[1]
    await _cleanup_users(*client_ids)


@pytest.mark.asyncio
async def test_client_list_pagination_and_profile_back_keep_source_context_after_switching_lists():
    await init_db()
    await _ensure_admin()
    alpha_ids = tuple(range(98273430, 98273460))
    beta_ids = tuple(range(98273460, 98273490))
    await _cleanup_users(*alpha_ids, *beta_ids)
    async with async_session_maker() as session:
        session.add_all([
            User(id=user_id, first_name=f"alpha source {index}")
            for index, user_id in enumerate(alpha_ids)
        ])
        session.add_all([
            User(id=user_id, first_name=f"beta source {index}")
            for index, user_id in enumerate(beta_ids)
        ])
        await session.commit()

    state = _state(ADMIN_ID)
    await state.update_data(
        client_search_query="alpha",
        admin_client_search_reset_callback="admin_clients_list_1",
        admin_client_search_reset_query=None,
    )
    list_a = _callback(ADMIN_ID, "admin_clients_list_1", chat_id=710, message_id=603)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as render_a:
        await handlers.admin_clients_list(list_a, state)
    markup_a = render_a.call_args.args[3]
    profile_a_data = markup_a.inline_keyboard[0][0].callback_data
    pagination_a_data = next(
        button.callback_data
        for row in markup_a.inline_keyboard
        for button in row
        if button.text == "➡️"
    )
    assert ":clientctx_" in profile_a_data
    assert ":clientctx_" in pagination_a_data

    await state.update_data(
        client_search_query="beta",
        admin_client_search_reset_callback="admin_clients_list_0",
        admin_client_search_reset_query=None,
    )
    list_b = _callback(ADMIN_ID, "admin_clients_list_0", chat_id=711, message_id=604)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as render_b:
        await handlers.admin_clients_list(list_b, state)
    assert "beta" in render_b.call_args.args[1]

    profile_a = _callback(ADMIN_ID, profile_a_data, chat_id=710, message_id=603)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as profile_render:
        await handlers.admin_user_profile(profile_a, state)
    profile_markup = profile_render.call_args.args[3]
    profile_back_data = next(
        button.callback_data
        for row in profile_markup.inline_keyboard
        for button in row
        if button.text == "⬅️ К списку"
    )
    assert profile_back_data.startswith("admin_clients_list_1:")
    assert ":clientctx_" in profile_back_data

    await state.update_data(
        client_search_query="beta",
        admin_client_search_reset_callback="admin_clients_list_0",
        admin_client_search_reset_query=None,
    )
    back_to_a = _callback(ADMIN_ID, profile_back_data, chat_id=710, message_id=603)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as back_render:
        await handlers.admin_clients_list(back_to_a, state)
    assert "alpha" in back_render.call_args.args[1]
    assert "Страница 2" in back_render.call_args.args[1]
    assert "beta" not in back_render.call_args.args[1]

    await state.update_data(
        client_search_query="beta",
        admin_client_search_reset_callback="admin_clients_list_0",
        admin_client_search_reset_query=None,
    )
    old_a_page = _callback(ADMIN_ID, pagination_a_data, chat_id=710, message_id=603)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as old_a_render:
        await handlers.admin_clients_list(old_a_page, state)
    assert "alpha" in old_a_render.call_args.args[1]
    assert "Страница 3" in old_a_render.call_args.args[1]
    assert "beta" not in old_a_render.call_args.args[1]
    await _cleanup_users(*alpha_ids, *beta_ids)


@pytest.mark.asyncio
async def test_client_navigation_context_isolated_by_admin_chat_and_message():
    await init_db()
    await _ensure_admin()
    await _ensure_admin(ADMIN_ID_2)
    client_id = 98273490
    await _cleanup_users(client_id)
    async with async_session_maker() as session:
        session.add(User(id=client_id, first_name="Изолированный клиент"))
        await session.commit()

    state = _state(ADMIN_ID)
    list_callback = _callback(ADMIN_ID, "admin_clients_list_0", chat_id=720, message_id=610)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as list_render:
        await handlers.admin_clients_list(list_callback, state)
    profile_data = next(
        button.callback_data
        for row in list_render.call_args.args[3].inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith(f"admin_user_profile_{client_id}")
    )

    for wrong_admin, wrong_chat, wrong_message in (
        (ADMIN_ID_2, 720, 610),
        (ADMIN_ID, 721, 610),
        (ADMIN_ID, 720, 611),
    ):
        forged = _callback(
            wrong_admin,
            profile_data,
            chat_id=wrong_chat,
            message_id=wrong_message,
        )
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as forged_render:
            await handlers.admin_user_profile(forged, state)
        forged_render.assert_not_awaited()
        forged.answer.assert_awaited_once_with(
            "Экран списка устарел. Откройте список клиентов заново.",
            show_alert=True,
        )

    await _cleanup_users(client_id, ADMIN_ID, ADMIN_ID_2)


@pytest.mark.asyncio
async def test_profile_from_landing_returns_to_the_landing_page():
    await init_db()
    await _ensure_admin()
    client_id = 98273112
    await _cleanup_users(client_id)
    async with async_session_maker() as session:
        session.add(User(id=client_id, first_name="Лид"))
        await session.commit()

    landing_markup = funnel_landing_detail_keyboard(7, client_id, "paid", 2)
    profile_callback_data = next(
        button.callback_data
        for row in landing_markup.inline_keyboard
        for button in row
        if button.text == "👤 Профиль клиента"
    )
    assert profile_callback_data == f"admin_user_profile_{client_id}:landing_paid_2"
    assert len(profile_callback_data.encode("utf-8")) <= 64

    profile_callback = _callback(ADMIN_ID, profile_callback_data)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as render:
        await handlers.admin_user_profile(profile_callback, _state(ADMIN_ID))
    assert "admin_funnel_landings_paid_2" in _callbacks(render.call_args.args[3])
    await _cleanup_users(client_id, ADMIN_ID)


@pytest.mark.asyncio
async def test_landing_filter_with_underscore_round_trips_through_real_handlers():
    await init_db()
    await _ensure_admin()
    list_callback = _callback(ADMIN_ID, "admin_funnel_landings_form_submitted_0")
    with patch.object(funnel, "show_admin_funnel_landings", new_callable=AsyncMock) as show_list:
        await funnel.admin_funnel_landings(list_callback, _state(ADMIN_ID))
    show_list.assert_awaited_once_with(list_callback, "form_submitted", 0)

    detail_callback = _callback(ADMIN_ID, "admin_flanding_7_form_submitted_0")
    with patch.object(funnel, "show_admin_landing_detail", new_callable=AsyncMock) as show_detail:
        await funnel.admin_flanding_detail(detail_callback, _state(ADMIN_ID))
    show_detail.assert_awaited_once_with(detail_callback, 7, "form_submitted", 0)

    async with async_session_maker() as session:
        landing = FunnelLanding(
            token=f"pr20-{uuid.uuid4().hex}",
            user_id=ADMIN_ID,
            status="opened",
        )
        session.add(landing)
        await session.flush()
        landing_id = landing.id
        await session.commit()

    mark_callback = _callback(
        ADMIN_ID,
        f"admin_flanding_mark_paid_{landing_id}_form_submitted_3",
    )
    with patch.object(funnel, "show_admin_landing_detail", new_callable=AsyncMock) as show_marked:
        await funnel.admin_flanding_mark(mark_callback, _state(ADMIN_ID))
    show_marked.assert_awaited_once_with(mark_callback, landing_id, "form_submitted", 3)
    async with async_session_maker() as session:
        marked = await session.get(FunnelLanding, landing_id)
        assert marked.status == "paid"
        await session.delete(marked)
        await session.execute(
            delete(FunnelEvent).where(
                FunnelEvent.user_id == ADMIN_ID,
                FunnelEvent.event_type == "landing_marked_paid",
            )
        )
        await session.commit()
    await _cleanup_users(ADMIN_ID)


@pytest.mark.asyncio
async def test_filtered_ai_logs_keep_client_role_and_file_context():
    import ai_logs_admin

    log = SimpleNamespace(
        id=12,
        created_at=None,
        provider="OpenAI",
        model="test-model",
        latency_ms=0,
        status="success",
        attempt_role="primary",
        request_type="chat",
    )
    list_markup = kb.admin_ai_logs_keyboard(
        [log],
        page=0,
        total_pages=1,
        filter_user_id=98273120,
    )
    detail_markup = kb.admin_ai_log_detail_keyboard(
        log.id,
        page=0,
        filter_user_id=98273120,
    )
    file_callback = next(
        value for value in _callbacks(detail_markup)
        if value.startswith("admin_ai_log_file_")
    )
    detail_callback = next(
        value for value in _callbacks(list_markup)
        if value.startswith("admin_ai_log_") and not value.startswith("admin_ai_log_file_")
    )
    assert file_callback.endswith("_98273120")
    assert "_98273120_" in detail_callback
    assert permission_for_callback(file_callback) == AdminPermission.CLIENTS
    assert permission_for_callback(detail_callback) == AdminPermission.CLIENTS

    callback = _callback(98273120, detail_callback)
    with (
        patch.object(ai_logs_admin, "is_client_admin_actor", new_callable=AsyncMock, return_value=True),
        patch.object(ai_logs_admin, "show_ai_log_detail", new_callable=AsyncMock) as show_detail,
    ):
        await ai_logs_admin.callback_admin_ai_log_detail(callback)
    show_detail.assert_awaited_once()


@pytest.mark.asyncio
async def test_filtered_ai_logs_cannot_cross_user_or_reach_system_logs():
    import ai_logs_admin

    actor_id = 98273321
    own_user_id = 98273322
    other_user_id = 98273323
    await init_db()
    await _cleanup_users(actor_id, own_user_id, other_user_id)
    async with async_session_maker() as session:
        session.add(User(
            id=actor_id,
            first_name="Support manager",
            is_admin=True,
            admin_role=AdminRole.SUPPORT.value,
        ))
        own_log = AILog(
            user_id=own_user_id,
            provider="OpenAI",
            model="test-model",
            request_payload="own",
            raw_response="own",
        )
        other_log = AILog(
            user_id=other_user_id,
            provider="OpenAI",
            model="test-model",
            request_payload="other",
            raw_response="other",
        )
        system_log = AILog(
            user_id=None,
            provider="OpenAI",
            model="test-model",
            request_payload="system",
            raw_response="system",
        )
        session.add_all([own_log, other_log, system_log])
        await session.flush()
        log_ids = (own_log.id, other_log.id, system_log.id)
        await session.commit()

    try:
        forged_detail = _callback(
            actor_id,
            f"admin_ai_log_{log_ids[1]}_0_{own_user_id}_all_all_all",
        )
        await ai_logs_admin.callback_admin_ai_log_detail(forged_detail)
        forged_detail.message.edit_text.assert_not_called()
        forged_detail.answer.assert_awaited_once_with(
            "⛔️ Доступ к этому логу запрещён.",
            show_alert=True,
        )

        forged_file = _callback(actor_id, f"admin_ai_log_file_{log_ids[1]}_{own_user_id}")
        await ai_logs_admin.callback_admin_ai_log_file_explicit(forged_file)
        forged_file.message.answer_document.assert_not_called()
        forged_file.answer.assert_awaited_once_with(
            "⛔️ Доступ к этому логу запрещён.",
            show_alert=True,
        )

        system_detail = _callback(
            actor_id,
            f"admin_ai_log_{log_ids[2]}_0_0_all_all_all",
        )
        await ai_logs_admin.callback_admin_ai_log_detail(system_detail)
        system_detail.message.edit_text.assert_not_called()
        system_detail.answer.assert_awaited_once_with(
            "⛔️ Доступ запрещён.",
            show_alert=True,
        )

        system_file = _callback(actor_id, f"admin_ai_log_file_{log_ids[2]}")
        await ai_logs_admin.callback_admin_ai_log_file_explicit(system_file)
        system_file.message.answer_document.assert_not_called()
        system_file.answer.assert_awaited_once_with(
            "⛔️ Доступ запрещён.",
            show_alert=True,
        )
    finally:
        async with async_session_maker() as session:
            await session.execute(delete(AILog).where(AILog.id.in_(log_ids)))
            await session.commit()
        await _cleanup_users(actor_id, own_user_id, other_user_id)
