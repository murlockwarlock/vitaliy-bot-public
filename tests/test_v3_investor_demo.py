import asyncio
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine

import funnel
import handlers
from database import (
    Content,
    ContentMedia,
    FunnelEvent,
    FunnelConfig,
    FunnelLanding,
    FunnelState,
    Message as DBMessage,
    MenuButton,
    User,
    _ensure_sqlite_compat_schema,
    async_session_maker,
    init_db,
)
from content_service import invalidate_content_cache
from automation_events import wait_for_pending_automation_events
from crisis_safety import mark_crisis_detected
from funnel_response import FunnelParseError, parse_funnel_analysis
from menu_service import button_to_telegram, load_menu_buttons
from navigation_service import render_main_menu_anchor


class CaptureBot:
    def __init__(self):
        self.sent = []
        self._message_id = 100

    def _message(self, kind, text=None, **kwargs):
        self._message_id += 1
        item = SimpleNamespace(
            message_id=self._message_id,
            kind=kind,
            text=text,
            caption=kwargs.get("caption"),
            reply_markup=kwargs.get("reply_markup"),
            chat=SimpleNamespace(id=kwargs.get("chat_id")),
            photo=[SimpleNamespace(file_id=kwargs.get("photo"))] if kind == "photo" else None,
            video=SimpleNamespace(file_id=kwargs.get("video")) if kind == "video" else None,
        )
        self.sent.append(item)
        return item

    async def send_message(self, chat_id, text, **kwargs):
        return self._message("text", text=text, chat_id=chat_id, **kwargs)

    async def send_photo(self, chat_id, photo, **kwargs):
        return self._message("photo", chat_id=chat_id, photo=photo, **kwargs)

    async def send_video(self, chat_id, video, **kwargs):
        return self._message("video", chat_id=chat_id, video=video, **kwargs)

    async def edit_message_media(self, **kwargs):
        self.edited_media = kwargs
        return self._message("edited_media", chat_id=kwargs["chat_id"], media=kwargs["media"])

    async def edit_message_text(self, **kwargs):
        self.edited_text = kwargs
        return self._message("edited_text", text=kwargs["text"], chat_id=kwargs["chat_id"])

    async def edit_message_caption(self, **kwargs):
        self.edited_caption = kwargs
        return self._message("edited_caption", chat_id=kwargs["chat_id"], caption=kwargs["caption"])

    async def edit_message_reply_markup(self, **kwargs):
        self.edited_reply_markup = kwargs


def _button_data(markup):
    return [button for row in markup.inline_keyboard for button in row]


def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=999, chat_id=user_id, user_id=user_id),
    )


async def _prepare_user(user_id: int, *, stage: str = "post_analysis"):
    await init_db()
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user:
            session.add(User(id=user_id, first_name="Demo", name="Demo", gender="female"))
        else:
            user.first_name = "Demo"
        state = await session.get(FunnelState, user_id)
        if not state:
            session.add(FunnelState(user_id=user_id, stage=stage, is_active=True))
        else:
            state.stage = stage
            state.is_active = True
        await session.execute(delete(FunnelEvent).where(FunnelEvent.user_id == user_id))
        await session.commit()


@pytest.mark.asyncio
async def test_funnel_parser_accepts_wrapped_json_and_rejects_raw_text():
    parsed = parse_funnel_analysis(
        'Ответ модели: ```json {"mode":"normal","has_experience":false,'
        '"has_question":false,"is_skeptic":false,"is_empty":false,'
        '"show_offer":true,"reply":"Разбор","bridge":"Можно углубиться"} ```'
    )
    assert parsed.reply == "Разбор"
    assert parsed.offer_eligible is True

    with pytest.raises(FunnelParseError):
        parse_funnel_analysis("обычный ответ без JSON")


@pytest.mark.asyncio
async def test_funnel_render_never_exposes_json_and_uses_bridge_actions():
    user_id = 94000101
    await _prepare_user(user_id)
    bot = CaptureBot()
    raw = (
        '{"mode":"normal","has_experience":false,"has_question":false,'
        '"is_skeptic":false,"is_empty":false,"show_offer":true,'
        '"reply":"Вот твой разбор.","bridge":"Если хочешь, разберёмся глубже."}'
    )

    await funnel.render_funnel_analysis(
        bot=bot,
        user_id=user_id,
        chat_id=user_id,
        raw_response=raw,
        reflection_text="Текст пользователя",
        meditation_id=None,
    )

    assert [item.text for item in bot.sent] == [
        "Вот твой разбор.",
        "Если хочешь, разберёмся глубже.",
    ]
    assert all("mode" not in (item.text or "") for item in bot.sent)
    assert [button.text for button in _button_data(bot.sent[1].reply_markup)] == [
        "Разобраться глубже",
        "Побуду с этим",
    ]
    async with async_session_maker() as session:
        state = await session.get(FunnelState, user_id)
        assert state.next_followup_at is not None


@pytest.mark.asyncio
async def test_malformed_funnel_response_uses_fallback_and_records_event():
    user_id = 94000102
    await _prepare_user(user_id)
    bot = CaptureBot()

    async with async_session_maker() as session:
        content = await session.get(Content, "funnel_fallback_text")
        if not content:
            content = Content(key="funnel_fallback_text")
            session.add(content)
        content.text_content = "Безопасный fallback."
        await session.commit()

    await funnel.render_funnel_analysis(
        bot=bot,
        user_id=user_id,
        chat_id=user_id,
        raw_response='{"mode": "normal", "reply":',
        reflection_text="Проверка",
        meditation_id=None,
    )

    assert bot.sent[0].text == "Безопасный fallback."
    assert "mode" not in bot.sent[0].text
    async with async_session_maker() as session:
        event = await session.scalar(
            select(FunnelEvent).where(
                FunnelEvent.user_id == user_id,
                FunnelEvent.event_type == "parse_failed",
            )
        )
        assert event is not None
        state = await session.get(FunnelState, user_id)
        assert state.next_followup_at is None
        assert state.followup_sequence_anchor is None


@pytest.mark.asyncio
async def test_clinical_and_crisis_responses_cancel_nurture_and_crisis_uses_contacts():
    clinical_id = 94000108
    crisis_id = 94000109
    await _prepare_user(clinical_id)
    await _prepare_user(crisis_id)
    clinical_bot = CaptureBot()
    crisis_bot = CaptureBot()

    clinical_json = (
        '{"mode":"clinical","has_experience":true,"has_question":true,'
        '"is_skeptic":false,"is_empty":false,"show_offer":true,'
        '"reply":"Лучше обсудить это со специалистом.","bridge":"Оффер не показываем"}'
    )
    crisis_json = (
        '{"mode":"crisis","has_experience":false,"has_question":false,'
        '"is_skeptic":false,"is_empty":false,"show_offer":true,'
        '"reply":"Я слышу, что тебе сейчас очень тяжело.","bridge":"Не продвигаем"}'
    )
    await funnel.render_funnel_analysis(
        bot=clinical_bot,
        user_id=clinical_id,
        chat_id=clinical_id,
        raw_response=clinical_json,
        reflection_text="",
        meditation_id=None,
    )
    await funnel.render_funnel_analysis(
        bot=crisis_bot,
        user_id=crisis_id,
        chat_id=crisis_id,
        raw_response=crisis_json,
        reflection_text="",
        meditation_id=None,
    )

    assert len(clinical_bot.sent) == 1
    assert len(crisis_bot.sent) == 2
    assert crisis_bot.sent[0].reply_markup is None
    assert [button.text for button in _button_data(crisis_bot.sent[1].reply_markup)] == ["Главное меню"]
    assert "112" in crisis_bot.sent[1].text
    async with async_session_maker() as session:
        clinical_state = await session.get(FunnelState, clinical_id)
        crisis_state = await session.get(FunnelState, crisis_id)
        crisis_user = await session.get(User, crisis_id)
        assert clinical_state.next_followup_at is None
        assert clinical_state.followup_sequence_anchor is None
        assert crisis_state.next_followup_at is None
        assert crisis_state.followup_sequence_anchor is None
        assert crisis_state.is_active is False
        assert crisis_user.crisis_detected_at is not None
        assert await session.scalar(
            select(FunnelEvent).where(
                FunnelEvent.user_id == crisis_id,
                FunnelEvent.event_type == "crisis_detected",
            )
        ) is not None

        crisis_state.is_active = True
        crisis_state.next_followup_at = funnel.msk_now()
        await session.commit()
    await funnel.process_funnel_followups(crisis_bot, single_iteration=True)
    assert len(crisis_bot.sent) == 2


@pytest.mark.asyncio
async def test_existing_crisis_flag_suppresses_normal_bridge_and_nurture():
    user_id = 94000112
    await _prepare_user(user_id)
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        user.crisis_detected_at = funnel.msk_now()
        await session.commit()

    bot = CaptureBot()
    raw = (
        '{"mode":"normal","has_experience":true,"has_question":false,'
        '"is_skeptic":false,"is_empty":false,"show_offer":true,'
        '"reply":"Разбор без продвижения.","bridge":"Скрытый мостик"}'
    )
    await funnel.render_funnel_analysis(
        bot=bot,
        user_id=user_id,
        chat_id=user_id,
        raw_response=raw,
        reflection_text="",
        meditation_id=None,
    )

    assert len(bot.sent) == 1
    assert bot.sent[0].text == "Разбор без продвижения."
    assert [button.text for button in _button_data(bot.sent[0].reply_markup)] == [
        "Выбрать другую практику",
        "Главное меню",
    ]
    async with async_session_maker() as session:
        state = await session.get(FunnelState, user_id)
        assert state.next_followup_at is None
        assert state.followup_sequence_anchor is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_name", "callback_data", "user_id"),
    [
        ("funnel_offer_requested", "funnel_offer_requested", 94000115),
        ("funnel_show_sales", "funnel_show_sales", 94000116),
        ("funnel_open_landing", "funnel_open_landing", 94000117),
    ],
)
async def test_crisis_flag_blocks_legacy_commercial_callbacks(
    callback_name,
    callback_data,
    user_id,
):
    await _prepare_user(user_id)
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        user.crisis_detected_at = funnel.msk_now()
        await session.commit()

    bot = CaptureBot()
    callback = SimpleNamespace(
        data=callback_data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=user_id)),
        bot=bot,
        answer=AsyncMock(),
    )

    await getattr(funnel, callback_name)(callback, _state(user_id))

    callback.answer.assert_awaited_once()
    assert len(bot.sent) == 1
    assert [button.text for button in _button_data(bot.sent[0].reply_markup)] == ["Главное меню"]
    async with async_session_maker() as session:
        assert await session.scalar(
            select(FunnelLanding).where(FunnelLanding.user_id == user_id)
        ) is None
        assert await session.scalar(
            select(FunnelEvent).where(
                FunnelEvent.user_id == user_id,
                FunnelEvent.event_type.in_([
                    "offer_requested",
                    "signup_clicked",
                    "landing_open_requested",
                    "strategy_offer_opened",
                ]),
            )
        ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_response", "user_id"),
    [
        (
            '{"mode":"clinical","has_experience":true,"has_question":true,'
            '"is_skeptic":false,"is_empty":false,"show_offer":true,'
            '"reply":"Кризисный безопасный ответ.","bridge":"Не продаём"}',
            94000118,
        ),
        ('{"mode": "clinical", "reply":', 94000119),
    ],
)
async def test_new_crisis_flag_has_priority_over_clinical_and_parse_failure(raw_response, user_id):
    await _prepare_user(user_id)
    async with async_session_maker() as session:
        await mark_crisis_detected(session, user_id)
        await session.commit()

    bot = CaptureBot()
    await funnel.render_funnel_analysis(
        bot=bot,
        user_id=user_id,
        chat_id=user_id,
        raw_response=raw_response,
        reflection_text="Кризисный текст",
        meditation_id=None,
    )
    await wait_for_pending_automation_events()

    user_messages = [item for item in bot.sent if item.chat.id == user_id]
    assert len(user_messages) == 2
    assert user_messages[0].reply_markup is None
    assert [button.text for button in _button_data(user_messages[1].reply_markup)] == ["Главное меню"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["meditation_card", "listening"])
async def test_crisis_flag_suppresses_meditation_timeout(stage):
    user_id = 94000113 if stage == "meditation_card" else 94000114
    await _prepare_user(user_id, stage=stage)
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        user.crisis_detected_at = funnel.msk_now()
        state = await session.get(FunnelState, user_id)
        state.stage = stage
        state.is_active = True
        state.stage_timeout_at = funnel.msk_now() - timedelta(minutes=1)
        state.stage_timeout_sent = False
        await session.commit()

    bot = CaptureBot()
    await funnel.process_funnel_followups(bot, single_iteration=True)

    assert bot.sent == []
    async with async_session_maker() as session:
        state = await session.get(FunnelState, user_id)
        assert state.stage_timeout_sent is True


@pytest.mark.asyncio
async def test_offer_and_no_strategy_callbacks_show_spec_navigation():
    user_id = 94000103
    await _prepare_user(user_id)
    bot = CaptureBot()
    callback = SimpleNamespace(
        data="funnel_offer_requested",
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=user_id)),
        bot=bot,
        answer=AsyncMock(),
    )

    await funnel.funnel_offer_requested(callback, _state(user_id))
    assert [button.text for button in _button_data(bot.sent[-1].reply_markup)] == [
        "Записаться на сессию",
        "У меня вопросы",
        "Хочу, но что-то останавливает",
        "Подробнее о Виталии",
        "Выбрать другую практику",
        "Главное меню",
    ]

    bot.sent.clear()
    callback.data = "funnel_no_strategy_selected"
    await funnel.funnel_no_strategy_selected(callback, _state(user_id))
    assert [button.text for button in _button_data(bot.sent[-1].reply_markup)] == [
        "📢 Подписаться на канал",
        "Выбрать другую практику",
        "Главное меню",
    ]


@pytest.mark.asyncio
async def test_admin_content_groups_editable_demo_screens_without_technical_names():
    user_id = 94000104
    await _prepare_user(user_id)
    admin = SimpleNamespace(from_user=SimpleNamespace(id=999999999))
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as send_content:
        await handlers.admin_content_list(admin)

    markup = send_content.call_args.args[3]
    labels = [button.text for button in _button_data(markup)]
    assert "📂 Гипно-медитации и воронка" in labels
    assert "📂 Дневник" in labels
    assert "📂 Расширенное меню" in labels
    assert all("funnel_" not in label and "source_type" not in label for label in labels)


@pytest.mark.asyncio
async def test_main_menu_uses_configured_media_and_resume_button_in_one_anchor():
    user_id = 94000105
    await _prepare_user(user_id)
    bot = CaptureBot()
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        user.welcome_shown_at = user.created_at
        user.current_topic_slug = "free_session"
        user.current_dialogue_id = 4
        session.add(DBMessage(
            user_id=user_id,
            topic_slug="free_session",
            dialogue_id=4,
            role="user",
            content="Сохрани контекст для демо",
        ))
        content = await session.get(Content, "start_msg")
        if not content:
            content = Content(key="start_msg")
            session.add(content)
        content.text_content = "Меню для демо"
        await session.execute(delete(ContentMedia).where(ContentMedia.content_key == "start_msg"))
        session.add(ContentMedia(content_key="start_msg", file_type="photo", file_id="real-demo-photo"))
        await session.commit()
    invalidate_content_cache()

    target = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=user_id),
        bot=bot,
    )
    await handlers.send_main_menu(target, bot=bot)

    assert len(bot.sent) == 1
    assert bot.sent[0].kind == "photo"
    labels = [button.text for button in _button_data(bot.sent[0].reply_markup)]
    assert "▶️ Продолжить диалог" in labels


@pytest.mark.asyncio
async def test_previous_version_db_marks_existing_users_as_welcome_seen(tmp_path):
    legacy_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.sqlite'}")
    try:
        async with legacy_engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY, first_name VARCHAR, created_at DATETIME"
                ")"
            ))
            await conn.execute(text(
                "INSERT INTO users (id, first_name, created_at) "
                "VALUES (1, 'Legacy', '2026-11-01 10:00:00')"
            ))
            await _ensure_sqlite_compat_schema(conn)
            migrated = await conn.scalar(text(
                'SELECT "welcome_shown_at" FROM "users" WHERE "id" = 1'
            ))
        assert migrated is not None
    finally:
        await legacy_engine.dispose()


@pytest.mark.asyncio
async def test_main_menu_anchor_replaces_media_across_photo_and_text_screens():
    scenarios = [
        ("photo", "photo", "old-photo", "new-photo"),
        ("photo", "text", "old-photo", None),
        ("text", "photo", None, "new-photo"),
    ]
    for index, (current_kind, desired_kind, current_media, desired_media) in enumerate(scenarios, start=1):
        user_id = 94000120 + index
        await _prepare_user(user_id)
        bot = CaptureBot()
        current = SimpleNamespace(
            message_id=500 + index,
            content_type=current_kind,
            chat=SimpleNamespace(id=user_id),
            photo=[SimpleNamespace(file_id=current_media)] if current_kind == "photo" else None,
            video=None,
            edit_text=AsyncMock(),
            edit_caption=AsyncMock(),
        )
        target = SimpleNamespace(
            message=current,
            from_user=SimpleNamespace(id=user_id),
            bot=bot,
        )
        media = [SimpleNamespace(file_type=desired_kind, file_id=desired_media)] if desired_media else []

        await render_main_menu_anchor(
            target,
            text=f"Меню {index}",
            media=media,
            reply_markup=None,
            bot=bot,
        )

        if current_kind == desired_kind == "photo":
            assert bot.edited_media["message_id"] == current.message_id
            assert bot.edited_media["media"].media == desired_media
        elif desired_kind == "text":
            assert bot.sent[-1].kind == "text"
            assert bot.sent[-1].text == f"Меню {index}"
            assert bot.edited_reply_markup["message_id"] == current.message_id
        else:
            assert bot.sent[-1].kind == "photo"
            assert bot.sent[-1].photo[-1].file_id == desired_media
            assert bot.edited_reply_markup["message_id"] == current.message_id


@pytest.mark.asyncio
async def test_landing_back_navigation_preserves_funnel_origin():
    user_id = 94000106
    await _prepare_user(user_id, stage="strategy_faq")
    async with async_session_maker() as session:
        content = await session.get(Content, "btn_funnel_landing")
        if not content:
            content = Content(key="btn_funnel_landing", button_text="Записаться")
            session.add(content)
        else:
            content.button_text = "Записаться"
        await session.commit()

    markup = await funnel.build_landing_keyboard_for_user(user_id)
    buttons = _button_data(markup)
    assert buttons[-1].callback_data == "funnel_show_faq"
    async with async_session_maker() as session:
        landing = await session.scalar(
            select(FunnelLanding).where(FunnelLanding.user_id == user_id)
        )
        assert landing.origin_stage == "strategy_faq"


@pytest.mark.asyncio
async def test_admin_button_action_update_keeps_human_editor_flow():
    user_id = 94000107
    await _prepare_user(user_id)
    async with async_session_maker() as session:
        button = await session.scalar(
            select(MenuButton).where(MenuButton.section_key == "main")
        )
        button_id = button.id

    state = _state(999999999)
    callback = SimpleNamespace(
        data=f"admin_menu_button_action:{button_id}",
        from_user=SimpleNamespace(id=999999999),
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )
    await handlers.admin_menu_button_action_start(callback, state)
    assert await state.get_state() == handlers.AdminStates.menu_button_action.state

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=999999999),
        text="https://example.test/investor-demo",
        answer=AsyncMock(),
    )
    await handlers.admin_menu_button_action_save(message, state)

    async with async_session_maker() as session:
        button = await session.get(MenuButton, button_id)
        assert button.action_type == "url"
        assert button.action_value == "https://example.test/investor-demo"
    assert await state.get_state() is None
    assert message.answer.await_count == 1
    assert any(
        item.callback_data == f"admin_menu_button_action:{button_id}"
        for row in message.answer.call_args.kwargs["reply_markup"].inline_keyboard
        for item in row
    )


@pytest.mark.asyncio
async def test_admin_action_update_for_content_button_changes_content_link_without_conflict():
    user_id = 94000110
    await _prepare_user(user_id)
    async with async_session_maker() as session:
        button = await session.scalar(
            select(MenuButton).where(MenuButton.content_key == "btn_rich_thinking")
        )
        content = await session.get(Content, "btn_rich_thinking")
        button.action_type = "url"
        button.action_value = None
        content.link_url = "https://old.example.test/channel"
        button_id = button.id
        await session.commit()

    state = _state(999999999)
    callback = SimpleNamespace(
        data=f"admin_menu_button_action:{button_id}",
        from_user=SimpleNamespace(id=999999999),
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )
    await handlers.admin_menu_button_action_start(callback, state)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=999999999),
        text="https://new.example.test/channel",
        answer=AsyncMock(),
    )
    await handlers.admin_menu_button_action_save(message, state)

    async with async_session_maker() as session:
        button = await session.get(MenuButton, button_id)
        content = await session.get(Content, "btn_rich_thinking")
        assert button.action_type == "url"
        assert button.action_value is None
        assert content.link_url == "https://new.example.test/channel"
    _, visible = await load_menu_buttons("extended")
    rendered = button_to_telegram(next(item for item in visible if item.id == button_id))
    assert rendered.url == "https://new.example.test/channel"

    webapp_state = _state(999999999)
    await handlers.admin_menu_button_action_start(callback, webapp_state)
    message.text = "webapp:https://new.example.test/app"
    await handlers.admin_menu_button_action_save(message, webapp_state)
    async with async_session_maker() as session:
        button = await session.get(MenuButton, button_id)
        content = await session.get(Content, "btn_rich_thinking")
        assert button.action_type == "web_app"
        assert button.action_value is None
        assert content.link_url == "https://new.example.test/app"
    _, visible = await load_menu_buttons("extended")
    rendered = button_to_telegram(next(item for item in visible if item.id == button_id))
    assert rendered.web_app.url == "https://new.example.test/app"

    invalid_webapp_state = _state(999999999)
    await handlers.admin_menu_button_action_start(callback, invalid_webapp_state)
    message.text = "webapp:http://insecure.example.test/app"
    await handlers.admin_menu_button_action_save(message, invalid_webapp_state)
    assert await invalid_webapp_state.get_state() == handlers.AdminStates.menu_button_action.state
    async with async_session_maker() as session:
        button = await session.get(MenuButton, button_id)
        content = await session.get(Content, "btn_rich_thinking")
        assert button.action_type == "web_app"
        assert content.link_url == "https://new.example.test/app"


@pytest.mark.asyncio
async def test_admin_action_rejects_unknown_callback_and_invalid_url():
    user_id = 94000111
    await _prepare_user(user_id)
    async with async_session_maker() as session:
        button = await session.scalar(select(MenuButton).where(MenuButton.section_key == "main"))
        original_type = button.action_type
        original_value = button.action_value
        button_id = button.id
        await session.commit()

    state = _state(999999999)
    callback = SimpleNamespace(
        data=f"admin_menu_button_action:{button_id}",
        from_user=SimpleNamespace(id=999999999),
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )
    await handlers.admin_menu_button_action_start(callback, state)
    invalid = SimpleNamespace(
        from_user=SimpleNamespace(id=999999999),
        text="not-a-real-menu-action",
        answer=AsyncMock(),
    )
    await handlers.admin_menu_button_action_save(invalid, state)
    assert await state.get_state() == handlers.AdminStates.menu_button_action.state
    async with async_session_maker() as session:
        button = await session.get(MenuButton, button_id)
        assert (button.action_type, button.action_value) == (original_type, original_value)

    invalid.text = "https:// bad.example.test"
    await handlers.admin_menu_button_action_save(invalid, state)
    assert await state.get_state() == handlers.AdminStates.menu_button_action.state


async def _set_funnel_switches(*, timeouts=True, nurture=True, remarketing=True):
    await init_db()
    async with async_session_maker() as session:
        config = await session.get(FunnelConfig, 1)
        config.meditation_timeouts_enabled = timeouts
        config.nurture_enabled = nurture
        config.remarketing_enabled = remarketing
        await session.commit()


@pytest.mark.asyncio
async def test_funnel_switches_cancel_due_messages_without_backlog():
    user_ids = (94000201, 94000202, 94000203)
    for user_id in user_ids:
        await _prepare_user(user_id)
    now = funnel.msk_now()
    try:
        await _set_funnel_switches(timeouts=False, nurture=False, remarketing=False)
        async with async_session_maker() as session:
            timeout_state = await session.get(FunnelState, user_ids[0])
            timeout_state.stage = "meditation_card"
            timeout_state.stage_timeout_at = now - timedelta(minutes=1)
            timeout_state.stage_timeout_sent = False

            nurture_state = await session.get(FunnelState, user_ids[1])
            nurture_state.stage = "post_analysis"
            nurture_state.next_followup_at = now - timedelta(minutes=1)
            nurture_state.followup_sequence_anchor = now - timedelta(days=1)
            nurture_state.reminder_count = 0

            remarketing_state = await session.get(FunnelState, user_ids[2])
            remarketing_state.stage = "remarketing_7d"
            remarketing_state.next_followup_at = now - timedelta(minutes=1)
            remarketing_state.followup_sequence_anchor = now - timedelta(days=7)
            await session.commit()

        bot = CaptureBot()
        await funnel.process_funnel_followups(bot, single_iteration=True)
        assert bot.sent == []

        async with async_session_maker() as session:
            timeout_state = await session.get(FunnelState, user_ids[0])
            nurture_state = await session.get(FunnelState, user_ids[1])
            remarketing_state = await session.get(FunnelState, user_ids[2])
            assert timeout_state.stage_timeout_at is None
            assert timeout_state.stage_timeout_sent is True
            assert nurture_state.next_followup_at is None
            assert nurture_state.followup_sequence_anchor is None
            assert remarketing_state.next_followup_at is None
            assert remarketing_state.is_active is False
            assert remarketing_state.stage == "completed_unconverted"

        await _set_funnel_switches()
        await funnel.process_funnel_followups(bot, single_iteration=True)
        assert bot.sent == []
    finally:
        await _set_funnel_switches()


@pytest.mark.asyncio
async def test_funnel_switches_are_independent_and_persist_through_admin_actions():
    await _set_funnel_switches()
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=999999999),
        answer=AsyncMock(),
    )
    try:
        with patch("funnel.show_admin_funnel_config", new=AsyncMock()):
            await funnel.admin_fcfg_toggle_nurture(callback)
        async with async_session_maker() as session:
            config = await session.get(FunnelConfig, 1)
            assert config.nurture_enabled is False
            assert config.meditation_timeouts_enabled is True
            assert config.remarketing_enabled is True

        with patch("funnel.show_admin_funnel_config", new=AsyncMock()):
            await funnel.admin_fcfg_toggle_remarketing(callback)
        async with async_session_maker() as session:
            config = await session.get(FunnelConfig, 1)
            assert config.nurture_enabled is False
            assert config.meditation_timeouts_enabled is True
            assert config.remarketing_enabled is False
            markup = funnel.funnel_config_keyboard(config)
            labels = [button.text for row in markup.inline_keyboard for button in row]
        assert any("Напоминания после практики" in label and "ВЫКЛ" in label for label in labels)
        assert any("Повторное предложение через 7 дней" in label and "ВЫКЛ" in label for label in labels)

        with patch("funnel.show_admin_funnel_config", new=AsyncMock()):
            await funnel.admin_fcfg_toggle_timeouts(callback)
        async with async_session_maker() as session:
            config = await session.get(FunnelConfig, 1)
            assert config.meditation_timeouts_enabled is False
            assert config.nurture_enabled is False
            assert config.remarketing_enabled is False
        callback.answer.assert_awaited()
    finally:
        await _set_funnel_switches()


@pytest.mark.asyncio
async def test_pobudu_s_etim_allows_enabled_nurture_without_immediate_offer():
    user_id = 94000204
    await _prepare_user(user_id)
    await _set_funnel_switches()
    due_time = funnel.msk_now() + timedelta(hours=1)
    anchor_time = funnel.msk_now()
    try:
        async with async_session_maker() as session:
            state = await session.get(FunnelState, user_id)
            state.stage = "strategy_offer"
            state.is_active = True
            state.next_followup_at = due_time
            state.followup_sequence_anchor = anchor_time
            await session.commit()

        bot = CaptureBot()
        target = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )
        await funnel.show_no_strategy_options(target, bot=bot)
        assert len(bot.sent) == 1
        async with async_session_maker() as session:
            state = await session.get(FunnelState, user_id)
            assert state.stage == "refusal_declined"
            assert state.is_active is True
            assert state.next_followup_at == due_time
            assert state.followup_sequence_anchor == anchor_time

        with patch("funnel.msk_now", return_value=due_time + timedelta(minutes=5)):
            await funnel.process_funnel_followups(bot, single_iteration=True)
        assert len(bot.sent) == 2
    finally:
        await _set_funnel_switches()


@pytest.mark.asyncio
async def test_pobudu_s_etim_does_not_schedule_when_nurture_is_disabled():
    user_id = 94000205
    await _prepare_user(user_id)
    await _set_funnel_switches(nurture=False)
    try:
        bot = CaptureBot()
        target = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )
        await funnel.show_no_strategy_options(target, bot=bot)
        async with async_session_maker() as session:
            state = await session.get(FunnelState, user_id)
            assert state.stage == "refusal_declined"
            assert state.is_active is False
            assert state.next_followup_at is None
            assert state.followup_sequence_anchor is None
        await _set_funnel_switches()
        await funnel.process_funnel_followups(bot, single_iteration=True)
        assert len(bot.sent) == 1
    finally:
        await _set_funnel_switches()


@pytest.mark.asyncio
async def test_crisis_flag_has_priority_over_enabled_followup_switches():
    user_ids = (94000206, 94000207)
    for user_id in user_ids:
        await _prepare_user(user_id)
    await _set_funnel_switches()
    now = funnel.msk_now()
    try:
        async with async_session_maker() as session:
            for user_id in user_ids:
                user = await session.get(User, user_id)
                user.crisis_detected_at = now
            nurture_state = await session.get(FunnelState, user_ids[0])
            nurture_state.stage = "post_analysis"
            nurture_state.next_followup_at = now - timedelta(minutes=1)
            nurture_state.followup_sequence_anchor = now - timedelta(days=1)
            remarketing_state = await session.get(FunnelState, user_ids[1])
            remarketing_state.stage = "remarketing_7d"
            remarketing_state.next_followup_at = now - timedelta(minutes=1)
            remarketing_state.followup_sequence_anchor = now - timedelta(days=7)
            await session.commit()

        bot = CaptureBot()
        await funnel.process_funnel_followups(bot, single_iteration=True)
        assert bot.sent == []
        async with async_session_maker() as session:
            for user_id in user_ids:
                state = await session.get(FunnelState, user_id)
                assert state.is_active is False
                assert state.next_followup_at is None
                assert state.followup_sequence_anchor is None
    finally:
        await _set_funnel_switches()


@pytest.mark.asyncio
async def test_legacy_funnel_config_migration_enables_new_switches(tmp_path):
    legacy_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_funnel.sqlite'}")
    try:
        async with legacy_engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE funnel_config ("
                "id INTEGER PRIMARY KEY, page_size INTEGER, first_reminder_hours INTEGER, "
                "next_reminder_hours INTEGER, resume_after_hours INTEGER, "
                "reminder_intervals VARCHAR NOT NULL, meditation_timeout_hours INTEGER NOT NULL"
                ")"
            ))
            await conn.execute(text(
                "INSERT INTO funnel_config "
                "(id, page_size, first_reminder_hours, next_reminder_hours, resume_after_hours, reminder_intervals, meditation_timeout_hours) "
                "VALUES (1, 25, 8, 30, 48, '2,4', 3)"
            ))
            await _ensure_sqlite_compat_schema(conn)
            row = (await conn.execute(text(
                "SELECT page_size, reminder_intervals, meditation_timeout_hours, "
                "meditation_timeouts_enabled, nurture_enabled, remarketing_enabled "
                "FROM funnel_config WHERE id = 1"
            ))).one()
        assert row == (25, "2,4", 3, 1, 1, 1)
    finally:
        await legacy_engine.dispose()


@pytest.mark.asyncio
async def test_nurture_disable_waits_for_selected_worker_send():
    user_id = 94000208
    await _prepare_user(user_id)
    await _set_funnel_switches()
    now = funnel.msk_now()
    try:
        async with async_session_maker() as session:
            state = await session.get(FunnelState, user_id)
            state.stage = "post_analysis"
            state.is_active = True
            state.next_followup_at = now - timedelta(minutes=1)
            state.followup_sequence_anchor = now - timedelta(days=1)
            await session.commit()

        pause_started = asyncio.Event()
        release_send = asyncio.Event()
        timeline = []

        async def pause_before_send(target_user_id):
            if target_user_id != user_id:
                return
            timeline.append("selected")
            pause_started.set()
            await release_send.wait()

        bot = CaptureBot()
        original_send_message = bot.send_message

        async def send_message(*args, **kwargs):
            if args and args[0] == user_id:
                timeline.append("sent")
            return await original_send_message(*args, **kwargs)

        bot.send_message = send_message
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=999999999),
            answer=AsyncMock(side_effect=lambda *args, **kwargs: timeline.append("off-confirmed")),
        )
        with patch("funnel._before_funnel_followup_send", new=pause_before_send), patch(
            "funnel.show_admin_funnel_config", new=AsyncMock()
        ):
            worker_task = asyncio.create_task(funnel.process_funnel_followups(bot, single_iteration=True))
            await asyncio.wait_for(pause_started.wait(), timeout=2)
            toggle_task = asyncio.create_task(funnel.admin_fcfg_toggle_nurture(callback))
            await asyncio.sleep(0.05)
            assert toggle_task.done() is False
            release_send.set()
            await worker_task
            await toggle_task

        assert timeline.index("sent") < timeline.index("off-confirmed")
        target_messages = [item for item in bot.sent if item.chat.id == user_id]
        assert len(target_messages) == 1
        async with async_session_maker() as session:
            state = await session.get(FunnelState, user_id)
            assert state.next_followup_at is None
        await funnel.process_funnel_followups(bot, single_iteration=True)
        assert len([item for item in bot.sent if item.chat.id == user_id]) == 1
    finally:
        if "release_send" in locals():
            release_send.set()
        await _set_funnel_switches()


@pytest.mark.asyncio
async def test_nurture_disable_waits_for_manual_followup_send():
    user_id = 94000209
    await _prepare_user(user_id)
    await _set_funnel_switches()
    try:
        async with async_session_maker() as session:
            state = await session.get(FunnelState, user_id)
            state.stage = "post_analysis"
            state.is_active = True
            state.next_followup_at = funnel.msk_now() + timedelta(days=1)
            state.followup_sequence_anchor = funnel.msk_now()
            await session.commit()

            pause_started = asyncio.Event()
            release_send = asyncio.Event()

            async def pause_before_send(target_user_id):
                if target_user_id != user_id:
                    return
                pause_started.set()
                await release_send.wait()

            bot = CaptureBot()
            callback = SimpleNamespace(
                from_user=SimpleNamespace(id=999999999),
                answer=AsyncMock(),
            )
            with patch("funnel._before_funnel_followup_send", new=pause_before_send), patch(
                "funnel.show_admin_funnel_config", new=AsyncMock()
            ):
                manual_task = asyncio.create_task(funnel.send_funnel_followup_message(session, bot, state))
                await asyncio.wait_for(pause_started.wait(), timeout=2)
                toggle_task = asyncio.create_task(funnel.admin_fcfg_toggle_nurture(callback))
                await asyncio.sleep(0.05)
                assert toggle_task.done() is False
                release_send.set()
                assert await manual_task is True
                await toggle_task

            assert len([item for item in bot.sent if item.chat.id == user_id]) == 1
            assert await funnel.send_funnel_followup_message(session, bot, state) is False
            assert len([item for item in bot.sent if item.chat.id == user_id]) == 1
    finally:
        if "release_send" in locals():
            release_send.set()
        await _set_funnel_switches()


@pytest.mark.asyncio
async def test_remarketing_off_preserves_active_nurture_schedule():
    user_id = 94000210
    await _prepare_user(user_id)
    await _set_funnel_switches(remarketing=False)
    due_time = funnel.msk_now() + timedelta(days=1)
    anchor_time = funnel.msk_now()
    try:
        async with async_session_maker() as session:
            state = await session.get(FunnelState, user_id)
            state.stage = "post_analysis"
            state.is_active = True
            state.reminder_count = 2
            state.next_followup_at = due_time
            state.followup_sequence_anchor = anchor_time
            await session.commit()

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await funnel.funnel_remind_7d(callback, AsyncMock())

        async with async_session_maker() as session:
            state = await session.get(FunnelState, user_id)
            assert state.stage == "post_analysis"
            assert state.is_active is True
            assert state.reminder_count == 2
            assert state.next_followup_at == due_time
            assert state.followup_sequence_anchor == anchor_time
        callback.message.answer.assert_not_awaited()
    finally:
        await _set_funnel_switches()
