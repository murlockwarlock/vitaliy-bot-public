from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

import journal_handlers
from database import (
    Content,
    ContentMedia,
    JournalEntry,
    PaymentConfig,
    QuickNote,
    User,
    async_session_maker,
    init_db,
)
from journal_handlers import JournalStates
from journal_keyboards import journal_entry_keyboard, journal_menu_keyboard
from time_utils import msk_now


def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=901, chat_id=user_id, user_id=user_id),
    )


def _callback(user_id: int, data: str, content_type: str = "text"):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=user_id),
        content_type=content_type,
        message_id=1,
        edit_text=AsyncMock(),
        answer=AsyncMock(),
        delete=AsyncMock(),
    )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, first_name="Тест"),
        chat=message.chat,
        message=message,
        bot=AsyncMock(),
        answer=AsyncMock(),
    )


def _message(user_id: int, text: str):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id, first_name="Тест"),
        chat=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
    )


async def _cleanup(*user_ids: int):
    async with async_session_maker() as session:
        await session.execute(delete(QuickNote).where(QuickNote.user_id.in_(user_ids)))
        await session.execute(delete(JournalEntry).where(JournalEntry.user_id.in_(user_ids)))
        await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


@pytest.mark.asyncio
async def test_quick_notes_full_history_privacy_and_journal_statistics_are_separate():
    await init_db()
    user_id = 98262101
    other_user_id = 98262102
    await _cleanup(user_id, other_user_id)

    async with async_session_maker() as session:
        session.add_all([
            User(id=user_id, first_name="Автор", journal_points=17),
            User(id=other_user_id, first_name="Другой пользователь", journal_points=4),
            JournalEntry(
                user_id=user_id,
                reflection_date=date(2000, 1, 1),
                status="completed",
                questions_snapshot_json="[]",
                points_awarded=17,
            ),
        ])
        await session.commit()

    state = _state(user_id)
    bot = AsyncMock()
    with patch("journal_handlers.check_paid_access", new=AsyncMock(return_value=True)), patch(
        "journal_handlers.send_smart_content", new_callable=AsyncMock
    ) as render:
        await journal_handlers.cb_quick_note_new(_callback(user_id, "quick_note_new"), state, bot)
        await journal_handlers.cb_quick_note_new(_callback(user_id, "quick_note_new"), state, bot)

    assert await state.get_state() == JournalStates.quick_note_input.state
    assert render.await_count == 1

    await journal_handlers.msg_quick_note_save(
        _message(user_id, "  Моя важная мысль <сегодня>  "),
        state,
        bot,
    )
    await journal_handlers.msg_quick_note_save(_message(user_id, "повторное нажатие"), state, bot)
    assert await state.get_state() is None

    async with async_session_maker() as session:
        note = await session.scalar(select(QuickNote).where(QuickNote.user_id == user_id))
        note.created_at = datetime(2026, 1, 2, 3, 4)
        session.add(QuickNote(user_id=other_user_id, text="Чужая запись"))
        entry = await session.scalar(select(JournalEntry).where(JournalEntry.user_id == user_id))
        user_before = await session.get(User, user_id)
        points_before = user_before.journal_points
        await session.commit()

    view = _callback(user_id, "quick_notes")
    with patch("journal_handlers.check_paid_access", new=AsyncMock(return_value=True)), patch(
        "journal_handlers.send_smart_content", new_callable=AsyncMock
    ) as render:
        await journal_handlers.cb_quick_notes(view, bot)

    rendered_text = render.call_args.args[1]
    markup = render.call_args.args[3]
    assert "📝 <b>Оперативные записи</b>" in rendered_text
    assert "02.01.2026 03:04" in rendered_text
    assert "Моя важная мысль &lt;сегодня&gt;" in rendered_text
    assert "Чужая запись" not in rendered_text
    assert any(
        button.callback_data == "quick_note_new:0"
        for row in markup.inline_keyboard
        for button in row
    )

    async with async_session_maker() as session:
        notes = (await session.scalars(select(QuickNote).where(QuickNote.user_id == user_id))).all()
        entry_after = await session.scalar(select(JournalEntry).where(JournalEntry.user_id == user_id))
        user_after = await session.get(User, user_id)
        assert len(notes) == 1
        assert notes[0].text == "Моя важная мысль <сегодня>"
        assert entry_after.status == entry.status == "completed"
        assert entry_after.points_awarded == 17
        assert user_after.journal_points == points_before == 17

    assert any(button.callback_data == "quick_notes" for row in journal_entry_keyboard().inline_keyboard for button in row)
    assert any(button.callback_data == "quick_notes" for row in journal_menu_keyboard().inline_keyboard for button in row)
    await _cleanup(user_id, other_user_id)


@pytest.mark.asyncio
async def test_quick_notes_paginate_and_open_full_text_with_timestamp():
    await init_db()
    user_id = 98262103
    await _cleanup(user_id)
    async with async_session_maker() as session:
        session.add(User(id=user_id, first_name="Пагинация"))
        for index in range(21):
            session.add(QuickNote(
                user_id=user_id,
                text=f"Запись {index} " + ("полный текст. " * 40),
                created_at=datetime(2026, 1, 1) + timedelta(minutes=index),
            ))
        await session.commit()

    bot = AsyncMock()
    page_two = _callback(user_id, "quick_notes_page:2")
    with patch("journal_handlers.check_paid_access", new=AsyncMock(return_value=True)), patch(
        "journal_handlers.send_smart_content", new_callable=AsyncMock
    ) as render:
        await journal_handlers.cb_quick_notes(page_two, bot)

    rendered_text = render.call_args.args[1]
    markup = render.call_args.args[3]
    assert "Страница 3 из 3" in rendered_text
    pagination = next(row for row in markup.inline_keyboard if [button.text for button in row] == ["⬅️", "3/3", "➡️"])
    assert [button.callback_data for button in pagination] == ["quick_notes_page:1", "noop", "noop"]

    async with async_session_maker() as session:
        note = await session.scalar(select(QuickNote).where(QuickNote.text.startswith("Запись 20")))
        note_id = note.id
        full_text = note.text

    detail = _callback(user_id, f"quick_note_view:{note_id}:2")
    with patch("journal_handlers.check_paid_access", new=AsyncMock(return_value=True)), patch(
        "journal_handlers.send_smart_content", new_callable=AsyncMock
    ) as render:
        await journal_handlers.cb_quick_note_view(detail, bot)

    chunks = [render.call_args.args[1]] + [call.args[1] for call in bot.send_message.await_args_list]
    combined = "".join(chunks)
    assert full_text in combined
    assert "Дата и время" in combined
    await _cleanup(user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["text", "photo", "video"])
async def test_quick_notes_open_from_text_and_media_journal_messages(content_type):
    await init_db()
    user_id = 98262104
    await _cleanup(user_id)
    async with async_session_maker() as session:
        session.add(User(id=user_id, first_name="Медиа"))
        await session.commit()

    callback = _callback(user_id, "quick_notes", content_type)
    bot = AsyncMock()
    with patch("journal_handlers.check_paid_access", new=AsyncMock(return_value=True)), patch(
        "journal_handlers.send_smart_content", new_callable=AsyncMock
    ) as render:
        await journal_handlers.cb_quick_notes(callback, bot)
    render.assert_awaited_once()
    assert callback.message.content_type == content_type
    await _cleanup(user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("file_type", [None, "photo", "video"])
async def test_journal_intro_text_and_media_keep_quick_notes_route(file_type):
    await init_db()
    user_id = 98262109
    await _cleanup(user_id)

    async with async_session_maker() as session:
        session.add(User(id=user_id, first_name="Вход в дневник"))
        content = await session.get(Content, "journal_intro", options=[selectinload(Content.media)])
        old_text = content.text_content if content else None
        old_media = (
            [(item.file_type, item.file_id) for item in content.media]
            if content
            else []
        )
        if content is None:
            content = Content(key="journal_intro")
            session.add(content)
        content.text_content = "Вступление дневника"
        await session.execute(delete(ContentMedia).where(ContentMedia.content_key == "journal_intro"))
        if file_type:
            session.add(ContentMedia(content_key="journal_intro", file_type=file_type, file_id=f"intro-{file_type}"))
        await session.commit()

    try:
        callback = _callback(user_id, "mode_journal", file_type or "text")
        with patch("journal_handlers.send_smart_content", new_callable=AsyncMock) as render:
            await journal_handlers.show_journal_menu(callback, AsyncMock())
        markup = render.call_args.kwargs["reply_markup"]
        assert any(
            button.callback_data == "quick_notes"
            for row in markup.inline_keyboard
            for button in row
        )
    finally:
        async with async_session_maker() as session:
            content = await session.get(Content, "journal_intro")
            await session.execute(delete(ContentMedia).where(ContentMedia.content_key == "journal_intro"))
            if content is not None:
                content.text_content = old_text
                for old_file_type, old_file_id in old_media:
                    session.add(ContentMedia(
                        content_key="journal_intro",
                        file_type=old_file_type,
                        file_id=old_file_id,
                    ))
            await session.commit()
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_quick_notes_access_is_checked_on_open_create_view_and_save():
    await init_db()
    user_id = 98262105
    owner_id = 98262106
    await _cleanup(user_id, owner_id)
    async with async_session_maker() as session:
        session.add_all([User(id=user_id, first_name="Без доступа"), User(id=owner_id, first_name="Владелец")])
        owned = QuickNote(user_id=owner_id, text="закрытая запись")
        session.add(owned)
        await session.commit()
        owned_id = owned.id

    bot = AsyncMock()
    denied = AsyncMock(return_value=False)
    with patch("journal_handlers.check_paid_access", new=denied):
        await journal_handlers.cb_quick_notes(_callback(user_id, "quick_notes"), bot)
        await journal_handlers.cb_quick_note_new(_callback(user_id, "quick_note_new"), _state(user_id), bot)
        await journal_handlers.cb_quick_note_view(_callback(user_id, f"quick_note_view:{owned_id}:0"), bot)
        save_state = _state(user_id)
        await save_state.set_state(JournalStates.quick_note_input)
        await save_state.update_data(quick_note_operation_key="denied-operation")
        await journal_handlers.msg_quick_note_save(_message(user_id, "не сохранить"), save_state, bot)

    assert denied.await_count == 4
    assert await save_state.get_state() is None
    async with async_session_maker() as session:
        assert await session.scalar(select(QuickNote).where(QuickNote.user_id == user_id)) is None
    await _cleanup(user_id, owner_id)


@pytest.mark.asyncio
async def test_quick_notes_use_current_subscription_at_open():
    await init_db()
    user_id = 98262108
    await _cleanup(user_id)
    async with async_session_maker() as session:
        session.add(User(id=user_id, first_name="Подписка"))
        payment_config = await session.get(PaymentConfig, 1)
        previous_mode = payment_config.mode
        payment_config.mode = "subscription"
        user = await session.get(User, user_id)
        user.subscription_end = msk_now() - timedelta(minutes=1)
        await session.commit()

    try:
        bot = AsyncMock()
        denied_callback = _callback(user_id, "quick_notes")
        with patch("journal_handlers.send_smart_content", new_callable=AsyncMock) as denied_render:
            await journal_handlers.cb_quick_notes(denied_callback, bot)
        denied_render.assert_not_awaited()

        async with async_session_maker() as session:
            user = await session.get(User, user_id)
            user.subscription_end = msk_now() + timedelta(days=1)
            await session.commit()

        allowed_callback = _callback(user_id, "quick_notes")
        with patch("journal_handlers.send_smart_content", new_callable=AsyncMock) as allowed_render:
            await journal_handlers.cb_quick_notes(allowed_callback, bot)
        allowed_render.assert_awaited_once()
    finally:
        async with async_session_maker() as session:
            payment_config = await session.get(PaymentConfig, 1)
            payment_config.mode = previous_mode
            await session.commit()
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_quick_note_cancel_clears_fsm_and_late_message_cannot_save():
    await init_db()
    user_id = 98262107
    await _cleanup(user_id)
    async with async_session_maker() as session:
        session.add(User(id=user_id, first_name="Отмена"))
        await session.commit()

    state = _state(user_id)
    await state.set_state(JournalStates.quick_note_input)
    await state.update_data(quick_note_page=2, quick_note_operation_key="cancelled-operation")
    callback = _callback(user_id, "quick_note_cancel:2")
    bot = AsyncMock()
    with patch("journal_handlers.check_paid_access", new=AsyncMock(return_value=True)), patch(
        "journal_handlers.send_smart_content", new_callable=AsyncMock
    ):
        await journal_handlers.cb_quick_note_cancel(callback, state, bot)
        await journal_handlers.msg_quick_note_save(_message(user_id, "пришло после отмены"), state, bot)

    assert await state.get_state() is None
    async with async_session_maker() as session:
        assert await session.scalar(select(QuickNote).where(QuickNote.user_id == user_id)) is None
    await _cleanup(user_id)
