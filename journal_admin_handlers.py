"""Admin handlers for Journal of Self-Reflection configuration and question CRUD."""

import html
import json
import logging
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select, update

from admin_roles import AdminPermission, require_permission
from database import JournalAnswer, JournalConfig, JournalQuestion, async_session_maker
from handlers import send_smart_content
from journal_keyboards import (
    journal_admin_cancel_keyboard,
    journal_admin_menu_keyboard,
    journal_admin_questions_keyboard,
)
from journal_service import get_or_create_journal_config
from universal_tests import (
    QUESTION_TYPE_CHOICE,
    QUESTION_TYPE_SCALE,
    QUESTION_TYPE_TEXT,
    get_question_type,
    json_dumps,
    normalize_question_type,
    reorder_questions,
)

logger = logging.getLogger(__name__)

router = Router(name="journal_admin")


class JournalAdminStates(StatesGroup):
    new_question_text = State()
    edit_question_text = State()
    edit_points = State()


def _journal_type_label(question_type: str) -> str:
    return {
        QUESTION_TYPE_TEXT: "текст",
        QUESTION_TYPE_SCALE: "шкала 1–10",
        QUESTION_TYPE_CHOICE: "варианты",
    }.get(question_type, question_type)


def _journal_options(raw_options) -> list[dict]:
    if raw_options is None:
        return []
    if isinstance(raw_options, str):
        raw_options = json.loads(raw_options)
    if not isinstance(raw_options, list):
        raise ValueError("Поле options должно быть списком.")
    options = []
    for item in raw_options:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("label") or "").strip()
            value = item.get("value")
        else:
            text = str(item).strip()
            value = None
        if not text:
            raise ValueError("Вариант ответа не может быть пустым.")
        options.append({"text": text, "value": value})
    return options


def _journal_question_payload(raw_text: str, question: JournalQuestion | None = None) -> dict:
    raw_text = (raw_text or "").strip()
    if not raw_text:
        raise ValueError("Текст вопроса не может быть пустым.")
    if raw_text.startswith("{"):
        payload = json.loads(raw_text)
        if not isinstance(payload, dict):
            raise ValueError("Ожидается JSON-объект.")
    else:
        payload = {"text": raw_text}

    text_value = str(payload.get("text", question.text if question else "") or "").strip()
    if not text_value:
        raise ValueError("Текст вопроса не может быть пустым.")
    fallback_type = get_question_type(question) if question else QUESTION_TYPE_TEXT
    question_type = normalize_question_type(payload.get("type", payload.get("question_type")), fallback_type)
    if question_type is None:
        raise ValueError("Тип вопроса: text, scale или choice.")
    options_present = "options" in payload or "answer_options" in payload or "answer_options_json" in payload
    if options_present:
        options = _journal_options(payload.get("options", payload.get("answer_options", payload.get("answer_options_json"))))
    elif question:
        options = json.loads(question.answer_options_json or "[]")
    else:
        options = []
    if question_type in {QUESTION_TYPE_TEXT, QUESTION_TYPE_SCALE}:
        options = []
    allow_custom = payload.get("allow_custom_answer", question.allow_custom_answer if question else False)
    if isinstance(allow_custom, str):
        allow_custom = allow_custom.strip().lower() in {"1", "true", "yes", "да"}
    return {
        "text": text_value,
        "question_type": question_type,
        "allow_custom_answer": bool(allow_custom),
        "answer_options_json": json_dumps(options) if options else None,
        "order_num": payload.get("order", payload.get("order_num")),
    }


async def _ensure_admin(event: CallbackQuery | Message) -> bool:
    return await require_permission(
        event,
        AdminPermission.CONTENT,
        denial_message="⛔️ Действие доступно только администраторам.",
    )


@router.callback_query(F.data == "admin_journal_menu")
async def admin_journal_menu_handler(callback: CallbackQuery, state: FSMContext):
    if not await _ensure_admin(callback):
        return
    await state.clear()
    async with async_session_maker() as session:
        cfg = await get_or_create_journal_config(session)
        total_q = (await session.scalar(select(func.count(JournalQuestion.id)))) or 0
        active_q = (
            await session.scalar(
                select(func.count(JournalQuestion.id)).where(JournalQuestion.is_active == True)
            )
        ) or 0

    text = (
        "⚙️ <b>Управление Дневником саморефлексии</b>\n\n"
        f"• Баллов за рефлексию: <b>{cfg.points_per_reflection}</b>\n"
        f"• Всего вопросов: <b>{total_q}</b> (активных: <b>{active_q}</b>)\n"
        "• Рекомендуемый формат: 6–7 шагов\n"
    )
    await send_smart_content(callback, text, None, journal_admin_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "j_adm_q_list")
async def admin_journal_q_list_handler(callback: CallbackQuery, state: FSMContext):
    if not await _ensure_admin(callback):
        return
    await state.clear()
    async with async_session_maker() as session:
        questions = (
            await session.execute(
                select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
            )
        ).scalars().all()

    text = (
        "📝 <b>Вопросы Дневника саморефлексии</b>\n\n"
        "Здесь вы можете добавлять, редактировать, включать/выключать, "
        "менять порядок и удалять вопросы. Для обычного вопроса достаточно текста; "
        "для шкалы и вариантов ответа доступны расширенные настройки.\n"
        "<i>Активные вопросы в текущем порядке задаются пользователям при рефлексии.</i>"
    )
    await send_smart_content(callback, text, None, journal_admin_questions_keyboard(list(questions)))
    await callback.answer()


@router.callback_query(F.data == "j_adm_q_add")
async def admin_journal_q_add_handler(callback: CallbackQuery, state: FSMContext):
    if not await _ensure_admin(callback):
        return
    await state.set_state(JournalAdminStates.new_question_text)
    text = (
        "➕ <b>Добавление нового вопроса</b>\n\n"
        "Отправь текст нового вопроса. Для обычного вопроса достаточно одной фразы.\n"
        "Если нужно настроить шкалу или варианты ответа, дополнительные параметры можно передать в расширенном формате."
    )
    await send_smart_content(callback, text, None, journal_admin_cancel_keyboard("j_adm_q_list"))
    await callback.answer()


@router.message(JournalAdminStates.new_question_text)
async def admin_journal_q_add_submit(message: Message, state: FSMContext):
    if not await _ensure_admin(message):
        return
    try:
        payload = _journal_question_payload(message.text or "")
    except (ValueError, json.JSONDecodeError) as exc:
        await message.answer(f"⚠️ Не удалось разобрать вопрос: {html.escape(str(exc))}")
        return

    async with async_session_maker() as session:
        max_order = (await session.scalar(select(func.coalesce(func.max(JournalQuestion.order_num), 0)))) or 0
        new_q = JournalQuestion(
            text=payload["text"],
            order_num=max_order + 1,
            is_active=True,
            question_type=payload["question_type"],
            allow_custom_answer=payload["allow_custom_answer"],
            answer_options_json=payload["answer_options_json"],
        )
        session.add(new_q)
        await session.flush()

        # Normalize all questions to 1..N
        all_q = (
            await session.execute(
                select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
            )
        ).scalars().all()
        for idx, q in enumerate(all_q, start=1):
            q.order_num = idx
        await session.commit()

        refreshed = (
            await session.execute(
                select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
            )
        ).scalars().all()

    await state.clear()
    await message.answer("✅ Вопрос успешно добавлен!")
    text = "📝 <b>Вопросы Дневника саморефлексии</b>\n\nТекущий список вопросов:"
    await message.answer(text, reply_markup=journal_admin_questions_keyboard(list(refreshed)), parse_mode="HTML")


@router.callback_query(F.data.startswith("j_adm_q_ed:"))
async def admin_journal_q_edit_handler(callback: CallbackQuery, state: FSMContext):
    if not await _ensure_admin(callback):
        return
    try:
        qid = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка параметров.", show_alert=True)
        return

    async with async_session_maker() as session:
        q = await session.get(JournalQuestion, qid)
    if not q:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return

    await state.set_state(JournalAdminStates.edit_question_text)
    await state.update_data(question_id=qid)
    text = (
        f"✏️ <b>Редактирование вопроса #{q.order_num}</b>\n\n"
        f"Текущий текст:\n<i>{html.escape(q.text)}</i>\n\n"
        "Отправь новый текст вопроса. Текущий тип ответа и варианты сохранятся.\n"
        "Для расширенной настройки можно использовать дополнительный формат:"
    )
    await send_smart_content(callback, text, None, journal_admin_cancel_keyboard("j_adm_q_list"))
    await callback.answer()


@router.message(JournalAdminStates.edit_question_text)
async def admin_journal_q_edit_submit(message: Message, state: FSMContext):
    if not await _ensure_admin(message):
        return
    data = await state.get_data()
    qid = data.get("question_id")

    async with async_session_maker() as session:
        q = await session.get(JournalQuestion, qid)
        if q:
            try:
                payload = _journal_question_payload(message.text or "", q)
            except (ValueError, json.JSONDecodeError) as exc:
                await message.answer(f"⚠️ Не удалось сохранить вопрос: {html.escape(str(exc))}")
                return
            q.text = payload["text"]
            q.question_type = payload["question_type"]
            q.allow_custom_answer = payload["allow_custom_answer"]
            q.answer_options_json = payload["answer_options_json"]
            if payload["order_num"] is not None:
                try:
                    q.order_num = int(payload["order_num"])
                except (TypeError, ValueError):
                    await message.answer("⚠️ Порядок вопроса должен быть целым числом.")
                    return
            await session.flush()
            all_q = list((await session.scalars(
                select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
            )).all())
            if payload["order_num"] is not None:
                reorder_questions(all_q, qid, int(payload["order_num"]))
            else:
                for index, item in enumerate(all_q, start=1):
                    item.order_num = index
            await session.commit()
        refreshed = (
            await session.execute(
                select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
            )
        ).scalars().all()

    await state.clear()
    await message.answer("✅ Вопрос обновлён!")
    text = "📝 <b>Вопросы Дневника саморефлексии</b>\n\nТекущий список вопросов:"
    await message.answer(text, reply_markup=journal_admin_questions_keyboard(list(refreshed)), parse_mode="HTML")


@router.callback_query(F.data.startswith("j_adm_q_tog:"))
async def admin_journal_q_tog_handler(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _ensure_admin(callback):
        return
    if state is not None:
        await state.clear()
    try:
        qid = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка параметров.", show_alert=True)
        return

    async with async_session_maker() as session:
        q = await session.get(JournalQuestion, qid)
        if q:
            q.is_active = not q.is_active
            await session.commit()
        refreshed = (
            await session.execute(
                select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
            )
        ).scalars().all()

    text = "📝 <b>Вопросы Дневника саморефлексии</b>\n\nТекущий список вопросов:"
    await send_smart_content(callback, text, None, journal_admin_questions_keyboard(list(refreshed)))
    await callback.answer("Статус вопроса изменен.")


@router.callback_query(F.data.startswith("j_adm_q_up:"))
async def admin_journal_q_up_handler(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _ensure_admin(callback):
        return
    if state is not None:
        await state.clear()
    try:
        qid = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка параметров.", show_alert=True)
        return

    async with async_session_maker() as session:
        questions = list(
            (
                await session.execute(
                    select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
                )
            ).scalars().all()
        )
        idx = next((i for i, q in enumerate(questions) if q.id == qid), -1)
        if idx > 0:
            reorder_questions(questions, qid, idx)
            await session.commit()
        refreshed = (
            await session.execute(
                select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
            )
        ).scalars().all()

    text = "📝 <b>Вопросы Дневника саморефлексии</b>\n\nТекущий список вопросов:"
    await send_smart_content(callback, text, None, journal_admin_questions_keyboard(list(refreshed)))
    await callback.answer()


@router.callback_query(F.data.startswith("j_adm_q_dn:"))
async def admin_journal_q_dn_handler(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _ensure_admin(callback):
        return
    if state is not None:
        await state.clear()
    try:
        qid = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка параметров.", show_alert=True)
        return

    async with async_session_maker() as session:
        questions = list(
            (
                await session.execute(
                    select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
                )
            ).scalars().all()
        )
        idx = next((i for i, q in enumerate(questions) if q.id == qid), -1)
        if 0 <= idx < len(questions) - 1:
            reorder_questions(questions, qid, idx + 2)
            await session.commit()
        refreshed = (
            await session.execute(
                select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
            )
        ).scalars().all()

    text = "📝 <b>Вопросы Дневника саморефлексии</b>\n\nТекущий список вопросов:"
    await send_smart_content(callback, text, None, journal_admin_questions_keyboard(list(refreshed)))
    await callback.answer()


@router.callback_query(F.data.startswith("j_adm_q_del_confirm:"))
async def admin_journal_q_del_confirm_handler(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _ensure_admin(callback):
        return
    if state is not None:
        await state.clear()
    try:
        qid = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка параметров.", show_alert=True)
        return
    async with async_session_maker() as session:
        question = await session.get(JournalQuestion, qid)
    if not question:
        await callback.answer("Вопрос уже удален.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(
            "⚠️ <b>Удалить вопрос?</b>\n\n"
            f"{html.escape(question.text)}\n\n"
            "Связанные ответы пользователей сохранятся, но вопрос исчезнет из будущих рефлексий.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"j_adm_q_del_execute:{qid}")],
                    [InlineKeyboardButton(text="⬅️ Отмена", callback_data="j_adm_q_list")],
                ]
            ),
            parse_mode="HTML",
        )
    await callback.answer()


async def _execute_journal_question_delete(callback: CallbackQuery, state: FSMContext | None, qid: int):
    if state is not None:
        await state.clear()
    async with async_session_maker() as session:
        await session.execute(
            update(JournalAnswer).where(JournalAnswer.question_id == qid).values(question_id=None)
        )
        await session.execute(delete(JournalQuestion).where(JournalQuestion.id == qid))
        await session.flush()
        remaining = (
            await session.execute(
                select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
            )
        ).scalars().all()
        for idx, q in enumerate(remaining, start=1):
            q.order_num = idx
        await session.commit()
        refreshed = (
            await session.execute(
                select(JournalQuestion).order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
            )
        ).scalars().all()

    text = "📝 <b>Вопросы Дневника саморефлексии</b>\n\nТекущий список вопросов:"
    await send_smart_content(callback, text, None, journal_admin_questions_keyboard(list(refreshed)))
    await callback.answer("Вопрос удален.")


@router.callback_query(F.data.startswith("j_adm_q_del:"))
async def admin_journal_q_del_handler(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _ensure_admin(callback):
        return
    if state is not None:
        await state.clear()
    try:
        qid = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка параметров.", show_alert=True)
        return

    if isinstance(callback, CallbackQuery):
        await admin_journal_q_del_confirm_handler(
            callback,
            state,
        )
        return
    await _execute_journal_question_delete(callback, state, qid)


@router.callback_query(F.data.startswith("j_adm_q_del_execute:"))
async def admin_journal_q_del_execute_handler(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _ensure_admin(callback):
        return
    try:
        qid = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка параметров.", show_alert=True)
        return
    await _execute_journal_question_delete(callback, state, qid)


@router.callback_query(F.data == "j_adm_pts_edit")
async def admin_journal_pts_edit_handler(callback: CallbackQuery, state: FSMContext):
    if not await _ensure_admin(callback):
        return
    await state.set_state(JournalAdminStates.edit_points)
    async with async_session_maker() as session:
        cfg = await get_or_create_journal_config(session)
        curr_pts = cfg.points_per_reflection

    text = (
        "⭐️ <b>Настройка баллов за рефлексию</b>\n\n"
        f"Текущее количество баллов: <b>{curr_pts}</b>\n\n"
        "Отправьте новое значение (целое число >= 0):"
    )
    await send_smart_content(callback, text, None, journal_admin_cancel_keyboard("admin_journal_menu"))
    await callback.answer()


@router.message(JournalAdminStates.edit_points)
async def admin_journal_pts_edit_submit(message: Message, state: FSMContext):
    if not await _ensure_admin(message):
        return
    raw = (message.text or "").strip()
    try:
        new_pts = int(raw)
        if new_pts < 0:
            raise ValueError()
    except ValueError:
        await message.answer("⚠️ Пожалуйста, введите целое неотрицательное число (например, 10 или 15):")
        return

    async with async_session_maker() as session:
        cfg = await get_or_create_journal_config(session)
        cfg.points_per_reflection = new_pts
        await session.commit()

        total_q = (await session.scalar(select(func.count(JournalQuestion.id)))) or 0
        active_q = (
            await session.scalar(
                select(func.count(JournalQuestion.id)).where(JournalQuestion.is_active == True)
            )
        ) or 0

    await state.clear()
    await message.answer(f"✅ Количество баллов за рефлексию установлено: <b>{new_pts}</b>", parse_mode="HTML")
    text = (
        "⚙️ <b>Управление Дневником саморефлексии</b>\n\n"
        f"• Баллов за рефлексию: <b>{new_pts}</b>\n"
        f"• Всего вопросов: <b>{total_q}</b> (активных: <b>{active_q}</b>)\n"
    )
    await message.answer(text, reply_markup=journal_admin_menu_keyboard(), parse_mode="HTML")
