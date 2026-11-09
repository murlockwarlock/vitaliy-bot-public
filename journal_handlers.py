import asyncio
import html
import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from database import (
    Content,
    JournalAnswer,
    JournalEntry,
    JournalQuestion,
    JournalSettings,
    QuickNote,
    User,
    async_session_maker,
)
from handlers import check_and_request_ai_disclaimer, check_paid_access, send_smart_content
from crisis_safety import crisis_was_detected_in_turn, send_crisis_contacts_if_active
from journal_keyboards import (
    journal_completed_today_keyboard,
    journal_dynamics_menu_keyboard,
    journal_entry_keyboard,
    journal_entry_detail_keyboard,
    journal_history_list_keyboard,
    journal_in_progress_keyboard,
    journal_menu_keyboard,
    journal_question_keyboard,
    journal_reminder_time_keyboard,
    journal_snooze_menu_keyboard,
    quick_note_detail_keyboard,
    quick_note_input_keyboard,
    quick_notes_keyboard,
)
from journal_service import (
    build_questions_snapshot,
    execute_transaction_a,
    get_journal_stats,
    get_or_create_journal_settings,
)
from time_utils import msk_now
from universal_tests import QUESTION_TYPE_TEXT, normalize_question_type

logger = logging.getLogger("journal_handlers")
_quick_note_save_locks: dict[int, asyncio.Lock] = {}
_QUICK_NOTE_PAGE_SIZE = 10

router = Router(name="journal")


class JournalStates(StatesGroup):
    answering_question = State()
    setting_reminder_time = State()
    quick_note_input = State()


def _snapshot_question(snapshot: list[dict[str, Any]], position: int) -> dict[str, Any] | None:
    return next((item for item in snapshot if item.get("position") == position), None)


def _journal_question_text(question: dict[str, Any], position: int, total: int) -> str:
    lines = [f"📔 <b>Вопрос {position} из {total}</b>", "", html.escape(question.get("text", ""))]
    question_type = normalize_question_type(question.get("question_type"), QUESTION_TYPE_TEXT)
    options = question.get("options") or []
    if question_type == QUESTION_TYPE_TEXT:
        lines.extend(["", "Напиши свой ответ сообщением."])
    elif question.get("allow_custom_answer"):
        lines.extend(["", "Напиши свой ответ или выбери вариант ниже."])
    elif options:
        lines.extend(["", "Выбери один вариант ниже."])
    return "\n".join(lines)


async def _send_journal_question(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    entry_id: int,
    question: dict[str, Any],
    total: int,
    *,
    edit_message: Any = None,
) -> None:
    position = int(question["position"])
    await state.set_state(JournalStates.answering_question)
    await state.update_data(entry_id=entry_id, position=position, total=total)
    text_msg = _journal_question_text(question, position, total)
    markup = journal_question_keyboard(entry_id, position, question)
    if edit_message is not None:
        await edit_message.edit_text(text_msg, parse_mode="HTML", reply_markup=markup)
    else:
        await bot.send_message(chat_id=chat_id, text=text_msg, parse_mode="HTML", reply_markup=markup)


async def _persist_journal_answer(
    entry_id: int,
    user_id: int,
    position: int,
    answer_text: str = "",
    *,
    is_skipped: bool = False,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    """Persist one answer or skip and return completion plus the next snapshot item."""
    async with async_session_maker() as session:
        entry = await session.get(JournalEntry, entry_id)
        if not entry or entry.user_id != user_id:
            raise ValueError("Ошибка доступа к записи.")
        if entry.status == "completed":
            return True, None, {"already_completed": True}

        snapshot = json.loads(entry.questions_snapshot_json or "[]")
        current_question = _snapshot_question(snapshot, position)
        if not current_question:
            raise ValueError("Вопрос больше не найден в сохранённой версии дневника.")

        qid = current_question.get("question_id")
        valid_qid = qid if qid and await session.get(JournalQuestion, qid) else None
        answer = JournalAnswer(
            entry_id=entry_id,
            question_id=valid_qid,
            position=position,
            question_text_snapshot=current_question.get("text", ""),
            answer_text="" if is_skipped else answer_text.strip(),
            is_skipped=is_skipped,
        )
        try:
            async with session.begin_nested():
                session.add(answer)
                await session.flush()
        except IntegrityError:
            pass

        persisted_positions = set((await session.scalars(
            select(JournalAnswer.position).where(JournalAnswer.entry_id == entry_id)
        )).all())
        next_question = next(
            (item for item in snapshot if item.get("position") not in persisted_positions),
            None,
        )
        if next_question is None:
            result = await execute_transaction_a(session, entry_id=entry_id, user_id=user_id)
            return True, None, result

        await session.commit()
        return False, next_question, None


async def _continue_after_journal_answer(
    message: Any,
    state: FSMContext,
    bot: Bot,
    entry_id: int,
    user_id: int,
    position: int,
    answer_text: str = "",
    *,
    is_skipped: bool = False,
) -> None:
    completed, next_question, result = await _persist_journal_answer(
        entry_id,
        user_id,
        position,
        answer_text,
        is_skipped=is_skipped,
    )
    if (result or {}).get("already_completed"):
        await state.clear()
        await message.answer(
            "✅ Эта рефлексия уже завершена.",
            reply_markup=journal_completed_today_keyboard(entry_id),
        )
        return
    if not completed:
        await _send_journal_question(
            bot,
            message.chat.id,
            state,
            entry_id,
            next_question,
            int((await state.get_data()).get("total", 1)),
        )
        return

    await state.clear()
    points = (result or {}).get("points_awarded", 0)
    reward_granted = (result or {}).get("reward_granted", False)
    celebration_msg = (
        "✨ <b>Рефлексия на сегодня успешно завершена!</b>\n\n"
        f"⭐️ Начислено баллов: +{points}\n"
    )
    if reward_granted:
        celebration_msg += (
            "\n🎉 <b>Поздравляем! Ты завершил(а) 21 день рефлексии!</b>\n"
            "🎁 Тебе начислено <b>+7 дней подписки</b> в подарок!\n"
        )
    celebration_msg += "\n⏳ <i>Формирую бережный персональный итог дня...</i>"
    await message.answer(celebration_msg, parse_mode="HTML")
    asyncio.create_task(run_journal_summary_and_update(entry_id, user_id, message.chat.id, bot))


async def show_journal_menu(
    event: Any,
    bot: Bot,
    send_new_message: bool = False,
    show_stats: bool = False,
) -> None:
    """Render the main Journal menu with current streak, progress, points, and day status."""
    user_id = event.from_user.id
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id

    async with async_session_maker() as session:
        if not show_stats:
            content_intro = await session.get(Content, "journal_intro", options=[selectinload(Content.media)])
            intro_media = content_intro.media if content_intro and content_intro.media else None
            intro_text = content_intro.text_content if content_intro and content_intro.text_content else None
            if intro_text or intro_media:
                await send_smart_content(
                    event,
                    intro_text or "📔 Дневник саморефлексии",
                    intro_media,
                    reply_markup=journal_entry_keyboard(),
                    bot=bot,
                )
                return

        # Load journal_video content slot if configured
        content_video = await session.get(Content, "journal_video", options=[selectinload(Content.media)])
        video_media = content_video.media if (content_video and content_video.media) else None
        has_video = bool(video_media)

        stats = await get_journal_stats(session, user_id)
        today = msk_now().date()

        today_entry = await session.scalar(
            select(JournalEntry).where(
                JournalEntry.user_id == user_id,
                JournalEntry.reflection_date == today,
            )
        )

        has_in_progress = False
        in_progress_id = None
        is_today_completed = False
        completed_id = None

        if today_entry:
            if today_entry.status == "completed":
                is_today_completed = True
                completed_id = today_entry.id
                day_status_line = "✅ Сегодняшняя рефлексия уже пройдена."
            else:
                has_in_progress = True
                in_progress_id = today_entry.id
                snapshot = json.loads(today_entry.questions_snapshot_json or "[]")
                answers_cnt = len((await session.scalars(select(JournalAnswer).where(JournalAnswer.entry_id == today_entry.id))).all())
                total = len(snapshot)
                day_status_line = f"⏳ Рефлексия начата (отвечено на {answers_cnt} из {total})."
        else:
            day_status_line = "✨ Сегодняшняя рефлексия ещё не начата."

        text_msg = (
            "📔 <b>Дневник саморефлексии</b>\n\n"
            f"🔥 Текущая серия: {stats['streak']} дн.\n"
            f"🎯 21 день: {stats['target_progress']}/{stats['target_days']}\n"
            f"⭐️ Баллы: {stats['points']}\n"
            f"⏰ Напоминание: {stats['reminder_str']}\n\n"
            f"{day_status_line}"
        )

        kb = journal_menu_keyboard(
            has_in_progress=has_in_progress,
            in_progress_id=in_progress_id,
            is_today_completed=is_today_completed,
            completed_id=completed_id,
            has_video=has_video,
        )

    await send_smart_content(event, text_msg, video_media, reply_markup=kb, bot=bot)


@router.callback_query(F.data == "journal_open_stats")
async def cb_journal_open_stats(callback: CallbackQuery, bot: Bot) -> None:
    await show_journal_menu(callback, bot, show_stats=True)
    await callback.answer()


@router.callback_query(F.data == "mode_journal")
async def cb_mode_journal(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext | None = None,
) -> None:
    """Root entry point for Journal feature."""
    if state is not None:
        await state.clear()
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    chat_id = callback.message.chat.id
    if not await check_and_request_ai_disclaimer(bot, callback.from_user.id, chat_id, flow_type="journal"):
        await callback.answer()
        return

    await show_journal_menu(callback, bot)
    await callback.answer()


def _quick_note_page_from_callback(data: str) -> int:
    try:
        return max(0, int(data.rsplit(":", 1)[1]))
    except (TypeError, ValueError):
        return 0


async def _render_quick_notes(
    target: Any,
    bot: Bot,
    page: int = 0,
    *,
    notice: str | None = None,
) -> None:
    user_id = target.from_user.id
    async with async_session_maker() as session:
        total_count = await session.scalar(
            select(func.count(QuickNote.id)).where(QuickNote.user_id == user_id)
        ) or 0
        total_pages = max(1, (total_count + _QUICK_NOTE_PAGE_SIZE - 1) // _QUICK_NOTE_PAGE_SIZE)
        page = max(0, min(int(page or 0), total_pages - 1))
        notes = (await session.scalars(
            select(QuickNote)
            .where(QuickNote.user_id == user_id)
            .order_by(QuickNote.created_at.desc(), QuickNote.id.desc())
            .offset(page * _QUICK_NOTE_PAGE_SIZE)
            .limit(_QUICK_NOTE_PAGE_SIZE)
        )).all()

    lines = []
    if notice:
        lines.extend([notice, ""])
    lines.extend([
        "📝 <b>Оперативные записи</b>",
        f"Страница {page + 1} из {total_pages}",
        "",
    ])
    if notes:
        lines.append("Выберите запись, чтобы открыть полный текст:")
        for note in notes:
            created_at = note.created_at.strftime("%d.%m.%Y %H:%M") if note.created_at else "дата неизвестна"
            preview = " ".join((note.text or "").split())
            if len(preview) > 120:
                preview = preview[:117] + "..."
            lines.append(f"• {html.escape(created_at)} — {html.escape(preview or 'Без текста')}")
    else:
        lines.append("Здесь можно сохранить мысль отдельно от ежедневного дневника, серии и наград.")

    markup = quick_notes_keyboard(page, total_pages, notes)
    is_callback = isinstance(target, CallbackQuery) or (
        hasattr(target, "data") and hasattr(target, "message")
    )
    if is_callback:
        await send_smart_content(target, "\n".join(lines), None, markup, bot=bot)
    else:
        await target.answer("\n".join(lines), parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data == "quick_notes")
@router.callback_query(F.data.startswith("quick_notes_page:"))
async def cb_quick_notes(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext | None = None,
) -> None:
    if state is not None:
        await state.clear()
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return
    page = _quick_note_page_from_callback(callback.data) if ":" in callback.data else 0
    await _render_quick_notes(callback, bot, page)
    await callback.answer()


def _split_quick_note_text(text: str, limit: int = 3000) -> list[str]:
    text = text or ""
    if not text:
        return [""]

    chunks = []
    current = []
    current_size = 0
    for character in text:
        escaped_size = len(html.escape(character))
        if current and current_size + escaped_size > limit:
            chunks.append("".join(current))
            current = []
            current_size = 0
        current.append(character)
        current_size += escaped_size
    if current:
        chunks.append("".join(current))
    return chunks


@router.callback_query(F.data.startswith("quick_note_view:"))
async def cb_quick_note_view(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext | None = None,
) -> None:
    if state is not None:
        await state.clear()
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    parts = callback.data.split(":")
    try:
        note_id = int(parts[1])
        page = max(0, int(parts[2])) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    async with async_session_maker() as session:
        note = await session.scalar(
            select(QuickNote).where(
                QuickNote.id == note_id,
                QuickNote.user_id == callback.from_user.id,
            )
        )
    if note is None:
        await callback.answer("Запись не найдена или принадлежит другому пользователю.", show_alert=True)
        return

    created_at = note.created_at.strftime("%d.%m.%Y %H:%M") if note.created_at else "дата неизвестна"
    chunks = _split_quick_note_text(note.text)
    for index, chunk in enumerate(chunks):
        detail_text = (
            "📝 <b>Оперативная запись</b>\n"
            f"📅 <b>Дата и время:</b> {html.escape(created_at)}\n\n"
            f"{html.escape(chunk)}"
        )
        markup = quick_note_detail_keyboard(page) if index == len(chunks) - 1 else None
        if index == 0:
            await send_smart_content(callback, detail_text, None, markup, bot=bot)
        else:
            await bot.send_message(
                callback.message.chat.id,
                detail_text,
                parse_mode="HTML",
                reply_markup=markup,
            )
    await callback.answer()


@router.callback_query(F.data.startswith("quick_note_new"))
async def cb_quick_note_new(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return
    if await state.get_state() == JournalStates.quick_note_input.state:
        await callback.answer("Я уже жду текст записи.")
        return

    page = _quick_note_page_from_callback(callback.data) if ":" in callback.data else 0
    await state.set_state(JournalStates.quick_note_input)
    await state.update_data(
        quick_note_page=page,
        quick_note_operation_key=uuid.uuid4().hex,
    )
    await send_smart_content(
        callback,
        "📝 <b>Новая оперативная запись</b>\n\nНапиши одну мысль. Она сохранится отдельно от дневника, серии и наград.",
        None,
        quick_note_input_keyboard(page),
        bot=bot,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("quick_note_cancel"))
async def cb_quick_note_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    page = data.get("quick_note_page", _quick_note_page_from_callback(callback.data))
    await state.clear()
    await callback.answer("Запись отменена.")
    if await check_paid_access(callback, bot, topic_slug="journal"):
        await _render_quick_notes(callback, bot, page)


@router.message(JournalStates.quick_note_input, F.text)
async def msg_quick_note_save(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Запись не может быть пустой.")
        return

    user_id = message.from_user.id
    lock = _quick_note_save_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        if await state.get_state() != JournalStates.quick_note_input.state:
            return
        if not await check_paid_access(message, bot, topic_slug="journal"):
            await state.clear()
            return
        data = await state.get_data()
        operation_key = data.get("quick_note_operation_key")
        if not operation_key:
            await state.clear()
            return

        async with async_session_maker() as session:
            existing = await session.scalar(
                select(QuickNote).where(QuickNote.operation_key == operation_key)
            )
            if existing is None:
                session.add(QuickNote(
                    user_id=user_id,
                    text=text,
                    operation_key=operation_key,
                ))
                if not await check_paid_access(message, bot, topic_slug="journal"):
                    await session.rollback()
                    await state.clear()
                    return
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    existing = await session.scalar(
                        select(QuickNote).where(QuickNote.operation_key == operation_key)
                    )
                    if existing is None:
                        raise

        page = data.get("quick_note_page", 0)
        await state.clear()
        await _render_quick_notes(message, bot, page, notice="✅ Оперативная запись сохранена.")


@router.callback_query(F.data == "j_video")
async def cb_journal_video(callback: CallbackQuery, bot: Bot) -> None:
    """Show educational video for journal self-reflection."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    async with async_session_maker() as session:
        content_video = await session.get(Content, "journal_video", options=[selectinload(Content.media)])
        video_media = content_video.media if (content_video and content_video.media) else None
        caption_text = (content_video.text_content if content_video and content_video.text_content else "🎬 <b>Видеоурок по саморефлексии</b>")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ В меню Дневника", callback_data="mode_journal")

    await send_smart_content(callback, caption_text, video_media, reply_markup=builder.as_markup(), bot=bot)
    await callback.answer()


async def handle_journal_start(
    event: Any,
    state: FSMContext,
    bot: Bot,
    send_new_message: bool = False,
) -> None:
    """
    Start or resume today's reflection:
    - Handles concurrent clicks via nested transaction catch;
    - If completed, shows completed view;
    - If in progress, resumes at first unanswered question;
    - Freezes snapshot questions if new entry.
    """
    user_id = event.from_user.id
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    today = msk_now().date()

    async with async_session_maker() as session:
        entry = await session.scalar(
            select(JournalEntry).where(
                JournalEntry.user_id == user_id,
                JournalEntry.reflection_date == today,
            )
        )

        if entry:
            if entry.status == "completed":
                msg = "✅ Сегодняшняя рефлексия уже пройдена."
                kb = journal_completed_today_keyboard(entry.id)
                if isinstance(event, CallbackQuery) and not send_new_message:
                    await event.message.edit_text(msg, reply_markup=kb)
                else:
                    await bot.send_message(chat_id=chat_id, text=msg, reply_markup=kb)
                return

            # Entry in progress -> resume
            snapshot = json.loads(entry.questions_snapshot_json or "[]")
            existing_answers = (await session.scalars(
                select(JournalAnswer).where(JournalAnswer.entry_id == entry.id)
            )).all()
            answered_positions = {a.position for a in existing_answers}

            # Defensive self-recovery if all answered
            if snapshot and len(answered_positions) >= len(snapshot):
                await execute_transaction_a(session, entry.id, user_id)
                asyncio.create_task(run_journal_summary_and_update(entry.id, user_id, chat_id, bot=bot))
                msg = "✅ Все ответы были сохранены! Рефлексия завершена.\n\n⏳ Формирую итог дня..."
                kb = journal_completed_today_keyboard(entry.id)
                await bot.send_message(chat_id=chat_id, text=msg, reply_markup=kb)
                return

            first_unanswered = None
            for q_data in snapshot:
                if q_data["position"] not in answered_positions:
                    first_unanswered = q_data
                    break

            if not first_unanswered:
                first_unanswered = snapshot[0]

            await _send_journal_question(bot, chat_id, state, entry.id, first_unanswered, len(snapshot))
            return

        # No entry exists -> create with snapshot
        snapshot = await build_questions_snapshot(session)
        if not snapshot:
            msg = "Вопросы рефлексии настраиваются. Попробуйте позже."
            if isinstance(event, CallbackQuery) and not send_new_message:
                await event.message.edit_text(msg)
            else:
                await bot.send_message(chat_id=chat_id, text=msg)
            return

        snapshot_json = json.dumps(snapshot, ensure_ascii=False)
        is_winner = False
        try:
            async with session.begin_nested():
                entry = JournalEntry(
                    user_id=user_id,
                    reflection_date=today,
                    status="in_progress",
                    questions_snapshot_json=snapshot_json,
                )
                session.add(entry)
                await session.flush()
            await session.commit()
            is_winner = True
        except IntegrityError:
            # Race condition: concurrent click won. Reload existing entry by user_id + reflection_date.
            entry = await session.scalar(
                select(JournalEntry).where(
                    JournalEntry.user_id == user_id,
                    JournalEntry.reflection_date == today,
                )
            )

        if not is_winner:
            if not entry:
                return

            if entry.status == "completed":
                msg = "✅ Сегодняшняя рефлексия уже пройдена."
                kb = journal_completed_today_keyboard(entry.id)
                if isinstance(event, CallbackQuery) and not send_new_message:
                    await event.message.edit_text(msg, reply_markup=kb)
                else:
                    await bot.send_message(chat_id=chat_id, text=msg, reply_markup=kb)
                return

            # in_progress: reload snapshot and derive first unanswered position from DB answers
            snapshot = json.loads(entry.questions_snapshot_json or "[]")
            existing_answers = (await session.scalars(
                select(JournalAnswer).where(JournalAnswer.entry_id == entry.id)
            )).all()
            answered_positions = {a.position for a in existing_answers}

            if snapshot and len(answered_positions) >= len(snapshot):
                # Defensive self-recovery if all answered
                await execute_transaction_a(session, entry.id, user_id)
                asyncio.create_task(run_journal_summary_and_update(entry.id, user_id, chat_id, bot=bot))
                msg = "✅ Все ответы были сохранены! Рефлексия завершена.\n\n⏳ Формирую итог дня..."
                kb = journal_completed_today_keyboard(entry.id)
                if isinstance(event, CallbackQuery) and not send_new_message:
                    await event.message.edit_text(msg, reply_markup=kb)
                else:
                    await bot.send_message(chat_id=chat_id, text=msg, reply_markup=kb)
                return

            first_unanswered = None
            for q_data in snapshot:
                if q_data["position"] not in answered_positions:
                    first_unanswered = q_data
                    break

            if not first_unanswered and snapshot:
                first_unanswered = snapshot[0]

            if first_unanswered:
                await _send_journal_question(bot, chat_id, state, entry.id, first_unanswered, len(snapshot))
            return

        # Winner of insertion (brand new entry starting at position 1)
        first_q = snapshot[0]
        await _send_journal_question(bot, chat_id, state, entry.id, first_q, len(snapshot))


@router.callback_query(F.data == "j_start")
async def cb_journal_start(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Start today's reflection."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    chat_id = callback.message.chat.id
    if not await check_and_request_ai_disclaimer(bot, callback.from_user.id, chat_id, flow_type="journal_start"):
        await callback.answer()
        return

    await handle_journal_start(callback, state, bot)
    await callback.answer()


@router.callback_query(F.data.startswith("j_res:"))
async def cb_journal_resume(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Resume in-progress reflection."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    entry_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    async with async_session_maker() as session:
        entry = await session.get(JournalEntry, entry_id)
        if not entry or entry.user_id != user_id:
            await callback.answer("⛔️ Запись не найдена или принадлежит другому пользователю.", show_alert=True)
            return

        if entry.status == "completed":
            await callback.message.edit_text("✅ Эта рефлексия уже завершена.", reply_markup=journal_completed_today_keyboard(entry.id))
            await callback.answer()
            return

        snapshot = json.loads(entry.questions_snapshot_json or "[]")
        answers = (await session.scalars(select(JournalAnswer).where(JournalAnswer.entry_id == entry.id))).all()
        answered_positions = {a.position for a in answers}

        if snapshot and len(answered_positions) >= len(snapshot):
            # Defensive self-recovery
            await execute_transaction_a(session, entry.id, user_id)
            asyncio.create_task(run_journal_summary_and_update(entry.id, user_id, chat_id, bot=bot))
            await callback.message.edit_text(
                "✅ Все ответы были сохранены! Рефлексия завершена.\n\n⏳ Формирую итог дня...",
                reply_markup=journal_completed_today_keyboard(entry.id),
            )
            await callback.answer()
            return

        next_q = None
        for q in snapshot:
            if q["position"] not in answered_positions:
                next_q = q
                break
        if not next_q:
            next_q = snapshot[0]

        await _send_journal_question(
            bot,
            chat_id,
            state,
            entry.id,
            next_q,
            len(snapshot),
            edit_message=callback.message,
        )
        await callback.answer()


@router.callback_query(F.data.startswith("j_rst:"))
async def cb_journal_restart(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Restart reflection from question 1 (clears existing answers, keeps snapshot)."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    entry_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    async with async_session_maker() as session:
        entry = await session.get(JournalEntry, entry_id)
        if not entry or entry.user_id != user_id:
            await callback.answer("⛔️ Запись не найдена или принадлежит другому пользователю.", show_alert=True)
            return

        if entry.status == "completed":
            await callback.answer("✅ Завершенную запись нельзя перезапустить.", show_alert=True)
            return

        await session.execute(delete(JournalAnswer).where(JournalAnswer.entry_id == entry_id))
        await session.commit()

        snapshot = json.loads(entry.questions_snapshot_json or "[]")
        first_q = snapshot[0]
        await _send_journal_question(
            bot,
            callback.message.chat.id,
            state,
            entry.id,
            first_q,
            len(snapshot),
            edit_message=callback.message,
        )
        await callback.answer("🔄 Ответы сброшены. Начинаем заново!")


@router.message(JournalStates.answering_question, F.text)
async def msg_answer_question(message: Message, state: FSMContext, bot: Bot) -> None:
    """Handle text answer for question position 1..N."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    answer_text = (message.text or "").strip()

    if not answer_text:
        await message.answer("Пожалуйста, напиши свой ответ текстом.")
        return

    data = await state.get_data()
    entry_id = data.get("entry_id")
    pos = data.get("position", 1)
    total = data.get("total", 1)

    if not entry_id:
        await state.clear()
        await message.answer("Сессия ответа устарела. Откройте Дневник заново.")
        return

    async with async_session_maker() as session:
        entry = await session.get(JournalEntry, entry_id)
        if not entry or entry.user_id != user_id:
            await state.clear()
            await message.answer("Ошибка доступа к записи.")
            return

        if entry.status == "completed":
            await state.clear()
            await message.answer("✅ Эта рефлексия уже завершена.", reply_markup=journal_completed_today_keyboard(entry.id))
            return

        snapshot = json.loads(entry.questions_snapshot_json or "[]")
        current_q_snap = None
        for q in snapshot:
            if q.get("position") == pos:
                current_q_snap = q
                break

        if not current_q_snap:
            current_q_snap = {"position": pos, "text": f"Вопрос {pos}", "question_id": None}

        question_type = normalize_question_type(current_q_snap.get("question_type"), QUESTION_TYPE_TEXT)
        if question_type != QUESTION_TYPE_TEXT and not current_q_snap.get("allow_custom_answer"):
            await message.answer("Выбери один из вариантов кнопкой ниже.")
            return

        # Intermediate answer (pos < total)
        if pos < total:
            qid = current_q_snap.get("question_id")
            valid_qid = None
            if qid:
                q_exists = await session.get(JournalQuestion, qid)
                if q_exists:
                    valid_qid = qid

            ans = JournalAnswer(
                entry_id=entry_id,
                question_id=valid_qid,
                position=pos,
                question_text_snapshot=current_q_snap.get("text", ""),
                answer_text=answer_text,
            )
            try:
                async with session.begin_nested():
                    session.add(ans)
                    await session.flush()
                await session.commit()
            except IntegrityError:
                # Concurrent duplicate answer for this position.
                # Do NOT overwrite the first stored answer.
                pass

            # Reload answer positions from DB to determine source of truth
            answers_stmt = select(JournalAnswer.position).where(JournalAnswer.entry_id == entry_id)
            persisted_positions = set((await session.scalars(answers_stmt)).all())

            # Derive the first unanswered position from the immutable snapshot
            unanswered_pos = None
            for q in snapshot:
                p = q.get("position")
                if p not in persisted_positions:
                    unanswered_pos = p
                    break

            if unanswered_pos is None:
                # All questions answered! Check if entry is completed or complete it
                refreshed_entry = await session.get(JournalEntry, entry_id)
                if refreshed_entry and refreshed_entry.status == "completed":
                    await state.clear()
                    await message.answer("✅ Эта рефлексия уже завершена.", reply_markup=journal_completed_today_keyboard(entry_id))
                    return
                # If not yet completed, transition to Transaction A
                res = await execute_transaction_a(
                    session=session,
                    entry_id=entry_id,
                    user_id=user_id,
                )
                await state.clear()
                pts = res.get("points_awarded", 0)
                reward_granted = res.get("reward_granted", False)
                celebration_msg = (
                    "✨ <b>Рефлексия на сегодня успешно завершена!</b>\n\n"
                    f"⭐️ Начислено баллов: +{pts}\n"
                )
                if reward_granted:
                    celebration_msg += (
                        "\n🎉 <b>Поздравляем! Ты завершил(а) 21 день рефлексии!</b>\n"
                        "🎁 Тебе начислено <b>+7 дней подписки</b> в подарок!\n"
                    )
                celebration_msg += "\n⏳ <i>Формирую бережный персональный итог дня...</i>"
                await message.answer(celebration_msg, parse_mode="HTML")
                asyncio.create_task(run_journal_summary_and_update(entry_id, user_id, chat_id, bot=bot))
                return

            next_pos = unanswered_pos
            await state.update_data(position=next_pos)

            next_q = next((q for q in snapshot if q.get("position") == next_pos), None)
            if next_q:
                await state.set_state(JournalStates.answering_question)
                await state.update_data(entry_id=entry_id, position=next_pos, total=total)
                await message.answer(
                    _journal_question_text(next_q, next_pos, total),
                    parse_mode="HTML",
                    reply_markup=journal_question_keyboard(entry_id, next_pos, next_q),
                )
            return

        # Final answer (pos == total) -> execute Transaction A
        res = await execute_transaction_a(
            session=session,
            entry_id=entry_id,
            user_id=user_id,
            final_answer_text=answer_text,
            final_question_snapshot=current_q_snap,
        )
        await state.clear()

        pts = res.get("points_awarded", 0)
        reward_granted = res.get("reward_granted", False)

        celebration_msg = (
            "✨ <b>Рефлексия на сегодня успешно завершена!</b>\n\n"
            f"⭐️ Начислено баллов: +{pts}\n"
        )
        if reward_granted:
            celebration_msg += (
                "\n🎉 <b>Поздравляем! Ты завершил(а) 21 день рефлексии!</b>\n"
                "🎁 Тебе начислено <b>+7 дней подписки</b> в подарок!\n"
            )

        celebration_msg += "\n⏳ <i>Формирую бережный персональный итог дня...</i>"
        status_msg = await message.answer(celebration_msg, parse_mode="HTML")

        # Trigger AI summary generation
        asyncio.create_task(run_journal_summary_and_update(entry_id, user_id, chat_id, bot))


@router.callback_query(F.data.startswith("j_opt:"))
async def cb_journal_option(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    try:
        _, entry_id_raw, position_raw, option_raw = callback.data.split(":", 3)
        entry_id, position, option_index = int(entry_id_raw), int(position_raw), int(option_raw)
    except (TypeError, ValueError):
        await callback.answer("Некорректный вариант ответа.", show_alert=True)
        return

    state_data = await state.get_data()
    if state_data.get("entry_id") != entry_id or state_data.get("position") != position:
        await callback.answer("Этот вопрос уже закрыт.", show_alert=True)
        return

    async with async_session_maker() as session:
        entry = await session.get(JournalEntry, entry_id)
        if not entry or entry.user_id != callback.from_user.id:
            await callback.answer("⛔️ Запись не найдена.", show_alert=True)
            return
        snapshot = json.loads(entry.questions_snapshot_json or "[]")
        question = _snapshot_question(snapshot, position)
        options = question.get("options", []) if question else []
        if option_index < 0 or option_index >= len(options):
            await callback.answer("Такого варианта ответа нет.", show_alert=True)
            return
        option = options[option_index]
        answer_text = option.get("text", "") if isinstance(option, dict) else str(option)

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _continue_after_journal_answer(
        callback.message,
        state,
        bot,
        entry_id,
        callback.from_user.id,
        position,
        answer_text,
    )


@router.callback_query(F.data.startswith("j_skip:"))
async def cb_journal_skip(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    try:
        _, entry_id_raw, position_raw = callback.data.split(":", 2)
        entry_id, position = int(entry_id_raw), int(position_raw)
    except (TypeError, ValueError):
        await callback.answer("Некорректный вопрос.", show_alert=True)
        return

    state_data = await state.get_data()
    if state_data.get("entry_id") != entry_id or state_data.get("position") != position:
        await callback.answer("Этот вопрос уже закрыт.", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _continue_after_journal_answer(
        callback.message,
        state,
        bot,
        entry_id,
        callback.from_user.id,
        position,
        is_skipped=True,
    )


@router.callback_query(F.data == "j_exit")
async def cb_journal_exit(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await callback.answer()
    from handlers import send_main_menu
    await send_main_menu(callback, bot=bot)


async def run_journal_summary_and_update(
    entry_id: int,
    user_id: int,
    chat_id: int,
    bot: Bot,
) -> None:
    """Run automatic daily summary and update chat according to JournalAiResult contract:
    - succeeded: render and send saved summary
    - already_running: idempotent duplicate, send nothing, winner produces final summary
    - insufficient_tokens: show standard token top-up alert
    - failed: show approved retry-summary action with fresh token
    """
    from ai_integration import generate_journal_daily_summary, JournalAiResult

    ai_res = await generate_journal_daily_summary(
        entry_id=entry_id,
        user_id=user_id,
        chat_id=chat_id,
        bot=bot,
    )

    if crisis_was_detected_in_turn(user_id):
        await send_crisis_contacts_if_active(bot, user_id, chat_id)
        return

    if ai_res == JournalAiResult.ALREADY_RUNNING:
        # Idempotent duplicate: existing running operation remains owner, send nothing
        return

    if ai_res == JournalAiResult.INSUFFICIENT_TOKENS:
        from keyboards import token_alert_keyboard
        await bot.send_message(
            chat_id=chat_id,
            text="❌ На твоем балансе недостаточно токенов для формирования итога дня. Пополни баланс, чтобы получить персональный анализ.",
            reply_markup=token_alert_keyboard(),
        )
        return

    async with async_session_maker() as final_session:
        completed_entry = await final_session.get(JournalEntry, entry_id)
        if ai_res == JournalAiResult.SUCCEEDED and completed_entry and completed_entry.summary_text:
            summary_block = (
                f"📔 <b>Итог дня ({completed_entry.reflection_date})</b>\n\n"
                f"{html.escape(completed_entry.summary_text)}\n\n"
            )
            if completed_entry.encouraging_message:
                summary_block += f"💌 <b>Поддержка:</b>\n{html.escape(completed_entry.encouraging_message)}\n\n"
            if completed_entry.tomorrow_focus:
                summary_block += f"🎯 <b>Фокус на завтра:</b>\n{html.escape(completed_entry.tomorrow_focus)}\n\n"

            kb = journal_completed_today_keyboard(entry_id)
            await bot.send_message(chat_id=chat_id, text=summary_block, reply_markup=kb, parse_mode="HTML")
        else:
            token8 = uuid.uuid4().hex[:8]
            retry_kb = journal_entry_detail_keyboard(entry_id, has_summary=False, token8=token8)
            await bot.send_message(
                chat_id=chat_id,
                text="⚠️ Не удалось сформировать итог дня прямо сейчас. Вы можете повторить формирование позже из деталей записи.",
                reply_markup=retry_kb,
            )


# History Handlers
@router.callback_query(F.data.startswith("j_h_p:"))
async def cb_journal_history_page(callback: CallbackQuery, bot: Bot) -> None:
    """Paginated list of completed entries (page size = 5)."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    page = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    page_size = 5

    async with async_session_maker() as session:
        total_count = (await session.scalar(
            select(func.count(JournalEntry.id)).where(
                JournalEntry.user_id == user_id,
                JournalEntry.status == "completed",
            )
        )) if "func" in globals() else len((await session.scalars(select(JournalEntry).where(JournalEntry.user_id == user_id, JournalEntry.status == "completed"))).all())

        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
        page = max(0, min(page, total_pages - 1))

        entries_stmt = (
            select(JournalEntry)
            .where(
                JournalEntry.user_id == user_id,
                JournalEntry.status == "completed",
            )
            .order_by(JournalEntry.reflection_date.desc())
            .offset(page * page_size)
            .limit(page_size)
        )
        entries = (await session.scalars(entries_stmt)).all()

        if not entries:
            text_msg = "📖 У тебя пока нет сохранённых записей рефлексии."
            kb = journal_menu_keyboard()
        else:
            text_msg = f"📖 <b>Твои прошлые записи (страница {page + 1} из {total_pages})</b>:"
            kb = journal_history_list_keyboard(page, total_pages, entries)

        await callback.message.edit_text(text_msg, reply_markup=kb, parse_mode="HTML")
        await callback.answer()


@router.callback_query(F.data.startswith("j_h_v:"))
async def cb_journal_history_view(callback: CallbackQuery, bot: Bot) -> None:
    """Detail view of a completed entry."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    entry_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    async with async_session_maker() as session:
        entry = await session.get(JournalEntry, entry_id)
        if not entry or entry.user_id != user_id:
            await callback.answer("⛔️ Запись не найдена или принадлежит другому пользователю.", show_alert=True)
            return
        if entry.status != "completed":
            await callback.answer("⛔️ Запись ещё не завершена.", show_alert=True)
            return

        answers_stmt = (
            select(JournalAnswer)
            .where(JournalAnswer.entry_id == entry_id)
            .order_by(JournalAnswer.position.asc())
        )
        answers = (await session.scalars(answers_stmt)).all()

        text_lines = [f"📅 <b>Запись за {entry.reflection_date}</b>\n"]
        for a in answers:
            answer_label = "Пропущено" if a.is_skipped else (a.answer_text or "")
            text_lines.append(
                f"<b>Вопрос {a.position}:</b> {html.escape(a.question_text_snapshot)}\n"
                f"<b>Ответ:</b> {html.escape(answer_label)}\n"
            )

        has_summary = bool(entry.summary_text and entry.summary_text.strip())
        if has_summary:
            text_lines.append(f"✨ <b>Итог дня:</b>\n{html.escape(entry.summary_text)}\n")
            if entry.encouraging_message:
                text_lines.append(f"💌 <b>Поддержка:</b>\n{html.escape(entry.encouraging_message)}\n")
            if entry.tomorrow_focus:
                text_lines.append(f"🎯 <b>Фокус:</b>\n{html.escape(entry.tomorrow_focus)}\n")
        else:
            text_lines.append("<i>Итог дня ещё не сформирован.</i>\n")

        token8 = uuid.uuid4().hex[:8]
        kb = journal_entry_detail_keyboard(entry_id, has_summary=has_summary, token8=token8)
        content_text = "\n".join(text_lines)

        await callback.message.edit_text(content_text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()


@router.callback_query(F.data.startswith("j_ret:"))
async def cb_journal_retry_summary(callback: CallbackQuery, bot: Bot) -> None:
    """Retry summary generation for an entry that lacks it."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    parts = callback.data.split(":")
    entry_id = int(parts[1])
    token8 = parts[2] if len(parts) > 2 else uuid.uuid4().hex[:8]
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    if not await check_and_request_ai_disclaimer(bot, user_id, chat_id, flow_type="journal"):
        await callback.answer()
        return

    async with async_session_maker() as session:
        entry = await session.get(JournalEntry, entry_id)
        if not entry or entry.user_id != user_id or entry.status != "completed":
            await callback.answer("⛔️ Запись не найдена или ещё не завершена.", show_alert=True)
            return

        if entry.summary_text and entry.summary_text.strip():
            await callback.answer("Итог дня уже сформирован.", show_alert=True)
            return

    await callback.answer("⏳ Формирую итог дня...")
    await callback.message.edit_text("⏳ Формирую итог дня, пожалуйста, подождите...")

    from ai_integration import generate_journal_daily_summary
    op_key = f"summary_retry:{entry_id}:{token8}"
    res = await generate_journal_daily_summary(
        entry_id=entry_id,
        user_id=user_id,
        chat_id=chat_id,
        bot=bot,
        operation_key=op_key,
    )

    if crisis_was_detected_in_turn(user_id):
        await send_crisis_contacts_if_active(bot, user_id, chat_id)
        return

    if res == "succeeded":
        async with async_session_maker() as session:
            refreshed = await session.get(JournalEntry, entry_id)
        if refreshed and refreshed.summary_text:
            text_msg = (
                f"✨ <b>Итог дня ({refreshed.reflection_date})</b>:\n\n"
                f"{html.escape(refreshed.summary_text)}\n\n"
            )
            if refreshed.encouraging_message:
                text_msg += f"💌 <b>Поддержка:</b>\n{html.escape(refreshed.encouraging_message)}\n\n"
            if refreshed.tomorrow_focus:
                text_msg += f"🎯 <b>Фокус на завтра:</b>\n{html.escape(refreshed.tomorrow_focus)}\n\n"
            kb = journal_entry_detail_keyboard(entry_id, has_summary=True, token8=token8)
            await callback.message.edit_text(text_msg, reply_markup=kb, parse_mode="HTML")
            return

    if res == "already_running":
        await callback.message.edit_text("⏳ Итог дня уже формируется. Пожалуйста, подождите...")
        return

    if res == "insufficient_tokens":
        await callback.message.edit_text("⚠️ Недостаточно токенов для формирования итога дня.")
        return

    # Failed
    kb = journal_entry_detail_keyboard(entry_id, has_summary=False, token8=uuid.uuid4().hex[:8])
    await callback.message.edit_text(
        "⚠️ Не удалось сформировать итог дня. Попробуйте повторить позже.",
        reply_markup=kb,
    )


# Dynamics Handlers
@router.callback_query(F.data == "j_dyn_menu")
async def cb_journal_dynamics_menu(callback: CallbackQuery, bot: Bot) -> None:
    """Menu to select dynamics window (7 or 21 days)."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    chat_id = callback.message.chat.id
    if not await check_and_request_ai_disclaimer(bot, callback.from_user.id, chat_id, flow_type="journal"):
        await callback.answer()
        return

    token8 = uuid.uuid4().hex[:8]
    kb = journal_dynamics_menu_keyboard(token8)
    text_msg = "📈 <b>Выберите период для анализа динамики</b>:\n\nИИ проанализирует изменения, закономерности и ключевые точки роста."
    await callback.message.edit_text(text_msg, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("j_d7:") | F.data.startswith("j_d21:"))
async def cb_journal_run_dynamics(callback: CallbackQuery, bot: Bot) -> None:
    """Run 7-day or 21-day dynamics analysis."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    chat_id = callback.message.chat.id
    if not await check_and_request_ai_disclaimer(bot, callback.from_user.id, chat_id, flow_type="journal"):
        await callback.answer()
        return

    prefix, token8 = callback.data.split(":")
    days = 7 if prefix == "j_d7" else 21
    user_id = callback.from_user.id

    await callback.answer("⏳ Анализирую динамику...")
    await callback.message.edit_text(f"⏳ Анализирую ваши записи за {days} дней, пожалуйста, подождите...")

    from ai_integration import generate_journal_dynamics_analysis
    success, result_text = await generate_journal_dynamics_analysis(
        user_id=user_id,
        days=days,
        chat_id=chat_id,
        token8=token8,
        bot=bot,
    )

    if crisis_was_detected_in_turn(user_id):
        await send_crisis_contacts_if_active(bot, user_id, chat_id)
        return

    kb = journal_dynamics_menu_keyboard(uuid.uuid4().hex[:8])
    if success:
        formatted = f"📈 <b>Анализ динамики за {days} дней</b>:\n\n{html.escape(result_text)}"
        await callback.message.edit_text(formatted, reply_markup=kb, parse_mode="HTML")
    else:
        await callback.message.edit_text(result_text, reply_markup=kb)


# Reminder Settings Handlers
@router.callback_query(F.data == "j_time_menu")
async def cb_journal_reminder_menu(callback: CallbackQuery, bot: Bot) -> None:
    """Show reminder configuration screen."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    user_id = callback.from_user.id
    async with async_session_maker() as session:
        settings = await get_or_create_journal_settings(session, user_id)
        if settings.reminders_enabled and settings.reminder_time:
            status_text = f"Текущее время: <b>{settings.reminder_time} МСК</b> (включено)"
        else:
            status_text = "Напоминания <b>отключены</b>"

    text_msg = (
        f"⏰ <b>Настройка времени вечерней рефлексии</b>\n\n"
        f"{status_text}\n\n"
        "Выберите удобное время или укажите своё:"
    )
    kb = journal_reminder_time_keyboard()
    await callback.message.edit_text(text_msg, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("j_time_set:"))
async def cb_journal_time_set_preset(callback: CallbackQuery, bot: Bot) -> None:
    """Set preset reminder time (19:00, 20:00, 21:00, 22:00)."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    code = callback.data.split(":")[1]
    time_map = {
        "1900": "19:00",
        "2000": "20:00",
        "2100": "21:00",
        "2200": "22:00",
    }
    target_time = time_map.get(code, "21:00")
    user_id = callback.from_user.id

    async with async_session_maker() as session:
        settings = await get_or_create_journal_settings(session, user_id)
        settings.reminder_time = target_time
        settings.reminders_enabled = True
        await session.commit()

    await callback.answer(f"⏰ Напоминание установлено на {target_time} МСК!", show_alert=True)
    await show_journal_menu(callback, bot)


@router.callback_query(F.data == "j_time_manual")
async def cb_journal_time_manual(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Switch to manual time input mode."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    await state.set_state(JournalStates.setting_reminder_time)
    text_msg = (
        "✏️ Введите желаемое время напоминания в формате <b>ЧЧ:ММ</b> по московскому времени (например, <code>21:30</code>):"
    )
    await callback.message.edit_text(text_msg, parse_mode="HTML")
    await callback.answer()


@router.message(JournalStates.setting_reminder_time, F.text)
async def msg_journal_time_manual_input(message: Message, state: FSMContext, bot: Bot) -> None:
    r"""Validate manual time input ^([01]\d|2[0-3]):[0-5]\d$."""
    raw = (message.text or "").strip()
    match = re.match(r"^([01]\d|2[0-3]):[0-5]\d$", raw)

    if not match:
        await message.answer(
            "⚠️ Неверный формат времени. Введите время в формате <b>ЧЧ:ММ</b> (от <code>00:00</code> до <code>23:59</code>, например: <code>21:15</code>):",
            parse_mode="HTML",
        )
        return

    normalized_time = match.group(0)
    user_id = message.from_user.id

    async with async_session_maker() as session:
        settings = await get_or_create_journal_settings(session, user_id)
        settings.reminder_time = normalized_time
        settings.reminders_enabled = True
        await session.commit()

    await state.clear()
    await message.answer(f"✅ Время напоминания установлено на <b>{normalized_time} МСК</b>!", parse_mode="HTML")
    await show_journal_menu(message, bot, send_new_message=True)


@router.callback_query(F.data == "j_time_off")
async def cb_journal_time_off(callback: CallbackQuery, bot: Bot) -> None:
    """Turn off reminders (sets reminders_enabled=False, clears snooze)."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    user_id = callback.from_user.id
    async with async_session_maker() as session:
        settings = await get_or_create_journal_settings(session, user_id)
        settings.reminders_enabled = False
        settings.snooze_until = None
        settings.snooze_claimed_at = None
        await session.commit()

    await callback.answer("🔕 Напоминания отключены.", show_alert=True)
    await show_journal_menu(callback, bot)


@router.callback_query(F.data == "j_snooze_menu")
async def cb_journal_snooze_menu(callback: CallbackQuery, bot: Bot) -> None:
    """Show snooze delay options."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    kb = journal_snooze_menu_keyboard()
    text_msg = "⏰ <b>Напомнить о рефлексии позже</b>:\n\nВыберите время отсрочки:"
    await callback.message.edit_text(text_msg, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("j_snooze:"))
async def cb_journal_snooze_set(callback: CallbackQuery, bot: Bot) -> None:
    """Apply snooze duration (15, 30, 60 minutes)."""
    if not await check_paid_access(callback, bot, topic_slug="journal"):
        return

    mins = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    async with async_session_maker() as session:
        settings = await get_or_create_journal_settings(session, user_id)
        settings.snooze_until = msk_now() + timedelta(minutes=mins)
        settings.snooze_claimed_at = None
        await session.commit()

    await callback.answer(f"⏰ Напоминание отложено на {mins} минут.", show_alert=True)
    await show_journal_menu(callback, bot)
