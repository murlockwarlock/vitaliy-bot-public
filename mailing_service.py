import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Set

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import Base

logger = logging.getLogger("mailing_service")


class MailingDeliveryLog(Base):
    __tablename__ = 'mailing_delivery_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    mailing_id = Column(Integer, ForeignKey('mailings.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    status = Column(String, nullable=False)  # 'delivered', 'failed', 'blocked', 'skipped', 'delivering'
    error = Column(Text, nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint('mailing_id', 'user_id', name='uq_mailing_delivery_user'),
    )


async def get_delivered_user_ids(session: AsyncSession, mailing_id: int) -> Set[int]:
    """Get the set of user IDs that have already been successfully processed or are actively delivering.

    Stale in-flight claims (> 300s old) are excluded from the delivered set, allowing the background
    worker to reclaim and retry them after process crashes or worker restarts.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=300)
    result = await session.execute(
        select(MailingDeliveryLog.user_id).where(
            MailingDeliveryLog.mailing_id == mailing_id,
            or_(
                MailingDeliveryLog.status.in_(['delivered', 'blocked', 'skipped']),
                and_(
                    MailingDeliveryLog.status.in_(['delivering', 'claimed']),
                    MailingDeliveryLog.sent_at >= cutoff,
                ),
            ),
        )
    )
    return set(result.scalars().all())


async def claim_mailing_recipient(
    session: AsyncSession,
    mailing_id: int,
    user_id: int,
) -> bool:
    """Atomically claims a recipient for mailing delivery BEFORE sending.

    Delivery Semantics:
    - Initial send: inserts a new record with status='delivering'. Concurrency is guarded
      by the DB unique constraint (uq_mailing_delivery_user) via savepoint.
    - Stale lease recovery (> 300s): if worker previously crashed while delivering,
      reclaims via atomic CAS matching the exact observed id, status, and sent_at.
    - Failed retry: transitions 'failed' to 'delivering' via atomic CAS.
    - Crash window semantics:
      If a crash occurs AFTER the external Telegram message is sent but BEFORE the local DB
      commits status='delivered', the stale lease recovery mechanism will eventually retry sending
      the message once the 300-second lease expires. Since the Telegram Bot API does not support
      idempotency keys or distributed 2PC transactions, the system guarantees AT-LEAST-ONCE
      delivery under hardware/process failure.
    """
    existing = await session.scalar(
        select(MailingDeliveryLog).where(
            MailingDeliveryLog.mailing_id == mailing_id,
            MailingDeliveryLog.user_id == user_id,
        )
    )
    if existing:
        if existing.status in ('delivered', 'blocked', 'skipped'):
            return False

        now_dt = datetime.utcnow()
        if existing.status in ('delivering', 'claimed'):
            cutoff = now_dt - timedelta(seconds=300)
            if existing.sent_at and existing.sent_at >= cutoff:
                # Active fresh lease held by another worker
                return False

            # Stale lease recovery (> 300s): exact CAS matching observed status and timestamp
            res = await session.execute(
                update(MailingDeliveryLog)
                .where(
                    MailingDeliveryLog.id == existing.id,
                    MailingDeliveryLog.status == existing.status,
                    MailingDeliveryLog.sent_at == existing.sent_at,
                )
                .values(
                    status='delivering',
                    sent_at=now_dt,
                    error="stale_lease_recovered",
                )
            )
            return bool(res.rowcount and res.rowcount > 0)

        if existing.status == 'failed':
            # Failed retry: exact CAS matching status='failed'
            res = await session.execute(
                update(MailingDeliveryLog)
                .where(
                    MailingDeliveryLog.id == existing.id,
                    MailingDeliveryLog.status == 'failed',
                )
                .values(
                    status='delivering',
                    sent_at=now_dt,
                    error=None,
                )
            )
            return bool(res.rowcount and res.rowcount > 0)

        return False

    log = MailingDeliveryLog(
        mailing_id=mailing_id,
        user_id=user_id,
        status='delivering',
        sent_at=datetime.utcnow(),
    )
    try:
        async with session.begin_nested():
            session.add(log)
            await session.flush()
        return True
    except IntegrityError:
        return False
    except Exception as exc:
        logger.warning("Failed to claim mailing recipient %s for %s: %s", user_id, mailing_id, exc)
        return False


async def update_delivery_status(
    session: AsyncSession,
    mailing_id: int,
    user_id: int,
    status: str,
    error: Optional[str] = None,
) -> None:
    """Updates the status and error of a claimed mailing delivery."""
    log = await session.scalar(
        select(MailingDeliveryLog).where(
            MailingDeliveryLog.mailing_id == mailing_id,
            MailingDeliveryLog.user_id == user_id,
        )
    )
    if log:
        log.status = status
        log.error = str(error)[:1000] if error else None
        log.sent_at = datetime.utcnow()
        await session.flush()
    else:
        await record_delivery_attempt(session, mailing_id, user_id, status, error=error)


async def record_delivery_attempt(
    session: AsyncSession,
    mailing_id: int,
    user_id: int,
    status: str,
    error: Optional[str] = None,
) -> bool:
    """Safely record a delivery attempt in MailingDeliveryLog.

    Guarantees idempotency via database unique constraint and savepoint.
    Returns True if recorded, False if already exists.
    """
    try:
        log = MailingDeliveryLog(
            mailing_id=mailing_id,
            user_id=user_id,
            status=status,
            error=str(error)[:1000] if error else None,
            sent_at=datetime.utcnow(),
        )
        async with session.begin_nested():
            session.add(log)
            await session.flush()
        return True
    except IntegrityError:
        return False
    except Exception as exc:
        logger.warning("Failed to record MailingDeliveryLog: %s", exc)
        return False


async def send_mailing_to_recipient(
    bot: Bot,
    uid: int,
    mailing,
    reply_markup=None,
    max_retries: int = 3,
    session: AsyncSession | None = None,
) -> tuple[bool, str, Optional[str]]:
    """Sends a single mailing message to recipient with Telegram retry and error classification.

    Returns: (success: bool, status: str, error_message: Optional[str])
    status can be 'delivered', 'blocked', 'failed', or 'skipped'.
    """
    if session is None:
        from database import async_session_maker

        async with async_session_maker() as owned_session:
            return await send_mailing_to_recipient(
                bot,
                uid,
                mailing,
                reply_markup=reply_markup,
                max_retries=max_retries,
                session=owned_session,
            )

    from crisis_safety import automatic_send_allowed

    for attempt in range(max_retries):
        if not await automatic_send_allowed(session, uid, "mailing"):
            return False, "skipped", "crisis_flag"
        try:
            if mailing.media_file_id:
                caption_text = mailing.text
                if caption_text and len(caption_text) > 1024:
                    if mailing.media_file_type == 'photo':
                        await bot.send_photo(uid, mailing.media_file_id)
                    else:
                        await bot.send_video(uid, mailing.media_file_id)
                    await bot.send_message(uid, caption_text, reply_markup=reply_markup, parse_mode='HTML')
                else:
                    if mailing.media_file_type == 'photo':
                        await bot.send_photo(uid, mailing.media_file_id, caption=caption_text, reply_markup=reply_markup, parse_mode='HTML')
                    else:
                        await bot.send_video(uid, mailing.media_file_id, caption=caption_text, reply_markup=reply_markup, parse_mode='HTML')
            else:
                await bot.send_message(uid, mailing.text, reply_markup=reply_markup, parse_mode='HTML')
            return True, "delivered", None
        except TelegramRetryAfter as retry_err:
            wait_time = min(retry_err.retry_after, 60)
            logger.info("TelegramRetryAfter for uid=%s: waiting %ss", uid, wait_time)
            await asyncio.sleep(wait_time)
        except TelegramForbiddenError as forbidden_err:
            logger.info("User uid=%s blocked bot or deleted chat: %s", uid, forbidden_err)
            return False, "blocked", str(forbidden_err)
        except TelegramBadRequest as bad_req_err:
            logger.warning("TelegramBadRequest for uid=%s: %s", uid, bad_req_err)
            return False, "failed", str(bad_req_err)
        except Exception as exc:
            logger.warning("Error delivering mailing to uid=%s (attempt %s/%s): %s", uid, attempt + 1, max_retries, exc)
            if attempt == max_retries - 1:
                return False, "failed", str(exc)
            await asyncio.sleep(0.5)

    return False, "failed", "max retries exceeded"
