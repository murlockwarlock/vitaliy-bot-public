from __future__ import annotations

import html
import io
import json
import logging
import math
import re
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from admin_roles import AdminPermission, user_has_permission
from keyboards import standard_pagination_row
from database import (
    TestAttempt,
    TestQuestion,
    TestSession,
    TestSet,
    async_session_maker,
)
from file_parser import parse_formulas_file, parse_questions_file, parse_questions_txt_blocks
from universal_tests import (
    QUESTION_TYPE_CHOICE,
    QUESTION_TYPE_SCALE,
    QUESTION_TYPE_TEXT,
    get_answer_options,
    get_question_type,
    is_esoteric_question,
    json_dumps,
    normalize_question_type,
    reorder_questions,
    is_test_set_ready,
    validate_test_definition,
)

logger = logging.getLogger(__name__)

router = Router(name="test_set_admin")


class TestSetAdminStates(StatesGroup):
    waiting_new_title = State()
    waiting_new_slug = State()
    waiting_new_description = State()
    waiting_rename_title = State()
    waiting_edit_description = State()
    waiting_interpretation_prompt = State()
    waiting_system_prompt = State()
    waiting_questions_file = State()
    waiting_formulas_file = State()
    waiting_question_edit = State()


async def _is_admin(user_id: int) -> bool:
    if not user_id:
        return False
    return await user_has_permission(user_id, AdminPermission.CONTENT)


def _extract_user_id(event: Any) -> int:
    from_user = getattr(event, "from_user", None)
    return getattr(from_user, "id", 0) if from_user else 0



def _cancel_kb(callback_data: str = "admin_test_sets_list") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=callback_data)
    builder.adjust(1)
    return builder.as_markup()


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"[\s_-]+", "_", cleaned)[:50]


def _question_type_label(question_type: str) -> str:
    return {
        QUESTION_TYPE_TEXT: "текст",
        QUESTION_TYPE_SCALE: "шкала 1–10",
        QUESTION_TYPE_CHOICE: "варианты",
    }.get(question_type, question_type)


PRELIMINARY_RESULT_LABELS = {
    "off": "не показывать",
    "simple": "краткий итог",
    "formula": "итог по формуле",
}
HANDOFF_LABELS = {
    "prompt": "предложить продолжить",
    "direct": "сразу передать результат",
    "none": "не передавать",
}


def _normalize_question_options(raw_options) -> list[dict]:
    if raw_options is None:
        return []
    if isinstance(raw_options, str):
        raw_options = json.loads(raw_options)
    if not isinstance(raw_options, list):
        raise ValueError("Поле options должно быть списком.")

    result = []
    for item in raw_options:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("label") or "").strip()
            value = item.get("value")
        else:
            text = str(item).strip()
            value = None
        if not text:
            raise ValueError("Вариант ответа не может быть пустым.")
        result.append({"text": text, "value": value})
    return result


def _question_edit_payload(raw_text: str, question: TestQuestion) -> dict:
    raw_text = (raw_text or "").strip()
    if not raw_text:
        raise ValueError("Текст вопроса не может быть пустым.")

    if raw_text.startswith("{"):
        payload = json.loads(raw_text)
        if not isinstance(payload, dict):
            raise ValueError("Ожидается JSON-объект с полями вопроса.")
    else:
        payload = {"text": raw_text}

    text_value = str(payload.get("text", question.text) or "").strip()
    if not text_value:
        raise ValueError("Текст вопроса не может быть пустым.")

    question_type = normalize_question_type(payload.get("question_type", payload.get("type")), get_question_type(question))
    if question_type is None:
        raise ValueError("Тип вопроса: text, scale или choice.")

    options_present = "options" in payload or "answer_options" in payload or "answer_options_json" in payload
    options_raw = payload.get("options", payload.get("answer_options", payload.get("answer_options_json")))
    if options_present:
        options = _normalize_question_options(options_raw)
    else:
        options = json.loads(question.answer_options_json or "[]")
    if question_type == QUESTION_TYPE_SCALE:
        options = []

    allow_custom = payload.get("allow_custom_answer", payload.get("custom_answer", question.allow_custom_answer))
    if isinstance(allow_custom, str):
        allow_custom = allow_custom.strip().lower() in {"1", "true", "yes", "да"}

    return {
        "text": text_value,
        "question_type": question_type,
        "allow_custom_answer": bool(allow_custom),
        "answer_options_json": json_dumps(options) if options else None,
        "order_num": payload.get("order", payload.get("order_num", question.order_num)),
    }


@router.callback_query(F.data == "admin_test_sets_list")
@router.callback_query(F.data.startswith("admin_test_sets_list_"))
async def list_test_sets(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    if state:
        await state.clear()

    try:
        page = int(callback.data.rsplit("_", 1)[1]) if callback.data.startswith("admin_test_sets_list_") else 0
    except ValueError:
        page = 0
    page_size = 10
    async with async_session_maker() as session:
        base_query = select(TestSet).where(TestSet.is_archived == False).order_by(TestSet.id.asc())
        total = await session.scalar(select(func.count()).select_from(base_query.subquery())) or 0
        total_pages = max(1, math.ceil(total / page_size)) if total else 1
        page = max(0, min(page, total_pages - 1))
        test_sets = (await session.execute(
            base_query.offset(page * page_size).limit(page_size)
        )).scalars().all()

        builder = InlineKeyboardBuilder()
        for ts in test_sets:
            q_count = (await session.execute(
                select(func.count(TestQuestion.id)).where(TestQuestion.test_set_id == ts.id)
            )).scalar() or 0
            status_badge = "🟢" if ts.is_enabled else "🔴"
            builder.row(InlineKeyboardButton(
                text=f"{status_badge} {ts.title} ({q_count} вопр.)",
                callback_data=f"edit_testset_{ts.id}",
            ))

        builder.row(*standard_pagination_row(
            page,
            total_pages,
            lambda target: f"admin_test_sets_list_{target}",
        ))
        builder.row(InlineKeyboardButton(text="➕ Создать новый набор", callback_data="testset_create_new"))
        builder.row(InlineKeyboardButton(text="⬅️ В контент и программы", callback_data="admin_section_content"))

    text = f"🧪 <b>Управление наборами тестов</b>\n\nСтраница {page + 1} из {total_pages}. Выберите набор или создайте новый:"
    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "testset_create_new")
async def start_create_test_set(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    await state.set_state(TestSetAdminStates.waiting_new_title)
    await callback.message.edit_text(
        "📝 Введите название нового набора тестов (например: <i>Тест на уровень стресса</i>):",
        reply_markup=_cancel_kb("admin_test_sets_list"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(TestSetAdminStates.waiting_new_title, F.text, ~F.text.startswith("/"))
async def handle_new_title(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    title = message.text.strip()
    slug_candidate = _slugify(title)
    await state.update_data(new_title=title, new_slug=slug_candidate)
    await state.set_state(TestSetAdminStates.waiting_new_slug)
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Использовать '{slug_candidate}'", callback_data="testset_confirm_slug")
    builder.button(text="❌ Отмена", callback_data="admin_test_sets_list")
    builder.adjust(1)
    await message.answer(
        f"Название: <b>{html.escape(title)}</b>\n\n"
        f"Для внутренней ссылки можно использовать предложенное короткое название.\n"
        f"Нажмите «Использовать» или отправьте свой вариант латиницей:\n"
        f"<code>{slug_candidate}</code>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(TestSetAdminStates.waiting_new_slug, F.data == "testset_confirm_slug")
async def confirm_suggested_slug(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    data = await state.get_data()
    slug = data.get("new_slug", "test_set")
    await _proceed_to_description(callback.message, state, slug, edit=True)
    await callback.answer()


@router.message(TestSetAdminStates.waiting_new_slug, F.text, ~F.text.startswith("/"))
async def handle_custom_slug(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    slug = _slugify(message.text)
    if not slug:
        await message.answer(
            "Короткое название должно содержать буквы, цифры или подчёркивания.",
            reply_markup=_cancel_kb(),
        )
        return
    await _proceed_to_description(message, state, slug, edit=False)


async def _proceed_to_description(target, state: FSMContext, slug: str, edit: bool = False):
    await state.update_data(new_slug=slug)
    await state.set_state(TestSetAdminStates.waiting_new_description)
    text = "📄 Введите краткое описание набора (или отправьте '-' чтобы пропустить):"
    markup = _cancel_kb("admin_test_sets_list")
    if edit and hasattr(target, "edit_text"):
        await target.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(TestSetAdminStates.waiting_new_description, F.text, ~F.text.startswith("/"))
async def handle_new_description(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    title = data.get("new_title", "Новый тест")
    slug = data.get("new_slug", _slugify(title))
    desc = message.text.strip()
    if desc == "-":
        desc = None

    async with async_session_maker() as session:
        # Check slug collision deterministically (P1-8)
        base_slug = slug
        counter = 2
        while await session.scalar(select(TestSet).where(TestSet.slug == slug)):
            slug = f"{base_slug}_{counter}"
            counter += 1

        ts = TestSet(
            title=title,
            slug=slug,
            description=desc,
            is_enabled=True,
            show_progress=True,
        )
        session.add(ts)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            while True:
                slug = f"{base_slug}_{counter}"
                counter += 1
                if not await session.scalar(select(TestSet).where(TestSet.slug == slug)):
                    ts = TestSet(
                        title=title,
                        slug=slug,
                        description=desc,
                        is_enabled=True,
                        show_progress=True,
                    )
                    session.add(ts)
                    try:
                        await session.commit()
                        break
                    except IntegrityError:
                        await session.rollback()
        test_set_id = ts.id

    await state.clear()
    await message.answer(f"✅ Набор тестов <b>{html.escape(title)}</b> успешно создан!", parse_mode="HTML")
    await render_test_set_details(message, test_set_id)


async def render_test_set_details(event, test_set_id: int):
    async with async_session_maker() as session:
        ts = await session.get(TestSet, test_set_id)
        if not ts:
            return
        q_count = (await session.execute(
            select(func.count(TestQuestion.id)).where(TestQuestion.test_set_id == ts.id)
        )).scalar() or 0
        attempts_count = (await session.execute(
            select(func.count(TestAttempt.id)).where(TestAttempt.test_set_id == ts.id, TestAttempt.purpose != "preview")
        )).scalar() or 0

    builder = InlineKeyboardBuilder()
    enabled_icon = "🟢 ВКЛ" if ts.is_enabled else "🔴 ВЫКЛ"
    progress_icon = "🟢 ВКЛ" if ts.show_progress else "🔴 ВЫКЛ"
    calc_icon = "🟢 ВКЛ" if ts.calculation_enabled else "🔴 ВЫКЛ"
    sep_icon = "🟢 ВКЛ" if ts.separate_interpretation_prompt else "🔴 ВЫКЛ"

    builder.button(text=f"Статус: {enabled_icon}", callback_data=f"ts_toggle_enabled_{ts.id}")
    builder.button(text=f"Прогресс: {progress_icon}", callback_data=f"ts_toggle_progress_{ts.id}")
    builder.button(text=f"Формулы: {calc_icon}", callback_data=f"ts_toggle_calc_{ts.id}")
    builder.button(text=f"Отдельный текст результата: {sep_icon}", callback_data=f"ts_toggle_sep_{ts.id}")
    builder.button(
        text=f"Промежуточный итог: {PRELIMINARY_RESULT_LABELS.get(ts.preliminary_results_mode, 'не настроен')}",
        callback_data=f"ts_cycle_prelim_{ts.id}",
    )
    builder.button(
        text=f"Передача результата: {HANDOFF_LABELS.get(ts.handoff_mode, 'не настроена')}",
        callback_data=f"ts_cycle_handoff_{ts.id}",
    )

    builder.button(text="✏️ Изменить название", callback_data=f"ts_rename_{ts.id}")
    builder.button(text="✏️ Изменить описание", callback_data=f"ts_edit_desc_{ts.id}")
    builder.button(text="✏️ Инструкция для разбора", callback_data=f"ts_edit_interp_{ts.id}")
    builder.button(text="✏️ Системные правила", callback_data=f"ts_edit_sys_{ts.id}")

    builder.button(text=f"📥 Загрузить вопросы ({q_count})", callback_data=f"ts_upload_q_{ts.id}")
    builder.button(text=f"👁 Просмотр вопросов ({q_count})", callback_data=f"ts_view_q_{ts.id}")
    builder.button(text="📥 Загрузить правила расчёта", callback_data=f"ts_upload_f_{ts.id}")

    builder.button(text="▶️ Проверить набор", callback_data=f"ts_preview_{ts.id}")

    if attempts_count > 0:
        builder.button(text=f"📦 Архивировать набор ({attempts_count} попыток)", callback_data=f"confirm_ts_delete_{ts.id}")
    else:
        builder.button(text="🗑 Удалить набор", callback_data=f"confirm_ts_delete_{ts.id}")

    builder.button(text="⬅️ Назад к списку тестов", callback_data="admin_test_sets_list")
    builder.adjust(1)

    interp_preview = (ts.interpretation_prompt[:100] + "...") if ts.interpretation_prompt and len(ts.interpretation_prompt) > 100 else (ts.interpretation_prompt or "По умолчанию")
    formulas_present = "Загружены" if ts.formulas_json else "Нет"

    text = (
        f"📋 <b>Набор тестов: {html.escape(ts.title)}</b>\n\n"
        f"<b>Описание:</b> {html.escape(ts.description or 'Не указано')}\n"
        f"<b>Вопросов в наборе:</b> {q_count}\n"
        f"<b>Ожидаемое число вопросов:</b> {ts.expected_question_count or 'не задано'}\n"
        f"<b>Формулы:</b> {formulas_present}\n"
        f"<b>Завершенных попыток:</b> {attempts_count}\n\n"
        f"<b>Инструкция для разбора:</b>\n<i>{html.escape(interp_preview)}</i>\n"
    )

    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except TelegramBadRequest:
            await event.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("edit_testset_"))
async def open_test_set_details(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    await state.clear()
    ts_id = int(callback.data.split("edit_testset_")[1])
    await render_test_set_details(callback, ts_id)
    await callback.answer()


@router.callback_query(F.data.startswith("ts_toggle_enabled_"))
async def toggle_ts_enabled(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    ts_id = int(callback.data.split("ts_toggle_enabled_")[1])
    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if ts:
            if not ts.is_enabled:
                active_questions = list((await session.scalars(
                    select(TestQuestion).where(
                        TestQuestion.test_set_id == ts.id,
                        TestQuestion.is_active == True,
                    )
                )).all())
                if ts.slug in {"diagnostics", "express_psychological_profile"}:
                    active_count = sum(not is_esoteric_question(question) for question in active_questions)
                else:
                    active_count = len(active_questions)
                if not is_test_set_ready(ts, active_count):
                    await callback.answer(
                        f"Набор нельзя включить: нужно подготовить ожидаемое число активных вопросов ({ts.expected_question_count or 'не задано'}).",
                        show_alert=True,
                    )
                    return
            ts.is_enabled = not ts.is_enabled
            await session.commit()
    await render_test_set_details(callback, ts_id)
    await callback.answer()


@router.callback_query(F.data.startswith("ts_toggle_progress_"))
async def toggle_ts_progress(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    ts_id = int(callback.data.split("ts_toggle_progress_")[1])
    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if ts:
            ts.show_progress = not ts.show_progress
            await session.commit()
    await render_test_set_details(callback, ts_id)
    await callback.answer()


@router.callback_query(F.data.startswith("ts_toggle_calc_"))
async def toggle_ts_calc(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    ts_id = int(callback.data.split("ts_toggle_calc_")[1])
    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if ts:
            if not ts.calculation_enabled and not ts.formulas_json:
                await callback.answer("Сначала загрузите формулы для этого набора.", show_alert=True)
                return
            ts.calculation_enabled = not ts.calculation_enabled
            await session.commit()
    await render_test_set_details(callback, ts_id)
    await callback.answer()


@router.callback_query(F.data.startswith("ts_toggle_sep_"))
async def toggle_ts_sep(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    ts_id = int(callback.data.split("ts_toggle_sep_")[1])
    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if ts:
            ts.separate_interpretation_prompt = not ts.separate_interpretation_prompt
            await session.commit()
    await render_test_set_details(callback, ts_id)
    await callback.answer()


@router.callback_query(F.data.startswith("ts_cycle_prelim_"))
async def cycle_ts_prelim(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    ts_id = int(callback.data.split("ts_cycle_prelim_")[1])
    modes = ["off", "simple", "formula"]
    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if ts:
            cur_idx = modes.index(ts.preliminary_results_mode) if ts.preliminary_results_mode in modes else 0
            ts.preliminary_results_mode = modes[(cur_idx + 1) % len(modes)]
            await session.commit()
    await render_test_set_details(callback, ts_id)
    await callback.answer()


@router.callback_query(F.data.startswith("ts_cycle_handoff_"))
async def cycle_ts_handoff(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    ts_id = int(callback.data.split("ts_cycle_handoff_")[1])
    modes = ["prompt", "direct", "none"]
    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if ts:
            cur_idx = modes.index(ts.handoff_mode) if ts.handoff_mode in modes else 0
            ts.handoff_mode = modes[(cur_idx + 1) % len(modes)]
            await session.commit()
    await render_test_set_details(callback, ts_id)
    await callback.answer()


async def _ask_test_set_delete_confirmation(callback: CallbackQuery, ts_id: int):
    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if not ts:
            await callback.answer("Набор уже удален.", show_alert=True)
            return
        attempts_count = (await session.execute(
            select(func.count(TestAttempt.id)).where(TestAttempt.test_set_id == ts_id, TestAttempt.purpose != "preview")
        )).scalar() or 0
    action = "архивирован" if attempts_count else "удалён без возможности восстановления"
    if callback.message:
        await callback.message.edit_text(
            "⚠️ <b>Подтвердите действие</b>\n\n"
            f"Набор «{html.escape(ts.title)}» будет {action}.\n"
            "История завершённых попыток при архивации сохранится.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Да, продолжить", callback_data=f"confirmed_ts_delete_{ts_id}")],
                    [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"edit_testset_{ts_id}")],
                ]
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_ts_delete_"))
async def confirm_delete_or_archive_test_set(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    try:
        ts_id = int(callback.data.split("confirm_ts_delete_")[1])
    except (IndexError, ValueError):
        await callback.answer("Набор не найден.", show_alert=True)
        return
    await _ask_test_set_delete_confirmation(callback, ts_id)


async def _execute_test_set_delete(callback: CallbackQuery, state: FSMContext | None, ts_id: int):
    if state is not None:
        await state.clear()

    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if not ts:
            await callback.answer("Набор не найден.")
            return

        if ts.id == 1 or ts.slug == "diagnostics":
            await callback.answer("⚠️ Нельзя удалить или архивировать системный диагностический тест.", show_alert=True)
            return

        attempts_count = (await session.execute(
            select(func.count(TestAttempt.id)).where(TestAttempt.test_set_id == ts.id, TestAttempt.purpose != "preview")
        )).scalar() or 0

        if attempts_count > 0:
            # Audit safety requirement: do not destroy historical attempt audit trail
            ts.is_archived = True
            ts.is_enabled = False
            await session.commit()
            await callback.answer("Набор архивирован! История попыток сохранена.", show_alert=True)
        else:
            await session.execute(delete(TestAttempt).where(TestAttempt.test_set_id == ts_id, TestAttempt.purpose == "preview"))
            await session.execute(delete(TestSession).where(TestSession.test_set_id == ts_id))
            await session.execute(delete(TestQuestion).where(TestQuestion.test_set_id == ts_id))
            await session.delete(ts)
            await session.commit()
            await callback.answer("Набор тестов полностью удален.", show_alert=True)

    await list_test_sets(callback)


@router.callback_query(F.data.startswith("ts_delete_"))
async def delete_or_archive_test_set(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    try:
        ts_id = int(callback.data.split("ts_delete_")[1])
    except (IndexError, ValueError):
        await callback.answer("Набор не найден.", show_alert=True)
        return
    if isinstance(callback, CallbackQuery):
        await _ask_test_set_delete_confirmation(callback, ts_id)
        return
    await _execute_test_set_delete(callback, state, ts_id)


@router.callback_query(F.data.startswith("confirmed_ts_delete_"))
async def confirmed_delete_or_archive_test_set(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    try:
        ts_id = int(callback.data.split("confirmed_ts_delete_")[1])
    except (IndexError, ValueError):
        await callback.answer("Набор не найден.", show_alert=True)
        return
    await _execute_test_set_delete(callback, state, ts_id)


@router.callback_query(F.data.startswith("ts_view_q_"))
async def view_test_set_questions(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    ts_id = int(callback.data.split("ts_view_q_")[1])
    async with async_session_maker() as session:
        questions = (await session.execute(
            select(TestQuestion).where(TestQuestion.test_set_id == ts_id).order_by(TestQuestion.order_num.asc())
        )).scalars().all()

    if not questions:
        await callback.answer("В этом наборе еще нет вопросов.", show_alert=True)
        return

    lines = [
        f"📋 <b>Вопросы набора ({len(questions)} шт.):</b>",
        "Нажми на вопрос, чтобы изменить текст, тип, варианты или порядок.",
    ]
    builder = InlineKeyboardBuilder()
    for q in questions[:30]:
        opts = get_answer_options(q)
        type_label = _question_type_label(get_question_type(q))
        status = "" if q.is_active else " ⏸"
        lines.append(f"{q.order_num}. {html.escape(q.text[:80])} [{type_label}, {len(opts)} вар.]{status}")
        builder.row(InlineKeyboardButton(
            text=f"✏️ {q.order_num}. {q.text[:35]}",
            callback_data=f"ts_q_edit_{ts_id}_{q.id}",
        ))
        builder.row(
            InlineKeyboardButton(text="⬆️", callback_data=f"ts_q_up_{ts_id}_{q.id}"),
            InlineKeyboardButton(text="⬇️", callback_data=f"ts_q_down_{ts_id}_{q.id}"),
        )

    if len(questions) > 30:
        lines.append(f"\n<i>... и еще {len(questions) - 30} вопросов</i>")

    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"edit_testset_{ts_id}"))
    await callback.message.edit_text("\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("ts_q_edit_"))
async def start_question_edit(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    try:
        _, _, _, ts_id, question_id = callback.data.split("_", 4)
        ts_id, question_id = int(ts_id), int(question_id)
    except (ValueError, IndexError):
        await callback.answer("Ошибка параметров.", show_alert=True)
        return

    async with async_session_maker() as session:
        question = await session.scalar(
            select(TestQuestion).where(TestQuestion.id == question_id, TestQuestion.test_set_id == ts_id)
        )
    if not question:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return

    await state.set_state(TestSetAdminStates.waiting_question_edit)
    await state.update_data(target_ts_id=ts_id, target_question_id=question_id)
    options = json.loads(question.answer_options_json or "[]")
    type_label = _question_type_label(get_question_type(question))
    options_label = ", ".join(str(item.get("text", "")) for item in options if isinstance(item, dict)) or "не заданы"
    await callback.message.edit_text(
        "✏️ <b>Редактирование вопроса</b>\n\n"
        "Отправь новый текст, чтобы изменить формулировку вопроса.\n"
        "Тип ответа, варианты и порядок можно изменить через расширенный импорт.\n\n"
        f"Текущий текст:\n<i>{html.escape(question.text)}</i>\n"
        f"Тип ответа: <b>{html.escape(type_label)}</b>\n"
        f"Варианты: <i>{html.escape(options_label)}</i>",
        reply_markup=_cancel_kb(f"edit_testset_{ts_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(TestSetAdminStates.waiting_question_edit, F.text, ~F.text.startswith("/"))
async def save_question_edit(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    ts_id = data.get("target_ts_id")
    question_id = data.get("target_question_id")
    if not ts_id or not question_id:
        await state.clear()
        return

    try:
        async with async_session_maker() as session:
            question = await session.scalar(
                select(TestQuestion).where(TestQuestion.id == question_id, TestQuestion.test_set_id == ts_id)
            )
            if not question:
                raise ValueError("Вопрос не найден в выбранном наборе.")
            payload = _question_edit_payload(message.text, question)
            try:
                order_num = int(payload["order_num"])
            except (TypeError, ValueError) as exc:
                raise ValueError("Порядок вопроса должен быть целым числом.") from exc

            question.text = payload["text"]
            question.question_type = payload["question_type"]
            question.allow_custom_answer = payload["allow_custom_answer"]
            question.answer_options_json = payload["answer_options_json"]
            question.order_num = order_num

            all_questions = list((await session.scalars(
                select(TestQuestion)
                .where(TestQuestion.test_set_id == ts_id)
                .order_by(TestQuestion.order_num.asc(), TestQuestion.id.asc())
            )).all())
            ordered_questions = reorder_questions(all_questions, question.id, order_num)
            errors = validate_test_definition(ordered_questions)
            if errors:
                raise ValueError("\n".join(errors[:8]))
            await session.commit()
    except (ValueError, json.JSONDecodeError) as exc:
        await message.answer(f"Не удалось сохранить вопрос:\n{html.escape(str(exc))}", reply_markup=_cancel_kb(f"edit_testset_{ts_id}"))
        return

    await state.clear()
    await message.answer("✅ Вопрос обновлён.")
    await render_test_set_details(message, ts_id)


async def _move_question(callback: CallbackQuery, direction: int, state: FSMContext | None = None):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    try:
        _, _, _, ts_id, question_id = callback.data.split("_", 4)
        ts_id, question_id = int(ts_id), int(question_id)
    except (ValueError, IndexError):
        await callback.answer("Ошибка параметров.", show_alert=True)
        return

    async with async_session_maker() as session:
        questions = list((await session.scalars(
            select(TestQuestion)
            .where(TestQuestion.test_set_id == ts_id)
            .order_by(TestQuestion.order_num.asc(), TestQuestion.id.asc())
        )).all())
        index = next((idx for idx, item in enumerate(questions) if item.id == question_id), -1)
        target = index + direction
        if 0 <= index < len(questions) and 0 <= target < len(questions):
            reorder_questions(questions, question_id, target + 1)
            await session.commit()

    await render_test_set_details(callback, ts_id)
    await callback.answer()


@router.callback_query(F.data.startswith("ts_q_up_"))
async def move_question_up(callback: CallbackQuery, state: FSMContext | None = None):
    await _move_question(callback, -1, state)


@router.callback_query(F.data.startswith("ts_q_down_"))
async def move_question_down(callback: CallbackQuery, state: FSMContext | None = None):
    await _move_question(callback, 1, state)


@router.callback_query(F.data.startswith("ts_upload_q_"))
async def start_upload_questions(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    ts_id = int(callback.data.split("ts_upload_q_")[1])
    await state.set_state(TestSetAdminStates.waiting_questions_file)
    await state.update_data(target_ts_id=ts_id)
    await callback.message.edit_text(
        "📥 <b>Загрузка вопросов для набора</b>\n\n"
        "Отправьте файл <code>.xlsx</code>, <code>.csv</code>, <code>.txt</code> (блочный или табличный) или <code>.json</code>,\n"
        "либо вставьте текст с вопросами прямо в чат.",
        reply_markup=_cancel_kb(f"edit_testset_{ts_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(TestSetAdminStates.waiting_questions_file, F.document | F.text)
async def handle_questions_upload(message: Message, state: FSMContext, bot: Bot):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    ts_id = data.get("target_ts_id")
    if not ts_id:
        await state.clear()
        return

    file_io = io.BytesIO()
    filename = ""

    if message.document:
        filename = (message.document.file_name or "").lower()
        await bot.download(message.document, destination=file_io)
        file_io.seek(0)
    elif message.text and not message.text.startswith("/"):
        filename = "questions.txt"
        file_io.write(message.text.encode("utf-8"))
        file_io.seek(0)
    else:
        await message.answer("Не удалось прочитать содержимое. Попробуйте еще раз:", reply_markup=_cancel_kb(f"edit_testset_{ts_id}"))
        return

    # Parse questions using canonical parser
    parsed_questions = []
    parsed_formulas = []
    try:
        if filename.endswith(".json"):
            raw_str = file_io.getvalue().decode("utf-8", errors="replace")
            json_data = json.loads(raw_str)
            if isinstance(json_data, list):
                parsed_questions = json_data
            elif isinstance(json_data, dict):
                parsed_questions = json_data.get("questions", [])
                parsed_formulas = json_data.get("formulas", [])
        else:
            file_res = await parse_questions_file(file_io, filename)
            parsed_questions = file_res.get("questions", [])
            parsed_formulas = file_res.get("formulas", [])
    except Exception as exc:
        logger.warning("Questions parsing failed: %s", exc)

    if not parsed_questions:
        await message.answer(
            "❌ Ошибка разбора формата вопросов. Поддерживаются .xlsx, .csv, .txt (блочный и табличный) и .json.\nПопробуйте снова:",
            reply_markup=_cancel_kb(f"edit_testset_{ts_id}"),
        )
        return

    # Persist scoped strictly to ts_id. The old set remains intact if the new
    # payload cannot be normalized or committed.
    try:
        async with async_session_maker() as session:
            valid_cols = {
                "text", "category", "is_reverse", "comment", "variable_name",
                "allow_custom_answer", "buttons_layout", "answer_options_json", "question_type", "is_active",
            }
            prepared = []
            for index, item in enumerate(parsed_questions):
                if not isinstance(item, dict):
                    raise ValueError("Каждый вопрос должен быть JSON-объектом.")
                filtered = {k: v for k, v in item.items() if k in valid_cols}
                raw_options = item.get(
                    "answer_options_json",
                    item.get("options", item.get("answer_options")),
                )
                options = _normalize_question_options(raw_options)
                raw_type = item.get("question_type", item.get("type"))
                question_type = normalize_question_type(
                    raw_type,
                    QUESTION_TYPE_CHOICE if options else QUESTION_TYPE_TEXT,
                )
                if question_type is None:
                    raise ValueError("У вопроса указан неизвестный тип.")
                if question_type in {QUESTION_TYPE_TEXT, QUESTION_TYPE_SCALE}:
                    options = []

                filtered["answer_options_json"] = json_dumps(options) if options else None
                filtered["question_type"] = question_type
                if "allow_custom_answer" not in item and "custom_answer" in item:
                    filtered["allow_custom_answer"] = item["custom_answer"]
                if isinstance(filtered.get("allow_custom_answer"), str):
                    filtered["allow_custom_answer"] = filtered["allow_custom_answer"].strip().lower() in {
                        "1", "true", "yes", "да",
                    }
                if filtered.get("is_active") is None:
                    filtered["is_active"] = True
                try:
                    filtered["order_num"] = int(item.get("order_num", item.get("sort_order", index)))
                except (TypeError, ValueError):
                    filtered["order_num"] = index
                prepared.append(filtered)

            await session.execute(delete(TestQuestion).where(TestQuestion.test_set_id == ts_id))
            for index, filtered in enumerate(sorted(prepared, key=lambda item: item["order_num"]), start=1):
                filtered["order_num"] = index
                session.add(TestQuestion(test_set_id=ts_id, **filtered))

            if parsed_formulas:
                ts = await session.get(TestSet, ts_id)
                if ts:
                    ts.formulas_json = json.dumps(parsed_formulas, ensure_ascii=False)
                    ts.formulas_enabled = True
                    ts.calculation_enabled = True

            await session.commit()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        await message.answer(
            f"❌ Вопросы не сохранены: {html.escape(str(exc))}",
            reply_markup=_cancel_kb(f"edit_testset_{ts_id}"),
        )
        return

    await state.clear()
    await message.answer(f"✅ Успешно загружено <b>{len(parsed_questions)}</b> вопросов в набор!", parse_mode="HTML")
    await render_test_set_details(message, ts_id)


@router.callback_query(F.data.startswith("ts_upload_f_"))
async def start_upload_formulas(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    ts_id = int(callback.data.split("ts_upload_f_")[1])
    await state.set_state(TestSetAdminStates.waiting_formulas_file)
    await state.update_data(target_ts_id=ts_id)
    await callback.message.edit_text(
        "📥 <b>Загрузка правил расчёта</b>\n\n"
        "Отправьте файл с правилами расчёта в формате JSON, XLSX или CSV либо вставьте его содержимое в чат:",
        reply_markup=_cancel_kb(f"edit_testset_{ts_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(TestSetAdminStates.waiting_formulas_file, F.document | F.text)
async def handle_formulas_upload(message: Message, state: FSMContext, bot: Bot):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    ts_id = data.get("target_ts_id")
    if not ts_id:
        await state.clear()
        return

    file_io = io.BytesIO()
    filename = ""

    if message.document:
        filename = (message.document.file_name or "").lower()
        await bot.download(message.document, destination=file_io)
        file_io.seek(0)
    elif message.text and not message.text.startswith("/"):
        filename = "formulas.json"
        file_io.write(message.text.encode("utf-8"))
        file_io.seek(0)
    else:
        await message.answer("Не удалось прочитать формулы. Попробуйте снова:", reply_markup=_cancel_kb(f"edit_testset_{ts_id}"))
        return

    parsed_formulas = None
    try:
        if filename.endswith(".json") or filename == "formulas.json":
            raw_str = file_io.getvalue().decode("utf-8", errors="replace").strip()
            data = json.loads(raw_str)
            if isinstance(data, list):
                parsed_formulas = data
            elif isinstance(data, dict):
                parsed_formulas = data.get("formulas", [data])
            else:
                raise ValueError("JSON must be a list of formulas or an object with 'formulas' key.")
        else:
            file_io.seek(0)
            parsed_formulas = await parse_formulas_file(file_io, filename)
    except Exception as exc:
        logger.warning("Formulas parsing failed: %s", exc)
        parsed_formulas = None

    if not parsed_formulas:
        await message.answer("❌ Ошибка парсинга формул. Проверьте формат и попробуйте снова:", reply_markup=_cancel_kb(f"edit_testset_{ts_id}"))
        return

    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if ts:
            ts.formulas_json = json.dumps(parsed_formulas, ensure_ascii=False)
            ts.calculation_enabled = True
            ts.formulas_enabled = True
            await session.commit()

    await state.clear()
    await message.answer(f"✅ Успешно загружено <b>{len(parsed_formulas)}</b> правил расчёта в набор!", parse_mode="HTML")
    await render_test_set_details(message, ts_id)


@router.callback_query(F.data.startswith("ts_edit_interp_"))
async def start_edit_interp(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    ts_id = int(callback.data.split("ts_edit_interp_")[1])
    await state.set_state(TestSetAdminStates.waiting_interpretation_prompt)
    await state.update_data(target_ts_id=ts_id)
    await callback.message.edit_text(
        "✏️ Отправьте инструкцию, по которой бот будет разбирать результаты теста:",
        reply_markup=_cancel_kb(f"edit_testset_{ts_id}"),
    )
    await callback.answer()


@router.message(TestSetAdminStates.waiting_interpretation_prompt, F.text, ~F.text.startswith("/"))
async def save_interp_prompt(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    ts_id = data.get("target_ts_id")
    if not ts_id:
        await state.clear()
        return

    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if ts:
            ts.interpretation_prompt = message.text.strip()
            ts.separate_interpretation_prompt = True
            await session.commit()

    await state.clear()
    await message.answer("✅ Инструкция для разбора сохранена!")
    await render_test_set_details(message, ts_id)


@router.callback_query(F.data.startswith("ts_edit_sys_"))
async def start_edit_sys(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    ts_id = int(callback.data.split("ts_edit_sys_")[1])
    await state.set_state(TestSetAdminStates.waiting_system_prompt)
    await state.update_data(target_ts_id=ts_id)
    await callback.message.edit_text(
        "✏️ Отправьте системные правила для этого набора тестов:",
        reply_markup=_cancel_kb(f"edit_testset_{ts_id}"),
    )
    await callback.answer()


@router.message(TestSetAdminStates.waiting_system_prompt, F.text, ~F.text.startswith("/"))
async def save_sys_prompt(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    ts_id = data.get("target_ts_id")
    if not ts_id:
        await state.clear()
        return

    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if ts:
            ts.system_prompt = message.text.strip()
            await session.commit()

    await state.clear()
    await message.answer("✅ Системные правила сохранены!")
    await render_test_set_details(message, ts_id)


@router.callback_query(F.data.startswith("ts_rename_"))
async def start_rename_test_set(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    ts_id = int(callback.data.split("ts_rename_")[1])
    await state.set_state(TestSetAdminStates.waiting_rename_title)
    await state.update_data(target_ts_id=ts_id)
    await callback.message.edit_text(
        "✏️ Введите новое название для этого набора тестов:",
        reply_markup=_cancel_kb(f"edit_testset_{ts_id}"),
    )
    await callback.answer()


@router.message(TestSetAdminStates.waiting_rename_title, F.text, ~F.text.startswith("/"))
async def handle_rename_test_set(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    ts_id = data.get("target_ts_id")
    if not ts_id:
        await state.clear()
        return
    new_title = message.text.strip()
    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if ts:
            ts.title = new_title
            await session.commit()
    await state.clear()
    await message.answer(f"✅ Название обновлено на: <b>{html.escape(new_title)}</b>", parse_mode="HTML")
    await render_test_set_details(message, ts_id)


@router.callback_query(F.data.startswith("ts_edit_desc_"))
async def start_edit_description(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    ts_id = int(callback.data.split("ts_edit_desc_")[1])
    await state.set_state(TestSetAdminStates.waiting_edit_description)
    await state.update_data(target_ts_id=ts_id)
    await callback.message.edit_text(
        "✏️ Введите новое описание для этого набора тестов (или '-' чтобы очистить):",
        reply_markup=_cancel_kb(f"edit_testset_{ts_id}"),
    )
    await callback.answer()


@router.message(TestSetAdminStates.waiting_edit_description, F.text, ~F.text.startswith("/"))
async def handle_edit_description(message: Message, state: FSMContext):
    if not await _is_admin(_extract_user_id(message)):
        return
    data = await state.get_data()
    ts_id = data.get("target_ts_id")
    if not ts_id:
        await state.clear()
        return
    desc = message.text.strip()
    if desc == "-":
        desc = None
    async with async_session_maker() as session:
        ts = await session.get(TestSet, ts_id)
        if ts:
            ts.description = desc
            await session.commit()
    await state.clear()
    await message.answer("✅ Описание обновлено!", parse_mode="HTML")
    await render_test_set_details(message, ts_id)


@router.callback_query(F.data.startswith("ts_preview_"))
async def start_test_set_preview(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not await _is_admin(_extract_user_id(callback)):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    ts_id = int(callback.data.split("ts_preview_")[1])
    from test_system_handlers import start_universal_test
    await callback.answer("Запуск тестового прогона...", show_alert=False)
    ok = await start_universal_test(
        bot,
        callback.from_user.id,
        state,
        test_set_id=ts_id,
        purpose="preview",
        admin_preview=True,
    )
    if not ok:
        await callback.message.answer("⚠️ Не удалось запустить тестовый прогон. Проверьте, загружены ли вопросы.")
