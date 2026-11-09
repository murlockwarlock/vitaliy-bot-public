"""
Subscription scheduler - проверка подписок, уведомления об истечении,
напоминания о продлении.
Запускается каждые 15 минут.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import (
    async_session_maker, UserSubscription, SubscriptionPlan, SubscriptionConfig,
    User, PromoCode, get_all_admin_ids
)
from config import OWNER_IDS
from time_utils import msk_now, format_msk
from subscription_notifications import should_send_upcoming_charge_notification
from subscription_retry_policy import can_retry_now, get_next_retry_at
from crisis_safety import automatic_send_allowed

log = logging.getLogger("subscriptions")
plog = logging.getLogger("payments")
EXPIRATION_DEDUP_WINDOW = timedelta(hours=2)
MSK = timezone(timedelta(hours=3))


def _to_msk(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK)


def _format_msk(dt: datetime, fmt: str = "%d.%m.%Y %H:%M МСК") -> str:
    return _to_msk(dt).strftime(fmt)


def _payment_log_path() -> str:
    return os.path.join(os.path.dirname(__file__), "logs", "payment_events.log")


def _format_user_ref(user: User, user_id: int) -> str:
    user_ref = user.first_name or ""
    if user.username:
        user_ref += f" (@{user.username})"
    user_ref += f" [id=<code>{user_id}</code>]"
    return user_ref


def _was_recent_notification_logged(recipient_id: int, key: str, now: datetime, window: timedelta) -> bool:
    log_path = _payment_log_path()
    if not os.path.exists(log_path):
        return False

    event_suffix = f"NOTIFY_SENT | recipient_id={recipient_id} | key={key}"
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            recent_lines = f.readlines()[-500:]
    except OSError:
        return False

    now_local = _to_msk(now).replace(tzinfo=None)
    for line in reversed(recent_lines):
        line = line.strip()
        if event_suffix not in line:
            continue
        timestamp_raw = line.split(" | ", 1)[0]
        try:
            logged_at = datetime.strptime(timestamp_raw, "%d.%m.%Y %H:%M:%S")
        except ValueError:
            continue
        if now_local - logged_at <= window:
            return True
    return False


async def _send_deduplicated_notification(
        bot: Bot,
        recipient_id: int,
        text: str,
        key: str,
        now: datetime,
        *,
        reply_markup=None,
        window: timedelta = EXPIRATION_DEDUP_WINDOW,
) -> bool:
    if _was_recent_notification_logged(recipient_id, key, now, window):
        return False
    async with async_session_maker() as session:
        if not await automatic_send_allowed(session, recipient_id, "subscription_scheduler"):
            return False
    try:
        await bot.send_message(recipient_id, text, reply_markup=reply_markup)
    except Exception:
        return False
    plog.info(f"NOTIFY_SENT | recipient_id={recipient_id} | key={key}")
    return True


async def check_subscriptions(bot: Bot):
    """Основная функция проверки подписок."""
    log.info("Running subscription check...")
    now = msk_now()
    two_hours_later = now + timedelta(hours=2)
    lookback_cutoff = now - timedelta(days=30)
    notification_threshold = now - timedelta(hours=1, minutes=15)

    subscribe_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Продлить доступ к программе", callback_data="show_subscription_info_from_chat")]
    ])

    async with async_session_maker() as session:
        config = await session.get(SubscriptionConfig, 1)
        if not config:
            return

        all_admin_ids = await get_all_admin_ids()

        stmt = select(UserSubscription).where(
            UserSubscription.end_date > lookback_cutoff
        ).options(
            selectinload(UserSubscription.plan).selectinload(SubscriptionPlan.upgrades_to_plan)
        )

        result = await session.execute(stmt)
        subscriptions = result.scalars().all()

        for sub in subscriptions:
            try:
                user = await session.get(User, sub.user_id, options=[
                    selectinload(User.promo_codes).selectinload(PromoCode.applicable_plans)])
                if not user:
                    continue

                user_ref = _format_user_ref(user, sub.user_id)

                message_text = None
                is_trial_promo = sub.plan_id is None and sub.payment_provider in ['Trial Promo', 'Trial Welcome']
                time_left = sub.end_date - now

                two_day_window_start = timedelta(days=2)
                two_day_window_end = timedelta(days=2, hours=1)
                one_day_window_start = timedelta(days=1)
                one_day_window_end = timedelta(days=1, hours=1)
                two_hour_window_start = timedelta(hours=2)
                two_hour_window_end = timedelta(hours=3)

                should_send = False
                reminder_bucket = None
                if two_day_window_start <= time_left < two_day_window_end:
                    should_send = True
                    reminder_bucket = "2d"
                elif one_day_window_start <= time_left < one_day_window_end:
                    should_send = True
                    reminder_bucket = "1d"
                elif two_hour_window_start <= time_left < two_hour_window_end:
                    should_send = True
                    reminder_bucket = "2h"

                date_str = format_msk(sub.end_date)

                if should_send:
                    if is_trial_promo:
                        discount = sub.discount_percent
                        remaining_seconds = time_left.total_seconds()
                        if remaining_seconds > 86400:
                            d_val = int(remaining_seconds // 86400)
                            time_display = f"{d_val} дн."
                        elif remaining_seconds > 3600:
                            h_val = int(remaining_seconds // 3600)
                            time_display = f"{h_val} ч."
                        else:
                            time_display = "менее часа"

                        message_text = f"Твой пробный период истекает {date_str} (через {time_display})."
                        if discount > 0:
                            message_text += f"\n\nОформи доступ к образовательной программе, чтобы сохранить скидку {discount}%!"
                        else:
                            message_text += "\n\nОформи доступ к образовательной программе для продолжения работы."

                    elif sub.auto_renewal and sub.plan:
                        if sub.payment_provider == "Prodamus" and sub.prodamus_subscription_id:
                            message_text = None
                            should_send = False
                        else:
                            plan_to_charge = sub.plan.upgrades_to_plan if (
                                    sub.plan.is_trial and sub.plan.upgrades_to_plan) else sub.plan
                            if should_send_upcoming_charge_notification(sub.auto_renewal, sub.plan):
                                user_promos = user.promo_codes if user else []
                                current_discount = sub.discount_percent

                                best_promo = next((p for p in user_promos if not p.applies_to_all_plans and any(
                                    ap.id == plan_to_charge.id for ap in p.applicable_plans)), None)
                                if best_promo and best_promo.discount_percent > current_discount:
                                    current_discount = best_promo.discount_percent
                                elif not best_promo:
                                    global_promo = next((p for p in user_promos if p.applies_to_all_plans), None)
                                    if global_promo and global_promo.discount_percent > current_discount:
                                        current_discount = global_promo.discount_percent

                                price = plan_to_charge.price * (1 - current_discount / 100)
                                message_text = (
                                    f"Напоминаем: {date_str} продление доступа к образовательной программе "
                                    f"по тарифу «{plan_to_charge.name}» на сумму {price:.2f} руб."
                                )

                if message_text:
                    try:
                        reply_markup = subscribe_kb if is_trial_promo else None
                        plan_marker = sub.plan.id if sub.plan else "trial"
                        reminder_kind = "trial_reminder" if is_trial_promo else "renewal_reminder"
                        reminder_key = f"{reminder_kind}:{sub.id}:{plan_marker}:{reminder_bucket}"
                        await _send_deduplicated_notification(
                            bot,
                            sub.user_id,
                            message_text,
                            reminder_key,
                            now,
                            reply_markup=reply_markup,
                            window=timedelta(hours=3),
                        )
                    except Exception:
                        pass

                if sub.end_date <= two_hours_later:
                    if is_trial_promo:
                        if sub.discount_percent > 0:
                            sub.discount_percent = 0
                            await session.commit()
                            if sub.end_date > notification_threshold:
                                try:
                                    await _send_deduplicated_notification(
                                        bot,
                                        sub.user_id,
                                        "Пробный период завершен. Выбери тариф образовательной программы для продолжения.",
                                        f"trial_finished:{sub.id}",
                                        now,
                                        reply_markup=subscribe_kb,
                                        window=timedelta(hours=6),
                                    )
                                except Exception:
                                    pass
                        continue

                    if not sub.auto_renewal:
                        if sub.end_date <= now and sub.end_date > notification_threshold:
                            plan_name_exp = sub.plan.name if sub.plan else (sub.payment_provider or "Trial")
                            if not _was_recent_notification_logged(
                                    sub.user_id,
                                    f"expired:{sub.id}:{plan_name_exp}",
                                    now,
                                    EXPIRATION_DEDUP_WINDOW,
                            ):
                                plog.info(f"ИСТЕЧЕНИЕ | {user_ref} | {plan_name_exp}")
                                try:
                                    await _send_deduplicated_notification(
                                        bot,
                                        sub.user_id,
                                        "Доступ к образовательной программе истек. Продлите его в меню.",
                                        f"expired:{sub.id}:{plan_name_exp}",
                                        now,
                                        reply_markup=subscribe_kb,
                                    )
                                except Exception:
                                    pass
                                if config and config.notifications_enabled:
                                    for admin_id in all_admin_ids:
                                        try:
                                            await bot.send_message(
                                                admin_id,
                                                f"⏰ Доступ к образовательной программе истек\nПользователь: {user_ref}\nТариф программы: {plan_name_exp}",
                                                parse_mode="HTML"
                                            )
                                        except Exception:
                                            pass
                        continue

            except Exception as e:
                log.error(f"Error checking sub {sub.id}: {e}")


async def subscription_scheduler_worker(bot: Bot):
    """Воркер, запускающий проверку подписок каждые 15 минут."""
    while True:
        try:
            await check_subscriptions(bot)
        except Exception as e:
            log.error(f"Subscription scheduler error: {e}")
        await asyncio.sleep(900)  # 15 минут
