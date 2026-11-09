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
from time_utils import as_utc, calculate_next_habit_reminder, habit_local_now, msk_now, utc_naive
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
    if not user:
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

    next_reminder_at = calculate_next_habit_reminder(
        habit.time_target,
        timezone_name,
        now=now_utc,
        last_reminder_at=now_utc,
    )
    due_reminder_at = habit.next_reminder_at
    claim = await session.execute(
        update(Habit)
        .where(Habit.id == habit.id, Habit.next_reminder_at == due_reminder_at)
        .values(
            next_reminder_at=utc_naive(next_reminder_at),
            last_reminder_at=utc_naive(now_utc),
        )
    )
    if not claim.rowcount:
        return
    await session.commit()

    try:
        safe_name = html.escape(habit.name or "")
        text = (
            f"🔔 <b>Напоминание привычки!</b>\n\n"
            f"🎗 <b>{safe_name}</b>\n\n"
            f"Пора уделить время себе. Выполнил(а)?"
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Сделано!", callback_data=f"habit_done_track_{habit.id}")
        kb.button(text="⚙️ Мои привычки", callback_data="habit_my_list")
        kb.adjust(1)

        await bot.send_message(habit.user_id, text, reply_markup=kb.as_markup(), parse_mode='HTML')

    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        if not await automatic_send_allowed(session, habit.user_id, "habit_reminder"):
            return
        try:
            await bot.send_message(habit.user_id, text, reply_markup=kb.as_markup(), parse_mode='HTML')
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error sending habit reminder to {habit.user_id}: {e}")


async def process_habit_reminders(bot, now: datetime | None = None):
    now_utc = as_utc(now)

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
                await _process_habit_reminder(session, bot, habit, now_utc)
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


async def main_bot_reminder_worker(bot):
    while True:
        try:
            async with async_session_maker() as session:
                bot_conf = await session.get(BotConfig, 1)
                if not bot_conf or not bot_conf.main_reminders_enabled:
                    await asyncio.sleep(60)
                    continue

                now = msk_now()
                users = (
                    await session.execute(
                        select(User).where(
                            User.next_main_reminder_at != None,
                            User.next_main_reminder_at <= now,
                            User.is_blocked == False,
                        )
                    )
                ).scalars().all()

                first_content = await session.get(Content, "main_reminder_first")
                common_content = await session.get(Content, "main_reminder_common")

                for user in users:
                    if not await automatic_send_allowed(session, user.id, "main_reminder"):
                        user.next_main_reminder_at = None
                        continue
                    # Пропускаем, если пользователь в активной воронке (у неё свои напоминания)
                    funnel_state = await session.get(FunnelState, user.id)
                    if funnel_state and funnel_state.is_active:
                        user.next_main_reminder_at = None
                        continue

                    if user.main_reminder_count == 0 and first_content and first_content.text_content:
                        text = first_content.text_content
                    elif common_content and common_content.text_content:
                        text = common_content.text_content
                    else:
                        text = "Давно не виделись! Загляни в бот — там много полезного."

                    try:
                        await bot.send_message(
                            user.id,
                            text,
                            reply_markup=main_reminder_keyboard(),
                            parse_mode="HTML",
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                        )
                        user.main_reminder_count += 1
                        user.next_main_reminder_at = now + timedelta(
                            hours=bot_conf.main_next_reminder_hours if user.main_reminder_count > 0 else bot_conf.main_first_reminder_hours
                        )
                    except TelegramBadRequest:
                        if not await automatic_send_allowed(session, user.id, "main_reminder"):
                            user.next_main_reminder_at = None
                            continue
                        await bot.send_message(
                            user.id,
                            text,
                            reply_markup=main_reminder_keyboard(),
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                        )
                        user.main_reminder_count += 1
                        user.next_main_reminder_at = now + timedelta(
                            hours=bot_conf.main_next_reminder_hours if user.main_reminder_count > 0 else bot_conf.main_first_reminder_hours
                        )
                    except TelegramRetryAfter as e:
                        await asyncio.sleep(e.retry_after)
                        if not await automatic_send_allowed(session, user.id, "main_reminder"):
                            user.next_main_reminder_at = None
                            continue
                        try:
                            await bot.send_message(
                                user.id,
                                text,
                                reply_markup=main_reminder_keyboard(),
                                parse_mode="HTML",
                                link_preview_options=LinkPreviewOptions(is_disabled=True),
                            )
                            user.main_reminder_count += 1
                            user.next_main_reminder_at = now + timedelta(hours=bot_conf.main_next_reminder_hours)
                        except TelegramBadRequest:
                            if not await automatic_send_allowed(session, user.id, "main_reminder"):
                                user.next_main_reminder_at = None
                                continue
                            await bot.send_message(
                                user.id,
                                text,
                                reply_markup=main_reminder_keyboard(),
                                link_preview_options=LinkPreviewOptions(is_disabled=True),
                            )
                            user.main_reminder_count += 1
                            user.next_main_reminder_at = now + timedelta(hours=bot_conf.main_next_reminder_hours)
                        except Exception:
                            user.next_main_reminder_at = now + timedelta(hours=bot_conf.main_next_reminder_hours)
                    except Exception as exc:
                        logger.warning("Main reminder failed for user_id=%s: %s", user.id, exc)
                        user.next_main_reminder_at = now + timedelta(hours=bot_conf.main_next_reminder_hours)

                await session.commit()
        except Exception as e:
            logger.error(f"Main reminder worker error: {e}")

        await asyncio.sleep(60)


async def dispatch_journal_reminder_message(
    bot: Any,
    uid: int,
    reminder_text: str,
    kb: Any,
    is_snooze: bool,
    today: date,
) -> bool:
    """
    Deliver a journal reminder with TelegramRetryAfter handling and uniform failure transitions:
    - Initial send attempt.
    - If TelegramRetryAfter: sleep and retry on second attempt under the SAME claim.
    - If second attempt (or initial attempt) succeeds:
      - Snooze: clear snooze_until and snooze_claimed_at.
      - Regular: claim is kept.
    - If second attempt (or initial attempt) fails:
      - Forbidden/blocked user:
        - mark User.is_blocked = True;
        - Regular: consume/keep the daily claim (last_reminded_date == today);
        - Snooze: clear snooze_until = None and snooze_claimed_at = None.
      - Retryable/network failure:
        - Regular: conditionally release claim:
          UPDATE journal_settings SET last_reminded_date = NULL WHERE user_id = :uid AND last_reminded_date = :today
        - Snooze: set snooze_claimed_at = NULL, keep snooze_until unchanged so future worker tick can retry.
    """
    async def _allowed() -> bool:
        async with async_session_maker() as session:
            return await automatic_send_allowed(session, uid, "journal_reminder")

    async def _clear_snooze_claim() -> None:
        if not is_snooze:
            return
        async with async_session_maker() as session:
            await session.execute(
                update(JournalSettings)
                .where(JournalSettings.user_id == uid)
                .values(snooze_until=None, snooze_claimed_at=None)
            )
            await session.commit()

    if not await _allowed():
        await _clear_snooze_claim()
        return False

    final_exc = None
    try:
        await bot.send_message(chat_id=uid, text=reminder_text, reply_markup=kb, parse_mode="HTML")
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        if not await _allowed():
            await _clear_snooze_claim()
            return False
        try:
            await bot.send_message(chat_id=uid, text=reminder_text, reply_markup=kb, parse_mode="HTML")
        except Exception as exc:
            final_exc = exc
    except Exception as exc:
        final_exc = exc

    if final_exc is None:
        if is_snooze:
            async with async_session_maker() as session:
                await session.execute(
                    update(JournalSettings)
                    .where(JournalSettings.user_id == uid)
                    .values(snooze_until=None, snooze_claimed_at=None)
                )
                await session.commit()
        return True

    exc_str = str(final_exc).lower()
    is_forbidden = (
        isinstance(final_exc, TelegramForbiddenError)
        or "blocked" in exc_str
        or "forbidden" in exc_str
        or "deactivated" in exc_str
        or "chat not found" in exc_str
    )

    async with async_session_maker() as session:
        if is_forbidden:
            u = await session.get(User, uid)
            if u:
                u.is_blocked = True
            if is_snooze:
                await session.execute(
                    update(JournalSettings)
                    .where(JournalSettings.user_id == uid)
                    .values(snooze_until=None, snooze_claimed_at=None)
                )
            # Regular keeps last_reminded_date = today
            await session.commit()
        else:
            if is_snooze:
                await session.execute(
                    update(JournalSettings)
                    .where(JournalSettings.user_id == uid)
                    .values(snooze_claimed_at=None)
                )
            else:
                await session.execute(
                    update(JournalSettings)
                    .where(JournalSettings.user_id == uid, JournalSettings.last_reminded_date == today)
                    .values(last_reminded_date=None)
                )
            await session.commit()

    return False


async def journal_reminder_worker(bot):
    """Background worker for Journal evening reminders, snooze alerts, and stale AI recovery."""
    while True:
        try:
            from journal_service import recover_stale_journal_ai_operations
            await recover_stale_journal_ai_operations()
        except Exception as e:
            logger.error(f"Error in recover_stale_journal_ai_operations: {e}")

        try:
            now = msk_now()
            today = now.date()
            current_hhmm = now.strftime("%H:%M")

            # 1. Regular evening reminders
            async with async_session_maker() as session:
                cand_stmt = (
                    select(JournalSettings.user_id)
                    .where(
                        JournalSettings.reminders_enabled == True,
                        JournalSettings.reminder_time != None,
                        JournalSettings.reminder_time <= current_hhmm,
                        (JournalSettings.last_reminded_date == None) | (JournalSettings.last_reminded_date < today),
                    )
                )
                candidates = (await session.execute(cand_stmt)).scalars().all()

            for uid in candidates:
                async with async_session_maker() as session:
                    not_completed_subq = (
                        select(1)
                        .select_from(JournalEntry)
                        .where(
                            JournalEntry.user_id == uid,
                            JournalEntry.reflection_date == today,
                            JournalEntry.status == "completed",
                        )
                    )
                    claim_stmt = (
                        update(JournalSettings)
                        .where(
                            JournalSettings.user_id == uid,
                            JournalSettings.reminders_enabled == True,
                            JournalSettings.reminder_time != None,
                            JournalSettings.reminder_time <= current_hhmm,
                            (JournalSettings.last_reminded_date == None) | (JournalSettings.last_reminded_date < today),
                            ~not_completed_subq.exists(),
                        )
                        .values(last_reminded_date=today)
                        .returning(JournalSettings.user_id)
                    )
                    claimed_uid = (await session.execute(claim_stmt)).scalar()
                    await session.commit()

                if not claimed_uid:
                    continue

                async with async_session_maker() as session:
                    u = await session.get(User, uid)
                    if u and u.is_blocked:
                        continue

                reminder_text = (
                    "🔔 <b>Время вечерней рефлексии!</b>\n\n"
                    "Выдели несколько минут, чтобы подвести итоги дня в Дневнике."
                )
                from journal_keyboards import journal_reminder_message_keyboard
                kb = journal_reminder_message_keyboard()

                await dispatch_journal_reminder_message(bot, uid, reminder_text, kb, is_snooze=False, today=today)

            # 2. Snooze reminders
            # Clear stale snooze claims older than 5 minutes (at-most-once semantics)
            async with async_session_maker() as session:
                await session.execute(
                    update(JournalSettings)
                    .where(
                        JournalSettings.snooze_claimed_at != None,
                        JournalSettings.snooze_claimed_at < now - timedelta(minutes=5),
                    )
                    .values(snooze_until=None, snooze_claimed_at=None)
                )
                await session.commit()

            async with async_session_maker() as session:
                snooze_cand_stmt = (
                    select(JournalSettings.user_id)
                    .where(
                        JournalSettings.snooze_until != None,
                        JournalSettings.snooze_until <= now,
                        JournalSettings.snooze_claimed_at == None,
                    )
                )
                snooze_candidates = (await session.execute(snooze_cand_stmt)).scalars().all()

            for uid in snooze_candidates:
                async with async_session_maker() as session:
                    not_completed_subq = (
                        select(1)
                        .select_from(JournalEntry)
                        .where(
                            JournalEntry.user_id == uid,
                            JournalEntry.reflection_date == today,
                            JournalEntry.status == "completed",
                        )
                    )
                    snooze_claim_stmt = (
                        update(JournalSettings)
                        .where(
                            JournalSettings.user_id == uid,
                            JournalSettings.snooze_until <= now,
                            JournalSettings.snooze_claimed_at == None,
                            ~not_completed_subq.exists(),
                        )
                        .values(snooze_claimed_at=now)
                        .returning(JournalSettings.user_id)
                    )
                    claimed_uid = (await session.execute(snooze_claim_stmt)).scalar()
                    await session.commit()

                if not claimed_uid:
                    continue

                async with async_session_maker() as session:
                    u = await session.get(User, uid)
                    if u and u.is_blocked:
                        continue

                reminder_text = (
                    "🔔 <b>Время вечерней рефлексии!</b>\n\n"
                    "Выдели несколько минут, чтобы подвести итоги дня в Дневнике."
                )
                from journal_keyboards import journal_reminder_message_keyboard
                kb = journal_reminder_message_keyboard()

                await dispatch_journal_reminder_message(bot, uid, reminder_text, kb, is_snooze=True, today=today)

        except Exception as e:
            logger.error(f"Journal reminder worker error: {e}")

        await asyncio.sleep(60)
