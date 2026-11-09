import asyncio
import copy
import html
import hashlib
import hmac
import json
import math
import re
import chromadb
from pathlib import Path
from urllib.parse import urlencode, urlparse
from aiohttp import ClientSession
import secrets
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InputMediaPhoto, InputMediaVideo, BotCommand, BotCommandScopeChat, LabeledPrice, PreCheckoutQuery, ContentType, InlineKeyboardButton, ReplyKeyboardRemove, LinkPreviewOptions, InlineKeyboardMarkup
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, update, func, delete, text, or_, and_, cast, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert
from sqlalchemy.orm import selectinload
from aiogram.utils.deep_linking import decode_payload
from datetime import datetime, timedelta, date, timezone
from dateutil.relativedelta import relativedelta
from time_utils import (
    HABIT_TIMEZONE_OPTIONS,
    as_utc,
    calculate_next_habit_reminder,
    format_habit_timezone,
    format_msk,
    habit_local_now,
    habit_timezone_name,
    msk_now,
    normalize_habit_time,
    normalize_habit_timezone,
    utc_naive,
)
from logging_config import get_logger
from crisis_safety import crisis_was_detected_in_turn, is_crisis_active, send_crisis_contacts_if_active

from config import OWNER_IDS
from admin_roles import (
    AdminPermission,
    AdminRole,
    get_admin_role,
    install_admin_route_guards,
    permission_for_callback,
    require_permission,
    role_has_permission,
    role_label,
)
from database import async_session_maker, User, Topic, Content, Message as DBMessage, ReferralConfig, AIConfig, \
    IndexingQueue, ContentMedia, KnowledgeBase, Mailing, HabitConfig, Habit, PaymentConfig, PaymentProvider, \
    Transaction, Book, SubscriptionPlan, FunnelLanding, FunnelState, FunnelEvent, BotConfig, SubscriptionConfig, \
    UserSubscription, PromoCode, ProdamusPayment, TrialUsageHistory, get_all_admin_ids, \
    PaymentIntent, ReferralPaymentLog, AiDisclaimerClaim, TopicDialogueConfig, PendingAiFlow, SessionSummary, SummaryClaim, \
    UserTopicSession, PreTestClaim, TopicPrerecordedDelivery, TestSession, TestAttempt, MenuSection, MenuButton, \
    AdminNavigationContext, sync_book_menu_buttons
import keyboards as kb
from menu_service import classify_menu_action, is_menu_section_enabled, validate_menu_url
from ai_dialogue_service import (
    TopicContextSnapshot,
    acquire_pre_test_claim,
    allocate_next_dialogue_id,
    clear_pending_ai_flow,
    consume_pending_ai_flow,
    deliver_prerecorded_first_message,
    generate_session_summary,
    get_active_topic_dialogue_id,
    get_pending_ai_flow,
    get_pre_test_claim,
    set_active_topic_dialogue_id,
    store_pending_ai_flow,
)
from ai_integration import (
    ai_typing_action,
    analyze_image,
    edit_image,
    generate_image,
    get_ai_response,
    get_ai_response_for_topic,
    transcribe_audio,
)
from kie_client import KIE_CHAT_MODELS, KIE_IMAGE_EDIT_MODELS, KIE_IMAGE_MODELS, KIE_TRANSCRIPTION_MODELS, KIE_VISION_MODELS, remaining_credits
from background_worker import update_vector_index
from test_system_handlers import (
    extract_test_start_directive,
    get_test_set_readiness,
    send_test_setup_unavailable,
    start_universal_test,
    UniversalTestStates,
    _send_question,
)
from universal_tests import expected_test_set_question_count
from funnel import parse_start_action, start_funnel_intro
from content_service import (
    get_content as cs_get_content,
    get_text as cs_get_text,
    get_button as cs_get_button,
    get_link as cs_get_link,
    invalidate_content_cache,
)
from admin_html_preview import truncate_html_preview

_ROOT_MENU_SECTION_KEYS = ("main", "extended")
_menu_section_toggle_lock = asyncio.Lock()


async def _read_welcome_shown_at(session, user):
    scalar = getattr(session, "scalar", None)
    if callable(scalar):
        return await scalar(
            select(User.__table__.c.welcome_shown_at).where(User.id == user.id)
        )
    return getattr(user, "welcome_shown_at", None)

router = Router()
logger = get_logger("bot")
payments_logger = get_logger("payments")
vector_logger = get_logger("vector")


_ADMIN_PAYMENT_MAIN_CALLBACK = "admin_payment_settings"
_ADMIN_PAYMENT_SETTINGS_CALLBACK = "admin_prodamus_settings"
_ADMIN_PAYMENT_RETURN_CALLBACKS = {
    _ADMIN_PAYMENT_MAIN_CALLBACK,
    _ADMIN_PAYMENT_SETTINGS_CALLBACK,
}


def _normalize_admin_payment_return(value: str | None, fallback: str = _ADMIN_PAYMENT_MAIN_CALLBACK) -> str:
    if value == "admin_subscriptions":
        return _ADMIN_PAYMENT_MAIN_CALLBACK
    if value in _ADMIN_PAYMENT_RETURN_CALLBACKS:
        return value
    return fallback


def _admin_payment_action_source(data: str | None, fallback: str = _ADMIN_PAYMENT_MAIN_CALLBACK) -> str:
    if not isinstance(data, str) or "__" not in data:
        return fallback
    return _normalize_admin_payment_return(data.rsplit("__", 1)[1], fallback)


def _admin_payment_action_callback(action: str, source: str = _ADMIN_PAYMENT_SETTINGS_CALLBACK) -> str:
    normalized_source = _normalize_admin_payment_return(source, _ADMIN_PAYMENT_SETTINGS_CALLBACK)
    return action if normalized_source == _ADMIN_PAYMENT_MAIN_CALLBACK else f"{action}__{normalized_source}"


def _admin_payment_settings_parent(value: str | None) -> str:
    current_screen = _normalize_admin_payment_return(value, _ADMIN_PAYMENT_MAIN_CALLBACK)
    if current_screen == _ADMIN_PAYMENT_SETTINGS_CALLBACK:
        return _ADMIN_PAYMENT_MAIN_CALLBACK
    return current_screen


def _normalize_admin_payment_resume(
    value: str | None,
    fallback_source: str = _ADMIN_PAYMENT_MAIN_CALLBACK,
) -> str | None:
    if not isinstance(value, str):
        return None
    base_value = value.split("__", 1)[0]
    if base_value in {"admin_prodamus_keys_from_root", "admin_prodamus_keys_from_settings"}:
        return base_value
    if base_value in {"admin_pay_prices", "admin_webhook_settings", "admin_pay_mode"}:
        source = _admin_payment_action_source(value, fallback_source)
        return _admin_payment_action_callback(base_value, source)
    provider_match = re.fullmatch(r"admin_pay_providers(?:_(\d+))?", base_value)
    if provider_match:
        page = provider_match.group(1) or "0"
        source = _admin_payment_action_source(value, fallback_source)
        return _admin_payment_action_callback(f"admin_pay_providers_{page}", source)
    return None


async def _admin_payment_state_return(state: FSMContext | None, fallback: str = _ADMIN_PAYMENT_MAIN_CALLBACK) -> str:
    if state is None:
        return fallback
    data = await state.get_data()
    return _normalize_admin_payment_return(data.get("admin_payment_return_callback"), fallback)


async def _admin_payment_state_resume(
    state: FSMContext | None,
    fallback_source: str = _ADMIN_PAYMENT_MAIN_CALLBACK,
) -> str | None:
    if state is None:
        return None
    data = await state.get_data()
    return _normalize_admin_payment_resume(data.get("admin_payment_resume_callback"), fallback_source)


async def _render_admin_payment_return(event, state: FSMContext | None, return_callback: str) -> None:
    target = _normalize_admin_payment_return(return_callback)
    if target == _ADMIN_PAYMENT_SETTINGS_CALLBACK:
        await admin_prodamus_settings(event, state=state)
    else:
        await admin_payment_settings(event, state=state)


async def _render_admin_payment_resume(
    event,
    state: FSMContext | None,
    resume_callback: str | None,
    fallback_source: str = _ADMIN_PAYMENT_MAIN_CALLBACK,
) -> None:
    target = _normalize_admin_payment_resume(resume_callback, fallback_source)
    if target is None:
        await _render_admin_payment_return(event, state, fallback_source)
        return
    base_target = target.split("__", 1)[0]
    if base_target.startswith("admin_pay_providers"):
        page_match = re.fullmatch(r"admin_pay_providers_(\d+)", base_target)
        page = int(page_match.group(1)) if page_match else 0
        await admin_pay_providers(event, state=state, requested_page=page)
        return
    if base_target == "admin_pay_prices":
        await admin_pay_prices(event, state=state)
        return
    if base_target == "admin_webhook_settings":
        await admin_webhook_settings(event, state=state)
        return
    if base_target == "admin_pay_mode":
        await admin_pay_mode(event, state=state)
        return
    if base_target.startswith("admin_prodamus_keys_from_"):
        if isinstance(event, CallbackQuery):
            await admin_prodamus_keys(
                event.model_copy(update={"data": base_target}),
                state=state,
            )
        else:
            target = (
                _ADMIN_PAYMENT_SETTINGS_CALLBACK
                if base_target.endswith("_settings")
                else _ADMIN_PAYMENT_MAIN_CALLBACK
            )
            await _render_admin_payment_return(event, state, target)
        return
    await _render_admin_payment_return(event, state, fallback_source)


async def _clear_admin_navigation_state(
    state: FSMContext | None,
    *,
    preserve_client_context: bool = False,
) -> None:
    if state is None:
        return
    preserved = {}
    if preserve_client_context:
        data = await state.get_data()
        for key in (
            "admin_client_return_callback",
            "admin_client_parent_callback",
            "client_search_query",
            "admin_client_search_reset_callback",
            "admin_client_search_reset_query",
            "admin_client_navigation_token",
        ):
            if key in data:
                preserved[key] = data[key]
    await state.clear()
    if preserved:
        await state.update_data(**preserved)


def _admin_client_return_callback(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"admin_clients_list_\d+", value):
        return value
    if re.fullmatch(r"admin_funnel_landings_(?:all|opened|form_submitted|paid|failed)_\d+", value):
        return value
    compact_match = re.fullmatch(r"landing_(all|opened|form|paid|failed)_(\d+)", value)
    if compact_match:
        status_code, page = compact_match.groups()
        status_filter = {
            "all": "all",
            "opened": "opened",
            "form": "form_submitted",
            "paid": "paid",
            "failed": "failed",
        }[status_code]
        return f"admin_funnel_landings_{status_filter}_{page}"
    return None


def _admin_client_parent_callback(value: str | None) -> str:
    if value == "admin_section_clients":
        return "admin_panel"
    if value == "admin_panel":
        return value
    return _admin_client_return_callback(value) or "admin_panel"


def _admin_client_action_context(value: str | None) -> str | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    context_value = value.rsplit(":", 1)[1]
    return context_value if _admin_client_context_token(context_value) else None


def _admin_client_action_suffix(value: str | None) -> str:
    context_value = _admin_client_action_context(value)
    return f":{context_value}" if context_value else ""


def _admin_client_action_user_id(value: str, prefix: str) -> int:
    return int(value.removeprefix(prefix).split(":", 1)[0])


def _callback_with_data(callback, data: str):
    if isinstance(callback, CallbackQuery):
        return callback.model_copy(update={"data": data})
    clone = copy.copy(callback)
    clone.data = data
    return clone


_ADMIN_CLIENT_CONTEXT_PREFIX = "clientctx_"
_ADMIN_CLIENT_CONTEXT_RE = re.compile(r"^clientctx_([0-9a-f]{16})$")
_ADMIN_CLIENT_LIST_RE = re.compile(
    r"^admin_clients_list_(\d+)(?::(clientctx_[0-9a-f]{16}))?$"
)
_ADMIN_CLIENT_CONTEXT_TTL = timedelta(hours=24)


def _admin_client_context_token(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    match = _ADMIN_CLIENT_CONTEXT_RE.fullmatch(value)
    return match.group(1) if match else None


def _parse_admin_client_list_callback(value: str | None) -> tuple[int, str | None] | None:
    if not isinstance(value, str):
        return None
    match = _ADMIN_CLIENT_LIST_RE.fullmatch(value)
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def _admin_client_list_callback(page: int, context_value: str | None = None) -> str:
    suffix = f":{context_value}" if _admin_client_context_token(context_value) else ""
    return f"admin_clients_list_{page}{suffix}"


def _callback_chat_id(callback: CallbackQuery) -> int | None:
    return getattr(getattr(callback.message, "chat", None), "id", None)


def _callback_message_id(callback: CallbackQuery) -> int | None:
    return getattr(callback.message, "message_id", None)


async def _create_admin_client_context(
    callback: CallbackQuery,
    *,
    return_callback: str,
    search_query: str | None,
    reset_callback: str,
    reset_query: str | None,
    parent_callback: str = "admin_panel",
) -> str:
    token = secrets.token_hex(8)
    async with async_session_maker() as session:
        await session.execute(
            delete(AdminNavigationContext).where(
                AdminNavigationContext.admin_id == callback.from_user.id,
                AdminNavigationContext.created_at < datetime.utcnow() - _ADMIN_CLIENT_CONTEXT_TTL,
            )
        )
        session.add(
            AdminNavigationContext(
                token=token,
                admin_id=callback.from_user.id,
                chat_id=_callback_chat_id(callback) or 0,
                message_id=_callback_message_id(callback) or 0,
                return_callback=return_callback,
                parent_callback=_admin_client_parent_callback(parent_callback),
                search_query=search_query or None,
                reset_callback=reset_callback,
                reset_query=reset_query or None,
            )
        )
        await session.commit()
    return f"{_ADMIN_CLIENT_CONTEXT_PREFIX}{token}"


async def _load_admin_client_context(
    callback: CallbackQuery,
    context_value: str | None,
) -> AdminNavigationContext | None:
    token = _admin_client_context_token(context_value)
    if not token:
        return None
    async with async_session_maker() as session:
        context = await session.get(AdminNavigationContext, token)
    if context is None:
        return None
    if context.admin_id != callback.from_user.id:
        return None
    if context.chat_id != (_callback_chat_id(callback) or 0):
        return None
    if context.message_id != (_callback_message_id(callback) or 0):
        return None
    return context


async def _bind_admin_client_context_message(
    context_value: str,
    callback: CallbackQuery,
    rendered_message,
) -> None:
    token = _admin_client_context_token(context_value)
    message_id = getattr(rendered_message, "message_id", None)
    chat_id = getattr(getattr(rendered_message, "chat", None), "id", None)
    if not token or not isinstance(message_id, int):
        return
    chat_id = chat_id if isinstance(chat_id, int) else _callback_chat_id(callback)
    if not isinstance(chat_id, int):
        return
    async with async_session_maker() as session:
        context = await session.get(AdminNavigationContext, token)
        if context is None or context.admin_id != callback.from_user.id:
            return
        context.chat_id = chat_id
        context.message_id = message_id
        await session.commit()


def _admin_confirmation_markup(confirm_callback: str, cancel_callback: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, продолжить", callback_data=confirm_callback)
    builder.button(text="⬅️ Отмена", callback_data=cancel_callback)
    builder.adjust(1)
    return builder.as_markup()


async def _ask_admin_confirmation(
    callback: CallbackQuery,
    text: str,
    confirm_callback: str,
    cancel_callback: str,
) -> None:
    message = getattr(callback, "message", None)
    if message is not None and hasattr(message, "edit_text"):
        await message.edit_text(
            f"⚠️ <b>Подтвердите действие</b>\n\n{text}",
            reply_markup=_admin_confirmation_markup(confirm_callback, cancel_callback),
            parse_mode="HTML",
        )
    await callback.answer()

LOG_FILES = {
    "bot": ("Общий лог бота", Path("logs/bot.log")),
    "errors": ("Ошибки", Path("logs/errors.log")),
    "funnel": ("Воронка", Path("logs/funnel.log")),
    "ai": ("Запросы к ИИ", Path("logs/ai.log")),
    "payments": ("Платежи", Path("logs/payments.log")),
    "webhook": ("Уведомления с лендинга", Path("logs/webhook.log")),
    "vector": ("Индексация базы знаний", Path("logs/vector.log")),
}

_PLOG_ENTRY_MAX_LEN = 220
_PLOG_MESSAGE_MAX_LEN = 3900
_PLOG_FILTERS = {
    "all": ((), "Все"),
    "prodamus": (("Prodamus",), "Prodamus"),
    "telegram": (("Telegram", "Telegram Pay"), "Telegram"),
    "landing": (("Landing payment", "лендинг", "GetCourse"), "Лендинг"),
    "errors": (("ERROR", "Failed", "не удалось", "ошибка"), "Ошибки"),
}


class UserStates(StatesGroup):
    diagnostics_process = State()
    chatting = State()
    awaiting_new_name = State()
    awaiting_gender = State()
    awaiting_age = State()
    awaiting_promo_code = State()


class HabitStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_time = State()
    waiting_for_edit_time = State()


class AdminStates(StatesGroup):
    edit_content = State()
    edit_url = State()
    edit_topic_prompt = State()
    set_referral_points = State()
    edit_ref_reg_points = State()
    edit_ref_first_percent = State()
    edit_ref_recurring_percent = State()
    edit_ref_bonus_days = State()
    mailing_content = State()
    mailing_edit_text = State()
    mailing_edit_media = State()
    mailing_btn_text = State()
    mailing_btn_url = State()
    upload_kb = State()
    set_ai_key = State()
    set_kie_key = State()
    add_admin = State()
    last_msg_id = State()
    set_deepseek_key = State()
    set_openrouter_key = State()
    set_model_name = State()
    set_limit_first = State()
    set_limit_recent = State()
    add_habit_template = State()
    set_game_days = State()
    set_game_rewards = State()
    set_game_window = State()
    set_game_msg = State()
    add_book_title = State()
    add_book_url = State()
    plan_name = State()
    plan_price = State()
    plan_days = State()
    plan_desc = State()
    set_reminder_first_hours = State()
    set_reminder_next_hours = State()
    set_webhook_secret = State()
    set_webhook_port = State()
    set_single_payment_key = State()
    edit_plan_field = State()
    edit_promo_field = State()
    set_promo_code = State()
    set_promo_discount = State()
    set_promo_days = State()
    set_promo_uses = State()
    set_welcome_bonus_days = State()
    search_client = State()
    menu_button_label = State()
    menu_button_action = State()
    menu_button_order = State()


class PaymentStates(StatesGroup):
    add_provider_token = State()
    add_provider_name = State()
    set_token_price = State()
    set_token_amount = State()


async def get_content(key):
    return await cs_get_content(key)


async def clean_chat_after_input(message: Message, state: FSMContext):
    try:
        await message.delete()
    except:
        pass

    data = await state.get_data()
    last_msg_id = data.get('last_msg_id')
    if last_msg_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=last_msg_id)
        except:
            pass


async def send_smart_content(target, text, media, reply_markup=None, bot=None):
    # АВТОМАТИЧЕСКОЕ ОПРЕДЕЛЕНИЕ БОТА
    if bot is None:
        # Проверяем наличие атрибута bot (для FakeUpdate и стандартных типов)
        if hasattr(target, 'bot') and target.bot:
            bot = target.bot
        elif hasattr(target, 'message') and hasattr(target.message, 'bot') and target.message.bot:
            bot = target.message.bot

    # Если текст не задан, делаем его пустым во избежание ошибок
    if not text: text = ""

    # Определение chat_id и message_obj
    # Проверяем атрибут message, так как FakeUpdate тоже его имеет
    if hasattr(target, 'message') and target.message:
        message_obj = target.message
        chat_id = message_obj.chat.id
    else:
        message_obj = target
        chat_id = target.chat.id

    # 1. Попытка редактирования (только если это настоящий CallbackQuery и нет медиа)
    if not media and isinstance(target, CallbackQuery):
        is_text_msg = (
            getattr(message_obj, 'content_type', None) == 'text'
            or (hasattr(message_obj, 'text') and getattr(message_obj, 'text', None) is not None)
            or (hasattr(message_obj, 'edit_text') and not hasattr(message_obj, 'photo') and getattr(message_obj, 'content_type', None) is None)
        )
        if is_text_msg and hasattr(message_obj, 'edit_text'):
            try:
                await message_obj.edit_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode='HTML',
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                return message_obj
            except TelegramBadRequest as b_err:
                b_err_msg = str(b_err).lower()
                if "message is not modified" in b_err_msg:
                    return message_obj
                if "can't parse entities" in b_err_msg or "entity" in b_err_msg:
                    try:
                        await message_obj.edit_text(
                            text,
                            reply_markup=reply_markup,
                            parse_mode=None,
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                        )
                        return message_obj
                    except TelegramBadRequest as b_err2:
                        if "message is not modified" in str(b_err2).lower():
                            return message_obj
            except Exception as e:
                if "message is not modified" in str(e).lower():
                    return message_obj

    # 2. Удаление старого сообщения (для CallbackQuery перед отправкой нового)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.delete()
        except:
            pass
    elif not isinstance(target, (Message, CallbackQuery)) and hasattr(target, 'message'):
        try:
            await target.message.delete()
        except:
            pass

    last_message = None

    async def _safe_send_message(t, rm=None):
        nonlocal last_message
        try:
            last_message = await bot.send_message(
                chat_id,
                t,
                reply_markup=rm,
                parse_mode='HTML',
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return last_message
        except TelegramBadRequest as b_err:
            if "can't parse entities" in str(b_err).lower() or "entity" in str(b_err).lower():
                last_message = await bot.send_message(
                    chat_id,
                    t,
                    reply_markup=rm,
                    parse_mode=None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                return last_message
            raise

    # 3. Отправка нового контента
    if media:
        # СЛУЧАЙ А: ОДНО медиа — отправляем фото/видео с подписью и кнопками (ОДНИМ СООБЩЕНИЕМ)
        if len(media) == 1:
            m = media[0]
            if len(text) <= 1024:
                try:
                    if m.file_type == 'photo':
                        last_message = await bot.send_photo(chat_id, m.file_id, caption=text, reply_markup=reply_markup, parse_mode='HTML')
                    elif m.file_type == 'video':
                        last_message = await bot.send_video(chat_id, m.file_id, caption=text, reply_markup=reply_markup, parse_mode='HTML')
                    elif m.file_type == 'animation':
                        last_message = await bot.send_animation(chat_id, m.file_id, caption=text, reply_markup=reply_markup, parse_mode='HTML')
                    elif m.file_type == 'document':
                        last_message = await bot.send_document(chat_id, m.file_id, caption=text, reply_markup=reply_markup, parse_mode='HTML')
                except TelegramBadRequest as b_err:
                    if "can't parse entities" in str(b_err).lower() or "entity" in str(b_err).lower():
                        if m.file_type == 'photo':
                            last_message = await bot.send_photo(chat_id, m.file_id, caption=text, reply_markup=reply_markup, parse_mode=None)
                        elif m.file_type == 'video':
                            last_message = await bot.send_video(chat_id, m.file_id, caption=text, reply_markup=reply_markup, parse_mode=None)
                        elif m.file_type == 'animation':
                            last_message = await bot.send_animation(chat_id, m.file_id, caption=text, reply_markup=reply_markup, parse_mode=None)
                        elif m.file_type == 'document':
                            last_message = await bot.send_document(chat_id, m.file_id, caption=text, reply_markup=reply_markup, parse_mode=None)
                    else:
                        raise
            else:
                # Текст слишком длинный для caption — отправляем медиа отдельно, потом текст с кнопками
                if m.file_type == 'photo':
                    await bot.send_photo(chat_id, m.file_id)
                elif m.file_type == 'video':
                    await bot.send_video(chat_id, m.file_id)
                elif m.file_type == 'animation':
                    await bot.send_animation(chat_id, m.file_id)
                elif m.file_type == 'document':
                    await bot.send_document(chat_id, m.file_id)
                await _safe_send_message(text, reply_markup)

        # СЛУЧАЙ Б: МНОГО медиа — отправляем альбом (кнопки придется слать отдельно)
        else:
            media_group = []
            first = True
            for m in media:
                caption_text = text if first and len(text) <= 1024 else None
                if m.file_type == 'photo':
                    media_group.append(InputMediaPhoto(media=m.file_id, caption=caption_text, parse_mode='HTML'))
                elif m.file_type == 'video':
                    media_group.append(InputMediaVideo(media=m.file_id, caption=caption_text, parse_mode='HTML'))
                first = False

            await bot.send_media_group(chat_id, media_group)
            # Если текст был слишком длинный для caption или есть кнопки
            if len(text) > 1024:
                await _safe_send_message(text, reply_markup)
            elif reply_markup:
                last_message = await bot.send_message(
                    chat_id,
                    "Меню:",
                    reply_markup=reply_markup,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
    else:
        # СЛУЧАЙ В: НЕТ медиа — просто текст с кнопками
        await _safe_send_message(text, reply_markup)

    return last_message


async def get_welcome_content_and_media():
    """Retrieve welcome message text and prioritized media without duplicates.

    Prefers media configured for welcome_msg; falls back to start_msg media if welcome_msg has none.
    """
    welcome_content = await get_content("welcome_msg")
    start_content = await get_content("start_msg")

    welcome_text = (
        welcome_content.text_content
        if (welcome_content and welcome_content.text_content)
        else (
            "👋 Привет! Добро пожаловать.\n\n"
            "Я твой проводник и персональный помощник по практикам и методикам Виталия Наумова.\n\n"
            "Я буду общаться на ты, чтобы наш контакт был ближе и проще."
        )
    )

    media = None
    if welcome_content and welcome_content.media:
        media = welcome_content.media
    elif start_content and start_content.media:
        media = start_content.media

    return welcome_text, media


async def send_canonical_welcome_message(target, bot: Bot = None) -> Message | None:
    """Send standalone welcome message (Message 1) with configured photo (or text)

    and ReplyKeyboardRemove(), without attaching navigation inline keyboard.

    If welcome text exceeds Telegram's media caption limit (1024 chars), delivers the
    configured media separately and the full welcome text as a separate message with
    ReplyKeyboardRemove(), preserving complete database content without truncation.
    """
    if bot is None:
        if hasattr(target, 'bot') and target.bot:
            bot = target.bot
        elif hasattr(target, 'message') and hasattr(target.message, 'bot') and target.message.bot:
            bot = target.message.bot

    chat_id = (
        getattr(getattr(target, "chat", None), "id", None)
        or getattr(getattr(getattr(target, "message", None), "chat", None), "id", None)
        or getattr(getattr(target, "from_user", None), "id", None)
    )

    welcome_text, media = await get_welcome_content_and_media()
    reply_markup = ReplyKeyboardRemove()

    async def _send_welcome_text_msg():
        if isinstance(target, CallbackQuery):
            if bot and hasattr(bot, "send_message") and callable(bot.send_message):
                return await bot.send_message(chat_id, welcome_text, reply_markup=reply_markup)
            elif hasattr(target, "message") and hasattr(target.message, "answer") and callable(target.message.answer):
                return await target.message.answer(welcome_text, reply_markup=reply_markup)
        if hasattr(target, "answer") and callable(target.answer):
            return await target.answer(welcome_text, reply_markup=reply_markup)
        elif bot and hasattr(bot, "send_message") and callable(bot.send_message):
            return await bot.send_message(chat_id, welcome_text, reply_markup=reply_markup)
        return None

    if media:
        m = media[0]
        # Telegram caption limit is 1024 characters.
        # When exceeded, deliver media separately followed by complete welcome text message.
        if len(welcome_text or "") > 1024:
            if m.file_type == "photo" and bot and hasattr(bot, "send_photo") and callable(bot.send_photo):
                await bot.send_photo(chat_id, m.file_id)
            elif m.file_type == "video" and bot and hasattr(bot, "send_video") and callable(bot.send_video):
                await bot.send_video(chat_id, m.file_id)
            return await _send_welcome_text_msg()

        try:
            if m.file_type == "photo" and bot and hasattr(bot, "send_photo") and callable(bot.send_photo):
                return await bot.send_photo(chat_id, m.file_id, caption=welcome_text, reply_markup=reply_markup)
            elif m.file_type == "video" and bot and hasattr(bot, "send_video") and callable(bot.send_video):
                return await bot.send_video(chat_id, m.file_id, caption=welcome_text, reply_markup=reply_markup)
        except TelegramBadRequest as b_err:
            if "caption" in str(b_err).lower():
                if m.file_type == "photo" and bot and hasattr(bot, "send_photo") and callable(bot.send_photo):
                    await bot.send_photo(chat_id, m.file_id)
                elif m.file_type == "video" and bot and hasattr(bot, "send_video") and callable(bot.send_video):
                    await bot.send_video(chat_id, m.file_id)
                return await _send_welcome_text_msg()
            raise

    return await _send_welcome_text_msg()


async def get_main_menu_content(
    user_id: int | None = None,
    *,
    include_media: bool = True,
) -> tuple[str, list, InlineKeyboardMarkup]:
    """Retrieve persisted main menu text, media and keyboard."""
    content = await get_content("start_msg")
    text = content.text_content if (content and content.text_content) else "Главное меню"
    media = list(content.media) if include_media and content and content.media else []
    reply_markup = await kb.main_menu_keyboard(user_id=user_id)
    return text, media, reply_markup


async def send_main_menu(
    target,
    bot: Bot = None,
    *,
    include_media: bool = True,
) -> Message | None:
    """Render the configured main menu as one anchored Telegram message."""
    user = getattr(target, "from_user", None)
    if user is None and getattr(target, "message", None) is not None:
        user = getattr(target.message, "from_user", None)
    user_id = getattr(user, "id", None)
    if user_id is None:
        user_id = getattr(getattr(target, "chat", None), "id", None)
    text, media, reply_markup = await get_main_menu_content(
        user_id=user_id,
        include_media=include_media,
    )
    from navigation_service import render_main_menu_anchor

    return await render_main_menu_anchor(
        target,
        text=text,
        media=media,
        reply_markup=reply_markup,
        bot=bot,
    )


async def send_temp_notification(bot, chat_id, text):
    try:
        msg = await bot.send_message(
            chat_id,
            text,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        await asyncio.sleep(3)
        try:
            await msg.delete()
        except:
            pass
    except:
        pass


async def is_admin_actor(user_id: int) -> bool:
    return await get_admin_role(user_id) is not None


def admin_logs_keyboard():
    builder = InlineKeyboardBuilder()
    for key, (label, _) in LOG_FILES.items():
        builder.button(text=label, callback_data=f"admin_log_view_{key}")
    builder.button(text="⬅️ Назад", callback_data="admin_section_logs")
    builder.adjust(1)
    return builder.as_markup()


def admin_log_view_keyboard(log_key: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="💾 Скачать файл", callback_data=f"admin_log_download_{log_key}")
    builder.button(text="🔄 Обновить", callback_data=f"admin_log_view_{log_key}")
    builder.button(text="⬅️ К логам", callback_data="admin_logs")
    builder.adjust(1)
    return builder.as_markup()


def read_log_tail(path: Path, line_limit: int = 80, char_limit: int = 3600) -> str:
    if not path.exists():
        return "Файл лога пока не создан."

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return f"Не удалось прочитать лог: {exc}"

    tail = "\n".join(lines[-line_limit:]).strip()
    if not tail:
        return "Лог пуст."
    if len(tail) > char_limit:
        tail = tail[-char_limit:]
        tail = "...\n" + tail
    return tail


def normalize_promo_code(code: str | None) -> str:
    """Canonical promo code normalization helper."""
    if not code:
        return ""
    return code.strip().upper()


_active_disclaimer_sends: set[int] = set()


async def check_and_request_ai_disclaimer(
    bot: Bot,
    user_id: int,
    chat_id: int,
    flow_type: str,
    payload: dict | None = None,
) -> bool:
    """
    Guard 1: Checks if user accepted the AI disclaimer.
    If yes, returns True.
    If no, stores pending AI flow and sends disclaimer message with consent keyboard, returns False.
    """
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user and user.ai_disclaimer_accepted_at and (user.ai_disclaimer_version or 0) >= 1:
            return True

        content_row = await session.get(Content, "disclaimer_text")
        disclaimer_text = (
            content_row.text_content
            if content_row and content_row.text_content
            else "<i>Бот не заменяет консультацию специалиста. Если ты в кризисной ситуации, обратись к профессионалу.</i>"
        )

    await store_pending_ai_flow(user_id, flow_type, payload or {})
    await bot.send_message(
        chat_id=chat_id,
        text=disclaimer_text,
        reply_markup=kb.ai_disclaimer_consent_keyboard(),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    return False


async def ensure_ai_disclaimer_sent(*args, **kwargs) -> bool:
    """
    Atomically claims the right to show the AI disclaimer via durable DB-backed claim/send/finalize.
    Supports:
      ensure_ai_disclaimer_sent(bot, user_id, [chat_id])
      ensure_ai_disclaimer_sent(session, bot, user_id, [chat_id])
    """
    if len(args) == 4:
        _, bot, user_id, chat_id = args
    elif len(args) == 3:
        if hasattr(args[0], "send_message"):
            bot, user_id, chat_id = args
        elif hasattr(args[0], "execute"):
            _, bot, user_id = args
            chat_id = user_id
        else:
            bot, user_id, chat_id = args
    elif len(args) == 2:
        bot, user_id = args
        chat_id = user_id
    else:
        bot = kwargs.get("bot")
        user_id = kwargs.get("user_id")
        chat_id = kwargs.get("chat_id", user_id)

    now = msk_now()
    disclaimer_text = "<i>Бот не заменяет консультацию специалиста. Если ты в кризисной ситуации, обратись к профессионалу.</i>"

    # 1. Atomic DB claim
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user or user.ai_disclaimer_shown:
            return False

        claim = await session.get(AiDisclaimerClaim, user_id)
        if claim:
            if claim.status == "delivered":
                if not user.ai_disclaimer_shown:
                    user.ai_disclaimer_shown = True
                    await session.commit()
                return False
            # Check lease expiry (30s)
            if claim.claimed_at and (now - claim.claimed_at).total_seconds() < 30:
                return False
            # Atomic CAS lease takeover
            res = await session.execute(
                update(AiDisclaimerClaim)
                .where(
                    AiDisclaimerClaim.user_id == user_id,
                    AiDisclaimerClaim.claimed_at == claim.claimed_at,
                    AiDisclaimerClaim.status != "delivered",
                )
                .values(claimed_at=now, status="claimed")
            )
            if res.rowcount == 0:
                return False
            await session.commit()
        else:
            claim = AiDisclaimerClaim(user_id=user_id, status="claimed", claimed_at=now)
            session.add(claim)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                return False

        content_row = await session.get(Content, "disclaimer_text")
        if content_row and content_row.text_content:
            disclaimer_text = content_row.text_content

    # 2. Attempt delivery
    delivery_ok = False
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=disclaimer_text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        delivery_ok = True
    except Exception as e:
        logger.error(f"Failed to send AI disclaimer to {chat_id}: {e}")

    # 3. Finalize or release claim
    async with async_session_maker() as session:
        claim = await session.get(AiDisclaimerClaim, user_id)
        user = await session.get(User, user_id)
        if delivery_ok:
            if claim:
                claim.status = "delivered"
                claim.delivered_at = msk_now()
            if user:
                user.ai_disclaimer_shown = True
            await session.commit()
            return True
        else:
            if claim:
                await session.delete(claim)
            await session.commit()
            return False


async def grant_referral_payment_reward(
    session,
    bot: Bot,
    paying_user_id: int,
    amount: float,
    source_payment_key: str,
):
    """
    Grants referral reward for successful cash payment of referred user.
    - 1st successful cash payment -> referral_first_pay_percent.
    - Every later unique successful cash payment -> referral_recurring_pay_percent.
    - Zero cash -> no reward.
    - Deduplicated by source_payment_key.
    """
    if amount <= 0:
        return

    sub_config = await session.get(SubscriptionConfig, 1)
    if not sub_config or not sub_config.referral_enabled:
        return

    paying_user = await session.get(User, paying_user_id)
    if not paying_user or not paying_user.referrer_id:
        return

    referrer = await session.get(User, paying_user.referrer_id)
    if not referrer:
        return

    # Check deduplication by source_payment_key
    existing_log = await session.scalar(
        select(ReferralPaymentLog).where(ReferralPaymentLog.source_payment_key == source_payment_key)
    )
    if existing_log:
        return

    prev_cash_count = await session.scalar(
        select(func.count(ReferralPaymentLog.id)).where(
            ReferralPaymentLog.referred_user_id == paying_user_id,
            ReferralPaymentLog.amount > 0,
        )
    ) or 0
    is_first = (prev_cash_count == 0)

    reward_mode = getattr(sub_config, "referral_reward_mode", "points")
    points_reward = 0
    bonus_days_reward = 0

    if reward_mode == "points":
        percent = sub_config.referral_first_pay_percent if is_first else sub_config.referral_recurring_pay_percent
        calc_points = int(round(amount * (percent / 100.0)))
        if calc_points > 0:
            from referral_service import grant_payment_points_reward
            granted = await grant_payment_points_reward(
                session,
                referrer_id=referrer.id,
                referred_user_id=paying_user_id,
                payment_key=source_payment_key,
                points=calc_points,
            )
            if granted:
                points_reward = calc_points
    else:
        bonus_days = sub_config.referral_pay_bonus_days
        if sub_config.referral_pay_bonus_first_only and not is_first:
            bonus_days = 0
        if bonus_days > 0:
            from referral_service import grant_payment_bonus_days_reward
            granted = await grant_payment_bonus_days_reward(
                session,
                referrer_id=referrer.id,
                referred_user_id=paying_user_id,
                payment_key=source_payment_key,
                bonus_days=bonus_days,
            )
            if granted:
                bonus_days_reward = bonus_days

    session.add(ReferralPaymentLog(
        referrer_id=referrer.id,
        referred_user_id=paying_user_id,
        amount=amount,
        source_payment_key=source_payment_key,
    ))

    if points_reward > 0:
        try:
            await bot.send_message(
                referrer.id,
                f"🎉 Твой реферал оплатил доступ! Тебе начислено <b>{points_reward} баллов</b>.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    elif bonus_days_reward > 0:
        try:
            await bot.send_message(
                referrer.id,
                f"🎉 Твой реферал оплатил доступ! Тебе начислено <b>{bonus_days_reward} бонусных дн.</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def get_active_reserved_points(session, user_id: int) -> int:
    now = msk_now()
    stmt = (
        select(func.coalesce(func.sum(PaymentIntent.points_reserved), 0))
        .where(
            PaymentIntent.user_id == user_id,
            PaymentIntent.status.in_(["pending", "authorized"]),
            PaymentIntent.points_reserved > 0,
            or_(
                PaymentIntent.provider == "prodamus",
                or_(PaymentIntent.expires_at == None, PaymentIntent.expires_at > now),
            ),
        )
    )
    result = await session.scalar(stmt)
    return int(result or 0)


def calculate_checkout_price(
    user: User | None,
    plan: SubscriptionPlan,
    available_points: int | None = None,
    points_to_use: int | None = None,
) -> dict:
    gross = float(plan.price)
    if plan.is_trial:
        discount_percent = 0
        promo_code_id = None
    else:
        user_sub = None
        user_promos = []
        if user:
            from sqlalchemy import inspect
            try:
                insp = inspect(user)
                if "subscription" in insp.dict:
                    user_sub = user.subscription
                if "promo_codes" in insp.dict:
                    user_promos = list(user.promo_codes or [])
            except Exception:
                user_sub = getattr(user, "subscription", None)
                user_promos = list(getattr(user, "promo_codes", []) or [])
        discount_percent, promo_code_id = _calculate_effective_promo(user_sub, user_promos, plan.id)
    promo_discount_rub = round(gross * (discount_percent / 100.0), 2)
    after_promo = max(0.0, round(gross - promo_discount_rub, 2))

    if available_points is None:
        user_points = user.referral_balance if user and user.referral_balance else 0
    else:
        user_points = max(0, available_points)

    if points_to_use is not None:
        points_reserved = min(user_points, max(0, points_to_use), int(after_promo))
    else:
        points_reserved = min(user_points, int(after_promo))

    final_amount = max(0.0, round(after_promo - points_reserved, 2))
    return {
        "gross_amount": gross,
        "discount_percent": discount_percent,
        "promo_discount_rub": promo_discount_rub,
        "points_reserved": points_reserved,
        "final_amount": final_amount,
        "promo_code_id": promo_code_id,
    }


async def create_payment_intent(
    session,
    user_id: int,
    plan_id: int,
    provider: str,
    points_to_use: int | None = None,
    bot: Bot | None = None,
    notify_user: bool = True,
) -> PaymentIntent:
    # 1. DB-level serialization lock:
    # In SQLite, BEGIN IMMEDIATE serializes write transactions immediately.
    # In PostgreSQL / RDBMS, with_for_update locks the user row for update.
    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind else ""
    if dialect_name == "sqlite":
        for attempt in range(5):
            try:
                await session.execute(text("BEGIN IMMEDIATE"))
                break
            except Exception as e:
                err_msg = str(e).lower()
                if "cannot start a transaction" in err_msg or "in a transaction" in err_msg:
                    # Transaction already opened by a previous SELECT in this session.
                    # Force SQLite to acquire a RESERVED write lock immediately via dummy UPDATE:
                    await session.execute(
                        text("UPDATE users SET referral_balance = referral_balance WHERE id = :uid"),
                        {"uid": user_id},
                    )
                    break
                if "locked" in err_msg or "busy" in err_msg:
                    if attempt < 4:
                        await asyncio.sleep(0.05 * (2 ** attempt))
                        continue
                raise
    else:
        await session.execute(select(User.id).where(User.id == user_id).with_for_update())

    user = await session.get(
        User,
        user_id,
        options=[
            selectinload(User.subscription),
            selectinload(User.promo_codes).selectinload(PromoCode.applicable_plans),
        ]
    )
    plan = await session.get(SubscriptionPlan, plan_id)
    if not plan:
        raise ValueError("Plan not found")

    active_reserved = await get_active_reserved_points(session, user_id)
    user_balance = (user.referral_balance or 0) if user else 0
    available_points = max(0, user_balance - active_reserved)

    pricing = calculate_checkout_price(
        user,
        plan,
        available_points=available_points,
        points_to_use=points_to_use,
    )
    token = secrets.token_hex(16)
    final_amt = pricing["final_amount"]

    if final_amt <= 0.0:
        intent_provider = "zero_cash"
        intent_status = "pending"
        paid_at = None
        expires_at = None
    elif provider in ("telegram", "telegram_pay", "tg"):
        intent_provider = "telegram_pay"
        intent_status = "pending"
        paid_at = None
        expires_at = msk_now() + timedelta(minutes=30)
    elif provider in ("prodamus",):
        intent_provider = "prodamus"
        intent_status = "pending"
        paid_at = None
        expires_at = None
    else:
        intent_provider = provider
        intent_status = "pending"
        paid_at = None
        expires_at = msk_now() + timedelta(minutes=30)

    intent = PaymentIntent(
        token=token,
        user_id=user_id,
        plan_id=plan_id,
        promo_code_id=pricing.get("promo_code_id"),
        gross_amount=pricing["gross_amount"],
        promo_discount_rub=pricing["promo_discount_rub"],
        points_reserved=pricing["points_reserved"],
        final_amount=final_amt,
        provider=intent_provider,
        status=intent_status,
        expires_at=expires_at,
        paid_at=paid_at,
    )
    session.add(intent)
    await session.commit()

    if final_amt <= 0.0 and bot:
        await settle_payment_intent(session, bot, intent, payment_charge_id=f"zero_{intent.token}", notify_user=notify_user)

    return intent


async def settle_payment_intent(
    session,
    bot: Bot,
    intent: PaymentIntent,
    payment_charge_id: str = "",
    prodamus_subscription_id: int | None = None,
    prodamus_profile_id: int | None = None,
    notify_user: bool = True,
):
    if intent.paid_at or intent.status in ("paid", "settling", "cancelled", "expired"):
        return

    now = msk_now()
    is_pre_checkout_approved = bool(getattr(intent, "pre_checkout_approved_at", None) or intent.status == "authorized")
    if intent.expires_at and intent.expires_at < now and not is_pre_checkout_approved:
        await session.execute(
            update(PaymentIntent)
            .where(PaymentIntent.id == intent.id, PaymentIntent.status.in_(["pending", "authorized"]))
            .values(status="expired")
        )
        await session.commit()
        intent.status = "expired"
        raise ValueError("Payment intent has expired")

    # Atomic CAS claim transition: only ONE caller can transition from pending/authorized to settling
    claim_res = await session.execute(
        update(PaymentIntent)
        .where(PaymentIntent.id == intent.id, PaymentIntent.status.in_(["pending", "authorized"]))
        .values(status="settling")
    )
    if claim_res.rowcount == 0:
        return

    intent.status = "settling"

    user = await session.get(User, intent.user_id)
    plan = await session.get(SubscriptionPlan, intent.plan_id)
    if not user or not plan:
        return

    if intent.points_reserved > 0:
        user.referral_balance = max(0, (user.referral_balance or 0) - intent.points_reserved)

    if plan.is_trial:
        session.add(TrialUsageHistory(user_id=user.id, plan_id=plan.id, used_at=now))

    user_sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user.id))
    end_date = _extend_subscription_end_date(
        user_sub.end_date if user_sub else None,
        now,
        plan.duration_value,
        plan.duration_unit,
    )

    if intent.provider in ("telegram_pay", "telegram", "tg"):
        provider_name = "TelegramPay"
    elif intent.provider == "prodamus":
        provider_name = "Prodamus"
    else:
        provider_name = "Points"

    sub_id = prodamus_subscription_id or (getattr(user_sub, "prodamus_subscription_id", None) if user_sub else None)
    prof_id = prodamus_profile_id or (getattr(user_sub, "prodamus_profile_id", None) if user_sub else None)

    if user_sub:
        user_sub.plan_id = plan.id
        user_sub.start_date = user_sub.start_date if user_sub.end_date and user_sub.end_date > now else now
        user_sub.end_date = end_date
        user_sub.auto_renewal = bool(sub_id and plan.allow_auto_renewal) if provider_name == "Prodamus" else plan.allow_auto_renewal
        user_sub.payment_provider = provider_name
        user_sub.payment_attempt_count = 0
        user_sub.last_payment_attempt = None
        if sub_id:
            user_sub.prodamus_subscription_id = sub_id
        if prof_id:
            user_sub.prodamus_profile_id = prof_id
    else:
        session.add(UserSubscription(
            user_id=user.id,
            plan_id=plan.id,
            start_date=now,
            end_date=end_date,
            auto_renewal=bool(sub_id and plan.allow_auto_renewal) if provider_name == "Prodamus" else plan.allow_auto_renewal,
            payment_provider=provider_name,
            payment_attempt_count=0,
            discount_percent=0,
            prodamus_subscription_id=sub_id,
            prodamus_profile_id=prof_id,
        ))

    user.subscription_end = end_date

    session.add(Transaction(
        user_id=user.id,
        amount=intent.final_amount,
        currency="RUB",
        provider=payment_charge_id or provider_name,
        status="success",
    ))

    await grant_referral_payment_reward(
        session,
        bot,
        paying_user_id=user.id,
        amount=intent.final_amount,
        source_payment_key=f"pi_{intent.token}_{payment_charge_id}",
    )

    intent.status = "paid"
    intent.paid_at = now
    if not intent.provider_payment_id and payment_charge_id:
        intent.provider_payment_id = payment_charge_id

    await session.execute(
        update(PaymentIntent)
        .where(PaymentIntent.id == intent.id)
        .values(
            status="paid",
            paid_at=now,
            provider_payment_id=intent.provider_payment_id,
        )
    )

    await session.commit()

    if notify_user:
        try:
            await bot.send_message(
                user.id,
                f"✅ Доступ к образовательной программе по тарифу «{plan.name}» успешно оформлен!\n"
                f"Действует до: {format_msk(end_date, '%d.%m.%Y %H:%M')} МСК."
            )
        except Exception:
            pass

    sub_config = await session.get(SubscriptionConfig, 1)
    if sub_config and sub_config.notifications_enabled:
        admin_ids = await get_all_admin_ids()
        for admin_id in set(admin_ids):
            try:
                await bot.send_message(
                    admin_id,
                    f"🔔 Новый платеж ({provider_name})!\n\n"
                    f"Пользователь: {user.id}\n"
                    f"Тариф программы: {plan.name}\n"
                    f"Сумма: {intent.final_amount:.2f} руб."
                )
            except Exception:
                pass


def _extend_subscription_end_date(base_end: datetime | None, now: datetime, duration_value: int, duration_unit: str) -> datetime:
    base = base_end if base_end and base_end > now else now
    if duration_unit == "months":
        return base + relativedelta(months=duration_value)
    return base + timedelta(days=duration_value)


def _calculate_effective_discount(user_sub: UserSubscription | None, user_promos: list[PromoCode], plan_id: int | None) -> int:
    discount_percent, _ = _calculate_effective_promo(user_sub, user_promos, plan_id)
    return discount_percent


def _calculate_effective_promo(user_sub: UserSubscription | None, user_promos: list[PromoCode], plan_id: int | None) -> tuple[int, int | None]:
    discount_percent = user_sub.discount_percent if user_sub else 0
    selected_promo_id = None
    if not plan_id:
        return discount_percent, selected_promo_id

    plan_specific_promo = next((
        p for p in user_promos
        if not p.applies_to_all_plans and any(ap.id == plan_id for ap in p.applicable_plans)
    ), None)
    if plan_specific_promo and plan_specific_promo.discount_percent > discount_percent:
        discount_percent = plan_specific_promo.discount_percent
        selected_promo_id = plan_specific_promo.id

    all_plans_promo = next((p for p in user_promos if p.applies_to_all_plans), None)
    if all_plans_promo and all_plans_promo.discount_percent > discount_percent:
        discount_percent = all_plans_promo.discount_percent
        selected_promo_id = all_plans_promo.id

    return discount_percent, selected_promo_id


async def _ensure_subscription_config(session):
    config = await session.get(SubscriptionConfig, 1)
    if not config:
        config = SubscriptionConfig(id=1)
        session.add(config)
        await session.flush()
    return config


async def _sync_legacy_subscription_end(session, user_id: int):
    user = await session.get(User, user_id)
    user_sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    if user:
        user.subscription_end = user_sub.end_date if user_sub else None


def _duration_text(plan: SubscriptionPlan) -> str:
    return f"{plan.duration_value} {'мес.' if plan.duration_unit == 'months' else 'дн.'}"


def _format_user_ref(user: User | None, user_id: int) -> str:
    if not user:
        return f"[id=<code>{user_id}</code>]"
    user_ref = f"{user.first_name or ''}"
    if user.username:
        user_ref += f" (@{user.username})"
    user_ref += f" [id=<code>{user_id}</code>]"
    return user_ref


def _sort_nested_payload(data):
    if isinstance(data, dict):
        return {k: _sort_nested_payload(v) for k, v in sorted(data.items())}
    if isinstance(data, list):
        return [_sort_nested_payload(item) for item in data]
    return data


def _stringify_prodamus_payload(data):
    if isinstance(data, dict):
        return {k: _stringify_prodamus_payload(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_stringify_prodamus_payload(item) for item in data]
    if data is None:
        return ""
    return str(data)


def _prodamus_signature_payload(payload: dict) -> str:
    sorted_payload = _sort_nested_payload(_stringify_prodamus_payload(payload))
    json_str = json.dumps(sorted_payload, ensure_ascii=False, separators=(",", ":"))
    return json_str.replace("/", r"\/")


def _build_prodamus_signature(payload: dict, secret_key: str) -> str:
    return hmac.new(
        secret_key.encode("utf-8"),
        _prodamus_signature_payload(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _flatten_prodamus_query(prefix: str, value):
    if isinstance(value, dict):
        pairs = []
        for key, item in value.items():
            next_prefix = f"{prefix}[{key}]" if prefix else str(key)
            pairs.extend(_flatten_prodamus_query(next_prefix, item))
        return pairs
    if isinstance(value, list):
        pairs = []
        for index, item in enumerate(value):
            next_prefix = f"{prefix}[{index}]"
            pairs.extend(_flatten_prodamus_query(next_prefix, item))
        return pairs
    return [(prefix, value)]


def _prodamus_api_base(form_url: str) -> str:
    parsed = urlparse(form_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/"


async def _call_prodamus_set_activity(
        config: SubscriptionConfig,
        *,
        subscription_id: int,
        active_user: int,
        active_manager: int,
        profile_id: int | None,
        tg_user_id: int | None,
) -> tuple[bool, str]:
    if not config.prodamus_url or not config.prodamus_secret_key:
        return False, "Prodamus не настроен."

    api_base = _prodamus_api_base(config.prodamus_url)
    if not api_base:
        return False, "Некорректный URL Prodamus."

    payload = {
        "subscription": subscription_id,
        "active_user": active_user,
        "active_manager": active_manager,
    }
    if profile_id:
        payload["profile"] = profile_id
    elif tg_user_id:
        payload["tg_user_id"] = tg_user_id
    else:
        return False, "Не найден идентификатор профиля клиента в Prodamus."

    payload["signature"] = _build_prodamus_signature(payload, config.prodamus_secret_key)
    endpoint = f"{api_base}rest-api-1/setactivity/"

    try:
        async with ClientSession() as http:
            async with http.post(endpoint, json=payload, timeout=20) as response:
                response_text = await response.text()
                if response.status >= 400:
                    return False, f"HTTP {response.status}: {response_text[:200]}"
                try:
                    response_json = json.loads(response_text)
                except json.JSONDecodeError:
                    return True, response_text[:200]
    except Exception as exc:
        return False, str(exc)

    if isinstance(response_json, dict):
        if response_json.get("status") in {"ok", "success", True}:
            return True, "ok"
        if response_json.get("success") is True:
            return True, "ok"
        error_text = response_json.get("message") or response_json.get("error") or json.dumps(response_json, ensure_ascii=False)
        return False, error_text[:200]

    return True, str(response_json)[:200]


async def _show_admin_edit_plan_menu(bot: Bot, chat_id: int, message_id: int, plan_id: int):
    async with async_session_maker() as session:
        plan = await session.get(
            SubscriptionPlan,
            plan_id,
            options=[selectinload(SubscriptionPlan.upgrades_to_plan)]
        )
        if not plan:
            await bot.edit_message_text("Тариф не найден.", chat_id=chat_id, message_id=message_id)
            return

        upgrade_name = plan.upgrades_to_plan.name if plan.upgrades_to_plan else "не назначен"
        text = (
            f"🎫 <b>{html.escape(plan.name)}</b>\n\n"
            f"💰 Цена: {plan.price:.2f} руб.\n"
            f"⏳ Длительность: {_duration_text(plan)}\n"
            f"📝 Описание: {html.escape(plan.description or 'не задано')}\n"
            f"Статус: {'✅ Активен' if plan.is_active else '❌ Неактивен'}\n"
            f"Пробный: {'да' if plan.is_trial else 'нет'}\n"
            f"Автопродление: {'вкл' if plan.allow_auto_renewal else 'выкл'}\n"
            f"Только для админов: {'да' if plan.admin_only else 'нет'}\n"
            f"Код подписки в Prodamus: {plan.prodamus_subscription_id or 'не задан'}\n"
            f"Лимит автосписаний: {plan.prodamus_autopayments_limit or 'по умолчанию Prodamus'}"
        )
        if plan.is_trial:
            text += (
                f"\nПереход на тариф: {html.escape(upgrade_name)}"
                f"\nПовторный пробный период: через {plan.trial_cooldown_days} дн."
            )

        await bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=kb.admin_plan_edit_keyboard(
                plan.id,
                plan.is_active,
                is_trial=plan.is_trial,
                allow_auto_renewal=plan.allow_auto_renewal,
                admin_only=plan.admin_only,
            ),
            parse_mode="HTML",
        )


async def _show_admin_edit_promo_menu(bot: Bot, chat_id: int, message_id: int, promo_id: int):
    async with async_session_maker() as session:
        promo = await session.get(PromoCode, promo_id, options=[selectinload(PromoCode.applicable_plans)])
        if not promo:
            await bot.edit_message_text("Промокод не найден.", chat_id=chat_id, message_id=message_id)
            return

        applies_to_text = "✅ Все тарифы" if promo.applies_to_all_plans else f"Только {len(promo.applicable_plans)} тарифов"
        text = (
            f"🎁 <b>{html.escape(promo.code)}</b>\n\n"
            f"Скидка: {promo.discount_percent}%\n"
            f"Бесплатные дни: {promo.free_days}\n"
            f"Использовано: {promo.times_used}/{promo.max_uses}\n"
            f"Применяется к: {applies_to_text}\n"
            f"Статус: {'✅ Активен' if promo.is_active else '❌ Неактивен'}"
        )
        await bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=kb.admin_edit_promo_keyboard(promo.id, promo.is_active),
            parse_mode="HTML",
        )


async def _show_assign_promo_to_plan_menu(bot: Bot, chat_id: int, message_id: int, promo_id: int, page: int = 0):
    page_size = 10
    async with async_session_maker() as session:
        promo = await session.get(PromoCode, promo_id, options=[selectinload(PromoCode.applicable_plans)])
        if not promo:
            await bot.edit_message_text("Промокод не найден.", chat_id=chat_id, message_id=message_id)
            return

        assigned_plan_ids = {plan.id for plan in promo.applicable_plans}
        total_plans = await session.scalar(select(func.count(SubscriptionPlan.id))) or 0
        total_pages = max(1, math.ceil(total_plans / page_size)) if total_plans else 1
        page = max(0, min(page, total_pages - 1))
        all_plans = (
            await session.execute(
                select(SubscriptionPlan)
                .order_by(SubscriptionPlan.name)
                .offset(page * page_size)
                .limit(page_size)
            )
        ).scalars().all()

    text = f"📎 Привязка тарифов к промокоду «{html.escape(promo.code)}» (стр. {page + 1}/{total_pages})"
    if not promo.applies_to_all_plans:
        text += "\n\nНажмите на тариф, чтобы применить или убрать промокод."

    await bot.edit_message_text(
        text,
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=kb.assign_promo_to_plan_keyboard(
            promo_id, all_plans, assigned_plan_ids, promo.applies_to_all_plans, page, total_pages
        ),
        parse_mode="HTML",
    )


async def _send_subscription_info(user_id: int, chat_id: int, bot: Bot):
    async with async_session_maker() as session:
        user_sub = await session.scalar(
            select(UserSubscription)
            .options(selectinload(UserSubscription.plan).selectinload(SubscriptionPlan.upgrades_to_plan))
            .where(UserSubscription.user_id == user_id)
        )
        user = await session.get(
            User,
            user_id,
            options=[selectinload(User.promo_codes).selectinload(PromoCode.applicable_plans)]
        )
        user_promos = user.promo_codes if user else []
        config = await _ensure_subscription_config(session)
        subscription_content = await session.get(
            Content,
            "subscription_info",
            options=[selectinload(Content.media)],
        )
        subscription_media = subscription_content.media if subscription_content and subscription_content.media else None
        referral_info = None
        if config.referral_enabled:
            referral_info = {
                "enabled": True,
                "sub_btn_name": "🤝 Бонус за приглашение",
            }

    sub_info = None
    text = ""
    msk = timezone(timedelta(hours=3))
    now = msk_now()

    if not config.subscriptions_enabled:
        text = "Система доступа к образовательной программе сейчас выключена. Доступ открыт без оплаты."
    elif user_sub and user_sub.end_date > now and user_sub.plan_id is not None:
        end_date_msk = user_sub.end_date.astimezone(msk)
        plan = user_sub.plan
        plan_name_with_duration = "Неизвестный тариф"
        if plan:
            duration_unit_text = "дн." if plan.duration_unit == 'days' else "мес."
            plan_name_with_duration = f"{plan.name} ({plan.duration_value} {duration_unit_text})"

        plan_allows_renewal = getattr(plan, 'allow_auto_renewal', True) if plan else True
        renewal_line = ""
        if plan_allows_renewal:
            renewal_line = f"\n<b>Автопродление:</b> {'✅ Включено' if user_sub.auto_renewal else '❌ Выключено'}"
        text = (
            f"<b>⭐️ Доступ к образовательной программе активен</b>\n\n"
            f"<b>Тариф программы:</b> {plan_name_with_duration}\n"
            f"<b>Действует до:</b> {end_date_msk.strftime('%d.%m.%Y %H:%M')} МСК"
            f"{renewal_line}"
        )

        plan_to_charge = None
        price_line = ""
        target_plan_allows_renewal = False
        if plan and plan.is_trial and plan.upgrades_to_plan:
            plan_to_charge = plan.upgrades_to_plan
            target_plan_allows_renewal = getattr(plan_to_charge, 'allow_auto_renewal', True)
        elif plan and not plan.is_trial:
            plan_to_charge = plan
            target_plan_allows_renewal = getattr(plan_to_charge, 'allow_auto_renewal', True)
            if target_plan_allows_renewal:
                price_line = "\n(применится при автопродлении)"

        if plan_to_charge:
            plan_discount_percent = _calculate_effective_discount(user_sub, list(user_promos or []), plan_to_charge.id)
            if plan_discount_percent > 0:
                text += f"\n<b>Твоя скидка:</b> {plan_discount_percent}%"

            final_price_to_charge = plan_to_charge.price * (1 - plan_discount_percent / 100)
            if plan and plan.is_trial:
                up_duration_unit = "дн" if plan_to_charge.duration_unit == 'days' else "мес"
                price_line = "\n<b>Стоимость основного тарифа"
                if plan_discount_percent > 0:
                    price_line += " (со скидкой)"
                price_line += f":</b> {final_price_to_charge:.2f} руб за {plan_to_charge.duration_value} {up_duration_unit}"
                if target_plan_allows_renewal:
                    price_line += " (спишется при включенном автопродлении)"
                else:
                    price_line += " (оформляется вручную после окончания пробного периода)"
            else:
                price_line = "\n<b>Стоимость"
                if plan_discount_percent > 0:
                    price_line += " (со скидкой)"
                price_line += f":</b> {final_price_to_charge:.2f} руб."
            text += price_line

        sub_info = {
            'auto_renewal': user_sub.auto_renewal,
            'is_trial': plan.is_trial if plan else False,
            'allow_auto_renewal': getattr(plan, 'allow_auto_renewal', True) if plan else True
        }
    elif user_sub and user_sub.end_date > now and user_sub.plan_id is None and user_sub.payment_provider in ['Trial Promo', 'Trial Welcome']:
        remaining_time = user_sub.end_date - now
        remaining_seconds = remaining_time.total_seconds()
        remaining_days_display = 0 if remaining_seconds <= 0 else int((remaining_seconds + 86399) // 86400)
        remaining_hours = max(0, int(remaining_time.total_seconds() / 3600))

        content_obj = subscription_content
        base_text = content_obj.text_content if content_obj and content_obj.text_content else (
            "У тебя нет активного доступа ко всем функциям.\n\nОформи доступ, чтобы пользоваться всеми возможностями бота!"
        )

        extra = []
        if remaining_days_display > 1:
            extra.append(f"Доступные бонусные дни: {remaining_days_display}")
        elif remaining_days_display == 1:
            if remaining_hours > 0:
                extra.append(f"Доступные бонусные часы: ~{remaining_hours}")
            else:
                extra.append("Бонусный доступ скоро закончится.")
        elif remaining_days_display == 0:
            extra.append("Бонусный доступ скоро закончится.")

        if user_sub.discount_percent > 0:
            extra.append(f"🔥 У тебя есть скидка <b>{user_sub.discount_percent}%</b>, которая сгорит, если не оформить доступ до окончания бонусных дней!")

        if extra:
            text = f"{base_text}\n\n" + "\n".join(extra)
        else:
            text = base_text
    else:
        content_obj = subscription_content
        base_text = content_obj.text_content if content_obj and content_obj.text_content else (
            "У тебя нет активного доступа ко всем функциям.\n\nОформи доступ, чтобы пользоваться всеми возможностями бота!"
        )
        discount_percent = user_sub.discount_percent if user_sub and user_sub.discount_percent > 0 else 0
        for p in user_promos:
            if p.applies_to_all_plans and p.discount_percent > discount_percent:
                discount_percent = p.discount_percent
        if discount_percent > 0:
            text = f"{base_text}\n\nДоступная скидка: <b>{discount_percent}%</b> (применяется к подходящим тарифам)"
        else:
            text = base_text

    reply_markup = await kb.configured_subscription_info_keyboard(sub_info, referral_info=referral_info)
    if subscription_media and len(text) <= 1024:
        media = subscription_media[0]
        if media.file_type == "photo":
            await bot.send_photo(chat_id, media.file_id, caption=text, parse_mode="HTML", reply_markup=reply_markup)
            return
        if media.file_type == "video":
            await bot.send_video(chat_id, media.file_id, caption=text, parse_mode="HTML", reply_markup=reply_markup)
            return
    if subscription_media:
        media = subscription_media[0]
        if media.file_type == "photo":
            await bot.send_photo(chat_id, media.file_id)
        elif media.file_type == "video":
            await bot.send_video(chat_id, media.file_id)
    await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, command: Command = None):
    await state.clear()

    referrer_id = None
    start_action = None

    if command and command.args:
        start_action, start_value = parse_start_action(command.args)
        try:
            if start_action == "referral":
                candidate_id = int(start_value)
                if candidate_id != message.from_user.id:
                    referrer_id = candidate_id
        except:
            pass

    # 1. Атомарная регистрация реферала и приветственный бонус
    is_new_referral = False
    welcome_bonus_applied = False
    should_show_welcome = False
    async with async_session_maker() as session:
        if referrer_id:
            ref_user = await session.get(User, referrer_id)
            if not ref_user:
                referrer_id = None

        user = await session.get(User, message.from_user.id)
        if not user:
            try:
                user = User(
                    id=message.from_user.id,
                    username=message.from_user.username,
                    first_name=message.from_user.first_name,
                    is_admin=(message.from_user.id in OWNER_IDS),
                    referrer_id=referrer_id,
                    welcome_shown_at=msk_now(),
                )
                session.add(user)
                should_show_welcome = True
                if referrer_id:
                    is_new_referral = True
                sub_config = await _ensure_subscription_config(session)
                if sub_config.welcome_bonus_days > 0:
                    bonus_end = msk_now() + timedelta(days=sub_config.welcome_bonus_days)
                    session.add(UserSubscription(
                        user_id=message.from_user.id,
                        plan_id=None,
                        start_date=msk_now(),
                        end_date=bonus_end,
                        auto_renewal=False,
                        payment_provider="Trial Welcome",
                        payment_attempt_count=0,
                        discount_percent=0,
                    ))
                    user.subscription_end = bonus_end
                    welcome_bonus_applied = True
                await session.flush()
            except IntegrityError:
                await session.rollback()
                user = await session.get(User, message.from_user.id)
                is_new_referral = False
        else:
            if user.is_blocked:
                if hasattr(message, "answer"):
                    await message.answer("⛔️ Ты заблокирован(а) администратором.")
                elif getattr(message, "bot", None):
                    cid = getattr(getattr(message, "chat", None), "id", message.from_user.id)
                    await message.bot.send_message(cid, "⛔️ Ты заблокирован(а) администратором.")
                return
            user.username = getattr(message.from_user, "username", None)
            user.first_name = getattr(message.from_user, "first_name", None)
            welcome_shown_at = await _read_welcome_shown_at(session, user)
            if welcome_shown_at is None:
                user.welcome_shown_at = msk_now()
                should_show_welcome = True
            if referrer_id and not user.referrer_id and referrer_id != user.id:
                # Atomic CAS update
                res = await session.execute(
                    update(User)
                    .where(User.id == user.id, User.referrer_id == None)
                    .values(referrer_id=referrer_id)
                )
                if res.rowcount > 0:
                    is_new_referral = True
                    user.referrer_id = referrer_id

        if is_new_referral and referrer_id:
            sub_conf = await session.get(SubscriptionConfig, 1)
            ref_conf = await session.get(ReferralConfig, 1)
            if sub_conf and sub_conf.referral_enabled and ref_conf and ref_conf.points_per_invite > 0:
                from referral_service import grant_registration_reward
                reward_granted = await grant_registration_reward(
                    session,
                    referrer_id=referrer_id,
                    referred_user_id=message.from_user.id,
                    points=ref_conf.points_per_invite,
                )
                if reward_granted:
                    try:
                        await message.bot.send_message(
                            referrer_id,
                            f"🎉 По твоей ссылке присоединился: {message.from_user.first_name or 'Новый пользователь'}! Тебе начислено {ref_conf.points_per_invite} баллов."
                        )
                    except Exception:
                        pass

        user.last_activity_at = msk_now()
        await session.commit()

    # 2. Отправка welcome_msg только при первом запуске
    chat_id = getattr(getattr(message, "chat", None), "id", message.from_user.id)
    if should_show_welcome:
        await send_canonical_welcome_message(message, bot=message.bot)

    if welcome_bonus_applied:
        bonus_msg = (
            "🎁 <b>Тебе начислен приветственный бонус!</b>\n"
            "Активирован бонусный доступ. Подробности доступны в разделе подписки."
        )
        if hasattr(message, "answer") and callable(message.answer):
            await message.answer(bonus_msg, parse_mode="HTML")
        elif hasattr(message, "bot") and hasattr(message.bot, "send_message") and callable(message.bot.send_message):
            await message.bot.send_message(chat_id, bonus_msg, parse_mode="HTML")

    # 3. Опрос профиля (имя/пол)
    target_action = "funnel_intro" if start_action == "funnel" else "main_menu"
    from funnel import maybe_ask_profile, get_or_create_funnel_state, get_resume_mode, show_resume_menu, continue_active_funnel
    if await maybe_ask_profile(message, state, target_action):
        return

    # 4. Если профиль уже заполнен:
    if start_action == "funnel":
        await start_funnel_intro(message, bot=message.bot)
        return

    await send_main_menu(message, bot=message.bot, include_media=not should_show_welcome)


@router.message(Command("menu"))
async def cmd_menu(message: Message, bot: Bot = None, state: FSMContext = None):
    if state:
        await state.clear()
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    should_show_welcome = False
    if user_id:
        async with async_session_maker() as session:
            user = await session.get(User, user_id)
            if user:
                user.last_activity_at = msk_now()
                welcome_shown_at = await _read_welcome_shown_at(session, user)
                if welcome_shown_at is None:
                    user.welcome_shown_at = msk_now()
                    should_show_welcome = True
                await session.commit()
    if should_show_welcome:
        await send_canonical_welcome_message(message, bot=bot)
    await send_main_menu(message, bot=bot, include_media=not should_show_welcome)


@router.message(Command("settings"))
async def cmd_settings(message: Message, *args, **kwargs):
    bot = kwargs.get("bot")
    for a in args:
        if isinstance(a, Bot) or hasattr(a, "send_message"):
            bot = a
    if not bot and hasattr(message, "bot"):
        bot = message.bot

    async with async_session_maker() as session:
        user = await session.get(User, message.from_user.id)
    if not user:
        return
    text = _settings_text(user)
    if bot and hasattr(message, "chat"):
        await bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=kb.user_settings_keyboard(user))
    elif hasattr(message, "answer"):
        await message.answer(text, parse_mode="HTML", reply_markup=kb.user_settings_keyboard(user))


@router.message(Command("subscription"))
async def cmd_subscription(message: Message, bot: Bot):
    await _send_subscription_info(message.from_user.id, message.chat.id, bot)


@router.callback_query(F.data == "open_extended_menu")
async def back_to_extended_menu(callback: CallbackQuery):
    content = await get_content("extended_menu_intro")
    text = content.text_content if content and content.text_content else "Выбери раздел:"
    await send_smart_content(callback, text, content.media if content else None, await kb.extended_menu_keyboard())


@router.callback_query(F.data.in_(["back_to_main_menu_text", "back_to_main_menu"]))
async def back_to_main(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext | None = None,
):
    if state is not None:
        await state.clear()
    await touch_user_activity(callback.from_user.id)
    try:
        await callback.answer()
    except Exception:
        pass
    await send_main_menu(callback, bot=bot)


@router.callback_query(F.data == "menu_closed_club")
async def show_closed_club(callback: CallbackQuery, bot: Bot):
    if not await is_menu_section_enabled("club"):
        await send_smart_content(callback, "Этот раздел сейчас недоступен.", None, await kb.extended_menu_keyboard(), bot)
        return
    content = await get_content("closed_club_intro")
    await send_smart_content(callback, content.text_content, content.media, await kb.closed_club_keyboard(), bot)


@router.callback_query(F.data == "menu_referral")
async def show_referral(callback: CallbackQuery, bot: Bot):
    if not await is_menu_section_enabled("referral"):
        await send_smart_content(callback, "Этот раздел сейчас недоступен.", None, await kb.extended_menu_keyboard(), bot)
        return
    content = await get_content("referral_intro")
    await send_smart_content(callback, content.text_content, content.media, await kb.configured_referral_keyboard(), bot)


@router.callback_query(F.data == "menu_vitaly")
async def show_vitaly(callback: CallbackQuery, bot: Bot):
    if not await is_menu_section_enabled("vitaly"):
        await send_smart_content(callback, "Этот раздел сейчас недоступен.", None, await kb.extended_menu_keyboard(), bot)
        return
    content = await get_content("vitaly_intro")
    await send_smart_content(callback, content.text_content, content.media, await kb.vitaly_keyboard(), bot)


async def _send_access_denied(event, text: str, reply_markup=None):
    if reply_markup is None:
        reply_markup = kb.payment_alert_keyboard()
    if hasattr(event, 'answer') and callable(event.answer):
        try:
            await event.answer(text, reply_markup=reply_markup)
            return
        except Exception:
            pass
    if hasattr(event, 'message') and event.message:
        await event.message.answer(text, reply_markup=reply_markup)


async def check_paid_access(event, bot: Bot, topic_slug: str | None = None, section: str | None = None) -> bool:
    user_id = event.from_user.id
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user.is_admin:
            return True

        # Проверяем, платный ли раздел
        if topic_slug:
            topic = await session.scalar(select(Topic).where(Topic.slug == topic_slug))
            if topic and not topic.is_paid:
                return True
        if section == "habits":
            habit_conf = await session.get(HabitConfig, 1)
            if habit_conf and not habit_conf.is_paid:
                return True

        pay_conf = await session.get(PaymentConfig, 1)
        if pay_conf.mode == 'free':
            return True
        if pay_conf.mode == 'subscription':
            if not user.subscription_end or user.subscription_end < msk_now():
                await _send_access_denied(
                    event,
                    "❌ Твой доступ к образовательной программе истёк или демо-период закончился. Оформи доступ к программе, чтобы продолжить.",
                    reply_markup=kb.payment_alert_keyboard(),
                )
                return False
        if pay_conf.mode == 'token':
            cost = pay_conf.cost_per_message
            if user.balance < cost:
                await _send_access_denied(
                    event,
                    f"❌ Недостаточно токенов. Стоимость сообщения: {cost}, твой баланс: {user.balance}. Пополни баланс, чтобы продолжить.",
                    reply_markup=kb.token_alert_keyboard(),
                )
                return False
    return True


async def touch_user_activity(user_id: int):
    async with async_session_maker() as session:
        from database import BotConfig
        user = await session.get(User, user_id)
        if not user:
            return
        bot_conf = await session.get(BotConfig, 1)
        user.last_activity_at = msk_now()
        user.main_reminder_count = 0
        if bot_conf and bot_conf.main_reminders_enabled:
            user.next_main_reminder_at = msk_now() + timedelta(hours=bot_conf.main_first_reminder_hours)
        else:
            user.next_main_reminder_at = None
        await session.commit()


async def activate_ai_mode(event, slug: str, state: FSMContext, bot: Bot, *, from_start_button: bool = False, dialogue_id: int | None = None):
    user_id = event.from_user.id
    chat_id = (
        event.message.chat.id
        if (hasattr(event, "message") and event.message and hasattr(event.message, "chat") and event.message.chat)
        else (event.chat.id if hasattr(event, "chat") and event.chat else user_id)
    )

    if not await check_paid_access(event, bot, topic_slug=slug):
        return

    # 1. Guard 1: AI Disclaimer gate
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        has_accepted = bool(user and user.ai_disclaimer_accepted_at and (user.ai_disclaimer_version or 0) >= 1)

    if not has_accepted:
        flow_type = "diagnostics" if slug == "diagnostics" else ("thematic" if slug in [
            "stress", "self_esteem", "relationships", "sleep_recovery", "motivation_goals", "emotional_intelligence"
        ] else "topic")
        await check_and_request_ai_disclaimer(
            bot,
            user_id,
            chat_id,
            flow_type=flow_type,
            payload={"topic_slug": slug},
        )
        return

    await touch_user_activity(user_id)

    # 2. Topic Dialogue Config & Dialogue ID allocation
    async with async_session_maker() as session:
        cfg = (await session.execute(
            select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == slug)
        )).scalar_one_or_none()
        reset_on_entry = cfg.reset_dialogue_on_entry if cfg else True

        # Atomic Start Button Validation & Ownership Linearization (P0-2):
        if from_start_button:
            if dialogue_id is None and hasattr(event, "data") and event.data:
                parts = event.data.split(":")
                if len(parts) >= 3 and parts[-1].isdigit():
                    dialogue_id = int(parts[-1])
            if dialogue_id is not None:
                stmt = (
                    update(UserTopicSession)
                    .where(
                        UserTopicSession.user_id == user_id,
                        UserTopicSession.topic_slug == slug,
                        UserTopicSession.active_dialogue_id == dialogue_id,
                    )
                    .values(updated_at=msk_now())
                    .returning(UserTopicSession.active_dialogue_id)
                )
                res = (await session.execute(stmt)).scalar_one_or_none()
                if res is None:
                    await session.rollback()
                    if hasattr(event, "answer"):
                        await event.answer("⚠️ Эта кнопка устарела. Начни диалог заново из меню темы.", show_alert=True)
                    elif hasattr(event, "message") and hasattr(event.message, "answer"):
                        await event.message.answer("⚠️ Эта кнопка устарела. Начни диалог заново из меню темы.")
                    return

        # P0-1: Check if user is currently in a live pre-test for THIS topic:
        current_tsess = await session.get(TestSession, user_id)
        if (
            current_tsess
            and not current_tsess.is_finished
            and current_tsess.invocation_topic_slug == slug
            and current_tsess.attempt_id
        ):
            active_test_set, active_count, active_ready = await get_test_set_readiness(
                session,
                current_tsess.test_set_id or 1,
                slug,
            )
            if not active_ready:
                await send_test_setup_unavailable(
                    bot,
                    user_id,
                    state,
                    active_count=active_count,
                    expected_count=expected_test_set_question_count(active_test_set, slug) if active_test_set else None,
                )
                return
            active_attempt = await session.get(TestAttempt, current_tsess.attempt_id)
            if active_attempt and active_attempt.purpose == "pre_dialogue" and active_attempt.status == "in_progress":
                claim = await get_pre_test_claim(
                    session, user_id, slug, current_tsess.invocation_dialogue_id, test_set_id=current_tsess.test_set_id
                )
                if claim and claim.status == "in_progress" and (claim.lease_until is None or claim.lease_until > msk_now()):
                    # User has a live progressing pre-test session. Restore FSM, resend question and return.
                    await state.set_state(UniversalTestStates.answering)
                    await _send_question(
                        bot,
                        chat_id,
                        current_tsess.current_question_index,
                        current_tsess.test_set_id,
                        state,
                    )
                    return

        if from_start_button:
            # Active dialogue was atomically validated and touched; do not alter or lower it
            pass
        elif dialogue_id is not None:
            await set_active_topic_dialogue_id(session, user_id, slug, dialogue_id, monotonic=False)
        elif reset_on_entry:
            dialogue_id = await allocate_next_dialogue_id(session, user_id, slug)
            await set_active_topic_dialogue_id(session, user_id, slug, dialogue_id)
        else:
            dialogue_id = await get_active_topic_dialogue_id(session, user_id, slug, default=0)
            if dialogue_id == 0:
                dialogue_id = await allocate_next_dialogue_id(session, user_id, slug)
                await set_active_topic_dialogue_id(session, user_id, slug, dialogue_id)

        user = await session.get(User, user_id)
        if user:
            user.current_topic_slug = slug
            if dialogue_id is not None and (not user.current_dialogue_id or dialogue_id > user.current_dialogue_id):
                user.current_dialogue_id = dialogue_id
        await session.commit()

    await state.clear()

    # 3. Pre-dialogue Test Gate (Guard 3) - MUST run before Start button
    if slug in {"diagnostics", "express"} and cfg and cfg.test_set_id:
        async with async_session_maker() as session:
            configured_set, active_count, configured_ready = await get_test_set_readiness(
                session,
                cfg.test_set_id,
                slug,
            )
        if not configured_ready:
            await send_test_setup_unavailable(
                bot,
                user_id,
                state,
                active_count=active_count,
                expected_count=expected_test_set_question_count(configured_set, slug) if configured_set else None,
            )
            return

    pre_test_enabled = bool(cfg and cfg.pre_dialogue_test_enabled)
    if pre_test_enabled:
        if not cfg.test_set_id:
            logger.warning("Topic %s has pre_dialogue_test_enabled=True but test_set_id is None", slug)
            await send_test_setup_unavailable(bot, user_id, state)
            return

        test_set_id = cfg.test_set_id
        async with async_session_maker() as session:
            claim = await get_pre_test_claim(session, user_id, slug, dialogue_id, test_set_id=test_set_id)
            is_completed = bool(claim and claim.status == "completed")

        if not is_completed:
            welcome_text = (cfg.welcome_text if cfg and cfg.welcome_text else None)
            media_id = cfg.welcome_media_id if cfg else None
            if welcome_text or media_id:
                media_rows = []
                if media_id:
                    async with async_session_maker() as session:
                        m = await session.get(ContentMedia, media_id)
                        if m:
                            media_rows.append(m)
                await send_smart_content(event, welcome_text or "", media_rows or None, None, bot)

            await start_universal_test(
                bot,
                user_id,
                state,
                test_set_id=test_set_id,
                dialogue_id=dialogue_id,
                topic_slug=slug,
                purpose="pre_dialogue",
            )
            return

    # 4. Start Dialogue Button Gate (Runs after pre-test is completed or not required)
    start_btn_enabled = cfg.start_dialogue_button_enabled if cfg else False
    if start_btn_enabled and not from_start_button:
        welcome_text = (cfg.welcome_text if cfg and cfg.welcome_text else None) or "Добро пожаловать в сессию."
        btn_text = (cfg.start_dialogue_button_text if cfg and cfg.start_dialogue_button_text else None) or "Начать диалог"
        keyboard = kb.topic_start_dialogue_keyboard(slug, dialogue_id, btn_text)
        media_id = cfg.welcome_media_id if cfg else None
        if media_id:
            async with async_session_maker() as session:
                media_row = await session.get(ContentMedia, media_id)
            if media_row:
                await send_smart_content(event, welcome_text, [media_row], keyboard, bot)
                return
        await send_smart_content(event, welcome_text, None, keyboard, bot)
        return

    # 5. Prerecorded First Message (Guard 2)
    prerecorded_text = cfg.prerecorded_first_message if (cfg and cfg.prerecorded_first_message) else None
    delivered_prerecorded = False
    if prerecorded_text:
        async with async_session_maker() as session:
            delivered_prerecorded = await deliver_prerecorded_first_message(
                bot,
                session,
                user_id,
                slug,
                dialogue_id,
                prerecorded_text,
            )

    # 6. Enter Chatting State
    await state.set_state(UserStates.chatting)
    await state.update_data(current_topic_slug=slug, dialogue_id=dialogue_id)

    if not delivered_prerecorded and not (start_btn_enabled and from_start_button and cfg and cfg.welcome_text):
        welcome_prompt = (cfg.welcome_text if cfg and cfg.welcome_text else None) or "🤖 Режим активирован. Напиши свой запрос."
        markup = await kb.ai_response_keyboard_for_topic(slug, dialogue_id)
        sent_message = await send_smart_content(event, welcome_prompt, None, markup, bot)
        await _remember_ai_response_message(state, sent_message)


@router.callback_query(F.data == "cb_accept_disclaimer")
async def cb_accept_disclaimer(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    now = msk_now()
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user:
            user.ai_disclaimer_accepted_at = now
            user.ai_disclaimer_version = 1
            user.ai_disclaimer_shown = True
        stmt = (
            sqlite_upsert(AiDisclaimerClaim)
            .values(
                user_id=user_id,
                status="delivered",
                claimed_at=now,
                delivered_at=now,
            )
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_={"status": "delivered", "delivered_at": now},
            )
        )
        await session.execute(stmt)
        await session.commit()

    await callback.answer("Согласие принято")
    try:
        await callback.message.delete()
    except Exception:
        pass

    pending = await consume_pending_ai_flow(user_id)
    if not pending:
        # Atomic consume: if already consumed by concurrent task, do NOT send main menu or duplicate resumption
        return

    flow_type = pending.flow_type
    payload = pending.payload or {}

    if flow_type in ("topic", "thematic"):
        slug = payload.get("topic_slug", "free_session")
        await activate_ai_mode(callback, slug, state, bot)
    elif flow_type == "diagnostics":
        await activate_ai_mode(callback, "diagnostics", state, bot)
    elif flow_type == "funnel_reflection":
        from funnel import resume_funnel_reflection_after_disclaimer
        await resume_funnel_reflection_after_disclaimer(
            bot=bot,
            user_id=user_id,
            chat_id=callback.message.chat.id,
            reflection_text=payload.get("reflection_text", ""),
            meditation_id=payload.get("meditation_id"),
        )
    elif flow_type == "funnel_strategy":
        from funnel import resume_funnel_strategy_after_disclaimer
        await resume_funnel_strategy_after_disclaimer(callback, state, bot, payload)
    elif flow_type == "funnel_objection":
        from funnel import resume_funnel_objection_after_disclaimer
        await resume_funnel_objection_after_disclaimer(callback, state, bot, payload)
    elif flow_type == "journal":
        from journal_handlers import show_journal_menu
        await show_journal_menu(callback, bot)
    elif flow_type == "journal_start":
        from journal_handlers import handle_journal_start
        await handle_journal_start(event=callback, bot=bot, state=state)
    else:
        await send_main_menu(callback, bot=bot)


@router.callback_query(F.data == "cb_cancel_disclaimer")
async def cb_cancel_disclaimer(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    await clear_pending_ai_flow(user_id)
    await state.clear()
    try:
        await callback.answer("Действие отменено")
    except Exception:
        pass
    await send_main_menu(callback, bot=bot)


@router.callback_query(F.data == "exit_ai_mode")
async def exit_ai_mode_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await touch_user_activity(callback.from_user.id)
    try:
        await callback.answer()
    except Exception:
        pass
    await send_main_menu(callback, bot=bot)


@router.callback_query(F.data.startswith("ai_start_dialogue:"))
async def cb_ai_start_dialogue(callback: CallbackQuery, state: FSMContext, bot: Bot):
    parts = callback.data.split(":")
    topic_slug = parts[1] if len(parts) > 1 else "free_session"
    btn_dialogue_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    await activate_ai_mode(callback, topic_slug, state, bot, from_start_button=True, dialogue_id=btn_dialogue_id)
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("ai_finish_dialogue:"))
async def cb_ai_finish_dialogue(callback: CallbackQuery, state: FSMContext, bot: Bot):
    parts = callback.data.split(":")
    topic_slug = parts[1] if len(parts) > 1 else None
    dialogue_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    await callback.answer()

    effective_slug = topic_slug or "free_session"
    effective_dialogue_id = dialogue_id or 1
    await state.update_data(
        ai_landing_return_callback=f"ai_session_completed:{effective_slug}:{effective_dialogue_id}",
        ai_landing_origin_stage="ai_session_completed",
        ai_last_response_message_id=None,
    )
    await state.set_state(None)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    text, markup = await _ai_session_completed_payload(effective_slug, effective_dialogue_id)
    await callback.message.answer(text, reply_markup=markup)


async def _ai_session_completed_payload(topic_slug: str, dialogue_id: int):
    async with async_session_maker() as session:
        cfg = (await session.execute(
            select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == topic_slug)
        )).scalar_one_or_none()
        summary_enabled = bool(cfg and cfg.summary_generation_enabled)

    completed_content = await get_content("session_completed")
    text = completed_content.text_content if completed_content and completed_content.text_content else (
        "Сессия завершена. Выберите следующий шаг."
    )
    if summary_enabled:
        text += "\n\nМожно сформировать итог сессии или продолжить диалог."
    markup = await kb.configured_topic_finish_actions_keyboard(topic_slug, dialogue_id, has_summary=summary_enabled)
    return text, markup


@router.callback_query(F.data.startswith("ai_session_completed:"))
async def cb_ai_session_completed(callback: CallbackQuery, state: FSMContext, bot: Bot):
    parts = callback.data.split(":")
    topic_slug = parts[1] if len(parts) > 1 and parts[1] else "free_session"
    dialogue_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    await callback.answer()
    text, markup = await _ai_session_completed_payload(topic_slug, dialogue_id)
    await send_smart_content(callback, text, None, markup, bot)


@router.callback_query(F.data.startswith("ai_continue_dialogue:"))
async def cb_ai_continue_dialogue(callback: CallbackQuery, state: FSMContext, bot: Bot):
    parts = callback.data.split(":")
    topic_slug = parts[1] if len(parts) > 1 else "free_session"
    dialogue_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    user_id = callback.from_user.id

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user:
            user.current_topic_slug = topic_slug
            if not user.current_dialogue_id or dialogue_id > user.current_dialogue_id:
                user.current_dialogue_id = dialogue_id
            await session.commit()
        await set_active_topic_dialogue_id(session, user_id, topic_slug, dialogue_id, monotonic=False)

    await state.set_state(UserStates.chatting)
    await state.update_data(current_topic_slug=topic_slug, dialogue_id=dialogue_id)
    try:
        state_data = await state.get_data()
    except Exception:
        state_data = None
    if isinstance(state_data, dict) and (
        "ai_landing_return_callback" in state_data or "ai_landing_origin_stage" in state_data
    ):
        await state.update_data(ai_landing_return_callback=None, ai_landing_origin_stage=None)
    await callback.answer("Продолжаем диалог")
    markup = await kb.ai_response_keyboard_for_topic(topic_slug, dialogue_id)
    sent_message = await callback.message.answer(
        "Диалог продолжается. Напишите свой запрос или мысль:",
        reply_markup=markup,
    )
    await _remember_ai_response_message(state, sent_message)


@router.callback_query(F.data.startswith("ai_new_dialogue:"))
async def cb_ai_new_dialogue(callback: CallbackQuery, state: FSMContext, bot: Bot):
    parts = callback.data.split(":")
    topic_slug = parts[1] if len(parts) > 1 else "free_session"
    user_id = callback.from_user.id
    async with async_session_maker() as session:
        new_dialogue_id = await allocate_next_dialogue_id(session, user_id, topic_slug)
        await set_active_topic_dialogue_id(
            session,
            user_id,
            topic_slug,
            new_dialogue_id,
            monotonic=False,
        )
        await session.commit()
    await callback.answer("Начинаем новый разговор")
    await activate_ai_mode(
        callback,
        topic_slug,
        state,
        bot,
        from_start_button=True,
        dialogue_id=new_dialogue_id,
    )


@router.callback_query(F.data.startswith("ai_gen_summary:"))
async def cb_ai_gen_summary(callback: CallbackQuery, state: FSMContext, bot: Bot):
    parts = callback.data.split(":")
    topic_slug = parts[1] if len(parts) > 1 else "free_session"
    dialogue_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    user_id = callback.from_user.id
    await callback.answer("Формирую итог сессии...")
    async with ai_typing_action(bot, user_id):
        waiting = await callback.message.answer("⏳ Анализирую диалог и формирую итог сессии…")
        try:
            summary = await generate_session_summary(user_id, topic_slug, dialogue_id, version=1)
            if summary and (getattr(summary, 'is_generating', False) or summary.summary_text == "already_generating"):
                summary_text = "⏳ Итог сессии уже формируется. Пожалуйста, подождите немного и повторите попытку."
            elif summary:
                summary_text = summary.summary_text
            else:
                summary_text = "Не удалось сформировать итог."
        except Exception:
            logger.exception("Failed to generate session summary")
            summary_text = "Произошла ошибка при формировании итога сессии."
        finally:
            try:
                await waiting.delete()
            except Exception:
                pass

        markup = await kb.configured_topic_summary_result_keyboard(topic_slug, dialogue_id)
        safe_summary = html.escape(summary_text)
        await callback.message.answer(f"📝 <b>Итог сессии:</b>\n\n{safe_summary}", reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("ai_regen_summary:"))
async def cb_ai_regen_summary(callback: CallbackQuery, state: FSMContext, bot: Bot):
    parts = callback.data.split(":")
    topic_slug = parts[1] if len(parts) > 1 else "free_session"
    dialogue_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    user_id = callback.from_user.id
    await callback.answer("Перегенерирую итог...")
    async with ai_typing_action(bot, user_id):
        waiting = await callback.message.answer("⏳ Формирую обновлённый итог сессии…")

        try:
            summary = await generate_session_summary(user_id, topic_slug, dialogue_id, is_regeneration=True)
            if summary and (getattr(summary, 'is_generating', False) or summary.summary_text == "already_generating"):
                summary_text = "⏳ Итог сессии уже формируется. Пожалуйста, подождите немного и повторите попытку."
                ver_text = ""
            elif summary:
                summary_text = summary.summary_text
                ver_text = f" (версия {getattr(summary, 'version', 2)})"
            else:
                summary_text = "Не удалось сформировать итог."
                ver_text = ""
        except Exception:
            logger.exception("Failed to regenerate session summary")
            summary_text = "Произошла ошибка при повторном формировании итога."
            ver_text = ""
        finally:
            try:
                await waiting.delete()
            except Exception:
                pass

        markup = await kb.configured_topic_summary_result_keyboard(topic_slug, dialogue_id)
        safe_summary = html.escape(summary_text)
        await callback.message.answer(
            f"📝 <b>Обновлённый итог сессии{ver_text}:</b>\n\n{safe_summary}",
            reply_markup=markup,
            parse_mode="HTML",
        )


@router.callback_query(F.data == "main_menu_action")
async def cb_main_menu_action(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await touch_user_activity(callback.from_user.id)
    try:
        await callback.answer()
    except Exception:
        pass
    await send_main_menu(callback, bot=bot)


@router.callback_query(F.data == "menu_thematic_sessions")
async def menu_thematic_sessions_callback(callback: CallbackQuery, bot: Bot):
    if not await is_menu_section_enabled("thematic"):
        await send_smart_content(callback, "Этот раздел сейчас недоступен.", None, await kb.main_menu_keyboard(), bot)
        return
    content = await get_content("thematic_sessions_intro")
    text_val = content.text_content if content else "🧠 <b>Тематические сессии</b>\n\nВыберите тему для работы с AI:"
    await send_smart_content(callback, text_val, content.media if content else None, await kb.configured_thematic_sessions_keyboard(), bot)
    await callback.answer()


@router.callback_query(F.data.startswith("thematic_"))
async def thematic_session_choice(callback: CallbackQuery, state: FSMContext, bot: Bot):
    topic_slug = callback.data.replace("thematic_", "", 1)
    await activate_ai_mode(callback, topic_slug, state, bot)
    await callback.answer()


@router.callback_query(F.data == "mode_free_session")
async def mode_free_session(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await activate_ai_mode(callback, "free_session", state, bot)
    await callback.answer()


@router.callback_query(F.data == "mode_diagnostics")
async def mode_diagnostics(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await activate_ai_mode(callback, "diagnostics", state, bot)
    await callback.answer()


@router.callback_query(F.data == "mode_express")
async def mode_express(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await activate_ai_mode(callback, "express", state, bot)
    await callback.answer()


@router.callback_query(F.data == "mode_habits")
async def mode_habits(callback: CallbackQuery, bot: Bot, state: FSMContext | None = None):
    if state is not None and await state.get_state() in {
        HabitStates.waiting_for_name.state,
        HabitStates.waiting_for_time.state,
        HabitStates.waiting_for_edit_time.state,
    }:
        await state.clear()
    async with async_session_maker() as session:
        conf = await session.get(HabitConfig, 1)
        mode = conf.mode

        content_obj = await session.get(Content, "habits_text", options=[selectinload(Content.media)])
        intro_text = content_obj.text_content if content_obj else "Раздел формирования привычек."
        media = content_obj.media if content_obj else None

    if mode == 'catalog':
        intro_text += "\n\n📚 <b>Режим библиотеки:</b> Выбери полезную привычку из списка ниже, чтобы начать."
    elif mode == 'custom':
        intro_text += "\n\n🛠 <b>Конструктор:</b> Создай свою собственную привычку и поставь таймер."
    elif mode == 'hybrid':
        intro_text += "\n\n🚀 <b>Трекер привычек:</b> Выбери готовую или создай свою."

    await send_smart_content(
        callback,
        intro_text,
        media,
        await kb.configured_habits_entry_keyboard(mode),
        bot,
    )
    await callback.answer()


# --- Заглушки для кнопок (чтобы бот не вис при нажатии) ---

@router.callback_query(F.data == "habit_open_catalog")
async def habit_open_catalog(callback: CallbackQuery):
    # Тут будет логика показа списка шаблонов из БД
    async with async_session_maker() as session:
        templates = await session.execute(select(Habit).where(Habit.user_id == None))
        templates = templates.scalars().all()

    text = "📚 <b>Каталог привычек:</b>\n"
    builder = InlineKeyboardBuilder()
    for t in templates:
        builder.button(text=f"🔹 {t.name}", callback_data=f"habit_add_tpl_{t.id}")

    builder.button(text="⬅️ Назад", callback_data="mode_habits")
    builder.adjust(1)

    await send_smart_content(callback, text or "Каталог пуст", None, builder.as_markup())


# --- HABITS LOGIC ---

@router.callback_query(F.data == "habit_create_new")
async def habit_create_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(HabitStates.waiting_for_name)

    # Кнопка отмены
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="mode_habits")

    await callback.message.answer(
        "📝 <b>Новая привычка</b>\n\n"
        "Напиши название привычки (например: <i>Читать 20 минут</i>):\n",
        reply_markup=builder.as_markup(),
        parse_mode='HTML'
    )
    await callback.answer()


@router.callback_query(F.data == "habit_ask_manual_time")
async def habit_ask_manual_time(callback: CallbackQuery):
    await callback.answer("⌨️ Просто напиши время в чат!\nНапример: 07:30 или 22:15", show_alert=True)


@router.message(HabitStates.waiting_for_name, ~F.text.startswith('/'))
async def habit_save_name(message: Message, state: FSMContext):
    await clean_chat_after_input(message, state)
    name = message.text.strip()
    await state.update_data(habit_name=name)

    await state.set_state(HabitStates.waiting_for_time)

    builder = InlineKeyboardBuilder()
    times = ["07:00", "08:00", "09:00", "10:00", "20:00", "21:00"]
    for t in times:
        builder.button(text=t, callback_data=f"set_time_{t}")

    builder.button(text="✍️ Своё время", callback_data="habit_ask_manual_time")
    builder.button(text="❌ Отмена", callback_data="mode_habits")
    builder.adjust(3, 2)

    msg = await message.answer(
        f"🕒 <b>Время напоминания для «{html.escape(name)}»</b>\n\n"
        f"Выбери вариант из списка или <b>напиши время в чат</b> (формат ЧЧ:ММ).\n\n"
        f"⚠️ <i>Время указывается по часовому поясу из раздела «Привычки».</i>",
        reply_markup=builder.as_markup(),
        parse_mode='HTML'
    )
    await state.update_data(last_msg_id=msg.message_id)


@router.callback_query(F.data.startswith("set_time_"))
async def habit_time_click(callback: CallbackQuery, state: FSMContext, bot: Bot):
    time_str = callback.data.split("_")[-1]
    await habit_finalize_creation(callback.message, state, time_str, callback.from_user.id, bot)
    await callback.answer()


@router.message(HabitStates.waiting_for_time, ~F.text.startswith('/'))
async def habit_time_manual(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    time_str = normalize_habit_time(message.text)

    if not time_str:
        msg = await message.answer(
            "❌ Неверный формат. Пожалуйста, введи время как <b>ЧЧ:ММ</b> (например 09:00).",
            parse_mode="HTML",
        )
        asyncio.create_task(send_temp_notification(bot, message.chat.id, "❌ Ошибка формата времени"))
        return

    await habit_finalize_creation(message, state, time_str, message.from_user.id, bot)


async def habit_finalize_creation(message_obj, state, time_str, user_id, bot):
    data = await state.get_data()
    name = data.get('habit_name', 'Новая привычка')
    time_str = normalize_habit_time(time_str)
    if not time_str:
        return

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        timezone_value = user.habit_timezone if user else None
        new_habit = Habit(
            user_id=user_id,
            name=name,
            time_target=time_str,
            is_active=True,
            next_reminder_at=utc_naive(
                calculate_next_habit_reminder(time_str, timezone_value)
            ),
        )
        session.add(new_habit)
        await session.commit()

    await state.clear()

    # Возвращаемся в меню привычек
    # Имитируем нажатие кнопки меню
    fake_cb = message_obj
    # Если это Message, нам нужен объект похожий на Callback для переиспользования функции
    # Но проще просто отправить меню

    content = await get_content("habits_text")
    text = content.text_content if content else "Привычка создана!"

    # Отправляем подтверждение и возвращаем меню
    await bot.send_message(
        message_obj.chat.id,
        f"✅ Привычка <b>«{html.escape(name)}»</b> установлена на <b>{html.escape(time_str)}</b> "
        f"(часовой пояс: <b>{html.escape(habit_timezone_name(timezone_value))}</b>).",
        parse_mode='HTML',
    )

    # Чтобы вернуть меню, нам нужен callback, создадим фейковый объект или просто вызовем клавиатуру
    # Проще вызвать клавиатуру заново
    from handlers import mode_habits  # self import trick
    # Но так нельзя. Поэтому просто отправляем сообщение с кнопкой назад

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Вернуться в раздел привычек", callback_data="mode_habits")
    await bot.send_message(message_obj.chat.id, "Что делаем дальше?", reply_markup=builder.as_markup())


# --- КАТАЛОГ ---

@router.callback_query(F.data == "habit_open_catalog")
async def habit_open_catalog(callback: CallbackQuery):
    async with async_session_maker() as session:
        templates = await session.execute(select(Habit).where(Habit.user_id == None))
        templates = templates.scalars().all()

    text = "📚 <b>Каталог идей:</b>\nВыбери привычку, чтобы добавить её себе:"
    builder = InlineKeyboardBuilder()

    if not templates:
        text = "Каталог пока пуст. Попроси админа добавить шаблоны."

    for t in templates:
        builder.button(text=f"➕ {t.name}", callback_data=f"habit_add_tpl_{t.id}")

    builder.button(text="⬅️ Назад", callback_data="mode_habits")
    builder.adjust(1)

    await send_smart_content(callback, text, None, builder.as_markup())


@router.callback_query(F.data.startswith("habit_add_tpl_"))
async def habit_add_template_start(callback: CallbackQuery, state: FSMContext):
    tpl_id = int(callback.data.split("_")[-1])

    async with async_session_maker() as session:
        tpl = await session.get(Habit, tpl_id)
        if not tpl:
            await callback.answer("Ошибка: шаблон не найден")
            return
        habit_name = tpl.name

    await state.set_state(HabitStates.waiting_for_time)
    await state.update_data(habit_name=habit_name)

    builder = InlineKeyboardBuilder()
    times = ["07:00", "08:00", "09:00", "10:00", "20:00", "21:00"]
    for t in times:
        builder.button(text=t, callback_data=f"set_time_{t}")

    builder.button(text="✍️ Своё время", callback_data="habit_ask_manual_time")
    builder.button(text="❌ Отмена", callback_data="mode_habits")
    builder.adjust(3, 2)

    msg = await callback.message.answer(
        f"🕒 <b>Настройка: «{html.escape(habit_name)}»</b>\n\n"
        f"Выбери время напоминания или <b>напиши своё в чат</b> (формат ЧЧ:ММ).\n\n"
        f"⚠️ <i>Время указывается по часовому поясу из раздела «Привычки».</i>",
        reply_markup=builder.as_markup(),
        parse_mode='HTML'
    )
    await state.update_data(last_msg_id=msg.message_id)
    await callback.answer()


async def _reschedule_user_habits(session, user, now: datetime | None = None):
    now_utc = as_utc(now)
    habits = await session.execute(
        select(Habit).where(Habit.user_id == user.id, Habit.is_active == True)
    )
    for habit in habits.scalars().all():
        try:
            if habit.time_target:
                habit.next_reminder_at = utc_naive(
                    calculate_next_habit_reminder(
                        habit.time_target,
                        user.habit_timezone,
                        now=now_utc,
                        last_reminder_at=habit.last_reminder_at,
                    )
                )
        except Exception:
            logger.exception(
                "Habit timezone reschedule failed habit_id=%s user_id=%s",
                habit.id,
                user.id,
            )


@router.callback_query(F.data == "habit_timezone")
async def habit_timezone_menu(callback: CallbackQuery):
    async with async_session_maker() as session:
        user = await session.get(User, callback.from_user.id)
        timezone_value = user.habit_timezone if user else None

    current_timezone = habit_timezone_name(timezone_value)
    text = (
        "🌍 <b>Часовой пояс привычек</b>\n\n"
        f"Сейчас: <b>{html.escape(format_habit_timezone(timezone_value))}</b>\n\n"
        "Выбери город. Напоминания и день выполнения будут считаться по этому поясу."
    )
    builder = InlineKeyboardBuilder()
    for timezone_value, label in HABIT_TIMEZONE_OPTIONS:
        prefix = "✅ " if timezone_value == current_timezone else ""
        builder.button(text=f"{prefix}{label} ({timezone_value})", callback_data=f"habit_set_timezone_{timezone_value}")
    builder.button(text="⬅️ Назад", callback_data="mode_habits")
    builder.adjust(1)

    await send_smart_content(callback, text, None, builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("habit_set_timezone_"))
async def habit_set_timezone(callback: CallbackQuery):
    timezone_value = normalize_habit_timezone(callback.data.removeprefix("habit_set_timezone_"))
    if not timezone_value:
        await callback.answer("Неизвестный часовой пояс", show_alert=True)
        return

    async with async_session_maker() as session:
        user = await session.get(User, callback.from_user.id)
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        user.habit_timezone = timezone_value
        await _reschedule_user_habits(session, user)
        await session.commit()

    await habit_timezone_menu(callback)


async def _set_habit_time(
    user_id: int,
    habit_id: int,
    time_target: str,
    now: datetime | None = None,
) -> str | None:
    time_target = normalize_habit_time(time_target)
    if not time_target:
        return None

    async with async_session_maker() as session:
        habit = await session.get(Habit, habit_id)
        if not habit or habit.user_id != user_id:
            return None
        user = await session.get(User, user_id)
        habit.time_target = time_target
        habit.next_reminder_at = utc_naive(
            calculate_next_habit_reminder(
                time_target,
                user.habit_timezone if user else None,
                now=now,
                last_reminder_at=habit.last_reminder_at,
            )
        )
        await session.commit()
        return habit.name


@router.callback_query(F.data.startswith("habit_edit_time_"))
async def habit_edit_time_start(callback: CallbackQuery, state: FSMContext):
    try:
        habit_id = int(callback.data.removeprefix("habit_edit_time_"))
    except ValueError:
        await callback.answer("Привычка не найдена", show_alert=True)
        return

    async with async_session_maker() as session:
        habit = await session.get(Habit, habit_id)
        if not habit or habit.user_id != callback.from_user.id:
            await callback.answer("Привычка не найдена", show_alert=True)
            return

    await state.set_state(HabitStates.waiting_for_edit_time)
    await state.update_data(habit_id=habit_id)
    builder = InlineKeyboardBuilder()
    for target_time in ["07:00", "08:00", "09:00", "10:00", "20:00", "21:00"]:
        builder.button(text=target_time, callback_data=f"habit_edit_set_time_{habit_id}_{target_time}")
    builder.button(text="✍️ Своё время", callback_data="habit_edit_manual_time")
    builder.button(text="❌ Отмена", callback_data="habit_edit_cancel")
    builder.adjust(3, 2)
    await callback.message.answer(
        f"🕒 <b>Новое время для «{html.escape(habit.name)}»</b>\n\n"
        "Выбери время или напиши его в чат (формат ЧЧ:ММ).",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "habit_edit_manual_time")
async def habit_edit_manual_time(callback: CallbackQuery):
    await callback.answer("⌨️ Напиши время в чат, например 07:30", show_alert=True)


@router.callback_query(F.data == "habit_edit_cancel")
async def habit_edit_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await habit_my_list(callback, bot)
    await callback.answer()


@router.callback_query(F.data.startswith("habit_edit_set_time_"))
async def habit_edit_time_click(callback: CallbackQuery, state: FSMContext, bot: Bot):
    payload = callback.data.removeprefix("habit_edit_set_time_")
    try:
        habit_id, time_target = payload.split("_", 1)
        habit_id = int(habit_id)
    except ValueError:
        await callback.answer("Неверное время", show_alert=True)
        return

    name = await _set_habit_time(callback.from_user.id, habit_id, time_target)
    if name is None:
        await callback.answer("Привычка не найдена", show_alert=True)
        return
    await state.clear()
    await callback.answer("✅ Время изменено")
    await habit_my_list(callback, bot)


@router.message(HabitStates.waiting_for_edit_time, ~F.text.startswith("/"))
async def habit_edit_time_manual_input(message: Message, state: FSMContext):
    await clean_chat_after_input(message, state)
    time_target = normalize_habit_time(message.text)
    if not time_target:
        await message.answer(
            "❌ Неверный формат. Введи время как <b>ЧЧ:ММ</b>, например 09:00.",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    name = await _set_habit_time(message.from_user.id, data.get("habit_id"), time_target)
    if name is None:
        await message.answer("❌ Привычка не найдена.")
        await state.clear()
        return
    await state.clear()
    await message.answer(
        f"✅ Для привычки <b>«{html.escape(name)}»</b> установлено время <b>{time_target}</b>.",
        parse_mode="HTML",
    )


# --- МОИ ПРИВЫЧКИ (СПИСОК) ---

@router.callback_query(F.data == "habit_my_list")
async def habit_my_list(callback: CallbackQuery, bot: Bot):
    async with async_session_maker() as session:
        user = await session.get(User, callback.from_user.id)
        habits = await session.execute(
            select(Habit).where(Habit.user_id == callback.from_user.id).order_by(Habit.time_target)
        )
        habits = habits.scalars().all()

    if not habits:
        await mode_habits(callback, bot)
        return

    timezone_text = format_habit_timezone(user.habit_timezone if user else None)
    text = (
        "📋 <b>Твои привычки:</b>\n\n"
        f"🌍 Часовой пояс: <b>{html.escape(timezone_text)}</b>\n\n"
        "Нажми «изменить», чтобы перенести время, или на корзину, чтобы удалить привычку."
    )
    builder = InlineKeyboardBuilder()

    for h in habits:
        builder.button(text=f"🕒 Изменить {h.time_target} — {h.name}", callback_data=f"habit_edit_time_{h.id}")
        builder.button(text=f"🗑 {h.time_target} — {h.name}", callback_data=f"habit_delete_{h.id}")

    builder.button(text="🌍 Изменить часовой пояс", callback_data="habit_timezone")
    builder.button(text="⬅️ Назад", callback_data="mode_habits")
    builder.adjust(1)

    await send_smart_content(callback, text, None, builder.as_markup(), bot)


@router.callback_query(F.data.startswith("habit_delete_"))
async def habit_delete(callback: CallbackQuery, bot: Bot):
    h_id = int(callback.data.split("_")[-1])

    async with async_session_maker() as session:
        habit = await session.get(Habit, h_id)
        if habit and habit.user_id == callback.from_user.id:
            await session.delete(habit)
            await session.commit()
            await callback.answer("✅ Привычка удалена")
        else:
            await callback.answer("❌ Ошибка или привычка уже удалена")

    await habit_my_list(callback, bot)


@router.callback_query(F.data.startswith("habit_done_track"))
async def habit_track_done(callback: CallbackQuery):
    # Пытаемся достать ID привычки из callback (если мы его туда положили, если нет - надо искать по контексту)
    # В background_worker кнопка просто "habit_done_track". Это проблема, так как мы не знаем КАКУЮ привычку он выполнил.
    # НАДО ИСПРАВИТЬ background_worker.py, чтобы он слал callback="habit_done_track_{habit_id}"

    # ПОДРАЗУМЕВАЕМ, ЧТО В background_worker.py ВЫ ИСПРАВИТЕ button callback на f"habit_done_track_{h.id}"
    # Если callback старый (просто текст), то код сломается.

    parts = callback.data.split("_")
    if len(parts) < 4:
        # Старая кнопка без ID
        await callback.answer("Ошибка версии. Пересоздай привычку.", show_alert=True)
        return

    habit_id = int(parts[3])

    async with async_session_maker() as session:
        habit = await session.get(Habit, habit_id)
        config = await session.get(HabitConfig, 1)
        user = await session.get(User, callback.from_user.id)

        if not habit or habit.user_id != callback.from_user.id:
            await callback.answer("Привычка не найдена")
            return

        now = habit_local_now(user.habit_timezone if user else None)

        # 1. ПРОВЕРКА ВРЕМЕНИ (ОКНО)
        # Парсим цель "09:00"
        try:
            h_h, h_m = map(int, habit.time_target.split(":"))
            target_time = now.replace(hour=h_h, minute=h_m, second=0, microsecond=0)

            # Если сейчас 00:30, а цель 23:00, значит цель была вчера. Сложно.
            # Упрощаем: если разница между "сейчас" и "цель сегодня" больше окна, и мы ПОСЛЕ цели - опоздал.

            diff = (now - target_time).total_seconds() / 60  # разница в минутах

            # Если нажал ДО времени (заранее) - ок? Пусть будет ок.
            # Если нажал ПОЗЖЕ чем окно - сброс.

            # Однако background worker шлет сообщение именно в target_time.
            # Значит пользователь видит его и жмет.

            # Если diff > config.time_window_minutes: ОПОЗДАЛ.
            # Но надо проверить, не нажал ли он кнопку "вчерашнюю" сегодня.
            # Проверяем дату last_completed_date

            today_date = now.date()
            if habit.last_completed_date == today_date:
                await callback.answer("✅ Ты уже отмечал(а) выполнение сегодня!", show_alert=True)
                return

            is_late = False
            if diff > config.time_window_minutes:
                # Опоздал
                is_late = True

            # Если опоздал или пропустил день (если last_date < yesterday) -> Сброс
            reset_needed = False
            if is_late:
                reset_needed = True
            elif habit.last_completed_date:
                # Если последний раз делал позавчера (разница > 1 дня)
                if (today_date - habit.last_completed_date).days > 1:
                    reset_needed = True

            rewards_list = [x.strip() for x in config.reward_sequence.split(",")]
            max_days = config.streak_days

            message_text = ""

            if reset_needed:
                habit.current_streak = 1  # Начинаем заново с 1 (так как текущее действие засчитываем)
                safe_reward = html.escape(rewards_list[0]) if rewards_list else "⭐"
                message_text = f"😔 <b>Серия прервана!</b> (Ты не успел(а) вовремя или пропустил(а) день).\nНачинаем сначала!\n\nНаграда: <b>{safe_reward}</b>"
            else:
                habit.current_streak += 1
                # Защита от выхода за пределы массива
                idx = habit.current_streak - 1
                if idx < len(rewards_list):
                    char = rewards_list[idx]
                else:
                    char = "⭐"
                safe_char = html.escape(char)
                message_text = f"✅ <b>Принято!</b>\nТвоя награда: <b>{safe_char}</b>\nСерия: {habit.current_streak} дн."

            habit.last_completed_date = today_date

            # ПРОВЕРКА ПОБЕДЫ
            if habit.current_streak >= max_days:
                # ПОБЕДА!
                full_word = "".join(rewards_list[:max_days])
                safe_full_word = html.escape(full_word)
                final_msg = html.escape(config.final_message) if config.final_message else ""

                await callback.message.edit_text(
                    f"{callback.message.html_text}\n\n"
                    f"🎉 <b>ПОБЕДА! ЦИКЛ ЗАВЕРШЕН!</b>\n"
                    f"Собрано: <b>{safe_full_word}</b>\n\n"
                    f"{final_msg}",
                    parse_mode='HTML'
                )

                # Сброс после победы? Или оставляем?
                # "После победного должны отправиться все награды... И так до победного".
                # Обычно после победы серия обнуляется для нового круга.
                habit.current_streak = 0

            else:
                # Просто обновление сообщения
                await callback.message.edit_text(
                    f"{callback.message.html_text}\n\n{message_text}",
                    parse_mode='HTML'
                )

            await session.commit()

        except Exception as e:
            logging.error(f"Habit done error: {e}")
            await callback.answer("Ошибка обработки", show_alert=True)


# --- Diagnostics Logic ---

@router.message(UserStates.diagnostics_process, ~F.text.startswith('/'))
async def process_diagnostics(message: Message, state: FSMContext, bot: Bot):
    if message.text == "❌ Выход":
        await state.clear()
        await send_main_menu(message, bot=bot)
        return

    data = await state.get_data()
    questions = data['questions']
    current_q = data['current_q']
    answers = data['answers']

    # Сохраняем ответ пользователя
    answers.append(f"В: {questions[current_q]}\nО: {message.text}")

    next_q = current_q + 1
    if next_q < len(questions):
        await state.update_data(current_q=next_q, answers=answers)
        await message.answer(questions[next_q], reply_markup=await kb.ai_exit_keyboard_for_topic())
    else:
        # Диагностика завершена
        full_context = "\n".join(answers)

        # 1. Сохраняем ВЕСЬ контекст диагностики как одно большое сообщение пользователя в историю
        async with async_session_maker() as session:
            user = await session.get(User, message.from_user.id)
            diag_did = await get_active_topic_dialogue_id(session, message.from_user.id, "diagnostics", default=(user.current_dialogue_id if user else 1) or 1)

            db_msg = DBMessage(
                user_id=message.from_user.id,
                role='user',
                content=f"РЕЗУЛЬТАТЫ ДИАГНОСТИКИ:\n{full_context}\n\nПроанализируй это и дай рекомендации.",
                topic_slug="diagnostics",
                dialogue_id=diag_did,
            )
            session.add(db_msg)
            await session.commit()

        await state.set_state(UserStates.chatting)
        await message.answer(
            "✅ Данные собраны. Анализирую...",
            reply_markup=await kb.ai_exit_keyboard_for_topic(),
        )
        await ensure_ai_disclaimer_sent(bot, message.from_user.id, message.chat.id)

        # 2. Генерируем ответ (теперь AI увидит контекст из базы, который мы только что сохранили)
        response = await get_ai_response(
            message.from_user.id,
            "Проведи детальный разбор диагностики и дай рекомендации.",  # Промпт для генерации
            message.from_user.first_name,
            bot=bot,
            chat_id=message.chat.id,
        )

        # 3. Сохраняем ответ AI
        async with async_session_maker() as session:
            user = await session.get(User, message.from_user.id)
            diag_did = await get_active_topic_dialogue_id(session, message.from_user.id, "diagnostics", default=(user.current_dialogue_id if user else 1) or 1)
            db_ai_msg = DBMessage(
                user_id=message.from_user.id,
                role='assistant',
                content=response,
                topic_slug="diagnostics",
                dialogue_id=diag_did,
            )
            session.add(db_ai_msg)
            await session.commit()

        # 4. Отправляем
        await send_ai_response(
            message.chat.id,
            response,
            bot,
            state,
            await kb.ai_exit_keyboard_for_topic(),
        )


async def _save_media_ai_exchange(user_id: int, user_content: str, response_text: str) -> None:
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user:
            return
        slug = user.current_topic_slug or "free_session"
        did = await get_active_topic_dialogue_id(session, user_id, slug, default=(user.current_dialogue_id or 1))
        session.add_all([
            DBMessage(user_id=user_id, dialogue_id=did, role='user', content=user_content, topic_slug=slug),
            DBMessage(user_id=user_id, dialogue_id=did, role='assistant', content=response_text, topic_slug=slug),
        ])
        await session.commit()


@router.message(UserStates.chatting, F.photo, F.caption.startswith('/edit'))
async def process_image_edit(message: Message, bot: Bot):
    prompt = (message.caption or "")[5:].strip()
    if not prompt:
        await message.answer("Добавьте задачу после /edit в подписи к фотографии.")
        return
    waiting = await message.answer("Редактирую изображение…")
    try:
        file = await bot.get_file(message.photo[-1].file_id)
        source = await bot.download_file(file.file_path)
        result = await edit_image(source.read(), prompt)
        await message.answer_photo(BufferedInputFile(result, filename="kie_edit.png"))
    except Exception:
        logger.exception("KIE image edit failed")
        await message.answer("Не удалось отредактировать изображение. Проверьте настройки KIE.")
    finally:
        try:
            await waiting.delete()
        except Exception:
            pass


@router.message(UserStates.chatting, F.photo)
async def process_chat_photo(message: Message, bot: Bot, state: FSMContext | None = None):
    user_id = message.from_user.id
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user or not user.ai_disclaimer_accepted_at or (user.ai_disclaimer_version or 0) < 1:
            await check_and_request_ai_disclaimer(
                bot,
                user_id,
                message.chat.id,
                flow_type="topic",
                payload={"topic_slug": user.current_topic_slug if user else "free_session"},
            )
            return

        if user.is_blocked:
            await message.answer("⛔️ Ты заблокирован(а) администратором.")
            return

        pay_conf = await session.get(PaymentConfig, 1)
        if not user.is_admin and pay_conf.mode != 'free':
            if pay_conf.mode == 'subscription':
                if not user.subscription_end or user.subscription_end < msk_now():
                    await message.answer(
                        "❌ Твой доступ к образовательной программе истёк. Оформи доступ к программе, чтобы продолжить.",
                        reply_markup=kb.payment_alert_keyboard(),
                    )
                    return
            elif pay_conf.mode == 'token':
                cost = pay_conf.cost_per_message
                if user.balance < cost:
                    await message.answer(
                        f"❌ Недостаточно токенов. Стоимость сообщения: {cost}, твой баланс: {user.balance}",
                        reply_markup=kb.token_alert_keyboard(),
                    )
                    return
                user.balance -= cost
                await session.commit()

        # Resolve immutable snapshot ONCE before any network awaits
        current_slug = user.current_topic_slug or "free_session"
        current_did = await get_active_topic_dialogue_id(
            session, user_id, current_slug, default=user.current_dialogue_id or 1
        )
        snapshot = TopicContextSnapshot(
            user_id=user_id,
            topic_slug=current_slug,
            dialogue_id=current_did,
        )
        user_name = (user.name or user.first_name or "пользователь").strip()

    async with ai_typing_action(bot, message.chat.id):
        waiting = await message.answer("Анализирую фотографию…")
        try:
            file = await bot.get_file(message.photo[-1].file_id)
            image = await bot.download_file(file.file_path)
            caption = (message.caption or "").strip()

            # Stage 1: Vision extraction -> textual factual image description
            prompt = caption or "Опиши подробно, что изображено на фото."
            image_description = await analyze_image(image.read(), prompt)

            photo_text = f"[Фото: {image_description}]" if not caption else f"[Фото: {image_description}]\nПодпись: {caption}"

            # Persist user photo representation before main dialogue generation using snapshot
            async with async_session_maker() as session:
                user_msg = DBMessage(
                    user_id=snapshot.user_id,
                    dialogue_id=snapshot.dialogue_id,
                    role='user',
                    content=photo_text,
                    topic_slug=snapshot.topic_slug,
                )
                session.add(user_msg)
                await session.flush()
                current_msg_id = user_msg.id
                await session.commit()

            # Stage 2: Standard Topic Dialogue LLM pipeline using the immutable snapshot
            response_text = await get_ai_response_for_topic(
                snapshot.user_id,
                photo_text,
                user_name,
                current_message_id=current_msg_id,
                topic_slug=snapshot.topic_slug,
                dialogue_id=snapshot.dialogue_id,
                bot=bot,
                chat_id=message.chat.id,
            )

            async with async_session_maker() as session:
                ai_msg = DBMessage(
                    user_id=snapshot.user_id,
                    dialogue_id=snapshot.dialogue_id,
                    role='assistant',
                    content=response_text,
                    topic_slug=snapshot.topic_slug,
                )
                session.add(ai_msg)
                await session.commit()

            await send_ai_response(
                message.chat.id,
                response_text,
                bot,
                state,
                await kb.ai_response_keyboard_for_topic(snapshot.topic_slug, snapshot.dialogue_id),
            )
        except Exception:
            logger.exception("Photo processing failed")
            await message.answer("Не удалось обработать изображение. Попробуйте позже.")
        finally:
            try:
                await waiting.delete()
            except Exception:
                pass


@router.message(UserStates.chatting, F.voice | F.audio)
async def process_chat_audio(message: Message, bot: Bot, state: FSMContext | None = None):
    user_id = message.from_user.id
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user or not user.ai_disclaimer_accepted_at or (user.ai_disclaimer_version or 0) < 1:
            await check_and_request_ai_disclaimer(
                bot,
                user_id,
                message.chat.id,
                flow_type="topic",
                payload={"topic_slug": user.current_topic_slug if user else "free_session"},
            )
            return

        if user.is_blocked:
            await message.answer("⛔️ Ты заблокирован(а) администратором.")
            return

        pay_conf = await session.get(PaymentConfig, 1)
        if not user.is_admin and pay_conf.mode != 'free':
            if pay_conf.mode == 'subscription':
                if not user.subscription_end or user.subscription_end < msk_now():
                    await message.answer(
                        "❌ Твой доступ к образовательной программе истёк. Оформи доступ к программе, чтобы продолжить.",
                        reply_markup=kb.payment_alert_keyboard(),
                    )
                    return
            elif pay_conf.mode == 'token':
                cost = pay_conf.cost_per_message
                if user.balance < cost:
                    await message.answer(
                        f"❌ Недостаточно токенов. Стоимость сообщения: {cost}, твой баланс: {user.balance}",
                        reply_markup=kb.token_alert_keyboard(),
                    )
                    return
                user.balance -= cost
                await session.commit()

        current_slug = user.current_topic_slug or "free_session"
        current_did = await get_active_topic_dialogue_id(
            session, user_id, current_slug, default=user.current_dialogue_id or 1
        )
        snapshot = TopicContextSnapshot(
            user_id=user_id,
            topic_slug=current_slug,
            dialogue_id=current_did,
        )
        user_name = (user.name or user.first_name or "пользователь").strip()

    source = message.voice or message.audio
    async with ai_typing_action(bot, message.chat.id):
        waiting = await message.answer("Распознаю аудио…")
        try:
            file = await bot.get_file(source.file_id)
            audio = await bot.download_file(file.file_path)
            transcript = await transcribe_audio(audio.read(), getattr(source, 'file_name', None) or 'voice.ogg')

            audio_text = f"[Аудио] {transcript}"
            async with async_session_maker() as session:
                user_msg = DBMessage(
                    user_id=snapshot.user_id,
                    dialogue_id=snapshot.dialogue_id,
                    role='user',
                    content=audio_text,
                    topic_slug=snapshot.topic_slug,
                )
                session.add(user_msg)
                await session.flush()
                current_msg_id = user_msg.id
                await session.commit()

            response_text = await get_ai_response_for_topic(
                snapshot.user_id,
                f"Пользователь прислал аудио. Расшифровка:\n{transcript}",
                user_name,
                current_message_id=current_msg_id,
                topic_slug=snapshot.topic_slug,
                dialogue_id=snapshot.dialogue_id,
                bot=bot,
                chat_id=message.chat.id,
            )

            async with async_session_maker() as session:
                ai_msg = DBMessage(
                    user_id=snapshot.user_id,
                    dialogue_id=snapshot.dialogue_id,
                    role='assistant',
                    content=response_text,
                    topic_slug=snapshot.topic_slug,
                )
                session.add(ai_msg)
                await session.commit()

            await send_ai_response(
                message.chat.id,
                response_text,
                bot,
                state,
                await kb.ai_response_keyboard_for_topic(snapshot.topic_slug, snapshot.dialogue_id),
            )
        except Exception:
            logger.exception("Audio transcription failed")
            await message.answer("Не удалось распознать аудио. Попробуйте позже.")
        finally:
            try:
                await waiting.delete()
            except Exception:
                pass


@router.message(Command("image"))
async def process_image_generation(message: Message, bot: Bot):
    prompt = (message.text or "").partition(" ")[2].strip()
    if not prompt:
        await message.answer("После /image напишите описание изображения.")
        return
    async with ai_typing_action(bot, message.chat.id, action="upload_photo"):
        waiting = await message.answer("Генерирую изображение…")
        try:
            result = await generate_image(prompt)
            await message.answer_photo(BufferedInputFile(result, filename="kie_image.png"))
        except Exception:
            logger.exception("KIE image generation failed")
            await message.answer("Не удалось сгенерировать изображение. Проверьте настройки KIE.")
        finally:
            try:
                await waiting.delete()
            except Exception:
                pass


@router.message(UserStates.chatting, ~F.text.startswith('/'))
async def process_chat(message: Message, state: FSMContext, bot: Bot):
    await touch_user_activity(message.from_user.id)
    if message.text == "❌ Выход":
        await state.clear()
        await send_main_menu(message, bot=bot)
        return

    user_id = message.from_user.id
    user_prompt = message.text

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user or not user.ai_disclaimer_accepted_at or (user.ai_disclaimer_version or 0) < 1:
            await check_and_request_ai_disclaimer(
                bot,
                user_id,
                message.chat.id,
                flow_type="topic",
                payload={"topic_slug": user.current_topic_slug if user else "free_session"},
            )
            return

        if user.is_blocked:
            await message.answer("⛔️ Ты заблокирован(а) администратором.")
            await state.clear()
            return

        pay_conf = await session.get(PaymentConfig, 1)

        if not user.is_admin and pay_conf.mode != 'free':
            if pay_conf.mode == 'subscription':
                if not user.subscription_end or user.subscription_end < msk_now():
                    await message.answer("❌ Твой доступ к образовательной программе истёк. Оформи доступ к программе, чтобы продолжить.",
                                         reply_markup=kb.payment_alert_keyboard())
                    return

            elif pay_conf.mode == 'token':
                cost = pay_conf.cost_per_message
                if user.balance < cost:
                    await message.answer(
                        f"❌ Недостаточно токенов. Стоимость сообщения: {cost}, твой баланс: {user.balance}",
                        reply_markup=kb.token_alert_keyboard(),
                    )
                    return
                user.balance -= cost
                if user.balance <= pay_conf.low_balance_threshold and not user.low_balance_notified:
                    asyncio.create_task(bot.send_message(user_id, "⚠️ Баланс подходит к концу!"))
                    user.low_balance_notified = True
                elif user.balance > pay_conf.low_balance_threshold:
                    user.low_balance_notified = False
                await session.commit()

        # Resolve immutable snapshot ONCE before any network awaits
        current_slug = user.current_topic_slug or "free_session"
        current_did = await get_active_topic_dialogue_id(
            session, user_id, current_slug, default=user.current_dialogue_id or 1
        )
        snapshot = TopicContextSnapshot(
            user_id=user_id,
            topic_slug=current_slug,
            dialogue_id=current_did,
        )
        user_name = (user.name or user.first_name or "пользователь").strip()

        user_msg_db = DBMessage(
            user_id=snapshot.user_id,
            dialogue_id=snapshot.dialogue_id,
            role='user',
            content=user_prompt,
            topic_slug=snapshot.topic_slug,
        )
        session.add(user_msg_db)
        await session.flush()
        current_msg_id = user_msg_db.id
        await session.commit()

    waiting_msg = await message.answer("⏳ ...")
    response_text = await get_ai_response_for_topic(
        snapshot.user_id,
        user_prompt,
        user_name,
        current_message_id=current_msg_id,
        topic_slug=snapshot.topic_slug,
        dialogue_id=snapshot.dialogue_id,
        bot=bot,
        chat_id=message.chat.id,
    )
    should_start_test, response_text = extract_test_start_directive(response_text)

    try:
        await waiting_msg.delete()
    except Exception:
        pass

    async with async_session_maker() as session:
        if response_text:
            ai_msg_db = DBMessage(
                user_id=snapshot.user_id,
                dialogue_id=snapshot.dialogue_id,
                role='assistant',
                content=response_text,
                topic_slug=snapshot.topic_slug,
            )
            session.add(ai_msg_db)
            await session.commit()

    if response_text:
        await send_ai_response(
            message.chat.id,
            response_text,
            bot,
            state,
            await kb.ai_response_keyboard_for_topic(snapshot.topic_slug, snapshot.dialogue_id),
        )
    if should_start_test:
        await start_universal_test(
            bot,
            user_id,
            state,
            topic_slug=snapshot.topic_slug,
            dialogue_id=snapshot.dialogue_id,
        )


@router.callback_query(F.data == "ref_create_link")
async def ref_create_link(callback: CallbackQuery, bot: Bot):
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"

    async with async_session_maker() as session:
        content = await session.get(Content, "referral_link_text")
        header_text = content.text_content if content and content.text_content else "Твоя реферальная ссылка"

    safe_header = html.escape(header_text)
    safe_link = html.escape(link)

    msg_text = (
        f"🔗 <b>{safe_header}</b>\n\n"
        f"Скопируй ссылку:\n"
        f"<code>{safe_link}</code>\n\n"
        f"<b>Шаблоны сообщений для отправки друзьям:</b>\n\n"
        f"1️⃣ <i>«Привет! Делюсь ботом с медитациями и практиками от Виталия Наумова. Держи ссылку: {safe_link}»</i>\n\n"
        f"2️⃣ <i>«Попробуй практику осознанности в этом боте, очень помогает перезагрузиться: {safe_link}»</i>"
    )

    await callback.message.answer(
        msg_text,
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    await callback.answer()


@router.callback_query(F.data == "ref_balance")
async def ref_balance(callback: CallbackQuery):
    async with async_session_maker() as session:
        user = await session.get(User, callback.from_user.id)
        balance = user.referral_balance if user and user.referral_balance else 0
    await callback.message.answer(f"💰 Твой баланс: {balance} баллов.")
    await callback.answer()


@router.callback_query(F.data == "ref_who_invited")
async def ref_who_invited(callback: CallbackQuery):
    async with async_session_maker() as session:
        user = await session.get(User, callback.from_user.id)
        if user and user.referrer_id:
            referrer = await session.get(User, user.referrer_id)
            text = f"Тебя пригласил(а): @{referrer.username}" if referrer and referrer.username else f"ID пригласившего: {user.referrer_id}"
        else:
            text = "Ты пришел(ла) самостоятельно."
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("ref_view_list_"))
async def ref_view_list(callback: CallbackQuery):
    page = int(callback.data.split("_")[-1])
    async with async_session_maker() as session:
        offset = page * 10
        refs = await session.execute(
            select(User).where(User.referrer_id == callback.from_user.id).limit(10).offset(offset))
        refs = refs.scalars().all()

    if not refs:
        await callback.message.answer("Рефералов пока нет или это конец списка.")
        return await callback.answer()

    text = "👥 Твои рефералы:\n"
    for r in refs:
        text += f"- {r.first_name} (@{r.username})\n"

    await callback.message.answer(text)
    await callback.answer()


# --- ADMIN PANEL ---

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    # СБРОС СОСТОЯНИЯ ПРИ ВХОДЕ В АДМИНКУ
    await state.clear()

    if not await require_permission(message, AdminPermission.PANEL, state=state):
        return
    role = await get_admin_role(message.from_user.id)
    await message.answer("Админ-панель", reply_markup=kb.admin_panel_keyboard(role))


@router.callback_query(F.data == "admin_panel")
async def admin_panel_cb(callback: CallbackQuery, state: FSMContext | None = None):
    if not await require_permission(callback, AdminPermission.PANEL, state=state):
        return
    await _clear_admin_navigation_state(state)
    role = await get_admin_role(callback.from_user.id)
    await send_smart_content(callback, "Админ-панель", None, kb.admin_panel_keyboard(role))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_section_"))
async def admin_section_menu(callback: CallbackQuery, state: FSMContext | None = None):
    section_key = callback.data.removeprefix("admin_section_")
    if section_key == "clients":
        if not await require_permission(callback, AdminPermission.CLIENTS, state=state):
            return
        await admin_clients_list(callback, state, parent_callback="admin_panel")
        return
    if section_key == "admins":
        if not await require_permission(callback, AdminPermission.ADMIN_MANAGEMENT, state=state):
            return
        await admin_manage_admins(callback, state)
        return
    if section_key == "payments":
        if not await require_permission(callback, AdminPermission.PAYMENTS, state=state):
            return
        await admin_payment_settings(callback, state)
        return
    section_config = {
        "ai": (
            "🤖 <b>ИИ и знания</b>\n\nНастройки ИИ, провайдеры, модели, темы диалогов и база знаний.",
            kb.admin_ai_section_keyboard,
            AdminPermission.CONTENT,
        ),
        "content": (
            "✏️ <b>Контент и программы</b>\n\nТексты, медиа, воронка, привычки, дневник и тестовые программы.",
            kb.admin_content_section_keyboard,
            AdminPermission.CONTENT,
        ),
        "automation": (
            "⚙️ <b>Автоматизации</b>\n\nСценарии, напоминания и рассылки, которые бот выполняет сам.",
            kb.admin_automation_section_keyboard,
            AdminPermission.SYSTEM,
        ),
        "logs": (
            "📋 <b>Логи</b>\n\nОбщие логи и запросы к ИИ.",
            kb.admin_logs_section_keyboard,
            AdminPermission.SYSTEM,
        ),
    }
    config = section_config.get(section_key)
    if config is None:
        await callback.answer("Раздел не найден.", show_alert=True)
        return
    text, keyboard_factory, permission = config
    if not await require_permission(callback, permission, state=state):
        return
    await _clear_admin_navigation_state(state)
    role = await get_admin_role(callback.from_user.id)
    await send_smart_content(callback, text, None, keyboard_factory(role))
    await callback.answer()


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not await require_permission(message, AdminPermission.SYSTEM):
        return

    parts = [
        # Часть 1: Воронка + AI
        (
            "📖 <b>Инструкция по настройке бота (1/4)</b>\n\n"
            "Для входа в админку отправь /admin\n"
            "Для вызова этой инструкции отправь /help\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "🌀 <b>ВОРОНКА ГИПНОСЕССИЙ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Это главная фишка бота. Пользователь нажимает «Практика», "
            "выбирает медитацию, слушает аудио, описывает свои ощущения, "
            "ИИ анализирует его рефлексию, а потом бот мягко предлагает "
            "записаться на стратегическую сессию.\n\n"
            "<b>Этапы воронки:</b>\n"
            "1. Вступление (рассказ про метод)\n"
            "2. Выбор медитации\n"
            "3. Карточка медитации\n"
            "4. Прослушивание аудио\n"
            "5. Рефлексия (пользователь описывает ощущения)\n"
            "6. ИИ разбор рефлексии\n"
            "7. Рассказ про метод Виталия\n"
            "8. Предложение стратегической сессии\n"
            "9. FAQ по сессии\n"
            "10. Работа с возражениями через ИИ\n"
            "11. Переход на лендинг для оплаты\n\n"
            "<b>Как настроить:</b>\n"
            "Админка → Воронка гипносессий → добавлять медитации "
            "(аудиофайл + описание), редактировать тексты этапов, "
            "настраивать напоминания для тех кто бросил.\n"
            "Все тексты воронки находятся в разделе «Контент» → «Гипно-медитации и воронка».\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "🧠 <b>AI ТЕМЫ И ПРОМПТЫ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "В боте 8 AI режимов:\n"
            "• <b>Свободная сессия</b> общение с ИИ как с терапевтом\n"
            "• <b>Диагностика + сессия</b> вопросы и разбор\n"
            "• <b>Экспресс диагностика</b> быстрый разбор\n"
            "• <b>Дневник саморефлексии</b> ведение дневника\n"
            "• <b>Привычки</b> поддержка при формировании\n"
            "• <b>Разбор гипносессии</b> анализ после медитации\n"
            "• <b>Стратегическая сессия</b> продающий диалог\n"
            "• <b>Возражения</b> работа с сомнениями\n\n"
            "Промпты меняются в Админка → AI Темы и Промпты. "
            "Там же можно сделать тему платной.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 <b>НАСТРОЙКИ AI</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Три провайдера: OpenAI, DeepSeek и KIE. Переключаются одной кнопкой.\n\n"
            "KIE поддерживает чат, анализ фото, транскрибацию аудио, генерацию и редактирование изображений. "
            "Для каждого сценария модель выбирается отдельно в «Провайдеры и модели».\n\n"
            "<b>Память диалога:</b>\n"
            "• «Начало» сколько первых сообщений бот помнит всегда\n"
            "• «Контекст» сколько последних сообщений держит в памяти\n\n"
            "Без API ключа ИИ работать не будет!"
        ),

        # Часть 2: Оплата + Контент
        (
            "📖 <b>Инструкция (2/4)</b>\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "💳 <b>НАСТРОЙКИ ОПЛАТЫ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Три режима:\n"
            "• <b>Бесплатный</b> всё бесплатно, кошелёк скрыт\n"
            "• <b>По подписке</b> доступ по подписке. Создаёшь тарифы "
            "(например «1 месяц за 990₽»), пользователь платит через "
            "Telegram Stars и получает доступ на N дней\n"
            "• <b>Оплата за сообщения</b> оплата за каждое сообщение. Пополняет "
            "баланс, за каждый ответ ИИ списываются токены\n\n"
            "<b>Демо период:</b> бесплатный доступ для новых. 0 = отключён.\n\n"
            "Провайдеры оплаты настраиваются через BotFather.\n"
            "Текст «О подписке» меняется в «Контент» → «Подписка».\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "✏️ <b>КОНТЕНТ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Здесь настраиваются ВСЕ тексты и кнопки бота.\n\n"
            "Категории:\n"
            "• <b>Главная</b> приветствие, привычки, подписка\n"
            "• <b>Виталий</b> текст «О Виталии», ссылки на консультацию\n"
            "• <b>Клуб</b> закрытый клуб, рефералка, чат, материалы\n"
            "• <b>Доп. кнопки</b> Академия, Богатое мышление, Трансформации\n"
            "• <b>AI</b> вопросы для диагностики\n"
            "• <b>Книги</b> библиотека книг\n\n"
            "У каждого элемента: текст, медиа (фото/видео), ссылка для кнопки.\n\n"

            "⚠️ <b>Важно про кнопки:</b>\n"
            "Для каждой кнопки отдельно задаются надпись и ссылка или действие. "
            "Проверьте, что ссылка ведёт на нужную страницу, а не осталась примером. "
            "\n\n"
            "<b>Заполни в первую очередь:</b>\n"
            "• кнопка «Задать вопрос Виталию»\n"
            "• кнопка канала Виталия\n"
            "• кнопка чата закрытого клуба\n"
            "• кнопка записи на консультацию\n"
            "• кнопка формы спикерства"
        ),

        # Часть 3: Привычки, рефералка, база знаний, рассылка, клиенты
        (
            "📖 <b>Инструкция (3/4)</b>\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "🧘 <b>ПРИВЫЧКИ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Пользователь выбирает привычку (или создаёт свою), "
            "ставит время, бот каждый день присылает напоминание. "
            "Можно настроить серии дней и награды.\n\n"
            "В админке: шаблоны привычек, геймификация (дни цикла, "
            "награды, окно выполнения, текст победы), платность.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "🤝 <b>РЕФЕРАЛЬНАЯ ПРОГРАММА</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Пользователь создаёт ссылку приглашение. "
            "Когда по ней кто-то приходит, пригласивший получает баллы. "
            "Баллы за приглашение настраиваются в админке → Настройки Рефералки.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "📚 <b>БАЗА ЗНАНИЙ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Загружай файлы (TXT, PDF, DOCX), бот разбирает текст "
            "и использует его при ответах ИИ. Это помогает дать ИИ материалы проекта.\n"
            "Для индексации нужен ключ OpenAI.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "✉️ <b>РАССЫЛКА</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Отправляет сообщение ВСЕМ пользователям бота.\n"
            "Можно: текст, фото/видео, кнопка со ссылкой.\n\n"
            "⚠️ Если есть фото/видео, текст макс 1024 символа. "
            "Если длиннее, бот отправит медиа и текст отдельно.\n"
            "Перед отправкой покажет превью.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "👥 <b>КЛИЕНТЫ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Список всех пользователей. В карточке: ID, имя, пол, баланс, "
            "подписка, сообщения, воронка, привычки, лендинг.\n\n"
            "Можно блокировать, смотреть историю, состояние воронки.\n\n"
            "<b>Сбросы для тестирования:</b>\n"
            "В карточке клиента кнопка «Сбросы». Можно сбросить: "
            "воронку, диалог, подписку, баланс, привычки, напоминания, "
            "лендинг, имя и пол. По отдельности или всё сразу. "
            "Удобно чтобы тестить бота с одного аккаунта."
        ),

        # Часть 4: Напоминания, вебхук, логи, админы, быстрый старт
        (
            "📖 <b>Инструкция (4/4)</b>\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔔 <b>НАПОМИНАНИЯ НЕАКТИВНЫМ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Если пользователь давно не писал, бот пришлёт напоминание. "
            "Настраивается интервал первого и последующих (в часах).\n"
            "Тексты можно изменить так: «Контент» → «Главное меню / Старт» →\n"
            "«Первое напоминание неактивным» или «Повторное напоминание неактивным».\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "🌐 <b>WEBHOOK ЛЕНДИНГА</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Принимает оплаты с внешнего лендинга. Когда клиент "
            "платит на сайте, бот уведомляет клиента и вас.\n"
            "Настройки: Настройки Оплаты → Webhook лендинга.\n"
            "После изменений перезапустить бота.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "📋 <b>ЛОГИ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Просмотр и скачивание:\n"
            "• Общий лог (всё подряд)\n"
            "• Ошибки\n"
            "• Воронка\n"
            "• AI (запросы к нейросети)\n"
            "• Платежи\n"
            "• Webhook (уведомления с лендинга)\n"
            "• Индексация базы знаний\n"
            "Хранятся 7 дней.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "👥 <b>АДМИНИСТРАТОРЫ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Добавляй других админов по Telegram ID. "
            "Главного владельца удалить нельзя.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "⚡️ <b>БЫСТРЫЙ СТАРТ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "1. Добавь API-ключ OpenAI в разделе «Настройки ИИ»\n"
            "2. Проверь названия и ссылки кнопок в разделе «Контент» → «Меню и разделы»\n"
            "3. Отредактируй приветствие в разделе «Контент» → «Главное меню / Старт»\n"
            "4. Добавь медитации в воронку\n"
            "5. Настрой оплату (или оставь режим «Бесплатный»)\n"
            "6. Протестируй на себе (Сбросы в карточке клиента)\n"
            "7. Запусти рассылку или раздай ссылку"
        ),
    ]

    for part in parts:
        await message.answer(part, parse_mode='HTML')


@router.callback_query(F.data == "admin_logs")
async def admin_logs(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await _clear_admin_navigation_state(state)
    await callback.answer()
    await send_smart_content(callback, "📋 <b>Логи</b>\nВыберите файл для просмотра.", None, admin_logs_keyboard())


@router.callback_query(F.data.startswith("admin_log_view_"))
async def admin_log_view(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    await _clear_admin_navigation_state(state)
    log_key = callback.data.replace("admin_log_view_", "", 1)
    if log_key not in LOG_FILES:
        await callback.answer("Лог не найден", show_alert=True)
        return

    label, path = LOG_FILES[log_key]
    tail_text = read_log_tail(path)
    text = (
        f"📄 <b>{label}</b>\n\n"
        f"<pre>{html.escape(tail_text)}</pre>"
    )
    await callback.answer()
    await send_smart_content(callback, text, None, admin_log_view_keyboard(log_key))


@router.callback_query(F.data.startswith("admin_log_download_"))
async def admin_log_download(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    await _clear_admin_navigation_state(state)
    log_key = callback.data.replace("admin_log_download_", "", 1)
    if log_key not in LOG_FILES:
        await callback.answer("Лог не найден", show_alert=True)
        return

    label, path = LOG_FILES[log_key]
    if not path.exists():
        await callback.answer("Файл ещё не создан", show_alert=True)
        return

    file_data = path.read_bytes()
    input_file = BufferedInputFile(file_data, filename=path.name)
    await callback.answer()
    await callback.message.answer_document(input_file, caption=f"📄 {label}")


@router.callback_query(F.data == "admin_content_list")
async def admin_content_list(event, state: FSMContext | None = None):
    if hasattr(event, "from_user") and not await is_admin_actor(event.from_user.id):
        if isinstance(event, CallbackQuery):
            await event.answer("⛔️ Доступ запрещён.", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("⛔️ Доступ запрещён.")
        return
    await _clear_admin_navigation_state(state)
    builder = kb.InlineKeyboardBuilder()
    builder.button(text="🧭 Меню и разделы", callback_data="admin_menu_sections")
    builder.button(text="📂 Главное меню / Старт", callback_data="admin_cat_main")
    builder.button(text="📂 Гипно-медитации и воронка", callback_data="admin_cat_funnel")
    builder.button(text="📂 Дневник", callback_data="admin_cat_journal")
    builder.button(text="📂 Привычки", callback_data="admin_cat_habits")
    builder.button(text="📂 Подписка", callback_data="admin_cat_subscription")
    builder.button(text="📂 Расширенное меню", callback_data="admin_cat_extended")
    builder.button(text="📂 Виталий Наумов", callback_data="admin_cat_vitaly")
    builder.button(text="📂 Закрытый клуб", callback_data="admin_cat_club")
    builder.button(text="📂 Книги (Библиотека)", callback_data="admin_cat_books")
    builder.button(text="📂 Доп. разделы (Академия и др.)", callback_data="admin_cat_extra")
    builder.button(text="📂 AI Диагностика", callback_data="admin_cat_ai")

    builder.button(text="⬅️ Назад", callback_data="admin_section_content")
    builder.adjust(1)

    await send_smart_content(
        event,
        "🛠 <b>Контент</b>\n\n"
        "Здесь меняются тексты, медиа и ссылки, которые видят пользователи.\n"
        "Для названий и порядка кнопок откройте «Меню и разделы».\n\n"
        "Откройте нужный раздел ниже:",
        None,
                             builder.as_markup())
    if isinstance(event, CallbackQuery):
        await event.answer()


def _menu_admin_back_keyboard(button_id: int):
    builder = kb.InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад к кнопке", callback_data=f"admin_menu_button:{button_id}")
    builder.adjust(1)
    return builder.as_markup()


async def _menu_admin_allowed(user_id: int) -> bool:
    return await is_admin_actor(user_id)


_MENU_EDITOR_STATES = {
    AdminStates.menu_button_label.state,
    AdminStates.menu_button_action.state,
    AdminStates.menu_button_order.state,
}


async def _clear_menu_editor_state(state: FSMContext | None):
    if state is not None and await state.get_state() in _MENU_EDITOR_STATES:
        await state.clear()


@router.callback_query(F.data == "admin_menu_sections")
async def admin_menu_sections(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _menu_admin_allowed(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        sections = (await session.scalars(
            select(MenuSection).order_by(MenuSection.order_num.asc(), MenuSection.key.asc())
        )).all()
    builder = kb.InlineKeyboardBuilder()
    for section in sections:
        builder.button(text=section.title, callback_data=f"admin_menu_section:{section.key}")
    builder.button(text="⬅️ Назад к контенту", callback_data="admin_content_list")
    builder.adjust(1)
    await send_smart_content(
        callback,
        "🧭 <b>Меню и разделы</b>\n\nВыбери раздел. Здесь можно переименовать кнопки, скрыть их и вернуть обратно.",
        None,
        builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_menu_section_move:"))
async def admin_menu_section_move(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _menu_admin_allowed(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    _, section_key, direction = callback.data.split(":", 2)
    async with async_session_maker() as session:
        sections = list((await session.scalars(
            select(MenuSection).order_by(MenuSection.order_num.asc(), MenuSection.key.asc())
        )).all())
        index = next((idx for idx, item in enumerate(sections) if item.key == section_key), -1)
        neighbor = index - 1 if direction == "up" else index + 1
        if 0 <= index < len(sections) and 0 <= neighbor < len(sections):
            sections[index].order_num, sections[neighbor].order_num = sections[neighbor].order_num, sections[index].order_num
            await session.commit()
    await admin_menu_sections(callback, state=state)


@router.callback_query(F.data.startswith("admin_menu_section:"))
async def admin_menu_section(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _menu_admin_allowed(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    await _clear_admin_navigation_state(state)
    section_key = callback.data.split(":", 1)[1]
    if section_key == "books":
        async with async_session_maker() as session:
            await sync_book_menu_buttons(session)
            await session.commit()
    async with async_session_maker() as session:
        section = await session.get(MenuSection, section_key)
        buttons = (await session.scalars(
            select(MenuButton).where(MenuButton.section_key == section_key).order_by(MenuButton.order_num.asc(), MenuButton.id.asc())
        )).all()
        if section_key == "books":
            book_ids = set((await session.scalars(select(Book.id))).all())
            buttons = [
                button for button in buttons
                if button.source_type != "book" or button.source_id in book_ids
            ]
        display_labels = {}
        for button in buttons:
            label = button.label
            if button.content_key and not button.label_override:
                content = await session.get(Content, button.content_key)
                if content and content.button_text:
                    label = content.button_text
            display_labels[button.id] = label
    if not section:
        await callback.answer("Раздел не найден", show_alert=True)
        return
    builder = kb.InlineKeyboardBuilder()
    for button in buttons:
        status = "👁" if button.enabled else "🙈"
        builder.button(text=f"{status} {display_labels[button.id]}", callback_data=f"admin_menu_button:{button.id}")
    section_action = "🙈 Скрыть раздел" if section.enabled else "👁 Показать раздел"
    builder.button(text=section_action, callback_data=f"admin_menu_section_toggle:{section.key}")
    builder.button(text="⬅️ К разделам", callback_data="admin_menu_sections")
    builder.adjust(1)
    section_status = "Раздел виден клиентам" if section.enabled else "Раздел скрыт от клиентов"
    await send_smart_content(
        callback,
        f"🧩 <b>{html.escape(section.title)}</b>\n\n{section_status}.\nВыбери кнопку:",
        None,
        builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_menu_section_toggle:"))
async def admin_menu_section_toggle(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _menu_admin_allowed(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    section_key = callback.data.split(":", 1)[1]
    blocked_last_root = False
    section_not_found = False
    async with _menu_section_toggle_lock:
        async with async_session_maker() as session:
            if section_key in _ROOT_MENU_SECTION_KEYS:
                sections = list((await session.scalars(
                    select(MenuSection)
                    .where(MenuSection.key.in_(_ROOT_MENU_SECTION_KEYS))
                    .order_by(MenuSection.key.asc())
                    .with_for_update()
                )).all())
                section_by_key = {section.key: section for section in sections}
                section = section_by_key.get(section_key)
                if section is None:
                    section_not_found = True
                else:
                    counterpart_key = next(
                        key for key in _ROOT_MENU_SECTION_KEYS if key != section_key
                    )
                    counterpart = section_by_key.get(counterpart_key)
                    if section.enabled and (counterpart is None or not counterpart.enabled):
                        blocked_last_root = True
                    else:
                        section.enabled = not section.enabled
                        await session.commit()
            else:
                result = await session.execute(
                    update(MenuSection)
                    .where(MenuSection.key == section_key)
                    .values(enabled=~MenuSection.enabled)
                )
                if not result.rowcount:
                    section_not_found = True
                else:
                    await session.commit()

    if section_not_found:
        await callback.answer("Раздел не найден", show_alert=True)
        return
    if blocked_last_root:
        await callback.answer(
            "Нельзя выключить оба основных меню. Оставьте включённым главное или расширенное меню.",
            show_alert=True,
        )
        return
    await admin_menu_section(
        callback.model_copy(update={"data": f"admin_menu_section:{section_key}"}),
        state=state,
    )


async def _get_menu_button_view(button_id: int):
    async with async_session_maker() as session:
        button = await session.get(MenuButton, button_id)
        if not button:
            return None
        buttons = list((await session.scalars(
            select(MenuButton)
            .where(MenuButton.section_key == button.section_key)
            .order_by(MenuButton.order_num.asc(), MenuButton.id.asc())
        )).all())
        position = next((index for index, item in enumerate(buttons) if item.id == button.id), None)
        if position is None:
            return None
        label = button.label
        if button.content_key and not button.label_override:
            content = await session.get(Content, button.content_key)
            if content and content.button_text:
                label = content.button_text
        return {
            "id": button.id,
            "section_key": button.section_key,
            "label": label,
            "enabled": bool(button.enabled),
            "position": position + 1,
            "total": len(buttons),
        }


def _menu_button_editor_keyboard(view: dict):
    builder = kb.InlineKeyboardBuilder()
    builder.button(text="✏️ Переименовать", callback_data=f"admin_menu_button_label:{view['id']}")
    builder.button(text="🔗 Изменить ссылку или действие", callback_data=f"admin_menu_button_action:{view['id']}")
    builder.button(text="🔢 Изменить порядок", callback_data=f"admin_menu_button_order:{view['id']}")
    toggle_text = "🙈 Скрыть" if view["enabled"] else "👁 Показать"
    builder.button(text=toggle_text, callback_data=f"admin_menu_button_toggle:{view['id']}")
    builder.button(text="⬆️ Выше", callback_data=f"admin_menu_button_move:{view['id']}:up")
    builder.button(text="⬇️ Ниже", callback_data=f"admin_menu_button_move:{view['id']}:down")
    builder.button(text="⬅️ Назад к списку", callback_data=f"admin_menu_section:{view['section_key']}")
    builder.adjust(1)
    return builder.as_markup()


def _menu_button_editor_text(view: dict):
    status = "👁 Видна клиенту" if view["enabled"] else "🙈 Скрыта от клиентов"
    return (
        f"🔘 <b>{html.escape(view['label'])}</b>\n\n"
        f"{status}.\n"
        f"Позиция: {view['position']} из {view['total']}."
    )


async def _send_menu_button_editor_message(message: Message, button_id: int):
    view = await _get_menu_button_view(button_id)
    if not view:
        await message.answer("Кнопка не найдена. Открой меню ещё раз.")
        return False
    await message.answer(
        _menu_button_editor_text(view),
        parse_mode="HTML",
        reply_markup=_menu_button_editor_keyboard(view),
    )
    return True


@router.callback_query(F.data.startswith("admin_menu_button:"))
async def admin_menu_button(
    callback: CallbackQuery,
    state: FSMContext | None = None,
    *,
    answer: bool = True,
):
    if not await _menu_admin_allowed(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    await _clear_admin_navigation_state(state)
    try:
        button_id = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        await callback.answer("Кнопка не найдена", show_alert=True)
        return
    view = await _get_menu_button_view(button_id)
    if not view:
        await callback.answer("Кнопка не найдена", show_alert=True)
        return
    await send_smart_content(
        callback,
        _menu_button_editor_text(view),
        None,
        _menu_button_editor_keyboard(view),
    )
    if answer:
        await callback.answer()


async def _move_menu_button(button_id: int, direction: str):
    async with async_session_maker() as session:
        button = await session.get(MenuButton, button_id)
        if not button:
            return None
        buttons = list((await session.scalars(
            select(MenuButton)
            .where(MenuButton.section_key == button.section_key)
            .order_by(MenuButton.order_num.asc(), MenuButton.id.asc())
        )).all())
        current_index = next((index for index, item in enumerate(buttons) if item.id == button_id), None)
        if current_index is None:
            return None
        neighbor_index = current_index - 1 if direction == "up" else current_index + 1
        if not 0 <= neighbor_index < len(buttons):
            return button.section_key, False
        buttons[current_index], buttons[neighbor_index] = buttons[neighbor_index], buttons[current_index]
        for index, item in enumerate(buttons):
            item.order_num = index
        await session.commit()
        return button.section_key, True


@router.callback_query(F.data.startswith("admin_menu_button_move:"))
async def admin_menu_button_move(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _menu_admin_allowed(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    try:
        _, button_id_value, direction = callback.data.split(":", 2)
        button_id = int(button_id_value)
    except (TypeError, ValueError, IndexError):
        await callback.answer("Кнопка не найдена", show_alert=True)
        return
    if direction not in {"up", "down"}:
        await callback.answer("Действие недоступно", show_alert=True)
        return
    result = await _move_menu_button(button_id, direction)
    if result is None:
        await callback.answer("Кнопка не найдена", show_alert=True)
        return
    _, moved = result
    await admin_menu_button(
        callback.model_copy(update={"data": f"admin_menu_button:{button_id}"}),
        state=state,
        answer=False,
    )
    if moved:
        await callback.answer()
    elif direction == "up":
        await callback.answer("Кнопка уже сверху.", show_alert=True)
    else:
        await callback.answer("Кнопка уже снизу.", show_alert=True)


@router.callback_query(F.data.startswith("admin_menu_button_toggle:"))
async def admin_menu_button_toggle(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _menu_admin_allowed(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    try:
        button_id = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        await callback.answer("Кнопка не найдена", show_alert=True)
        return
    async with async_session_maker() as session:
        button = await session.get(MenuButton, button_id)
        if not button:
            await callback.answer("Кнопка не найдена", show_alert=True)
            return
        if not button.enabled and button.source_type == "book":
            book = await session.get(Book, button.source_id)
            if not book:
                await callback.answer("Книга удалена. Кнопка оставлена выключенной.", show_alert=True)
                return
            duplicate = await session.scalar(select(MenuButton.id).where(
                MenuButton.section_key == "books",
                MenuButton.source_type == "book",
                MenuButton.source_id == button.source_id,
                MenuButton.enabled == True,
                MenuButton.id != button.id,
            ))
            if duplicate:
                await callback.answer("Эта книга уже есть в меню.", show_alert=True)
                return
        result = await session.execute(
            update(MenuButton)
            .where(MenuButton.id == button_id)
            .values(enabled=~MenuButton.enabled)
        )
        if not result.rowcount:
            await callback.answer("Кнопка не найдена", show_alert=True)
            return
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await callback.answer("Кнопка уже изменяется в другом запросе.", show_alert=True)
            return
    await admin_menu_button(callback, state=state)


@router.callback_query(F.data.startswith("admin_menu_button_label:"))
async def admin_menu_button_label_start(callback: CallbackQuery, state: FSMContext):
    if not await _menu_admin_allowed(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    try:
        button_id = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        await callback.answer("Кнопка не найдена", show_alert=True)
        return
    await state.set_state(AdminStates.menu_button_label)
    await state.update_data(menu_button_id=button_id)
    await callback.message.answer(
        "Напиши новое понятное название кнопки.",
        reply_markup=_menu_admin_back_keyboard(button_id),
    )
    await callback.answer()


@router.message(AdminStates.menu_button_label, F.text)
async def admin_menu_button_label_save(message: Message, state: FSMContext):
    if not await _menu_admin_allowed(message.from_user.id):
        await state.clear()
        await message.answer("⛔️ Доступ запрещён.")
        return
    value = " ".join((message.text or "").split())
    if not value or len(value) > 64:
        await message.answer("Название должно быть от 1 до 64 символов. Попробуй ещё раз.")
        return
    data = await state.get_data()
    button_id_value = data.get("menu_button_id")
    if button_id_value is None:
        return
    button_id = int(button_id_value)
    async with async_session_maker() as session:
        button = await session.get(MenuButton, button_id)
        if not button:
            await state.clear()
            await message.answer("Кнопка не найдена. Открой разделы ещё раз.")
            return
        button.label = value
        button.label_override = True
        await session.commit()
    await state.clear()
    await _send_menu_button_editor_message(message, button_id)


@router.callback_query(F.data.startswith("admin_menu_button_action:"))
async def admin_menu_button_action_start(callback: CallbackQuery, state: FSMContext):
    if not await _menu_admin_allowed(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    try:
        button_id = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        await callback.answer("Кнопка не найдена", show_alert=True)
        return
    await state.set_state(AdminStates.menu_button_action)
    await state.update_data(menu_button_id=button_id)
    await callback.message.answer(
        "Напиши ссылку (https:// или tg://) или действие кнопки. "
        "Для возврата в меню напиши «меню».",
        reply_markup=_menu_admin_back_keyboard(button_id),
    )
    await callback.answer()


@router.message(AdminStates.menu_button_action, F.text)
async def admin_menu_button_action_save(message: Message, state: FSMContext):
    if not await _menu_admin_allowed(message.from_user.id):
        await state.clear()
        await message.answer("⛔️ Доступ запрещён.")
        return
    value = (message.text or "").strip()
    if not value or len(value) > 512:
        await message.answer("Укажи действие или ссылку длиной до 512 символов.")
        return
    try:
        action_type, action_value = classify_menu_action(value)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    data = await state.get_data()
    button_id_value = data.get("menu_button_id")
    if button_id_value is None:
        await state.clear()
        return
    button_id = int(button_id_value)
    async with async_session_maker() as session:
        button = await session.get(MenuButton, button_id)
        if not button:
            await state.clear()
            await message.answer("Кнопка не найдена. Открой разделы ещё раз.")
            return
        if button.content_key and button.action_type in {"url", "web_app"}:
            if action_type not in {"url", "web_app"}:
                await message.answer("Для этой кнопки укажи ссылку, а не действие.")
                return
            content = await session.get(Content, button.content_key)
            if not content:
                await message.answer("У кнопки не найдено содержимое. Открой её настройки ещё раз.")
                return
            content.link_url = action_value
            button.action_type = action_type
            button.action_value = None
        else:
            button.action_type = action_type
            button.action_value = action_value
        await session.commit()
    await state.clear()
    await _send_menu_button_editor_message(message, button_id)


@router.callback_query(F.data.startswith("admin_menu_button_order:"))
async def admin_menu_button_order_start(callback: CallbackQuery, state: FSMContext):
    if not await _menu_admin_allowed(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    try:
        button_id = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        await callback.answer("Кнопка не найдена", show_alert=True)
        return
    await state.set_state(AdminStates.menu_button_order)
    await state.update_data(menu_button_id=button_id)
    await callback.message.answer(
        "Введи номер кнопки в списке (1, 2, 3...).",
        reply_markup=_menu_admin_back_keyboard(button_id),
    )
    await callback.answer()


@router.message(AdminStates.menu_button_order, F.text)
async def admin_menu_button_order_save(message: Message, state: FSMContext):
    if not await _menu_admin_allowed(message.from_user.id):
        await state.clear()
        await message.answer("⛔️ Доступ запрещён.")
        return
    try:
        order_num = max(1, int((message.text or "").strip())) - 1
    except ValueError:
        await message.answer("Нужен целый номер, например 2.")
        return
    data = await state.get_data()
    button_id_value = data.get("menu_button_id")
    if button_id_value is None:
        return
    button_id = int(button_id_value)
    async with async_session_maker() as session:
        button = await session.get(MenuButton, button_id)
        if not button:
            await state.clear()
            await message.answer("Кнопка не найдена. Открой разделы ещё раз.")
            return
        section_key = button.section_key
        buttons = list((await session.scalars(
            select(MenuButton).where(MenuButton.section_key == section_key).order_by(MenuButton.order_num.asc(), MenuButton.id.asc())
        )).all())
        target_index = buttons.index(button)
        order_num = min(order_num, len(buttons) - 1)
        buttons.pop(target_index)
        buttons.insert(order_num, button)
        for index, item in enumerate(buttons):
            item.order_num = index
        await session.commit()
    await state.clear()
    await _send_menu_button_editor_message(message, button_id)


# Kept for imports from older integrations. The visible editor uses the
# database-backed section registry above.
MENU_BUTTON_ITEMS = [
    ("main", "btn_book_session_main", "📅 Кнопка 'Запись на сессию'"),
    ("vitaly", "btn_vitaly_consult", "📅 Кнопка 'Консультация'"),
    ("vitaly", "btn_vitaly_speaker", "🎤 Кнопка 'Спикерство'"),
    ("club", "btn_club_materials", "📚 Кнопка 'Материалы'"),
    ("club", "btn_club_articles", "📰 Кнопка 'Статьи'"),
    ("club", "btn_club_chat", "💬 Кнопка 'Чат'"),
    ("club", "btn_club_session", "📅 Кнопка 'Сессия'"),
    ("club", "btn_club_question", "❓ Кнопка 'Вопрос'"),
    ("club", "btn_club_masterclass", "🎭 Кнопка 'Мастер-классы'"),
    ("extra", "btn_academy", "🎓 Академия Транскоучинг"),
    ("extra", "btn_rich_thinking", "💎 Богатое мышление"),
    ("extra", "btn_transfo", "🦋 Трансформации"),
]


@router.callback_query(F.data == "admin_manage_buttons")
@router.callback_query(F.data.startswith("admin_manage_buttons_page_"))
async def admin_manage_buttons(event, state: FSMContext | None = None):
    if hasattr(event, "from_user") and not await is_admin_actor(event.from_user.id):
        if isinstance(event, CallbackQuery):
            await event.answer("⛔️ Доступ запрещён.", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("⛔️ Доступ запрещён.")
        return

    # Legacy callback kept as an alias so saved administrator messages do not
    # break. The old flat editor is intentionally no longer rendered.
    if isinstance(event, CallbackQuery):
        await admin_menu_sections(event, state=state)
    else:
        await event.answer("Откройте раздел «Контент», чтобы управлять кнопками.")
    return

CATEGORIES = {
    "main": {
        "welcome_msg": "Приветственное сообщение",
        "start_msg": "Текст главного меню",
        "btn_book_session_main": "📅 Кнопка 'Запись на сессию'",
        "habits_text": "🧘 Раздел 'Привычки'",
        "subscription_info": "ℹ️ Информация о подписке",
        "main_reminder_first": "🔔 Первое напоминание неактивным",
        "main_reminder_common": "🔔 Повторное напоминание неактивным",
    },
    "vitaly": {
        "vitaly_intro": "👤 Виталий Наумов (Текст раздела)",
        "btn_vitaly_consult": "📅 Кнопка 'Консультация'",
        "btn_vitaly_speaker": "🎤 Кнопка 'Спикерство'",
    },
    "club": {
        "closed_club_intro": "🔐 Закрытый клуб (Текст раздела)",
        "referral_intro": "🤝 Реферальная программа (Текст)",
        "referral_link_text": "🔗 Текст самой ссылки (кликабельный)",
        "btn_club_materials": "📚 Кнопка 'Материалы'",
        "btn_club_articles": "📰 Кнопка 'Статьи'",
        "btn_club_chat": "💬 Кнопка 'Чат'",
        "btn_club_session": "📅 Кнопка 'Сессия'",
        "btn_club_question": "❓ Кнопка 'Вопрос'",
        "btn_club_masterclass": "🎭 Кнопка 'Мастер-классы'",
    },
    "extra": {
        "btn_academy": "🎓 Академия Транскоучинг",
        "btn_rich_thinking": "💎 Богатое мышление",
        "btn_transfo": "🦋 Трансформации",
    },
    "ai": {
        "diagnostics_questions": "🩺 Вопросы для диагностики",
        "btn_ai_finish_dialogue": "🔘 Кнопка «Завершить диалог»",
    },
    "funnel": {
        "funnel_intro": "Приветствие гипно-медитаций",
        "funnel_choice_intro": "Выбор практики",
        "funnel_listen_instruction": "Инструкция перед практикой",
        "funnel_reflection_prompt": "Запрос ощущений после практики",
        "funnel_fallback_text": "Ответ при технической ошибке разбора",
        "crisis_contacts": "Контакты помощи при кризисе",
        "funnel_strategy_sales": "Экран стратегической сессии",
        "funnel_vitaly_method": "Подробнее о Виталии",
        "funnel_no_strategy": "Экран отказа от оффера",
        "funnel_strategy_faq": "Вопросы о стратегической сессии",
        "funnel_objection_intro": "Диалог о сомнениях",
        "funnel_landing_intro": "Подводка к записи",
        "session_completed": "Экран завершения AI-сессии",
    },
    "journal": {
        "journal_intro": "Вход в дневник",
        "journal_video": "Видео дневника",
    },
    "habits": {
        "habits_text": "Экран полезных привычек",
    },
    "subscription": {
        "subscription_info": "Экран подписки",
    },
    "extended": {
        "extended_menu_intro": "Вступление расширенного меню",
        "vitaly_intro": "Страница Виталия",
        "books_intro": "Страница библиотеки книг",
        "closed_club_intro": "Страница закрытого клуба",
        "referral_intro": "Страница реферальной программы",
        "thematic_sessions_intro": "Страница тематических сессий",
    },
}

HUMAN_NAMES = {
    "welcome_msg": "Приветственное сообщение",
    "start_msg": "Текст главного меню",
    "btn_book_session_main": "Кнопка 'Запись на сессию'",
    "habits_text": "Раздел 'Привычки'",
    "subscription_info": "Информация о подписке",
    "main_reminder_first": "Первое напоминание неактивным",
    "main_reminder_common": "Повторное напоминание неактивным",
    "vitaly_intro": "Виталий Наумов (Текст)",
    "btn_vitaly_consult": "Кнопка 'Консультация'",
    "btn_vitaly_speaker": "Кнопка 'Спикерство'",
    "closed_club_intro": "Закрытый клуб (Текст)",
    "referral_intro": "Реферальная программа (Текст)",
    "referral_link_text": "Текст самой реферальной ссылки",
    "btn_club_materials": "Кнопка 'Материалы'",
    "btn_club_articles": "Кнопка 'Статьи'",
    "btn_club_chat": "Кнопка 'Чат'",
    "btn_club_session": "Кнопка 'Сессия'",
    "btn_club_question": "Кнопка 'Вопрос'",
    "btn_club_masterclass": "Кнопка 'Мастер-классы'",
    "books_intro": "Раздел 'Книги' (Описание)",
    "btn_academy": "Академия Транскоучинг",
    "btn_rich_thinking": "Богатое мышление",
    "btn_transfo": "Трансформации",
    "diagnostics_questions": "Вопросы диагностики",
    "btn_ai_finish_dialogue": "Кнопка «Завершить диалог»",
    "funnel_intro": "Приветствие гипно-медитаций",
    "funnel_choice_intro": "Выбор практики",
    "funnel_listen_instruction": "Инструкция перед практикой",
    "funnel_reflection_prompt": "Запрос ощущений после практики",
    "funnel_fallback_text": "Ответ при технической ошибке разбора",
    "crisis_contacts": "Контакты помощи при кризисе",
    "funnel_strategy_sales": "Экран стратегической сессии",
    "funnel_vitaly_method": "Подробнее о Виталии",
    "funnel_no_strategy": "Экран отказа от оффера",
    "funnel_strategy_faq": "Вопросы о стратегической сессии",
    "funnel_objection_intro": "Диалог о сомнениях",
    "funnel_landing_intro": "Подводка к записи",
    "session_completed": "Экран завершения AI-сессии",
    "journal_intro": "Вход в дневник",
    "journal_video": "Видео дневника",
    "extended_menu_intro": "Вступление расширенного меню",
    "thematic_sessions_intro": "Страница тематических сессий",
}


_CONTENT_EDITOR_DEFAULT_BACK = {
    "pay": "admin_payment_settings",
    "buttons": "admin_manage_buttons",
}
_CONTENT_EDITOR_ALLOWED_BACK = {
    "admin_content_list",
    "admin_manage_buttons",
    "admin_payment_settings",
    "admin_prodamus_settings",
    "admin_reminders_settings",
}


def _content_editor_return_callback(data: dict) -> str:
    explicit = data.get("return_callback")
    if explicit in _CONTENT_EDITOR_ALLOWED_BACK:
        return explicit
    if isinstance(explicit, str) and explicit.startswith("admin_cat_"):
        category = explicit.removeprefix("admin_cat_").split("_page_", 1)[0]
        if category in CATEGORIES:
            return explicit
    category = data.get("category", "main")
    return _CONTENT_EDITOR_DEFAULT_BACK.get(category, f"admin_cat_{category}")


async def _return_from_content_editor(
    event: Message,
    category: str,
    return_callback: str | None = None,
    state: FSMContext | None = None,
) -> None:
    callback_data = _content_editor_return_callback(
        {"category": category, "return_callback": return_callback}
    )
    if callback_data == "admin_reminders_settings":
        await admin_reminders_settings(event, state=state)
        return
    if callback_data == "admin_payment_settings":
        role = await get_admin_role(getattr(event.from_user, "id", None))
        if role_has_permission(role, AdminPermission.PAYMENTS):
            await admin_payment_settings(event, state=state)
        else:
            await admin_content_list(event, state=state)
        return
    if callback_data == "admin_prodamus_settings":
        role = await get_admin_role(getattr(event.from_user, "id", None))
        if role_has_permission(role, AdminPermission.PAYMENTS):
            await admin_prodamus_settings(event, state=state)
        else:
            await admin_content_list(event, state=state)
        return
    if callback_data == "admin_content_list":
        await admin_content_list(event, state=state)
        return
    if callback_data == "admin_manage_buttons":
        await admin_manage_buttons(event, state=state)
        return

    class FakeUpdate:
        def __init__(self, msg, data):
            self.message = msg
            self.data = data
            self.from_user = msg.from_user
            self.bot = msg.bot
            self.chat = msg.chat

    await admin_content_category(FakeUpdate(event, callback_data), state=state)


@router.callback_query(F.data.startswith("admin_cat_"))
async def admin_content_category(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    await _clear_admin_navigation_state(state)
    category_payload = callback.data.split("admin_cat_")[1]
    if "_page_" in category_payload:
        category, page_raw = category_payload.rsplit("_page_", 1)
        page = int(page_raw)
    else:
        category = category_payload
        page = 0

    if category == "books":
        await admin_books_manager(callback, state=state)
        return

    current_cat_items = CATEGORIES.get(category, {})

    builder = kb.InlineKeyboardBuilder()
    items = list(current_cat_items.items())
    page_size = 10
    total_pages = max(1, math.ceil(len(items) / page_size)) if items else 1
    page = max(0, min(page, total_pages - 1))
    page_items = items[page * page_size:(page + 1) * page_size]

    for key, name in page_items:
        builder.row(InlineKeyboardButton(text=f"✏️ {name}", callback_data=f"edit_cont_{category}:{key}"))

    builder.row(*kb.standard_pagination_row(
        page,
        total_pages,
        lambda target: f"admin_cat_{category}_page_{target}",
    ))
    builder.row(InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="admin_content_list"))

    category_names = {
        "main": "Главное меню / Старт",
        "funnel": "Гипно-медитации и воронка",
        "journal": "Дневник",
        "habits": "Привычки",
        "subscription": "Подписка",
        "extended": "Расширенное меню",
        "vitaly": "Виталий Наумов",
        "club": "Закрытый клуб",
        "extra": "Дополнительные разделы",
        "ai": "AI-диагностика",
    }
    await send_smart_content(
        callback,
        f"✏️ <b>{category_names.get(category, 'Раздел')}</b>\n\n"
        "Выберите, что изменить. После отправки нового текста бот покажет результат сохранения.",
        None,
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("edit_cont_"))
async def edit_content_start(callback: CallbackQuery, state: FSMContext):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    data_str = callback.data.split("edit_cont_")[1]
    source_callback = None
    if "__" in data_str:
        data_str, source_callback = data_str.rsplit("__", 1)
    state_data = await state.get_data()

    if ":" in data_str:
        category, key, *return_parts = data_str.split(":", 2)
        return_callback = return_parts[0] if return_parts else source_callback
    else:
        category, key = "main", data_str
        return_callback = source_callback
    if not return_callback:
        return_callback = state_data.get("return_callback")

    await state.set_state(AdminStates.edit_content)
    await state.update_data(key=key, category=category, return_callback=return_callback)

    readable_name = HUMAN_NAMES.get(key, key)

    async with async_session_maker() as session:
        content_obj = await session.get(Content, key, options=[selectinload(Content.media)])
        has_media = bool(content_obj and content_obj.media)

    info_parts = [f"📝 <b>Редактирование: {readable_name}</b>\n"]
    if content_obj:
        if content_obj.text_content:
            preview_text = truncate_html_preview(content_obj.text_content, max_length=1200)
            info_parts.append(f"<b>Текущий текст:</b>\n{preview_text}\n")
        if content_obj.button_text:
            info_parts.append(f"<b>Текст кнопки:</b> <code>{content_obj.button_text}</code>")
        if content_obj.link_url:
            info_parts.append(f"<b>Ссылка:</b> <code>{content_obj.link_url}</code>")
        if content_obj.media:
            media_types = ", ".join(m.file_type for m in content_obj.media)
            info_parts.append(f"<b>Текущее медиа:</b> {media_types}")
    info_parts.append("\nОтправьте новое значение (текст, фото или видео) или нажмите «Назад»:")
    text = "\n".join(info_parts)

    back_callback = _content_editor_return_callback(
        {"category": category, "return_callback": return_callback}
    )

    back_kb = InlineKeyboardBuilder()
    if has_media:
        media_callback = f"confirm_del_cont_media_{category}:{key}"
        back_kb.button(text="🗑 Удалить медиа", callback_data=media_callback)
    back_kb.button(text="⬅️ Назад", callback_data=back_callback)
    back_kb.adjust(1)

    if content_obj and content_obj.media:
        m = content_obj.media[0]
        try:
            if m.file_type == "photo":
                await callback.message.answer_photo(m.file_id, caption="Текущее медиа")
            elif m.file_type == "video":
                await callback.message.answer_video(m.file_id, caption="Текущее медиа")
        except Exception:
            pass

    if hasattr(callback.message, "answer") and callable(callback.message.answer):
        msg = await callback.message.answer(text, reply_markup=back_kb.as_markup(), parse_mode='HTML')
    else:
        bot = getattr(callback, "bot", getattr(callback.message, "bot", None))
        msg = await bot.send_message(callback.message.chat.id, text, reply_markup=back_kb.as_markup(), parse_mode='HTML')

    await state.update_data(last_msg_id=msg.message_id)

    try:
        await callback.message.delete()
    except:
        pass
    await callback.answer()


async def _execute_content_media_delete(callback: CallbackQuery, state: FSMContext | None, data_str: str):
    if ":" in data_str:
        category, key, *return_parts = data_str.split(":", 2)
        return_callback = return_parts[0] if return_parts else None
    else:
        category, key = "main", data_str
        return_callback = None
    if not return_callback:
        return_callback = (await state.get_data()).get("return_callback")
    async with async_session_maker() as session:
        await session.execute(delete(ContentMedia).where(ContentMedia.content_key == key))
        await session.commit()
    invalidate_content_cache(key)
    await callback.answer("Медиа удалено.")
    next_callback_data = f"edit_cont_{category}:{key}"
    if return_callback:
        next_callback_data += f":{return_callback}"
    await edit_content_start(
        _callback_with_data(callback, next_callback_data),
        state,
    )


@router.callback_query(F.data.startswith("del_cont_media_"))
async def del_cont_media(callback: CallbackQuery, state: FSMContext):
    if not await require_permission(callback, AdminPermission.CONTENT):
        return
    data_str = callback.data.split("del_cont_media_", 1)[1]
    if isinstance(callback, CallbackQuery):
        if ":" in data_str:
            category, key, *return_parts = data_str.split(":", 2)
            return_callback = return_parts[0] if return_parts else None
        else:
            category, key = "main", data_str
            return_callback = None
        if not key:
            await callback.answer("Медиа не найдено.", show_alert=True)
            return
        await _ask_admin_confirmation(
            callback,
            "Медиа этого элемента будет удалено. Текст и остальные настройки сохранятся.",
            f"confirmed_del_cont_media_{category}:{key}",
            return_callback or _content_editor_return_callback(await state.get_data()),
        )
        return
    await _execute_content_media_delete(callback, state, data_str)


@router.callback_query(F.data.startswith("confirmed_del_cont_media_"))
async def confirmed_del_cont_media(callback: CallbackQuery, state: FSMContext):
    if not await require_permission(callback, AdminPermission.CONTENT):
        return
    data_str = callback.data.removeprefix("confirmed_del_cont_media_")
    await _execute_content_media_delete(callback, state, data_str)


@router.callback_query(F.data.startswith("confirm_del_cont_media_"))
async def confirm_del_cont_media(callback: CallbackQuery, state: FSMContext):
    if not await require_permission(callback, AdminPermission.CONTENT):
        return
    data_str = callback.data.removeprefix("confirm_del_cont_media_")
    if ":" not in data_str:
        await callback.answer("Медиа не найдено.", show_alert=True)
        return
    category, key = data_str.split(":", 1)
    await _ask_admin_confirmation(
        callback,
        "Медиа этого элемента будет удалено. Текст и остальные настройки сохранятся.",
        f"confirmed_del_cont_media_{category}:{key}",
        _content_editor_return_callback(await state.get_data()),
    )


@router.message(AdminStates.edit_content, ~F.text.startswith('/'))
async def save_content(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    if not await is_admin_actor(message.from_user.id):
        await message.answer("⛔️ Доступ запрещён.")
        await state.clear()
        return

    data = await state.get_data()
    key = data['key']
    category = data.get('category', 'main')
    return_callback = data.get('return_callback')

    if message.text != ".":
        # Определяем текст (из сообщения или из подписи к медиа)
        text = message.html_text or message.caption or message.text

        async with async_session_maker() as session:
            content = await session.get(Content, key, options=[selectinload(Content.media)])
            if not content:
                content = Content(key=key)
                session.add(content)

            # Обновляем текст, если он есть
            if text:
                content.text_content = text
                if key.startswith("btn_"):
                    content.button_text = text

            has_media = bool(content.media)
            # Если прислали медиа — обновляем его
            if message.photo or message.video:
                # Сначала удаляем старое медиа для этого ключа
                await session.execute(delete(ContentMedia).where(ContentMedia.content_key == key))

                # Добавляем новое
                if message.photo:
                    session.add(ContentMedia(content_key=key, file_type='photo', file_id=message.photo[-1].file_id))
                elif message.video:
                    session.add(ContentMedia(content_key=key, file_type='video', file_id=message.video.file_id))
                has_media = True

            await session.commit()
        invalidate_content_cache(key)

        # Предупреждение о длине caption
        final_text = content.text_content or ""
        if has_media and len(final_text) > 1024:
            await message.answer(
                f"⚠️ Текст ({len(final_text)} символов) превышает лимит подписи к медиа (1024). "
                f"При отправке медиа и текст будут отдельными сообщениями. "
                f"Сократите текст до 1024 символов для отправки одним сообщением."
            )

    if key.startswith("btn_"):
        await state.set_state(AdminStates.edit_url)

        async with async_session_maker() as session:
            content = await session.get(Content, key)
            current_url = content.link_url if content else "нет"

        back_callback = _content_editor_return_callback(data)
        back_kb = InlineKeyboardBuilder()
        back_kb.button(text="⬅️ Отмена", callback_data=back_callback)

        msg = await message.answer(
            f"👇 <b>Ссылка для кнопки</b>\nТекущая: {current_url}\n\nОтправьте новую ссылку или точку «.»",
            reply_markup=back_kb.as_markup(),
            parse_mode='HTML'
        )
        await state.update_data(last_msg_id=msg.message_id)
        return

    asyncio.create_task(send_temp_notification(bot, message.chat.id, "✅ Контент обновлен!"))
    await state.clear()
    await _return_from_content_editor(message, category, return_callback)


@router.message(AdminStates.edit_url, ~F.text.startswith('/'))
async def save_content_url(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    if not await is_admin_actor(message.from_user.id):
        await message.answer("⛔️ Доступ запрещён.")
        await state.clear()
        return

    data = await state.get_data()
    key = data['key']
    category = data.get('category', 'main')
    return_callback = data.get('return_callback')
    new_url = message.text.strip()

    if new_url != ".":
        try:
            new_url = validate_menu_url(new_url)
        except ValueError as exc:
            msg = await message.answer(
                f"❌ {exc}",
                reply_markup=kb.back_to_previous_menu(_content_editor_return_callback(data)),
            )
            await state.update_data(last_msg_id=msg.message_id)
            return

        async with async_session_maker() as session:
            content = await session.get(Content, key)
            if content:
                content.link_url = new_url
                await session.commit()
        invalidate_content_cache(key)

    asyncio.create_task(send_temp_notification(bot, message.chat.id, "✅ Кнопка обновлена!"))
    await state.clear()
    await _return_from_content_editor(message, category, return_callback)


# -- Topics/Prompt Editing --
TOPIC_ADMIN_SECTION_ORDER = (
    ("funnel", "Воронка"),
    ("ai_sessions", "ИИ-сессии"),
    ("thematic", "Тематические"),
    ("journal", "Дневник"),
    ("non_ai", "⚠️ Без ИИ-диалога"),
)
TOPIC_ADMIN_SECTION_SLUGS = {
    "funnel": ("funnel_demo", "strategy_objections", "strategy_session"),
    "ai_sessions": ("free_session", "diagnostics", "express"),
    "thematic": (
        "stress",
        "self_esteem",
        "relationships",
        "sleep_recovery",
        "motivation_goals",
        "emotional_intelligence",
    ),
    "journal": ("journal",),
    "non_ai": ("habits",),
}


def classify_topic_admin_section(topic) -> str:
    slug = str(getattr(topic, "slug", "") or "").strip().casefold()
    for section, slugs in TOPIC_ADMIN_SECTION_SLUGS.items():
        if slug in slugs:
            return section
    if getattr(topic, "system_prompt", None):
        return "thematic"
    return "non_ai"


def group_topics_for_admin(topics) -> dict[str, list]:
    grouped = {section: [] for section, _ in TOPIC_ADMIN_SECTION_ORDER}
    section_order = {
        slug: index
        for section, slugs in TOPIC_ADMIN_SECTION_SLUGS.items()
        for index, slug in enumerate(slugs)
    }
    for topic in topics:
        grouped[classify_topic_admin_section(topic)].append(topic)
    for section, topics_in_section in grouped.items():
        topics_in_section.sort(
            key=lambda topic: (
                section_order.get(getattr(topic, "slug", ""), 10_000),
                str(getattr(topic, "name", "") or "").casefold(),
                int(getattr(topic, "id", 0) or 0),
            )
        )
    return grouped


def topic_admin_button_text(topic, section: str) -> str:
    paid_icon = "💰" if topic.is_paid else "🆓"
    if section == "non_ai":
        return f"⚠️ {topic.name} (без ИИ-диалога)"
    return f"📝 {topic.name} {paid_icon}"


@router.callback_query(F.data == "admin_topics_list")
@router.callback_query(F.data.startswith("admin_topics_list_"))
@router.callback_query(F.data.startswith("admin_topics_page_"))
async def admin_topics_list(event, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    raw_page = str(getattr(event, "data", "") or "")
    try:
        page = (
            int(raw_page.rsplit("_", 1)[1])
            if raw_page.startswith(("admin_topics_list_", "admin_topics_page_"))
            else 0
        )
    except ValueError:
        page = 0
    async with async_session_maker() as session:
        topics = await session.execute(
            select(Topic).order_by(Topic.id.asc())
        )
        topics = topics.scalars().all()

    grouped = group_topics_for_admin(topics)
    ordered_topics = [
        (section, topic)
        for section, _ in TOPIC_ADMIN_SECTION_ORDER
        for topic in grouped[section]
    ]
    page_size = 10
    total_pages = max(1, (len(ordered_topics) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    page_topics = ordered_topics[page * page_size:(page + 1) * page_size]
    builder = kb.InlineKeyboardBuilder()
    text_lines = [
        "💬 <b>Темы диалогов</b>",
        "",
        f"Страница {page + 1} из {total_pages}. Выберите тему для настройки:",
    ]
    current_section = None
    for section, topic in page_topics:
        if section != current_section:
            label = dict(TOPIC_ADMIN_SECTION_ORDER).get(section, section)
            text_lines.extend(["", f"<b>{html.escape(label)}</b>"])
            current_section = section
        text_lines.append(f"• {html.escape(topic.name)}")
        builder.row(InlineKeyboardButton(
            text=topic_admin_button_text(topic, section),
            callback_data=f"edit_topic_{topic.slug}",
        ))
    builder.row(*kb.standard_pagination_row(
        page,
        total_pages,
        lambda target: f"admin_topics_list_{target}",
    ))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_section_ai"))

    await send_smart_content(event, "\n".join(text_lines), None, builder.as_markup())


@router.callback_query(F.data.startswith("edit_topic_"))
async def edit_topic_menu(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    slug = callback.data.split("edit_topic_")[1]

    async with async_session_maker() as session:
        topic = await session.scalar(select(Topic).where(Topic.slug == slug))
        topic_name = topic.name if topic else slug
        current_prompt = topic.system_prompt if topic and topic.system_prompt else "Стандартный (не задан)"
        is_paid = topic.is_paid if topic else True

    preview = current_prompt[:200] + "..." if len(current_prompt) > 200 else current_prompt
    paid_status = "💰 Платная" if is_paid else "🆓 Бесплатная"

    text = (
        f"⚙️ <b>Настройки темы: {topic_name}</b>\n\n"
        f"💳 <b>Доступ:</b> {paid_status}\n\n"
        f"📝 <b>Текущий системный промпт (начало):</b>\n"
        f"<i>{html.escape(preview)}</i>\n\n"
        f"👇 <b>Что можно сделать:</b>\n"
        f"1. <b>Изменить промпт</b> — задать новую инструкцию для AI в этом режиме.\n"
        f"2. <b>Прикрепить файл</b> — выбрать документы из Базы Знаний, которые AI будет использовать для ответов в этой теме.\n"
        f"3. <b>Скачать</b> — получить текущий промпт файлом.\n"
        f"4. <b>Переключить платный/бесплатный</b> — сделать тему доступной всем или только по подписке."
    )

    toggle_text = "🆓 Сделать бесплатной" if is_paid else "💰 Сделать платной"
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить промпт", callback_data=f"set_prompt_{slug}")
    builder.button(text="📥 Скачать текущий промпт (.txt)", callback_data=f"download_prompt_{slug}")
    builder.button(text="📎 База знаний (привязка)", callback_data=f"attach_kb_{slug}")
    builder.button(text=toggle_text, callback_data=f"toggle_topic_paid_{slug}")
    builder.button(text="⚙️ Параметры диалога", callback_data=f"admin_topic_dialogue_cfg_{slug}")
    builder.button(text="⬅️ Назад", callback_data="admin_topics_list")
    builder.adjust(1)

    await send_smart_content(callback, text, None, builder.as_markup())


@router.callback_query(F.data.startswith("download_prompt_"))
async def download_topic_prompt(callback: CallbackQuery, bot: Bot):
    slug = callback.data.split("download_prompt_")[1]

    async with async_session_maker() as session:
        topic = await session.scalar(select(Topic).where(Topic.slug == slug))
        content = topic.system_prompt if topic and topic.system_prompt else "Prompt is empty."
        topic_name = topic.name if topic else slug

    file_data = content.encode('utf-8')
    input_file = BufferedInputFile(file_data, filename=f"prompt_{slug}.txt")

    await callback.message.answer_document(input_file, caption=f"📄 Промпт для темы: <b>{topic_name}</b>", parse_mode='HTML')
    await callback.answer()


@router.callback_query(F.data.startswith("set_prompt_"))
async def set_prompt_start(callback: CallbackQuery, state: FSMContext):
    slug = callback.data.split("set_prompt_")[1]
    await state.set_state(AdminStates.edit_topic_prompt)
    await state.update_data(slug=slug)

    # Кнопка назад ведет к редактированию темы, а не в главное меню
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=f"edit_topic_{slug}")

    msg = await callback.message.answer("Пришлите новый системный промпт (можно .txt файл):",
                                        reply_markup=builder.as_markup())
    await state.update_data(last_msg_id=msg.message_id)
    try:
        await callback.message.delete()
    except:
        pass


@router.message(AdminStates.edit_topic_prompt, ~F.text.startswith('/'))
async def save_topic_prompt(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    data = await state.get_data()
    slug = data['slug']

    prompt = ""
    if message.document:
        file = await bot.download(message.document)
        prompt = file.read().decode('utf-8')
    else:
        prompt = message.text

    async with async_session_maker() as session:
        topic = await session.scalar(select(Topic).where(Topic.slug == slug))
        topic.system_prompt = prompt
        await session.commit()

    asyncio.create_task(send_temp_notification(bot, message.chat.id, "✅ Промпт обновлен!"))
    await state.clear()

    await admin_topics_list(message)


@router.callback_query(F.data.startswith("toggle_topic_paid_"))
async def toggle_topic_paid(callback: CallbackQuery):
    slug = callback.data.split("toggle_topic_paid_")[1]
    async with async_session_maker() as session:
        topic = await session.scalar(select(Topic).where(Topic.slug == slug))
        if topic:
            topic.is_paid = not topic.is_paid
            await session.commit()
    await callback.answer("Обновлено")
    await edit_topic_menu(_callback_with_data(callback, f"edit_topic_{slug}"))


@router.callback_query(F.data.startswith("attach_kb_"))
async def attach_kb_list(callback: CallbackQuery):
    payload = callback.data.split("attach_kb_")[1]
    if "_page_" in payload:
        slug, page_raw = payload.rsplit("_page_", 1)
        page = int(page_raw)
    else:
        slug = payload
        page = 0
    page_size = 10

    async with async_session_maker() as session:
        total = await session.scalar(select(func.count(KnowledgeBase.id))) or 0
        total_pages = max(1, math.ceil(total / page_size)) if total else 1
        page = max(0, min(page, total_pages - 1))
        files = await session.execute(
            select(KnowledgeBase).order_by(KnowledgeBase.id.desc()).limit(page_size).offset(page * page_size)
        )
        files = files.scalars().all()
        topic = await session.scalar(
            select(Topic).where(Topic.slug == slug).options(selectinload(Topic.knowledge_base_files)))
        attached_ids = [f.id for f in topic.knowledge_base_files]
        topic_name = topic.name if topic else slug

    builder = InlineKeyboardBuilder()
    for f in files:
        icon = "✅" if f.id in attached_ids else "⭕️"
        builder.row(InlineKeyboardButton(text=f"{icon} {f.filename}", callback_data=f"toggle_kb_{slug}_{f.id}"))

    builder.row(*kb.standard_pagination_row(
        page,
        total_pages,
        lambda target: f"attach_kb_{slug}_page_{target}",
    ))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"edit_topic_{slug}"))

    text = (
        f"📎 <b>Управление знаниями для: {topic_name}</b>\n\n"
        f"Здесь вы решаете, какие документы AI должен читать при ответе на вопросы в этой теме.\n\n"
        f"✅ — Файл используется (AI его видит)\n"
        f"⭕️ — Файл отключен (AI его игнорирует)\n\n"
        f"<i>Нажимайте на кнопки ниже, чтобы переключить статус:</i>"
    )

    await send_smart_content(callback, text, None, builder.as_markup())


@router.callback_query(F.data.startswith("toggle_kb_"))
async def toggle_kb(callback: CallbackQuery):
    parts = callback.data.split("_")

    fid = int(parts[-1])
    slug = "_".join(parts[2:-1])

    async with async_session_maker() as session:
        topic = await session.scalar(
            select(Topic).where(Topic.slug == slug).options(selectinload(Topic.knowledge_base_files)))
        kb_file = await session.get(KnowledgeBase, fid)

        if kb_file in topic.knowledge_base_files:
            topic.knowledge_base_files.remove(kb_file)
        else:
            topic.knowledge_base_files.append(kb_file)
        await session.commit()

    new_callback = callback.model_copy(update={"data": f"attach_kb_{slug}"})
    await attach_kb_list(new_callback)


@router.callback_query(F.data == "admin_mailing")
async def mailing_start(callback: CallbackQuery, state: FSMContext):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    await state.set_state(AdminStates.mailing_content)
    # Сбрасываем данные рассылки при новом старте
    await state.update_data(mail_text="", mail_media_id=None, mail_media_type=None, mail_btn_text=None,
                            mail_btn_action=None, mail_audience="all")

    msg = await callback.message.answer(
        "📣 <b>Создание рассылки</b>\n\nПришлите сообщение (текст, фото или видео), которое хотите отправить пользователям:",
        reply_markup=kb.back_to_previous_menu("admin_section_automation"), parse_mode='HTML')
    await state.update_data(last_msg_id=msg.message_id)
    try:
        await callback.message.delete()
    except:
        pass


@router.message(AdminStates.mailing_content, ~F.text.startswith('/'))
async def mailing_receive_first(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)

    text = message.html_text or message.caption or message.text or ""
    mid = None
    mtype = None
    if message.photo:
        mid = message.photo[-1].file_id
        mtype = 'photo'
    elif message.video:
        mid = message.video.file_id
        mtype = 'video'

    if not text and not mid:
        await message.answer("Добавьте текст, фото или видео для рассылки.")
        return

    await state.update_data(mail_text=text, mail_media_id=mid, mail_media_type=mtype)

    if mid and text and len(text) > 1024:
        await message.answer(
            f"⚠️ <b>Внимание:</b> текст ({len(text)} символов) превышает лимит подписи к медиа (1024).\n"
            f"Рассылка будет отправлена двумя сообщениями: сначала медиа, затем текст с кнопкой.\n\n"
            f"Чтобы отправить одним сообщением — сократите текст до 1024 символов.",
            parse_mode='HTML'
        )

    await show_mailing_preview(message.chat.id, state, bot)


async def show_mailing_preview(chat_id, state: FSMContext, bot: Bot):
    data = await state.get_data()
    text = data.get('mail_text', '')
    mid = data.get('mail_media_id')
    mtype = data.get('mail_media_type')

    btn_text = data.get('mail_btn_text')
    btn_action = data.get('mail_btn_action')

    # Формируем кнопку для предпросмотра
    preview_markup = None
    if btn_text and btn_action:
        builder = InlineKeyboardBuilder()
        if btn_action.startswith("http") or btn_action.startswith("tg://"):
            builder.button(text=btn_text, url=btn_action)
        else:
            builder.button(text=btn_text, callback_data="dummy_btn")
        preview_markup = builder.as_markup()

    # Отправляем "черновик"
    await bot.send_message(chat_id, "👁 <b>ПРЕДПРОСМОТР (так увидит пользователь):</b>", parse_mode='HTML')

    try:
        if mid:
            if text and len(text) > 1024:
                if mtype == 'photo':
                    await bot.send_photo(chat_id, mid)
                elif mtype == 'video':
                    await bot.send_video(chat_id, mid)
                await bot.send_message(chat_id, text, reply_markup=preview_markup, parse_mode='HTML')
            else:
                if mtype == 'photo':
                    await bot.send_photo(chat_id, mid, caption=text, reply_markup=preview_markup, parse_mode='HTML')
                elif mtype == 'video':
                    await bot.send_video(chat_id, mid, caption=text, reply_markup=preview_markup, parse_mode='HTML')
        else:
            await bot.send_message(chat_id, text, reply_markup=preview_markup, parse_mode='HTML')
    except Exception as e:
        await bot.send_message(chat_id, f"⚠️ Ошибка предпросмотра: {e}")

    # Меню управления
    audience = data.get('mail_audience', 'all')
    await bot.send_message(chat_id, "⚙️ <b>Меню рассылки:</b>", reply_markup=kb.mailing_preview_keyboard(audience),
                           parse_mode='HTML')


@router.callback_query(F.data == "mail_preview_back")
async def mail_preview_back_handler(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(AdminStates.mailing_content)
    try:
        await callback.message.delete()
    except:
        pass
    await show_mailing_preview(callback.message.chat.id, state, bot)


@router.callback_query(F.data == "mail_edit_text")
async def mail_edit_text_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.mailing_edit_text)
    msg = await callback.message.answer(
        "✏️ Введите новый текст рассылки:",
        reply_markup=kb.back_to_previous_menu("mail_preview_back"),
    )
    await state.update_data(last_msg_id=msg.message_id)
    await callback.answer()


@router.message(AdminStates.mailing_edit_text, ~F.text.startswith('/'))
async def mail_edit_text_save(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    text = message.html_text or message.text
    await state.update_data(mail_text=text)

    data = await state.get_data()
    if data.get('mail_media_id') and text and len(text) > 1024:
        await message.answer(
            f"⚠️ Текст ({len(text)} символов) превышает лимит подписи к медиа (1024). "
            f"Рассылка будет отправлена двумя сообщениями.",
            parse_mode='HTML'
        )

    await show_mailing_preview(message.chat.id, state, bot)


@router.callback_query(F.data == "mail_edit_media")
async def mail_edit_media_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.mailing_edit_media)
    msg = await callback.message.answer(
        "🖼 Пришлите новое фото или видео (или напишите «нет», чтобы удалить медиа):",
        reply_markup=kb.back_to_previous_menu("mail_preview_back"),
    )
    await state.update_data(last_msg_id=msg.message_id)
    await callback.answer()


@router.message(AdminStates.mailing_edit_media, ~F.text.startswith('/'))
async def mail_edit_media_save(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)

    if message.text and message.text.lower() == 'нет':
        await state.update_data(mail_media_id=None, mail_media_type=None)
    else:
        mid = None
        mtype = None
        if message.photo:
            mid = message.photo[-1].file_id
            mtype = 'photo'
        elif message.video:
            mid = message.video.file_id
            mtype = 'video'

        if mid:
            await state.update_data(mail_media_id=mid, mail_media_type=mtype)

    await show_mailing_preview(message.chat.id, state, bot)


@router.callback_query(F.data == "mail_setup_btn")
async def mail_setup_btn(callback: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.set_state(AdminStates.mailing_content)
    await send_smart_content(callback, "🔘 <b>Настройка кнопки</b>\nВыберите действие:", None,
                             kb.mailing_buttons_type_keyboard())


@router.callback_query(F.data.in_({"mail_btn_clear", "mail_btn_clear_confirmed"}))
async def mail_btn_clear(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if callback.data != "mail_btn_clear_confirmed":
        await _ask_admin_confirmation(
            callback,
            "Кнопка из черновика рассылки будет удалена. Текст и аудитория рассылки сохранятся.",
            "mail_btn_clear_confirmed",
            "mail_setup_btn",
        )
        return
    await state.set_state(AdminStates.mailing_content)
    await state.update_data(mail_btn_text=None, mail_btn_action=None)
    await show_mailing_preview(callback.message.chat.id, state, bot)


@router.callback_query(F.data.startswith("mail_btn_preset_"))
async def mail_btn_preset(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(AdminStates.mailing_content)
    preset = callback.data.split("mail_btn_preset_")[1]

    text = ""
    action = ""

    if preset == "sub":
        text = "💳 Продлить доступ к программе"
        action = "pay_buy_subscription"
    elif preset == "topup":
        text = "💰 Пополнить баланс"
        action = "open_payment_menu"

    await state.update_data(mail_btn_text=text, mail_btn_action=action)
    await show_mailing_preview(callback.message.chat.id, state, bot)


@router.callback_query(F.data == "mail_btn_custom_link")
async def mail_btn_custom_link(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.mailing_btn_text)
    msg = await callback.message.answer(
        "📝 Введите текст для кнопки:",
        reply_markup=kb.back_to_previous_menu("mail_preview_back"),
    )
    await state.update_data(last_msg_id=msg.message_id)
    await callback.answer()


@router.message(AdminStates.mailing_btn_text, ~F.text.startswith('/'))
async def mail_save_btn_text(message: Message, state: FSMContext):
    await clean_chat_after_input(message, state)
    button_text = (message.text or "").strip()
    if not button_text:
        await message.answer("Текст кнопки не может быть пустым. Попробуйте ещё раз:")
        return
    await state.update_data(mail_btn_text=button_text)

    await state.set_state(AdminStates.mailing_btn_url)
    msg = await message.answer(
        "🔗 Введите ссылку (https://... или tg://...):",
        reply_markup=kb.back_to_previous_menu("mail_preview_back"),
    )
    await state.update_data(last_msg_id=msg.message_id)


@router.message(AdminStates.mailing_btn_url, ~F.text.startswith('/'))
async def mail_save_btn_url(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    url = message.text.strip()
    if not url.startswith("http") and not url.startswith("tg://"):
        msg = await message.answer(
            "❌ Ссылка должна начинаться с http://, https:// или tg://",
            reply_markup=kb.back_to_previous_menu("mail_preview_back"),
        )
        asyncio.create_task(send_temp_notification(bot, message.chat.id, "❌ Некорректная ссылка"))
        return

    await state.update_data(mail_btn_action=url)
    await show_mailing_preview(message.chat.id, state, bot)


@router.callback_query(F.data.in_({"mail_send_final", "mail_send_final_confirmed"}))
async def mail_send_final(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if callback.data != "mail_send_final_confirmed":
        await _ask_admin_confirmation(
            callback,
            "Рассылка будет поставлена в очередь для выбранной аудитории. Продолжить?",
            "mail_send_final_confirmed",
            "mail_preview_back",
        )
        return
    data = await state.get_data()

    text = data.get('mail_text')
    mid = data.get('mail_media_id')
    mtype = data.get('mail_media_type')
    btn_text = data.get('mail_btn_text')
    btn_action = data.get('mail_btn_action')

    audience = data.get('mail_audience', 'all')

    async with async_session_maker() as session:
        session.add(Mailing(
            text=text,
            media_file_id=mid,
            media_file_type=mtype,
            button_text=btn_text,
            button_action=btn_action,
            target_audience=audience
        ))
        await session.commit()

    from keyboards import AUDIENCE_LABELS
    label = AUDIENCE_LABELS.get(audience, audience)
    asyncio.create_task(send_temp_notification(bot, callback.message.chat.id, f"✅ Рассылка в очередь! Аудитория: {label}"))
    await state.clear()

    class FakeUpdate:
        def __init__(self, msg, data):
            self.message = msg
            self.data = data
            self.from_user = msg.from_user
            self.bot = msg.bot
            self.chat = msg.chat
            self.answer = callback.answer

    fake_cb = FakeUpdate(callback.message, "admin_section_automation")
    await admin_section_menu(fake_cb)


@router.callback_query(F.data == "mail_set_audience")
async def mail_set_audience(callback: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.set_state(AdminStates.mailing_content)
    await send_smart_content(
        callback,
        "🎯 <b>Кому отправить рассылку?</b>\n\n"
        "👥 <b>Все пользователи</b> — всем кто когда-либо запускал бота\n"
        "👤 <b>Только мне</b> — тестовая отправка только вам\n"
        "🟢 <b>Активные</b> — писали боту за последние 7 дней\n"
        "🔴 <b>Неактивные</b> — молчат больше 7 дней\n"
        "💳 <b>С подпиской</b> — у кого сейчас активная подписка\n"
        "🚫 <b>Без подписки</b> — подписка истекла или не оформлялась\n"
        "🌀 <b>Не прошли воронку</b> — начали, но не дошли до конца\n"
        "💰 <b>Оплатили с лендинга</b> — есть оплата через лендинг/GetCourse",
        None,
        kb.mailing_audience_keyboard()
    )


@router.callback_query(F.data.startswith("mail_audience_"))
async def mail_audience_selected(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(AdminStates.mailing_content)
    audience = callback.data.split("mail_audience_")[1]
    await state.update_data(mail_audience=audience)
    await callback.answer(f"Выбрано: {kb.AUDIENCE_LABELS.get(audience, audience)}")
    try:
        await callback.message.delete()
    except:
        pass
    await show_mailing_preview(callback.message.chat.id, state, bot)


@router.callback_query(F.data == "admin_ai_settings")
async def admin_ai_settings(event, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)

        provider = conf.provider
        or_model = getattr(conf, "openrouter_model", None) or "openai/gpt-5.6-terra"
        if provider == "OpenRouter":
            model_name = or_model
        else:
            model_name = getattr(conf, f"{provider.lower()}_model", None) or conf.openai_model

        oa_key = "✅ Установлен" if conf.openai_api_key else "❌ Не установлен"
        ds_key = "✅ Установлен" if conf.deepseek_api_key else "❌ Не установлен"
        kie_key = "✅ Установлен" if getattr(conf, "kie_api_key", None) else "❌ Не установлен"
        or_key = "✅ Установлен" if getattr(conf, "openrouter_api_key", None) else "❌ Не установлен"

    text = (
        f"🤖 <b>Настройки ИИ</b>\n\n"
        f"▫️ Текущий провайдер: <b>{provider}</b>\n"
        f"▫️ Активная модель: <code>{model_name}</code>\n\n"
        f"🔑 <b>Ключи:</b>\n"
        f"▫️ OpenAI: <b>{oa_key}</b>\n"
        f"▫️ DeepSeek: <b>{ds_key}</b>\n"
        f"▫️ KIE: <b>{kie_key}</b>\n"
        f"▫️ OpenRouter: <b>{or_key}</b>"
    )
    await send_smart_content(event, text, None, kb.ai_settings_keyboard(provider))


@router.callback_query(F.data.startswith("ai_provider_"))
async def set_ai_provider(callback: CallbackQuery, bot: Bot):
    provider = callback.data.split("_")[-1]
    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        conf.provider = provider
        await session.commit()
    await callback.answer(f"Провайдер изменен на {provider}")
    await admin_ai_settings(callback)


@router.callback_query(F.data == "admin_ai_keys")
async def admin_ai_keys(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)

    or_model = getattr(conf, "openrouter_model", None) or "openai/gpt-5.6-terra"
    text = (
        "⚙️ <b>Провайдеры и модели</b>\n\n"
        f"▫️ Модель OpenAI: <code>{conf.openai_model}</code>\n"
        f"▫️ Модель DeepSeek: <code>{conf.deepseek_model}</code>\n"
        f"▫️ Модель KIE: <code>{conf.kie_model}</code>\n"
        f"▫️ Модель OpenRouter: <code>{or_model}</code>\n"
        f"▫️ Vision KIE: <code>{conf.vision_model}</code>\n"
        f"▫️ Генерация KIE: <code>{conf.image_generation_model}</code>\n"
        f"▫️ Редактирование KIE: <code>{conf.image_edit_model}</code>\n"
        f"▫️ Транскрибация KIE: <code>{conf.kie_transcription_model}</code>\n"
        f"▫️ Сообщений из начала диалога: <b>{conf.context_limit_first}</b>\n"
        f"▫️ Последних сообщений: <b>{conf.context_limit_recent}</b>"
    )
    await send_smart_content(
        callback,
        text,
        None,
        kb.ai_keys_models_keyboard(
            conf.context_limit_first,
            conf.context_limit_recent,
            conf.openai_model,
            conf.deepseek_model,
            conf.kie_model,
            conf.vision_model,
            conf.image_generation_model,
            conf.image_edit_model,
            conf.kie_transcription_model,
            openrouter_model=or_model,
        ),
    )


# --- Настройка памяти ---

@router.callback_query(F.data == "set_mem_first")
async def set_mem_first_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.set_limit_first)
    await state.update_data(message_id_to_edit=callback.message.message_id)
    await callback.message.edit_text(
        "Введите количество <b>ПЕРВЫХ</b> сообщений, которые бот должен помнить всегда.\n\nОбычно: 2-5.",
        reply_markup=kb.back_to_previous_menu("admin_ai_keys"),
        parse_mode='HTML'
    )
    await callback.answer()


@router.message(AdminStates.set_limit_first, ~F.text.startswith('/'))
async def save_mem_first(message: Message, state: FSMContext, bot: Bot):
    try:
        val = int(message.text)
        if val < 0:
            raise ValueError
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.context_limit_first = val
            await session.commit()
    except ValueError:
        await message.answer("❌ Введите целое положительное число.")
        return

    data = await state.get_data()
    msg_id = data.get("message_id_to_edit")
    await state.clear()
    await message.delete()

    callback_mock = type("obj", (object,), {
        "message": type("obj", (object,), {
            "chat": message.chat,
            "bot": bot,
            "message_id": msg_id,
            "edit_text": lambda *args, **kwargs: bot.edit_message_text(chat_id=message.chat.id, message_id=msg_id, *args, **kwargs),
        }),
        "bot": bot,
    })()
    await admin_ai_keys(callback_mock)


@router.callback_query(F.data == "set_mem_recent")
async def set_mem_recent_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.set_limit_recent)
    await state.update_data(message_id_to_edit=callback.message.message_id)
    await callback.message.edit_text(
        "Введите количество <b>ПОСЛЕДНИХ</b> сообщений, которые бот должен помнить.\n\nОбычно: 10-20.",
        reply_markup=kb.back_to_previous_menu("admin_ai_keys"),
        parse_mode='HTML'
    )
    await callback.answer()


@router.message(AdminStates.set_limit_recent, ~F.text.startswith('/'))
async def save_mem_recent(message: Message, state: FSMContext, bot: Bot):
    try:
        val = int(message.text)
        if val < 0:
            raise ValueError
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.context_limit_recent = val
            await session.commit()
    except ValueError:
        await message.answer("❌ Введите целое положительное число.")
        return

    data = await state.get_data()
    msg_id = data.get("message_id_to_edit")
    await state.clear()
    await message.delete()

    callback_mock = type("obj", (object,), {
        "message": type("obj", (object,), {
            "chat": message.chat,
            "bot": bot,
            "message_id": msg_id,
            "edit_text": lambda *args, **kwargs: bot.edit_message_text(chat_id=message.chat.id, message_id=msg_id, *args, **kwargs),
        }),
        "bot": bot,
    })()
    await admin_ai_keys(callback_mock)


# -- Провайдеры --

@router.callback_query(F.data == "set_prov_deepseek")
async def set_provider_deepseek(callback: CallbackQuery):
    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        conf.provider = 'DeepSeek'
        await session.commit()
    await admin_ai_settings(callback)


@router.callback_query(F.data == "set_prov_openai")
async def set_provider_openai(callback: CallbackQuery):
    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        conf.provider = 'OpenAI'
        await session.commit()
    await admin_ai_settings(callback)


# -- Ключи --

@router.callback_query(F.data == "set_ai_apikey")
async def set_ai_apikey(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.set_ai_key)
    await state.update_data(message_id_to_edit=callback.message.message_id)
    await callback.message.edit_text(
        "Пожалуйста, отправьте новый API ключ для OpenAI.",
        reply_markup=kb.back_to_previous_menu("admin_ai_keys")
    )
    await callback.answer()


@router.message(AdminStates.set_ai_key, ~F.text.startswith('/'))
async def save_ai_key(message: Message, state: FSMContext, bot: Bot):
    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        conf.openai_api_key = message.text.strip()
        await session.commit()
    data = await state.get_data()
    msg_id = data.get("message_id_to_edit")
    await state.clear()
    await message.delete()
    callback_mock = type("obj", (object,), {
        "message": type("obj", (object,), {
            "chat": message.chat,
            "bot": bot,
            "message_id": msg_id,
            "edit_text": lambda *args, **kwargs: bot.edit_message_text(chat_id=message.chat.id, message_id=msg_id, *args, **kwargs),
        }),
        "bot": bot,
    })()
    await admin_ai_keys(callback_mock)


@router.callback_query(F.data == "set_ds_apikey")
async def set_ds_apikey(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.set_deepseek_key)
    await state.update_data(message_id_to_edit=callback.message.message_id)
    await callback.message.edit_text(
        "Пожалуйста, отправьте новый API ключ для DeepSeek.",
        reply_markup=kb.back_to_previous_menu("admin_ai_keys")
    )
    await callback.answer()


@router.callback_query(F.data == "set_kie_apikey")
async def set_kie_apikey(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.set_kie_key)
    await state.update_data(message_id_to_edit=callback.message.message_id)
    await callback.message.edit_text("Отправьте API ключ KIE.", reply_markup=kb.back_to_previous_menu("admin_ai_keys"))
    await callback.answer()


@router.message(AdminStates.set_kie_key, ~F.text.startswith('/'))
async def save_kie_key(message: Message, state: FSMContext, bot: Bot):
    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        conf.kie_api_key = message.text.strip()
        conf.kie_credit_alert_sent = False
        await session.commit()
    data = await state.get_data()
    await state.clear()
    await message.delete()
    callback_mock = type("obj", (object,), {"message": type("obj", (object,), {"chat": message.chat, "bot": bot, "message_id": data.get("message_id_to_edit"), "edit_text": lambda *args, **kwargs: bot.edit_message_text(chat_id=message.chat.id, message_id=data.get("message_id_to_edit"), *args, **kwargs)})(), "bot": bot})()
    await admin_ai_keys(callback_mock)


@router.message(AdminStates.set_deepseek_key, ~F.text.startswith('/'))
async def save_ds_key(message: Message, state: FSMContext, bot: Bot):
    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        conf.deepseek_api_key = message.text.strip()
        await session.commit()
    data = await state.get_data()
    msg_id = data.get("message_id_to_edit")
    await state.clear()
    await message.delete()
    callback_mock = type("obj", (object,), {
        "message": type("obj", (object,), {
            "chat": message.chat,
            "bot": bot,
            "message_id": msg_id,
            "edit_text": lambda *args, **kwargs: bot.edit_message_text(chat_id=message.chat.id, message_id=msg_id, *args, **kwargs),
        }),
        "bot": bot,
    })()
    await admin_ai_keys(callback_mock)


@router.callback_query(F.data == "set_openrouter_apikey")
async def set_openrouter_apikey(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.set_openrouter_key)
    await state.update_data(message_id_to_edit=callback.message.message_id)
    await callback.message.edit_text(
        "Пожалуйста, отправьте новый API ключ для OpenRouter.",
        reply_markup=kb.back_to_previous_menu("admin_ai_keys"),
        parse_mode='HTML',
    )
    await callback.answer()


@router.message(AdminStates.set_openrouter_key, ~F.text.startswith('/'))
async def save_openrouter_key(message: Message, state: FSMContext, bot: Bot):
    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        conf.openrouter_api_key = message.text.strip()
        await session.commit()
    data = await state.get_data()
    msg_id = data.get("message_id_to_edit")
    await state.clear()
    await message.delete()
    callback_mock = type("obj", (object,), {
        "message": type("obj", (object,), {
            "chat": message.chat,
            "bot": bot,
            "message_id": msg_id,
            "edit_text": lambda *args, **kwargs: bot.edit_message_text(chat_id=message.chat.id, message_id=msg_id, *args, **kwargs),
        }),
        "bot": bot,
    })()
    await admin_ai_keys(callback_mock)


@router.callback_query(F.data == "set_model_openrouter")
async def set_model_openrouter_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        cur_model = getattr(conf, "openrouter_model", None) or "openai/gpt-5.6-terra"

    await callback.message.edit_text(
        "📝 Выберите модель для <b>OpenRouter</b> из списка ниже:",
        reply_markup=kb.openrouter_models_keyboard(cur_model),
        parse_mode='HTML',
    )
    await callback.answer()


@router.callback_query(F.data.startswith("or_mod_"))
async def save_openrouter_model_cb(callback: CallbackQuery, bot: Bot):
    from openrouter_models import get_openrouter_model_by_index
    raw_idx = callback.data.removeprefix("or_mod_")
    try:
        idx = int(raw_idx)
    except (ValueError, TypeError):
        await callback.answer("Недопустимый параметр модели.", show_alert=True)
        return

    model_id = get_openrouter_model_by_index(idx)
    if not model_id:
        await callback.answer("Модель не найдена в каталоге.", show_alert=True)
        return

    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        conf.openrouter_model = model_id
        await session.commit()

    await callback.answer(f"Выбрана модель: {model_id}")
    await admin_ai_keys(callback)


# -- Модели --

@router.callback_query(F.data.startswith("set_model_"))
async def set_model_start(callback: CallbackQuery, state: FSMContext):
    target = callback.data.split("_")[2]
    await state.set_state(AdminStates.set_model_name)
    await state.update_data(target_provider=target, message_id_to_edit=callback.message.message_id)

    builder = InlineKeyboardBuilder()

    # Предлагаем популярные модели кнопками
    if target == 'openai':
        builder.button(text="gpt-4o", callback_data="quick_model_gpt-4o")
        builder.button(text="gpt-4o-mini", callback_data="quick_model_gpt-4o-mini")
    elif target == 'deepseek':
        builder.button(text="deepseek-chat", callback_data="quick_model_deepseek-chat")
        builder.button(text="deepseek-reasoner", callback_data="quick_model_deepseek-reasoner")
    else:
        for model in KIE_CHAT_MODELS:
            builder.button(text=model, callback_data=f"quick_model_{model}")

    builder.button(text="⬅️ Назад", callback_data="admin_ai_keys")
    builder.adjust(2, 1)

    await callback.message.edit_text(
        f"📝 Введите название модели для <b>{target.upper()}</b>\n"
        f"(Или выберите из списка ниже):",
        reply_markup=builder.as_markup(),
        parse_mode='HTML'
    )
    await callback.answer()


@router.callback_query(F.data.startswith("quick_model_"))
async def quick_set_model(callback: CallbackQuery, state: FSMContext, bot: Bot):
    model = callback.data.split("quick_model_")[1]
    data = await state.get_data()
    target = data.get('target_provider', 'openai')
    msg_id = data.get("message_id_to_edit")

    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        setattr(conf, f"{target}_model", model)
        await session.commit()

    await state.clear()
    callback_mock = type("obj", (object,), {
        "message": type("obj", (object,), {
            "chat": callback.message.chat,
            "bot": bot,
            "message_id": msg_id,
            "edit_text": lambda *args, **kwargs: bot.edit_message_text(chat_id=callback.message.chat.id, message_id=msg_id, *args, **kwargs),
        }),
        "bot": bot,
    })()
    await admin_ai_keys(callback_mock)


@router.message(AdminStates.set_model_name, ~F.text.startswith('/'))
async def save_model_name(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target = data.get('target_provider', 'openai')
    model = message.text.strip()
    msg_id = data.get("message_id_to_edit")

    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        setattr(conf, f"{target}_model", model)
        await session.commit()

    await state.clear()
    await message.delete()
    callback_mock = type("obj", (object,), {
        "message": type("obj", (object,), {
            "chat": message.chat,
            "bot": bot,
            "message_id": msg_id,
            "edit_text": lambda *args, **kwargs: bot.edit_message_text(chat_id=message.chat.id, message_id=msg_id, *args, **kwargs),
        }),
        "bot": bot,
    })()
    await admin_ai_keys(callback_mock)


async def _show_kie_model_picker(callback: CallbackQuery, title: str, field: str, models: tuple[str, ...]):
    builder = InlineKeyboardBuilder()
    for model in models:
        builder.button(text=model, callback_data=f"save_kie_field_{field}_{model}")
    builder.button(text="⬅️ Назад", callback_data="admin_ai_keys")
    builder.adjust(1)
    await callback.message.edit_text(title, reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data == "set_kie_vision_model")
async def set_kie_vision_model(callback: CallbackQuery):
    await _show_kie_model_picker(callback, "Выберите KIE-модель для анализа фото:", "vision_model", KIE_VISION_MODELS)


@router.callback_query(F.data == "set_kie_image_model")
async def set_kie_image_model(callback: CallbackQuery):
    await _show_kie_model_picker(callback, "Выберите KIE-модель для генерации изображений:", "image_generation_model", KIE_IMAGE_MODELS)


@router.callback_query(F.data == "set_kie_image_edit_model")
async def set_kie_image_edit_model(callback: CallbackQuery):
    await _show_kie_model_picker(callback, "Выберите KIE-модель для редактирования изображений:", "image_edit_model", KIE_IMAGE_EDIT_MODELS)


@router.callback_query(F.data == "set_kie_transcription_model")
async def set_kie_transcription_model(callback: CallbackQuery):
    await _show_kie_model_picker(callback, "Выберите KIE-модель для транскрибации:", "kie_transcription_model", KIE_TRANSCRIPTION_MODELS)


@router.callback_query(F.data.startswith("save_kie_field_"))
async def save_kie_field(callback: CallbackQuery):
    allowed = {"vision_model", "image_generation_model", "image_edit_model", "kie_transcription_model"}
    payload = callback.data.removeprefix("save_kie_field_")
    field = next((name for name in allowed if payload.startswith(f"{name}_")), None)
    model = payload[len(field) + 1:] if field else ""
    if field not in allowed or not model:
        await callback.answer("Некорректная настройка", show_alert=True)
        return
    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        setattr(conf, field, model)
        await session.commit()
    await callback.answer("Модель сохранена")
    await admin_ai_keys(callback)


@router.callback_query(F.data == "check_kie_credits")
async def check_kie_credits(callback: CallbackQuery):
    async with async_session_maker() as session:
        conf = await session.get(AIConfig, 1)
        key = conf.kie_api_key if conf else None
        base_url = conf.kie_base_url if conf else None
    if not key:
        await callback.answer("Ключ KIE не задан", show_alert=True)
        return
    try:
        credits = await remaining_credits(key, base_url or "https://api.kie.ai")
        await callback.answer(f"Остаток KIE: {credits:g}", show_alert=True)
    except Exception:
        logger.exception("KIE credits check failed")
        await callback.answer("Не удалось проверить кредиты KIE. Подробности в логах.", show_alert=True)


# --- ADMIN: Knowledge Base ---

@router.callback_query(F.data.startswith("admin_kb_page_"))
async def admin_kb_list(event, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    if isinstance(event, CallbackQuery):
        page = int(event.data.split("_")[-1])
    else:
        page = 0

    page_size = 10
    async with async_session_maker() as session:
        total = await session.scalar(select(func.count(KnowledgeBase.id))) or 0
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    page = max(0, min(page, total_pages - 1))

    async with async_session_maker() as session:
        files = await session.execute(
            select(KnowledgeBase).limit(page_size).offset(page * page_size).order_by(KnowledgeBase.id.desc())
        )
        files = files.scalars().all()

    builder = kb.InlineKeyboardBuilder()
    for f in files:
        builder.row(kb.InlineKeyboardButton(text=f"🗑 {f.filename}", callback_data=f"confirm_del_kb_{f.id}_{page}"))

    builder.row(*kb.standard_pagination_row(page, total_pages, lambda target: f"admin_kb_page_{target}"))
    builder.row(kb.InlineKeyboardButton(text="➕ Загрузить файл", callback_data="upload_kb_start"))
    builder.row(kb.InlineKeyboardButton(text="⬅️ В раздел ИИ и знаний", callback_data="admin_section_ai"))

    await send_smart_content(event, f"📚 База знаний ({total} файлов):", None, builder.as_markup())


@router.callback_query(F.data == "upload_kb_start")
async def upload_kb_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.upload_kb)

    # Кнопка назад ведет к списку файлов (страница 0)
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_kb_page_0")

    msg = await callback.message.answer("Пришлите файл (PDF, DOCX, TXT) для добавления в базу знаний:",
                                        reply_markup=builder.as_markup())
    await state.update_data(last_msg_id=msg.message_id)
    try:
        await callback.message.delete()
    except:
        pass


@router.message(AdminStates.upload_kb, F.document)
async def process_kb_upload(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    doc = message.document
    async with async_session_maker() as session:
        session.add(IndexingQueue(file_id=doc.file_id, filename=doc.file_name, uploader_id=message.from_user.id))
        await session.commit()

    asyncio.create_task(send_temp_notification(bot, message.chat.id, f"✅ Файл {doc.file_name} добавлен в очередь."))
    await state.clear()

    await admin_kb_list(message)


async def _execute_kb_file_delete(callback: CallbackQuery, state: FSMContext | None, fid: int, page: int):
    await _clear_admin_navigation_state(state)

    # Удаление из SQL
    async with async_session_maker() as session:
        await session.execute(text("DELETE FROM knowledge_base WHERE id = :fid"), {"fid": fid})
        await session.execute(text("DELETE FROM topic_kb_association WHERE knowledge_base_id = :fid"), {"fid": fid})
        await session.commit()

    # Удаление из векторной базы
    try:
        client = chromadb.PersistentClient(path="./chroma_db")
        col = client.get_or_create_collection(name="knowledge_base")
        col.delete(where={"file_id": fid})
    except Exception as e:
        vector_logger.error(f"Vector delete error: {e}")

    await callback.answer("Файл удален.")
    await admin_kb_list(callback)


@router.callback_query(F.data.startswith("del_kb_"))
async def delete_kb_file(callback: CallbackQuery, state: FSMContext | None = None):
    try:
        _, _, fid_raw, page_raw = callback.data.split("_")
        fid, page = int(fid_raw), int(page_raw)
    except (ValueError, IndexError):
        await callback.answer("Файл не найден.", show_alert=True)
        return
    if isinstance(callback, CallbackQuery):
        async with async_session_maker() as session:
            knowledge_file = await session.get(KnowledgeBase, fid)
        if not knowledge_file:
            await callback.answer("Файл уже удалён.", show_alert=True)
            return
        await _ask_admin_confirmation(
            callback,
            f"Файл «{html.escape(knowledge_file.filename)}» и его связи с темами будут удалены.",
            f"confirmed_del_kb_{fid}_{page}",
            f"admin_kb_page_{page}",
        )
        return
    await _execute_kb_file_delete(callback, state, fid, page)


@router.callback_query(F.data.startswith("confirmed_del_kb_"))
async def confirmed_delete_kb_file(callback: CallbackQuery, state: FSMContext | None = None):
    try:
        _, _, _, fid_raw, page_raw = callback.data.split("_")
        fid, page = int(fid_raw), int(page_raw)
    except (ValueError, IndexError):
        await callback.answer("Файл не найден.", show_alert=True)
        return
    await _execute_kb_file_delete(callback, state, fid, page)


@router.callback_query(F.data.startswith("confirm_del_kb_"))
async def confirm_delete_kb_file(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    parts = callback.data.split("_")
    try:
        file_id = int(parts[3])
        page = int(parts[4])
    except (IndexError, ValueError):
        await callback.answer("Файл не найден.", show_alert=True)
        return
    async with async_session_maker() as session:
        knowledge_file = await session.get(KnowledgeBase, file_id)
    if not knowledge_file:
        await callback.answer("Файл уже удалён.", show_alert=True)
        return
    await _ask_admin_confirmation(
        callback,
        f"Файл «{html.escape(knowledge_file.filename)}» и его связи с темами будут удалены.",
        f"confirmed_del_kb_{file_id}_{page}",
        f"admin_kb_page_{page}",
    )


# --- ADMIN: Referral Settings ---

@router.callback_query(F.data == "admin_referral_settings")
async def admin_referral_settings(event, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        ref_conf = await session.get(ReferralConfig, 1)
        sub_conf = await _ensure_subscription_config(session)

    status_str = "✅ Включена" if sub_conf.referral_enabled else "❌ Выключена"
    mode_str = "Баллы (1 балл = 1 руб)" if sub_conf.referral_reward_mode == "points" else "Бонусные дни"

    text = (
        "🤝 <b>Настройки реферальной программы</b>\n\n"
        f"• <b>Статус:</b> {status_str}\n"
        f"• <b>Режим вознаграждения:</b> {mode_str}\n"
        f"• <b>Баллы за регистрацию друга:</b> {ref_conf.points_per_invite}\n"
        f"• <b>% от первой оплаты:</b> {sub_conf.referral_first_pay_percent}%\n"
        f"• <b>% от повторных оплат:</b> {sub_conf.referral_recurring_pay_percent}%\n"
        f"• <b>Фиксированные бонусные дни:</b> {sub_conf.referral_pay_bonus_days} дн.\n\n"
        "Нажмите на нужный пункт для переключения или изменения значения:"
    )

    markup = kb.admin_referral_settings_keyboard(ref_conf, sub_conf)
    await send_smart_content(event, text, None, markup)


@router.callback_query(F.data == "admin_ref_toggle_enabled")
async def admin_ref_toggle_enabled(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with async_session_maker() as session:
        sub_conf = await _ensure_subscription_config(session)
        sub_conf.referral_enabled = not sub_conf.referral_enabled
        await session.commit()
    await admin_referral_settings(callback, state)
    await callback.answer("Статус реферальной программы изменен.")


@router.callback_query(F.data == "admin_ref_toggle_mode")
async def admin_ref_toggle_mode(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with async_session_maker() as session:
        sub_conf = await _ensure_subscription_config(session)
        sub_conf.referral_reward_mode = "bonus_days" if sub_conf.referral_reward_mode == "points" else "points"
        await session.commit()
    await admin_referral_settings(callback, state)
    await callback.answer("Режим вознаграждения изменен.")


@router.callback_query(F.data == "admin_ref_edit_reg_points")
async def admin_ref_edit_reg_points(callback: CallbackQuery, state: FSMContext):
    async with async_session_maker() as session:
        ref_conf = await session.get(ReferralConfig, 1)
        cur_val = ref_conf.points_per_invite

    await state.set_state(AdminStates.edit_ref_reg_points)
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Отмена", callback_data="admin_referral_settings")
    msg = await callback.message.answer(
        f"Текущее значение: <b>{cur_val}</b> баллов.\n\n"
        f"Введите новое количество баллов за регистрацию друга (целое число >= 0):",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await state.update_data(last_msg_id=msg.message_id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.message(AdminStates.edit_ref_reg_points, ~F.text.startswith('/'))
async def save_ref_reg_points(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    try:
        val = int(message.text.strip())
        if val < 0:
            raise ValueError
    except ValueError:
        asyncio.create_task(send_temp_notification(bot, message.chat.id, "❌ Введите целое неотрицательное число."))
        return

    async with async_session_maker() as session:
        ref_conf = await session.get(ReferralConfig, 1)
        ref_conf.points_per_invite = val
        await session.commit()

    await state.clear()
    asyncio.create_task(send_temp_notification(bot, message.chat.id, "✅ Баллы за регистрацию обновлены."))
    await admin_referral_settings(message, state)


@router.callback_query(F.data == "admin_ref_edit_first_percent")
async def admin_ref_edit_first_percent(callback: CallbackQuery, state: FSMContext):
    async with async_session_maker() as session:
        sub_conf = await _ensure_subscription_config(session)
        cur_val = sub_conf.referral_first_pay_percent

    await state.set_state(AdminStates.edit_ref_first_percent)
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Отмена", callback_data="admin_referral_settings")
    msg = await callback.message.answer(
        f"Текущее значение: <b>{cur_val}%</b>.\n\n"
        f"Введите процент вознаграждения от первой оплаты реферала (целое число от 0 до 100):",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await state.update_data(last_msg_id=msg.message_id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.message(AdminStates.edit_ref_first_percent, ~F.text.startswith('/'))
async def save_ref_first_percent(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    try:
        val = int(message.text.strip())
        if not (0 <= val <= 100):
            raise ValueError
    except ValueError:
        asyncio.create_task(send_temp_notification(bot, message.chat.id, "❌ Введите целое число от 0 до 100."))
        return

    async with async_session_maker() as session:
        sub_conf = await _ensure_subscription_config(session)
        sub_conf.referral_first_pay_percent = val
        await session.commit()

    await state.clear()
    asyncio.create_task(send_temp_notification(bot, message.chat.id, "✅ Процент от первой оплаты обновлен."))
    await admin_referral_settings(message, state)


@router.callback_query(F.data == "admin_ref_edit_recurring_percent")
async def admin_ref_edit_recurring_percent(callback: CallbackQuery, state: FSMContext):
    async with async_session_maker() as session:
        sub_conf = await _ensure_subscription_config(session)
        cur_val = sub_conf.referral_recurring_pay_percent

    await state.set_state(AdminStates.edit_ref_recurring_percent)
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Отмена", callback_data="admin_referral_settings")
    msg = await callback.message.answer(
        f"Текущее значение: <b>{cur_val}%</b>.\n\n"
        f"Введите процент вознаграждения от повторных оплат реферала (целое число от 0 до 100):",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await state.update_data(last_msg_id=msg.message_id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.message(AdminStates.edit_ref_recurring_percent, ~F.text.startswith('/'))
async def save_ref_recurring_percent(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    try:
        val = int(message.text.strip())
        if not (0 <= val <= 100):
            raise ValueError
    except ValueError:
        asyncio.create_task(send_temp_notification(bot, message.chat.id, "❌ Введите целое число от 0 до 100."))
        return

    async with async_session_maker() as session:
        sub_conf = await _ensure_subscription_config(session)
        sub_conf.referral_recurring_pay_percent = val
        await session.commit()

    await state.clear()
    asyncio.create_task(send_temp_notification(bot, message.chat.id, "✅ Процент от повторных оплат обновлен."))
    await admin_referral_settings(message, state)


@router.callback_query(F.data == "admin_ref_edit_bonus_days")
async def admin_ref_edit_bonus_days(callback: CallbackQuery, state: FSMContext):
    async with async_session_maker() as session:
        sub_conf = await _ensure_subscription_config(session)
        cur_val = sub_conf.referral_pay_bonus_days

    await state.set_state(AdminStates.edit_ref_bonus_days)
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Отмена", callback_data="admin_referral_settings")
    msg = await callback.message.answer(
        f"Текущее значение: <b>{cur_val}</b> дн.\n\n"
        f"Введите количество бонусных дней за оплату реферала (целое число >= 0):",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await state.update_data(last_msg_id=msg.message_id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.message(AdminStates.edit_ref_bonus_days, ~F.text.startswith('/'))
async def save_ref_bonus_days(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    try:
        val = int(message.text.strip())
        if val < 0:
            raise ValueError
    except ValueError:
        asyncio.create_task(send_temp_notification(bot, message.chat.id, "❌ Введите целое неотрицательное число."))
        return

    async with async_session_maker() as session:
        sub_conf = await _ensure_subscription_config(session)
        sub_conf.referral_pay_bonus_days = val
        await session.commit()

    await state.clear()
    asyncio.create_task(send_temp_notification(bot, message.chat.id, "✅ Бонусные дни обновлены."))
    await admin_referral_settings(message, state)



@router.callback_query(F.data == "admin_section_admins")
@router.callback_query(F.data == "admin_manage_admins")
@router.callback_query(F.data.startswith("admin_manage_admins_"))
async def admin_manage_admins(event, state: FSMContext | None = None):
    if not await require_permission(event, AdminPermission.ADMIN_MANAGEMENT):
        return
    await _clear_admin_navigation_state(state)
    if isinstance(event, CallbackQuery) and event.data.startswith("admin_manage_admins_"):
        page = int(event.data.split("_")[-1])
    else:
        page = 0
    page_size = 10
    async with async_session_maker() as session:
        admins = list((await session.execute(
            select(User).where(User.is_admin == True).order_by(User.id.asc())
        )).scalars().all())
        known_ids = {admin.id for admin in admins}
        for owner_id in OWNER_IDS:
            if owner_id not in known_ids:
                admins.append(User(id=owner_id, first_name="Владелец", is_admin=True))
        admins.sort(key=lambda admin: admin.id)

        total = len(admins)
        total_pages = max(1, math.ceil(total / page_size)) if total else 1
        page = max(0, min(page, total_pages - 1))
        admins = admins[page * page_size:(page + 1) * page_size]

    text = "👮\u200d♂️ <b>Администраторы:</b>\n\n"

    builder = InlineKeyboardBuilder()

    for a in admins:
        # Формируем красивую строку
        # Имя - ссылка на профиль (tg://user?id=...)
        # Юзернейм - просто текст @username (телеграм сам делает его ссылкой)
        first_name_display = a.first_name or a.username or f"User {a.id}"
        name_link = f"<a href='tg://user?id={a.id}'>{html.escape(first_name_display)}</a>"
        username_text = f"(@{a.username})" if a.username else ""
        current_role = AdminRole.OWNER if a.id in OWNER_IDS else getattr(a, "admin_role", None)

        text += f"- {name_link} {username_text} <code>{a.id}</code>: {html.escape(role_label(current_role))}\n"

        if a.id not in OWNER_IDS and a.id != event.from_user.id:
            builder.row(
                InlineKeyboardButton(
                    text=f"🎭 Изменить роль: {first_name_display}",
                    callback_data=f"admin_role_edit_{a.id}",
                )
            )
            builder.row(InlineKeyboardButton(
                text=f"🗑 Удалить: {first_name_display}",
                callback_data=f"confirm_del_admin_{a.id}",
            ))

    builder.row(*kb.standard_pagination_row(
        page,
        total_pages,
        lambda target: f"admin_manage_admins_{target}",
    ))
    builder.row(InlineKeyboardButton(text="➕ Добавить админа", callback_data="add_admin_start"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel"))

    await send_smart_content(event, text, None, builder.as_markup())


def _admin_role_keyboard(user_id: int):
    builder = InlineKeyboardBuilder()
    for role in (AdminRole.FULL_ADMIN, AdminRole.CONTENT_EDITOR, AdminRole.SUPPORT):
        builder.button(
            text=f"{role_label(role)}",
            callback_data=f"admin_role_set_{user_id}_{role.value}",
        )
    builder.button(text="⬅️ Назад", callback_data="admin_manage_admins")
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data.startswith("admin_role_edit_"))
async def edit_admin_role(callback: CallbackQuery):
    if not await require_permission(callback, AdminPermission.ADMIN_MANAGEMENT):
        return
    target_id = int(callback.data.removeprefix("admin_role_edit_"))
    if target_id in OWNER_IDS or target_id == callback.from_user.id:
        await callback.answer("❌ Нельзя менять права владельца или свои права.", show_alert=True)
        return

    async with async_session_maker() as session:
        user = await session.get(User, target_id)
    if not user or not user.is_admin:
        await callback.answer("❌ Администратор не найден.", show_alert=True)
        return

    await callback.message.edit_text(
        f"Выберите роль для пользователя <code>{target_id}</code>:\n"
        f"Текущая роль: <b>{html.escape(role_label(user.admin_role))}</b>",
        reply_markup=_admin_role_keyboard(target_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_role_set_"))
async def set_admin_role(callback: CallbackQuery):
    if not await require_permission(callback, AdminPermission.ADMIN_MANAGEMENT):
        return

    payload = callback.data.removeprefix("admin_role_set_")
    target_raw, separator, role_raw = payload.partition("_")
    if not separator:
        await callback.answer("❌ Некорректная роль.", show_alert=True)
        return
    try:
        target_id = int(target_raw)
        target_role = AdminRole(role_raw)
    except (TypeError, ValueError):
        await callback.answer("❌ Некорректная роль.", show_alert=True)
        return

    if target_id in OWNER_IDS or target_id == callback.from_user.id:
        await callback.answer("❌ Нельзя менять права владельца или свои права.", show_alert=True)
        return
    actor_role = await get_admin_role(callback.from_user.id)
    if target_role == AdminRole.OWNER:
        await callback.answer("❌ Роль владельца задаётся конфигурацией.", show_alert=True)
        return
    if target_role != AdminRole.FULL_ADMIN and actor_role != AdminRole.OWNER:
        await callback.answer("❌ Ограниченные роли назначает только владелец.", show_alert=True)
        return

    async with async_session_maker() as session:
        user = await session.get(User, target_id)
        if not user:
            await callback.answer("❌ Пользователь не найден.", show_alert=True)
            return
        user.is_admin = True
        user.admin_role = target_role.value
        await session.commit()

    await callback.answer(f"✅ Назначена роль: {role_label(target_role)}")
    await admin_manage_admins(callback)


async def _execute_admin_delete(callback: CallbackQuery, bot: Bot, user_id: int):
    if user_id in OWNER_IDS or user_id == callback.from_user.id:
        await callback.answer("❌ Нельзя удалить владельца или отозвать свои права.", show_alert=True)
        return

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user:
            user.is_admin = False
            user.admin_role = None
            await session.commit()

            # Сбрасываем меню команд у удаленного админа на обычное
            try:
                user_commands = [BotCommand(command="start", description="🚀 Перезапустить бота")]
                await bot.set_my_commands(user_commands, scope=BotCommandScopeChat(chat_id=user_id))
            except:
                pass

    await callback.answer("✅ Права администратора отозваны.")

    # Обновляем список
    await admin_manage_admins(callback)


@router.callback_query(F.data.startswith("del_admin_"))
async def delete_admin_handler(callback: CallbackQuery, bot: Bot):
    if not await require_permission(callback, AdminPermission.ADMIN_MANAGEMENT):
        return
    try:
        user_id = int(callback.data.removeprefix("del_admin_"))
    except ValueError:
        await callback.answer("Администратор не найден.", show_alert=True)
        return
    if isinstance(callback, CallbackQuery):
        async with async_session_maker() as session:
            user = await session.get(User, user_id)
        if not user or not user.is_admin:
            await callback.answer("Администратор уже удалён.", show_alert=True)
            return
        if user_id in OWNER_IDS or user_id == callback.from_user.id:
            await callback.answer("❌ Нельзя удалить владельца или отозвать свои права.", show_alert=True)
            return
        name = html.escape(user.first_name or user.username or str(user_id))
        await _ask_admin_confirmation(
            callback,
            f"Права администратора у пользователя «{name}» будут отозваны.",
            f"confirmed_del_admin_{user_id}",
            "admin_manage_admins",
        )
        return
    await _execute_admin_delete(callback, bot, user_id)


@router.callback_query(F.data.startswith("confirmed_del_admin_"))
async def confirmed_delete_admin(callback: CallbackQuery, bot: Bot):
    if not await require_permission(callback, AdminPermission.ADMIN_MANAGEMENT):
        return
    try:
        user_id = int(callback.data.removeprefix("confirmed_del_admin_"))
    except ValueError:
        await callback.answer("Администратор не найден.", show_alert=True)
        return
    await _execute_admin_delete(callback, bot, user_id)


@router.callback_query(F.data.startswith("confirm_del_admin_"))
async def confirm_delete_admin(callback: CallbackQuery):
    if not await require_permission(callback, AdminPermission.ADMIN_MANAGEMENT):
        return
    try:
        user_id = int(callback.data.removeprefix("confirm_del_admin_"))
    except ValueError:
        await callback.answer("Администратор не найден.", show_alert=True)
        return
    if user_id in OWNER_IDS or user_id == callback.from_user.id:
        await callback.answer("❌ Нельзя отозвать эти права.", show_alert=True)
        return
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
    if not user or not user.is_admin:
        await callback.answer("Администратор уже удалён.", show_alert=True)
        return
    name = html.escape(user.first_name or user.username or str(user_id))
    await _ask_admin_confirmation(
        callback,
        f"Права администратора у пользователя «{name}» будут отозваны.",
        f"confirmed_del_admin_{user_id}",
        "admin_manage_admins",
    )


@router.callback_query(F.data == "add_admin_start")
async def add_admin_start(callback: CallbackQuery, state: FSMContext):
    if not await require_permission(callback, AdminPermission.ADMIN_MANAGEMENT, state=state):
        return
    await state.set_state(AdminStates.add_admin)

    # Кнопка назад ведет в управление админами
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_manage_admins")

    msg = await callback.message.answer(
        "Пришлите Telegram ID пользователя, которого нужно назначить администратором.\n"
        "Например: 123456789",
                                        reply_markup=builder.as_markup())
    await state.update_data(last_msg_id=msg.message_id)
    try:
        await callback.message.delete()
    except:
        pass


@router.message(AdminStates.add_admin, ~F.text.startswith('/'))
async def add_admin_save(message: Message, state: FSMContext, bot: Bot):
    if not await require_permission(message, AdminPermission.ADMIN_MANAGEMENT, state=state):
        return
    await clean_chat_after_input(message, state)
    if (message.text or "").strip().lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await admin_manage_admins(message)
        return
    try:
        uid = int((message.text or "").strip())
        if uid == message.from_user.id or uid in OWNER_IDS:
            await state.clear()
            await message.answer("❌ Нельзя менять свои права или права владельца.")
            return
        async with async_session_maker() as session:
            user = await session.get(User, uid)
            if user:
                user.is_admin = True
                user.admin_role = AdminRole.FULL_ADMIN.value
                await session.commit()

                try:
                    admin_commands = [
                        BotCommand(command="start", description="🚀 Перезапустить бота"),
                        BotCommand(command="admin", description="⚙️ Админ-панель"),
                        BotCommand(command="help", description="📖 Инструкция по настройке")
                    ]
                    await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=uid))
                except Exception as e:
                    pass

                asyncio.create_task(send_temp_notification(bot, message.chat.id,
                                                           f"✅ Пользователь {uid} теперь админ (меню обновлено)."))
            else:
                asyncio.create_task(send_temp_notification(bot, message.chat.id,
                                                           "❌ Пользователь не найден (он должен сначала запустить бота)."))

        await state.clear()
        await admin_manage_admins(message)

    except ValueError:
        asyncio.create_task(send_temp_notification(bot, message.chat.id, "❌ Введите корректный ID (число)."))


# --- SMART MESSAGE SPLITTER ---

async def send_safe_response(chat_id, text, bot, reply_markup=None):
    """
    Конвертирует markdown AI-ответ в Telegram HTML, разбивает на части
    с корректным закрытием тегов и отправляет.
    """
    from md_utils import markdown_to_html, split_html_message, remove_markdown

    html_text = markdown_to_html(text)
    chunks = split_html_message(html_text, 4090)
    last_message = None

    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None

        try:
            last_message = await bot.send_message(chat_id, chunk, parse_mode='HTML', reply_markup=markup)
        except TelegramBadRequest:
            # HTML сломан — отправляем без разметки
            try:
                clean = remove_markdown(text)
                plain_chunks = split_html_message(clean, 4090)
                idx = min(i, len(plain_chunks) - 1)
                last_message = await bot.send_message(
                    chat_id,
                    plain_chunks[idx] if plain_chunks else chunk,
                    parse_mode=None,
                    reply_markup=markup,
                )
            except Exception as e:
                logger.error(f"Failed to send message chunk: {e}")
        except Exception as e:
            logger.error(f"General send error: {e}")

        await asyncio.sleep(0.3)

    return last_message


async def _clear_previous_ai_response_keyboard(state: FSMContext | None, bot: Bot, chat_id: int) -> None:
    if state is None:
        return
    try:
        data = await state.get_data()
    except Exception:
        return
    if not isinstance(data, dict):
        return
    message_id = data.get("ai_last_response_message_id")
    if not isinstance(message_id, int):
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.debug("Could not clear previous AI response keyboard: %s", exc)
    except Exception as exc:
        logger.debug("Could not clear previous AI response keyboard: %s", exc)


async def _remember_ai_response_message(state: FSMContext | None, message) -> None:
    if state is None:
        return
    message_id = getattr(message, "message_id", None)
    if isinstance(message_id, int):
        try:
            await state.update_data(ai_last_response_message_id=message_id)
        except Exception:
            pass


async def send_ai_response(
    chat_id: int,
    text: str,
    bot: Bot,
    state: FSMContext | None,
    reply_markup=None,
):
    await _clear_previous_ai_response_keyboard(state, bot, chat_id)
    crisis_detected_now = crisis_was_detected_in_turn(chat_id)
    crisis_active = False
    async with async_session_maker() as session:
        crisis_active = await is_crisis_active(session, chat_id)
    sent_message = await send_safe_response(
        chat_id,
        text,
        bot,
        None if crisis_active else reply_markup,
    )
    await _remember_ai_response_message(state, sent_message)
    if crisis_detected_now:
        await send_crisis_contacts_if_active(bot, chat_id, chat_id)
    return sent_message


@router.callback_query(F.data.startswith("menu_books"))
async def show_books(callback: CallbackQuery, bot: Bot):
    if not await is_menu_section_enabled("books"):
        await send_smart_content(callback, "Этот раздел сейчас недоступен.", None, await kb.extended_menu_keyboard(), bot)
        return
    page = 0
    if "_pg_" in callback.data:
        page = int(callback.data.split("_pg_")[1])

    content = await get_content("books_intro")
    text = content.text_content if content else "Библиотека книг"
    media = content.media if content else None

    reply_markup = await kb.books_keyboard(page)
    await send_smart_content(callback, text, media, reply_markup, bot)


# --- ADMIN: Habit Settings ---

@router.callback_query(F.data == "admin_habit_settings")
async def admin_habit_settings(event, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    await send_smart_content(
        event,
        "🧘 <b>Настройки раздела «Привычки»</b>\n\n"
        "Выберите режим для пользователей, шаблоны и правила наград.\n"
        "Текущие значения показываются внутри каждого раздела.",
        None,
                             await kb.admin_habit_keyboard())


@router.callback_query(F.data == "admin_toggle_habits_paid")
async def admin_toggle_habits_paid(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    await callback.answer("Раздел «Привычки» всегда бесплатный для всех пользователей.", show_alert=True)
    await admin_habit_settings(callback, state=state)


@router.callback_query(F.data == "admin_habit_modes")
async def admin_habit_modes(event, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        conf = await session.get(HabitConfig, 1)
        current_mode = conf.mode

    modes_desc = {
        "catalog": "📚 <b>Каталог</b> — клиент выбирает привычку из готовых шаблонов, которые вы добавили в админке. Свои привычки создавать нельзя.",
        "custom": "🛠 <b>Конструктор</b> — клиент сам придумывает и создаёт свои привычки. Готовые шаблоны не показываются.",
        "hybrid": "🚀 <b>Гибрид</b> — клиент видит готовые шаблоны и может создавать свои привычки. Оба варианта доступны."
    }

    text = (
        f"⚙️ <b>Режим привычек</b>\n\n"
        f"Этот параметр определяет, как клиенты работают с привычками:\n"
        f"могут ли они только выбирать из ваших шаблонов, "
        f"только создавать свои, или и то и другое.\n\n"
        f"Текущий режим:\n{modes_desc.get(current_mode, current_mode)}"
    )

    builder = InlineKeyboardBuilder()
    btn_cat = "✅ Каталог" if current_mode == 'catalog' else "📚 Каталог"
    btn_cus = "✅ Конструктор" if current_mode == 'custom' else "🛠 Конструктор"
    btn_hyb = "✅ Гибрид" if current_mode == 'hybrid' else "🚀 Гибрид"

    builder.button(text=btn_cat, callback_data="set_habit_mode_catalog")
    builder.button(text=btn_cus, callback_data="set_habit_mode_custom")
    builder.button(text=btn_hyb, callback_data="set_habit_mode_hybrid")
    builder.button(text="⬅️ Назад", callback_data="admin_habit_settings")
    builder.adjust(1)

    await send_smart_content(event, text, None, builder.as_markup())


@router.callback_query(F.data == "admin_habit_templates")
@router.callback_query(F.data.startswith("admin_habit_templates_"))
async def admin_habit_templates(event, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    if isinstance(event, CallbackQuery) and event.data.startswith("admin_habit_templates_"):
        page = int(event.data.split("_")[-1])
    else:
        page = 0
    page_size = 10
    async with async_session_maker() as session:
        total = await session.scalar(select(func.count(Habit.id)).where(Habit.user_id == None)) or 0
        total_pages = max(1, math.ceil(total / page_size)) if total else 1
        page = max(0, min(page, total_pages - 1))
        templates = await session.execute(select(Habit).where(Habit.user_id == None).order_by(Habit.id.asc()).limit(page_size).offset(page * page_size))
        templates = templates.scalars().all()

    text = (
        f"📚 <b>Шаблоны привычек ({total}):</b>\n"
        "Готовые варианты, которые пользователи увидят в каталоге.\n"
        "Удаление потребует подтверждения."
    )
    builder = InlineKeyboardBuilder()

    for t in templates:
        builder.row(InlineKeyboardButton(text=f"🗑 {t.name}", callback_data=f"confirm_adm_del_tpl_{t.id}"))

    builder.row(*kb.standard_pagination_row(
        page,
        total_pages,
        lambda target: f"admin_habit_templates_{target}",
    ))
    builder.row(InlineKeyboardButton(text="➕ Добавить шаблон", callback_data="adm_add_tpl_start"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_habit_settings"))

    await send_smart_content(event, text, None, builder.as_markup())


@router.callback_query(F.data == "adm_add_tpl_start")
async def adm_add_tpl_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.add_habit_template)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_habit_templates")

    await callback.message.answer("📝 Введите название нового шаблона привычки:", reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("set_habit_mode_"))
async def set_habit_mode_handler(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    mode = callback.data.split("set_habit_mode_")[1]
    async with async_session_maker() as session:
        conf = await session.get(HabitConfig, 1)
        conf.mode = mode
        await session.commit()

    await admin_habit_modes(callback, state=state)


@router.message(AdminStates.add_habit_template, ~F.text.startswith('/'))
async def adm_save_new_template(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    name = message.text.strip()

    async with async_session_maker() as session:
        session.add(Habit(name=name, user_id=None))
        await session.commit()

    asyncio.create_task(send_temp_notification(bot, message.chat.id, f"✅ Шаблон «{name}» добавлен!"))
    await state.clear()
    await admin_habit_templates(message)


async def _execute_template_delete(callback: CallbackQuery, state: FSMContext | None, tid: int):
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        tpl = await session.get(Habit, tid)
        if tpl and tpl.user_id is None:
            await session.delete(tpl)
            await session.commit()

    await callback.answer("Шаблон удален")
    await admin_habit_templates(callback)


@router.callback_query(F.data.startswith("adm_del_tpl_"))
async def adm_delete_template(callback: CallbackQuery, state: FSMContext | None = None):
    try:
        tid = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    if isinstance(callback, CallbackQuery):
        async with async_session_maker() as session:
            template = await session.get(Habit, tid)
        if not template or template.user_id is not None:
            await callback.answer("Шаблон уже удалён.", show_alert=True)
            return
        await _ask_admin_confirmation(
            callback,
            f"Шаблон «{html.escape(template.name)}» будет удалён из каталога.",
            f"confirmed_adm_del_tpl_{tid}",
            "admin_habit_templates",
        )
        return
    await _execute_template_delete(callback, state, tid)


@router.callback_query(F.data.startswith("confirmed_adm_del_tpl_"))
async def confirmed_delete_template(callback: CallbackQuery, state: FSMContext | None = None):
    try:
        tid = int(callback.data.removeprefix("confirmed_adm_del_tpl_"))
    except ValueError:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    await _execute_template_delete(callback, state, tid)


@router.callback_query(F.data.startswith("confirm_adm_del_tpl_"))
async def confirm_delete_template(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    try:
        template_id = int(callback.data.removeprefix("confirm_adm_del_tpl_"))
    except ValueError:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    async with async_session_maker() as session:
        template = await session.get(Habit, template_id)
    if not template or template.user_id is not None:
        await callback.answer("Шаблон уже удалён.", show_alert=True)
        return
    await _ask_admin_confirmation(
        callback,
        f"Шаблон «{html.escape(template.name)}» будет удалён из каталога.",
        f"confirmed_adm_del_tpl_{template_id}",
        "admin_habit_templates",
    )


@router.callback_query(F.data == "admin_habit_game")
async def admin_habit_game(event, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        conf = await session.get(HabitConfig, 1)

    text = (
        f"🏆 <b>Настройки геймификации</b>\n\n"
        f"🗓 <b>Дней в цикле:</b> {conf.streak_days}\n"
        f"🎁 <b>Награды:</b> {html.escape(conf.reward_sequence or '')}\n"
        f"⏳ <b>Окно выполнения:</b> {conf.time_window_minutes} мин.\n"
        f"🎉 <b>Сообщение победы:</b>\n<i>{html.escape(conf.final_message or '')[:100]}...</i>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить кол-во дней", callback_data="set_game_days")
    builder.button(text="✏️ Изменить награды (буквы)", callback_data="set_game_rewards")
    builder.button(text="✏️ Изменить время (минуты)", callback_data="set_game_window")
    builder.button(text="✏️ Изменить текст победы", callback_data="set_game_msg")
    builder.button(text="⬅️ Назад", callback_data="admin_habit_settings")
    builder.adjust(1)

    await send_smart_content(event, text, None, builder.as_markup())


# Хендлеры настроек геймификации

@router.callback_query(F.data == "set_game_days")
async def set_game_days_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.set_game_days)
    await callback.message.answer(
        "Введите количество дней для полного цикла.\n"
        "Пример: 7. Допустимо целое число от 1 и выше.",
        reply_markup=kb.back_to_previous_menu("admin_habit_game"),
    )
    await callback.answer()


@router.message(AdminStates.set_game_days, ~F.text.startswith('/'))
async def set_game_days_save(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    try:
        val = int(message.text)
        if val < 1:
            raise ValueError
        async with async_session_maker() as session:
            conf = await session.get(HabitConfig, 1)
            conf.streak_days = val
            await session.commit()
        await state.clear()
        await admin_habit_game(message)
    except:
        await message.answer("Нужно число.")


@router.callback_query(F.data == "set_game_rewards")
async def set_game_rewards_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.set_game_rewards)
    await callback.message.answer(
        "Введите награды через запятую.\n"
        "Пример: M,O,L,O,D,E,C. Количество элементов должно совпадать с числом дней.",
        reply_markup=kb.back_to_previous_menu("admin_habit_game"),
    )
    await callback.answer()


@router.message(AdminStates.set_game_rewards, ~F.text.startswith('/'))
async def set_game_rewards_save(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    async with async_session_maker() as session:
        conf = await session.get(HabitConfig, 1)
        conf.reward_sequence = message.text.strip()
        await session.commit()
    await state.clear()
    await admin_habit_game(message)


@router.callback_query(F.data == "set_game_window")
async def set_game_window_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.set_game_window)
    await callback.message.answer(
        "Введите время на выполнение после напоминания в минутах.\n"
        "Пример: 60. Допустимо целое число от 1 и выше.",
        reply_markup=kb.back_to_previous_menu("admin_habit_game"),
    )
    await callback.answer()


@router.message(AdminStates.set_game_window, ~F.text.startswith('/'))
async def set_game_window_save(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    try:
        val = int(message.text)
        if val < 1:
            raise ValueError
        async with async_session_maker() as session:
            conf = await session.get(HabitConfig, 1)
            conf.time_window_minutes = val
            await session.commit()
        await state.clear()
        await admin_habit_game(message)
    except:
        await message.answer("Нужно число.")


@router.callback_query(F.data == "set_game_msg")
async def set_game_msg_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.set_game_msg)
    await callback.message.answer(
        "Введите текст поздравления при завершении цикла.",
        reply_markup=kb.back_to_previous_menu("admin_habit_game"),
    )
    await callback.answer()


@router.message(AdminStates.set_game_msg, ~F.text.startswith('/'))
async def set_game_msg_save(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    if not (message.html_text or message.text or "").strip():
        await message.answer("Текст не может быть пустым. Попробуйте ещё раз:")
        return
    async with async_session_maker() as session:
        conf = await session.get(HabitConfig, 1)
        conf.final_message = message.html_text or message.text
        await session.commit()
    await state.clear()
    await admin_habit_game(message)


@router.callback_query(F.data == "admin_section_payments")
@router.callback_query(F.data == "admin_payment_settings")
async def admin_payment_settings(event, state: FSMContext | None = None):
    if not await require_permission(event, AdminPermission.PAYMENTS, state=state):
        return
    await _clear_admin_navigation_state(state)
    if state is not None:
        await state.update_data(admin_payment_stats_return="admin_payment_settings")
    async with async_session_maker() as session:
        conf = await session.get(PaymentConfig, 1)
        mode = conf.mode

    mode_label = {
        "free": "Бесплатный",
        "subscription": "По подписке",
        "token": "Оплата за сообщения",
    }.get(mode, mode)
    text = (
        f"💳 <b>Подписки и оплата</b>\n\n"
        f"Текущий режим: <b>{mode_label}</b>\n\n"
        f"<b>Бесплатный</b> - доступ открыт всем.\n"
        f"<b>По подписке</b> - доступ через тарифы и оплату.\n"
        f"<b>Оплата за сообщения</b> - расходуются токены за ответы ИИ.\n\n"
        f"Ниже собраны все экраны управления подписками и оплатой: тарифы, промокоды, подписчики, платежи, данные Prodamus, токены и лендинг."
    )
    await send_smart_content(event, text, None, kb.admin_payment_keyboard())


@router.callback_query(F.data.startswith("admin_pay_mode"))
async def admin_pay_mode(callback: CallbackQuery, state: FSMContext | None = None):
    return_callback = _admin_payment_action_source(callback.data)
    await _clear_admin_navigation_state(state)
    if state is not None:
        await state.update_data(admin_payment_return_callback=return_callback)
    async with async_session_maker() as session:
        conf = await session.get(PaymentConfig, 1)
    mode_labels = {
        "free": "Бесплатный",
        "subscription": "По подписке",
        "token": "Оплата за сообщения",
    }
    await send_smart_content(
        callback,
        "⚙️ <b>Режим оплаты</b>\n\n"
        f"Сейчас выбран: <b>{mode_labels.get(conf.mode, conf.mode)}</b>.\n"
        "Смена режима влияет на способ доступа пользователей и требует подтверждения.",
        None,
        kb.admin_pay_mode_keyboard(conf.mode, return_callback=return_callback),
    )


@router.callback_query(F.data.startswith("confirm_set_pay_mode_"))
async def confirm_set_pay_mode(callback: CallbackQuery, state: FSMContext | None = None):
    raw_mode = callback.data.removeprefix("confirm_set_pay_mode_")
    mode, separator, source = raw_mode.partition("__")
    return_callback = _normalize_admin_payment_return(source if separator else None)
    await _clear_admin_navigation_state(state)
    if state is not None:
        await state.update_data(admin_payment_return_callback=return_callback)
    labels = {"free": "Бесплатный", "subscription": "По подписке", "token": "Оплата за сообщения"}
    if mode not in labels:
        await callback.answer("Режим не найден.", show_alert=True)
        return
    await _ask_admin_confirmation(
        callback,
        f"Переключить режим оплаты на «{labels[mode]}»? Это изменит доступ и способ оплаты для пользователей.",
        f"set_pay_mode_{mode}__{return_callback}",
        f"admin_pay_mode__{return_callback}",
    )


@router.callback_query(F.data.startswith("set_pay_mode_"))
async def set_pay_mode(callback: CallbackQuery, state: FSMContext | None = None):
    raw_mode = callback.data.removeprefix("set_pay_mode_")
    mode, separator, source = raw_mode.partition("__")
    return_callback = _normalize_admin_payment_return(source if separator else None)
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        conf = await session.get(PaymentConfig, 1)
        conf.mode = mode
        await session.commit()
    labels = {"free": "Бесплатный", "subscription": "По подписке", "token": "Оплата за сообщения"}
    await callback.answer(f"Режим изменён: {labels.get(mode, mode)}")
    await _render_admin_payment_return(callback, state, return_callback)


@router.callback_query(F.data == "admin_pay_demo")
async def admin_pay_demo(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    await callback.answer()
    await admin_prodamus_settings(callback)


@router.callback_query(F.data == "admin_pay_demo_edit")
async def admin_pay_demo_edit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await admin_set_welcome_bonus(callback, state)


@router.callback_query(F.data == "admin_pay_providers")
@router.callback_query(F.data.startswith("admin_pay_providers_"))
async def admin_pay_providers(
    event,
    state: FSMContext | None = None,
    requested_page: int | None = None,
):
    event_data = getattr(event, "data", None)
    state_return = await _admin_payment_state_return(state)
    return_callback = _admin_payment_action_source(event_data, state_return)
    base_data = event_data.split("__", 1)[0] if isinstance(event_data, str) else event_data
    await _clear_admin_navigation_state(state)
    if state is not None:
        await state.update_data(admin_payment_return_callback=return_callback)
    if requested_page is not None:
        page = requested_page
    elif isinstance(event, CallbackQuery) and isinstance(base_data, str) and base_data.startswith("admin_pay_providers_"):
        page = int(base_data.removeprefix("admin_pay_providers_") or 0)
    else:
        page = 0
    page_size = 10
    async with async_session_maker() as session:
        total = await session.scalar(select(func.count(PaymentProvider.id))) or 0
        total_pages = max(1, math.ceil(total / page_size)) if total else 1
        page = max(0, min(page, total_pages - 1))
        providers = await session.execute(select(PaymentProvider).order_by(PaymentProvider.id.asc()).limit(page_size).offset(page * page_size))
        providers = providers.scalars().all()

    builder = InlineKeyboardBuilder()
    for p in providers:
        status = "✅" if p.is_active else "❌"
        builder.row(InlineKeyboardButton(
            text=f"{status} {p.name}",
            callback_data=_admin_payment_action_callback(
                f"toggle_prov_{p.id}_page_{page}",
                return_callback,
            ),
        ))

    builder.row(*kb.standard_pagination_row(
        page,
        total_pages,
        lambda target: _admin_payment_action_callback(f"admin_pay_providers_{target}", return_callback),
    ))
    builder.row(InlineKeyboardButton(
        text="➕ Добавить провайдера",
        callback_data=_admin_payment_action_callback(
            f"add_prov_start_page_{page}",
            return_callback,
        ),
    ))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=return_callback))

    if state is not None:
        await state.update_data(
            admin_payment_return_callback=return_callback,
            admin_payment_resume_callback=_admin_payment_action_callback(
                f"admin_pay_providers_{page}",
                return_callback,
            ),
        )

    await send_smart_content(
        event,
        "🏦 <b>Способы оплаты в Telegram</b>\n\n"
        "Добавьте токен провайдера из BotFather, затем включите нужный способ кнопкой.\n"
        "✅ означает, что способ доступен пользователям.",
        None,
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("add_prov_start"))
async def add_prov_start(callback: CallbackQuery, state: FSMContext):
    base_data = callback.data.split("__", 1)[0]
    page_match = re.fullmatch(r"add_prov_start_page_(\d+)", base_data)
    requested_page = int(page_match.group(1)) if page_match else 0
    return_callback = _admin_payment_action_source(
        callback.data,
        await _admin_payment_state_return(state, _ADMIN_PAYMENT_MAIN_CALLBACK),
    )
    resume_callback = await _admin_payment_state_resume(state, return_callback)
    if resume_callback is None:
        resume_action = (
            f"admin_pay_providers_{requested_page}"
            if page_match
            else "admin_pay_providers"
        )
        resume_callback = _admin_payment_action_callback(
            resume_action,
            return_callback,
        )
    await state.clear()
    await state.set_state(PaymentStates.add_provider_name)
    await state.update_data(
        admin_payment_return_callback=return_callback,
        admin_payment_resume_callback=resume_callback,
    )
    await callback.message.answer(
        "Введите название способа оплаты.\nПример: YooKassa.",
        reply_markup=kb.back_to_previous_menu(resume_callback),
    )
    await callback.answer()


@router.message(PaymentStates.add_provider_name, ~F.text.startswith('/'))
async def add_prov_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте ещё раз:")
        return
    await state.update_data(name=name)
    await state.set_state(PaymentStates.add_provider_token)
    await message.answer(
        "Теперь введите токен оплаты от BotFather для этого способа:",
        reply_markup=kb.back_to_previous_menu(
            await _admin_payment_state_resume(state)
            or _admin_payment_action_callback(
                "admin_pay_providers_0",
                await _admin_payment_state_return(state),
            )
        ),
    )


@router.message(PaymentStates.add_provider_token, ~F.text.startswith('/'))
async def add_prov_token(message: Message, state: FSMContext, bot: Bot):
    token = (message.text or "").strip()
    if not token:
        await message.answer("Токен не может быть пустым. Попробуйте ещё раз:")
        return
    data = await state.get_data()
    async with async_session_maker() as session:
        session.add(PaymentProvider(name=data['name'], provider_token=token))
        await session.commit()

    return_callback = await _admin_payment_state_return(state)
    resume_callback = await _admin_payment_state_resume(state, return_callback)
    await state.clear()
    await state.update_data(
        admin_payment_return_callback=return_callback,
        admin_payment_resume_callback=resume_callback,
    )
    await message.answer("✅ Провайдер добавлен.")
    await _render_admin_payment_resume(message, state, resume_callback, return_callback)


@router.callback_query(F.data.startswith("toggle_prov_"))
async def toggle_provider(callback: CallbackQuery, state: FSMContext | None = None):
    base_data, _, _ = callback.data.partition("__")
    provider_payload = base_data.removeprefix("toggle_prov_")
    provider_id_raw, _, page_raw = provider_payload.partition("_page_")
    try:
        provider_id = int(provider_id_raw)
        requested_page = int(page_raw) if page_raw else 0
    except ValueError:
        await callback.answer("Способ оплаты не найден.", show_alert=True)
        return
    return_callback = await _admin_payment_state_return(
        state,
        _admin_payment_action_source(callback.data),
    )
    resume_callback = await _admin_payment_state_resume(state, return_callback)
    if resume_callback is None:
        resume_callback = _admin_payment_action_callback(
            f"admin_pay_providers_{requested_page}",
            return_callback,
        )
    await _clear_admin_navigation_state(state)
    return_callback = _admin_payment_action_source(callback.data, return_callback)
    async with async_session_maker() as session:
        p = await session.get(PaymentProvider, provider_id)
        if p is None:
            await callback.answer("Способ оплаты не найден.", show_alert=True)
            return
        p.is_active = not p.is_active
        await session.commit()
    if state is not None:
        await state.update_data(
            admin_payment_return_callback=return_callback,
            admin_payment_resume_callback=resume_callback,
        )
    await _render_admin_payment_resume(callback, state, resume_callback, return_callback)


@router.callback_query(F.data.startswith("admin_pay_prices"))
async def admin_pay_prices(event, state: FSMContext | None = None):
    state_return = await _admin_payment_state_return(state)
    return_callback = _admin_payment_action_source(getattr(event, "data", None), state_return)
    await _clear_admin_navigation_state(state)
    if state is not None:
        await state.update_data(
            admin_payment_return_callback=return_callback,
            admin_payment_resume_callback=_admin_payment_action_callback(
                "admin_pay_prices",
                return_callback,
            ),
        )
    async with async_session_maker() as session:
        conf = await session.get(PaymentConfig, 1)

    text = (
        f"💰 <b>Баланс и стоимость сообщений</b>\n\n"
        f"Списывается за один ответ ИИ: <b>{conf.cost_per_message} токенов</b>\n"
        f"Пополнение: 1 рубль = <b>{conf.currency_ratio} токенов</b>\n\n"
        "Эти значения используются только в режиме «Оплата за сообщения»."
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text="✏️ Стоимость одного ответа",
        callback_data=_admin_payment_action_callback("edit_msg_cost", return_callback),
    )
    builder.button(text="⬅️ Назад", callback_data=return_callback)
    builder.adjust(1)

    await send_smart_content(event, text, None, builder.as_markup())


@router.callback_query(F.data == "edit_sub_price")
async def edit_sub_price(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Стоимость доступа к образовательной программе теперь настраивается в тарифах.", show_alert=True)
    await admin_subscriptions_menu(callback)


@router.callback_query(F.data.startswith("edit_msg_cost"))
async def edit_msg_cost(callback: CallbackQuery, state: FSMContext):
    return_callback = _admin_payment_action_source(callback.data)
    resume_callback = await _admin_payment_state_resume(state, return_callback)
    if resume_callback is None:
        resume_callback = _admin_payment_action_callback("admin_pay_prices", return_callback)
    await state.clear()
    await state.set_state(PaymentStates.set_token_price)
    await state.update_data(
        admin_payment_return_callback=return_callback,
        admin_payment_resume_callback=resume_callback,
    )
    await callback.message.answer(
        "Сколько токенов списывать за 1 сообщение ИИ?\n"
        "Пример: 1 или 0.5. Значение должно быть неотрицательным.",
        reply_markup=kb.back_to_previous_menu(resume_callback),
    )
    await callback.answer()


@router.message(PaymentStates.set_token_price, ~F.text.startswith('/'))
async def save_token_price(message: Message, state: FSMContext):
    try:
        val = float(message.text)
        if val < 0:
            raise ValueError
        async with async_session_maker() as session:
            conf = await session.get(PaymentConfig, 1)
            conf.cost_per_message = val
            await session.commit()
        return_callback = await _admin_payment_state_return(state)
        resume_callback = await _admin_payment_state_resume(state, return_callback)
        await state.clear()
        await state.update_data(
            admin_payment_return_callback=return_callback,
            admin_payment_resume_callback=resume_callback,
        )
        await _render_admin_payment_resume(message, state, resume_callback, return_callback)
    except:
        await message.answer("Введите число.")


@router.callback_query(F.data == "open_payment_menu")
async def open_payment_menu(callback: CallbackQuery, bot: Bot):
    async with async_session_maker() as session:
        user = await session.get(User, callback.from_user.id)
        conf = await session.get(PaymentConfig, 1)
        mode = conf.mode
        if mode == 'subscription':
            await _send_subscription_info(callback.from_user.id, callback.message.chat.id, bot)
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.answer()
            return

        # ВАЖНО: Загружаем контент через единый content_service
        sub_info_content = await cs_get_content("subscription_info")
        sub_info_text = sub_info_content.text_content if sub_info_content and sub_info_content.text_content else "Подписка на образовательную программу даёт доступ ко всем функциям."
        # Получаем медиа объект, если он есть
        media = sub_info_content.media if sub_info_content else None

    # Формируем итоговый текст сообщения из редактируемого контента
    text = sub_info_text

    if mode == 'token':
        text += (
            f"\n\nБаланс токенов: <b>{user.balance if user else 0}</b>\n"
            f"Стоимость 1 сообщения: {conf.cost_per_message} токенов."
        )
    # ВАЖНО: Передаем media в функцию отправки
    await send_smart_content(
        callback,
        text,
        media,
        await kb.configured_payment_main_keyboard(mode, user.balance, user.subscription_end),
        bot,
    )


@router.callback_query(F.data == "show_subscription_info")
async def show_subscription_info(callback: CallbackQuery, bot: Bot):
    await _send_subscription_info(callback.from_user.id, callback.message.chat.id, bot)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "show_subscription_info_from_chat")
async def show_subscription_info_from_chat(callback: CallbackQuery, bot: Bot):
    await _send_subscription_info(callback.from_user.id, callback.message.chat.id, bot)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "sub_select_plan")
async def show_subscription_plans(callback: CallbackQuery):
    async with async_session_maker() as session:
        user = await session.get(
            User,
            callback.from_user.id,
            options=[
                selectinload(User.subscription),
                selectinload(User.promo_codes).selectinload(PromoCode.applicable_plans),
            ]
        )
        is_admin_user = callback.from_user.id in OWNER_IDS or bool(user and user.is_admin)
        discount = _calculate_effective_discount(user.subscription if user else None, list(user.promo_codes or []), None) if user else 0

    await send_smart_content(
        callback,
        "👇 <b>Выбери тариф образовательной программы:</b>",
        None,
        await kb.user_plans_keyboard(
            user_promos=list(user.promo_codes or []) if user else None,
            current_discount=discount,
            is_admin=is_admin_user,
        )
    )


@router.callback_query(F.data == "pay_buy_subscription")
async def pay_buy_subscription(callback: CallbackQuery):
    await show_subscription_plans(callback)


@router.callback_query(F.data.startswith("select_plan_"))
async def process_plan_selection(callback: CallbackQuery):
    plan_id = int(callback.data.split("_")[-1])

    async with async_session_maker() as session:
        plan = await session.get(
            SubscriptionPlan,
            plan_id,
            options=[selectinload(SubscriptionPlan.upgrades_to_plan)]
        )
        if not plan:
            await callback.answer("Тариф недоступен", show_alert=True)
            return

        user = await session.get(
            User,
            callback.from_user.id,
            options=[
                selectinload(User.subscription),
                selectinload(User.promo_codes).selectinload(PromoCode.applicable_plans),
            ]
        )

        # Cancel any previous uncompleted pending choice intents for this user to release reservations
        # DO NOT cancel intents already handed to providers (telegram_pay or prodamus)
        await session.execute(
            update(PaymentIntent)
            .where(
                PaymentIntent.user_id == user.id,
                PaymentIntent.status == "pending",
                PaymentIntent.provider.in_(["checkout", "pending_choice"]),
            )
            .values(status="cancelled")
        )
        await session.commit()

        intent = await create_payment_intent(session, callback.from_user.id, plan.id, provider="pending_choice", bot=callback.bot, notify_user=False)
        final_price = intent.final_amount
        duration_text = _duration_text(plan)

        if intent.status == "paid" or final_price <= 0.0:
            builder = InlineKeyboardBuilder()
            builder.button(text="⬅️ В главное меню", callback_data="back_to_main_menu_text")
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user.id))
            end_date_str = format_msk(sub.end_date, "%d.%m.%Y %H:%M") if (sub and sub.end_date) else ""
            success_text = (
                f"🎉 <b>Доступ к образовательной программе успешно активирован!</b>\n\n"
                f"Тариф: <b>{plan.name}</b>\n"
                f"Оплачено баллами: <b>{intent.points_reserved}</b>\n"
                f"К оплате: <b>0.00 RUB</b>\n"
            )
            if end_date_str:
                success_text += f"Действует до: <b>{end_date_str} МСК</b>.\n"
            success_text += "\nПриятного пользования образовательной программой!"
            await send_smart_content(callback, success_text, None, builder.as_markup())
            return

        text = (
            f"🎫 <b>Выбран тариф образовательной программы: {plan.name}</b>\n"
            f"💰 Стоимость: {final_price:.2f} RUB\n"
            f"🗓 Срок: {duration_text}\n\n"
            f"<i>{plan.description or ''}</i>\n\n"
            f"Выбери способ оплаты доступа:"
        )
        if plan.is_trial and plan.upgrades_to_plan:
            text += (
                f"\n\nПосле пробного периода: {plan.upgrades_to_plan.name}"
                f" ({plan.upgrades_to_plan.price:.2f} RUB / {_duration_text(plan.upgrades_to_plan)})"
            )

    await send_smart_content(callback, text, None,
                             await kb.payment_provider_keyboard(plan_id, final_price, intent_token=intent.token))


@router.callback_query(F.data == "pay_topup_balance")
async def pay_topup_balance(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PaymentStates.set_token_amount)

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Отмена", callback_data="open_payment_menu")

    await callback.message.answer("Введи сумму пополнения в рублях (целое число):", reply_markup=builder.as_markup())
    await callback.answer()


@router.message(PaymentStates.set_token_amount, ~F.text.startswith('/'))
async def process_token_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text)
        if amount < 10: raise ValueError
    except:
        await message.answer("Минимальная сумма 10 рублей. Введи число.")
        return

    await state.update_data(amount=amount)
    await message.answer("Выбери способ оплаты:",
                         reply_markup=await kb.payment_providers_keyboard(is_subscription=False))


@router.callback_query(F.data.startswith("pay_prov_"))
async def process_payment_invoice(callback: CallbackQuery, state: FSMContext, bot: Bot):
    parts = callback.data.split("_")

    if "sub" in parts:
        prov_id = int(parts[3])
        token_or_plan_id = "_".join(parts[4:])
        action = 'sub'
    else:
        prov_id = int(parts[2])
        action = 'token'

    async with async_session_maker() as session:
        prices = []
        title = ""
        description = ""
        payload = ""
        amount_kops = 0

        if action == 'sub':
            intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token_or_plan_id))
            if intent:
                plan = await session.get(SubscriptionPlan, intent.plan_id)
                if not plan:
                    await callback.answer("Тариф не найден", show_alert=True)
                    return
                if intent.user_id != callback.from_user.id:
                    await callback.answer("Заказ принадлежит другому пользователю.", show_alert=True)
                    return
                if intent.status in ("paid", "settling", "cancelled", "expired"):
                    await callback.answer(f"Заказ уже {intent.status}. Пожалуйста, выбери тариф заново.", show_alert=True)
                    return
                now = msk_now()
                if intent.expires_at and intent.expires_at < now:
                    intent.status = "expired"
                    await session.commit()
                    await callback.answer("Срок действия предложения истёк, выбери тариф заново.", show_alert=True)
                    return
                # Atomic provider claim and idempotency guard
                if intent.provider == "telegram_pay":
                    await callback.answer("Счет на оплату уже выставлен в чате.", show_alert=False)
                    return
                elif intent.provider in ("pending_choice", "checkout"):
                    claim_res = await session.execute(
                        update(PaymentIntent)
                        .where(
                            PaymentIntent.id == intent.id,
                            PaymentIntent.user_id == callback.from_user.id,
                            PaymentIntent.status == "pending",
                            PaymentIntent.provider.in_(["pending_choice", "checkout"]),
                        )
                        .values(
                            provider="telegram_pay",
                            expires_at=now + timedelta(minutes=30),
                        )
                    )
                    await session.commit()
                    if claim_res.rowcount == 0:
                        fresh = await session.get(PaymentIntent, intent.id)
                        if fresh and fresh.provider == "prodamus":
                            await callback.answer("Для этого заказа уже выбран Prodamus. Сформируй новый заказ.", show_alert=True)
                        else:
                            await callback.answer("Заказ более недоступен. Выбери тариф заново.", show_alert=True)
                        return
                    intent.provider = "telegram_pay"
                    intent.expires_at = now + timedelta(minutes=30)
                else:
                    await callback.answer("Для этого заказа уже выбран другой способ оплаты.", show_alert=True)
                    return
            else:
                try:
                    plan_id = int(token_or_plan_id)
                    plan = await session.get(SubscriptionPlan, plan_id)
                except Exception:
                    plan = None
                if not plan:
                    await callback.answer("Тариф не найден", show_alert=True)
                    return
                intent = await create_payment_intent(session, callback.from_user.id, plan.id, provider="telegram_pay", bot=bot)

            if intent.final_amount <= 0.0:
                await callback.message.answer(f"✅ Доступ к образовательной программе по тарифу «{plan.name}» успешно оформлен за счет баллов!")
                await callback.answer()
                return

            amount_kops = int(round(intent.final_amount * 100))
            title = f"Образовательная программа: {plan.name}"
            description = f"Доступ к образовательной программе на {_duration_text(plan)}"
            payload = f"pi_{intent.token}"

        else:
            data = await state.get_data()
            rub_amount = data.get('amount', 100)
            amount_kops = rub_amount * 100
            title = "Пополнение баланса образовательной программы"
            description = f"Пополнение на {rub_amount} RUB"
            payload = f"token_{callback.from_user.id}_{rub_amount}"

        provider = await session.get(PaymentProvider, prov_id)

        if not provider:
            if action == 'sub' and intent:
                async with async_session_maker() as cleanup_session:
                    await cleanup_session.execute(
                        update(PaymentIntent)
                        .where(PaymentIntent.id == intent.id, PaymentIntent.status == "pending")
                        .values(status="cancelled")
                    )
                    await cleanup_session.commit()
            await callback.answer("Ошибка провайдера", show_alert=True)
            return

        prices.append(LabeledPrice(label=title, amount=amount_kops))

        try:
            payments_logger.info(
                "Creating Telegram invoice: user_id=%s action=%s provider=%s amount_kops=%s payload=%s",
                callback.from_user.id,
                action,
                provider.name,
                amount_kops,
                payload,
            )
            await bot.send_invoice(
                callback.message.chat.id,
                title=title,
                description=description,
                provider_token=provider.provider_token,
                currency="rub",
                prices=prices,
                start_parameter="pay",
                payload=payload
            )
        except TelegramBadRequest as e:
            if action == 'sub' and intent:
                async with async_session_maker() as cleanup_session:
                    await cleanup_session.execute(
                        update(PaymentIntent)
                        .where(PaymentIntent.id == intent.id, PaymentIntent.status == "pending")
                        .values(status="cancelled")
                    )
                    await cleanup_session.commit()
            error_text = str(e)
            payments_logger.error(
                "Telegram invoice error: user_id=%s provider=%s error=%s",
                callback.from_user.id,
                provider.name,
                error_text,
            )
            if "PAYMENT_PROVIDER_INVALID" in error_text:
                await callback.message.answer("⚠️ Извините, платежная система временно недоступна. Администратор уже уведомлен.")
                for owner_id in OWNER_IDS:
                    try:
                        safe_prov = html.escape(provider.name or "")
                        await bot.send_message(owner_id, f"🚨 <b>КРИТИЧЕСКАЯ ОШИБКА ОПЛАТЫ!</b>\n\nПровайдер: {safe_prov}\nОшибка: Неверный токен (PAYMENT_PROVIDER_INVALID).\n\nЗамените токен в настройках!", parse_mode='HTML')
                    except:
                        pass
            else:
                await callback.message.answer(f"⚠️ Ошибка при создании счета: {e}")
        except Exception as e:
            if action == 'sub' and intent:
                async with async_session_maker() as cleanup_session:
                    await cleanup_session.execute(
                        update(PaymentIntent)
                        .where(PaymentIntent.id == intent.id, PaymentIntent.status == "pending")
                        .values(status="cancelled")
                    )
                    await cleanup_session.commit()
            payments_logger.exception(
                "Unknown invoice creation error: user_id=%s provider=%s",
                callback.from_user.id,
                provider.name,
            )
            await callback.message.answer("Произошла неизвестная ошибка при оплате.")

    await callback.answer()


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    payload = pre_checkout_query.invoice_payload or ""
    if payload.startswith("pi_"):
        token = payload[3:]
        async with async_session_maker() as session:
            intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            if not intent:
                await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Счет не найден.")
                return
            if intent.provider != "telegram_pay":
                await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Неверный провайдер оплаты.")
                return
            if intent.status not in ("pending", "authorized"):
                await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Счет уже оплачен или отменен.")
                return

            expected_amount = int(round(intent.final_amount * 100))
            if pre_checkout_query.total_amount != expected_amount:
                await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Неверная сумма счета.")
                return
            if (pre_checkout_query.currency or "").upper() != "RUB":
                await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Неверная валюта счета.")
                return
            if pre_checkout_query.from_user and pre_checkout_query.from_user.id != intent.user_id:
                await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Счет выставлен другому пользователю.")
                return

            now = msk_now()
            if not intent.pre_checkout_approved_at:
                if intent.expires_at and intent.expires_at <= now:
                    intent.status = "expired"
                    await session.commit()
                    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Срок действия счета истек. Пожалуйста, сформируйте новый счет.")
                    return

            intent.status = "authorized"
            intent.pre_checkout_approved_at = now
            await session.commit()
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
            return

    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, bot: Bot):
    payment = message.successful_payment
    payload = payment.invoice_payload or ""
    payments_logger.info(
        "Successful Telegram payment: user_id=%s payload=%s amount=%s currency=%s provider_charge_id=%s",
        message.from_user.id,
        payload,
        payment.total_amount,
        payment.currency,
        payment.provider_payment_charge_id,
    )

    async with async_session_maker() as session:
        if payload.startswith("pi_"):
            token = payload[3:]
            intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            if intent:
                if intent.provider != "telegram_pay":
                    payments_logger.error(
                        "Telegram payment rejected: wrong provider for intent %s (provider=%s)",
                        intent.token,
                        intent.provider,
                    )
                    return
                expected_amount = int(round(intent.final_amount * 100))
                if payment.total_amount != expected_amount or (payment.currency or "").upper() != "RUB" or (message.from_user and message.from_user.id != intent.user_id):
                    payments_logger.error(
                        "Telegram payment mismatch: user_id=%s, intent_user=%s, total_amount=%s, expected=%s, currency=%s",
                        message.from_user.id if message.from_user else None,
                        intent.user_id,
                        payment.total_amount,
                        expected_amount,
                        payment.currency,
                    )
                    return
                if not (intent.pre_checkout_approved_at or intent.status == "authorized"):
                    payments_logger.error(
                        "Telegram payment rejected: intent %s not pre-checkout authorized",
                        intent.token,
                    )
                    return
                await settle_payment_intent(session, bot, intent, payment_charge_id=payment.provider_payment_charge_id or "")
                return

        user = await session.get(User, message.from_user.id)
        conf = await session.get(PaymentConfig, 1)

        if payload.startswith("sub_"):
            parts = payload.split("_")
            plan_id = int(parts[1])
            plan = await session.get(SubscriptionPlan, plan_id)
            if plan:
                intent = await create_payment_intent(session, message.from_user.id, plan.id, provider="telegram", bot=bot)
                await settle_payment_intent(session, bot, intent, payment_charge_id=payment.provider_payment_charge_id or "")
                return

        elif payload.startswith("token_"):
            parts = payload.split("_")
            rub_amount = int(parts[2])
            tokens = rub_amount * (conf.currency_ratio if conf else 1)
            if user:
                user.balance += tokens
                user.low_balance_notified = False
            await message.answer(f"✅ Баланс пополнен на {tokens} токенов.")

        # Лог транзакции
        txn = Transaction(
            user_id=message.from_user.id,
            amount=payment.total_amount,
            currency=payment.currency,
            provider=payment.provider_payment_charge_id,
            status='success'
        )
        session.add(txn)
        await session.commit()


async def admin_books_manager(event, page=0, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)

    if isinstance(event, CallbackQuery):
        data_parts = event.data.split("_")
        if len(data_parts) > 3 and data_parts[2].isdigit():
            page = int(data_parts[2])

    page_size = 10
    async with async_session_maker() as session:
        total = await session.scalar(select(func.count(Book.id))) or 0
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    page = max(0, min(page, total_pages - 1))

    async with async_session_maker() as session:
        books = await session.execute(
            select(Book).limit(page_size).offset(page * page_size).order_by(Book.id.desc())
        )
        books = books.scalars().all()

    builder = InlineKeyboardBuilder()

    for b in books:
        builder.row(InlineKeyboardButton(text=f"🗑 {b.title}", callback_data=f"confirm_del_book_{b.id}_{page}"))

    builder.row(*kb.standard_pagination_row(page, total_pages, lambda target: f"adm_books_pg_{target}"))
    builder.row(InlineKeyboardButton(text="➕ Добавить книгу", callback_data="add_book_start"))
    builder.row(InlineKeyboardButton(text="⬅️ К категориям", callback_data="admin_content_list"))

    text = f"📚 <b>Управление книгами ({total} шт)</b>\nНажмите на книгу, чтобы удалить ее."
    await send_smart_content(event, text, None, builder.as_markup())


@router.callback_query(F.data.startswith("adm_books_pg_"))
async def admin_books_pagination(callback: CallbackQuery, state: FSMContext | None = None):
    page = int(callback.data.split("_")[-1])
    await admin_books_manager(callback, page, state=state)


@router.callback_query(F.data == "add_book_start")
async def add_book_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.add_book_title)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_cat_books")

    msg = await callback.message.answer("Введите название книги:", reply_markup=builder.as_markup())
    await state.update_data(last_msg_id=msg.message_id)
    await callback.answer()


@router.message(AdminStates.add_book_title, ~F.text.startswith('/'))
async def save_book_title(message: Message, state: FSMContext):
    await clean_chat_after_input(message, state)
    title = (message.text or "").strip()
    if title.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await admin_books_manager(message)
        return
    if not title:
        await message.answer(
            "Название книги не должно быть пустым. Введите его ещё раз.",
            reply_markup=kb.back_to_previous_menu("admin_cat_books"),
        )
        return
    await state.update_data(title=title)
    await state.set_state(AdminStates.add_book_url)

    msg = await message.answer(
        "Теперь отправьте ссылку на книгу, например https://example.com.",
        reply_markup=kb.back_to_previous_menu("admin_cat_books"),
    )
    await state.update_data(last_msg_id=msg.message_id)


@router.message(AdminStates.add_book_url, ~F.text.startswith('/'))
async def save_book_url(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    url = message.text.strip()
    data = await state.get_data()
    if url.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await admin_books_manager(message)
        return
    if not url:
        await message.answer(
            "Ссылка не должна быть пустой. Например: https://example.com.",
            reply_markup=kb.back_to_previous_menu("admin_cat_books"),
        )
        return
    title = data['title']


    async with async_session_maker() as session:
        session.add(Book(title=title, url=url))
        await session.commit()
    async with async_session_maker() as session:
        await sync_book_menu_buttons(session)
        await session.commit()

    asyncio.create_task(send_temp_notification(bot, message.chat.id, "✅ Книга добавлена!"))
    await state.clear()

    class FakeUpdate:
        def __init__(self, msg, data):
            self.message = msg
            self.data = data
            self.from_user = msg.from_user
            self.bot = msg.bot
            self.chat = msg.chat

    fake_cb = FakeUpdate(message, "admin_cat_books")
    await admin_books_manager(fake_cb)


async def _execute_book_delete(callback: CallbackQuery, state: FSMContext | None, bid: int, page: int):
    await _clear_admin_navigation_state(state)

    async with async_session_maker() as session:
        b = await session.get(Book, bid)
        if b:
            menu_button = await session.scalar(select(MenuButton).where(
                MenuButton.section_key == "books",
                MenuButton.source_type == "book",
                MenuButton.source_id == bid,
            ))
            if menu_button:
                menu_button.enabled = False
            await session.delete(b)
            await session.commit()

    await callback.answer("Книга удалена")
    await admin_books_manager(callback, page)


@router.callback_query(F.data.startswith("del_book_"))
async def delete_book(callback: CallbackQuery, state: FSMContext | None = None):
    parts = callback.data.split("_")
    try:
        bid, page = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("Книга не найдена.", show_alert=True)
        return
    if isinstance(callback, CallbackQuery):
        async with async_session_maker() as session:
            book = await session.get(Book, bid)
        if not book:
            await callback.answer("Книга уже удалена.", show_alert=True)
            return
        await _ask_admin_confirmation(
            callback,
            f"Книга «{html.escape(book.title)}» будет удалена из библиотеки.",
            f"confirmed_del_book_{bid}_{page}",
            "admin_cat_books",
        )
        return
    await _execute_book_delete(callback, state, bid, page)


@router.callback_query(F.data.startswith("confirmed_del_book_"))
async def confirmed_delete_book(callback: CallbackQuery, state: FSMContext | None = None):
    parts = callback.data.split("_")
    try:
        bid, page = int(parts[3]), int(parts[4])
    except (IndexError, ValueError):
        await callback.answer("Книга не найдена.", show_alert=True)
        return
    await _execute_book_delete(callback, state, bid, page)


@router.callback_query(F.data.startswith("confirm_del_book_"))
async def confirm_delete_book(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    parts = callback.data.split("_")
    try:
        bid = int(parts[3])
        page = int(parts[4])
    except (IndexError, ValueError):
        await callback.answer("Книга не найдена.", show_alert=True)
        return
    async with async_session_maker() as session:
        book = await session.get(Book, bid)
    if not book:
        await callback.answer("Книга уже удалена.", show_alert=True)
        return
    await _ask_admin_confirmation(
        callback,
        f"Книга «{html.escape(book.title)}» будет удалена из библиотеки.",
        f"confirmed_del_book_{bid}_{page}",
        "admin_cat_books",
    )


@router.callback_query(F.data == "admin_manage_plans")
@router.callback_query(F.data.startswith("admin_manage_plans_"))
async def admin_manage_plans(event, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    if isinstance(event, CallbackQuery) and event.data.startswith("admin_manage_plans_"):
        page = int(event.data.split("_")[-1])
    else:
        page = 0
    await send_smart_content(event, "📈 <b>Управление тарифными планами</b>\nНажмите на тариф для управления.", None, await kb.admin_plans_keyboard(page))


@router.callback_query(F.data == "adm_add_plan_start")
async def admin_add_plan_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.plan_name)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_manage_plans")

    msg = await callback.message.answer("Введите название для нового тарифа (например, «Базовый месяц»):",
                                        reply_markup=builder.as_markup(), parse_mode='HTML')
    await state.update_data(last_msg_id=msg.message_id)
    await callback.answer()


@router.message(AdminStates.plan_name, ~F.text.startswith('/'))
async def save_plan_name(message: Message, state: FSMContext):
    await clean_chat_after_input(message, state)
    await state.update_data(p_name=message.text.strip())

    await state.set_state(AdminStates.plan_price)
    msg = await message.answer(
        "Отлично. Теперь введите стоимость тарифа в рублях (например, 990.50):",
        reply_markup=kb.back_to_previous_menu("admin_manage_plans"),
    )
    await state.update_data(last_msg_id=msg.message_id)


@router.message(AdminStates.plan_price, ~F.text.startswith('/'))
async def save_plan_price(message: Message, state: FSMContext):
    await clean_chat_after_input(message, state)
    try:
        price = float(message.text.replace(',', '.'))
        if price < 0:
            raise ValueError
        await state.update_data(p_price=price)

        await state.set_state(AdminStates.plan_days)
        msg = await message.answer(
            "Отлично. Теперь введите длительность тарифа в формате `число дни` или `число месяцы`.\n"
            "Примеры: `30 дни`, `1 месяц`",
            parse_mode='Markdown',
            reply_markup=kb.back_to_previous_menu("admin_manage_plans"),
        )
        await state.update_data(last_msg_id=msg.message_id)
    except:
        await message.answer("❌ Ошибка: Введите корректное число для цены.")


@router.message(AdminStates.plan_days, ~F.text.startswith('/'))
async def save_plan_days(message: Message, state: FSMContext):
    await clean_chat_after_input(message, state)
    try:
        raw = message.text.strip().lower().replace("ё", "е")
        parts = raw.split()
        duration_value = int(parts[0])
        if duration_value < 1:
            raise ValueError
        duration_unit = 'months' if len(parts) > 1 and parts[1].startswith('мес') else 'days'
        await state.update_data(p_days=duration_value, p_duration_unit=duration_unit)

        await state.set_state(AdminStates.plan_desc)
        msg = await message.answer(
            "Отлично. Теперь введите краткое описание тарифа:",
            reply_markup=kb.back_to_previous_menu("admin_manage_plans"),
        )
        await state.update_data(last_msg_id=msg.message_id)
    except:
        await message.answer("❌ Ошибка: Введите корректную длительность в формате `30 дни` или `1 месяц`.", parse_mode='Markdown')


@router.message(AdminStates.plan_desc, ~F.text.startswith('/'))
async def save_plan_desc(message: Message, state: FSMContext, bot: Bot):
    await clean_chat_after_input(message, state)
    data = await state.get_data()

    name = data['p_name']
    price_value = data['p_price']
    duration_value = data['p_days']
    duration_unit = data.get('p_duration_unit', 'days')
    desc = message.text.strip()
    if not desc:
        await message.answer("Описание не может быть пустым. Попробуйте ещё раз:")
        return

    async with async_session_maker() as session:
        new_plan = SubscriptionPlan(
            name=name,
            price=price_value,
            duration_value=duration_value,
            duration_unit=duration_unit,
            description=desc,
            is_active=True
        )
        session.add(new_plan)
        await session.commit()

    asyncio.create_task(send_temp_notification(bot, message.chat.id, f"✅ Новый тариф «{name}» создан! Теперь можно настроить его."))
    await state.clear()

    class FakeUpdate:
        def __init__(self, msg):
            self.message = msg
            self.from_user = msg.from_user
            self.bot = msg.bot
            self.chat = msg.chat

    await admin_manage_plans(FakeUpdate(message))


@router.callback_query(F.data.startswith("adm_edit_plan_"))
async def admin_edit_plan_menu(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    pid = int(callback.data.split("_")[-1])
    await _show_admin_edit_plan_menu(callback.bot, callback.message.chat.id, callback.message.message_id, pid)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_toggle_plan_"))
async def admin_toggle_plan(callback: CallbackQuery):
    pid = int(callback.data.split("_")[-1])

    async with async_session_maker() as session:
        plan = await session.get(SubscriptionPlan, pid)
        if plan:
            plan.is_active = not plan.is_active
            await session.commit()
    await _show_admin_edit_plan_menu(callback.bot, callback.message.chat.id, callback.message.message_id, pid)
    await callback.answer()


async def _execute_legacy_plan_delete(callback: CallbackQuery, pid: int):
    async with async_session_maker() as session:
        plan = await session.get(SubscriptionPlan, pid)
        if plan:
            await session.delete(plan)
            await session.commit()

    await callback.answer("Тариф удален")
    await admin_manage_plans(callback)


@router.callback_query(F.data.startswith("adm_del_plan_"))
async def admin_delete_plan(callback: CallbackQuery):
    try:
        pid = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    if isinstance(callback, CallbackQuery):
        async with async_session_maker() as session:
            plan = await session.get(SubscriptionPlan, pid)
        if not plan:
            await callback.answer("Тариф уже удалён.", show_alert=True)
            return
        await _ask_admin_confirmation(
            callback,
            f"Тариф «{html.escape(plan.name)}» будет удалён. Действие нельзя отменить.",
            f"confirmed_adm_del_plan_{pid}",
            f"adm_edit_plan_{pid}",
        )
        return
    await _execute_legacy_plan_delete(callback, pid)


@router.callback_query(F.data.startswith("confirmed_adm_del_plan_"))
async def confirmed_admin_delete_plan(callback: CallbackQuery):
    try:
        pid = int(callback.data.removeprefix("confirmed_adm_del_plan_"))
    except ValueError:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    await _execute_legacy_plan_delete(callback, pid)


@router.callback_query(F.data == "admin_subscriptions")
async def admin_subscriptions_menu(callback: CallbackQuery, state: FSMContext | None = None):
    if not await require_permission(callback, AdminPermission.PAYMENTS, state=state):
        return
    await _clear_admin_navigation_state(state)
    await callback.message.edit_text(
        "💳 <b>Подписки и оплата</b>\n\n"
        "Управляйте тарифами, подписчиками, промокодами, статистикой, платежами и реферальной программой.",
        reply_markup=kb.admin_payment_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_prodamus_settings")
async def admin_prodamus_settings(callback: CallbackQuery, state: FSMContext | None = None):
    if not await require_permission(callback, AdminPermission.PAYMENTS, state=state):
        return
    return_callback = _ADMIN_PAYMENT_MAIN_CALLBACK
    await _clear_admin_navigation_state(state)
    if state is not None:
        await state.update_data(
            admin_prodamus_return=return_callback,
        )
    async with async_session_maker() as session:
        config = await _ensure_subscription_config(session)
        await session.commit()
    await callback.message.edit_text(
        "⚙️ <b>Настройки оплаты</b>\n\n"
        "Технические параметры оплаты, провайдеры, стоимость сообщений и уведомления.",
        reply_markup=kb.admin_prodamus_settings_keyboard(config, return_callback),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_prodamus_keys"))
async def admin_prodamus_keys(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        config = await _ensure_subscription_config(session)
    back_callback = "admin_payment_settings"
    if callback.data.endswith("_from_settings"):
        back_callback = "admin_prodamus_settings"

    def _mask(value: str | None) -> str:
        if not value:
            return "не задан"
        return value if len(value) <= 10 else f"{value[:4]}...{value[-4:]}"

    def _preview(value: str | None, limit: int = 120) -> str:
        if not value:
            return "не задан"
        compact = " ".join(value.split())
        return compact if len(compact) <= limit else f"{compact[:limit - 1]}…"

    text = (
        "<b>Текущие ключи Prodamus</b>\n\n"
        f"URL формы: <code>{html.escape(config.prodamus_url or 'не задан')}</code>\n"
        f"Секретный ключ: <code>{_mask(config.prodamus_secret_key)}</code>\n"
        f"Контент / доступы для чека: <code>{html.escape(_preview(config.prodamus_paid_content))}</code>\n"
        f"Telegram Pay Token (не обязателен для Prodamus): <code>{_mask(config.telegram_pay_token)}</code>"
    )
    await callback.message.edit_text(
        text,
        reply_markup=kb.admin_prodamus_keys_keyboard(back_callback),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_toggle_sub_notifs"))
async def admin_toggle_sub_notifs(callback: CallbackQuery, state: FSMContext | None = None):
    current_screen = _admin_payment_action_source(callback.data, _ADMIN_PAYMENT_SETTINGS_CALLBACK)
    parent_callback = _admin_payment_settings_parent(current_screen)
    await _clear_admin_navigation_state(state)
    if state is not None:
        await state.update_data(
            admin_prodamus_return=current_screen,
        )
    async with async_session_maker() as session:
        config = await _ensure_subscription_config(session)
        config.notifications_enabled = not config.notifications_enabled
        await session.commit()
        reply_markup = kb.admin_prodamus_settings_keyboard(config, parent_callback)
    await callback.message.edit_reply_markup(reply_markup=reply_markup)
    await callback.answer("Настройка обновлена")


@router.callback_query(F.data.startswith("admin_toggle_subscriptions"))
async def admin_toggle_subscriptions(callback: CallbackQuery, state: FSMContext | None = None):
    current_screen = _admin_payment_action_source(callback.data, _ADMIN_PAYMENT_SETTINGS_CALLBACK)
    parent_callback = _admin_payment_settings_parent(current_screen)
    await _clear_admin_navigation_state(state)
    if state is not None:
        await state.update_data(
            admin_prodamus_return=current_screen,
        )
    async with async_session_maker() as session:
        config = await _ensure_subscription_config(session)
        config.subscriptions_enabled = not config.subscriptions_enabled
        await session.commit()
        reply_markup = kb.admin_prodamus_settings_keyboard(config, parent_callback)
    await callback.message.edit_reply_markup(reply_markup=reply_markup)
    await callback.answer("Настройка обновлена")


@router.callback_query(F.data.startswith("admin_payment_stats"))
async def admin_payment_stats(callback: CallbackQuery, state: FSMContext | None = None):
    if not await require_permission(callback, AdminPermission.PAYMENTS, state=state):
        return
    state_back_callback = None
    if state is not None:
        state_back_callback = (await state.get_data()).get("admin_payment_stats_return")
    back_callback = _normalize_admin_payment_return(state_back_callback)
    if ":" in callback.data:
        back_callback = _normalize_admin_payment_return(callback.data.split(":", 1)[1], back_callback)
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        now = msk_now()

        total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0

        active_paid_subs = (
            await session.execute(
                select(UserSubscription)
                .where(
                    UserSubscription.plan_id.is_not(None),
                    UserSubscription.end_date > now,
                )
                .options(selectinload(UserSubscription.plan))
            )
        ).scalars().all()

        active_paid_count = len(active_paid_subs)
        current_mrr = sum(sub.plan.price for sub in active_paid_subs if sub.plan)

        active_trials_count = (
            await session.execute(
                select(func.count(UserSubscription.id)).where(
                    UserSubscription.plan_id.is_(None),
                    UserSubscription.end_date > now,
                )
            )
        ).scalar() or 0

        expired_count = (
            await session.execute(
                select(func.count(UserSubscription.id)).where(UserSubscription.end_date <= now)
            )
        ).scalar() or 0

        total_prodamus_revenue = (
            await session.execute(
                select(func.sum(ProdamusPayment.amount)).where(ProdamusPayment.status == "completed")
            )
        ).scalar() or 0.0

        total_telegram_revenue_minor = (
            await session.execute(
                select(func.sum(Transaction.amount)).where(Transaction.status == "success")
            )
        ).scalar() or 0
        total_telegram_revenue = float(total_telegram_revenue_minor or 0) / 100

        total_landing_revenue = (
            await session.execute(
                select(func.sum(FunnelLanding.amount)).where(FunnelLanding.status == "paid")
            )
        ).scalar() or 0

        total_subscription_revenue = float(total_prodamus_revenue) + float(total_telegram_revenue)
        total_revenue = total_subscription_revenue + float(total_landing_revenue)

        plan_breakdown = (
            await session.execute(
                select(
                    SubscriptionPlan.name,
                    func.count(UserSubscription.id),
                )
                .join(UserSubscription)
                .where(UserSubscription.end_date > now)
                .group_by(SubscriptionPlan.name)
                .order_by(func.count(UserSubscription.id).desc(), SubscriptionPlan.name.asc())
            )
        ).all()

    text = (
        "<b>📊 Расширенная статистика</b>\n\n"
        f"👥 <b>Пользователи:</b>\n"
        f"• Всего в базе: {total_users}\n"
        f"• Активные платные: {active_paid_count}\n"
        f"• На пробном периоде: {active_trials_count}\n"
        f"• Истекшие подписки: {expired_count}\n\n"
        f"💰 <b>Финансы:</b>\n"
        f"• Ожидаемый ежемесячный доход от активных подписок: {current_mrr:,.2f} руб.\n"
        f"• Доход подписок через Prodamus: {float(total_prodamus_revenue):,.2f} руб.\n"
        f"• Доход Telegram Pay / токены: {total_telegram_revenue:,.2f} руб.\n"
        f"• Доход с лендинга / покупок: {float(total_landing_revenue):,.2f} руб.\n"
        f"• Общий доход: {total_revenue:,.2f} руб.\n"
        f"• Доход только по подпискам: {total_subscription_revenue:,.2f} руб.\n\n"
        f"📉 <b>Популярность тарифов (Активные):</b>\n"
    )

    if plan_breakdown:
        for name, count in plan_breakdown:
            text += f"• {html.escape(name)}: {count} шт.\n"
    else:
        text += "• Нет активных тарифов\n"

    await callback.message.edit_text(
        text,
        reply_markup=kb.back_to_previous_menu(back_callback),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_set_welcome_bonus"))
async def admin_set_welcome_bonus(callback: CallbackQuery, state: FSMContext):
    return_callback = _admin_payment_action_source(callback.data, _ADMIN_PAYMENT_SETTINGS_CALLBACK)
    await state.clear()
    await state.update_data(admin_prodamus_return=return_callback)
    await state.set_state(AdminStates.set_welcome_bonus_days)
    await state.update_data(
        last_msg_id=callback.message.message_id,
        admin_prodamus_return=return_callback,
    )
    await callback.message.edit_text(
        "Введите количество приветственных бонусных дней:",
        reply_markup=kb.back_to_previous_menu(return_callback),
    )
    await callback.answer()


@router.message(AdminStates.set_welcome_bonus_days, F.text)
async def process_welcome_bonus_days(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    message_id_to_edit = data.get("last_msg_id")
    current_screen = data.get("admin_prodamus_return", _ADMIN_PAYMENT_MAIN_CALLBACK)
    parent_callback = _admin_payment_settings_parent(current_screen)
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("Введите целое число.")
        return

    async with async_session_maker() as session:
        config = await _ensure_subscription_config(session)
        config.welcome_bonus_days = days
        await session.commit()

    await state.clear()
    await message.delete()
    if message_id_to_edit:
        await bot.edit_message_text(
            "⚙️ <b>Настройки оплаты</b>",
            chat_id=message.chat.id,
            message_id=message_id_to_edit,
            reply_markup=kb.admin_prodamus_settings_keyboard(config, parent_callback),
            parse_mode="HTML",
        )


@router.callback_query(
    F.data.startswith("set_prodamus_key_url")
    | F.data.startswith("set_prodamus_key_secret")
    | F.data.startswith("set_prodamus_key_paid_content")
    | F.data.startswith("set_prodamus_key_tg_token")
)
async def set_prodamus_key_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    key_map = {
        "set_prodamus_key_url": ("prodamus_url", "URL формы Prodamus"),
        "set_prodamus_key_secret": ("prodamus_secret_key", "секретный ключ Prodamus"),
        "set_prodamus_key_paid_content": ("prodamus_paid_content", "контент / доступы для чека Prodamus"),
        "set_prodamus_key_tg_token": ("telegram_pay_token", "Telegram Pay Token (не обязателен для Prodamus)"),
    }
    raw_key, back_callback = (callback.data.split("__", 1) + ["admin_payment_settings"])[:2]
    key_to_edit, key_name = key_map[raw_key]
    await state.set_state(AdminStates.set_single_payment_key)
    await state.update_data(
        key_to_edit=key_to_edit,
        key_source=back_callback,
        message_id_to_edit=callback.message.message_id
    )
    await callback.message.edit_text(
        f"Введите новое значение для <b>{key_name}</b>:",
        reply_markup=kb.back_to_previous_menu(f"admin_prodamus_keys_from_{'settings' if back_callback == 'admin_prodamus_settings' else 'root'}"),
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AdminStates.set_single_payment_key, F.text)
async def process_single_payment_key(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    key_to_edit = data["key_to_edit"]
    key_source = data.get("key_source", "admin_payment_settings")
    message_id_to_edit = data["message_id_to_edit"]

    async with async_session_maker() as session:
        config = await _ensure_subscription_config(session)
        setattr(config, key_to_edit, message.text.strip())
        await session.commit()

    await state.clear()
    await message.delete()
    def _preview(value: str | None, limit: int = 120) -> str:
        if not value:
            return "не задан"
        compact = " ".join(value.split())
        return compact if len(compact) <= limit else f"{compact[:limit - 1]}…"

    text = (
        "<b>Текущие ключи Prodamus</b>\n\n"
        f"URL формы: <code>{html.escape(config.prodamus_url or 'не задан')}</code>\n"
        f"Секретный ключ: <code>{(config.prodamus_secret_key[:4] + '...' + config.prodamus_secret_key[-4:]) if config.prodamus_secret_key and len(config.prodamus_secret_key) > 10 else (config.prodamus_secret_key or 'не задан')}</code>\n"
        f"Контент / доступы для чека: <code>{html.escape(_preview(config.prodamus_paid_content))}</code>\n"
        f"Telegram Pay Token (не обязателен для Prodamus): <code>{(config.telegram_pay_token[:4] + '...' + config.telegram_pay_token[-4:]) if config.telegram_pay_token and len(config.telegram_pay_token) > 10 else (config.telegram_pay_token or 'не задан')}</code>"
    )
    await bot.edit_message_text(
        text,
        chat_id=message.chat.id,
        message_id=message_id_to_edit,
        reply_markup=kb.admin_prodamus_keys_keyboard(key_source),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("toggle_plan_activity_"))
async def toggle_plan_activity(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    plan_id = int(callback.data.split("_")[-1])
    async with async_session_maker() as session:
        plan = await session.get(SubscriptionPlan, plan_id)
        if plan:
            plan.is_active = not plan.is_active
            await session.commit()
    await _show_admin_edit_plan_menu(callback.bot, callback.message.chat.id, callback.message.message_id, plan_id)
    await callback.answer()


@router.callback_query(F.data.startswith("toggle_plan_admin_only_"))
async def toggle_plan_admin_only(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    plan_id = int(callback.data.split("_")[-1])
    async with async_session_maker() as session:
        plan = await session.get(SubscriptionPlan, plan_id)
        if plan:
            plan.admin_only = not plan.admin_only
            await session.commit()
    await _show_admin_edit_plan_menu(callback.bot, callback.message.chat.id, callback.message.message_id, plan_id)
    await callback.answer()


@router.callback_query(F.data.startswith("toggle_plan_allow_auto_renewal_"))
async def toggle_plan_allow_auto_renewal(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    plan_id = int(callback.data.split("_")[-1])
    async with async_session_maker() as session:
        plan = await session.get(SubscriptionPlan, plan_id)
        if plan:
            plan.allow_auto_renewal = not plan.allow_auto_renewal
            await session.commit()
    await _show_admin_edit_plan_menu(callback.bot, callback.message.chat.id, callback.message.message_id, plan_id)
    await callback.answer()


@router.callback_query(F.data.startswith("toggle_plan_is_trial_"))
async def toggle_plan_is_trial(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    plan_id = int(callback.data.split("_")[-1])
    async with async_session_maker() as session:
        plan = await session.get(SubscriptionPlan, plan_id)
        if plan:
            plan.is_trial = not plan.is_trial
            if not plan.is_trial:
                plan.upgrades_to_plan_id = None
            await session.commit()
    await _show_admin_edit_plan_menu(callback.bot, callback.message.chat.id, callback.message.message_id, plan_id)
    await callback.answer()


@router.callback_query(F.data.startswith("set_plan_upgrade_target_"))
async def set_plan_upgrade_target_start(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    plan_id = int(callback.data.split("_")[-1])
    await callback.message.edit_text("Выберите тариф для автоперехода после пробного периода:", reply_markup=await kb.admin_select_upgrade_plan_keyboard(plan_id))
    await callback.answer()


@router.callback_query(F.data.startswith("set_upgrade_plan_"))
async def set_upgrade_plan(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    parts = callback.data.split("_")
    current_plan_id = int(parts[3])
    target_raw = parts[4]
    async with async_session_maker() as session:
        plan = await session.get(SubscriptionPlan, current_plan_id)
        if plan:
            plan.upgrades_to_plan_id = None if target_raw == "none" else int(target_raw)
            await session.commit()
    await _show_admin_edit_plan_menu(callback.bot, callback.message.chat.id, callback.message.message_id, current_plan_id)
    await callback.answer()


@router.callback_query(F.data.startswith("edit_plan_field_"))
async def edit_plan_field_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = callback.data.split("_")
    plan_id = int(parts[-1])
    field_to_edit = "_".join(parts[3:-1])
    field_map = {
        "name": "название",
        "description": "описание",
        "price": "цену (в рублях)",
        "duration_value": "длительность (число)",
        "duration_unit": "единицу длительности",
        "trial_cooldown_days": "повторный пробный период (в днях, 0 = нельзя повторно)",
        "prodamus_subscription_id": "код подписки в Prodamus",
        "prodamus_autopayments_limit": "лимит автосписаний Prodamus",
    }
    if field_to_edit == "duration_unit":
        await state.set_state(AdminStates.edit_plan_field)
        await state.update_data(plan_id=plan_id, field=field_to_edit, message_id_to_edit=callback.message.message_id)
        await callback.message.edit_text("Выберите новую единицу длительности:", reply_markup=kb.admin_select_duration_unit_keyboard())
        await callback.answer()
        return

    await state.set_state(AdminStates.edit_plan_field)
    await state.update_data(plan_id=plan_id, field=field_to_edit, message_id_to_edit=callback.message.message_id)
    await callback.message.edit_text(
        f"Введите новое {field_map.get(field_to_edit, 'значение')} для тарифа:",
        reply_markup=kb.back_to_previous_menu(f"adm_edit_plan_{plan_id}")
    )
    await callback.answer()


@router.callback_query(AdminStates.edit_plan_field, F.data.startswith("set_duration_unit_"))
async def edit_plan_duration_unit(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    plan_id = data["plan_id"]
    duration_unit = callback.data.split("_")[-1]
    async with async_session_maker() as session:
        plan = await session.get(SubscriptionPlan, plan_id)
        if plan:
            plan.duration_unit = duration_unit
            await session.commit()
    await state.clear()
    await _show_admin_edit_plan_menu(callback.bot, callback.message.chat.id, callback.message.message_id, plan_id)
    await callback.answer()


@router.message(AdminStates.edit_plan_field, F.text)
async def process_edit_plan_field(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    plan_id = data["plan_id"]
    field = data["field"]
    message_id_to_edit = data["message_id_to_edit"]
    raw_value = message.text.strip()

    converters = {
        "name": str,
        "description": str,
        "price": lambda x: float(x.replace(',', '.')),
        "duration_value": int,
        "trial_cooldown_days": int,
        "prodamus_subscription_id": lambda x: int(x) if x.strip() not in {"", "0", "-"} else None,
        "prodamus_autopayments_limit": lambda x: int(x) if x.strip() not in {"", "0", "-"} else None,
    }
    try:
        new_value = converters.get(field, str)(raw_value)
    except ValueError:
        await message.answer("❌ Ошибка: Введите корректное значение.")
        return

    async with async_session_maker() as session:
        plan = await session.get(SubscriptionPlan, plan_id)
        if plan:
            setattr(plan, field, new_value)
            await session.commit()
    await state.clear()
    await message.delete()
    await _show_admin_edit_plan_menu(bot, message.chat.id, message_id_to_edit, plan_id)


async def _execute_admin_delete_plan(callback: CallbackQuery, state: FSMContext | None, plan_id: int):
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        active_users = await session.scalar(select(func.count(UserSubscription.id)).where(UserSubscription.plan_id == plan_id))
        if active_users:
            await callback.answer("На тарифе есть активные подписчики, удалить нельзя.", show_alert=True)
            return
        plan = await session.get(SubscriptionPlan, plan_id)
        if plan:
            await session.delete(plan)
            await session.commit()
    await admin_manage_plans(callback)
    await callback.answer("Тариф удалён")


@router.callback_query(F.data.startswith("admin_delete_plan_"))
async def admin_delete_plan_new(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    try:
        plan_id = int(callback.data.removeprefix("admin_delete_plan_"))
    except ValueError:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    if isinstance(callback, CallbackQuery):
        async with async_session_maker() as session:
            active_users = await session.scalar(select(func.count(UserSubscription.id)).where(UserSubscription.plan_id == plan_id))
            plan = await session.get(SubscriptionPlan, plan_id)
        if active_users:
            await callback.answer("На тарифе есть активные подписчики, удалить нельзя.", show_alert=True)
            return
        if not plan:
            await callback.answer("Тариф уже удалён.", show_alert=True)
            return
        await _ask_admin_confirmation(
            callback,
            f"Тариф «{html.escape(plan.name)}» будет удалён. Действие нельзя отменить.",
            f"confirmed_admin_delete_plan_{plan_id}",
            f"adm_edit_plan_{plan_id}",
        )
        return
    await _execute_admin_delete_plan(callback, state, plan_id)


@router.callback_query(F.data.startswith("confirmed_admin_delete_plan_"))
async def confirmed_admin_delete_plan(callback: CallbackQuery, state: FSMContext | None = None):
    try:
        plan_id = int(callback.data.removeprefix("confirmed_admin_delete_plan_"))
    except ValueError:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    await _execute_admin_delete_plan(callback, state, plan_id)


@router.callback_query(F.data.startswith("confirm_admin_delete_plan_"))
async def confirm_delete_plan(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    try:
        plan_id = int(callback.data.removeprefix("confirm_admin_delete_plan_"))
    except ValueError:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    async with async_session_maker() as session:
        plan = await session.get(SubscriptionPlan, plan_id)
    if not plan:
        await callback.answer("Тариф уже удалён.", show_alert=True)
        return
    await _ask_admin_confirmation(
        callback,
        f"Тариф «{html.escape(plan.name)}» будет удалён. Действие нельзя отменить.",
        f"confirmed_admin_delete_plan_{plan_id}",
        f"adm_edit_plan_{plan_id}",
    )


@router.callback_query(F.data == "admin_promocodes")
@router.callback_query(F.data.startswith("admin_promocodes_"))
async def admin_promocodes(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    if callback.data.startswith("admin_promocodes_"):
        page = int(callback.data.split("_")[-1])
    else:
        page = 0
    page_size = 10
    async with async_session_maker() as session:
        total = await session.scalar(select(func.count(PromoCode.id))) or 0
        total_pages = max(1, math.ceil(total / page_size)) if total else 1
        page = max(0, min(page, total_pages - 1))
        promos = (
            await session.execute(
                select(PromoCode).order_by(PromoCode.id.desc()).limit(page_size).offset(page * page_size)
            )
        ).scalars().all()
    await callback.message.edit_text("🎁 Управление промокодами", reply_markup=kb.admin_promocodes_keyboard(promos, page, total_pages))
    await callback.answer()


@router.callback_query(F.data == "admin_create_promo")
async def admin_create_promo(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.set_promo_code)
    await state.update_data(message_id_to_edit=callback.message.message_id)
    await callback.message.edit_text("Введите текст промокода:", reply_markup=kb.back_to_previous_menu("admin_promocodes"))
    await callback.answer()


@router.message(AdminStates.set_promo_code, F.text)
async def process_promo_code(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(code=normalize_promo_code(message.text))
    await state.set_state(AdminStates.set_promo_discount)
    data = await state.get_data()
    await message.delete()
    await bot.edit_message_text(
        "Введите скидку в процентах (0-100):",
        chat_id=message.chat.id,
        message_id=data["message_id_to_edit"],
        reply_markup=kb.back_to_previous_menu("admin_promocodes"),
    )



@router.message(AdminStates.set_promo_discount, F.text)
async def process_promo_discount(message: Message, state: FSMContext, bot: Bot):
    try:
        discount = int(message.text.strip())
    except ValueError:
        await message.answer("Введите целое число.")
        return
    if not 0 <= discount <= 100:
        await message.answer("Введите скидку от 0 до 100%.")
        return
    await state.update_data(discount_percent=discount)
    await state.set_state(AdminStates.set_promo_days)
    data = await state.get_data()
    await message.delete()
    await bot.edit_message_text(
        "Введите количество бесплатных дней:",
        chat_id=message.chat.id,
        message_id=data["message_id_to_edit"],
        reply_markup=kb.back_to_previous_menu("admin_promocodes"),
    )


@router.message(AdminStates.set_promo_days, F.text)
async def process_promo_days(message: Message, state: FSMContext, bot: Bot):
    try:
        free_days = int(message.text.strip())
    except ValueError:
        await message.answer("Введите целое число.")
        return
    if free_days < 0:
        await message.answer("Количество бесплатных дней не может быть отрицательным.")
        return
    await state.update_data(free_days=free_days)
    await state.set_state(AdminStates.set_promo_uses)
    data = await state.get_data()
    await message.delete()
    await bot.edit_message_text(
        "Введите максимальное число использований:",
        chat_id=message.chat.id,
        message_id=data["message_id_to_edit"],
        reply_markup=kb.back_to_previous_menu("admin_promocodes"),
    )


@router.message(AdminStates.set_promo_uses, F.text)
async def process_promo_uses(message: Message, state: FSMContext, bot: Bot):
    try:
        max_uses = int(message.text.strip())
    except ValueError:
        await message.answer("Введите целое число.")
        return
    if max_uses < 1:
        await message.answer("Максимальное число использований должно быть не меньше 1.")
        return
    data = await state.get_data()
    norm_code = normalize_promo_code(data["code"])
    async with async_session_maker() as session:
        existing = await session.scalar(select(PromoCode).where(PromoCode.code == norm_code))
        if existing:
            await message.answer("Промокод с таким текстом уже существует.")
            return
        promo = PromoCode(
            code=norm_code,
            discount_percent=data["discount_percent"],
            free_days=data["free_days"],
            max_uses=max_uses,
        )
        session.add(promo)
        await session.commit()
        promo_id = promo.id
    await state.clear()
    await message.delete()
    await _show_admin_edit_promo_menu(bot, message.chat.id, data["message_id_to_edit"], promo_id)


@router.callback_query(F.data.startswith("admin_edit_promo_"))
async def admin_edit_promo(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    promo_id = int(callback.data.split("_")[-1])
    await _show_admin_edit_promo_menu(callback.bot, callback.message.chat.id, callback.message.message_id, promo_id)
    await callback.answer()


@router.callback_query(F.data.startswith("toggle_promo_active_"))
async def toggle_promo_active(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    promo_id = int(callback.data.split("_")[-1])
    async with async_session_maker() as session:
        promo = await session.get(PromoCode, promo_id)
        if promo:
            promo.is_active = not promo.is_active
            await session.commit()
    await _show_admin_edit_promo_menu(callback.bot, callback.message.chat.id, callback.message.message_id, promo_id)
    await callback.answer()


async def _execute_promo_delete(callback: CallbackQuery, state: FSMContext | None, promo_id: int):
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        promo = await session.get(PromoCode, promo_id)
        if promo and promo.times_used == 0:
            await session.delete(promo)
            await session.commit()
        elif promo:
            await callback.answer("Использованный промокод удалять нельзя.", show_alert=True)
            return
    await admin_promocodes(callback)
    await callback.answer("Промокод удалён")


@router.callback_query(F.data.startswith("delete_promo_"))
async def delete_promo(callback: CallbackQuery, state: FSMContext | None = None):
    try:
        promo_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("Промокод не найден.", show_alert=True)
        return
    if isinstance(callback, CallbackQuery):
        async with async_session_maker() as session:
            promo = await session.get(PromoCode, promo_id)
        if not promo:
            await callback.answer("Промокод уже удалён.", show_alert=True)
            return
        await _ask_admin_confirmation(
            callback,
            f"Промокод «{html.escape(promo.code)}» будет удалён.",
            f"confirmed_delete_promo_{promo_id}",
            f"admin_edit_promo_{promo_id}",
        )
        return
    await _execute_promo_delete(callback, state, promo_id)


@router.callback_query(F.data.startswith("confirmed_delete_promo_"))
async def confirmed_delete_promo(callback: CallbackQuery, state: FSMContext | None = None):
    try:
        promo_id = int(callback.data.removeprefix("confirmed_delete_promo_"))
    except ValueError:
        await callback.answer("Промокод не найден.", show_alert=True)
        return
    await _execute_promo_delete(callback, state, promo_id)


@router.callback_query(F.data.startswith("confirm_delete_promo_"))
async def confirm_delete_promo(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    try:
        promo_id = int(callback.data.removeprefix("confirm_delete_promo_"))
    except ValueError:
        await callback.answer("Промокод не найден.", show_alert=True)
        return
    async with async_session_maker() as session:
        promo = await session.get(PromoCode, promo_id)
    if not promo:
        await callback.answer("Промокод уже удалён.", show_alert=True)
        return
    await _ask_admin_confirmation(
        callback,
        f"Промокод «{html.escape(promo.code)}» будет удалён.",
        f"confirmed_delete_promo_{promo_id}",
        f"admin_edit_promo_{promo_id}",
    )


@router.callback_query(F.data.startswith("edit_promo_field_"))
async def edit_promo_field_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = callback.data.split("_")
    promo_id = int(parts[-1])
    field_map = {
        "code": "code",
        "discount": "discount_percent",
        "days": "free_days",
        "max_uses": "max_uses",
    }
    field_alias = "_".join(parts[3:-1])
    await state.set_state(AdminStates.edit_promo_field)
    await state.update_data(promo_id=promo_id, field=field_map[field_alias], message_id_to_edit=callback.message.message_id)
    await callback.message.edit_text("Введите новое значение:", reply_markup=kb.back_to_previous_menu(f"admin_edit_promo_{promo_id}"))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_assign_promo_"))
async def admin_assign_promo(callback: CallbackQuery):
    parts = callback.data.split("_")
    promo_id = int(parts[3])
    page = int(parts[5])
    await _show_assign_promo_to_plan_menu(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        promo_id,
        page,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("promo_plan_toggle_"))
async def promo_plan_toggle(callback: CallbackQuery):
    parts = callback.data.split("_")
    action = parts[3]
    promo_id = int(parts[4])
    plan_id = int(parts[5])
    page = int(parts[6])

    async with async_session_maker() as session:
        promo = await session.get(PromoCode, promo_id, options=[selectinload(PromoCode.applicable_plans)])
        plan = await session.get(SubscriptionPlan, plan_id)
        if not promo or not plan:
            await callback.answer("Промокод или тариф не найден.", show_alert=True)
            return
        if action == "add" and plan not in promo.applicable_plans:
            promo.applicable_plans.append(plan)
        elif action == "remove" and plan in promo.applicable_plans:
            promo.applicable_plans.remove(plan)
        await session.commit()

    await _show_assign_promo_to_plan_menu(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        promo_id,
        page,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("promo_toggle_all_plans_"))
async def promo_toggle_all_plans(callback: CallbackQuery):
    parts = callback.data.split("_")
    promo_id = int(parts[4])
    page = int(parts[5])

    async with async_session_maker() as session:
        promo = await session.get(PromoCode, promo_id)
        if not promo:
            await callback.answer("Промокод не найден.", show_alert=True)
            return
        promo.applies_to_all_plans = not promo.applies_to_all_plans
        await session.commit()

    await _show_assign_promo_to_plan_menu(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        promo_id,
        page,
    )
    await callback.answer()


@router.message(AdminStates.edit_promo_field, F.text)
async def process_edit_promo_field(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    promo_id = data["promo_id"]
    field = data["field"]
    message_id_to_edit = data["message_id_to_edit"]
    raw_value = message.text.strip()
    if field == "code":
        new_value = normalize_promo_code(raw_value)
    else:
        try:
            new_value = int(raw_value)
        except ValueError:
            await message.answer("Введите корректное значение.")
            return
        if field == "discount_percent" and not 0 <= new_value <= 100:
            await message.answer("Введите скидку от 0 до 100%.")
            return
        if field == "free_days" and new_value < 0:
            await message.answer("Количество бесплатных дней не может быть отрицательным.")
            return
        if field == "max_uses" and new_value < 1:
            await message.answer("Максимальное число использований должно быть не меньше 1.")
            return
    async with async_session_maker() as session:
        promo = await session.get(PromoCode, promo_id)
        if promo:
            if field == "code":
                existing = await session.scalar(
                    select(PromoCode).where(
                        PromoCode.code == new_value,
                        PromoCode.id != promo_id,
                    )
                )
                if existing:
                    await message.answer("Промокод с таким текстом уже существует.")
                    return
            setattr(promo, field, new_value)
            await session.commit()
    await state.clear()
    await message.delete()
    await _show_admin_edit_promo_menu(bot, message.chat.id, message_id_to_edit, promo_id)


@router.callback_query(F.data == "sub_enter_promo")
async def sub_enter_promo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.awaiting_promo_code)
    await callback.message.answer("Введи промокод:")
    await callback.answer()


@router.message(UserStates.awaiting_promo_code, F.text)
async def process_user_promo_code(message: Message, state: FSMContext, bot: Bot):
    code_text = normalize_promo_code(message.text)
    now = msk_now()
    back_kb = kb.back_to_previous_menu("show_subscription_info_from_chat")
    async with async_session_maker() as session:
        promo = await session.scalar(
            select(PromoCode).where(
                PromoCode.code == code_text,
                PromoCode.is_active == True,
                PromoCode.times_used < PromoCode.max_uses
            ).options(selectinload(PromoCode.applicable_plans))
        )
        if not promo:
            await message.answer("❌ Промокод не найден, истёк или недействителен. Попробуй ещё раз или нажми «Назад».", reply_markup=back_kb)
            return

        user = await session.get(
            User,
            message.from_user.id,
            options=[
                selectinload(User.subscription),
                selectinload(User.promo_codes).selectinload(PromoCode.applicable_plans),
            ]
        )
        if not user:
            await state.clear()
            await message.answer("Не удалось найти профиль пользователя. Нажми /start и попробуй снова.", reply_markup=back_kb)
            return
        user_sub = user.subscription
        if promo in user.promo_codes:
            await state.clear()
            await message.answer("❌ Ты уже активировал(а) этот промокод.", reply_markup=back_kb)
            return
        if promo.discount_percent == 0 and promo.free_days == 0:
            await state.clear()
            await message.answer("❌ Этот промокод неактивен (0% скидки и 0 дней). Обратись к администратору.", reply_markup=back_kb)
            return

        if promo not in user.promo_codes:
            user.promo_codes.append(promo)
        is_active_sub = user_sub and user_sub.end_date > now and user_sub.plan_id is not None
        if not user_sub:
            user_sub = UserSubscription(user_id=message.from_user.id, start_date=now, end_date=now, auto_renewal=False, payment_provider="Trial Promo", discount_percent=0)
            session.add(user_sub)

        trial_activated = False
        discount_banked = False

        if promo.free_days > 0:
            if is_active_sub:
                if promo.discount_percent == 0:
                    await state.clear()
                    await message.answer(
                        f"ℹ️ Этот промокод даёт пробный период ({promo.free_days} дн.). Активируй его после окончания текущего доступа к образовательной программе.",
                        reply_markup=back_kb
                    )
                    return
                await message.answer(
                    f"✅ Скидка {promo.discount_percent}% сохранена, но пробный период ({promo.free_days} дн.) нельзя активировать, пока у тебя есть другой активный платный доступ к образовательной программе."
                )
            else:
                user_sub.plan_id = None
                user_sub.start_date = user_sub.start_date if (user_sub.start_date and user_sub.start_date < now) else now
                user_sub.end_date = _extend_subscription_end_date(user_sub.end_date, now, promo.free_days, "days")
                user_sub.auto_renewal = False
                user_sub.payment_provider = "Trial Promo"
                user_sub.payment_method_id = None
                user_sub.last_payment_attempt = None
                user_sub.payment_attempt_count = 0
                session.add(TrialUsageHistory(user_id=message.from_user.id, plan_id=None, used_at=now))
                trial_activated = True

        if promo.discount_percent > 0:
            if promo.applies_to_all_plans and promo.discount_percent > user_sub.discount_percent:
                user_sub.discount_percent = promo.discount_percent
            discount_banked = True

        result = await session.execute(
            update(PromoCode)
            .where(PromoCode.id == promo.id, PromoCode.times_used < PromoCode.max_uses)
            .values(times_used=PromoCode.times_used + 1)
        )
        if result.rowcount == 0:
            await state.clear()
            await message.answer("❌ Промокод уже исчерпан. Попробуй другой.", reply_markup=back_kb)
            return
        await session.refresh(promo)
        user_ref_pc = f"{message.from_user.first_name or ''}"
        if message.from_user.username:
            user_ref_pc += f" (@{message.from_user.username})"
        user_ref_pc += f" [id=<code>{message.from_user.id}</code>]"
        logger.info(f"ПРОМОКОД | {user_ref_pc} | код={code_text} | скидка={promo.discount_percent}% | дней={promo.free_days}")

        config = await _ensure_subscription_config(session)
        if config.notifications_enabled:
            for admin_id in await get_all_admin_ids():
                try:
                    await bot.send_message(
                        admin_id,
                        f"🎁 Активирован промокод «{code_text}»\nПользователь: {user_ref_pc}\nСкидка: {promo.discount_percent}%\nДней: {promo.free_days}",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        user.subscription_end = user_sub.end_date if user_sub.end_date > now else None
        await session.commit()

    await state.clear()
    if promo.free_days > 0 and trial_activated and discount_banked:
        await message.answer(
            f"✅ Тебе начислен пробный период: <b>{promo.free_days} дн.</b> (до {format_msk(user.subscription_end, '%d.%m.%Y %H:%M')} МСК).\n\n"
            f"🔥 Также тебе назначена скидка <b>{promo.discount_percent}%</b>! "
            f"Она сохранится для всех автоплатежей, <b>если ты оформишь доступ к образовательной программе до окончания пробного периода</b>.",
            parse_mode="HTML"
        )
    elif promo.free_days > 0 and trial_activated:
        await message.answer(
            f"✅ Пробный период успешно активирован!\n"
            f"Тебе начислено: <b>{promo.free_days} бесплатных дней</b>.\n"
            f"Доступ активен до: {format_msk(user.subscription_end, '%d.%m.%Y %H:%M')} МСК.",
            parse_mode="HTML"
        )
    elif discount_banked:
        if not is_active_sub:
            await message.answer(
                f"✅ Скидка <b>{promo.discount_percent}%</b> сохранена!\n"
                "Она будет автоматически применена при выборе тарифа образовательной программы и всех последующих автоплатежах.",
                parse_mode="HTML"
            )
        else:
            await message.answer(
                f"✅ Скидка <b>{promo.discount_percent}%</b> сохранена! Она будет применена при <b>следующем</b> продлении или смене тарифа.",
                parse_mode="HTML"
            )
    await _send_subscription_info(message.from_user.id, message.chat.id, bot)


@router.callback_query(F.data == "sub_toggle_renewal")
async def sub_toggle_renewal(callback: CallbackQuery, bot: Bot):
    async with async_session_maker() as session:
        config = await _ensure_subscription_config(session)
        user_sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == callback.from_user.id).options(selectinload(UserSubscription.plan)))
        if not user_sub or not user_sub.plan:
            await callback.answer("Активный платный доступ к образовательной программе не найден.", show_alert=True)
            return
        if user_sub.payment_provider == "Prodamus" and user_sub.prodamus_subscription_id:
            if user_sub.auto_renewal:
                ok, details = await _call_prodamus_set_activity(
                    config,
                    subscription_id=user_sub.prodamus_subscription_id,
                    active_user=0,
                    active_manager=1,
                    profile_id=user_sub.prodamus_profile_id,
                    tg_user_id=callback.from_user.id,
                )
                if not ok:
                    await callback.answer(f"Не удалось отключить в Prodamus: {details}", show_alert=True)
                    return
                user_sub.auto_renewal = False
            else:
                await callback.answer(
                    "После отключения автосписания в Prodamus доступ к образовательной программе нужно будет оформить заново через оплату тарифа.",
                    show_alert=True,
                )
                return
        else:
            user_sub.auto_renewal = not user_sub.auto_renewal
        await session.commit()
        state_text = "включено" if user_sub.auto_renewal else "выключено"
    await callback.answer(f"Автопродление {state_text}.", show_alert=True)
    await _send_subscription_info(callback.from_user.id, callback.message.chat.id, bot)
    try:
        await callback.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay_prodamus_"))
async def pay_prodamus(callback: CallbackQuery):
    payload_str = callback.data[len("pay_prodamus_"):]
    token_or_plan_id = payload_str
    async with async_session_maker() as session:
        config = await _ensure_subscription_config(session)
        intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token_or_plan_id))
        if not intent and "_" in token_or_plan_id:
            first_part = token_or_plan_id.split("_")[0]
            candidate_intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == first_part))
            if candidate_intent:
                intent = candidate_intent
                token_or_plan_id = first_part
            else:
                token_or_plan_id = first_part
        if intent:
            if intent.user_id != callback.from_user.id:
                await callback.answer("Заказ принадлежит другому пользователю.", show_alert=True)
                return
            if intent.status in ("paid", "settling", "cancelled", "expired"):
                await callback.answer(f"Заказ уже {intent.status}. Пожалуйста, выбери тариф заново.", show_alert=True)
                return
            now = msk_now()
            if intent.expires_at and intent.expires_at < now:
                intent.status = "expired"
                await session.commit()
                await callback.answer("Срок действия заказа истёк, выбери тариф заново.", show_alert=True)
                return

            plan = await session.get(
                SubscriptionPlan,
                intent.plan_id,
                options=[selectinload(SubscriptionPlan.upgrades_to_plan)]
            )
        else:
            try:
                plan_id = int(token_or_plan_id)
                plan = await session.get(
                    SubscriptionPlan,
                    plan_id,
                    options=[selectinload(SubscriptionPlan.upgrades_to_plan)]
                )
            except Exception:
                plan = None

        if not plan or not config.prodamus_url or not config.prodamus_secret_key:
            await callback.answer("Prodamus не настроен полностью.", show_alert=True)
            return

        target_plan = plan.upgrades_to_plan if plan.is_trial and plan.upgrades_to_plan else plan
        if target_plan.allow_auto_renewal and not target_plan.prodamus_subscription_id:
            await callback.answer("Для тарифа не задан код подписки в Prodamus.", show_alert=True)
            return

        if intent:
            # Atomic provider claim and idempotency guard
            if intent.provider == "prodamus":
                pass
            elif intent.provider in ("pending_choice", "checkout"):
                claim_res = await session.execute(
                    update(PaymentIntent)
                    .where(
                        PaymentIntent.id == intent.id,
                        PaymentIntent.user_id == callback.from_user.id,
                        PaymentIntent.status == "pending",
                        PaymentIntent.provider.in_(["pending_choice", "checkout"]),
                    )
                    .values(
                        provider="prodamus",
                        expires_at=None,
                    )
                )
                await session.commit()
                if claim_res.rowcount == 0:
                    fresh = await session.get(PaymentIntent, intent.id)
                    if fresh and fresh.provider == "telegram_pay":
                        await callback.answer("Для этого заказа уже выбран Telegram Pay.", show_alert=True)
                    else:
                        await callback.answer("Заказ более недоступен. Выбери тариф заново.", show_alert=True)
                    return
                intent.provider = "prodamus"
                intent.expires_at = None
            else:
                await callback.answer("Для этого заказа уже выбран другой способ оплаты.", show_alert=True)
                return
        else:
            intent = await create_payment_intent(session, callback.from_user.id, plan.id, provider="prodamus", bot=callback.bot)

        if intent.final_amount <= 0.0:
            builder = InlineKeyboardBuilder()
            builder.button(text="⬅️ Главное меню", callback_data="back_to_main_menu_text")
            await callback.message.edit_text(
                f"✅ Доступ к образовательной программе по тарифу «{plan.name}» успешно оформлен за счет баллов!",
                reply_markup=builder.as_markup()
            )
            await callback.answer()
            return

        final_price = float(intent.final_amount)
        order_id = f"pi_{intent.token}"
        intent_token = str(intent.token)
        intent_id = int(intent.id)
        target_sub_id = target_plan.prodamus_subscription_id
        target_autopay_limit = target_plan.prodamus_autopayments_limit
        is_trial = bool(plan.is_trial)
        plan_duration_value = int(plan.duration_value) if plan.duration_value else 0
        plan_id = int(plan.id)
        plan_name = str(plan.name)
        target_plan_name = str(target_plan.name)
        prodamus_url = str(config.prodamus_url or "").strip()
        prodamus_secret_key = str(config.prodamus_secret_key or "").strip()
        prodamus_paid_content = (config.prodamus_paid_content or "").strip()

        existing_pmt = await session.scalar(
            select(ProdamusPayment).where(ProdamusPayment.order_id == order_id)
        )
        if not existing_pmt:
            session.add(ProdamusPayment(
                user_id=callback.from_user.id,
                plan_id=plan_id,
                amount=final_price,
                order_id=order_id,
                prodamus_subscription_id=target_sub_id,
                event_type="link_created",
                status="pending",
            ))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing_pmt = await session.scalar(
                    select(ProdamusPayment).where(ProdamusPayment.order_id == order_id)
                )

    try:
        payload = {
            "do": "pay",
            "order_id": order_id,
            "products": [{
                "name": target_plan_name,
                "price": final_price,
                "quantity": 1,
                "type": "service",
            }],
            "order_sum": final_price,
            "customer_extra": f"pi_token={intent_token};user_id={callback.from_user.id};plan_id={plan_id}",
        }
        if prodamus_paid_content:
            payload["paid_content"] = prodamus_paid_content
        if target_sub_id:
            payload["subscription"] = target_sub_id
        if target_sub_id and target_autopay_limit:
            payload["subscription_limit_autopayments"] = target_autopay_limit
        if target_sub_id and is_trial and plan_duration_value > 0:
            payload["subscription_demo_period"] = plan_duration_value
        payload["signature"] = _build_prodamus_signature(payload, prodamus_secret_key)
        query = urlencode(_flatten_prodamus_query("", payload))
        pay_url = f"{prodamus_url}{'&' if '?' in prodamus_url else '?'}{query}"
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Оплатить через Prodamus", url=pay_url)
        builder.button(text="⬅️ Назад", callback_data=f"select_plan_{plan_id}")
        builder.adjust(1)
        await callback.message.edit_text(
            f"Ссылка на оплату доступа к образовательной программе по тарифу <b>{html.escape(plan_name)}</b> готова.\n\n"
            f"Сумма: <b>{final_price:.2f} руб.</b>\n"
            f"Подписка Prodamus: <b>{target_sub_id or 'одноразовая оплата'}</b>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        await callback.answer()
    except Exception as e:
        payments_logger.exception("Prodamus link generation failed: %s", e)
        if intent_id:
            async with async_session_maker() as cleanup_session:
                await cleanup_session.execute(
                    update(PaymentIntent)
                    .where(PaymentIntent.id == intent_id, PaymentIntent.status == "pending")
                    .values(status="cancelled")
                )
                await cleanup_session.commit()
        await callback.answer("Ошибка при создании ссылки на оплату. Попробуй позже.", show_alert=True)


@router.callback_query(F.data == "admin_subscribers")
@router.callback_query(F.data == "admin_subs_list")
@router.callback_query(F.data.startswith("admin_subs_"))
@router.callback_query(F.data.startswith("admin_subs_list_"))
async def admin_subscribers(callback: CallbackQuery, state: FSMContext | None = None):
    if not await require_permission(callback, AdminPermission.PAYMENTS, state=state):
        return
    await _clear_admin_navigation_state(state)
    if callback.data in {"admin_subscribers", "admin_subs_list"}:
        page = 0
        filter_key = "all"
    else:
        parts = callback.data.split("_")
        if parts[2] == "list":
            page = int(parts[3])
            filter_key = parts[4] if len(parts) > 4 else "all"
        else:
            page = int(parts[2])
            filter_key = parts[3] if len(parts) > 3 else "all"
    page_size = 10
    now = msk_now()
    filters = {"all": None, "paid": False, "bonus": True}

    async with async_session_maker() as session:
        query = select(UserSubscription).options(selectinload(UserSubscription.plan)).order_by(UserSubscription.end_date.asc())
        if filters.get(filter_key) is True:
            query = query.where(UserSubscription.plan_id == None, UserSubscription.end_date > now)
        elif filters.get(filter_key) is False:
            query = query.where(UserSubscription.plan_id != None, UserSubscription.end_date > now)
        else:
            query = query.where(UserSubscription.end_date > now)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        rows = (await session.execute(query.offset(page * page_size).limit(page_size))).scalars().all()

        users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_([row.user_id for row in rows])))).scalars().all()} if rows else {}

    lines = [f"👥 Подписчики: {total} · стр. {page + 1}/{total_pages}", ""]
    for sub in rows:
        user = users.get(sub.user_id)
        name = html.escape(user.first_name) if user and user.first_name else str(sub.user_id)
        username = f" @{html.escape(user.username)}" if user and user.username else ""
        plan_name = html.escape(sub.plan.name) if sub.plan and sub.plan.name else html.escape(sub.payment_provider or "Бонус")
        lines.append(f"• {name}{username} [{sub.user_id}] · {plan_name} · до {format_msk(sub.end_date, '%d.%m.%Y %H:%M')}")
    if not rows:
        lines.append("Нет активных записей.")

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Все", callback_data="admin_subs_0_all"),
        InlineKeyboardButton(text="Платные", callback_data="admin_subs_0_paid"),
        InlineKeyboardButton(text="Бонусные", callback_data="admin_subs_0_bonus"),
    )
    builder.row(*kb.standard_pagination_row(
        page,
        total_pages,
        lambda target: f"admin_subs_{target}_{filter_key}",
    ))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_payment_settings"))
    await callback.message.edit_text("\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_plog_"))
async def admin_payment_log(callback: CallbackQuery, state: FSMContext | None = None):
    await _clear_admin_navigation_state(state)
    parts = callback.data.split("_")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    filter_key = parts[3] if len(parts) > 3 else "all"
    await _render_payment_log(callback, page, filter_key)


def _shorten_payment_log_entry(line: str, limit: int = _PLOG_ENTRY_MAX_LEN) -> str:
    normalized_line = re.sub(r"</?[^>]+>", "", line.strip())
    if len(normalized_line) <= limit:
        return normalized_line
    return f"{normalized_line[:limit - 3]}..."


async def _render_payment_log(callback: CallbackQuery, page: int, filter_key: str):
    if filter_key not in _PLOG_FILTERS:
        filter_key = "all"

    def empty_log_keyboard():
        builder = InlineKeyboardBuilder()
        builder.row(*kb.standard_pagination_row(0, 1, lambda target: f"admin_plog_{target}_{filter_key}"))
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_payment_settings"))
        return builder.as_markup()

    log_page_size = 10
    max_log_lines = 400
    log_path = Path("logs/payments.log")

    if not log_path.exists():
        await callback.message.edit_text(
            "📋 Лог пуст, платёжных событий ещё не было.",
            reply_markup=empty_log_keyboard(),
        )
        await callback.answer()
        return

    try:
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        await callback.message.edit_text(
            "Не удалось прочитать лог платежей.",
            reply_markup=empty_log_keyboard(),
        )
        await callback.answer()
        return

    recent_lines = list(reversed(all_lines[-max_log_lines:]))
    keywords, filter_label = _PLOG_FILTERS[filter_key]
    if keywords:
        recent_lines = [line for line in recent_lines if any(keyword in line for keyword in keywords)]

    if not recent_lines:
        filtered_text = f"📋 Лог платежей · {filter_label}\n\nЗаписей по этому фильтру нет."
        total_pages = 1
    else:
        total_pages = max(1, math.ceil(len(recent_lines) / log_page_size))
        page = min(page, total_pages - 1)
        start = page * log_page_size
        page_lines = recent_lines[start:start + log_page_size]
        separator = "─" * 38
        entries = [_shorten_payment_log_entry(line) for line in page_lines if line.strip()]
        log_body = f"\n{separator}\n".join(entries)
        max_body_len = _PLOG_MESSAGE_MAX_LEN - 120
        if len(log_body) > max_body_len:
            log_body = f"{log_body[:max_body_len - 4]} ..."
        filtered_text = (
            f"📋 Лог платежей · {filter_label} · стр. {page + 1}/{total_pages}\n\n"
            f"<pre>{html.escape(log_body)}</pre>"
        )

    builder = InlineKeyboardBuilder()
    row1 = []
    row2 = []
    for index, (key, (_, label)) in enumerate(_PLOG_FILTERS.items()):
        text = f"· {label} ·" if key == filter_key else label
        button = InlineKeyboardButton(text=text, callback_data=f"admin_plog_0_{key}")
        if index < 3:
            row1.append(button)
        else:
            row2.append(button)
    if row1:
        builder.row(*row1)
    if row2:
        builder.row(*row2)

    builder.row(*kb.standard_pagination_row(
        page,
        total_pages,
        lambda target: f"admin_plog_{target}_{filter_key}",
    ))

    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_payment_settings"))
    await callback.message.edit_text(filtered_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.in_({"noop", "ignore"}))
async def noop_callback(callback: CallbackQuery):
    await callback.answer()


async def _render_admin_clients_list(
    callback: CallbackQuery,
    state: FSMContext,
    requested_page: int,
    context_value: str | None = None,
    parent_callback: str | None = None,
):
    context = await _load_admin_client_context(callback, context_value)
    if context_value and context is None:
        await callback.answer("Экран списка устарел. Откройте список клиентов заново.", show_alert=True)
        return
    await state.set_state(None)
    page = requested_page
    limit = 10
    offset = page * limit
    data = await state.get_data()
    search_query = (
        (context.search_query or "").strip()
        if context is not None
        else (data.get("client_search_query") or "").strip()
    )
    reset_callback = (
        context.reset_callback
        if context is not None
        else _admin_client_return_callback(data.get("admin_client_search_reset_callback"))
    )
    reset_query = (
        context.reset_query
        if context is not None
        else data.get("admin_client_search_reset_query")
    )
    parent_callback = _admin_client_parent_callback(
        context.parent_callback if context is not None else parent_callback
    )

    async with async_session_maker() as session:
        base_query = select(User)
        count_query = select(func.count(User.id))

        if search_query:
            search_filter = or_(
                User.first_name.ilike(f"%{search_query}%"),
                User.name.ilike(f"%{search_query}%"),
                User.username.ilike(f"%{search_query}%"),
                cast(User.id, String).ilike(f"%{search_query}%"),
            )
            base_query = base_query.where(search_filter)
            count_query = count_query.select_from(User).where(search_filter)
        else:
            count_query = count_query.select_from(User)

        total_users = await session.scalar(count_query)
        total_pages = max(1, (total_users + limit - 1) // limit)
        page = max(0, min(page, total_pages - 1))
        offset = page * limit

        users_result = await session.execute(
            base_query.order_by(User.created_at.desc()).limit(limit).offset(offset)
        )
        users = users_result.scalars().all()

    current_callback = f"admin_clients_list_{page}"
    if not search_query or not reset_callback:
        reset_callback = current_callback
        reset_query = None
    context_token = await _create_admin_client_context(
        callback,
        return_callback=current_callback,
        search_query=search_query,
        reset_callback=reset_callback,
        reset_query=reset_query,
        parent_callback=parent_callback,
    )
    await state.update_data(
        admin_client_return_callback=current_callback,
        admin_client_parent_callback=parent_callback,
        client_search_query=search_query or None,
        admin_client_search_reset_callback=reset_callback if search_query else None,
        admin_client_search_reset_query=reset_query if search_query else None,
        admin_client_navigation_token=context_token,
    )

    if search_query:
        text = f"🔎 <b>Результаты поиска:</b> {html.escape(search_query)}\nНайдено: {total_users}\nСтраница {page + 1} из {total_pages}"
    else:
        text = f"👥 <b>Список клиентов ({total_users} чел.)</b>\nСтраница {page + 1} из {total_pages}"

    rendered = await send_smart_content(
        callback,
        text,
        None,
        kb.admin_clients_keyboard(
            users,
            page,
            total_pages,
            is_searching=bool(search_query),
            navigation_token=context_token,
        ),
    )
    await _bind_admin_client_context_message(context_token, callback, rendered)


@router.callback_query(F.data.startswith("admin_clients_list_"))
async def admin_clients_list(
    callback: CallbackQuery,
    state: FSMContext,
    parent_callback: str | None = None,
):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    parsed_callback = (0, None) if callback.data == "admin_section_clients" else _parse_admin_client_list_callback(callback.data)
    if parsed_callback is None:
        await callback.answer("Некорректная страница списка клиентов.", show_alert=True)
        return
    requested_page, context_value = parsed_callback
    state_data = await state.get_data()
    if parent_callback is None and callback.data in {"admin_section_clients", "admin_clients_list_0"}:
        parent_callback = "admin_panel"
    await _render_admin_clients_list(
        callback,
        state,
        requested_page,
        context_value,
        parent_callback or state_data.get("admin_client_parent_callback"),
    )


@router.callback_query(F.data.regexp(r"^admin_clients_back:clientctx_[0-9a-f]{16}$"))
async def admin_clients_back(callback: CallbackQuery, state: FSMContext):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    _, _, context_value = (callback.data or "").partition(":")
    context = await _load_admin_client_context(callback, context_value)
    if context is None:
        await callback.answer("Экран списка устарел. Откройте список клиентов заново.", show_alert=True)
        return
    parent_callback = _admin_client_parent_callback(context.parent_callback)
    await state.clear()
    if parent_callback == "admin_panel":
        await admin_panel_cb(callback, state=state)
        return
    if _parse_admin_client_list_callback(parent_callback) is not None:
        await admin_clients_list(
            callback.model_copy(update={"data": parent_callback}),
            state,
            parent_callback=parent_callback,
        )
        return
    await admin_panel_cb(callback, state=state)


@router.callback_query(F.data.startswith("view_client_"))
async def legacy_view_client(callback: CallbackQuery, state: FSMContext | None = None):
    try:
        user_id = int(callback.data.removeprefix("view_client_"))
    except (TypeError, ValueError):
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    await admin_user_profile(
        callback.model_copy(update={"data": f"admin_user_profile_{user_id}"}),
        state=state,
    )


@router.callback_query(F.data.regexp(r"^admin_clients_start_search(?::clientctx_[0-9a-f]{16})?$"))
async def start_client_search(callback: CallbackQuery, state: FSMContext):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    _, _, context_value = (callback.data or "").partition(":")
    context = await _load_admin_client_context(callback, context_value or None)
    if context_value and context is None:
        await callback.answer("Экран списка устарел. Откройте список клиентов заново.", show_alert=True)
        return
    state_data = await state.get_data()
    return_callback = (
        context.return_callback
        if context is not None
        else _admin_client_return_callback(state_data.get("admin_client_return_callback"))
    ) or "admin_clients_list_0"
    return_query = context.search_query if context is not None else (state_data.get("client_search_query") or None)
    parent_callback = _admin_client_parent_callback(
        context.parent_callback if context is not None else state_data.get("admin_client_parent_callback")
    )
    await state.set_state(AdminStates.search_client)
    await state.update_data(
        message_id_to_edit=callback.message.message_id,
        admin_client_search_input_return_callback=return_callback,
        admin_client_search_input_return_query=return_query,
        admin_client_parent_callback=parent_callback,
    )
    cancel_callback = (
        f"admin_clients_cancel_search:{context_value}"
        if context_value
        else return_callback
    )
    await callback.message.edit_text(
        "Введите имя, username или ID клиента для поиска:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Отмена поиска", callback_data=cancel_callback),
        ]]),
    )
    await callback.answer()


@router.message(AdminStates.search_client, F.text)
async def process_client_search(message: Message, state: FSMContext, bot: Bot):
    if not await is_admin_actor(message.from_user.id):
        return
    query = message.text.strip()
    data = await state.get_data()
    return_callback = _admin_client_return_callback(
        data.get("admin_client_search_input_return_callback")
    ) or _admin_client_return_callback(data.get("admin_client_return_callback")) or "admin_clients_list_0"
    return_query = data.get("admin_client_search_input_return_query")
    parent_callback = _admin_client_parent_callback(data.get("admin_client_parent_callback"))
    await state.update_data(
        client_search_query=query,
        admin_client_search_reset_callback=return_callback,
        admin_client_search_reset_query=return_query,
    )
    data = await state.get_data()
    message_id_to_edit = data.get("message_id_to_edit")
    await message.delete()
    await state.set_state(None)

    callback_mock = type("obj", (object,), {
        "message": type("obj", (object,), {
            "chat": message.chat,
            "bot": bot,
            "message_id": message_id_to_edit,
            "edit_text": lambda *args, **kwargs: bot.edit_message_text(chat_id=message.chat.id, message_id=message_id_to_edit, *args, **kwargs),
            "answer": lambda *args, **kwargs: bot.send_message(chat_id=message.chat.id, *args, **kwargs),
        }),
        "bot": bot,
        "data": "admin_clients_list_0",
        "from_user": message.from_user,
        "answer": lambda *args, **kwargs: None,
    })()

    await _render_admin_clients_list(callback_mock, state, 0, parent_callback=parent_callback)


@router.callback_query(F.data.regexp(r"^admin_clients_cancel_search(?::clientctx_[0-9a-f]{16})?$"))
async def cancel_client_search(callback: CallbackQuery, state: FSMContext):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    _, _, context_value = (callback.data or "").partition(":")
    context = await _load_admin_client_context(callback, context_value or None)
    if context_value and context is None:
        await callback.answer("Экран поиска устарел. Откройте список клиентов заново.", show_alert=True)
        return
    state_data = await state.get_data()
    return_callback = (
        context.return_callback
        if context is not None
        else _admin_client_return_callback(state_data.get("admin_client_search_input_return_callback"))
    ) or "admin_clients_list_0"
    return_query = context.search_query if context is not None else state_data.get("client_search_query")
    parent_callback = _admin_client_parent_callback(
        context.parent_callback if context is not None else state_data.get("admin_client_parent_callback")
    )
    await state.clear()
    await state.update_data(
        client_search_query=return_query or None,
        admin_client_search_reset_callback=(
            context.reset_callback if context is not None else None
        ),
        admin_client_search_reset_query=(
            context.reset_query if context is not None else None
        ),
        admin_client_parent_callback=parent_callback,
    )
    parsed_return = _parse_admin_client_list_callback(return_callback)
    await _render_admin_clients_list(
        callback,
        state,
        parsed_return[0] if parsed_return is not None else 0,
        parent_callback=parent_callback,
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_clients_reset_search(?::clientctx_[0-9a-f]{16})?$"))
async def reset_client_search(callback: CallbackQuery, state: FSMContext):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    _, _, context_value = (callback.data or "").partition(":")
    context = await _load_admin_client_context(callback, context_value or None)
    if context_value and context is None:
        await callback.answer("Экран списка устарел. Откройте список клиентов заново.", show_alert=True)
        return
    state_data = await state.get_data()
    return_callback = (
        context.reset_callback
        if context is not None
        else _admin_client_return_callback(state_data.get("admin_client_search_reset_callback"))
    ) or "admin_clients_list_0"
    return_query = context.reset_query if context is not None else None
    parent_callback = _admin_client_parent_callback(
        context.parent_callback if context is not None else state_data.get("admin_client_parent_callback")
    )
    await state.clear()
    await state.update_data(
        client_search_query=return_query,
        admin_client_search_reset_callback=None,
        admin_client_search_reset_query=None,
        admin_client_parent_callback=parent_callback,
    )
    parsed_return = _parse_admin_client_list_callback(return_callback)
    await _render_admin_clients_list(
        callback,
        state,
        parsed_return[0] if parsed_return is not None else 0,
        parent_callback=parent_callback,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_user_profile_"))
async def admin_user_profile(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    payload = callback.data.removeprefix("admin_user_profile_")
    user_id_raw, _, callback_return = payload.partition(":")
    client_context = await _load_admin_client_context(callback, callback_return)
    if _admin_client_context_token(callback_return) and client_context is None:
        await callback.answer("Экран списка устарел. Откройте список клиентов заново.", show_alert=True)
        return
    return_callback = (
        _admin_client_return_callback(client_context.return_callback)
        if client_context is not None
        else _admin_client_return_callback(callback_return)
    ) or "admin_clients_list_0"
    context_token = _admin_client_context_token(callback_return)
    if state is not None:
        state_data = await state.get_data()
        if client_context is None and not _admin_client_return_callback(callback_return):
            return_callback = _admin_client_return_callback(
                state_data.get("admin_client_return_callback")
            ) or return_callback
        await state.set_state(None)
        await state.update_data(
            admin_client_return_callback=return_callback,
            admin_client_parent_callback=_admin_client_parent_callback(
                client_context.parent_callback
                if client_context is not None
                else state_data.get("admin_client_parent_callback")
            ),
            client_search_query=(
                client_context.search_query
                if client_context is not None
                else state_data.get("client_search_query")
            ),
            admin_client_search_reset_callback=(
                client_context.reset_callback if client_context is not None
                else state_data.get("admin_client_search_reset_callback")
            ),
            admin_client_search_reset_query=(
                client_context.reset_query if client_context is not None
                else state_data.get("admin_client_search_reset_query")
            ),
            admin_client_navigation_token=(
                callback_return if client_context is not None else state_data.get("admin_client_navigation_token")
            ),
        )

    profile_return_callback = return_callback
    parsed_return = _parse_admin_client_list_callback(return_callback)
    if client_context is not None and context_token and parsed_return is not None:
        profile_return_callback = _admin_client_list_callback(
            parsed_return[0],
            callback_return,
        )

    try:
        user_id = int(user_id_raw)
    except ValueError:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user:
            await callback.answer("Пользователь не найден")
            return

        landing_total = await session.scalar(
            select(func.count(FunnelLanding.id)).where(FunnelLanding.user_id == user_id)
        )
        landing_paid = await session.scalar(
            select(func.count(FunnelLanding.id)).where(FunnelLanding.user_id == user_id, FunnelLanding.status == 'paid')
        )
        last_landing = await session.scalar(
            select(FunnelLanding).where(FunnelLanding.user_id == user_id).order_by(FunnelLanding.id.desc())
        )
        msg_count = await session.scalar(
            select(func.count(DBMessage.id)).where(DBMessage.user_id == user_id)
        )
        habit_count = await session.scalar(
            select(func.count(Habit.id)).where(Habit.user_id == user_id)
        )
        funnel_state = await session.get(FunnelState, user_id)

        reg_str = format_msk(user.created_at, "%d-%m-%Y %H:%M")
        username_txt = f"@{user.username}" if user.username else "нет"
        name_communication = user.name if user.name else "не задано"
        gender_txt = user.gender if user.gender else "не задан"
        sub_txt = format_msk(user.subscription_end, "%d-%m-%Y %H:%M") if user.subscription_end else "нет"
        funnel_stage_labels = {
            "idle": "Не запущена",
            "intro": "Старт воронки",
            "choice": "Выбор практики",
            "meditation_card": "Карточка практики",
            "listening": "Прослушивание практики",
            "waiting_reflection": "Ожидается ответ после практики",
            "post_analysis": "После AI-разбора",
            "vitaly_method": "Блок о методе Виталия",
            "strategy_offer": "Оффер стратегической сессии",
            "strategy_faq": "FAQ по сессии",
            "strategy_chat": "Диалог по сессии",
            "objection_chat": "Диалог по возражениям",
        }
        funnel_txt = funnel_stage_labels.get(funnel_state.stage, funnel_state.stage) if funnel_state else "нет"
        funnel_active = "да" if funnel_state and funnel_state.is_active else "нет"
        landing_status_labels = {"paid": "оплачено", "opened": "открыта", "failed": "не оплачена"}
        crisis_txt = (
            f"🚨 Кризисный флаг: активен с {format_msk(user.crisis_detected_at, '%d-%m-%Y %H:%M')}\n"
            if user.crisis_detected_at
            else "Кризисный флаг: нет\n"
        )

        text = (
            f"👤 <b>Карточка клиента</b>\n\n"
            f"ID клиента: <code>{user.id}</code>\n"
            f"Имя для общения: {name_communication}\n"
            f"Пол: {gender_txt}\n"
            f"Имя в Telegram: {user.first_name}\n"
            f"Ник в Telegram: {username_txt}\n"
            f"Дата регистрации: {reg_str}\n\n"
            f"💬 Сообщений: {msg_count or 0}\n"
            f"💰 Баланс: {user.balance}\n"
            f"📅 Подписка до: {sub_txt}\n"
            f"🔔 Напоминаний отправлено: {user.main_reminder_count}\n\n"
            f"🌀 Текущий этап сценария: {funnel_txt}\n"
            f"🌀 Сценарий активен: {funnel_active}\n"
            f"🎗 Привычек: {habit_count or 0}\n"
            f"🌐 Переходов на страницу оплаты: {landing_total or 0}\n"
            f"🌐 Оплат со страницы: {landing_paid or 0}\n"
            f"Последний статус заявки: {landing_status_labels.get(last_landing.status, last_landing.status) if last_landing else 'нет'}\n\n"
            f"{crisis_txt}"
            f"Заблокирован: {'да' if user.is_blocked else 'нет'}"
        )

        is_blocked = user.is_blocked
        crisis_detected = bool(user.crisis_detected_at)

    await send_smart_content(
        callback,
        text,
        None,
        kb.admin_user_profile_keyboard(
            user_id,
            is_blocked,
            crisis_detected,
            profile_return_callback,
            navigation_token=callback_return if client_context is not None else None,
        ),
    )


@router.callback_query(F.data.startswith("admin_block_toggle_"))
async def admin_block_toggle(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.set_state(None)
    payload = callback.data.removeprefix("admin_block_toggle_")
    user_id_raw, _, context_value = payload.partition(":")
    client_context = await _load_admin_client_context(callback, context_value or None)
    if context_value and client_context is None:
        await callback.answer("Экран профиля устарел. Откройте список клиентов заново.", show_alert=True)
        return
    try:
        user_id = int(user_id_raw)
    except ValueError:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user:
            user.is_blocked = not user.is_blocked
            await session.commit()

            status = "заблокирован" if user.is_blocked else "разблокирован"
            await callback.answer(f"Клиент {status}")

    new_cb = callback.model_copy(update={
        "data": f"admin_user_profile_{user_id}{_admin_client_action_suffix(callback.data)}",
    })
    await admin_user_profile(new_cb, state=state)


@router.callback_query(F.data.startswith("admin_crisis_clear_"))
async def admin_crisis_clear(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.set_state(None)
    context_value = _admin_client_action_context(callback.data)
    client_context = await _load_admin_client_context(callback, context_value)
    if context_value and client_context is None:
        await callback.answer("Экран профиля устарел. Откройте список клиентов заново.", show_alert=True)
        return
    confirmed = callback.data.startswith("admin_crisis_clear_confirm_") or not isinstance(callback, CallbackQuery)
    prefix = (
        "admin_crisis_clear_confirm_"
        if callback.data.startswith("admin_crisis_clear_confirm_")
        else "admin_crisis_clear_"
    )
    user_id_raw = callback.data.removeprefix(prefix).split(":", 1)[0]
    try:
        user_id = int(user_id_raw)
    except ValueError:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        if not confirmed:
            await _ask_admin_confirmation(
                callback,
                "Кризисный флаг будет снят вручную. После нового кризисного сигнала защита снова включится.",
                f"admin_crisis_clear_confirm_{user_id}{_admin_client_action_suffix(callback.data)}",
                f"admin_user_profile_{user_id}{_admin_client_action_suffix(callback.data)}",
            )
            return
        user.crisis_detected_at = None
        await session.commit()
    await callback.answer("Кризисный флаг снят вручную")
    await admin_user_profile(
        callback.model_copy(update={
            "data": f"admin_user_profile_{user_id}{_admin_client_action_suffix(callback.data)}",
        }),
        state=state,
    )


# ───── Меню сбросов клиента (для тестирования) ─────

_USER_RESET_LABELS = {
    "funnel": "состояние воронки",
    "dialog": "история диалога и активные сессии",
    "sub": "подписка клиента",
    "balance": "баланс клиента",
    "habits": "привычки и прогресс клиента",
    "remind": "счётчик напоминаний",
    "landing": "историю переходов и оплат на странице",
    "name": "имя и пол клиента",
    "full": "все данные клиента",
}


async def _ensure_user_reset_confirmed(
    callback: CallbackQuery,
    kind: str,
    user_id: int,
    state: FSMContext | None = None,
) -> bool:
    await _clear_admin_navigation_state(state, preserve_client_context=True)
    if callback.data.startswith("ureset_confirmed_") or not isinstance(callback, CallbackQuery):
        return True
    await _ask_admin_confirmation(
        callback,
        f"Будет сброшено: <b>{_USER_RESET_LABELS[kind]}</b>. Действие нельзя отменить.",
        f"ureset_confirmed_{kind}_{user_id}",
        f"admin_user_resets_{user_id}",
    )
    return False

@router.callback_query(F.data.startswith("admin_user_resets_"))
async def admin_user_resets(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    context_value = _admin_client_action_context(callback.data)
    client_context = await _load_admin_client_context(callback, context_value)
    if context_value and client_context is None:
        await callback.answer("Экран профиля устарел. Откройте список клиентов заново.", show_alert=True)
        return
    await _clear_admin_navigation_state(state, preserve_client_context=True)

    try:
        user_id = int(callback.data.removeprefix("admin_user_resets_").split(":", 1)[0])
    except ValueError:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    if state is not None and client_context is not None:
        await state.update_data(
            admin_client_return_callback=client_context.return_callback,
            admin_client_parent_callback=_admin_client_parent_callback(client_context.parent_callback),
            client_search_query=client_context.search_query,
            admin_client_search_reset_callback=client_context.reset_callback,
            admin_client_search_reset_query=client_context.reset_query,
            admin_client_navigation_token=context_value,
        )

    text = (
        f"🔄 <b>Сбросы для клиента</b> <code>{user_id}</code>\n\n"
        f"Здесь можно сбросить отдельные данные клиента, чтобы протестировать бота "
        f"с одного и того же аккаунта как будто с нуля.\n\n"
        f"<b>Что можно сбросить:</b>\n\n"
        f"🌀 <b>Воронка</b>\nСбрасывает состояние воронки гипносессий. "
        f"Клиент снова начнёт путь по воронке с самого начала.\n\n"
        f"💬 <b>Диалог</b>\nУдаляет всю историю переписки с ботом. "
        f"Бот «забудет» все прошлые сообщения клиента.\n\n"
        f"📅 <b>Подписка</b>\nСнимает активную подписку. "
        f"Клиент потеряет доступ к платному контенту (если режим подписки включён).\n\n"
        f"💰 <b>Баланс</b>\nОбнуляет баланс токенов. "
        f"В режиме оплаты за сообщения клиент не сможет писать, пока не пополнит.\n\n"
        f"🎗 <b>Привычки</b>\nУдаляет все привычки клиента и их прогресс.\n\n"
        f"🔔 <b>Напоминания</b>\nСбрасывает счётчик отправленных напоминаний. "
        f"Клиент снова получит первое напоминание.\n\n"
        f"🌐 <b>Страница оплаты</b>\nУдаляет все записи о переходах и оплатах со страницы.\n\n"
        f"👤 <b>Имя и пол</b>\nСбрасывает имя для общения и пол. "
        f"Бот заново спросит клиента как его зовут.\n\n"
        f"⚡️ <b>Полный сброс</b>\nСбрасывает ВСЁ перечисленное выше одной кнопкой."
    )

    builder = InlineKeyboardBuilder()
    reset_buttons = [
        ("🌀 Сбросить воронку", f"confirm_ureset_funnel_{user_id}{_admin_client_action_suffix(callback.data)}"),
        ("💬 Очистить диалог", f"confirm_ureset_dialog_{user_id}{_admin_client_action_suffix(callback.data)}"),
        ("📅 Сбросить подписку", f"confirm_ureset_sub_{user_id}{_admin_client_action_suffix(callback.data)}"),
        ("💰 Обнулить баланс", f"confirm_ureset_balance_{user_id}{_admin_client_action_suffix(callback.data)}"),
        ("🎗 Удалить привычки", f"confirm_ureset_habits_{user_id}{_admin_client_action_suffix(callback.data)}"),
        ("🔔 Сбросить напоминания", f"confirm_ureset_remind_{user_id}{_admin_client_action_suffix(callback.data)}"),
        ("🌐 Очистить лендинг", f"confirm_ureset_landing_{user_id}{_admin_client_action_suffix(callback.data)}"),
        ("👤 Сбросить имя и пол", f"confirm_ureset_name_{user_id}{_admin_client_action_suffix(callback.data)}"),
        ("⚡️ ПОЛНЫЙ СБРОС", f"confirm_ureset_full_{user_id}{_admin_client_action_suffix(callback.data)}"),
    ]
    role = await get_admin_role(callback.from_user.id)
    for text, callback_data in reset_buttons:
        permission = permission_for_callback(callback_data)
        if role is None or permission is None or role_has_permission(role, permission):
            builder.button(text=text, callback_data=callback_data)
    builder.button(
        text="⬅️ Назад",
        callback_data=f"admin_user_profile_{user_id}{_admin_client_action_suffix(callback.data)}",
    )
    builder.adjust(1)

    await send_smart_content(callback, text, None, builder.as_markup())


@router.callback_query(F.data.startswith("confirm_ureset_"))
async def confirm_user_reset(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await _clear_admin_navigation_state(state, preserve_client_context=True)
    payload = callback.data.removeprefix("confirm_ureset_")
    payload_without_context = payload.split(":", 1)[0]
    kind, separator, user_id_raw = payload_without_context.rpartition("_")
    if not separator or kind not in _USER_RESET_LABELS:
        await callback.answer("Некорректный сброс.", show_alert=True)
        return
    try:
        user_id = int(user_id_raw)
    except ValueError:
        await callback.answer("Некорректный клиент.", show_alert=True)
        return
    await _ask_admin_confirmation(
        callback,
        f"Будет сброшено: <b>{_USER_RESET_LABELS[kind]}</b>. Действие нельзя отменить.",
        f"ureset_confirmed_{kind}_{user_id}{_admin_client_action_suffix(callback.data)}",
        f"admin_user_resets_{user_id}{_admin_client_action_suffix(callback.data)}",
    )


@router.callback_query(F.data.startswith("ureset_confirmed_"))
async def execute_confirmed_user_reset(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await _clear_admin_navigation_state(state, preserve_client_context=True)
    payload = callback.data.removeprefix("ureset_confirmed_")
    payload_without_context = payload.split(":", 1)[0]
    kind, separator, user_id_raw = payload_without_context.rpartition("_")
    reset_handlers = {
        "funnel": ureset_funnel,
        "dialog": ureset_dialog,
        "sub": ureset_sub,
        "balance": ureset_balance,
        "habits": ureset_habits,
        "remind": ureset_remind,
        "landing": ureset_landing,
        "name": ureset_name,
        "full": ureset_full,
    }
    if not separator or kind not in reset_handlers:
        await callback.answer("Некорректный сброс.", show_alert=True)
        return
    try:
        user_id = int(user_id_raw)
    except ValueError:
        await callback.answer("Некорректный клиент.", show_alert=True)
        return
    confirmed_callback = callback.model_copy(update={
        "data": f"ureset_confirmed_{kind}_{user_id}{_admin_client_action_suffix(callback.data)}",
    })
    await reset_handlers[kind](confirmed_callback, state=state)


@router.callback_query(F.data.startswith("ureset_funnel_"))
async def ureset_funnel(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_id = _admin_client_action_user_id(callback.data, "ureset_funnel_")
    if not await _ensure_user_reset_confirmed(callback, "funnel", user_id, state):
        return
    async with async_session_maker() as session:
        fs = await session.get(FunnelState, user_id)
        if fs:
            fs.is_active = False
            fs.stage = "idle"
            fs.meditation_id = None
            fs.next_followup_at = None
            fs.reminder_count = 0
            fs.last_ai_reply = None
            fs.completed_at = None
            fs.started_at = None
        await session.commit()
    await callback.answer("🌀 Воронка сброшена")
    new_cb = callback.model_copy(update={
        "data": f"admin_user_resets_{user_id}{_admin_client_action_suffix(callback.data)}",
    })
    await admin_user_resets(new_cb, state=state)


@router.callback_query(F.data.startswith("ureset_dialog_"))
async def ureset_dialog(callback: CallbackQuery, state: FSMContext | None = None):
    if not await require_permission(callback, AdminPermission.SYSTEM):
        return
    user_id = _admin_client_action_user_id(callback.data, "ureset_dialog_")
    if not await _ensure_user_reset_confirmed(callback, "dialog", user_id, state):
        return
    async with async_session_maker() as session:
        # Delete conversation messages
        await session.execute(delete(DBMessage).where(DBMessage.user_id == user_id))

        # Clear UserTopicSession rows so Preserve cannot reopen old active IDs
        await session.execute(delete(UserTopicSession).where(UserTopicSession.user_id == user_id))

        # Clear active transient dialogue/test state that should not survive
        await session.execute(delete(TestSession).where(TestSession.user_id == user_id))
        await session.execute(delete(PendingAiFlow).where(PendingAiFlow.user_id == user_id))

        # Clear active in-progress claims
        await session.execute(
            delete(SummaryClaim).where(
                SummaryClaim.user_id == user_id,
                SummaryClaim.status == "in_progress",
            )
        )
        await session.execute(
            delete(PreTestClaim).where(
                PreTestClaim.user_id == user_id,
                PreTestClaim.status == "in_progress",
            )
        )

        user = await session.get(User, user_id)
        if user:
            user.current_topic_slug = None
            # DO NOT lower user.current_dialogue_id if dialogue-scoped records remain.
            # Monotonic counter stays at high-water mark so future allocations receive fresh ID.

        # Preserve TestAttempt audit trail on ordinary dialog reset, but mark in-progress attempt abandoned
        await session.execute(
            update(TestAttempt)
            .where(
                TestAttempt.user_id == user_id,
                TestAttempt.status == "in_progress",
            )
            .values(
                status="abandoned",
                completed_at=msk_now(),
            )
        )
        await session.commit()
    await callback.answer("💬 Диалог очищен")
    new_cb = callback.model_copy(update={
        "data": f"admin_user_resets_{user_id}{_admin_client_action_suffix(callback.data)}",
    })
    await admin_user_resets(new_cb, state=state)


@router.callback_query(F.data.startswith("ureset_sub_"))
async def ureset_sub(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_id = _admin_client_action_user_id(callback.data, "ureset_sub_")
    if not await _ensure_user_reset_confirmed(callback, "sub", user_id, state):
        return
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user:
            user.subscription_end = None
        await session.commit()
    await callback.answer("📅 Подписка сброшена")
    new_cb = callback.model_copy(update={
        "data": f"admin_user_resets_{user_id}{_admin_client_action_suffix(callback.data)}",
    })
    await admin_user_resets(new_cb, state=state)


@router.callback_query(F.data.startswith("ureset_balance_"))
async def ureset_balance(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_id = _admin_client_action_user_id(callback.data, "ureset_balance_")
    if not await _ensure_user_reset_confirmed(callback, "balance", user_id, state):
        return
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user:
            user.balance = 0.0
            user.low_balance_notified = False
        await session.commit()
    await callback.answer("💰 Баланс обнулён")
    new_cb = callback.model_copy(update={
        "data": f"admin_user_resets_{user_id}{_admin_client_action_suffix(callback.data)}",
    })
    await admin_user_resets(new_cb, state=state)


@router.callback_query(F.data.startswith("ureset_habits_"))
async def ureset_habits(callback: CallbackQuery, state: FSMContext | None = None):
    if not await require_permission(callback, AdminPermission.SYSTEM):
        return
    user_id = _admin_client_action_user_id(callback.data, "ureset_habits_")
    if not await _ensure_user_reset_confirmed(callback, "habits", user_id, state):
        return
    async with async_session_maker() as session:
        await session.execute(delete(Habit).where(Habit.user_id == user_id))
        await session.commit()
    await callback.answer("🎗 Привычки удалены")
    new_cb = callback.model_copy(update={
        "data": f"admin_user_resets_{user_id}{_admin_client_action_suffix(callback.data)}",
    })
    await admin_user_resets(new_cb, state=state)


@router.callback_query(F.data.startswith("ureset_remind_"))
async def ureset_remind(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_id = _admin_client_action_user_id(callback.data, "ureset_remind_")
    if not await _ensure_user_reset_confirmed(callback, "remind", user_id, state):
        return
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user:
            user.main_reminder_count = 0
            user.next_main_reminder_at = None
        await session.commit()
    await callback.answer("🔔 Напоминания сброшены")
    new_cb = callback.model_copy(update={
        "data": f"admin_user_resets_{user_id}{_admin_client_action_suffix(callback.data)}",
    })
    await admin_user_resets(new_cb, state=state)


@router.callback_query(F.data.startswith("ureset_landing_"))
async def ureset_landing(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_id = _admin_client_action_user_id(callback.data, "ureset_landing_")
    if not await _ensure_user_reset_confirmed(callback, "landing", user_id, state):
        return
    async with async_session_maker() as session:
        await session.execute(delete(FunnelLanding).where(FunnelLanding.user_id == user_id))
        await session.commit()
    await callback.answer("🌐 Лендинг очищен")
    new_cb = callback.model_copy(update={
        "data": f"admin_user_resets_{user_id}{_admin_client_action_suffix(callback.data)}",
    })
    await admin_user_resets(new_cb, state=state)


@router.callback_query(F.data.startswith("ureset_name_"))
async def ureset_name(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_id = _admin_client_action_user_id(callback.data, "ureset_name_")
    if not await _ensure_user_reset_confirmed(callback, "name", user_id, state):
        return
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user:
            user.name = None
            user.gender = None
        await session.commit()
    await callback.answer("👤 Имя и пол сброшены")
    new_cb = callback.model_copy(update={
        "data": f"admin_user_resets_{user_id}{_admin_client_action_suffix(callback.data)}",
    })
    await admin_user_resets(new_cb, state=state)


@router.callback_query(F.data.startswith("ureset_full_"))
async def ureset_full(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_id = _admin_client_action_user_id(callback.data, "ureset_full_")
    if not await _ensure_user_reset_confirmed(callback, "full", user_id, state):
        return
    async with async_session_maker() as session:
        # Воронка
        fs = await session.get(FunnelState, user_id)
        if fs:
            fs.is_active = False
            fs.stage = "idle"
            fs.meditation_id = None
            fs.next_followup_at = None
            fs.reminder_count = 0
            fs.last_ai_reply = None
            fs.completed_at = None
            fs.started_at = None

        # V6 dialogue-scoped records cleanup to prevent collisions when resetting counter to 1
        await session.execute(delete(UserTopicSession).where(UserTopicSession.user_id == user_id))
        await session.execute(delete(PendingAiFlow).where(PendingAiFlow.user_id == user_id))
        await session.execute(delete(PreTestClaim).where(PreTestClaim.user_id == user_id))
        await session.execute(delete(TopicPrerecordedDelivery).where(TopicPrerecordedDelivery.user_id == user_id))
        await session.execute(delete(SummaryClaim).where(SummaryClaim.user_id == user_id))
        await session.execute(delete(SessionSummary).where(SessionSummary.user_id == user_id))
        await session.execute(delete(TestSession).where(TestSession.user_id == user_id))
        await session.execute(delete(TestAttempt).where(TestAttempt.user_id == user_id))

        # Диалог
        await session.execute(delete(DBMessage).where(DBMessage.user_id == user_id))

        # Привычки
        await session.execute(delete(Habit).where(Habit.user_id == user_id))

        # Лендинг
        await session.execute(delete(FunnelLanding).where(FunnelLanding.user_id == user_id))

        # Пользователь
        user = await session.get(User, user_id)
        if user:
            user.name = None
            user.gender = None
            user.subscription_end = None
            user.balance = 0.0
            user.low_balance_notified = False
            user.main_reminder_count = 0
            user.next_main_reminder_at = None
            user.current_dialogue_id = 1
            user.current_topic_slug = None

        await session.commit()
    await callback.answer("⚡️ Полный сброс выполнен!")
    new_cb = callback.model_copy(update={
        "data": f"admin_user_resets_{user_id}{_admin_client_action_suffix(callback.data)}",
    })
    await admin_user_resets(new_cb, state=state)


@router.callback_query(F.data.startswith("admin_history_view_"))
async def admin_history_view(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    context_value = _admin_client_action_context(callback.data)
    client_context = await _load_admin_client_context(callback, context_value)
    if context_value and client_context is None:
        await callback.answer("Экран истории устарел. Откройте список клиентов заново.", show_alert=True)
        return
    await _clear_admin_navigation_state(state, preserve_client_context=True)
    parts = callback.data.split(":", 1)[0].split("_")
    user_id = int(parts[3])
    requested_page = int(parts[4])
    if state is not None and client_context is not None:
        await state.update_data(
            admin_client_return_callback=client_context.return_callback,
            admin_client_parent_callback=_admin_client_parent_callback(client_context.parent_callback),
            client_search_query=client_context.search_query,
            admin_client_search_reset_callback=client_context.reset_callback,
            admin_client_search_reset_query=client_context.reset_query,
            admin_client_navigation_token=context_value,
        )

    limit = 5
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        total_msgs = await session.scalar(
            select(func.count(DBMessage.id)).where(DBMessage.user_id == user_id)
        )
        total_pages = max(1, (total_msgs + limit - 1) // limit)
        page = max(0, min(requested_page, total_pages - 1))
        messages_res = await session.execute(
            select(DBMessage)
            .where(DBMessage.user_id == user_id)
            .order_by(DBMessage.id.asc())
            .limit(limit)
            .offset(page * limit)
        )
        messages = messages_res.scalars().all()
    text_header = f"📜 <b>История клиента:</b> {user.first_name} ({user.id})\n\n"
    content_text = text_header

    current_dialogue = None

    for m in messages:
        if m.dialogue_id != current_dialogue:
            content_text += f"--- <b>Диалог №{m.dialogue_id}</b> ---\n\n"
            current_dialogue = m.dialogue_id

        time_str = format_msk(m.timestamp, "%d-%m-%Y %H:%M")

        role_icon = "👤 (Клиент)" if m.role == 'user' else "🤖 (Бот)"

        msg_block = f"{role_icon} [{time_str}]:\n{html.escape(str(m.content))}\n"

        if len(content_text) + len(msg_block) > 4000:
            content_text += "\n<i>... (часть сообщений не влезла, листайте дальше) ...</i>"
            break

        content_text += msg_block

    if not messages:
        content_text += "История пуста."

    await send_smart_content(
        callback,
        content_text,
        None,
        kb.admin_history_keyboard(user_id, page, total_pages, navigation_token=context_value),
    )


@router.callback_query(F.data.startswith("admin_history_download_"))
async def admin_history_download(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    context_value = _admin_client_action_context(callback.data)
    client_context = await _load_admin_client_context(callback, context_value)
    if context_value and client_context is None:
        await callback.answer("Экран истории устарел. Откройте список клиентов заново.", show_alert=True)
        return
    await _clear_admin_navigation_state(state, preserve_client_context=True)
    user_id = _admin_client_action_user_id(callback.data, "admin_history_download_")
    if state is not None and client_context is not None:
        await state.update_data(
            admin_client_return_callback=client_context.return_callback,
            admin_client_parent_callback=_admin_client_parent_callback(client_context.parent_callback),
            client_search_query=client_context.search_query,
            admin_client_search_reset_callback=client_context.reset_callback,
            admin_client_search_reset_query=client_context.reset_query,
            admin_client_navigation_token=context_value,
        )

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        messages_res = await session.execute(
            select(DBMessage).where(DBMessage.user_id == user_id).order_by(DBMessage.id.asc())
        )
        messages = messages_res.scalars().all()

    username_txt = f"@{user.username}" if user.username else "Нет"

    file_content = (
        f"История диалога с клиентом: {user.first_name} ({username_txt}, ID: {user.id})\n"
        f"==================================================\n\n"
    )

    current_dialogue = None

    for m in messages:
        if m.dialogue_id != current_dialogue:
            file_content += f"\n==================== ДИАЛОГ №{m.dialogue_id} ====================\n\n"
            current_dialogue = m.dialogue_id

        time_str = format_msk(m.timestamp, "%d-%m-%Y %H:%M:%S")

        topic_name = "Общий режим"
        if m.topic_slug:
            if m.topic_slug == 'diagnostics':
                topic_name = "Диагностика"
            elif m.topic_slug == 'free_session':
                topic_name = "Свободная сессия"
            elif m.topic_slug == 'express':
                topic_name = "Экспресс"
            elif m.topic_slug == 'journal':
                topic_name = "Дневник"
            elif m.topic_slug == 'habits':
                topic_name = "Привычки"
            else:
                topic_name = m.topic_slug

        role_str = "Клиент" if m.role == 'user' else "Бот"

        file_content += (
            f"[{time_str}] (Тема: {topic_name}) {role_str}:\n"
            f"{m.content}\n\n"
            f"----------------------------------------\n\n"
        )

    file_data = file_content.encode('utf-8')
    input_file = BufferedInputFile(file_data, filename=f"history_{user.id}.txt")

    await callback.message.answer_document(input_file, caption=f"📄 Полная история: <b>{user.first_name}</b>",
                                           parse_mode='HTML')
    await callback.answer()


# ───── Настройки напоминаний неактивным ─────

@router.callback_query(F.data == "admin_reminders_settings")
async def admin_reminders_settings(event, state: FSMContext | None = None):
    if not await is_admin_actor(event.from_user.id):
        await event.answer("Недостаточно прав", show_alert=True)
        return
    await _clear_admin_navigation_state(state)
    if state is not None:
        await state.update_data(return_callback="admin_reminders_settings")

    async with async_session_maker() as session:
        conf = await session.get(BotConfig, 1)

    enabled_text = "✅ Включены" if conf.main_reminders_enabled else "❌ Выключены"
    text = (
        f"🔔 <b>Напоминания неактивным пользователям</b>\n\n"
        f"Статус: <b>{enabled_text}</b>\n"
        f"⏱ Первое напоминание через: <b>{conf.main_first_reminder_hours} ч.</b>\n"
        f"⏱ Следующие напоминания через: <b>{conf.main_next_reminder_hours} ч.</b>\n\n"
        f"<b>Как это работает:</b>\n"
        f"Бот отслеживает активность пользователей. Если человек давно не писал и не нажимал кнопки, "
        f"ему автоматически отправится напоминание с предложением вернуться.\n\n"
        f"<b>Первое напоминание</b> отправляется через указанное количество часов после последнего действия. "
        f"Если пользователь не отреагирует, следующие напоминания будут приходить с заданным интервалом.\n\n"
        f"Если напоминания <b>выключены</b>, бот не будет отправлять ничего неактивным пользователям.\n\n"
        f"📝 <b>Тексты напоминаний</b> можно изменить здесь или через «Контент» → «Главное меню / Старт»:\n"
        f"• «Первое напоминание неактивным» отправляется в первый раз\n"
        f"• «Повторное напоминание неактивным» отправляется далее\n"
        f"Если тексты не заданы, используется стандартная фраза."
    )

    builder = InlineKeyboardBuilder()
    toggle_text = "❌ Выключить" if conf.main_reminders_enabled else "✅ Включить"
    builder.button(text=toggle_text, callback_data="admin_toggle_reminders")
    builder.button(text="⏱ Первое напоминание (часы)", callback_data="admin_set_reminder_first")
    builder.button(text="⏱ Следующие напоминания (часы)", callback_data="admin_set_reminder_next")
    builder.button(
        text="✏️ Текст первого напоминания",
        callback_data="edit_cont_main:main_reminder_first",
    )
    builder.button(
        text="✏️ Текст повторного напоминания",
        callback_data="edit_cont_main:main_reminder_common",
    )
    builder.button(text="⬅️ Назад", callback_data="admin_section_automation")
    builder.adjust(1)

    await send_smart_content(event, text, None, builder.as_markup())


@router.callback_query(F.data == "admin_toggle_reminders")
async def admin_toggle_reminders(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        conf = await session.get(BotConfig, 1)
        conf.main_reminders_enabled = not conf.main_reminders_enabled
        new_state = conf.main_reminders_enabled
        await session.commit()
    await callback.answer("✅ Включены" if new_state else "❌ Выключены")
    await admin_reminders_settings(callback, state=state)


@router.callback_query(F.data == "admin_set_reminder_first")
async def admin_set_reminder_first(callback: CallbackQuery, state: FSMContext):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.set_reminder_first_hours)
    await callback.answer()

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_reminders_settings")
    await callback.message.answer(
        "⏱ <b>Первое напоминание</b>\n\n"
        "Через сколько часов после последнего действия пользователя отправить первое напоминание?\n\n"
        "Введите целое число (например, 24 означает что напоминание придёт через сутки неактивности):",
        reply_markup=builder.as_markup(),
        parse_mode='HTML'
    )


@router.message(AdminStates.set_reminder_first_hours, ~F.text.startswith("/"))
async def save_reminder_first_hours(message: Message, state: FSMContext, bot: Bot):
    if await state.get_state() != AdminStates.set_reminder_first_hours.state:
        return
    if not await is_admin_actor(message.from_user.id):
        await state.clear()
        return
    try:
        hours = int(message.text.strip())
        if hours < 1:
            await message.answer("Минимум 1 час. Попробуйте ещё раз:")
            return
        async with async_session_maker() as session:
            conf = await session.get(BotConfig, 1)
            conf.main_first_reminder_hours = hours
            await session.commit()
        await state.clear()
        asyncio.create_task(send_temp_notification(bot, message.chat.id, f"✅ Первое напоминание: через {hours} ч. неактивности"))
        await admin_reminders_settings(message)
    except ValueError:
        await message.answer("Нужно целое число. Попробуйте ещё раз:")


@router.callback_query(F.data == "admin_set_reminder_next")
async def admin_set_reminder_next(callback: CallbackQuery, state: FSMContext):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.set_reminder_next_hours)
    await callback.answer()

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_reminders_settings")
    await callback.message.answer(
        "⏱ <b>Повторные напоминания</b>\n\n"
        "С каким интервалом (в часах) отправлять последующие напоминания, если пользователь так и не вернулся?\n\n"
        "Введите целое число (например, 48 означает напоминание раз в двое суток):",
        reply_markup=builder.as_markup(),
        parse_mode='HTML'
    )


@router.message(AdminStates.set_reminder_next_hours, ~F.text.startswith("/"))
async def save_reminder_next_hours(message: Message, state: FSMContext, bot: Bot):
    if await state.get_state() != AdminStates.set_reminder_next_hours.state:
        return
    if not await is_admin_actor(message.from_user.id):
        await state.clear()
        return
    try:
        hours = int(message.text.strip())
        if hours < 1:
            await message.answer("Минимум 1 час. Попробуйте ещё раз:")
            return
        async with async_session_maker() as session:
            conf = await session.get(BotConfig, 1)
            conf.main_next_reminder_hours = hours
            await session.commit()
        await state.clear()
        asyncio.create_task(send_temp_notification(bot, message.chat.id, f"✅ Повторные напоминания: каждые {hours} ч."))
        await admin_reminders_settings(message)
    except ValueError:
        await message.answer("Нужно целое число. Попробуйте ещё раз:")


# ───── Настройки Webhook лендинга ─────

@router.callback_query(F.data.startswith("admin_webhook_settings"))
async def admin_webhook_settings(event, state: FSMContext | None = None):
    if not await is_admin_actor(event.from_user.id):
        await event.answer("Недостаточно прав", show_alert=True)
        return
    state_return = await _admin_payment_state_return(state)
    return_callback = _admin_payment_action_source(getattr(event, "data", None), state_return)
    await _clear_admin_navigation_state(state)
    if state is not None:
        await state.update_data(admin_payment_return_callback=return_callback)

    async with async_session_maker() as session:
        conf = await session.get(BotConfig, 1)

    enabled_text = "✅ Включён" if conf.webhook_enabled else "❌ Выключен"
    secret_display = conf.webhook_secret[:8] + "…" if conf.webhook_secret and len(conf.webhook_secret) > 8 else (conf.webhook_secret or "не задан")
    port = conf.webhook_port or 8081
    text = (
        f"🌐 <b>Прием уведомлений об оплате</b>\n\n"
        f"Статус: <b>{enabled_text}</b>\n"
        f"🔑 Секретный код: <code>{secret_display}</code>\n"
        f"🔌 Порт: <b>{port}</b>\n\n"

        f"<b>Что это такое:</b>\n"
        f"Этот адрес принимает уведомления об оплатах с вашей страницы оплаты или из GetCourse. "
        f"Когда клиент оплачивает, система отправляет запрос на бота, "
        f"и бот автоматически уведомляет клиента и вас.\n\n"

        f"<b>Как подключить:</b>\n"
        f"1. Включите прием уведомлений кнопкой ниже\n"
        f"2. Задайте секрет (или сгенерируйте случайный)\n"
        f"3. Укажите свободный порт на сервере\n"
        f"4. Перезапустите бота\n"
        f"5. Укажите этот адрес для уведомлений:\n"
        f"<code>http://ваш_IP:{port}/webhook/landing-payment/ваш_секрет</code>\n\n"

        f"<b>Подключение GetCourse:</b>\n"
        f"В GetCourse зайдите в Процессы, создайте процесс на смену статуса заказа. "
        f"Добавьте блок \"HTTP-запрос\", метод POST, URL как выше. "
        f"В теле передайте поля: deal_status, deal_id, deal_cost, deal_currency, "
        f"user_email, user_phone, user_name. "
        f"В поле token или deal_number передайте идентификатор страницы оплаты из бота.\n\n"

        f"Поддерживаемые статусы: payed (оплачен), new (новый), "
        f"cancelled (отменён), returned (возврат), part_payed (частично).\n\n"

        f"<b>Если выключено</b>, бот не принимает такие уведомления. "
        f"Оплаты продолжат работать, но бот не узнает о них.\n\n"
        f"⚠️ После изменений нужно <b>перезапустить бота</b>."
    )

    builder = InlineKeyboardBuilder()
    toggle_text = "❌ Выключить" if conf.webhook_enabled else "✅ Включить"
    builder.button(text=toggle_text, callback_data=_admin_payment_action_callback("admin_toggle_webhook", return_callback))
    builder.button(text="🔑 Задать секрет вручную", callback_data=_admin_payment_action_callback("admin_set_webhook_secret", return_callback))
    builder.button(text="🎲 Сгенерировать новый секрет", callback_data=_admin_payment_action_callback("admin_confirm_gen_webhook_secret", return_callback))
    builder.button(text="🔌 Изменить порт", callback_data=_admin_payment_action_callback("admin_set_webhook_port", return_callback))
    builder.button(text="⬅️ Назад", callback_data=return_callback)
    builder.adjust(1)

    await send_smart_content(event, text, None, builder.as_markup())


@router.callback_query(F.data.startswith("admin_toggle_webhook"))
async def admin_toggle_webhook(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    return_callback = await _admin_payment_state_return(
        state,
        _admin_payment_action_source(callback.data),
    )
    await _clear_admin_navigation_state(state)
    async with async_session_maker() as session:
        conf = await session.get(BotConfig, 1)
        conf.webhook_enabled = not conf.webhook_enabled
        new_state = conf.webhook_enabled
        await session.commit()
    status = "✅ Включён" if new_state else "❌ Выключен"
    await callback.answer(f"{status}. Перезапустите бота!")
    if state is not None:
        await state.update_data(admin_payment_return_callback=return_callback)
    await admin_webhook_settings(callback, state=state)


async def _execute_generate_webhook_secret(callback: CallbackQuery, state: FSMContext | None = None):
    return_callback = await _admin_payment_state_return(
        state,
        _admin_payment_action_source(getattr(callback, "data", None)),
    )
    await _clear_admin_navigation_state(state)
    import secrets as _secrets
    new_secret = _secrets.token_urlsafe(32)
    async with async_session_maker() as session:
        conf = await session.get(BotConfig, 1)
        conf.webhook_secret = new_secret
        await session.commit()
    await callback.answer("🎲 Секрет сгенерирован! Перезапустите бота.")
    if state is not None:
        await state.update_data(admin_payment_return_callback=return_callback)
    await admin_webhook_settings(callback, state=state)


@router.callback_query(F.data.startswith("admin_gen_webhook_secret"))
async def admin_gen_webhook_secret(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if isinstance(callback, CallbackQuery):
        return_callback = _admin_payment_action_source(callback.data)
        await _ask_admin_confirmation(
            callback,
            "Старый секрет перестанет работать после применения нового. Продолжить?",
            _admin_payment_action_callback("confirmed_admin_gen_webhook_secret", return_callback),
            _admin_payment_action_callback("admin_webhook_settings", return_callback),
        )
        return
    await _execute_generate_webhook_secret(callback, state)


@router.callback_query(F.data.startswith("admin_confirm_gen_webhook_secret"))
async def admin_confirm_gen_webhook_secret(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    return_callback = _admin_payment_action_source(callback.data)
    await _clear_admin_navigation_state(state)
    if state is not None:
        await state.update_data(admin_payment_return_callback=return_callback)
    await _ask_admin_confirmation(
        callback,
        "Старый секрет перестанет работать после применения нового. Продолжить?",
        f"confirmed_admin_gen_webhook_secret__{return_callback}",
        _admin_payment_action_callback("admin_webhook_settings", return_callback),
    )


@router.callback_query(F.data.startswith("confirmed_admin_gen_webhook_secret"))
async def confirmed_admin_gen_webhook_secret(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    return_callback = _admin_payment_action_source(callback.data)
    if state is not None:
        await state.update_data(admin_payment_return_callback=return_callback)
    await _execute_generate_webhook_secret(callback, state)


@router.callback_query(F.data.startswith("admin_set_webhook_secret"))
async def admin_set_webhook_secret(callback: CallbackQuery, state: FSMContext):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    return_callback = _admin_payment_action_source(callback.data)
    await state.clear()
    await state.set_state(AdminStates.set_webhook_secret)
    await state.update_data(admin_payment_return_callback=return_callback)
    await callback.answer()

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=_admin_payment_action_callback("admin_webhook_settings", return_callback))
    await callback.message.answer(
        "🔑 <b>Секретный код для уведомлений</b>\n\n"
        "Секрет используется как часть URL, чтобы посторонние не могли отправлять ложные уведомления об оплате.\n\n"
        "Введите любую строку без пробелов (или нажмите «Отмена» и используйте кнопку «Сгенерировать секрет»):",
        reply_markup=builder.as_markup(),
        parse_mode='HTML'
    )


@router.message(AdminStates.set_webhook_secret, ~F.text.startswith("/"))
async def save_webhook_secret(message: Message, state: FSMContext, bot: Bot):
    if not await is_admin_actor(message.from_user.id):
        await state.clear()
        return
    return_callback = await _admin_payment_state_return(state)
    secret = message.text.strip()
    if not secret:
        await message.answer("Секрет не может быть пустым. Введите строку или нажмите «Отмена»:")
        return
    if " " in secret:
        await message.answer("Секрет не должен содержать пробелов. Попробуйте ещё раз:")
        return
    async with async_session_maker() as session:
        conf = await session.get(BotConfig, 1)
        conf.webhook_secret = secret
        await session.commit()
    await state.clear()
    await state.update_data(admin_payment_return_callback=return_callback)
    asyncio.create_task(send_temp_notification(bot, message.chat.id, "✅ Секрет сохранён. Перезапустите бота для применения."))
    await admin_webhook_settings(message, state=state)


@router.callback_query(F.data.startswith("admin_set_webhook_port"))
async def admin_set_webhook_port(callback: CallbackQuery, state: FSMContext):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    return_callback = _admin_payment_action_source(callback.data)
    await state.clear()
    await state.set_state(AdminStates.set_webhook_port)
    await state.update_data(admin_payment_return_callback=return_callback)
    await callback.answer()

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=_admin_payment_action_callback("admin_webhook_settings", return_callback))
    await callback.message.answer(
        "🔌 <b>Порт для приема уведомлений</b>\n\n"
        "На этом порту бот будет слушать входящие уведомления об оплатах с лендинга.\n\n"
        "Убедитесь, что порт не занят другими сервисами на сервере и открыт в файрволе.\n"
        "Стандартное значение: 8081\n\n"
        "Введите номер порта (от 1 до 65535):",
        reply_markup=builder.as_markup(),
        parse_mode='HTML'
    )


@router.message(AdminStates.set_webhook_port, ~F.text.startswith("/"))
async def save_webhook_port(message: Message, state: FSMContext, bot: Bot):
    if not await is_admin_actor(message.from_user.id):
        await state.clear()
        return
    return_callback = await _admin_payment_state_return(state)
    try:
        port = int(message.text.strip())
        if port < 1 or port > 65535:
            await message.answer("Порт должен быть от 1 до 65535. Попробуйте ещё раз:")
            return
        async with async_session_maker() as session:
            conf = await session.get(BotConfig, 1)
            conf.webhook_port = port
            await session.commit()
        await state.clear()
        await state.update_data(admin_payment_return_callback=return_callback)
        asyncio.create_task(send_temp_notification(bot, message.chat.id, f"✅ Порт: {port}. Перезапустите бота для применения."))
        await admin_webhook_settings(message, state=state)
    except ValueError:
        await message.answer("Нужно целое число. Попробуйте ещё раз:")


# ───── Настройки клиента ─────

def _settings_text(user) -> str:
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"<b>Имя:</b> {html.escape(user.name or user.first_name or 'Не указано')}\n"
        f"<b>Пол:</b> {'👨 Мужской' if user.gender == 'male' else ('👩 Женский' if user.gender == 'female' else '❓ Не указан')}\n"
        f"<b>Возраст:</b> {user.age or 'Не указан'}\n"
        f"<b>Длина ответов:</b> {'📏 Обычный' if getattr(user, 'response_length', 'normal') != 'short' else '📏 Короткий'}\n"
    )


@router.callback_query(F.data == "settings_change_name")
async def settings_change_name_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.awaiting_new_name)
    await state.update_data(settings_message_id=callback.message.message_id)
    await callback.message.edit_text("Введи новое имя, как к тебе обращаться:")
    await callback.answer()


@router.message(UserStates.awaiting_new_name, F.text)
async def process_new_name(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    user_name = message.text.strip()

    if len(user_name) > 50 or not user_name:
        await message.answer("Пожалуйста, введи корректное имя.")
        return

    async with async_session_maker() as session:
        stmt = update(User).where(User.id == user_id).values(name=user_name)
        await session.execute(stmt)
        await session.commit()
        user = await session.get(User, user_id)

    data = await state.get_data()
    await state.clear()

    text = f"✅ Имя изменено: {html.escape(user_name)}\n\n" + _settings_text(user)
    settings_msg_id = data.get('settings_message_id')
    if settings_msg_id:
        try:
            await bot.edit_message_text(text, chat_id=message.chat.id, message_id=settings_msg_id,
                                        parse_mode='HTML', reply_markup=kb.user_settings_keyboard(user))
        except Exception:
            await message.answer(text, parse_mode='HTML', reply_markup=kb.user_settings_keyboard(user))
    else:
        await message.answer(text, parse_mode='HTML', reply_markup=kb.user_settings_keyboard(user))

    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "settings_change_gender")
async def settings_change_gender(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.awaiting_gender)
    await callback.message.edit_text("Выбери свой пол:", reply_markup=kb.gender_selection_keyboard())
    await callback.answer()


@router.callback_query(UserStates.awaiting_gender, F.data.startswith("gender_"))
async def process_gender(callback: CallbackQuery, state: FSMContext):
    gender_code = callback.data.split("_")[-1]
    user_id = callback.from_user.id

    async with async_session_maker() as session:
        stmt = update(User).where(User.id == user_id).values(gender=gender_code)
        await session.execute(stmt)
        await session.commit()
        user = await session.get(User, user_id)

    await state.clear()

    gender_label = "👨 Мужской" if gender_code == "male" else "👩 Женский"
    text = f"✅ Пол изменён: {gender_label}\n\n" + _settings_text(user)
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb.user_settings_keyboard(user))
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "settings_change_age")
async def settings_change_age(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.awaiting_age)
    await state.update_data(settings_message_id=callback.message.message_id)
    await callback.message.edit_text("Введи свой возраст числом (например, 25):")
    await callback.answer()


@router.message(UserStates.awaiting_age, F.text)
async def process_age(message: Message, state: FSMContext, bot: Bot):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Пожалуйста, введи возраст числом (например, 25).")
        return

    age = int(message.text.strip())
    if age < 1 or age > 120:
        await message.answer("Пожалуйста, введи реальный возраст.")
        return

    user_id = message.from_user.id

    async with async_session_maker() as session:
        stmt = update(User).where(User.id == user_id).values(age=str(age))
        await session.execute(stmt)
        await session.commit()
        user = await session.get(User, user_id)

    data = await state.get_data()
    await state.clear()

    text = f"✅ Возраст: {age}\n\n" + _settings_text(user)
    settings_msg_id = data.get('settings_message_id')
    if settings_msg_id:
        try:
            await bot.edit_message_text(text, chat_id=message.chat.id, message_id=settings_msg_id,
                                        parse_mode='HTML', reply_markup=kb.user_settings_keyboard(user))
        except Exception:
            await message.answer(text, parse_mode='HTML', reply_markup=kb.user_settings_keyboard(user))
    else:
        await message.answer(text, parse_mode='HTML', reply_markup=kb.user_settings_keyboard(user))

    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "settings_toggle_length")
async def settings_toggle_length(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user:
            await callback.answer("Пользователь не найден")
            return
        new_length = 'short' if getattr(user, 'response_length', 'normal') != 'short' else 'normal'
        stmt = update(User).where(User.id == user_id).values(response_length=new_length)
        await session.execute(stmt)
        await session.commit()
        user = await session.get(User, user_id)

    length_text = "📏 Короткий" if new_length == 'short' else "📏 Обычный"
    text = f"✅ Длина ответов: {length_text}\n\n" + _settings_text(user)
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb.user_settings_keyboard(user))
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "settings_close")
async def settings_close(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


install_admin_route_guards(router, globals())
