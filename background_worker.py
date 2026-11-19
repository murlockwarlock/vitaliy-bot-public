import asyncio
import html
import io
from typing import Any, Optional
from sqlalchemy import select, update
from pypdf import PdfReader
from docx import Document
from datetime import date, datetime, timedelta
from openai import AsyncOpenAI
from database import async_session_maker, IndexingQueue, KnowledgeBase, Mailing, User, AIConfig, Habit, PaymentConfig, BotConfig, Content, FunnelState, FunnelLanding, JournalEntry, JournalSettings
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.types import LinkPreviewOptions
from aiogram.utils.keyboard import InlineKeyboardBuilder
from logging_config import get_logger
from time_utils import as_utc, calculate_next_habit_reminder, habit_local_now, msk_now, next_allowed_reminder_at, resolve_habit_timezone, utc_naive
from crisis_safety import automatic_send_allowed
from vector_store import get_collection, get_active_path
from mailing_service import (
    claim_mailing_recipient,
    get_delivered_user_ids,
    record_delivery_attempt,
    send_mailing_to_recipient,
    update_delivery_status,
)

from config import OWNER_IDS as _OWNER_IDS

logger = get_logger("bot")
payments_logger = get_logger("payments")
vector_logger = get_logger("vector")


async def _get_mailing_recipients(session, audience: str, bot=None) -> list[int]:
    """Возвращает список user_id по выбранной аудитории."""
    now = msk_now()

    if audience == "me":
        return list(_OWNER_IDS)

    query = select(User.id).where(User.is_blocked == False, User.crisis_detected_at == None)

    if audience == "active":
        cutoff = now - timedelta(days=7)
        query = query.where(User.last_activity_at != None, User.last_activity_at >= cutoff)
    elif audience == "inactive":
        cutoff = now - timedelta(days=7)
        query = query.where((User.last_activity_at == None) | (User.last_activity_at < cutoff))
    elif audience == "has_sub":
        query = query.where(User.subscription_end != None, User.subscription_end > now)
    elif audience == "no_sub":
        query = query.where((User.subscription_end == None) | (User.subscription_end <= now))
    elif audience == "no_funnel":
        # Начали воронку, но не завершили (stage != 'completed')
        funnel_users = select(FunnelState.user_id).where(
            FunnelState.is_active == True
        )
        query = query.where(User.id.in_(funnel_users))
    elif audience == "paid_funnel":
        paid_users = select(FunnelLanding.user_id).where(FunnelLanding.status == "paid").distinct()
        query = query.where(User.id.in_(paid_users))

    result = await session.execute(query)
    return result.scalars().all()


async def get_openai_client():
    async with async_session_maker() as session:
        config = await session.get(AIConfig, 1)
        if config and config.openai_api_key:
            return AsyncOpenAI(api_key=config.openai_api_key)
    return None


async def parse_file(file_bytes: io.BytesIO, filename: str) -> str:
    text = ""
    ext = filename.split('.')[-1].lower()
    try:
        if ext == 'txt':
            text = file_bytes.read().decode('utf-8')
        elif ext == 'pdf':
            reader = PdfReader(file_bytes)
            for page in reader.pages: text += page.extract_text() or ""
        elif ext == 'docx':
            doc = Document(file_bytes)
            for p in doc.paragraphs: text += p.text + '\n'
    except Exception as e:
        logger.error(f"Parse error: {e}")
    return text


def smart_split(text, chunk_size=1000, overlap=100):
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


async def update_vector_index(file_id, text, client):
    collection = get_collection()
    if collection is None:
        raise RuntimeError("Векторная база Chroma недоступна. Проверьте папку chroma_db или используйте fallback.")

    chunks = smart_split(text)

    ids = [f"{file_id}_{i}" for i in range(len(chunks))]
    metadatas = [{"file_id": file_id} for _ in range(len(chunks))]

    embeddings = []

    batch_size = 20
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        response = await client.embeddings.create(input=batch, model="text-embedding-3-small")
        embeddings.extend([d.embedding for d in response.data])

    if embeddings:
        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=chunks,
            metadatas=metadatas
        )


async def process_queue(bot):
    from config import OWNER_IDS
    while True:
        await asyncio.sleep(5)
        async with async_session_maker() as session:
            job = await session.scalar(select(IndexingQueue).where(IndexingQueue.status == 'pending'))
            if not job: continue

            job.status = 'processing'
            await session.commit()

            client = await get_openai_client()
            if not client:
                job.status = 'failed'
                await bot.send_message(job.uploader_id, "❌ Ошибка: API ключ OpenAI не установлен в настройках.")
                await session.commit()
                continue

            try:
                file_info = await bot.get_file(job.file_id)
                file_bytes = await bot.download_file(file_info.file_path)
                text = await parse_file(file_bytes, job.filename)

                if not text:
                    raise ValueError("Не удалось извлечь текст из файла (пустой файл или неподдерживаемый формат).")

                kb = KnowledgeBase(filename=job.filename, indexed_content=text[:100] + "...")
                session.add(kb)
                await session.flush()

                await update_vector_index(kb.id, text, client)

                job.status = 'completed'
                active_path = get_active_path() or "./chroma_db"
                vector_logger.info(f"Indexed KB file {job.filename} into {active_path}")
                await bot.send_message(job.uploader_id,
                                       f"✅ Файл {job.filename} успешно обработан и добавлен в базу знаний.\nХранилище: {active_path}")
            except Exception as e:
                vector_logger.error(f"Indexing failed: {e}")
                job.status = 'failed'

                error_str = str(e)
                user_text = f"❌ Ошибка обработки {job.filename}:\n"

                admin_alert = ""

                if "insufficient_quota" in error_str:
                    user_text += "💰 На балансе OpenAI закончились средства (insufficient_quota). Пополните счет API."
                    admin_alert = f"🚨 ВНИМАНИЕ! Закончились средства на балансе OpenAI API.\nОшибка: {error_str}"
                elif "Rate limit" in error_str or "429" in error_str:
                    user_text += "🚦 Превышен лимит запросов к OpenAI (Rate limit). Попробуйте позже."
                elif "context_length_exceeded" in error_str:
                    user_text += "📄 Файл слишком большой для обработки этой моделью."
                elif "invalid_api_key" in error_str:
                    user_text += "🔑 Неверный API ключ OpenAI."
                else:
                    user_text += f"Техническая ошибка: {error_str}"

                await bot.send_message(job.uploader_id, user_text)

                if admin_alert:
                    for owner_id in OWNER_IDS:
                        try:
                            await bot.send_message(owner_id, admin_alert)
                        except:
                            pass

            await session.commit()


async def process_mailings(bot):
    while True:
        await asyncio.sleep(10)
        async with async_session_maker() as session:
            mailing = await session.scalar(select(Mailing).where(Mailing.status.in_(['pending', 'sending'])))
            if not mailing:
                continue

            if mailing.status == 'pending':
                mailing.status = 'sending'
                await session.commit()

            audience = mailing.target_audience or 'all'
            ids = await _get_mailing_recipients(session, audience, bot)

            reply_markup = None
            if mailing.button_text and mailing.button_action:
                kb = InlineKeyboardBuilder()
                if mailing.button_action.startswith('http') or mailing.button_action.startswith('tg://'):
                    kb.button(text=mailing.button_text, url=mailing.button_action)
                else:
                    kb.button(text=mailing.button_text, callback_data=mailing.button_action)
                reply_markup = kb.as_markup()

            delivered_uids = await get_delivered_user_ids(session, mailing.id)
            success = len(delivered_uids)
            failed = mailing.failure_count or 0

            for uid in ids:
                if uid in delivered_uids:
                    # Protection against duplicate delivery after worker restart/crash
                    continue

                claimed = await claim_mailing_recipient(session, mailing.id, uid)
                if not claimed:
                    continue
                # Persist claim immediately so other concurrent workers observe it
                await session.commit()

                ok, status_code, err_msg = await send_mailing_to_recipient(
                    bot, uid, mailing, reply_markup=reply_markup, session=session
                )
                await update_delivery_status(session, mailing.id, uid, status_code, error=err_msg)

                if ok:
                    success += 1
                    delivered_uids.add(uid)
                else:
                    failed += 1

                # Periodically update counts
                mailing.success_count = success
                mailing.failure_count = failed
                await session.commit()
                await asyncio.sleep(0.05)

            mailing.status = 'completed'
            mailing.success_count = success
            mailing.failure_count = failed
            await session.commit()


async def _process_habit_reminder(session, bot, habit, now_utc: datetime):
    user = await session.get(User, habit.user_id)
    if not user or user.is_blocked:
        return

    timezone_name = user.habit_timezone
    if habit.next_reminder_at is None:
        next_reminder_at = calculate_next_habit_reminder(
            habit.time_target,
            timezone_name,
            now=now_utc,
            last_reminder_at=habit.last_reminder_at,
        )
        claim = await session.execute(
            update(Habit)
            .where(Habit.id == habit.id, Habit.next_reminder_at == None)
            .values(next_reminder_at=utc_naive(next_reminder_at))
        )
        if claim.rowcount:
            await session.commit()
        return

    if as_utc(habit.next_reminder_at) > now_utc:
        return

    due = habit.next_reminder_at
    moscow = resolve_habit_timezone(None)
    now_msk = now_utc.astimezone(moscow).replace(tzinfo=None)
    allowed = next_allowed_reminder_at(now_msk, timezone_name)
    pending = update(Habit).where(
        Habit.id == habit.id, Habit.is_active == True, Habit.next_reminder_at == due,
    )
    if allowed > now_msk:
        deferred = utc_naive(allowed.replace(tzinfo=moscow))
        await session.execute(pending.values(next_reminder_at=deferred))
        await session.commit()
        return

    if habit.last_reminder_at is not None:
        current_local_date = habit_local_now(timezone_name, now_utc).date()
        last_local_date = habit_local_now(timezone_name, habit.last_reminder_at).date()
        if current_local_date == last_local_date:
            next_reminder_at = calculate_next_habit_reminder(
                habit.time_target,
                timezone_name,
                now=now_utc,
                last_reminder_at=habit.last_reminder_at,
            )
            claim = await session.execute(
                update(Habit)
                .where(Habit.id == habit.id, Habit.next_reminder_at == habit.next_reminder_at)
                .values(next_reminder_at=utc_naive(next_reminder_at))
            )
            if claim.rowcount:
                await session.commit()
            return

    if not await automatic_send_allowed(session, habit.user_id, "habit_reminder"):
        return

    next_due = calculate_next_habit_reminder(habit.time_target, timezone_name, now=now_utc)
    claimed_until = utc_naive(now_utc + timedelta(minutes=5))
    claim = await session.execute(pending.values(next_reminder_at=claimed_until))
    await session.commit()
    if not claim.rowcount:
        return

    safe_name = html.escape(habit.name or "")
    text = (
        f"🔔 <b>Напоминание о привычке</b>\n\n"
        f"🎗 <b>{safe_name}</b>\n\n"
        "Удели время себе. Когда закончишь, нажми «Сделано!»."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сделано!", callback_data=f"habit_done_track_{habit.id}")
    kb.button(text="⚙️ Мои привычки", callback_data="habit_my_list")
    kb.adjust(1)
    try:
        await bot.send_message(habit.user_id, text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except TelegramRetryAfter as exc:
        values = {"next_reminder_at": utc_naive(now_utc + timedelta(seconds=max(60, exc.retry_after)))}
    except TelegramForbiddenError:
        user.is_blocked = True
        values = {"next_reminder_at": due}
    except Exception:
        logger.warning("Habit reminder delivery failed habit_id=%s", habit.id)
        values = {"next_reminder_at": utc_naive(now_utc + timedelta(minutes=1))}
    else:
        values = {
            "next_reminder_at": utc_naive(next_due),
            "last_reminder_at": utc_naive(now_utc),
        }
    await session.execute(update(Habit).where(
        Habit.id == habit.id, Habit.next_reminder_at == claimed_until
    ).values(**values))
    await session.commit()


async def process_habit_reminders(bot, now: datetime | None = None):
    async with async_session_maker() as session:
        habits = await session.execute(
            select(Habit).where(
                Habit.time_target != None,
                Habit.user_id != None,
                Habit.is_active == True,
            )
        )
        habits = habits.scalars().all()

        for habit in habits:
            try:
                await _process_habit_reminder(session, bot, habit, as_utc(now))
            except Exception:
                logger.exception(
                    "Habit reminder processing failed habit_id=%s user_id=%s",
                    habit.id,
                    habit.user_id,
                )


async def habits_worker(bot):
    while True:
        try:
            await process_habit_reminders(bot)
        except Exception as e:
            logger.error(f"Habits worker error: {e}")
        await asyncio.sleep(60)


async def payment_monitor_worker(bot):
    def payment_alert_keyboard():
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Продлить доступ к программе", callback_data="open_payment_menu")
        return builder.as_markup()

    while True:
        try:
            async with async_session_maker() as session:
                conf = await session.get(PaymentConfig, 1)

                if conf.mode == 'subscription':
                    users_expiring = await session.execute(
                        select(User).where(User.subscription_end != None)
                    )
                    users = users_expiring.scalars().all()
                    now = msk_now()

                    for user in users:
                        if not await automatic_send_allowed(session, user.id, "subscription_reminder"):
                            continue
                        if user.subscription_end < now:
                            if user.is_admin: continue
                            payments_logger.info(f"Subscription expired for user {user.id}")
                            try:
                                await bot.send_message(user.id, "❌ Твой доступ к образовательной программе истёк.",
                                                       reply_markup=payment_alert_keyboard())
                            except TelegramRetryAfter as e:
                                await asyncio.sleep(e.retry_after)
                                if not await automatic_send_allowed(session, user.id, "subscription_reminder"):
                                    continue
                                try:
                                    await bot.send_message(user.id, "❌ Твой доступ к образовательной программе истёк.",
                                                           reply_markup=payment_alert_keyboard())
                                except Exception:
                                    pass
                            except Exception:
                                payments_logger.warning(f"Failed to notify user {user.id} about expired subscription")
                            user.subscription_end = None
                        else:
                            diff = user.subscription_end - now
                            hours_left = diff.total_seconds() / 3600

                            notify_type = None
                            if 47 < hours_left <= 48:
                                notify_type = "48 часов"
                            elif 23 < hours_left <= 24:
                                notify_type = "24 часа"
                            elif 1 < hours_left <= 2:
                                notify_type = "2 часа"

                            if notify_type:
                                payments_logger.info(f"Subscription expiry reminder for user {user.id}: {notify_type}")
                                try:
                                    await bot.send_message(user.id, f"⚠️ Твой доступ к образовательной программе истекает через {notify_type}!",
                                                           reply_markup=payment_alert_keyboard())
                                except TelegramRetryAfter as e:
                                    await asyncio.sleep(e.retry_after)
                                    if not await automatic_send_allowed(session, user.id, "subscription_reminder"):
                                        continue
                                    try:
                                        await bot.send_message(user.id, f"⚠️ Твой доступ к образовательной программе истекает через {notify_type}!",
                                                               reply_markup=payment_alert_keyboard())
                                    except Exception:
                                        pass
                                except Exception:
                                    payments_logger.warning(f"Failed to send expiry reminder to user {user.id}")

                elif conf.mode == 'token':
                    users_low = await session.execute(
                        select(User).where(User.balance <= conf.low_balance_threshold,
                                           User.low_balance_notified == False)
                    )
                    users = users_low.scalars().all()

                    for user in users:
                        if not await automatic_send_allowed(session, user.id, "low_balance_reminder"):
                            continue
                        if user.is_admin: continue
                        payments_logger.info(f"Low token balance reminder for user {user.id}")
                        try:
                            await bot.send_message(user.id,
                                                   f"⚠️ Твой баланс ниже {conf.low_balance_threshold}. Пополни для продолжения образовательной программы!",
                                                   reply_markup=payment_alert_keyboard())
                        except TelegramRetryAfter as e:
                            await asyncio.sleep(e.retry_after)
                            if not await automatic_send_allowed(session, user.id, "low_balance_reminder"):
                                continue
                            try:
                                await bot.send_message(user.id,
                                                       f"⚠️ Твой баланс ниже {conf.low_balance_threshold}. Пополни для продолжения образовательной программы!",
                                                       reply_markup=payment_alert_keyboard())
                            except Exception:
                                pass
                        except Exception:
                            payments_logger.warning(f"Failed to send low balance reminder to user {user.id}")
                        user.low_balance_notified = True

                await session.commit()
        except Exception as e:
            payments_logger.error(f"Payment monitor error: {e}")

        await asyncio.sleep(3600)


def payment_alert_keyboard():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Продлить доступ к программе", callback_data="open_payment_menu")
    return builder.as_markup()


def main_reminder_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Главное меню", callback_data="back_to_main_menu_text")
    builder.adjust(1)
    return builder.as_markup()


async def process_main_reminders(bot, now=None):
    async with async_session_maker() as session:
        config = await session.get(BotConfig, 1)
        if not config or not config.main_reminders_enabled:
            return
        users = (await session.scalars(select(User).where(
            User.next_main_reminder_at <= (now or msk_now()), User.is_blocked == False,
        ))).all()
        first = await session.get(Content, "main_reminder_first")
        common = await session.get(Content, "main_reminder_common")
        for user in users:
            current = now or msk_now()
            due = user.next_main_reminder_at
            pending = update(User).where(User.id == user.id, User.next_main_reminder_at == due)
            funnel = await session.get(FunnelState, user.id)
            if (funnel and funnel.is_active) or not await automatic_send_allowed(session, user.id, "main_reminder"):
                await session.execute(pending.values(next_main_reminder_at=None))
                await session.commit()
                continue
            allowed = next_allowed_reminder_at(current, user.habit_timezone)
            if allowed > current:
                await session.execute(pending.values(next_main_reminder_at=allowed))
                await session.commit()
                continue
            lease = current + timedelta(minutes=5)
            claim = await session.execute(pending.values(next_main_reminder_at=lease))
            await session.commit()
            if not claim.rowcount:
                continue
            text = (
                (not user.main_reminder_count and first and first.text_content)
                or (common and common.text_content)
                or "Давно не виделись! Загляни в бот, там много полезного."
            )
            kwargs = dict(reply_markup=main_reminder_keyboard(), link_preview_options=LinkPreviewOptions(is_disabled=True))
            try:
                try:
                    await bot.send_message(user.id, text, parse_mode="HTML", **kwargs)
                except TelegramBadRequest:
                    await bot.send_message(user.id, text, parse_mode=None, **kwargs)
            except TelegramRetryAfter as exc:
                values = {"next_main_reminder_at": current + timedelta(seconds=max(60, exc.retry_after))}
            except TelegramForbiddenError:
                values = {"is_blocked": True, "next_main_reminder_at": None}
            except Exception:
                logger.warning("Main reminder delivery failed user_id=%s", user.id)
                values = {"next_main_reminder_at": current + timedelta(minutes=1)}
            else:
                values = {
                    "main_reminder_count": User.main_reminder_count + 1,
                    "next_main_reminder_at": current + timedelta(hours=config.main_next_reminder_hours),
                }
            await session.execute(update(User).where(
                User.id == user.id, User.next_main_reminder_at == lease,
            ).values(**values))
            await session.commit()


async def main_bot_reminder_worker(bot):
    while True:
        try:
            await process_main_reminders(bot)
        except Exception:
            logger.exception("Main reminder worker error")
        await asyncio.sleep(60)


async def dispatch_journal_reminder_message(
    bot: Any,
    uid: int,
    reminder_text: str,
    kb: Any,
    is_snooze: bool,
    today: date,
    *,
    claim_time: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    pending = update(JournalSettings).where(JournalSettings.user_id == uid)
    if claim_time is not None:
        pending = pending.where(JournalSettings.snooze_claimed_at == claim_time)
    elif not is_snooze:
        pending = pending.where(JournalSettings.last_reminded_date == today)

    async with async_session_maker() as session:
        user = await session.get(User, uid)
        if not user or user.is_blocked or not await automatic_send_allowed(session, uid, "journal_reminder"):
            if is_snooze:
                await session.execute(pending.values(snooze_until=None, snooze_claimed_at=None))
                await session.commit()
            return False
        if claim_time is not None:
            if await session.scalar(select(JournalSettings.snooze_claimed_at).where(JournalSettings.user_id == uid)) != claim_time:
                return False
            current = now or msk_now()
            allowed = next_allowed_reminder_at(current, user.habit_timezone)
            if allowed > current:
                await session.execute(pending.values(snooze_until=allowed, snooze_claimed_at=None))
                await session.commit()
                return False
        try:
            await bot.send_message(chat_id=uid, text=reminder_text, reply_markup=kb, parse_mode="HTML")
        except TelegramRetryAfter as exc:
            if is_snooze:
                values = {"snooze_claimed_at": None}
                if claim_time is not None:
                    values["snooze_until"] = (now or msk_now()) + timedelta(seconds=max(60, exc.retry_after))
            else:
                values = {"last_reminded_date": None}
            await session.execute(pending.values(**values))
        except TelegramForbiddenError:
            user.is_blocked = True
            if is_snooze:
                await session.execute(pending.values(snooze_until=None, snooze_claimed_at=None))
        except Exception as exc:
            if any(reason in str(exc).lower() for reason in ("blocked", "forbidden", "deactivated", "chat not found")):
                user.is_blocked = True
                if is_snooze:
                    await session.execute(pending.values(snooze_until=None, snooze_claimed_at=None))
            elif is_snooze:
                await session.execute(pending.values(snooze_claimed_at=None))
            else:
                await session.execute(pending.values(last_reminded_date=None))
        else:
            if is_snooze:
                await session.execute(pending.values(snooze_until=None, snooze_claimed_at=None))
            await session.commit()
            return True
        await session.commit()
        return False


async def process_journal_reminders(bot, now=None):
    from journal_keyboards import journal_reminder_message_keyboard

    current = now or msk_now()
    today = current.date()
    hhmm = current.strftime("%H:%M")
    settings = JournalSettings
    regular_due = (
        (settings.reminders_enabled == True)
        & (settings.reminder_time <= hhmm)
        & (settings.last_reminded_date.is_(None) | (settings.last_reminded_date < today))
    )
    snooze_due = (settings.snooze_until <= current) & (
        settings.snooze_claimed_at.is_(None) | (settings.snooze_claimed_at < current - timedelta(minutes=5))
    )
    async with async_session_maker() as session:
        candidates = (await session.execute(
            select(settings.user_id, User.habit_timezone).join(User, User.id == settings.user_id)
            .where(User.is_blocked == False, regular_due | snooze_due)
        )).all()
    for uid, zone in candidates:
        async with async_session_maker() as session:
            pending = update(settings).where(settings.user_id == uid)
            completed = select(JournalEntry.id).where(
                JournalEntry.user_id == uid, JournalEntry.reflection_date == today,
                JournalEntry.status == "completed",
            ).exists()
            # Keep a user-selected snooze when the regular reminder is due too.
            allowed = next_allowed_reminder_at(current, zone)
            await session.execute(pending.where(regular_due, settings.snooze_until.is_(None), ~completed)
                                  .values(last_reminded_date=today, snooze_until=allowed))
            await session.execute(pending.where(regular_due, settings.snooze_until.is_not(None))
                                  .values(last_reminded_date=today))
            await session.execute(pending.where(snooze_due, completed)
                                  .values(snooze_until=None, snooze_claimed_at=None))
            if allowed > current:
                await session.execute(pending.where(snooze_due).values(snooze_until=allowed, snooze_claimed_at=None))
                await session.commit()
                continue
            claim = await session.execute(pending.where(snooze_due, ~completed).values(snooze_claimed_at=current))
            await session.commit()
            if not claim.rowcount:
                continue

        await dispatch_journal_reminder_message(
            bot, uid,
            "🔔 <b>Время вечерней рефлексии!</b>\n\nВыдели несколько минут, чтобы подвести итоги дня в Дневнике.",
            journal_reminder_message_keyboard(), is_snooze=True, today=today, claim_time=current, now=now,
        )


async def journal_reminder_worker(bot):
    from journal_service import recover_stale_journal_ai_operations

    while True:
        try:
            await recover_stale_journal_ai_operations()
        except Exception:
            logger.exception("Error in recover_stale_journal_ai_operations")
        try:
            await process_journal_reminders(bot)
        except Exception:
            logger.exception("Journal reminder worker error")
        await asyncio.sleep(60)
