from __future__ import annotations

import html
import logging
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from admin_roles import AdminPermission, user_has_permission
from database import (
    Content,
    ContentMedia,
    Topic,
    TopicDialogueConfig,
    TestQuestion,
    TestSet,
    User,
    async_session_maker,
)
from universal_tests import expected_test_set_question_count, is_esoteric_question, is_test_set_ready


logger = logging.getLogger(__name__)

router = Router(name="topic_admin")


class TopicAdminStates(StatesGroup):
    waiting_welcome_text = State()
    waiting_welcome_media = State()
    waiting_start_button_text = State()
    waiting_prerecorded_message = State()
    waiting_summary_prompt = State()


async def _is_admin(user_id: int) -> bool:
    return await user_has_permission(user_id, AdminPermission.CONTENT)


def _extract_user_id(event: Any) -> int:
    from_user = getattr(event, "from_user", None)
    return getattr(from_user, "id", 0) if from_user else 0



async def _get_or_create_config(session, topic_slug: str) -> TopicDialogueConfig:
    cfg = (await session.execute(
        select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == topic_slug)
    )).scalar_one_or_none()
    if not cfg:
        cfg = TopicDialogueConfig(topic_slug=topic_slug)
        session.add(cfg)
        await session.commit()
    return cfg


def _cfg_menu_keyboard(cfg: TopicDialogueConfig) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    slug = cfg.topic_slug

    reset_icon = "✅ ВКЛ" if cfg.reset_dialogue_on_entry else "❌ ВЫКЛ"
    start_btn_icon = "✅ ВКЛ" if cfg.start_dialogue_button_enabled else "❌ ВЫКЛ"
    pre_test_icon = "✅ ВКЛ" if cfg.pre_dialogue_test_enabled else "❌ ВЫКЛ"
    summary_icon = "✅ ВКЛ" if cfg.summary_generation_enabled else "❌ ВЫКЛ"

    builder.button(text="✏️ Изменить приветствие", callback_data=f"cfg_edit_welcome_{slug}")
    if cfg.welcome_text:
        builder.button(text="🗑 Очистить приветствие", callback_data=f"cfg_clear_welcome_{slug}")

    builder.button(text="🖼 Загрузить медиа приветствия", callback_data=f"cfg_edit_media_{slug}")
    if cfg.welcome_media_id:
        builder.button(text="🗑 Удалить медиа", callback_data=f"cfg_clear_media_{slug}")

    builder.button(text=f"🔄 Сброс контекста при входе: {reset_icon}", callback_data=f"cfg_toggle_reset_{slug}")
    builder.button(text=f"🔘 Кнопка «Начать диалог»: {start_btn_icon}", callback_data=f"cfg_toggle_startbtn_{slug}")
    builder.button(text="✏️ Текст кнопки «Начать»", callback_data=f"cfg_edit_btntext_{slug}")

    builder.button(text="💬 Первое сообщение ассистента", callback_data=f"cfg_edit_prerecorded_{slug}")
    if cfg.prerecorded_first_message:
        builder.button(text="🗑 Очистить первое сообщение", callback_data=f"cfg_clear_prerecorded_{slug}")

    builder.button(text=f"🩺 Тест перед диалогом: {pre_test_icon}", callback_data=f"cfg_toggle_pretest_{slug}")
    builder.button(text="📋 Выбрать TestSet", callback_data=f"cfg_pick_testset_{slug}")

    builder.button(text=f"📝 Генерация итогов: {summary_icon}", callback_data=f"cfg_toggle_summary_{slug}")
    builder.button(text="✏️ Промпт для итогов", callback_data=f"cfg_edit_sumprompt_{slug}")
    if cfg.summary_prompt:
        builder.button(text="🗑 Сбросить промпт итогов", callback_data=f"cfg_clear_sumprompt_{slug}")

    builder.button(text="⬅️ Назад к настройкам темы", callback_data=f"edit_topic_{slug}")
    builder.adjust(1)
    return builder.as_markup()


def cancel_keyboard(slug: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"admin_topic_dialogue_cfg_{slug}")
    builder.adjust(1)
    return builder.as_markup()


async def render_dialogue_cfg_text(session, cfg: TopicDialogueConfig) -> str:
    topic = await session.scalar(select(Topic).where(Topic.slug == cfg.topic_slug))
    topic_title = topic.name if topic else cfg.topic_slug

    test_set_title = "Не выбран"
    if cfg.test_set_id:
        tset = await session.get(TestSet, cfg.test_set_id)
        if tset:
            test_set_title = f"{tset.title} (ID: {tset.id})"

    welcome_preview = (cfg.welcome_text[:120] + "...") if cfg.welcome_text and len(cfg.welcome_text) > 120 else (cfg.welcome_text or "Стандартный")
    prerecorded_preview = (cfg.prerecorded_first_message[:120] + "...") if cfg.prerecorded_first_message and len(cfg.prerecorded_first_message) > 120 else (cfg.prerecorded_first_message or "Не задано")
    sumprompt_preview = (cfg.summary_prompt[:120] + "...") if cfg.summary_prompt and len(cfg.summary_prompt) > 120 else (cfg.summary_prompt or "По умолчанию")

    return (
        f"⚙️ <b>Параметры диалога темы: {html.escape(topic_title)}</b> (<code>{cfg.topic_slug}</code>)\n\n"
        f"1. <b>Приветственный текст:</b>\n<i>{html.escape(welcome_preview)}</i>\n\n"
        f"2. <b>Медиа приветствия:</b> {'Прикреплено (ID: ' + str(cfg.welcome_media_id) + ')' if cfg.welcome_media_id else 'Нет'}\n"
        f"3. <b>Сброс диалога при каждом входе:</b> {'Да' if cfg.reset_dialogue_on_entry else 'Нет'}\n"
        f"4. <b>Кнопка «Начать диалог»:</b> {'Включена' if cfg.start_dialogue_button_enabled else 'Выключена'}\n"
        f"5. <b>Текст кнопки:</b> {html.escape(cfg.start_dialogue_button_text or 'Начать диалог')}\n"
        f"6. <b>Первое сообщение ассистента:</b>\n<i>{html.escape(prerecorded_preview)}</i>\n\n"
        f"7. <b>Пре-диалоговый тест:</b> {'Включен' if cfg.pre_dialogue_test_enabled else 'Выключен'}\n"
        f"8. <b>Набор тестов (TestSet):</b> {html.escape(test_set_title)}\n"
        f"9. <b>Генерация итогов сессии:</b> {'Включена' if cfg.summary_generation_enabled else 'Выключена'}\n"
        f"10. <b>Промпт итогов:</b> <i>{html.escape(sumprompt_preview)}</i>\n"
    )


@router.callback_query(F.data.startswith("admin_topic_dialogue_cfg_"))
async def open_topic_dialogue_cfg(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    await state.clear()
    slug = callback.data.split("admin_topic_dialogue_cfg_")[1]
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)

    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("cfg_toggle_reset_"))
async def toggle_reset_dialogue(callback: CallbackQuery):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_toggle_reset_")[1]
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        cfg.reset_dialogue_on_entry = not cfg.reset_dialogue_on_entry
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("cfg_toggle_startbtn_"))
async def toggle_start_button(callback: CallbackQuery):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_toggle_startbtn_")[1]
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        cfg.start_dialogue_button_enabled = not cfg.start_dialogue_button_enabled
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("cfg_toggle_pretest_"))
async def toggle_pre_test(callback: CallbackQuery):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_toggle_pretest_")[1]
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        enabling = not cfg.pre_dialogue_test_enabled
        if enabling:
            if not cfg.test_set_id:
                await callback.answer("Сначала выберите и настройте TestSet.", show_alert=True)
                return
            test_set = await session.get(TestSet, cfg.test_set_id)
            if not test_set:
                await callback.answer("Выбранный TestSet не найден. Выберите набор заново.", show_alert=True)
                return
            if test_set.is_archived:
                await callback.answer("Нельзя включить: TestSet архивирован. Выберите другой набор.", show_alert=True)
                return
            if not test_set.is_enabled:
                await callback.answer("Нельзя включить: TestSet выключен. Включите его в управлении наборами.", show_alert=True)
                return
            active_questions = list((await session.scalars(
                select(TestQuestion).where(
                    TestQuestion.test_set_id == cfg.test_set_id,
                    TestQuestion.is_active == True,
                )
            )).all())
            if slug in {"diagnostics", "express"}:
                active_count = sum(not is_esoteric_question(question) for question in active_questions)
            else:
                active_count = len(active_questions)
            if not is_test_set_ready(test_set, active_count, slug):
                expected = expected_test_set_question_count(test_set, slug)
                detail = f" Нужно активных вопросов: {expected}." if expected else " Нужен хотя бы один активный вопрос."
                await callback.answer(f"Анкета ещё не готова.{detail}", show_alert=True)
                return
        cfg.pre_dialogue_test_enabled = enabling
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("cfg_toggle_summary_"))
async def toggle_summary_generation(callback: CallbackQuery):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_toggle_summary_")[1]
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        cfg.summary_generation_enabled = not cfg.summary_generation_enabled
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("cfg_pick_testset_"))
async def pick_test_set_menu(callback: CallbackQuery):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_pick_testset_")[1]
    async with async_session_maker() as session:
        test_sets = (await session.execute(
            select(TestSet).where(TestSet.is_archived == False).order_by(TestSet.id.asc())
        )).scalars().all()

    builder = InlineKeyboardBuilder()
    for ts in test_sets:
        builder.button(text=f"📋 {ts.title} (ID: {ts.id})", callback_data=f"cfg_set_testset_{slug}_{ts.id}")
    builder.button(text="❌ Сбросить набор (Без теста)", callback_data=f"cfg_set_testset_{slug}_0")
    builder.button(text="⬅️ Назад", callback_data=f"admin_topic_dialogue_cfg_{slug}")
    builder.adjust(1)

    await callback.message.edit_text(
        "📋 <b>Выберите набор тестов для привязки к теме:</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cfg_set_testset_"))
async def set_test_set_selection(callback: CallbackQuery):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    parts = callback.data.split("cfg_set_testset_")[1].split("_")
    slug = parts[0]
    test_set_id = int(parts[1])

    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        if cfg.pre_dialogue_test_enabled:
            if test_set_id <= 0:
                await callback.answer("Сначала выключите предварительную анкету, затем сбросьте TestSet.", show_alert=True)
                return
            selected_set = await session.get(TestSet, test_set_id)
            if not selected_set:
                await callback.answer("Выбранный TestSet не найден.", show_alert=True)
                return
            if selected_set.is_archived:
                await callback.answer("Нельзя подключить архивный TestSet к активной предварительной анкете.", show_alert=True)
                return
            if not selected_set.is_enabled:
                await callback.answer("Нельзя подключить выключенный TestSet к активной предварительной анкете.", show_alert=True)
                return
            active_questions = list((await session.scalars(
                select(TestQuestion).where(
                    TestQuestion.test_set_id == selected_set.id,
                    TestQuestion.is_active == True,
                )
            )).all())
            if slug in {"diagnostics", "express"}:
                active_count = sum(not is_esoteric_question(question) for question in active_questions)
            else:
                active_count = len(active_questions)
            if not is_test_set_ready(selected_set, active_count, slug):
                expected = expected_test_set_question_count(selected_set, slug)
                detail = f" Нужно активных вопросов: {expected}." if expected else " Нужен хотя бы один активный вопрос."
                await callback.answer(f"Нельзя подключить: анкета ещё не готова.{detail}", show_alert=True)
                return
        cfg.test_set_id = test_set_id if test_set_id > 0 else None
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)

    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await callback.answer("Набор обновлен")


@router.callback_query(F.data.startswith("cfg_clear_welcome_"))
async def clear_welcome_text(callback: CallbackQuery):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_clear_welcome_")[1]
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        cfg.welcome_text = None
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer("Приветствие очищено")


@router.callback_query(F.data.startswith("cfg_clear_media_"))
async def clear_welcome_media(callback: CallbackQuery):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_clear_media_")[1]
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        cfg.welcome_media_id = None
        cfg.welcome_media_type = None
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer("Медиа удалено")


@router.callback_query(F.data.startswith("cfg_clear_prerecorded_"))
async def clear_prerecorded_message(callback: CallbackQuery):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_clear_prerecorded_")[1]
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        cfg.prerecorded_first_message = None
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer("Первое сообщение очищено")


@router.callback_query(F.data.startswith("cfg_clear_sumprompt_"))
async def clear_summary_prompt(callback: CallbackQuery):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_clear_sumprompt_")[1]
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        cfg.summary_prompt = None
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer("Промпт итогов сброшен")




@router.callback_query(F.data.startswith("cfg_edit_welcome_"))
async def start_edit_welcome(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_edit_welcome_")[1]
    await state.set_state(TopicAdminStates.waiting_welcome_text)
    await state.update_data(target_slug=slug)
    await callback.message.edit_text(
        "✏️ Отправьте новый приветственный текст для этой темы:",
        reply_markup=cancel_keyboard(slug),
    )
    await callback.answer()


@router.message(TopicAdminStates.waiting_welcome_text, F.text, ~F.text.startswith("/"))
async def save_welcome_text(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    slug = data.get("target_slug")
    if not slug:
        await state.clear()
        return
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        cfg.welcome_text = message.text
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    await state.clear()
    await message.answer("✅ Приветственный текст сохранен!\n\n" + text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("cfg_edit_media_"))
async def start_edit_media(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_edit_media_")[1]
    await state.set_state(TopicAdminStates.waiting_welcome_media)
    await state.update_data(target_slug=slug)
    await callback.message.edit_text(
        "🖼 Отправьте фото, видео, анимацию (GIF) или документ для приветственного сообщения:",
        reply_markup=cancel_keyboard(slug),
    )
    await callback.answer()


@router.message(TopicAdminStates.waiting_welcome_media, F.photo | F.video | F.document | F.animation)
async def save_welcome_media(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    slug = data.get("target_slug")
    if not slug:
        await state.clear()
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.animation:
        file_id = message.animation.file_id
        file_type = "animation"
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
    else:
        await message.answer("Неподдерживаемый тип медиа. Отправьте фото, видео, анимацию (GIF) или документ.")
        return

    ckey = f"topic_welcome_{slug}"
    async with async_session_maker() as session:
        content_row = await session.get(Content, ckey)
        if not content_row:
            content_row = Content(key=ckey, text_content=f"Welcome media for {slug}")
            session.add(content_row)
            await session.flush()
        media = ContentMedia(content_key=ckey, file_id=file_id, file_type=file_type)
        session.add(media)
        await session.flush()
        cfg = await _get_or_create_config(session, slug)
        cfg.welcome_media_id = media.id
        cfg.welcome_media_type = file_type
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    await state.clear()
    await message.answer("✅ Медиа сохранено!\n\n" + text, reply_markup=markup, parse_mode="HTML")



@router.callback_query(F.data.startswith("cfg_edit_btntext_"))
async def start_edit_btntext(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_edit_btntext_")[1]
    await state.set_state(TopicAdminStates.waiting_start_button_text)
    await state.update_data(target_slug=slug)
    await callback.message.edit_text(
        "✏️ Отправьте новый текст кнопки «Начать диалог» (например: <i>Начать сессию</i>):",
        reply_markup=cancel_keyboard(slug),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(TopicAdminStates.waiting_start_button_text, F.text, ~F.text.startswith("/"))
async def save_start_button_text(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    slug = data.get("target_slug")
    if not slug:
        await state.clear()
        return
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        cfg.start_dialogue_button_text = message.text.strip()
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    await state.clear()
    await message.answer("✅ Текст кнопки сохранен!\n\n" + text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("cfg_edit_prerecorded_"))
async def start_edit_prerecorded(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_edit_prerecorded_")[1]
    await state.set_state(TopicAdminStates.waiting_prerecorded_message)
    await state.update_data(target_slug=slug)
    await callback.message.edit_text(
        "💬 Отправьте текст первого сообщения ассистента, которое будет отправлено пользователю при старте диалога:",
        reply_markup=cancel_keyboard(slug),
    )
    await callback.answer()


@router.message(TopicAdminStates.waiting_prerecorded_message, F.text, ~F.text.startswith("/"))
async def save_prerecorded_message(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    slug = data.get("target_slug")
    if not slug:
        await state.clear()
        return
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        cfg.prerecorded_first_message = message.text.strip()
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    await state.clear()
    await message.answer("✅ Первое сообщение ассистента сохранено!\n\n" + text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("cfg_edit_sumprompt_"))
async def start_edit_summary_prompt(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    slug = callback.data.split("cfg_edit_sumprompt_")[1]
    await state.set_state(TopicAdminStates.waiting_summary_prompt)
    await state.update_data(target_slug=slug)
    await callback.message.edit_text(
        "📝 Отправьте специальный системный промпт для генерации итогов сессии в этой теме:",
        reply_markup=cancel_keyboard(slug),
    )
    await callback.answer()


@router.message(TopicAdminStates.waiting_summary_prompt, F.text, ~F.text.startswith("/"))
async def save_summary_prompt(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    slug = data.get("target_slug")
    if not slug:
        await state.clear()
        return
    async with async_session_maker() as session:
        cfg = await _get_or_create_config(session, slug)
        cfg.summary_prompt = message.text.strip()
        await session.commit()
        text = await render_dialogue_cfg_text(session, cfg)
        markup = _cfg_menu_keyboard(cfg)
    await state.clear()
    await message.answer("✅ Промпт итогов сохранен!\n\n" + text, reply_markup=markup, parse_mode="HTML")
