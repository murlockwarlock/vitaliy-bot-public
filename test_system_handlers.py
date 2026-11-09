from __future__ import annotations

import html
import io
import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import delete, func, select, update

import keyboards as kb
from admin_roles import AdminPermission, role_has_permission, user_has_permission
from config import OWNER_IDS
from ai_dialogue_service import (
    acquire_pre_test_claim,
    build_ai_user_profile_context,
    complete_pre_test_claim,
    renew_pre_test_claim_lease,
)
from ai_integration import ai_typing_action, get_ai_response_for_topic, get_standalone_ai_response
from database import (
    Message as DBMessage,
    PreTestClaim,
    TestAttempt,
    TestConfig,
    TestQuestion,
    TestSession,
    TestSet,
    User,
    async_session_maker,
)
from file_parser import parse_formulas_file, parse_questions_file
from time_utils import msk_now
from universal_tests import (
    build_prompt_payload,
    build_test_attempt_snapshot,
    build_question_text,
    build_result_handoff_prompt,
    calculate_formulas,
    get_answer_options,
    json_dumps,
    json_loads,
    make_option_answer_record,
    make_text_answer_record,
    parse_answers,
    question_buttons_are_horizontal,
    serialize_answers,
    expected_test_set_question_count,
    is_esoteric_question,
    is_test_set_ready,
    load_test_attempt_snapshot,
    snapshot_questions,
    snapshot_test_set,
    validate_test_definition,
)


router = Router(name="universal_tests")
logger = logging.getLogger(__name__)
TEST_START_DIRECTIVE_RE = re.compile(
    r"\[(?:START_TEST|RUN_TEST|ЗАПУСТИТЬ_ТЕСТ)\]",
    re.IGNORECASE,
)


class UniversalTestStates(StatesGroup):
    answering = State()


class TestAdminStates(StatesGroup):
    upload_questions = State()
    upload_formulas = State()
    set_result_prompt = State()
    set_selected_variables = State()


def extract_test_start_directive(text: str | None) -> tuple[bool, str]:
    raw = text or ""
    should_start = bool(TEST_START_DIRECTIVE_RE.search(raw))
    clean = TEST_START_DIRECTIVE_RE.sub("", raw)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip(" \t\r\n-:;—–")
    return should_start, clean


async def _is_admin(user_id: int) -> bool:
    return await user_has_permission(user_id, AdminPermission.CONTENT)


async def _config(session) -> TestSet | None:
    """Delegates legacy config access to canonical TestSet(id=1) / diagnostics."""
    ts = (await session.execute(select(TestSet).where(TestSet.slug == "diagnostics"))).scalar_one_or_none()
    if not ts:
        ts = await session.get(TestSet, 1)
    return ts


async def _get_test_set(session, test_set_id: int | None = None) -> TestSet | None:
    if test_set_id is not None and test_set_id != 1:
        return await session.get(TestSet, test_set_id)
    return await _config(session)


async def _questions(
    session,
    test_set_id: int | None = None,
    topic_slug: str | None = None,
) -> list[TestQuestion]:
    effective_set_id = test_set_id if test_set_id is not None else 1
    stmt = (
        select(TestQuestion)
        .where(TestQuestion.test_set_id == effective_set_id, TestQuestion.is_active == True)
        .order_by(TestQuestion.sort_order, TestQuestion.id)
    )
    result = await session.execute(stmt)
    questions = list(result.scalars().all())
    test_set = await _get_test_set(session, effective_set_id)
    target_scenario = str(topic_slug or "").strip().casefold() in {"diagnostics", "express"}
    if target_scenario:
        questions = [question for question in questions if not is_esoteric_question(question)]
    return questions


async def get_test_set_readiness(session, test_set_id: int, topic_slug: str | None = None):
    test_set = await _get_test_set(session, test_set_id)
    if not test_set:
        return None, 0, False
    active_count = len(await _questions(session, test_set.id, topic_slug=topic_slug))
    return test_set, active_count, is_test_set_ready(test_set, active_count, topic_slug)


async def send_test_setup_unavailable(
    bot: Bot,
    user_id: int,
    state: FSMContext | None = None,
    *,
    active_count: int | None = None,
    expected_count: int | None = None,
) -> None:
    if state is not None:
        await state.clear()
    if expected_count:
        details = f"Сейчас подготовлено {active_count or 0} из {expected_count} утверждённых вопросов."
    else:
        details = "Утверждённые вопросы ещё не загружены."
    await bot.send_message(
        user_id,
        "🛠 <b>Анкета настраивается</b>\n\n"
        f"{details}\n"
        "Вернись позже, когда настройка будет завершена.",
        reply_markup=kb.universal_test_setup_keyboard(),
        parse_mode="HTML",
    )


def _answer_keyboard(question: TestQuestion, index: int):
    options = get_answer_options(question)
    return kb.universal_test_question_keyboard(
        options,
        horizontal=question_buttons_are_horizontal(question),
        question_index=index,
    )


async def _send_question(
    bot: Bot,
    chat_id: int,
    index: int,
    test_set_id: int | None = None,
    state: FSMContext | None = None,
) -> None:
    async with async_session_maker() as session:
        tsess = await session.get(TestSession, chat_id)
        if test_set_id is None and tsess and tsess.test_set_id:
            test_set_id = tsess.test_set_id
        attempt = None
        topic_slug = None
        if tsess and tsess.attempt_id:
            attempt = await session.get(TestAttempt, tsess.attempt_id)
            topic_slug = attempt.topic_slug if attempt else None

        live_test_set = await _get_test_set(session, test_set_id)
        snapshot = load_test_attempt_snapshot(
            attempt.configuration_snapshot_json if attempt else None
        )
        if snapshot:
            test_set = snapshot_test_set(snapshot, live_test_set)
            questions = snapshot_questions(snapshot)
            show_progress = bool(snapshot["test_set"].get("show_progress", True))
        else:
            test_set = live_test_set
            if not test_set:
                logger.warning("TestSet not found in _send_question for chat_id=%s test_set_id=%s", chat_id, test_set_id)
                await send_test_setup_unavailable(bot, chat_id, state)
                return
            questions = await _questions(session, test_set.id, topic_slug=topic_slug)
            if not is_test_set_ready(test_set, len(questions), topic_slug):
                await send_test_setup_unavailable(
                    bot,
                    chat_id,
                    state,
                    active_count=len(questions),
                    expected_count=expected_test_set_question_count(test_set, topic_slug),
                )
                return
            show_progress = test_set.show_progress

        if attempt:
            if attempt and attempt.purpose == "pre_dialogue" and attempt.status == "in_progress":
                await renew_pre_test_claim_lease(
                    session,
                    user_id=chat_id,
                    topic_slug=attempt.topic_slug,
                    dialogue_id=attempt.dialogue_id,
                    test_set_id=test_set.id,
                )
                await session.commit()

    if index >= len(questions):
        await bot.send_message(chat_id, "Вопросы теста закончились.")
        return
    question = questions[index]
    await bot.send_message(
        chat_id,
        build_question_text(question, index, len(questions), show_progress),
        reply_markup=_answer_keyboard(question, index),
        parse_mode="HTML",
    )


async def start_universal_test(
    bot: Bot,
    user_id: int,
    state: FSMContext,
    *,
    test_set_id: int | None = None,
    topic_slug: str | None = None,
    dialogue_id: int | None = None,
    purpose: str = "general",
    admin_preview: bool = False,
) -> bool:
    async with async_session_maker() as session:
        test_set = await _get_test_set(session, test_set_id)
        if not test_set:
            logger.warning("TestSet not found for test_set_id=%s, failing safely", test_set_id)
            await bot.send_message(user_id, "Тестирование временно недоступно: тест не настроен администратором.")
            return False

        is_enabled = test_set.is_enabled and not test_set.is_archived
        test_set_name = test_set.name
        test_set_slug = test_set.slug
        actual_set_id = test_set.id

        questions = await _questions(session, actual_set_id, topic_slug=topic_slug)
        if not is_test_set_ready(test_set, len(questions), topic_slug):
            await send_test_setup_unavailable(
                bot,
                user_id,
                state,
                active_count=len(questions),
                expected_count=expected_test_set_question_count(test_set, topic_slug),
            )
            return False
        user = await session.get(User, user_id)
        can_preview = user_id in OWNER_IDS or bool(
            user and user.is_admin and role_has_permission(
                getattr(user, "admin_role", None), AdminPermission.CONTENT
            )
        )
        if admin_preview and not can_preview:
            await bot.send_message(user_id, "⛔️ Доступ запрещён.")
            return False
        if not is_enabled and not can_preview:
            await bot.send_message(user_id, "Тестирование сейчас отключено.")
            return False
        if not questions:
            await bot.send_message(user_id, "Тест пока недоступен: администратор ещё не загрузил вопросы.")
            return False

        # Materialize the snapshot before acquiring the pre-test claim.  A
        # failed claim insert rolls back this session and expires ORM objects;
        # the retry path must still use the exact configuration selected here.
        configuration_snapshot_json = build_test_attempt_snapshot(test_set, questions)

        effective_topic_slug = topic_slug or (user.current_topic_slug if user else None)
        effective_dialogue_id = dialogue_id or (user.current_dialogue_id if user else 1)

        # Guard 3: DB-backed ownership for pre-dialogue test once-per-dialogue
        pre_claim = None
        if purpose == "pre_dialogue" and effective_topic_slug:
            acquired, pre_claim = await acquire_pre_test_claim(
                session,
                user_id=user_id,
                topic_slug=effective_topic_slug,
                dialogue_id=effective_dialogue_id,
                test_set_id=actual_set_id,
            )
            if not acquired:
                logger.info("Pre-test claim already owned or completed for user=%s topic=%s dialogue=%s", user_id, effective_topic_slug, effective_dialogue_id)
                # P0-1: Check if matching live TestSession exists for this user
                tsess = (await session.execute(
                    select(TestSession).where(
                        TestSession.user_id == user_id,
                        TestSession.is_finished == False,
                        TestSession.test_set_id == actual_set_id,
                        TestSession.invocation_topic_slug == effective_topic_slug,
                        TestSession.invocation_dialogue_id == effective_dialogue_id,
                    )
                )).scalar_one_or_none()
                if tsess and pre_claim and pre_claim.status == "in_progress" and (pre_claim.lease_until is None or pre_claim.lease_until > msk_now()):
                    await renew_pre_test_claim_lease(session, user_id, effective_topic_slug, effective_dialogue_id, actual_set_id)
                    await session.commit()
                    await state.set_state(UniversalTestStates.answering)
                    await _send_question(bot, user_id, tsess.current_question_index, actual_set_id, state)
                    return True
                return False

        attempt = TestAttempt(
            user_id=user_id,
            test_set_id=actual_set_id,
            test_set_name=test_set_name,
            test_set_slug=test_set_slug,
            topic_slug=effective_topic_slug,
            dialogue_id=effective_dialogue_id,
            purpose=purpose,
            answers="[]",
            status="in_progress",
            started_at=msk_now(),
            configuration_snapshot_json=configuration_snapshot_json,
        )
        session.add(attempt)
        await session.flush()

        if pre_claim:
            pre_claim.attempt_id = attempt.id

        await session.execute(delete(TestSession).where(TestSession.user_id == user_id))
        session.add(TestSession(
            user_id=user_id,
            answers="[]",
            current_question_index=0,
            is_finished=False,
            invocation_topic_slug=effective_topic_slug,
            invocation_dialogue_id=effective_dialogue_id,
            test_set_id=actual_set_id,
            topic_slug=effective_topic_slug,
            attempt_id=attempt.id,
            created_at=msk_now(),
        ))
        await session.commit()

    try:
        await state.set_state(UniversalTestStates.answering)
        await _send_question(bot, user_id, 0, test_set_id=actual_set_id, state=state)
    except Exception:
        logger.exception("Failed to send first test question to user=%s, marking claim/attempt failed", user_id)
        async with async_session_maker() as fail_session:
            if pre_claim:
                await fail_session.execute(
                    update(PreTestClaim)
                    .where(PreTestClaim.id == pre_claim.id)
                    .values(status='failed')
                )
            await fail_session.execute(
                update(TestAttempt)
                .where(TestAttempt.id == attempt.id)
                .values(status='failed')
            )
            await fail_session.commit()
        raise
    return True


async def process_ai_test_directive(
    response_text: str,
    bot: Bot,
    user_id: int,
    state: FSMContext,
    topic_slug: str | None = None,
    dialogue_id: int | None = None,
    **kwargs,
) -> tuple[bool, str]:
    topic_slug = topic_slug or kwargs.get("topic_slug")
    dialogue_id = dialogue_id or kwargs.get("dialogue_id")
    should_start, clean_text = extract_test_start_directive(response_text)
    if not should_start:
        return False, clean_text
    await start_universal_test(
        bot,
        user_id,
        state,
        topic_slug=topic_slug,
        dialogue_id=dialogue_id,
    )
    return True, clean_text


async def _save_answer(user_id: int, payload: str, *, is_text: bool) -> tuple[int, int, list[dict]]:
    async with async_session_maker() as session:
        test_session = await session.get(TestSession, user_id, with_for_update=True)
        if not test_session or test_session.is_finished:
            raise ValueError("Эта сессия уже завершена. Запустите тест заново.")
        attempt = await session.get(TestAttempt, test_session.attempt_id) if test_session.attempt_id else None
        snapshot = load_test_attempt_snapshot(
            attempt.configuration_snapshot_json if attempt else None
        )
        if snapshot:
            questions = snapshot_questions(snapshot)
        else:
            questions = await _questions(
                session,
                test_session.test_set_id,
                topic_slug=test_session.invocation_topic_slug,
            )
        index = test_session.current_question_index
        if index >= len(questions):
            raise ValueError("Этот вопрос уже закрыт.")
        question = questions[index]
        record = (
            make_text_answer_record(question, index, payload)
            if is_text
            else make_option_answer_record(question, index, payload)
        )
        answers = parse_answers(test_session.answers)
        answers.append(record)
        test_session.answers = serialize_answers(answers)
        test_session.current_question_index += 1

        if test_session.attempt_id:
            if attempt and attempt.purpose == "pre_dialogue" and attempt.status == "in_progress":
                await renew_pre_test_claim_lease(
                    session,
                    user_id=user_id,
                    topic_slug=attempt.topic_slug,
                    dialogue_id=attempt.dialogue_id,
                    test_set_id=test_session.test_set_id,
                )

        await session.commit()
        return test_session.current_question_index, len(questions), answers


async def _send_long(
    bot: Bot,
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = None,
    include_finish_markup: bool = True,
) -> None:
    remaining = (text or "").strip()
    if not remaining:
        remaining = "Интерпретация результата сейчас недоступна."
    finish_markup = await kb.ai_exit_keyboard_for_topic() if include_finish_markup else None

    if parse_mode == "HTML":
        from md_utils import split_html_message, strip_telegram_html
        chunks = split_html_message(remaining, 4000)
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            try:
                await bot.send_message(
                    chat_id,
                    chunk,
                    reply_markup=finish_markup if is_last else None,
                    parse_mode="HTML",
                )
            except TelegramBadRequest:
                plain_chunk = strip_telegram_html(chunk)
                await bot.send_message(
                    chat_id,
                    plain_chunk,
                    reply_markup=finish_markup if is_last else None,
                    parse_mode=None,
                )
        return

    while remaining:
        if len(remaining) <= 4000:
            chunk, remaining = remaining, ""
        else:
            split_at = remaining.rfind("\n", 0, 4000)
            if split_at < 1000:
                split_at = 4000
            chunk, remaining = remaining[:split_at], remaining[split_at:].lstrip()
        await bot.send_message(
            chat_id,
            chunk,
            reply_markup=finish_markup if not remaining else None,
            parse_mode=parse_mode,
        )


async def _finish_test(bot: Bot, user_id: int, state: FSMContext, answers: list[dict]) -> None:
    loading = await bot.send_message(user_id, "Спасибо! Анализирую ответы…")
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        test_session = await session.get(TestSession, user_id, with_for_update=True)
        attempt = await session.get(TestAttempt, test_session.attempt_id) if test_session and test_session.attempt_id else None
        live_test_set = await _get_test_set(session, test_session.test_set_id if test_session else None)
        snapshot = load_test_attempt_snapshot(
            attempt.configuration_snapshot_json if attempt else None
        )
        if snapshot:
            test_set = snapshot_test_set(snapshot, live_test_set)
            questions = snapshot_questions(snapshot)
        else:
            test_set = live_test_set
            questions = await _questions(
                session,
                test_session.test_set_id if test_session else None,
                topic_slug=test_session.invocation_topic_slug if test_session else None,
            )
        if not test_set:
            logger.warning("TestSet not found in _finish_test for user_id=%s, failing safely", user_id)
            await bot.send_message(user_id, "Ошибка: конфигурация теста не найдена.")
            return

        topic_slug = test_session.invocation_topic_slug if test_session else (user.current_topic_slug if user else None)
        dialogue_id = test_session.invocation_dialogue_id if test_session else (user.current_dialogue_id if user else None)
        if not snapshot and topic_slug in {"diagnostics", "express"} and not is_test_set_ready(test_set, len(questions), topic_slug):
            try:
                await loading.delete()
            except TelegramBadRequest:
                pass
            await send_test_setup_unavailable(
                bot,
                user_id,
                state,
                active_count=len(questions),
                expected_count=expected_test_set_question_count(test_set, topic_slug),
            )
            return

        formulas_enabled = test_set.formulas_enabled
        formulas_json = test_set.formulas_json
        formulas = json_loads(formulas_json, []) if formulas_enabled else []
        try:
            formula_results = calculate_formulas(answers, formulas) if formulas else {}
        except (ValueError, ZeroDivisionError, OverflowError):
            logger.exception("Universal test formula calculation failed for user_id=%s", user_id)
            formula_results = {}

        selected_vars = test_set.interpretation_selected_variables
        selected = json_loads(selected_vars, [])
        input_mode = test_set.interpretation_input_mode

        prompt_payload = build_prompt_payload(
            questions,
            answers,
            formula_results,
            mode=input_mode,
            selected_variables=selected,
        )
        sep_enabled = test_set.separate_result_prompt_enabled
        res_prompt = test_set.result_system_prompt
        separate_enabled = bool(sep_enabled and res_prompt)
        result_prompt = res_prompt

        attempt_id = test_session.attempt_id if test_session else None
        attempt_purpose = None
        if attempt_id:
            if attempt:
                attempt_purpose = attempt.purpose
                attempt.answers = serialize_answers(answers)
                attempt.formula_results = json_dumps(formula_results) if formula_results else None
                attempt.status = "completed"
                attempt.completed_at = msk_now()
                if attempt.purpose == "pre_dialogue" and topic_slug:
                    await complete_pre_test_claim(
                        session,
                        user_id=user_id,
                        topic_slug=topic_slug,
                        dialogue_id=dialogue_id,
                        test_set_id=test_session.test_set_id or 1,
                        attempt_id=attempt.id,
                    )

        if test_session:
            if attempt_purpose == "preview":
                await session.delete(test_session)
            else:
                test_session.formula_results = json_dumps(formula_results) if formula_results else None
                test_session.is_finished = True
        await session.commit()

    user_name = ((user.name or user.first_name) if user else "пользователь") or "пользователь"
    user_name = user_name.strip()
    profile_str = build_ai_user_profile_context(user)

    # Isolated Admin Preview completion semantics (P1-4)
    if attempt_purpose == "preview":
        lines = [f"📋 <b>Предпросмотр теста «{html.escape(test_set.title)}» завершён</b>\n"]
        if formula_results:
            lines.append("<b>Результаты расчёта формул:</b>")
            for var_name, val in formula_results.items():
                lines.append(f"• <code>{html.escape(str(var_name))}</code> = {html.escape(str(val))}")
            lines.append("")

        if separate_enabled:
            try:
                interp_system = result_prompt
                if profile_str:
                    interp_system = f"{result_prompt}\n\nПрофиль пользователя:\n{profile_str}"
                preview_interp = await get_standalone_ai_response(
                    interp_system,
                    "Интерпретируй результаты теста в режиме предпросмотра:\n\n" + prompt_payload,
                    bot=bot,
                    chat_id=user_id,
                    user_id=user_id,
                )
                safe_interp = html.escape(preview_interp)
                lines.append(f"<b>Интерпретация:</b>\n{safe_interp}")
            except Exception:
                lines.append("<i>(Интерпретация не сформирована)</i>")
        else:
            lines.append("✅ Все вопросы и ответы успешно пройдены в режиме предпросмотра.")

        result_text = "\n".join(lines)
        try:
            await loading.delete()
        except TelegramBadRequest:
            pass

        from crisis_safety import crisis_was_detected_in_turn, send_crisis_contacts_if_active
        crisis_detected_now = crisis_was_detected_in_turn(user_id)
        await _send_long(bot, user_id, result_text, parse_mode="HTML", include_finish_markup=not crisis_detected_now)
        if crisis_detected_now:
            await send_crisis_contacts_if_active(bot, user_id, user_id)
            await state.clear()
            return
        await state.clear()
        from test_set_admin_handlers import render_test_set_details
        class _PreviewCardEvent:
            async def answer(self, text, **kw):
                return await bot.send_message(user_id, text, **kw)
        await render_test_set_details(_PreviewCardEvent(), test_set.id)
        return

    preliminary = None
    try:
        async with ai_typing_action(bot, user_id):
            if separate_enabled:
                interp_system = result_prompt
                if profile_str:
                    interp_system = f"{result_prompt}\n\nПрофиль пользователя:\n{profile_str}"
                preliminary = await get_standalone_ai_response(
                    interp_system,
                    "Интерпретируй результаты теста. Не показывай технические имена переменных и формул, "
                    "не придумывай максимальные баллы, нормы или пороги.\n\n" + prompt_payload,
                    bot=bot,
                    chat_id=user_id,
                    user_id=user_id,
                )
            handoff = build_result_handoff_prompt(prompt_payload, preliminary, user_name, profile_context=profile_str)
            result_text = await get_ai_response_for_topic(
                user_id,
                handoff,
                user_name,
                topic_slug=topic_slug,
                dialogue_id=dialogue_id,
                bot=bot,
                chat_id=user_id,
            )
        async with async_session_maker() as session:
            session.add_all([
                DBMessage(
                    user_id=user_id,
                    dialogue_id=dialogue_id,
                    topic_slug=topic_slug,
                    role="user",
                    content=handoff,
                    message_kind="test_handoff",
                    test_attempt_id=attempt_id,
                ),
                DBMessage(
                    user_id=user_id,
                    dialogue_id=dialogue_id,
                    topic_slug=topic_slug,
                    role="assistant",
                    content=result_text,
                    message_kind="test_handoff",
                    test_attempt_id=attempt_id,
                ),
            ])
            await session.commit()
    except Exception:
        logger.exception("Universal test interpretation failed for user_id=%s", user_id)
        result_text = preliminary or "Не удалось сформировать интерпретацию. Ответы сохранены, попробуйте позже."

    try:
        await loading.delete()
    except TelegramBadRequest:
        pass
    from crisis_safety import crisis_was_detected_in_turn, is_crisis_active, send_crisis_contacts_if_active

    crisis_detected_now = crisis_was_detected_in_turn(user_id)
    async with async_session_maker() as session:
        crisis_active = await is_crisis_active(session, user_id)

    await _send_long(
        bot,
        user_id,
        result_text,
        include_finish_markup=not crisis_active,
    )
    if crisis_detected_now:
        await send_crisis_contacts_if_active(bot, user_id, user_id)
    if crisis_active:
        await state.clear()
        return

    # Resume topic lifecycle if pre_dialogue
    if attempt_purpose == "pre_dialogue" and topic_slug:
        from database import TopicDialogueConfig
        from ai_dialogue_service import deliver_prerecorded_first_message
        async with async_session_maker() as session:
            cfg = (await session.execute(
                select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == topic_slug)
            )).scalar_one_or_none()
            start_btn_enabled = cfg.start_dialogue_button_enabled if cfg else False
            prerecorded_text = cfg.prerecorded_first_message if cfg else None

        if start_btn_enabled:
            btn_text = (cfg.start_dialogue_button_text if cfg and cfg.start_dialogue_button_text else None) or "Начать диалог"
            keyboard = kb.topic_start_dialogue_keyboard(topic_slug, dialogue_id, btn_text)
            prompt = "Тестирование пройдено! Нажмите кнопку, чтобы начать диалог:"
            await bot.send_message(user_id, prompt, reply_markup=keyboard)
            await state.clear()
            return

        if prerecorded_text:
            async with async_session_maker() as session:
                await deliver_prerecorded_first_message(
                    bot, session, user_id, topic_slug, dialogue_id, prerecorded_text
                )

        await state.set_state("UserStates:chatting")
        await state.update_data(current_topic_slug=topic_slug, dialogue_id=dialogue_id)
        welcome_prompt = (cfg.welcome_text if cfg and cfg.welcome_text else None) or "🤖 Режим активирован. Напиши свой запрос."
        markup = await kb.ai_response_keyboard_for_topic(topic_slug, dialogue_id)
        sent_message = await bot.send_message(user_id, welcome_prompt, reply_markup=markup)
        if isinstance(getattr(sent_message, "message_id", None), int):
            await state.update_data(ai_last_response_message_id=sent_message.message_id)
        return

    await state.set_state("UserStates:chatting")


@router.callback_query(UniversalTestStates.answering, F.data.startswith("test_opt_"))
async def answer_test_option(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    try:
        next_index, total, answers = await _save_answer(callback.from_user.id, callback.data, is_text=False)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    if next_index < total:
        await _send_question(bot, callback.from_user.id, next_index, state=state)
    else:
        await _finish_test(bot, callback.from_user.id, state, answers)


@router.callback_query(UniversalTestStates.answering, F.data == "test_exit")
async def exit_universal_test(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Leave a questionnaire while keeping its persisted answers resumable."""
    user_id = callback.from_user.id
    async with async_session_maker() as session:
        test_session = await session.get(TestSession, user_id)
        if test_session and test_session.attempt_id:
            attempt = await session.get(TestAttempt, test_session.attempt_id)
            if attempt and attempt.status == "in_progress":
                attempt.answers = test_session.answers or "[]"
                await session.commit()

    await state.clear()
    await callback.answer()
    from handlers import send_main_menu
    await send_main_menu(callback, bot=bot)


@router.message(UniversalTestStates.answering, F.text)
async def answer_test_text(message: Message, state: FSMContext, bot: Bot):
    try:
        next_index, total, answers = await _save_answer(message.from_user.id, message.text, is_text=True)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    if next_index < total:
        await _send_question(bot, message.from_user.id, next_index, state=state)
    else:
        await _finish_test(bot, message.from_user.id, state, answers)


@router.message(Command("test"))
async def preview_test(message: Message, state: FSMContext, bot: Bot):
    if not await _is_admin(message.from_user.id):
        return
    await start_universal_test(bot, message.from_user.id, state, admin_preview=True)


def _mode_label(mode: str) -> str:
    return {"all": "все вопросы и ответы", "selected": "только выбранные переменные", "formulas": "только результаты формул"}.get(mode, mode)


async def _show_admin_menu(event: CallbackQuery | Message) -> None:
    async with async_session_maker() as session:
        config = await _config(session)
        count = await session.scalar(select(func.count(TestQuestion.id))) or 0
        formulas = json_loads(config.formulas_json, [])
    text = (
        "<b>Универсальная система тестов</b>\n\n"
        f"Статус: <b>{'включена' if config.is_enabled else 'выключена'}</b>\n"
        f"Вопросов: <b>{count}</b>\n"
        f"Прогресс: <b>{'да' if config.show_progress else 'нет'}</b>\n"
        f"Формулы: <b>{'включены' if config.formulas_enabled else 'выключены'}</b> ({len(formulas)})\n"
        f"Отдельная интерпретация: <b>{'да' if config.separate_result_prompt_enabled else 'нет'}</b>\n"
        f"Передавать: <b>{html.escape(_mode_label(config.interpretation_input_mode))}</b>\n\n"
        "Команда запуска из ответа ИИ: <code>[START_TEST]</code>"
    )
    markup = kb.admin_test_system_keyboard(config, bool(formulas), count)
    if isinstance(event, CallbackQuery):
        await event.answer()
        try:
            await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except TelegramBadRequest:
            await event.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "admin_test_system")
async def admin_test_menu(callback: CallbackQuery):
    if await _is_admin(callback.from_user.id):
        await _show_admin_menu(callback)


async def _toggle(callback: CallbackQuery, attr: str) -> None:
    if not await _is_admin(callback.from_user.id):
        return
    async with async_session_maker() as session:
        config = await _config(session)
        if attr == "formulas_enabled" and not config.formulas_json:
            await callback.answer("Сначала загрузите формулы.", show_alert=True)
            return
        if attr == "separate_result_prompt_enabled" and not config.result_system_prompt:
            await callback.answer("Сначала задайте отдельный промпт.", show_alert=True)
            return
        setattr(config, attr, not bool(getattr(config, attr)))
        if attr == "formulas_enabled" and not config.formulas_enabled and config.interpretation_input_mode == "formulas":
            config.interpretation_input_mode = "all"
        await session.commit()
    await _show_admin_menu(callback)


@router.callback_query(F.data == "admin_test_toggle_enabled")
async def toggle_enabled(callback: CallbackQuery):
    await _toggle(callback, "is_enabled")


@router.callback_query(F.data == "admin_test_toggle_progress")
async def toggle_progress(callback: CallbackQuery):
    await _toggle(callback, "show_progress")


@router.callback_query(F.data == "admin_test_toggle_formulas")
async def toggle_formulas(callback: CallbackQuery):
    await _toggle(callback, "formulas_enabled")


@router.callback_query(F.data == "admin_test_toggle_separate")
async def toggle_separate(callback: CallbackQuery):
    await _toggle(callback, "separate_result_prompt_enabled")


@router.callback_query(F.data == "admin_test_cycle_mode")
async def cycle_mode(callback: CallbackQuery):
    if not await _is_admin(callback.from_user.id):
        return
    async with async_session_maker() as session:
        config = await _config(session)
        modes = ["all", "selected"] + (["formulas"] if config.formulas_json else [])
        current = modes.index(config.interpretation_input_mode) if config.interpretation_input_mode in modes else 0
        config.interpretation_input_mode = modes[(current + 1) % len(modes)]
        await session.commit()
    await _show_admin_menu(callback)


async def _ask_for_file(callback: CallbackQuery, state: FSMContext, target_state: State, title: str) -> None:
    if not await _is_admin(callback.from_user.id):
        return
    await callback.answer()
    await state.set_state(target_state)
    await callback.message.answer(
        f"Отправьте {title}. Поддерживаются <b>.xlsx</b>, <b>.csv</b> и <b>.txt</b>. "
        "Данные заменятся только после полной проверки.",
        reply_markup=kb.admin_test_cancel_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_test_upload_questions")
async def ask_questions(callback: CallbackQuery, state: FSMContext):
    await _ask_for_file(callback, state, TestAdminStates.upload_questions, "файл с вопросами")


@router.callback_query(F.data == "admin_test_upload_formulas")
async def ask_formulas(callback: CallbackQuery, state: FSMContext):
    await _ask_for_file(callback, state, TestAdminStates.upload_formulas, "файл с формулами")


async def _download_document(message: Message, bot: Bot) -> tuple[io.BytesIO, str]:
    if not message.document:
        raise ValueError("Отправьте файл документом.")
    filename = message.document.file_name or "upload"
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in {"xlsx", "csv", "txt"}:
        raise ValueError("Поддерживаются только .xlsx, .csv и .txt.")
    buffer = io.BytesIO()
    await bot.download(message.document, destination=buffer)
    buffer.seek(0)
    return buffer, filename


def _question_preview(data: dict):
    return type("QuestionPreview", (), data)()


@router.message(TestAdminStates.upload_questions)
async def receive_questions(message: Message, state: FSMContext, bot: Bot):
    if not await _is_admin(message.from_user.id):
        return
    try:
        source, filename = await _download_document(message, bot)
        parsed = await parse_questions_file(source, filename)
        rows, formulas = parsed["questions"], parsed["formulas"]
        if not rows:
            raise ValueError("В файле не найдено ни одного вопроса.")
        async with async_session_maker() as session:
            state_data = await state.get_data()
            target_set_id = state_data.get("test_set_id", 1)
            target_ts = await _get_test_set(session, target_set_id)
            config = await _config(session)

            previews = [_question_preview(row) for row in rows]
            errors = validate_test_definition(previews, formulas)
            if errors:
                raise ValueError("\n".join(errors[:12]))
            old_formulas_disabled = False
            current_formulas_json = target_ts.formulas_json if target_ts else config.formulas_json
            if not formulas:
                old_formulas = json_loads(current_formulas_json, [])
                old_formulas_disabled = bool(old_formulas and validate_test_definition(previews, old_formulas))
            await session.execute(delete(TestQuestion).where(TestQuestion.test_set_id == target_set_id))
            session.add_all([TestQuestion(sort_order=index, test_set_id=target_set_id, **row) for index, row in enumerate(rows)])
            if target_ts:
                if formulas:
                    target_ts.formulas_json = json_dumps(formulas)
                    target_ts.formulas_enabled = True
                elif old_formulas_disabled:
                    target_ts.formulas_enabled = False
                    if target_ts.interpretation_input_mode == "formulas":
                        target_ts.interpretation_input_mode = "all"
            await session.commit()
        await state.clear()
        result = f"Загружено вопросов: {len(rows)}. Формул в этом файле: {len(formulas)}."
        if old_formulas_disabled:
            result += " Старые формулы несовместимы с новыми вопросами и поэтому выключены."
        await message.answer(result)
        await _show_admin_menu(message)
    except Exception as exc:
        await message.answer(f"Файл не принят:\n{html.escape(str(exc))}", reply_markup=kb.admin_test_cancel_keyboard())


@router.message(TestAdminStates.upload_formulas)
async def receive_formulas(message: Message, state: FSMContext, bot: Bot):
    if not await _is_admin(message.from_user.id):
        return
    try:
        source, filename = await _download_document(message, bot)
        formulas = await parse_formulas_file(source, filename)
        if not formulas:
            raise ValueError("В файле не найдено ни одной формулы.")
        async with async_session_maker() as session:
            state_data = await state.get_data()
            target_set_id = state_data.get("test_set_id", 1)
            target_ts = await _get_test_set(session, target_set_id)
            config = await _config(session)
            questions = await _questions(session, target_set_id)
            errors = validate_test_definition(questions, formulas)
            if errors:
                raise ValueError("\n".join(errors[:12]))
            if target_ts:
                target_ts.formulas_json = json_dumps(formulas)
                target_ts.formulas_enabled = True
            await session.commit()
        await state.clear()
        await message.answer(f"Загружено и включено формул: {len(formulas)}.")
        await _show_admin_menu(message)
    except Exception as exc:
        await message.answer(f"Файл не принят:\n{html.escape(str(exc))}", reply_markup=kb.admin_test_cancel_keyboard())


@router.callback_query(F.data == "admin_test_set_prompt")
async def ask_result_prompt(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(callback.from_user.id):
        return
    await callback.answer()
    await state.set_state(TestAdminStates.set_result_prompt)
    await callback.message.answer(
        "Отправьте текст отдельного системного промпта или файл .txt/.md.",
        reply_markup=kb.admin_test_cancel_keyboard(),
    )


@router.message(TestAdminStates.set_result_prompt)
async def receive_result_prompt(message: Message, state: FSMContext, bot: Bot):
    if not await _is_admin(message.from_user.id):
        return
    prompt = (message.text or "").strip()
    if message.document:
        filename = message.document.file_name or ""
        if not filename.lower().endswith((".txt", ".md")):
            await message.answer("Для промпта поддерживаются только .txt и .md.")
            return
        buffer = io.BytesIO()
        await bot.download(message.document, destination=buffer)
        try:
            prompt = buffer.getvalue().decode("utf-8-sig").strip()
        except UnicodeDecodeError:
            prompt = buffer.getvalue().decode("cp1251").strip()
    if not prompt:
        await message.answer("Промпт пуст. Отправьте текст или файл.")
        return
    async with async_session_maker() as session:
        config = await _config(session)
        config.result_system_prompt = prompt
        config.separate_result_prompt_enabled = True
        await session.commit()
    await state.clear()
    await message.answer("Отдельный промпт сохранён и включён.")
    await _show_admin_menu(message)


@router.callback_query(F.data == "admin_test_set_selected")
async def ask_selected(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(callback.from_user.id):
        return
    await callback.answer()
    async with async_session_maker() as session:
        questions = await _questions(session)
    variables = ", ".join((q.variable_name or f"answer_{i + 1:02d}") for i, q in enumerate(questions))
    await state.set_state(TestAdminStates.set_selected_variables)
    await callback.message.answer(
        "Отправьте через запятую переменные, которые нужно передавать в интерпретацию.\n\n"
        f"Доступны: <code>{html.escape(variables)}</code>",
        reply_markup=kb.admin_test_cancel_keyboard(),
        parse_mode="HTML",
    )


@router.message(TestAdminStates.set_selected_variables, F.text)
async def receive_selected(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    requested = [item.strip() for item in re.split(r"[,;\n]", message.text) if item.strip()]
    async with async_session_maker() as session:
        config = await _config(session)
        questions = await _questions(session)
        available = {(q.variable_name or f"answer_{i + 1:02d}") for i, q in enumerate(questions)}
        unknown = [item for item in requested if item not in available]
        if unknown:
            await message.answer("Неизвестные переменные: " + ", ".join(unknown))
            return
        config.interpretation_selected_variables = json_dumps(requested)
        config.interpretation_input_mode = "selected"
        await session.commit()
    await state.clear()
    await message.answer("Список переменных сохранён.")
    await _show_admin_menu(message)


@router.callback_query(F.data == "admin_test_preview")
async def admin_preview(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not await _is_admin(callback.from_user.id):
        return
    await callback.answer()
    await start_universal_test(bot, callback.from_user.id, state, admin_preview=True)


@router.callback_query(F.data == "admin_test_cancel")
async def cancel_admin_test_action(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(callback.from_user.id):
        return
    await state.clear()
    await _show_admin_menu(callback)


@router.callback_query(F.data == "admin_test_template")
async def download_template(callback: CallbackQuery):
    if not await _is_admin(callback.from_user.id):
        return
    await callback.answer()
    content = (
        "Номер вопроса|Вопрос|Комментарий|Имя переменной|Свой ответ|Вывод кнопок|Вариант ответа 1|Вариант ответа 2|Числовое значение 1|Числовое значение 2\n"
        "1|Насколько вы довольны своим состоянием?||state_score||1|Низко|Высоко|1|2\n"
        "2|Опишите своё состояние|Можно ответить несколькими предложениями|state_text|ДА|||||\n"
        "[FORMULAS]\n"
        "Переменная|Формула\n"
        "total_score|state_score\n"
    ).encode("utf-8-sig")
    await callback.message.answer_document(
        BufferedInputFile(content, filename="test_template.csv"),
        caption="Шаблон теста. Формулы можно оставить в этом файле или загрузить отдельно.",
    )
