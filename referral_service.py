import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, String, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import Base, User, UserSubscription
from time_utils import msk_now

logger = logging.getLogger("referral_service")


class ReferralRewardGrant(Base):
    """Database-enforced idempotency log for all referral benefit grants.

    Guarantees that no referral registration reward or payment commission
    can be granted twice, even across concurrent /start updates, webhook retries,
    or worker restarts.
    """
    __tablename__ = 'referral_reward_grants'

    id = Column(Integer, primary_key=True, autoincrement=True)
    grant_key = Column(String, unique=True, nullable=False, index=True)
    referrer_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    referred_user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    reward_type = Column(String, nullable=False)  # 'registration_points', 'payment_points', 'payment_bonus_days'
    points_amount = Column(Integer, default=0, nullable=False)
    bonus_days = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


async def grant_registration_reward(
    session: AsyncSession,
    *,
    referrer_id: int,
    referred_user_id: int,
    points: int,
) -> bool:
    """Atomically grants referral registration points to the referrer.

    Ensures:
    1. No self-referral (referrer_id != referred_user_id).
    2. Strictly idempotent at DB level using UNIQUE grant_key in a savepoint.
    3. Concurrency-safe atomic SQL balance increment.

    Returns True if reward was newly granted, False if already granted or invalid.
    """
    if referrer_id == referred_user_id:
        logger.info("Ignoring self-referral reward for user_id=%s", referrer_id)
        return False

    if points <= 0:
        return False

    grant_key = f"ref-registration:{referrer_id}:{referred_user_id}"

    # Check existing grant first to avoid unnecessary failures
    existing = await session.scalar(
        select(ReferralRewardGrant).where(ReferralRewardGrant.grant_key == grant_key)
    )
    if existing:
        logger.info("Referral registration grant %s already exists, skipping.", grant_key)
        return False

    grant = ReferralRewardGrant(
        grant_key=grant_key,
        referrer_id=referrer_id,
        referred_user_id=referred_user_id,
        reward_type="registration_points",
        points_amount=points,
        bonus_days=0,
        created_at=datetime.utcnow(),
    )

    try:
        async with session.begin_nested():
            session.add(grant)
            await session.flush()
    except IntegrityError:
        logger.info("Concurrent insert caught by unique constraint for grant_key=%s", grant_key)
        return False

    # Atomic SQL increment prevents lost update under high concurrency
    res = await session.execute(
        update(User)
        .where(User.id == referrer_id)
        .values(referral_balance=func.coalesce(User.referral_balance, 0) + points)
    )
    await session.flush()
    ref_user = await session.get(User, referrer_id)
    if ref_user is not None:
        await session.refresh(ref_user)
    return res.rowcount > 0


async def grant_payment_points_reward(
    session: AsyncSession,
    *,
    referrer_id: int,
    referred_user_id: int,
    payment_key: str,
    points: int,
) -> bool:
    """Atomically grants payment commission points to the referrer with DB uniqueness."""
    if referrer_id == referred_user_id or points <= 0 or not payment_key:
        return False

    grant_key = f"ref-payment-points:{payment_key}:{referrer_id}"

    existing = await session.scalar(
        select(ReferralRewardGrant).where(ReferralRewardGrant.grant_key == grant_key)
    )
    if existing:
        return False

    grant = ReferralRewardGrant(
        grant_key=grant_key,
        referrer_id=referrer_id,
        referred_user_id=referred_user_id,
        reward_type="payment_points",
        points_amount=points,
        bonus_days=0,
        created_at=datetime.utcnow(),
    )

    try:
        async with session.begin_nested():
            session.add(grant)
            await session.flush()
    except IntegrityError:
        return False

    # Atomic SQL increment prevents lost update under high concurrency
    res = await session.execute(
        update(User)
        .where(User.id == referrer_id)
        .values(referral_balance=func.coalesce(User.referral_balance, 0) + points)
    )
    await session.flush()
    ref_user = await session.get(User, referrer_id)
    if ref_user is not None:
        await session.refresh(ref_user)
    return res.rowcount > 0


async def grant_payment_bonus_days_reward(
    session: AsyncSession,
    *,
    referrer_id: int,
    referred_user_id: int,
    payment_key: str,
    bonus_days: int,
) -> bool:
    """Atomically grants subscription bonus days to the referrer with DB uniqueness."""
    if referrer_id == referred_user_id or bonus_days <= 0 or not payment_key:
        return False

    grant_key = f"ref-payment-days:{payment_key}:{referrer_id}"

    existing = await session.scalar(
        select(ReferralRewardGrant).where(ReferralRewardGrant.grant_key == grant_key)
    )
    if existing:
        return False

    grant = ReferralRewardGrant(
        grant_key=grant_key,
        referrer_id=referrer_id,
        referred_user_id=referred_user_id,
        reward_type="payment_bonus_days",
        points_amount=0,
        bonus_days=bonus_days,
        created_at=datetime.utcnow(),
    )

    try:
        async with session.begin_nested():
            session.add(grant)
            await session.flush()
    except IntegrityError:
        return False

    now = msk_now()
    # Concurrency-safe: lock row where supported
    try:
        referrer_sub = await session.scalar(
            select(UserSubscription)
            .where(UserSubscription.user_id == referrer_id)
            .with_for_update()
        )
    except Exception:
        referrer_sub = await session.scalar(
            select(UserSubscription).where(UserSubscription.user_id == referrer_id)
        )

    if referrer_sub and referrer_sub.end_date and referrer_sub.end_date > now:
        referrer_sub.end_date += timedelta(days=bonus_days)
        new_end = referrer_sub.end_date
    elif referrer_sub:
        referrer_sub.plan_id = None
        referrer_sub.start_date = now
        referrer_sub.end_date = now + timedelta(days=bonus_days)
        referrer_sub.payment_provider = "Referral Bonus"
        referrer_sub.auto_renewal = False
        referrer_sub.payment_attempt_count = 0
        new_end = referrer_sub.end_date
    else:
        new_end = now + timedelta(days=bonus_days)
        session.add(UserSubscription(
            user_id=referrer_id,
            plan_id=None,
            start_date=now,
            end_date=new_end,
            auto_renewal=False,
            payment_provider="Referral Bonus",
            payment_attempt_count=0,
            discount_percent=0,
        ))

    # Also update User.subscription_end if user exists
    referrer_user = await session.get(User, referrer_id)
    if referrer_user:
        if referrer_user.subscription_end and referrer_user.subscription_end > now:
            referrer_user.subscription_end += timedelta(days=bonus_days)
        else:
            referrer_user.subscription_end = new_end

    await session.flush()
    return True
