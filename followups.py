import asyncio
import html
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import relationship, selectinload

from database import Base, User, async_session_maker
from time_utils import msk_now
from crisis_safety import automatic_send_allowed

logger = logging.getLogger("followups")


class FollowupCampaign(Base):
    """Generic inactivity follow-up campaign."""
    __tablename__ = 'followup_campaigns'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    scope = Column(String, default='global', nullable=False, index=True)  # 'global', 'main', 'topic', 'funnel'
    topic_slug = Column(String, nullable=True, index=True)
    is_active = Column(Integer, default=1, nullable=False)  # 1 = active, 0 = paused
    quiet_hours_start = Column(Integer, default=23, nullable=False)  # MSK hour
    quiet_hours_end = Column(Integer, default=9, nullable=False)    # MSK hour
    created_at = Column(DateTime, default=msk_now, nullable=False)

    steps = relationship(
        "FollowupStep",
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="FollowupStep.step_number",
    )


class FollowupStep(Base):
    """A single sequential step inside a follow-up campaign."""
    __tablename__ = 'followup_steps'

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(Integer, ForeignKey('followup_campaigns.id', ondelete='CASCADE'), nullable=False, index=True)
    step_number = Column(Integer, default=1, nullable=False)
    delay_minutes = Column(Integer, default=60, nullable=False)
    step_type = Column(String, default='static', nullable=False)  # 'static' | 'ai'
    message_text = Column(Text, nullable=True)
    ai_instruction = Column(Text, nullable=True)
    button_text = Column(String, nullable=True)
    button_action = Column(String, nullable=True)

    campaign = relationship("FollowupCampaign", back_populates="steps")


class FollowupRun(Base):
    """Lifecycle tracking for a specific follow-up sequence execution."""
    __tablename__ = 'followup_runs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(Integer, ForeignKey('followup_campaigns.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    dialogue_id = Column(Integer, default=1, nullable=False, index=True)
    run_number = Column(Integer, default=1, nullable=False)
    status = Column(String, default='active', nullable=False, index=True)  # 'active', 'completed', 'cancelled'
    created_at = Column(DateTime, default=msk_now, nullable=False)
    updated_at = Column(DateTime, default=msk_now, onupdate=msk_now, nullable=False)

    campaign = relationship("FollowupCampaign", backref="runs")
    deliveries = relationship("FollowupDelivery", back_populates="run", cascade="all, delete-orphan")

    __table_args__ = (
        Index(
            'uq_active_followup_run',
            'campaign_id',
            'user_id',
            'dialogue_id',
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )


class FollowupDelivery(Base):
    """Immutable delivery log guaranteeing that no step is sent twice per follow-up run."""
    __tablename__ = 'followup_deliveries'

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey('followup_runs.id', ondelete='CASCADE'), nullable=True, index=True)
    campaign_id = Column(Integer, ForeignKey('followup_campaigns.id', ondelete='CASCADE'), nullable=False, index=True)
    step_id = Column(Integer, ForeignKey('followup_steps.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    dialogue_id = Column(Integer, default=1, nullable=False)
    status = Column(String, default='delivered', nullable=False)  # 'delivered', 'failed', 'blocked', 'delivering'
    delivered_at = Column(DateTime, default=msk_now, nullable=False, index=True)
    claim_token = Column(String(64), nullable=True, index=True)
    error = Column(Text, nullable=True)

    run = relationship("FollowupRun", back_populates="deliveries")

    __table_args__ = (
        UniqueConstraint('run_id', 'step_id', name='uq_followup_run_step'),
    )


def is_in_quiet_hours(now_dt: datetime, start_hour: int, end_hour: int) -> bool:
    """Returns True if the current MSK hour falls within the quiet hours window."""
    h = now_dt.hour
    if start_hour > end_hour:
        # e.g., 23:00 to 09:00
        return h >= start_hour or h < end_hour
    elif start_hour < end_hour:
        # e.g., 01:00 to 06:00
        return start_hour <= h < end_hour
    return False


def _followup_scope_matches_user(campaign: FollowupCampaign, user: User) -> bool:
    if campaign.scope == "main":
        return user.current_topic_slug is None
    if campaign.scope == "topic":
        return bool(campaign.topic_slug) and user.current_topic_slug == campaign.topic_slug
    return True


async def _refresh_followup_user(session: AsyncSession, user_id: int) -> Optional[User]:
    result = await session.execute(
        select(User)
        .where(User.id == user_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_or_create_active_followup_run(
    session: AsyncSession,
    *,
    campaign_id: int,
    user_id: int,
    dialogue_id: int,
    user_last_activity_at: Optional[datetime] = None,
) -> Optional[FollowupRun]:
    """Finds active follow-up run for (campaign_id, user_id, dialogue_id) or creates a new one in a concurrency-safe manner.

    Prevents completed follow-up restart loops: if the most recent run was completed or blocked,
    a new run will NOT be started unless the user has exhibited fresh activity since the run completed.
    """
    last_run = await session.scalar(
        select(FollowupRun).where(
            FollowupRun.campaign_id == campaign_id,
            FollowupRun.user_id == user_id,
            FollowupRun.dialogue_id == dialogue_id,
        ).order_by(FollowupRun.id.desc()).limit(1)
    )
    if last_run is not None:
        if last_run.status == 'active':
            return last_run
        if last_run.status in ('completed', 'blocked'):
            if user_last_activity_at is None:
                user_obj = await session.get(User, user_id)
                user_last_activity_at = user_obj.last_activity_at if user_obj else None
            completion_time = last_run.updated_at or last_run.created_at
            if user_last_activity_at is None or user_last_activity_at <= completion_time:
                # No new user activity since campaign completion -> do not restart
                return None

    try:
        async with session.begin_nested():
            last_num = await session.scalar(
                select(func.max(FollowupRun.run_number)).where(
                    FollowupRun.campaign_id == campaign_id,
                    FollowupRun.user_id == user_id,
                    FollowupRun.dialogue_id == dialogue_id,
                )
            )
            next_num = (last_num or 0) + 1
            run = FollowupRun(
                campaign_id=campaign_id,
                user_id=user_id,
                dialogue_id=dialogue_id,
                run_number=next_num,
                status='active',
                created_at=msk_now(),
                updated_at=msk_now(),
            )
            session.add(run)
            await session.flush()
        await session.commit()
        return run
    except (IntegrityError, Exception) as exc:
        logger.debug("Caught concurrent FollowupRun creation: %s", exc)

    existing_run = await session.scalar(
        select(FollowupRun).where(
            FollowupRun.campaign_id == campaign_id,
            FollowupRun.user_id == user_id,
            FollowupRun.dialogue_id == dialogue_id,
            FollowupRun.status == 'active',
        ).order_by(FollowupRun.id.desc()).limit(1)
    )
    if existing_run is not None:
        return existing_run

    return run


async def reset_user_followups(
    session: AsyncSession,
    user_id: int,
    dialogue_id: Optional[int] = None,
    campaign_id: Optional[int] = None,
) -> int:
    """Cancels active follow-up runs when user shows activity.

    Allows campaigns to cleanly reset and begin from step 1 on future inactivity cycles.
    """
    stmt = (
        update(FollowupRun)
        .where(
            FollowupRun.user_id == user_id,
            FollowupRun.status == 'active',
        )
        .values(
            status='cancelled',
            updated_at=msk_now(),
        )
    )
    if dialogue_id is not None:
        stmt = stmt.where(FollowupRun.dialogue_id == dialogue_id)
    if campaign_id is not None:
        stmt = stmt.where(FollowupRun.campaign_id == campaign_id)
    res = await session.execute(stmt)
    return res.rowcount or 0


async def is_step_already_delivered(
    session: AsyncSession,
    *,
    step_id: int,
    user_id: int,
    dialogue_id: int,
    campaign_id: Optional[int] = None,
    run_id: Optional[int] = None,
) -> bool:
    """Checks whether the step has already been delivered in the current active run.
    Failed steps return False to allow retrying per policy.
    Blocked steps return True (terminal).
    Delivering steps return True only if fresh (< 5m), allowing stale recovery.
    """
    if run_id is None:
        if campaign_id is None:
            step = await session.get(FollowupStep, step_id)
            if step:
                campaign_id = step.campaign_id
            else:
                active_runs = (await session.execute(
                    select(FollowupRun.id).where(
                        FollowupRun.user_id == user_id,
                        FollowupRun.dialogue_id == dialogue_id,
                        FollowupRun.status == 'active',
                    )
                )).scalars().all()
                if not active_runs:
                    return False
                delivery = await session.scalar(
                    select(FollowupDelivery).where(
                        FollowupDelivery.step_id == step_id,
                        FollowupDelivery.run_id.in_(active_runs),
                    )
                )
                if not delivery:
                    return False
                if delivery.status in ('delivered', 'blocked'):
                    return True
                if delivery.status == 'delivering':
                    return (msk_now() - delivery.delivered_at).total_seconds() < 300
                return False

        active_run = await session.scalar(
            select(FollowupRun).where(
                FollowupRun.campaign_id == campaign_id,
                FollowupRun.user_id == user_id,
                FollowupRun.dialogue_id == dialogue_id,
                FollowupRun.status == 'active',
            ).order_by(FollowupRun.id.desc()).limit(1)
        )
        if active_run is None:
            return False
        run_id = active_run.id

    delivery = await session.scalar(
        select(FollowupDelivery).where(
            FollowupDelivery.run_id == run_id,
            FollowupDelivery.step_id == step_id,
        )
    )
    if delivery is None:
        return False
    if delivery.status in ('delivered', 'blocked'):
        return True
    if delivery.status == 'delivering':
        return (msk_now() - delivery.delivered_at).total_seconds() < 300
    # status == 'failed' -> return False to allow retry
    return False


async def record_followup_delivery(
    session: AsyncSession,
    *,
    campaign_id: int,
    step_id: int,
    user_id: int,
    dialogue_id: int,
    run_id: Optional[int] = None,
    status: str = "delivered",
    error: Optional[str] = None,
) -> bool:
    """Records delivery attempt with DB-level idempotency protection per run.
    Supports updating existing 'delivering' or 'failed' records to avoid unique constraint conflicts on retry.
    """
    try:
        if run_id is None:
            active_run = await get_or_create_active_followup_run(
                session,
                campaign_id=campaign_id,
                user_id=user_id,
                dialogue_id=dialogue_id,
            )
            run_id = active_run.id

        existing = await session.scalar(
            select(FollowupDelivery).where(
                FollowupDelivery.run_id == run_id,
                FollowupDelivery.step_id == step_id,
            )
        )
        if existing:
            if existing.status in ('delivered', 'blocked'):
                return False
            existing.status = status
            existing.error = str(error)[:1000] if error else None
            existing.delivered_at = msk_now()
            await session.flush()
            return True

        delivery = FollowupDelivery(
            run_id=run_id,
            campaign_id=campaign_id,
            step_id=step_id,
            user_id=user_id,
            dialogue_id=dialogue_id,
            status=status,
            delivered_at=msk_now(),
            error=str(error)[:1000] if error else None,
        )
        session.add(delivery)
        await session.flush()
        return True
    except IntegrityError as exc:
        logger.warning("IntegrityError recording FollowupDelivery: %s", exc)
        await session.rollback()
        return False
    except Exception as exc:
        await session.rollback()
        logger.warning("Error recording FollowupDelivery: %s", exc)
        return False


from md_utils import strip_telegram_html


async def send_followup_step_to_user(
    bot: Bot,
    session: AsyncSession,
    *,
    step: FollowupStep,
    user: User,
    dialogue_id: int,
) -> Tuple[bool, str, Optional[str]]:
    """Sends a follow-up step to user and returns (success, status, error)."""
    if not await automatic_send_allowed(session, user.id, "generic_followup"):
        return False, "skipped", "crisis_flag"
    reply_markup = None
    if step.button_text and step.button_action:
        kb = InlineKeyboardBuilder()
        if step.button_action.startswith('http') or step.button_action.startswith('tg://'):
            kb.button(text=step.button_text, url=step.button_action)
        else:
            kb.button(text=step.button_text, callback_data=step.button_action)
        reply_markup = kb.as_markup()

    text_to_send = step.message_text or ""
    is_ai_step = (step.step_type == "ai" and bool(step.ai_instruction))
    if is_ai_step:
        try:
            from ai_integration import get_standalone_ai_response
            raw_user_name = getattr(user, 'name', None) or getattr(user, 'first_name', None) or "пользователь"
            ai_prompt = (
                f"Напиши теплое короткое напоминание пользователю по имени {raw_user_name}.\n"
                f"Инструкция: {step.ai_instruction}"
            )
            text_to_send = await get_standalone_ai_response(
                system_prompt="Ты бережный помощник и коуч. Напиши краткое напоминание пользователю.",
                user_prompt=ai_prompt,
            )
        except Exception as exc:
            logger.warning("AI generation failed for follow-up step id=%s: %s", step.id, exc)
            text_to_send = step.message_text or "Привет! Напоминаю о нашей практике. Буду рад продолжить!"

    if not text_to_send:
        return False, "skipped", "empty text"

    raw_user_name = getattr(user, 'name', None) or getattr(user, 'first_name', None) or "друг"
    safe_user_name = html.escape(raw_user_name)

    if is_ai_step:
        # AI output is plain text — do not trust LLM output as HTML markup; escape before sending
        clean_text = html.escape(text_to_send).replace("{name}", safe_user_name).replace("{first_name}", safe_user_name)
    else:
        # Static admin template: preserve trusted template HTML markup, escape dynamic placeholder
        clean_text = text_to_send.replace("{name}", safe_user_name).replace("{first_name}", safe_user_name)

    async def _dispatch_message(text: str) -> None:
        try:
            await bot.send_message(
                user.id,
                text,
                reply_markup=reply_markup,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramBadRequest as b_err:
            err_msg = str(b_err).lower()
            if "can't parse entities" in err_msg or "entity" in err_msg:
                logger.warning("Malformed HTML in followup step id=%s, falling back to clean plain text: %s", step.id, b_err)
                plain_fallback = strip_telegram_html(text_to_send).replace("{name}", raw_user_name).replace("{first_name}", raw_user_name)
                await bot.send_message(
                    user.id,
                    plain_fallback,
                    reply_markup=reply_markup,
                    parse_mode=None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            else:
                raise

    try:
        await _dispatch_message(clean_text)
        return True, "delivered", None
    except TelegramRetryAfter as e:
        await asyncio.sleep(min(e.retry_after, 60))
        if not await automatic_send_allowed(session, user.id, "generic_followup"):
            return False, "skipped", "crisis_flag"
        try:
            await _dispatch_message(clean_text)
            return True, "delivered", None
        except Exception as e2:
            return False, "failed", str(e2)
    except TelegramForbiddenError as e:
        return False, "blocked", str(e)
    except TelegramBadRequest as e:
        return False, "failed", str(e)
    except Exception as e:
        return False, "failed", str(e)


async def process_generic_followups(
    bot: Bot,
    session_maker=None,
    scope: Optional[str] = None,
    user_id: Optional[int] = None,
) -> int:
    """One pass of the generic follow-up background worker.

    Evaluates inactive users across active campaigns with:
    - Quiet hours (MSK)
    - Sequential state-machine semantics: at most ONE step per user per pass.
    - Step 2 is NEVER sent until Step 1 is delivered according to policy.
    - Step 2 delay is measured from Step 1 delivery time.
    - Atomic DB claim BEFORE external Telegram send (prevents concurrency duplicates).
    - Retry policy for failed deliveries (does not block retry).
    - Terminal handling for blocked users.
    Returns count of delivered messages.
    """
    now = msk_now()
    delivered_count = 0
    sm = session_maker or async_session_maker

    async with sm() as session:
        campaign_query = (
            select(FollowupCampaign)
            .where(FollowupCampaign.is_active == 1)
            .options(selectinload(FollowupCampaign.steps))
        )
        if scope is not None:
            campaign_query = campaign_query.where(FollowupCampaign.scope == scope)

        campaigns = (await session.execute(campaign_query)).scalars().all()

        for campaign in campaigns:
            if not campaign.steps:
                continue

            if is_in_quiet_hours(now, campaign.quiet_hours_start, campaign.quiet_hours_end):
                logger.debug("Campaign %s is in quiet hours, skipping pass.", campaign.name)
                continue

            sorted_steps = sorted(campaign.steps, key=lambda s: s.step_number)

            query = select(User).where(
                User.is_blocked == False,
                User.last_activity_at != None,
                User.crisis_detected_at == None,
            )
            if user_id is not None:
                query = query.where(User.id == user_id)
            if campaign.scope == "main":
                query = query.where(User.current_topic_slug.is_(None))
            elif campaign.scope == "topic":
                if not campaign.topic_slug:
                    continue
                query = query.where(User.current_topic_slug == campaign.topic_slug)

            users = (await session.execute(query)).scalars().all()

            for user in users:
                fresh_user = await _refresh_followup_user(session, user.id)
                if fresh_user is None or not _followup_scope_matches_user(campaign, fresh_user):
                    continue
                user = fresh_user
                dialogue_id = user.current_dialogue_id or 1

                active_run = await get_or_create_active_followup_run(
                    session,
                    campaign_id=campaign.id,
                    user_id=user.id,
                    dialogue_id=dialogue_id,
                    user_last_activity_at=user.last_activity_at,
                )
                if active_run is None:
                    continue

                # Reset stale run if user was active after run start
                if user.last_activity_at and active_run.created_at < user.last_activity_at:
                    active_run.status = 'cancelled'
                    await session.flush()
                    await session.commit()
                    active_run = await get_or_create_active_followup_run(
                        session,
                        campaign_id=campaign.id,
                        user_id=user.id,
                        dialogue_id=dialogue_id,
                        user_last_activity_at=user.last_activity_at,
                    )
                    if active_run is None:
                        continue

                # Fetch all existing deliveries for this active run
                run_deliveries = (await session.execute(
                    select(FollowupDelivery).where(FollowupDelivery.run_id == active_run.id)
                )).scalars().all()
                delivery_map = {d.step_id: d for d in run_deliveries}

                # Evaluate sequential state machine to find the EXACT single next step
                step_to_send: Optional[FollowupStep] = None
                existing_delivery_for_step: Optional[FollowupDelivery] = None

                for i, step in enumerate(sorted_steps):
                    deliv = delivery_map.get(step.id)
                    if deliv:
                        if deliv.status == 'delivered':
                            # Predecessor step completed; check next step
                            continue
                        elif deliv.status == 'blocked':
                            # User blocked bot; terminal stop for this run
                            active_run.status = 'blocked'
                            await session.flush()
                            await session.commit()
                            break
                        elif deliv.status == 'delivering':
                            if (now - deliv.delivered_at).total_seconds() < 300:
                                # Fresh in-flight claim by another worker; do not touch
                                break
                            # Stale in-flight (> 5m); recover and retry
                            step_to_send = step
                            existing_delivery_for_step = deliv
                            break
                        elif deliv.status == 'failed':
                            # Failed delivery: retryable!
                            step_to_send = step
                            existing_delivery_for_step = deliv
                            break
                    else:
                        # Step not yet attempted
                        if i == 0:
                            # Step 1: user must be inactive for step.delay_minutes
                            ref_time = user.last_activity_at or active_run.created_at
                            if now < ref_time + timedelta(minutes=step.delay_minutes):
                                break
                            step_to_send = step
                            existing_delivery_for_step = None
                            break
                        else:
                            # Step N > 1: Step N-1 MUST be delivered!
                            prev_step = sorted_steps[i - 1]
                            prev_deliv = delivery_map.get(prev_step.id)
                            if not prev_deliv or prev_deliv.status != 'delivered':
                                # Cannot proceed until previous step is delivered
                                break
                            # Delay for step N is measured from Step N-1 delivery time
                            if now < prev_deliv.delivered_at + timedelta(minutes=step.delay_minutes):
                                break
                            step_to_send = step
                            existing_delivery_for_step = None
                            break

                if step_to_send is None:
                    continue

                # ATOMIC CAS CLAIM BEFORE EXTERNAL SEND (Item 4)
                claimed = False
                claim_uid = user.id
                claim_sid = step_to_send.id
                new_claim_token = uuid.uuid4().hex
                now_deliv = msk_now()
                target_deliv_id = None
                try:
                    if existing_delivery_for_step is None:
                        async with session.begin_nested():
                            claim_record = FollowupDelivery(
                                run_id=active_run.id,
                                campaign_id=campaign.id,
                                step_id=step_to_send.id,
                                user_id=user.id,
                                dialogue_id=dialogue_id,
                                status='delivering',
                                delivered_at=now_deliv,
                                claim_token=new_claim_token,
                            )
                            session.add(claim_record)
                            await session.flush()
                            target_deliv_id = claim_record.id
                        await session.commit()
                        claimed = True
                    else:
                        obs_id = existing_delivery_for_step.id
                        obs_status = existing_delivery_for_step.status
                        obs_delivered_at = existing_delivery_for_step.delivered_at
                        obs_claim_token = existing_delivery_for_step.claim_token

                        if obs_status == 'failed':
                            # Exact CAS: transition only from 'failed' to 'delivering'
                            res = await session.execute(
                                update(FollowupDelivery)
                                .where(
                                    FollowupDelivery.id == obs_id,
                                    FollowupDelivery.status == 'failed',
                                )
                                .values(
                                    status='delivering',
                                    delivered_at=now_deliv,
                                    claim_token=new_claim_token,
                                    error=None,
                                )
                            )
                        elif obs_status == 'delivering':
                            # Exact CAS for stale lease recovery: match expired lease and token
                            stale_cutoff = now - timedelta(seconds=300)
                            conds = [
                                FollowupDelivery.id == obs_id,
                                FollowupDelivery.status == 'delivering',
                                FollowupDelivery.delivered_at <= stale_cutoff,
                            ]
                            if obs_claim_token:
                                conds.append(FollowupDelivery.claim_token == obs_claim_token)
                            res = await session.execute(
                                update(FollowupDelivery)
                                .where(*conds)
                                .values(
                                    status='delivering',
                                    delivered_at=now_deliv,
                                    claim_token=new_claim_token,
                                    error=None,
                                )
                            )
                        else:
                            res = None

                        if res is not None and res.rowcount and res.rowcount > 0:
                            await session.commit()
                            claimed = True
                            target_deliv_id = obs_id
                        else:
                            await session.rollback()
                            claimed = False
                except (IntegrityError, Exception) as exc:
                    await session.rollback()
                    logger.debug("Followup claim conflict for user=%s step=%s: %s", claim_uid, claim_sid, exc)
                    claimed = False

                if not claimed or not target_deliv_id:
                    continue

                fresh_user = await _refresh_followup_user(session, user.id)
                if fresh_user is None or not _followup_scope_matches_user(campaign, fresh_user):
                    async with sm() as finish_session:
                        d_row = await finish_session.scalar(
                            select(FollowupDelivery).where(
                                FollowupDelivery.id == target_deliv_id,
                                FollowupDelivery.claim_token == new_claim_token,
                            )
                        )
                        if d_row:
                            d_row.status = "failed"
                            d_row.error = "scope_mismatch_before_send"
                            d_row.delivered_at = msk_now()
                        await finish_session.commit()
                    continue
                user = fresh_user

                # Claim won: invoke external send
                ok, status, err = await send_followup_step_to_user(
                    bot,
                    session,
                    step=step_to_send,
                    user=user,
                    dialogue_id=dialogue_id,
                )

                async with sm() as finish_session:
                    d_row = await finish_session.scalar(
                        select(FollowupDelivery).where(
                            FollowupDelivery.id == target_deliv_id,
                            FollowupDelivery.claim_token == new_claim_token,
                        )
                    )
                    if d_row:
                        d_row.status = status
                        d_row.error = str(err)[:1000] if err else None
                        d_row.delivered_at = msk_now()
                    if status == 'delivered':
                        delivered_count += 1
                        if step_to_send.step_number >= max((s.step_number for s in sorted_steps), default=1):
                            r = await finish_session.get(FollowupRun, active_run.id)
                            if r:
                                r.status = 'completed'
                                r.updated_at = msk_now()
                    elif status == 'blocked':
                        r = await finish_session.get(FollowupRun, active_run.id)
                        if r:
                            r.status = 'blocked'
                            r.updated_at = msk_now()
                        u = await finish_session.get(User, user.id)
                        if u:
                            u.is_blocked = True
                    await finish_session.commit()

                await asyncio.sleep(0.05)

    return delivered_count


async def followup_worker(bot: Bot) -> None:
    """Long-running background worker executing follow-up campaigns."""
    while True:
        try:
            await process_generic_followups(bot)
        except Exception as exc:
            logger.error("Error in followup_worker: %s", exc, exc_info=True)
        await asyncio.sleep(60)
