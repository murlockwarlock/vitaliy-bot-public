import asyncio
import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import (
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import DATABASE_URL
from database import (
    CANONICAL_QUESTIONS,
    JournalAiOperation,
    JournalAnswer,
    JournalConfig,
    JournalEntry,
    JournalQuestion,
    JournalReward,
    JournalSettings,
    PaymentConfig,
    Topic,
    User,
    UserSubscription,
    async_session_maker,
)
from time_utils import msk_now
from universal_tests import get_answer_options, get_question_type

logger = logging.getLogger("journal_service")

TARGET_REWARD_DAYS = 21
REWARD_SUBSCRIPTION_DAYS = 7

JOURNAL_DEFAULT_SYSTEM_PROMPT = (
    "Ты — бережный, поддерживающий ассистент по саморефлексии и ведению личного дневника. "
    "Твоя цель — помочь пользователю осознать свой опыт, проявить эмпатию и заботу, "
    "опираясь исключительно на предоставленные данные Дневника. "
    "Категорически запрещено: ставить медицинские или психологические диагнозы, "
    "давать категоричные директивные советы или выдумывать факты, которых нет в данных Дневника. "
    "Ответ должен быть кратким, чутким, конструктивным и вдохновляющим."
)


class InsufficientTokensError(Exception):
    """Raised when user balance is insufficient for a billable journal AI operation."""
    pass


async def get_or_create_journal_settings(session: AsyncSession, user_id: int) -> JournalSettings:
    """Retrieve or create default JournalSettings for user."""
    settings = await session.get(JournalSettings, user_id)
    if not settings:
        settings = JournalSettings(
            user_id=user_id,
            reminders_enabled=False,
            reminder_time=None,
        )
        session.add(settings)
        await session.flush()
    return settings


async def get_or_create_journal_config(session: AsyncSession) -> JournalConfig:
    """Retrieve or create singleton JournalConfig row (id=1)."""
    cfg = await session.get(JournalConfig, 1)
    if not cfg:
        cfg = JournalConfig(id=1, points_per_reflection=10, questions_seeded=False)
        session.add(cfg)
        await session.flush()
    return cfg


async def calculate_journal_streak(session: AsyncSession, user_id: int) -> int:
    """Calculate current consecutive days streak for completed reflections."""
    stmt = (
        select(JournalEntry.reflection_date)
        .where(JournalEntry.user_id == user_id, JournalEntry.status == "completed")
        .distinct()
        .order_by(JournalEntry.reflection_date.desc())
    )
    result = await session.scalars(stmt)
    dates_list = result.all()
    if not dates_list:
        return 0

    dates_set = set(dates_list)
    today = msk_now().date()
    yesterday = today - timedelta(days=1)

    latest = dates_list[0]
    if latest < yesterday:
        # Missed at least yesterday and today -> streak broken
        return 0

    # Start counting streak
    current_check = today if latest == today else yesterday
    streak = 0
    while current_check in dates_set:
        streak += 1
        current_check -= timedelta(days=1)

    return streak


async def get_journal_completed_days_count(session: AsyncSession, user_id: int) -> int:
    """Count distinct calendar dates with completed reflections for user."""
    stmt = (
        select(func.count(func.distinct(JournalEntry.reflection_date)))
        .where(JournalEntry.user_id == user_id, JournalEntry.status == "completed")
    )
    count = await session.scalar(stmt)
    return count or 0


async def get_journal_stats(session: AsyncSession, user_id: int) -> dict[str, Any]:
    """Gather user stats for journal dashboard: streak, 21-day progress, points, reminder string."""
    streak = await calculate_journal_streak(session, user_id)
    completed_days = await get_journal_completed_days_count(session, user_id)

    user = await session.get(User, user_id)
    points = user.journal_points if user else 0

    settings = await get_or_create_journal_settings(session, user_id)
    if settings.reminders_enabled and settings.reminder_time:
        reminder_str = f"{settings.reminder_time} МСК"
    else:
        reminder_str = "отключено"

    return {
        "streak": streak,
        "completed_days": completed_days,
        "target_progress": min(completed_days, TARGET_REWARD_DAYS),
        "target_days": TARGET_REWARD_DAYS,
        "points": points,
        "reminder_str": reminder_str,
    }


async def build_questions_snapshot(session: AsyncSession) -> list[dict[str, Any]]:
    """Build a deterministic snapshot of active questions ordered by order_num ASC, id ASC."""
    stmt = (
        select(JournalQuestion)
        .where(JournalQuestion.is_active == True)
        .order_by(JournalQuestion.order_num.asc(), JournalQuestion.id.asc())
    )
    active_questions = (await session.scalars(stmt)).all()
    snapshot = []
    for idx, q in enumerate(active_questions, start=1):
        snapshot.append({
            "question_id": q.id,
            "position": idx,
            "text": q.text,
            "question_type": get_question_type(q),
            "allow_custom_answer": bool(q.allow_custom_answer),
            "options": [
                {"text": option.text, "value": option.value}
                for option in get_answer_options(q)
            ],
        })
    return snapshot


async def grant_subscription_bonus_days(
    session: AsyncSession,
    user_id: int,
    days: int = REWARD_SUBSCRIPTION_DAYS,
    source: str = "Journal 21-Day Reward",
) -> bool:
    """Safely extend or grant user subscription with with_for_update locking."""
    now = msk_now()
    try:
        user_sub = await session.scalar(
            select(UserSubscription)
            .where(UserSubscription.user_id == user_id)
            .with_for_update()
        )
    except Exception:
        # SQLite fallback where SELECT FOR UPDATE is not supported
        user_sub = await session.scalar(
            select(UserSubscription).where(UserSubscription.user_id == user_id)
        )

    if user_sub and user_sub.end_date and user_sub.end_date > now:
        user_sub.end_date += timedelta(days=days)
        new_end = user_sub.end_date
    elif user_sub:
        user_sub.plan_id = None
        user_sub.start_date = now
        user_sub.end_date = now + timedelta(days=days)
        user_sub.payment_provider = source
        user_sub.auto_renewal = False
        user_sub.payment_attempt_count = 0
        new_end = user_sub.end_date
    else:
        new_end = now + timedelta(days=days)
        session.add(UserSubscription(
            user_id=user_id,
            plan_id=None,
            start_date=now,
            end_date=new_end,
            auto_renewal=False,
            payment_provider=source,
            payment_attempt_count=0,
            discount_percent=0,
        ))

    user = await session.get(User, user_id)
    if user:
        if user.subscription_end and user.subscription_end > now:
            user.subscription_end += timedelta(days=days)
        else:
            user.subscription_end = new_end

    await session.flush()
    return True


async def execute_transaction_a(
    session: AsyncSession,
    entry_id: int,
    user_id: int,
    final_answer_text: str | None = None,
    final_question_snapshot: dict[str, Any] | None = None,
    final_is_skipped: bool = False,
) -> dict[str, Any]:
    """
    Execute Transaction A in ONE outer DB transaction:
    1. Persist final JournalAnswer if provided and not yet persisted.
    2. Mark JournalEntry status = 'completed', completed_at = now.
    3. Explicitly execute await session.flush() BEFORE milestone count!
    4. Atomically claim reflection points (JournalEntry points_awarded, User.journal_points).
    5. Count completed reflection dates.
    6. If count >= 21, attempt atomic savepoint insert of JournalReward and grant subscription days.
    7. Commit transaction.
    """
    entry = await session.get(JournalEntry, entry_id)
    if not entry or entry.user_id != user_id:
        raise ValueError("Запись не найдена или принадлежит другому пользователю.")

    now = msk_now()

    # 1. Persist final answer if passed
    if (final_answer_text is not None or final_is_skipped) and final_question_snapshot is not None:
        pos = final_question_snapshot.get("position", 1)
        qid = final_question_snapshot.get("question_id")
        valid_qid = None
        if qid:
            q_exists = await session.get(JournalQuestion, qid)
            if q_exists:
                valid_qid = qid

        answer = JournalAnswer(
            entry_id=entry_id,
            question_id=valid_qid,
            position=pos,
            question_text_snapshot=final_question_snapshot.get("text", ""),
            answer_text=(final_answer_text or "").strip(),
            is_skipped=final_is_skipped,
        )
        try:
            async with session.begin_nested():
                session.add(answer)
                await session.flush()
        except IntegrityError:
            # Concurrent duplicate already inserted the final position.
            # Do NOT overwrite the first stored answer.
            pass

    # 2. Mark completed (preserve completed_at if already completed)
    if entry.status != "completed":
        entry.status = "completed"
        entry.completed_at = now
    elif not entry.completed_at:
        entry.completed_at = now

    # CRITICAL: flush so the completed status is visible to the milestone count query!
    await session.flush()

    # 3. Atomic claim of reflection points
    cfg = await session.get(JournalConfig, 1)
    pts_to_award = cfg.points_per_reflection if cfg else 10

    claim_stmt = (
        update(JournalEntry)
        .where(
            JournalEntry.id == entry_id,
            JournalEntry.user_id == user_id,
            JournalEntry.points_awarded_at == None,
        )
        .values(
            points_awarded=pts_to_award,
            points_awarded_at=now,
        )
        .returning(JournalEntry.points_awarded)
    )
    awarded_amount = (await session.execute(claim_stmt)).scalar()
    if awarded_amount is not None and awarded_amount > 0:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(journal_points=User.journal_points + awarded_amount)
        )

    # 4. Milestone count query
    count_stmt = (
        select(func.count(func.distinct(JournalEntry.reflection_date)))
        .where(
            JournalEntry.user_id == user_id,
            JournalEntry.status == "completed",
        )
    )
    completed_dates_count = (await session.scalar(count_stmt)) or 0

    # 5. Milestone reward check (>= 21 days)
    reward_claimed = False
    if completed_dates_count >= TARGET_REWARD_DAYS:
        try:
            async with session.begin_nested():
                reward = JournalReward(
                    user_id=user_id,
                    target_days=TARGET_REWARD_DAYS,
                    cycle_number=1,
                    days_granted=REWARD_SUBSCRIPTION_DAYS,
                    granted_at=now,
                )
                session.add(reward)
                await session.flush()
                reward_claimed = True
        except IntegrityError:
            reward_claimed = False

        if reward_claimed:
            await grant_subscription_bonus_days(
                session,
                user_id=user_id,
                days=REWARD_SUBSCRIPTION_DAYS,
                source="Journal 21-Day Reward",
            )

    # 6. Single unified commit
    await session.commit()

    return {
        "entry": entry,
        "points_awarded": awarded_amount or 0,
        "reward_granted": reward_claimed,
        "completed_dates_count": completed_dates_count,
    }


async def initiate_journal_ai_operation(
    session: AsyncSession,
    operation_key: str,
    user_id: int,
    operation_type: str,
    entry_id: int | None = None,
) -> tuple[JournalAiOperation, bool]:
    """
    Initiate or reuse a JournalAiOperation:
    - Checks duplicate operation_key;
    - If running and leased, or succeeded, returns (op, False) [no bill, no AI call];
    - If new, checks if billable (token mode & paid topic & non-admin);
    - If billable, atomically CAS decrements balance and sets charged_at in ONE transaction;
    - Returns (op, True) if caller won execution rights.
    """
    now = msk_now()
    existing_op = await session.scalar(
        select(JournalAiOperation).where(JournalAiOperation.operation_key == operation_key)
    )
    if existing_op:
        if existing_op.status == "running" and existing_op.lease_until and existing_op.lease_until > now:
            return existing_op, False
        if existing_op.status == "succeeded":
            return existing_op, False
        if existing_op.status == "failed":
            return existing_op, False
        if existing_op.status == "running" and existing_op.lease_until and existing_op.lease_until <= now:
            # Stale op waiting for background recovery
            return existing_op, False

    user = await session.get(User, user_id)
    if not user:
        raise ValueError("Пользователь не найден.")

    topic = await session.scalar(select(Topic).where(Topic.slug == "journal"))
    payment_cfg = await session.get(PaymentConfig, 1)

    # Billable check
    is_admin = bool(user.is_admin)
    is_topic_paid = bool(topic.is_paid) if topic else True
    payment_mode = payment_cfg.mode if payment_cfg else "free"

    is_billable = (
        not is_admin
        and is_topic_paid
        and (payment_mode == "token")
    )

    owner_token = str(uuid.uuid4())
    lease_until = now + timedelta(seconds=120)

    cost = float(payment_cfg.cost_per_message) if (is_billable and payment_cfg and payment_cfg.cost_per_message is not None) else 0.0

    try:
        async with session.begin_nested():
            op = JournalAiOperation(
                operation_key=operation_key,
                user_id=user_id,
                entry_id=entry_id,
                operation_type=operation_type,
                status="running",
                token_cost=cost if is_billable else 0.0,
                owner_token=owner_token,
                started_at=now,
                lease_until=lease_until,
                charged_at=None,
            )
            session.add(op)
            await session.flush()
    except IntegrityError:
        # Duplicate key contention: another process inserted first
        existing = await session.scalar(
            select(JournalAiOperation).where(JournalAiOperation.operation_key == operation_key)
        )
        return existing, False

    if not is_billable:
        await session.commit()
        return op, True

    # Winner performs atomic token CAS debit
    debit_stmt = (
        update(User)
        .where(User.id == user_id, User.balance >= cost)
        .values(balance=User.balance - cost)
        .returning(User.balance)
    )
    new_balance = (await session.execute(debit_stmt)).scalar()
    if new_balance is not None:
        op.charged_at = now
        await session.commit()
        return op, True
    else:
        # Insufficient tokens: record failed audit op without charging
        op.status = "failed"
        op.charged_at = None
        op.completed_at = now
        op.owner_token = None
        op.lease_until = None
        await session.commit()
        raise InsufficientTokensError("Недостаточно токенов на балансе для генерации.")


async def renew_journal_ai_operation_lease(
    operation_id: int,
    owner_token: str,
    lease_duration_seconds: int = 120,
) -> bool:
    """Heartbeat helper executed in an ISOLATED AsyncSession to extend lease."""
    now = msk_now()
    async with async_session_maker() as heartbeat_session:
        stmt = (
            update(JournalAiOperation)
            .where(
                JournalAiOperation.id == operation_id,
                JournalAiOperation.owner_token == owner_token,
                JournalAiOperation.status == "running",
            )
            .values(lease_until=now + timedelta(seconds=lease_duration_seconds))
            .returning(JournalAiOperation.id)
        )
        res = (await heartbeat_session.execute(stmt)).scalar()
        await heartbeat_session.commit()
        return res is not None


async def run_lease_heartbeat(
    operation_id: int,
    owner_token: str,
    interval_seconds: int = 30,
    lease_duration_seconds: int = 120,
) -> None:
    """Background heartbeat loop while AI call is executing."""
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            renewed = await renew_journal_ai_operation_lease(
                operation_id, owner_token, lease_duration_seconds
            )
            if not renewed:
                logger.warning("Heartbeat lease renewal failed for op %s; stopping.", operation_id)
                break
    except asyncio.CancelledError:
        pass


async def finalize_journal_ai_operation_success(
    session: AsyncSession,
    operation_id: int,
    owner_token: str,
    result_payload: dict[str, Any] | str,
) -> bool:
    """
    Finalize successful AI operation with Owner-Token CAS:
    - Path A (daily summary): updates JournalEntry fields and op status='succeeded'.
    - Path B (dynamics): saves result_text in JournalAiOperation.
    Returns True if CAS was won.
    """
    now = msk_now()
    op = await session.get(JournalAiOperation, operation_id)
    if not op or op.owner_token != owner_token or op.status != "running":
        return False

    if isinstance(result_payload, dict):
        # Path A: daily_summary or summary_retry
        cas_stmt = (
            update(JournalAiOperation)
            .where(
                JournalAiOperation.id == operation_id,
                JournalAiOperation.owner_token == owner_token,
                JournalAiOperation.status == "running",
            )
            .values(
                status="succeeded",
                completed_at=now,
                lease_until=None,
            )
            .returning(JournalAiOperation.id, JournalAiOperation.entry_id)
        )
        res = (await session.execute(cas_stmt)).fetchone()
        if not res:
            await session.rollback()
            return False

        _, entry_id = res
        if entry_id:
            entry = await session.get(JournalEntry, entry_id)
            if entry:
                entry.summary_text = result_payload.get("summary_text")
                entry.encouraging_message = result_payload.get("encouraging_message")
                entry.tomorrow_focus = result_payload.get("tomorrow_focus")
                entry.metadata_json = result_payload.get("metadata_json")

        await session.commit()
        return True
    else:
        # Path B: dynamics_7 or dynamics_21
        cas_stmt = (
            update(JournalAiOperation)
            .where(
                JournalAiOperation.id == operation_id,
                JournalAiOperation.owner_token == owner_token,
                JournalAiOperation.status == "running",
            )
            .values(
                status="succeeded",
                completed_at=now,
                lease_until=None,
                result_text=str(result_payload),
            )
            .returning(JournalAiOperation.id)
        )
        won_id = (await session.execute(cas_stmt)).scalar()
        if not won_id:
            await session.rollback()
            return False

        await session.commit()
        return True


async def finalize_journal_ai_operation_failure(
    session: AsyncSession,
    operation_id: int,
    owner_token: str | None = None,
) -> bool:
    """
    Finalize failed AI operation with Owner CAS and exactly-once refund.
    Returns True if CAS was won.
    """
    now = msk_now()
    where_clause = [
        JournalAiOperation.id == operation_id,
        JournalAiOperation.status == "running",
    ]
    if owner_token is not None:
        where_clause.append(JournalAiOperation.owner_token == owner_token)

    cas_stmt = (
        update(JournalAiOperation)
        .where(*where_clause)
        .values(
            status="failed",
            completed_at=now,
            lease_until=None,
        )
        .returning(JournalAiOperation.id)
    )
    won = (await session.execute(cas_stmt)).scalar()
    if not won:
        return False

    # Perform atomic refund if charged and not yet refunded
    refund_stmt = (
        update(JournalAiOperation)
        .where(
            JournalAiOperation.id == operation_id,
            JournalAiOperation.charged_at != None,
            JournalAiOperation.refunded_at == None,
        )
        .values(refunded_at=now)
        .returning(JournalAiOperation.token_cost, JournalAiOperation.user_id)
    )
    refund_row = (await session.execute(refund_stmt)).fetchone()
    if refund_row:
        cost, uid = refund_row
        if cost and cost > 0:
            await session.execute(
                update(User)
                .where(User.id == uid)
                .values(balance=User.balance + cost)
            )

    await session.commit()
    return True


async def recover_stale_journal_ai_operations() -> int:
    """
    Worker task: recover expired running operations whose lease has expired without finishing.
    Guarantees exactly-once refund and never auto-invokes AI again.
    """
    recovered_count = 0
    now = msk_now()
    async with async_session_maker() as session:
        stale_ops = (await session.execute(
            select(JournalAiOperation.id).where(
                JournalAiOperation.status == "running",
                JournalAiOperation.lease_until != None,
                JournalAiOperation.lease_until <= now,
            )
        )).scalars().all()

        for op_id in stale_ops:
            cas_stmt = (
                update(JournalAiOperation)
                .where(
                    JournalAiOperation.id == op_id,
                    JournalAiOperation.status == "running",
                    JournalAiOperation.lease_until <= now,
                )
                .values(
                    status="failed",
                    completed_at=now,
                    lease_until=None,
                )
                .returning(
                    JournalAiOperation.id,
                    JournalAiOperation.user_id,
                    JournalAiOperation.token_cost,
                    JournalAiOperation.charged_at,
                    JournalAiOperation.refunded_at,
                )
            )
            res = (await session.execute(cas_stmt)).fetchone()
            if not res:
                continue

            recovered_count += 1
            _, uid, token_cost, charged_at, refunded_at = res
            if charged_at is not None and refunded_at is None and token_cost > 0:
                refund_stmt = (
                    update(JournalAiOperation)
                    .where(
                        JournalAiOperation.id == op_id,
                        JournalAiOperation.charged_at != None,
                        JournalAiOperation.refunded_at == None,
                    )
                    .values(refunded_at=now)
                    .returning(JournalAiOperation.token_cost)
                )
                refund_won = (await session.execute(refund_stmt)).scalar()
                if refund_won:
                    await session.execute(
                        update(User)
                        .where(User.id == uid)
                        .values(balance=User.balance + token_cost)
                    )
            await session.commit()

    return recovered_count


async def build_journal_daily_summary_context(
    session: AsyncSession,
    entry_id: int,
    user_id: int,
) -> tuple[str, str]:
    """
    Build strictly bounded context for daily summary generation:
    1. CURRENT ENTRY:
       - Loads completed entry owned by user;
       - Loads journal_answers WHERE entry_id = :id ORDER BY position ASC;
       - Passes pairs: question_text_snapshot and answer_text.
    2. PREVIOUS JOURNAL CONTEXT:
       - Loads up to 7 previous entries WHERE user_id = :uid AND status = 'completed' AND reflection_date < current_date ORDER BY reflection_date DESC LIMIT 7;
       - Compact fields only: reflection_date, summary_text, metadata_json.
       - Raw answers from previous days are strictly excluded.
       - Older entries without usable summary are skipped.
    3. FIRST ENTRY:
       - If no usable previous entries exist, prompt notes this is the beginning of observation history.
    4. STRICT EXCLUSION:
       - No messages table, no generic topics, no external dialogues.
    Returns (user_prompt_content, system_prompt).
    """
    entry = await session.get(JournalEntry, entry_id)
    if not entry or entry.user_id != user_id:
        raise ValueError("Запись не найдена или не принадлежит пользователю.")
    if entry.status != "completed":
        raise ValueError("Запись ещё не завершена.")

    answers_stmt = (
        select(JournalAnswer)
        .where(JournalAnswer.entry_id == entry_id)
        .order_by(JournalAnswer.position.asc())
    )
    answers = (await session.scalars(answers_stmt)).all()

    current_qa_lines = []
    skipped_positions = []
    for ans in answers:
        if ans.is_skipped:
            skipped_positions.append(str(ans.position))
            continue
        if ans.answer_text and ans.answer_text.strip():
            current_qa_lines.append(
                f"Вопрос {ans.position}: {ans.question_text_snapshot}\nОтвет: {ans.answer_text}"
            )
    current_qa_block = "\n\n".join(current_qa_lines) or "Нет сохранённых ответов на вопросы этого дня."
    skipped_block = (
        "Пропущенные вопросы (это не ответы пользователя): " + ", ".join(skipped_positions)
        if skipped_positions
        else "Пропущенных вопросов нет."
    )

    # Previous entries
    prev_stmt = (
        select(JournalEntry)
        .where(
            JournalEntry.user_id == user_id,
            JournalEntry.status == "completed",
            JournalEntry.reflection_date < entry.reflection_date,
        )
        .order_by(JournalEntry.reflection_date.desc())
        .limit(7)
    )
    prev_entries = (await session.scalars(prev_stmt)).all()

    usable_prev = []
    for pe in prev_entries:
        if pe.summary_text and pe.summary_text.strip():
            compact_entry = f"Дата: {pe.reflection_date}\nИтог дня: {pe.summary_text.strip()}"
            if pe.metadata_json and pe.metadata_json.strip() and pe.metadata_json != "{}":
                compact_entry += f"\nМетаданные: {pe.metadata_json.strip()}"
            usable_prev.append(compact_entry)

    if usable_prev:
        prev_block = (
            "ПРЕДЫДУЩИЕ ЗАПИСИ ДНЕВНИКА (до 7 прошлых дней, только краткие итоги):\n"
            + "\n---\n".join(usable_prev)
            + "\n\nИнструкция по динамике: проводи сравнение с предыдущими днями только при наличии явных свидетельств в переданных прошлых итогах."
        )
    else:
        prev_block = (
            "КОНТЕКСТ ИСТОРИИ: Это первая завершенная запись пользователя в Дневнике (начало истории наблюдений). "
            "Категорически запрещено придумывать прошлую динамику, изменения или сравнения с несуществующими днями."
        )

    user_prompt = (
        f"ОТВЕТЫ ПОЛЬЗОВАТЕЛЯ ЗА СЕГОДНЯ ({entry.reflection_date}):\n\n"
        f"{current_qa_block}\n\n"
        f"{skipped_block}\n\n"
        "Не приписывай пользователю ответы на пропущенные вопросы и не заполняй их догадками.\n\n"
        f"{prev_block}\n\n"
        "Сформируй бережный, чуткий ответ строго по следующему формату разметки:\n\n"
        "[[SUMMARY]]\n"
        "<Глубокий анализ сегодняшнего дня: что особенно заметно в переживаниях и опыте, ключевые смыслы>\n\n"
        "[[ENCOURAGING]]\n"
        "<Краткое теплое поддерживающее послание на завтра>\n\n"
        "[[FOCUS]]\n"
        "<Один конкретный бережный фокус внимания на завтрашний день>\n\n"
        "[[METADATA_JSON]]\n"
        "{\n"
        '  "dominant_emotions": ["..."],\n'
        '  "gratitude": ["..."],\n'
        '  "important_events": ["..."],\n'
        '  "lessons": ["..."],\n'
        '  "keep": ["..."],\n'
        '  "let_go": ["..."],\n'
        '  "tomorrow_intentions": ["..."]\n'
        "}\n"
    )

    topic = await session.scalar(select(Topic).where(Topic.slug == "journal"))
    sys_prompt = (
        topic.system_prompt
        if (topic and topic.system_prompt and topic.system_prompt.strip())
        else JOURNAL_DEFAULT_SYSTEM_PROMPT
    )

    return user_prompt, sys_prompt


def parse_journal_daily_summary_response(raw_text: str) -> dict[str, Any]:
    """
    Parse LLM response using re.DOTALL:
    Extracts [[SUMMARY]], [[ENCOURAGING]], [[FOCUS]], [[METADATA_JSON]].
    Normalizes metadata JSON; on JSON parse failure returns '{}' while keeping text.
    Strips metadata block before any visible-text fallback to ensure raw metadata is never exposed.
    """
    if not raw_text:
        return {
            "summary_text": "",
            "encouraging_message": None,
            "tomorrow_focus": None,
            "metadata_json": "{}",
        }

    # 1. Extract metadata block
    meta_match = re.search(r"\[\[METADATA_JSON\]\]\s*(.*?)(?=\[\[|\Z)", raw_text, re.DOTALL)
    metadata_str = "{}"
    if meta_match:
        raw_json = meta_match.group(1).strip()
        # Clean potential markdown fences ```json ... ```
        raw_json = re.sub(r"^```(?:json)?\s*", "", raw_json, flags=re.MULTILINE)
        raw_json = re.sub(r"\s*```$", "", raw_json, flags=re.MULTILINE).strip()
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                required_keys = [
                    "dominant_emotions",
                    "gratitude",
                    "important_events",
                    "lessons",
                    "keep",
                    "let_go",
                    "tomorrow_intentions",
                ]
                normalized = {}
                for k in required_keys:
                    val = parsed.get(k, [])
                    if isinstance(val, list):
                        normalized[k] = [str(item).strip() for item in val if str(item).strip()]
                    elif isinstance(val, str) and val.strip():
                        normalized[k] = [val.strip()]
                    else:
                        normalized[k] = []
                metadata_str = json.dumps(normalized, ensure_ascii=False)
        except Exception:
            metadata_str = "{}"

    # 2. Strip metadata block completely from visible text to prevent leakage
    text_without_meta = re.sub(r"\[\[METADATA_JSON\]\]\s*.*?(?=\[\[|\Z)", "", raw_text, flags=re.DOTALL).strip()

    # 3. Parse visible sections independently from text_without_meta
    summary_match = re.search(r"\[\[SUMMARY\]\]\s*(.*?)(?=\[\[|\Z)", text_without_meta, re.DOTALL)
    encouraging_match = re.search(r"\[\[ENCOURAGING\]\]\s*(.*?)(?=\[\[|\Z)", text_without_meta, re.DOTALL)
    focus_match = re.search(r"\[\[FOCUS\]\]\s*(.*?)(?=\[\[|\Z)", text_without_meta, re.DOTALL)

    summary_text = summary_match.group(1).strip() if summary_match else ""
    encouraging = encouraging_match.group(1).strip() if encouraging_match else None
    focus = focus_match.group(1).strip() if focus_match else None

    # 4. Visible-text fallback when [[SUMMARY]] is missing
    if not summary_text:
        # Check if there is prefix visible text before any [[ tags
        prefix_match = re.match(r"^(.*?)(?=\[\[)", text_without_meta, re.DOTALL)
        prefix_text = prefix_match.group(1).strip() if prefix_match else ""

        if prefix_text:
            summary_text = prefix_text
        elif encouraging or focus:
            # Meaningful NON-METADATA visible text exists in encouraging / focus
            parts = []
            if encouraging:
                parts.append(encouraging)
            if focus:
                parts.append(focus)
            summary_text = "\n\n".join(parts)
        else:
            # No tags at all: entire non-metadata text is the summary
            summary_text = text_without_meta.strip()

    # Absolute guarantee: no raw metadata markers or JSON in summary_text
    if "[[METADATA_JSON]]" in summary_text:
        summary_text = summary_text.split("[[METADATA_JSON]]")[0].strip()

    return {
        "summary_text": summary_text,
        "encouraging_message": encouraging,
        "tomorrow_focus": focus,
        "metadata_json": metadata_str,
    }


async def build_journal_dynamics_context(
    session: AsyncSession,
    user_id: int,
    days: int = 7,
) -> tuple[str, str, int]:
    """
    Build context for dynamics analysis (7 or 21 days).
    Requires at least 2 completed entries.
    Returns (user_prompt, system_prompt, entries_count).
    """
    today = msk_now().date()
    start_date = today - timedelta(days=days - 1)

    stmt = (
        select(JournalEntry)
        .where(
            JournalEntry.user_id == user_id,
            JournalEntry.status == "completed",
            JournalEntry.reflection_date >= start_date,
            JournalEntry.reflection_date <= today,
        )
        .order_by(JournalEntry.reflection_date.asc())
    )
    entries = (await session.scalars(stmt)).all()
    if len(entries) < 2:
        return "", "", len(entries)

    lines = []
    for e in entries:
        day_block = f"Дата: {e.reflection_date}"
        if e.summary_text:
            day_block += f"\nИтог дня: {e.summary_text}"
        if e.metadata_json and e.metadata_json != "{}":
            day_block += f"\nТеги и метаданные: {e.metadata_json}"
        lines.append(day_block)

    history_block = "\n---\n".join(lines)
    user_prompt = (
        f"Анализ динамики саморефлексии пользователя за период последних {days} дней "
        f"(с {start_date} по {today}, завершено записей: {len(entries)}):\n\n"
        f"{history_block}\n\n"
        "Проведи глубокий, поддерживающий анализ динамики за этот период:\n"
        "- повторяющиеся темы и фокусы внимания;\n"
        "- эмоциональные изменения и паттерны переживаний;\n"
        "- источники благодарности и ресурса;\n"
        "- ключевые события и уроки;\n"
        "- позитивные сдвиги и точки опоры;\n"
        "- бережные рекомендации и один конкретный бережный фокус на следующий период.\n\n"
        "Категорически запрещено: ставить диагнозы, выдумывать несуществующие тренды, давать категоричные советы."
    )

    topic = await session.scalar(select(Topic).where(Topic.slug == "journal"))
    sys_prompt = (
        topic.system_prompt
        if (topic and topic.system_prompt and topic.system_prompt.strip())
        else JOURNAL_DEFAULT_SYSTEM_PROMPT
    )

    return user_prompt, sys_prompt, len(entries)
