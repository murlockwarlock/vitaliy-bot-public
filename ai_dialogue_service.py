from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    Message as DBMessage,
    PendingAiFlow,
    PreTestClaim,
    SessionSummary,
    SummaryClaim,
    TestAttempt,
    Topic,
    TopicDialogueConfig,
    TopicPrerecordedDelivery,
    User,
    UserTopicSession,
    async_session_maker,
)
from time_utils import msk_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TopicContextSnapshot:
    user_id: int
    topic_slug: str
    dialogue_id: int


def build_ai_user_profile_context(user: User | None) -> str:
    """Canonical user profile context for AI prompts.

    Contains:
    - name (User.name, fallback User.first_name)
    - gender if present (no invented missing values)
    - age if present (no invented missing values)
    - response_length directive
    No billing, subscription, or balance metadata.
    """
    if not user:
        return ""

    lines = []
    effective_name = (getattr(user, 'name', None) or getattr(user, 'first_name', None) or "").strip()
    if effective_name:
        lines.append(f"Имя пользователя: {effective_name}")

    gender = getattr(user, 'gender', None)
    if gender and str(gender).strip():
        lines.append(f"Пол: {str(gender).strip()}")

    age = getattr(user, 'age', None)
    if age and str(age).strip():
        lines.append(f"Возраст: {str(age).strip()}")

    resp_len = getattr(user, 'response_length', 'normal') or 'normal'
    if resp_len in ('short', 'лаконичный'):
        lines.append(
            "Режим кратких ответов (лаконичный): пользователь предпочитает краткие и емкие ответы. "
            "Строго ограничивай длину ответа: не более 300-400 слов на весь ответ. Будь лаконичен, давай суть без лишних подробностей."
        )
    elif resp_len in ('detailed', 'развернутый'):
        lines.append(
            "Режим подробных ответов (развернутый): давай развернутые, глубокие ответы с примерами и деталями."
        )

    return "\n".join(lines)


async def get_active_topic_dialogue_id(
    session: AsyncSession,
    user_id: int,
    topic_slug: str,
    default: int = 1,
) -> int:
    """Returns the current active dialogue_id for a user in a given topic."""
    row = (await session.execute(
        select(UserTopicSession.active_dialogue_id)
        .where(
            UserTopicSession.user_id == user_id,
            UserTopicSession.topic_slug == topic_slug,
        )
    )).scalar_one_or_none()
    return row if row is not None else default


async def allocate_global_dialogue_id(
    session: AsyncSession,
    user_id: int,
) -> int:
    """Atomically allocates the next unique monotonic global dialogue_id using DB-level atomic mutation on User.current_dialogue_id."""
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(current_dialogue_id=func.coalesce(User.current_dialogue_id, 0) + 1)
        .returning(User.current_dialogue_id)
    )
    res = await session.execute(stmt)
    allocated_id = res.scalar_one_or_none()
    if allocated_id is not None:
        user = await session.get(User, user_id)
        if user:
            user.current_dialogue_id = allocated_id
        return allocated_id

    # Fallback if user row did not exist yet: insert user with current_dialogue_id=1
    user = User(id=user_id, current_dialogue_id=1)
    session.add(user)
    await session.flush()
    return 1


async def allocate_next_dialogue_id(
    session: AsyncSession,
    user_id: int,
    topic_slug: str,
) -> int:
    """Atomically allocates the next unique monotonic dialogue_id using DB-level atomic mutation on User.current_dialogue_id."""
    allocated_id = await allocate_global_dialogue_id(session, user_id)
    # Monotonically bind the allocated ID to UserTopicSession
    await set_active_topic_dialogue_id(session, user_id, topic_slug, allocated_id, monotonic=True)
    return allocated_id


async def set_active_topic_dialogue_id(
    session: AsyncSession,
    user_id: int,
    topic_slug: str,
    dialogue_id: int,
    monotonic: bool = True,
) -> int:
    """Sets active_dialogue_id with monotonic guard to prevent concurrency race overwriting."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

    now = msk_now()
    if monotonic:
        stmt = sqlite_upsert(UserTopicSession).values(
            user_id=user_id,
            topic_slug=topic_slug,
            active_dialogue_id=dialogue_id,
            updated_at=now,
        ).on_conflict_do_update(
            index_elements=['user_id', 'topic_slug'],
            set_={
                'active_dialogue_id': func.max(UserTopicSession.active_dialogue_id, dialogue_id),
                'updated_at': now,
            }
        )
    else:
        stmt = sqlite_upsert(UserTopicSession).values(
            user_id=user_id,
            topic_slug=topic_slug,
            active_dialogue_id=dialogue_id,
            updated_at=now,
        ).on_conflict_do_update(
            index_elements=['user_id', 'topic_slug'],
            set_={
                'active_dialogue_id': dialogue_id,
                'updated_at': now,
            }
        )

    await session.execute(stmt)
    await session.flush()
    return await get_active_topic_dialogue_id(session, user_id, topic_slug, default=dialogue_id)


@dataclass
class DialogueTurn:
    user_message: DBMessage | None = None
    assistant_message: DBMessage | None = None
    extra_messages: list[DBMessage] = field(default_factory=list)

    @property
    def messages(self) -> list[DBMessage]:
        res = []
        if self.user_message is not None:
            res.append(self.user_message)
        if self.assistant_message is not None:
            res.append(self.assistant_message)
        res.extend(self.extra_messages)
        return sorted(res, key=lambda m: m.id if m.id is not None else 0)

    @property
    def id(self) -> int:
        msgs = self.messages
        return msgs[0].id if msgs and msgs[0].id is not None else 0


def group_messages_into_turns(messages: list[DBMessage]) -> list[DialogueTurn]:
    """Groups chronological Message rows into logical DialogueTurn units."""
    turns: list[DialogueTurn] = []
    current_turn: DialogueTurn | None = None

    for msg in messages:
        if msg.role == "assistant":
            if current_turn is not None and current_turn.user_message is not None and current_turn.assistant_message is None:
                current_turn.assistant_message = msg
                turns.append(current_turn)
                current_turn = None
            elif current_turn is not None:
                current_turn.extra_messages.append(msg)
            else:
                turns.append(DialogueTurn(assistant_message=msg))
        elif msg.role == "user":
            if current_turn is not None:
                turns.append(current_turn)
            current_turn = DialogueTurn(user_message=msg)
        else:
            if current_turn is not None:
                current_turn.extra_messages.append(msg)
            else:
                turns.append(DialogueTurn(extra_messages=[msg]))

    if current_turn is not None:
        turns.append(current_turn)

    return turns


async def get_turn_aware_history(
    session: AsyncSession,
    user_id: int,
    topic_slug: str,
    dialogue_id: int,
    limit_first: int = 5,
    limit_recent: int = 5,
    exclude_message_id: int | None = None,
) -> list[DBMessage]:
    """Loads historical messages grouped by turns.

    Selects first N complete turns and last M complete turns,
    deduplicates overlapping turns, and flattens them chronologically.
    No half-turns caused by raw message limits.
    """
    stmt = (
        select(DBMessage)
        .where(
            DBMessage.user_id == user_id,
            DBMessage.topic_slug == topic_slug,
            DBMessage.dialogue_id == dialogue_id,
        )
    )
    if exclude_message_id is not None:
        stmt = stmt.where(DBMessage.id != exclude_message_id)

    stmt = stmt.order_by(DBMessage.id.asc())
    all_msgs = (await session.execute(stmt)).scalars().all()
    if not all_msgs:
        return []

    turns = group_messages_into_turns(all_msgs)
    if not turns:
        return []

    first_turns = turns[:limit_first] if limit_first > 0 else []
    recent_turns = turns[-limit_recent:] if limit_recent > 0 else []

    turn_map = {}
    for t in first_turns:
        turn_map[t.id] = t
    for t in recent_turns:
        turn_map[t.id] = t

    sorted_turns = sorted(turn_map.values(), key=lambda t: t.id)

    result_msgs = []
    for t in sorted_turns:
        result_msgs.extend(t.messages)

    return result_msgs


async def deliver_prerecorded_first_message(
    bot: Bot,
    session: AsyncSession,
    user_id: int,
    topic_slug: str,
    dialogue_id: int,
    text: str,
) -> bool:
    """Guard 2: Concurrency-safe DB-level exactly-once delivery of prerecorded first message with atomic CAS retry and stale recovery."""
    now = msk_now()
    stale_cutoff = now - timedelta(seconds=120)

    # 1. Try atomic INSERT first
    won_ownership = False
    delivery = TopicPrerecordedDelivery(
        user_id=user_id,
        topic_slug=topic_slug,
        dialogue_id=dialogue_id,
        status="claimed",
        claimed_at=now,
    )
    session.add(delivery)
    try:
        await session.flush()
        await session.commit()
        won_ownership = True
    except IntegrityError:
        await session.rollback()
        # Row already exists: atomic CAS retry for 'failed' or stale 'claimed'
        stmt = (
            update(TopicPrerecordedDelivery)
            .where(
                TopicPrerecordedDelivery.user_id == user_id,
                TopicPrerecordedDelivery.topic_slug == topic_slug,
                TopicPrerecordedDelivery.dialogue_id == dialogue_id,
                or_(
                    TopicPrerecordedDelivery.status == "failed",
                    and_(
                        TopicPrerecordedDelivery.status == "claimed",
                        or_(
                            TopicPrerecordedDelivery.claimed_at < stale_cutoff,
                            TopicPrerecordedDelivery.claimed_at.is_(None),
                        ),
                    ),
                ),
            )
            .values(status="claimed", claimed_at=now)
            .returning(TopicPrerecordedDelivery.id)
        )
        res = await session.execute(stmt)
        updated_id = res.scalar_one_or_none()
        await session.commit()
        won_ownership = (updated_id is not None)

    if not won_ownership:
        return False

    # 2. Try external send
    try:
        await bot.send_message(user_id, text)
    except Exception as exc:
        logger.warning("Failed to send prerecorded message via bot: %s", exc)
        # Update delivery status to failed so it can be retried and doesn't permanently suppress
        async with async_session_maker() as s:
            await s.execute(
                update(TopicPrerecordedDelivery)
                .where(
                    TopicPrerecordedDelivery.user_id == user_id,
                    TopicPrerecordedDelivery.topic_slug == topic_slug,
                    TopicPrerecordedDelivery.dialogue_id == dialogue_id,
                )
                .values(status="failed")
            )
            await s.commit()
        return False

    # 3. External send succeeded, persist assistant message and mark delivered
    async with async_session_maker() as s:
        msg = DBMessage(
            user_id=user_id,
            role="assistant",
            content=text,
            topic_slug=topic_slug,
            dialogue_id=dialogue_id,
            message_kind="topic_prerecorded_first",
        )
        s.add(msg)
        await s.flush()
        await s.execute(
            update(TopicPrerecordedDelivery)
            .where(
                TopicPrerecordedDelivery.user_id == user_id,
                TopicPrerecordedDelivery.topic_slug == topic_slug,
                TopicPrerecordedDelivery.dialogue_id == dialogue_id,
            )
            .values(
                status="delivered",
                message_id=msg.id,
                delivered_at=msk_now(),
            )
        )
        await s.commit()

    return True


async def acquire_pre_test_claim(
    session: AsyncSession,
    user_id: int,
    topic_slug: str,
    dialogue_id: int,
    test_set_id: int,
    lease_seconds: int = 120,
) -> tuple[bool, PreTestClaim | None]:
    """Guard 3: DB-backed ownership for pre-dialogue test once-per-dialogue with atomic retry CAS and lease."""
    now = msk_now()
    lease_until = now + timedelta(seconds=lease_seconds)

    # 1. Try atomic INSERT first
    claim = PreTestClaim(
        user_id=user_id,
        topic_slug=topic_slug,
        dialogue_id=dialogue_id,
        test_set_id=test_set_id,
        status="in_progress",
        claimed_at=now,
        lease_until=lease_until,
    )
    session.add(claim)
    try:
        await session.flush()
        return True, claim
    except IntegrityError:
        await session.rollback()

    existing = (await session.execute(
        select(PreTestClaim).where(
            PreTestClaim.user_id == user_id,
            PreTestClaim.topic_slug == topic_slug,
            PreTestClaim.dialogue_id == dialogue_id,
            PreTestClaim.test_set_id == test_set_id,
        )
    )).scalar_one_or_none()
    old_attempt_id = existing.attempt_id if existing else None

    # 2. Row exists: atomic CAS retry for failed/cancelled or stale in_progress
    stmt = (
        update(PreTestClaim)
        .where(
            PreTestClaim.user_id == user_id,
            PreTestClaim.topic_slug == topic_slug,
            PreTestClaim.dialogue_id == dialogue_id,
            PreTestClaim.test_set_id == test_set_id,
            or_(
                PreTestClaim.status.in_(["failed", "cancelled"]),
                and_(
                    PreTestClaim.status == "in_progress",
                    or_(
                        PreTestClaim.lease_until < now,
                        PreTestClaim.lease_until.is_(None),
                    ),
                ),
            ),
        )
        .values(
            status="in_progress",
            attempt_id=None,
            claimed_at=now,
            lease_until=lease_until,
        )
        .returning(PreTestClaim.id)
    )
    res = await session.execute(stmt)
    updated_id = res.scalar_one_or_none()
    if updated_id is not None:
        if old_attempt_id:
            old_attempt = await session.get(TestAttempt, old_attempt_id)
            if old_attempt and old_attempt.status == "in_progress":
                old_attempt.status = "abandoned"
                old_attempt.completed_at = now
                await session.flush()
        updated_claim = await session.get(PreTestClaim, updated_id)
        return True, updated_claim

    return False, existing


async def complete_pre_test_claim(
    session: AsyncSession,
    user_id: int,
    topic_slug: str,
    dialogue_id: int,
    test_set_id: int,
    attempt_id: int,
) -> None:
    """Marks PreTestClaim as completed and links attempt_id."""
    await session.execute(
        update(PreTestClaim)
        .where(
            PreTestClaim.user_id == user_id,
            PreTestClaim.topic_slug == topic_slug,
            PreTestClaim.dialogue_id == dialogue_id,
            PreTestClaim.test_set_id == test_set_id,
        )
        .values(
            status="completed",
            attempt_id=attempt_id,
        )
    )
    await session.commit()


async def renew_pre_test_claim_lease(
    session: AsyncSession,
    user_id: int,
    topic_slug: str,
    dialogue_id: int,
    test_set_id: int,
    duration_seconds: int = 120,
) -> bool:
    """Renews PreTestClaim lease on real user activity (answer accepted / next question dispatched)."""
    now = msk_now()
    stmt = (
        update(PreTestClaim)
        .where(
            PreTestClaim.user_id == user_id,
            PreTestClaim.topic_slug == topic_slug,
            PreTestClaim.dialogue_id == dialogue_id,
            PreTestClaim.test_set_id == test_set_id,
            PreTestClaim.status == "in_progress",
        )
        .values(lease_until=now + timedelta(seconds=duration_seconds))
        .returning(PreTestClaim.id)
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none() is not None


async def get_pre_test_claim(
    session: AsyncSession,
    user_id: int,
    topic_slug: str,
    dialogue_id: int,
    test_set_id: int | None = None,
) -> PreTestClaim | None:
    """Returns existing PreTestClaim row for user + topic + dialogue."""
    stmt = select(PreTestClaim).where(
        PreTestClaim.user_id == user_id,
        PreTestClaim.topic_slug == topic_slug,
        PreTestClaim.dialogue_id == dialogue_id,
    )
    if test_set_id is not None:
        stmt = stmt.where(PreTestClaim.test_set_id == test_set_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def claim_summary_generation(
    *args,
    **kwargs,
) -> Any:
    """Guard 4: Renewable lease claiming for session summary generation.

    Supports:
      claim_summary_generation(session, user_id, topic_slug, dialogue_id, [version=1], [lease_seconds=120]) -> (bool, SummaryClaim | None, str)
      claim_summary_generation(user_id, topic_slug, dialogue_id, owner_token, [lease_seconds=120], [version=1]) -> bool
    """
    if len(args) > 0 and hasattr(args[0], 'execute'):
        session, user_id, topic_slug, dialogue_id = args[0], args[1], args[2], args[3]
        version = kwargs.get('version', args[4] if len(args) > 4 else 1)
        lease_seconds = kwargs.get('lease_seconds', args[5] if len(args) > 5 else 120)
        token = str(uuid.uuid4())
        return await _do_claim_summary(session, user_id, topic_slug, dialogue_id, token, version, lease_seconds)
    else:
        user_id, topic_slug, dialogue_id = args[0], args[1], args[2]
        token = args[3] if len(args) > 3 else (kwargs.get('owner_token') or kwargs.get('token') or str(uuid.uuid4()))
        lease_seconds = kwargs.get('lease_seconds', args[4] if len(args) > 4 else 120)
        version = kwargs.get('version', args[5] if len(args) > 5 else 1)
        async with async_session_maker() as session:
            ok, _, _ = await _do_claim_summary(session, user_id, topic_slug, dialogue_id, token, version, lease_seconds)
            return ok


async def _do_claim_summary(
    session: AsyncSession,
    user_id: int,
    topic_slug: str,
    dialogue_id: int,
    token: str,
    version: int,
    lease_seconds: int,
) -> tuple[bool, SummaryClaim | None, str]:
    from sqlalchemy import or_
    now = msk_now()
    new_lease = now + timedelta(seconds=lease_seconds)

    existing = (await session.execute(
        select(SummaryClaim).where(
            SummaryClaim.user_id == user_id,
            SummaryClaim.topic_slug == topic_slug,
            SummaryClaim.dialogue_id == dialogue_id,
            SummaryClaim.version == version,
        )
    )).scalar_one_or_none()

    if not existing:
        claim = SummaryClaim(
            user_id=user_id,
            topic_slug=topic_slug,
            dialogue_id=dialogue_id,
            version=version,
            status="in_progress",
            claimed_at=now,
            lease_until=new_lease,
            owner_token=token,
        )
        session.add(claim)
        try:
            await session.flush()
            await session.commit()
            return True, claim, token
        except IntegrityError:
            await session.rollback()
            existing = (await session.execute(
                select(SummaryClaim).where(
                    SummaryClaim.user_id == user_id,
                    SummaryClaim.topic_slug == topic_slug,
                    SummaryClaim.dialogue_id == dialogue_id,
                    SummaryClaim.version == version,
                )
            )).scalar_one_or_none()

    if not existing:
        return False, None, ""

    if existing.status == "completed":
        return False, existing, ""

    if existing.status == "in_progress" and existing.lease_until > now:
        return False, existing, ""

    res = await session.execute(
        update(SummaryClaim)
        .where(
            SummaryClaim.id == existing.id,
            or_(
                SummaryClaim.status == "failed",
                SummaryClaim.lease_until <= now,
            ),
        )
        .values(
            owner_token=token,
            lease_until=new_lease,
            status="in_progress",
            error_message=None,
        )
    )
    if res.rowcount > 0:
        await session.commit()
        existing.owner_token = token
        existing.lease_until = new_lease
        existing.status = "in_progress"
        return True, existing, token

    return False, existing, ""


async def renew_summary_claim_lease(
    *args,
    **kwargs,
) -> bool:
    """Guard 4: Extends lease_until if owner_token matches and status is in_progress.

    Supports:
      renew_summary_claim_lease(session, claim_id, owner_token, [lease_seconds=120])
      renew_summary_claim_lease(user_id, topic_slug, dialogue_id, owner_token, [extend_seconds=120])
    """
    now = msk_now()
    if len(args) > 0 and hasattr(args[0], 'execute'):
        session, claim_id, owner_token = args[0], args[1], args[2]
        lease_seconds = kwargs.get('lease_seconds', args[3] if len(args) > 3 else 120)
        new_lease = now + timedelta(seconds=lease_seconds)
        res = await session.execute(
            update(SummaryClaim)
            .where(
                SummaryClaim.id == claim_id,
                SummaryClaim.owner_token == owner_token,
                SummaryClaim.status == "in_progress",
            )
            .values(lease_until=new_lease)
        )
        if res.rowcount > 0:
            await session.commit()
            return True
        return False
    else:
        user_id, topic_slug, dialogue_id, owner_token = args[0], args[1], args[2], args[3]
        extend_seconds = kwargs.get('extend_seconds', kwargs.get('lease_seconds', args[4] if len(args) > 4 else 120))
        new_lease = now + timedelta(seconds=extend_seconds)
        async with async_session_maker() as session:
            res = await session.execute(
                update(SummaryClaim)
                .where(
                    SummaryClaim.user_id == user_id,
                    SummaryClaim.topic_slug == topic_slug,
                    SummaryClaim.dialogue_id == dialogue_id,
                    SummaryClaim.owner_token == owner_token,
                    SummaryClaim.status == "in_progress",
                )
                .values(lease_until=new_lease)
            )
            if res.rowcount > 0:
                await session.commit()
                return True
            return False


async def finish_summary_claim(
    *args,
    **kwargs,
) -> bool:
    """Finalizes SummaryClaim.

    Supports:
      finish_summary_claim(session, claim_id, owner_token, [status='completed'], [error_message=None])
      finish_summary_claim(user_id, topic_slug, dialogue_id, owner_token, [summary_id=None], [status='completed'])
    """
    if len(args) > 0 and hasattr(args[0], 'execute'):
        session, claim_id, owner_token = args[0], args[1], args[2]
        status = kwargs.get('status', args[3] if len(args) > 3 else "completed")
        error_message = kwargs.get('error_message', args[4] if len(args) > 4 else None)
        res = await session.execute(
            update(SummaryClaim)
            .where(
                SummaryClaim.id == claim_id,
                SummaryClaim.owner_token == owner_token,
            )
            .values(
                status=status,
                completed_at=msk_now(),
                error_message=error_message,
            )
        )
        if res.rowcount > 0:
            await session.commit()
            return True
        return False
    else:
        user_id, topic_slug, dialogue_id, owner_token = args[0], args[1], args[2], args[3]
        summary_id = kwargs.get('summary_id', args[4] if len(args) > 4 else None)
        status = kwargs.get('status', "completed")
        async with async_session_maker() as session:
            res = await session.execute(
                update(SummaryClaim)
                .where(
                    SummaryClaim.user_id == user_id,
                    SummaryClaim.topic_slug == topic_slug,
                    SummaryClaim.dialogue_id == dialogue_id,
                    SummaryClaim.owner_token == owner_token,
                )
                .values(
                    status=status,
                    summary_id=summary_id,
                    completed_at=msk_now(),
                )
            )
            if res.rowcount > 0:
                await session.commit()
                return True
            return False


async def _default_summary_chunk(system_prompt: str, user_prompt: str) -> str:
    """Delegates to get_standalone_ai_response for summary Map-Reduce prompts."""
    from ai_integration import get_standalone_ai_response
    return await get_standalone_ai_response(system_prompt, user_prompt)


_generate_summary_chunk = _default_summary_chunk
get_ai_response = _default_summary_chunk


SUMMARY_HEARTBEAT_INTERVAL_SECONDS = 15.0


async def generate_session_summary(
    user_id: int,
    topic_slug: str,
    dialogue_id: int,
    version: int = 1,
    is_regeneration: bool = False,
    heartbeat_interval_seconds: float | None = None,
) -> SessionSummary | None:
    """Generates a structured whole-session summary using Map-Reduce with renewable lease and strict ownership fencing."""
    async with async_session_maker() as session:
        topic = (await session.execute(select(Topic).where(Topic.slug == topic_slug))).scalar_one_or_none()
        cfg = (await session.execute(
            select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == topic_slug)
        )).scalar_one_or_none()
        user = await session.get(User, user_id)
        profile_str = build_ai_user_profile_context(user)

        topic_name = topic.name if topic else topic_slug
        summary_prompt = (cfg.summary_prompt if cfg and cfg.summary_prompt else None) or (
            "Составь структурированное резюме психологической сессии:\n"
            "1. Основной запрос и контекст ситуации\n"
            "2. Ключевые паттерны, сопротивления и эмоции\n"
            "3. Инсайты и точки осознания\n"
            "4. Рекомендованные следующие шаги и практики"
        )

        # Check if ANY claim is currently live (in_progress with lease_until > msk_now())
        live_claim = (await session.execute(
            select(SummaryClaim).where(
                SummaryClaim.user_id == user_id,
                SummaryClaim.topic_slug == topic_slug,
                SummaryClaim.dialogue_id == dialogue_id,
                SummaryClaim.status == "in_progress",
                SummaryClaim.lease_until > msk_now(),
            )
        )).scalar_one_or_none()
        if live_claim:
            in_prog = SessionSummary(
                user_id=user_id,
                topic_slug=topic_slug,
                topic_slug_snapshot=topic_slug,
                topic_name_snapshot=topic_name,
                dialogue_id=dialogue_id,
                version=live_claim.version,
                summary_text="already_generating",
                extra_metadata=json.dumps({"status": "already_generating"}),
            )
            in_prog.is_generating = True
            in_prog.status = "already_generating"
            return in_prog

        if is_regeneration:
            max_summary_ver = (await session.execute(
                select(func.coalesce(func.max(SessionSummary.version), 0)).where(
                    SessionSummary.user_id == user_id,
                    SessionSummary.topic_slug_snapshot == topic_slug,
                    SessionSummary.dialogue_id == dialogue_id,
                )
            )).scalar() or 0

            version = max_summary_ver + 1
            if version <= 1:
                version = 2
        else:
            # Check if already generated
            existing_summary = (await session.execute(
                select(SessionSummary).where(
                    SessionSummary.user_id == user_id,
                    SessionSummary.topic_slug_snapshot == topic_slug,
                    SessionSummary.dialogue_id == dialogue_id,
                    SessionSummary.version == version,
                )
            )).scalar_one_or_none()
            if existing_summary:
                return existing_summary

        messages = (await session.execute(
            select(DBMessage)
            .where(
                DBMessage.user_id == user_id,
                DBMessage.topic_slug == topic_slug,
                DBMessage.dialogue_id == dialogue_id,
            )
            .order_by(DBMessage.id.asc())
        )).scalars().all()

        if not messages:
            return None

        claimed, claim, token = await claim_summary_generation(
            session, user_id, topic_slug, dialogue_id, version=version, lease_seconds=120
        )
        if not claimed or not claim:
            logger.info("Summary claim not acquired for user=%s topic=%s dialogue=%s version=%s", user_id, topic_slug, dialogue_id, version)
            if claim and claim.status == "completed":
                existing = (await session.execute(
                    select(SessionSummary).where(
                        SessionSummary.user_id == user_id,
                        SessionSummary.topic_slug_snapshot == topic_slug,
                        SessionSummary.dialogue_id == dialogue_id,
                        SessionSummary.version == version,
                    )
                )).scalar_one_or_none()
                if existing:
                    return existing
            if claim and claim.status == "in_progress":
                in_prog = SessionSummary(
                    user_id=user_id,
                    topic_slug=topic_slug,
                    topic_slug_snapshot=topic_slug,
                    topic_name_snapshot=topic_name,
                    dialogue_id=dialogue_id,
                    version=version,
                    summary_text="already_generating",
                    extra_metadata=json.dumps({"status": "already_generating"}),
                )
                in_prog.is_generating = True
                in_prog.status = "already_generating"
                return in_prog
            return None

    # Proceed with LLM Map-Reduce summarization outside of active DB transaction
    from ai_integration import GLOBAL_TONE_INSTRUCTION, get_standalone_ai_response

    heartbeat_stop = asyncio.Event()
    lost_ownership = False

    hb_interval = heartbeat_interval_seconds if heartbeat_interval_seconds is not None else SUMMARY_HEARTBEAT_INTERVAL_SECONDS

    async def _heartbeat_worker():
        nonlocal lost_ownership
        while not heartbeat_stop.is_set():
            try:
                await asyncio.wait_for(heartbeat_stop.wait(), timeout=hb_interval)
                break
            except asyncio.TimeoutError:
                async with async_session_maker() as h_session:
                    ok = await renew_summary_claim_lease(h_session, claim.id, token, lease_seconds=120)
                    if not ok:
                        logger.warning("Heartbeat failed to renew summary claim lease for claim=%s token=%s", claim.id, token)
                        lost_ownership = True
                        break

    heartbeat_task = asyncio.create_task(_heartbeat_worker())

    try:
        # Step 1: Group messages into turns and chunk by 10 turns with 1-turn overlap
        turns = group_messages_into_turns(messages)
        if not turns:
            return None

        turn_chunks = []
        if len(turns) <= 10:
            turn_chunks.append(turns)
        else:
            start = 0
            while start < len(turns):
                chunk = turns[start : start + 10]
                turn_chunks.append(chunk)
                if start + 10 >= len(turns):
                    break
                start += 9  # 1-turn overlap

        intermediate_summaries = []
        profile_part = f"\n\nПрофиль пользователя:\n{profile_str}" if profile_str else ""

        for idx, chunk in enumerate(turn_chunks):
            if lost_ownership:
                logger.warning("Lost summary claim ownership during map step. Aborting.")
                return None

            # Renew lease before map chunk and check return value
            async with async_session_maker() as session:
                ok = await renew_summary_claim_lease(session, claim.id, token, lease_seconds=120)
                if not ok:
                    logger.warning("Summary claim lease renewal returned False before map chunk %s. Aborting.", idx)
                    return None

            chunk_msgs = []
            for t in chunk:
                chunk_msgs.extend(t.messages)

            chunk_text = "\n".join(
                f"{m.role.upper()}: {m.content}"
                for m in chunk_msgs if m.content
            )
            if not chunk_text.strip():
                continue

            summary_fn = _generate_summary_chunk
            if get_ai_response is not _default_summary_chunk:
                summary_fn = get_ai_response
            elif _generate_summary_chunk is not _default_summary_chunk:
                summary_fn = _generate_summary_chunk

            if len(turn_chunks) > 1:
                map_system = f"Ты психолог-аналитик.{profile_part}{GLOBAL_TONE_INSTRUCTION}"
                map_prompt = (
                    f"Кратко выдели ключевые факты и инсайты из этой части сессии (часть {idx + 1}/{len(turn_chunks)}):\n\n"
                    f"{chunk_text}"
                )
                intermediate = await summary_fn(map_system, map_prompt)
                if lost_ownership:
                    return None
                intermediate_summaries.append(intermediate)
            else:
                intermediate_summaries.append(chunk_text)

        if lost_ownership:
            return None

        # Renew lease before Reduce step and check return value
        async with async_session_maker() as session:
            ok = await renew_summary_claim_lease(session, claim.id, token, lease_seconds=120)
            if not ok:
                logger.warning("Summary claim lease renewal returned False before reduce step. Aborting.")
                return None

        # Step 2: Reduce step
        joined_material = "\n\n--- Часть ---\n\n".join(intermediate_summaries)
        reduce_system = f"{summary_prompt}{profile_part}{GLOBAL_TONE_INSTRUCTION}"
        reduce_prompt = (
            f"На основе следующих материалов сессии составь итоговое резюме.\n\n"
            f"Материалы:\n{joined_material}\n\n"
            f"Инструкция по структуре:\n{summary_prompt}"
        )
        final_summary_text = await summary_fn(reduce_system, reduce_prompt)

        if lost_ownership:
            logger.warning("Lost summary claim ownership after reduce step. Aborting.")
            return None

        async with async_session_maker() as session:
            # Atomically prove owner_token still owns the live claim and transition to completed
            stmt = (
                update(SummaryClaim)
                .where(
                    SummaryClaim.id == claim.id,
                    SummaryClaim.owner_token == token,
                    SummaryClaim.status == "in_progress",
                )
                .values(
                    status="completed",
                    completed_at=msk_now(),
                )
                .returning(SummaryClaim.id)
            )
            res = await session.execute(stmt)
            updated_claim_id = res.scalar_one_or_none()
            if not updated_claim_id:
                logger.warning("SummaryClaim CAS failed for claim=%s token=%s. Lost ownership, refusing to persist summary.", claim.id, token)
                await session.rollback()
                return None

            summary = SessionSummary(
                user_id=user_id,
                topic_slug=topic_slug,
                topic_slug_snapshot=topic_slug,
                topic_name_snapshot=topic_name,
                dialogue_id=dialogue_id,
                version=version,
                summary_text=final_summary_text,
                summary_prompt_used=summary_prompt,
                extra_metadata=json.dumps({"message_count": len(messages)}, ensure_ascii=False),
                created_at=msk_now(),
            )
            session.add(summary)
            try:
                await session.commit()
                return summary
            except IntegrityError:
                await session.rollback()
                return (await session.execute(
                    select(SessionSummary).where(
                        SessionSummary.user_id == user_id,
                        SessionSummary.topic_slug_snapshot == topic_slug,
                        SessionSummary.dialogue_id == dialogue_id,
                        SessionSummary.version == version,
                    )
                )).scalar_one_or_none()

    except Exception as exc:
        logger.exception("Failed to generate session summary: %s", exc)
        async with async_session_maker() as session:
            await finish_summary_claim(session, claim.id, token, status="failed", error_message=str(exc))
        return None
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass


# ==============================================================================
# Guard 1: Allowlisted Flow Resumption for Disclaimer
# ==============================================================================

async def store_pending_ai_flow(
    *args,
    **kwargs,
) -> None:
    """Stores pending flow before disclaimer acceptance. Upsert by user_id.

    Supports:
      store_pending_ai_flow(session, user_id, flow_type, [payload])
      store_pending_ai_flow(user_id, flow_type, [payload])
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

    if len(args) >= 3 and hasattr(args[0], 'execute'):
        session, user_id, flow_type = args[0], args[1], args[2]
        payload = args[3] if len(args) > 3 else kwargs.get('payload')
        owns_session = False
    elif len(args) >= 2:
        session = None
        user_id, flow_type = args[0], args[1]
        payload = args[2] if len(args) > 2 else kwargs.get('payload')
        owns_session = True
    else:
        session = kwargs.get('session')
        user_id = kwargs.get('user_id')
        flow_type = kwargs.get('flow_type')
        payload = kwargs.get('payload')
        owns_session = session is None

    payload_json = json.dumps(payload, ensure_ascii=False) if payload else None
    stmt = sqlite_upsert(PendingAiFlow).values(
        user_id=user_id,
        flow_type=flow_type,
        payload_json=payload_json,
        created_at=msk_now(),
    ).on_conflict_do_update(
        index_elements=['user_id'],
        set_={
            'flow_type': flow_type,
            'payload_json': payload_json,
            'created_at': msk_now(),
        }
    )

    if owns_session:
        async with async_session_maker() as s:
            await s.execute(stmt)
            await s.commit()
    else:
        await session.execute(stmt)
        await session.commit()


async def get_pending_ai_flow(
    *args,
    **kwargs,
) -> PendingAiFlow | None:
    """Supports get_pending_ai_flow(session, user_id) or get_pending_ai_flow(user_id)."""
    if len(args) == 2 and hasattr(args[0], 'execute'):
        session, user_id = args[0], args[1]
        return await session.get(PendingAiFlow, user_id)
    elif len(args) == 1:
        user_id = args[0]
        async with async_session_maker() as session:
            return await session.get(PendingAiFlow, user_id)
    else:
        session = kwargs.get('session')
        user_id = kwargs.get('user_id')
        if session:
            return await session.get(PendingAiFlow, user_id)
        async with async_session_maker() as s:
            return await s.get(PendingAiFlow, user_id)


async def consume_pending_ai_flow(
    *args,
    **kwargs,
) -> PendingAiFlow | None:
    """Atomically consumes and clears PendingAiFlow for user_id to prevent double resumption using DELETE RETURNING."""
    if len(args) == 2 and hasattr(args[0], 'execute'):
        session, user_id = args[0], args[1]
        owns_session = False
    elif len(args) == 1:
        user_id = args[0]
        owns_session = True
        session = None
    else:
        session = kwargs.get('session')
        user_id = kwargs.get('user_id')
        owns_session = session is None

    stmt = (
        delete(PendingAiFlow)
        .where(PendingAiFlow.user_id == user_id)
        .returning(
            PendingAiFlow.user_id,
            PendingAiFlow.flow_type,
            PendingAiFlow.payload_json,
            PendingAiFlow.created_at,
        )
    )

    if owns_session:
        async with async_session_maker() as s:
            res = await s.execute(stmt)
            row = res.first()
            await s.commit()
            if row:
                return PendingAiFlow(
                    user_id=row[0],
                    flow_type=row[1],
                    payload_json=row[2],
                    created_at=row[3],
                )
            return None
    else:
        res = await session.execute(stmt)
        row = res.first()
        await session.commit()
        if row:
            return PendingAiFlow(
                user_id=row[0],
                flow_type=row[1],
                payload_json=row[2],
                created_at=row[3],
            )
        return None


async def clear_pending_ai_flow(
    *args,
    **kwargs,
) -> None:
    """Supports clear_pending_ai_flow(session, user_id) or clear_pending_ai_flow(user_id)."""
    if len(args) == 2 and hasattr(args[0], 'execute'):
        session, user_id = args[0], args[1]
        flow = await session.get(PendingAiFlow, user_id)
        if flow:
            await session.delete(flow)
            await session.commit()
    elif len(args) == 1:
        user_id = args[0]
        async with async_session_maker() as session:
            flow = await session.get(PendingAiFlow, user_id)
            if flow:
                await session.delete(flow)
                await session.commit()
    else:
        session = kwargs.get('session')
        user_id = kwargs.get('user_id')
        if session:
            flow = await session.get(PendingAiFlow, user_id)
            if flow:
                await session.delete(flow)
                await session.commit()
        else:
            async with async_session_maker() as s:
                flow = await s.get(PendingAiFlow, user_id)
                if flow:
                    await s.delete(flow)
                    await s.commit()
