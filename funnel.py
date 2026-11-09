import asyncio
import csv
import html
import io
import json
import re
import secrets
import weakref
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    BufferedInputFile,
    LinkPreviewOptions,
)
from aiogram.utils.deep_linking import decode_payload
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func, delete, update
from sqlalchemy.orm import selectinload
from time_utils import msk_now, format_msk

import keyboards as kb
from ai_dialogue_service import allocate_global_dialogue_id
from ai_integration import get_ai_response_for_topic
from automation_events import schedule_automation_events
from crisis_safety import (
    automatic_send_allowed,
    crisis_contacts_keyboard as safety_crisis_contacts_keyboard,
    crisis_was_detected_in_turn,
    get_crisis_contacts_text,
    is_crisis_active,
    mark_crisis_detected,
    send_crisis_contacts_if_active,
)
from funnel_response import FunnelParseError, parse_funnel_analysis, render_funnel_fallback_text
from content_service import (
    get_content as cs_get_content,
    get_text as cs_get_text,
    get_button as cs_get_button,
    get_link as cs_get_link,
    invalidate_content_cache,
)
from admin_html_preview import truncate_html_preview
from admin_roles import (
    AdminRole,
    get_admin_role,
    install_admin_route_guards,
    permission_for_callback,
    role_has_permission,
)
from database import (
    AIConfig,
    Content,
    ContentMedia,
    FunnelConfig,
    FunnelEvent,
    FunnelLanding,
    FunnelMeditation,
    FunnelState,
    Message as DBMessage,
    AdminNavigationContext,
    PaymentConfig,
    Topic,
    User,
    async_session_maker,
)
from logging_config import get_logger

router = Router()
logger = get_logger("bot")
funnel_logger = get_logger("funnel")
_FUNNEL_FOLLOWUP_LOCKS = weakref.WeakKeyDictionary()


async def _clear_admin_client_context(state: FSMContext | None) -> str:
    return_callback = "admin_clients_list_0"
    preserved = {}
    if state is not None:
        data = await state.get_data()
        return_callback = data.get("admin_client_return_callback", return_callback)
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
    return return_callback


async def _admin_client_context_return(callback: CallbackQuery, state: FSMContext | None) -> str | None:
    callback_data = getattr(callback, "data", "") or ""
    context_value = callback_data.rsplit(":", 1)[1] if ":" in callback_data else None
    if not context_value or not re.fullmatch(r"clientctx_[0-9a-f]{16}", context_value):
        return await _clear_admin_client_context(state)

    async with async_session_maker() as session:
        context = await session.get(AdminNavigationContext, context_value.removeprefix("clientctx_"))
    if context is None:
        return None
    if context.admin_id != callback.from_user.id:
        return None
    if context.chat_id != getattr(getattr(callback.message, "chat", None), "id", 0):
        return None
    if context.message_id != getattr(callback.message, "message_id", 0):
        return None

    return_callback = f"{context.return_callback}:{context_value}"
    if state is not None:
        await state.clear()
        await state.update_data(
            admin_client_return_callback=context.return_callback,
            admin_client_parent_callback=context.parent_callback or "admin_panel",
            client_search_query=context.search_query,
            admin_client_search_reset_callback=context.reset_callback,
            admin_client_search_reset_query=context.reset_query,
            admin_client_navigation_token=context_value,
        )
    return return_callback


def _funnel_followup_lock():
    loop = asyncio.get_running_loop()
    lock = _FUNNEL_FOLLOWUP_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _FUNNEL_FOLLOWUP_LOCKS[loop] = lock
    return lock


async def _before_funnel_followup_send(user_id: int):
    async with async_session_maker() as session:
        return not await automatic_send_allowed(session, user_id, "funnel_followup")

ACTIVE_FUNNEL_STAGES = {
    "intro",
    "choice",
    "meditation_card",
    "listening",
    "waiting_reflection",
    "post_analysis",
    "vitaly_method",
    "strategy_offer",
    "strategy_faq",
    "strategy_chat",
    "objection_chat",
}

FUNNEL_STAGE_LABELS = {
    "idle": "Не запущена",
    "intro": "Старт воронки",
    "choice": "Выбор практики",
    "meditation_card": "Карточка практики",
    "listening": "Прослушивание практики",
    "waiting_reflection": "Ожидается ответ после практики",
    "post_analysis": "После разбора ИИ",
    "vitaly_method": "Блок о методе Виталия",
    "strategy_offer": "Оффер стратегической сессии",
    "strategy_faq": "FAQ по стратегической сессии",
    "strategy_chat": "Диалог по стратегической сессии",
    "objection_chat": "Диалог по возражениям",
    "landing_admin": "Заявка с лендинга",
    "admin_reset": "Сброшена администратором",
}

LANDING_STATUS_LABELS = {
    "paid": "Оплачено",
    "form_submitted": "Анкета заполнена",
    "opened": "Открыта",
    "failed": "Не оплачена",
}


def human_funnel_stage(stage: str | None) -> str:
    if not stage:
        return "Не запущена"
    return FUNNEL_STAGE_LABELS.get(stage, stage.replace("_", " ").capitalize())


def human_landing_status(status: str | None) -> str:
    if not status:
        return "Нет данных"
    return LANDING_STATUS_LABELS.get(status, status.replace("_", " ").capitalize())

STATS_PERIODS = {
    "7": ("7 дней", 7),
    "30": ("30 дней", 30),
    "90": ("90 дней", 90),
    "all": ("За всё время", None),
}
LANDING_STATUS_FILTERS = {
    "all": "Все",
    "opened": "Открытые",
    "form_submitted": "Заполненные анкеты",
    "paid": "Оплаченные",
    "failed": "Проблемные",
}

FUNNEL_ANALYTICS_PRIMARY_STAGES = (
    ("starts", "funnel_started"),
    ("choices", "choice_opened"),
    ("selected", "meditation_started"),
    ("reflections", "reflection_submitted"),
    ("analyses", "analysis_sent"),
    ("bridge_shown", "bridge_shown"),
    ("offer_requested", "offer_requested"),
    ("signup_clicked", "signup_clicked"),
    ("landing", "landing_open_requested"),
    ("forms", "landing_form_submitted"),
    ("paid", "landing_paid"),
)
FUNNEL_ANALYTICS_STATUS_EVENT_ALIASES = {
    "landing_marked_form_submitted": "forms",
    "landing_marked_paid": "paid",
}
FUNNEL_ANALYTICS_LANDING_STATUS_EVENTS = {
    "landing_form_submitted": "form_submitted",
    "landing_marked_form_submitted": "form_submitted",
    "landing_paid": "paid",
    "landing_marked_paid": "paid",
    "landing_opened": "opened",
    "landing_marked_opened": "opened",
    "landing_failed": "failed",
    "landing_marked_failed": "failed",
    "landing_refunded": "refunded",
    "landing_marked_refunded": "refunded",
    "landing_partial": "partial",
    "landing_marked_partial": "partial",
}
FUNNEL_ANALYTICS_BRANCH_STAGES = (
    ("strategy", "strategy_chat_started"),
    ("objections", "objection_chat_started"),
)
FUNNEL_ANALYTICS_SUPPORT_EVENTS = {
    "post_analysis_offer_opened",
    "strategy_offer_opened",
    "strategy_faq_opened",
    "followup_sent",
    "resume_menu_opened",
    "resume_direct",
}
FUNNEL_ANALYTICS_JOURNEY_BOUNDARY_EVENTS = {
    "return_to_main",
    "resume_menu_opened",
}
FUNNEL_ANALYTICS_EVENT_TYPES = (
    {
        event_type
        for _, event_type in (*FUNNEL_ANALYTICS_PRIMARY_STAGES, *FUNNEL_ANALYTICS_BRANCH_STAGES)
    }
    | FUNNEL_ANALYTICS_SUPPORT_EVENTS
    | set(FUNNEL_ANALYTICS_STATUS_EVENT_ALIASES)
    | set(FUNNEL_ANALYTICS_LANDING_STATUS_EVENTS)
    | FUNNEL_ANALYTICS_JOURNEY_BOUNDARY_EVENTS
)
FUNNEL_ANALYTICS_STAGE_BY_EVENT = {
    event_type: stage
    for stage, event_type in (*FUNNEL_ANALYTICS_PRIMARY_STAGES, *FUNNEL_ANALYTICS_BRANCH_STAGES)
}
FUNNEL_ANALYTICS_STAGE_BY_EVENT.update(
    {event_type: stage for event_type, stage in FUNNEL_ANALYTICS_STATUS_EVENT_ALIASES.items()}
)
FUNNEL_ANALYTICS_PREREQUISITES = {
    "starts": (),
    "choices": ("starts",),
    "selected": ("choices",),
    "reflections": ("selected",),
    "analyses": ("reflections",),
    "bridge_shown": ("analyses",),
    "offer_requested": ("analyses",),
    "strategy": (("analyses", "post_analysis_offer_opened"),),
    "objections": (("analyses", "strategy_offer_opened"),),
    "signup_clicked": (("offer_requested", "strategy", "objections"),),
    "landing": ("signup_clicked",),
    "forms": ("landing",),
    "paid": ("landing",),
}


class ProfileStates(StatesGroup):
    waiting_name = State()
    waiting_gender = State()


class FunnelStates(StatesGroup):
    waiting_reflection = State()
    strategy_chat = State()
    objection_chat = State()


class FunnelAdminStates(StatesGroup):
    edit_content = State()
    edit_content_url = State()
    set_page_size = State()
    set_meditation_timeout = State()
    set_nurture_intervals = State()
    set_first_reminder = State()
    set_next_reminder = State()
    set_resume_hours = State()
    add_title = State()
    add_duration = State()
    add_short = State()
    add_full = State()
    add_url = State()
    add_media = State()
    edit_meditation_text = State()
    edit_meditation_media = State()


def quick_nav_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Главное меню"), KeyboardButton(text="Практики")],
            [KeyboardButton(text="Стратегическая сессия"), KeyboardButton(text="Настройки")],
        ],
        resize_keyboard=True,
    )


async def get_content(key: str):
    return await cs_get_content(key)


async def get_button_content(key: str, default_text: str, default_url: str):
    content = await cs_get_content(key)
    text = content.button_text if content and content.button_text else default_text
    url = content.link_url if content and content.link_url else default_url
    return text, url


def append_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({k: v for k, v in params.items() if v is not None})
    return urlunparse(parsed._replace(query=urlencode(query)))


async def is_admin_user(user_id: int) -> bool:
    return await get_admin_role(user_id) is not None


def parse_start_action(args: str | None):
    if not args:
        return None, None

    raw_value = args.strip()
    candidates = [raw_value]
    try:
        decoded = decode_payload(raw_value)
        if decoded and decoded != raw_value:
            candidates.append(decoded)
    except Exception:
        pass

    for candidate in candidates:
        value = candidate.strip()
        low = value.lower()
        if value.isdigit():
            return "referral", int(value)
        if low in {"practice", "funnel", "hypno", "hypnosis", "hypnosession", "meditation"}:
            return "funnel", None
        if low.startswith("practice_") or low.startswith("funnel_"):
            return "funnel", None

    return None, None


def resolve_bot(target, explicit_bot=None):
    if explicit_bot:
        return explicit_bot
    if hasattr(target, "bot") and target.bot:
        return target.bot
    if hasattr(target, "message") and getattr(target.message, "bot", None):
        return target.message.bot
    return None


def resolve_message_context(target):
    if hasattr(target, "message") and target.message:
        return target.message.chat.id, target.message
    return target.chat.id, target


def get_state_anchor_time(state: FunnelState) -> datetime:
    return state.last_user_action_at or state.stage_changed_at or state.started_at or msk_now()


def get_since_datetime(period_key: str) -> datetime | None:
    period_info = STATS_PERIODS.get(period_key, STATS_PERIODS["all"])
    if period_info[1] is None:
        return None
    return msk_now() - timedelta(days=period_info[1])


async def send_safe_response(chat_id: int, text: str, bot: Bot, reply_markup=None):
    from md_utils import markdown_to_html, split_html_message, remove_markdown

    html_text = markdown_to_html(text)
    chunks = split_html_message(html_text, 4090)

    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        try:
            await bot.send_message(
                chat_id,
                chunk,
                parse_mode="HTML",
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramBadRequest:
            try:
                clean = remove_markdown(text)
                await bot.send_message(
                    chat_id,
                    clean[:4090],
                    reply_markup=markup,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except Exception as e:
                logger.error(f"Failed to send funnel response: {e}")
        await asyncio.sleep(0.2)


async def send_content(target, text, media=None, reply_markup=None, bot=None):
    bot = resolve_bot(target, bot)
    chat_id, message_obj = resolve_message_context(target)
    text = text or ""

    if not media and isinstance(target, CallbackQuery):
        is_text_msg = (
            getattr(message_obj, "content_type", None) == "text"
            or (hasattr(message_obj, "text") and getattr(message_obj, "text", None) is not None)
            or (hasattr(message_obj, "edit_text") and not hasattr(message_obj, "photo") and getattr(message_obj, "content_type", None) is None)
        )
        if is_text_msg and hasattr(message_obj, "edit_text"):
            try:
                await message_obj.edit_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                return
            except TelegramBadRequest as b_err:
                b_err_msg = str(b_err).lower()
                if "message is not modified" in b_err_msg:
                    return
                if "can't parse entities" in b_err_msg or "entity" in b_err_msg:
                    try:
                        await message_obj.edit_text(
                            text,
                            reply_markup=reply_markup,
                            parse_mode=None,
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                        )
                        return
                    except TelegramBadRequest as b_err2:
                        if "message is not modified" in str(b_err2).lower():
                            return
            except Exception as e:
                if "message is not modified" in str(e).lower():
                    return

    if isinstance(target, CallbackQuery):
        try:
            await target.message.delete()
        except Exception:
            pass

    if media:
        media_obj = media[0]
        if len(text) <= 1024:
            if media_obj.file_type == "photo":
                await bot.send_photo(chat_id, media_obj.file_id, caption=text, parse_mode="HTML", reply_markup=reply_markup)
                return
            if media_obj.file_type == "video":
                await bot.send_video(chat_id, media_obj.file_id, caption=text, parse_mode="HTML", reply_markup=reply_markup)
                return
        else:
            # Текст слишком длинный для caption — медиа отдельно, текст с кнопками отдельно
            if media_obj.file_type == "photo":
                await bot.send_photo(chat_id, media_obj.file_id)
            elif media_obj.file_type == "video":
                await bot.send_video(chat_id, media_obj.file_id)
            await bot.send_message(
                chat_id,
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return

    await bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def send_crisis_aware_ai_response(
    bot: Bot,
    user_id: int,
    chat_id: int,
    text: str,
    reply_markup=None,
):
    crisis_detected_now = crisis_was_detected_in_turn(user_id)
    async with async_session_maker() as session:
        crisis_active = await is_crisis_active(session, user_id)
    await send_safe_response(chat_id, text, bot, None if crisis_active else reply_markup)
    if crisis_detected_now:
        await send_crisis_contacts_if_active(bot, user_id, chat_id)


async def _guard_crisis_commercial_path(target, bot=None, state: FSMContext | None = None) -> bool:
    user_id = target.from_user.id
    contacts_text = None
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user or not user.crisis_detected_at:
            return False
        funnel_state = await get_or_create_funnel_state(session, user_id)
        await cancel_nurture_campaign(session, funnel_state)
        funnel_state.is_active = False
        contacts_text = await get_crisis_contacts_text(session)
        await session.commit()

    if state is not None:
        await state.clear()
    current_bot = resolve_bot(target, bot)
    if current_bot:
        chat_id, _ = resolve_message_context(target)
        await send_safe_response(
            chat_id,
            contacts_text,
            current_bot,
            safety_crisis_contacts_keyboard(),
        )
    return True


def _payload_preview(payload: str | None, limit: int = 240) -> str:
    if not payload:
        return "-"
    normalized = " ".join(payload.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def log_funnel_event(user_id: int, stage: str, event_type: str, meditation_id: int | None = None, payload: str | None = None):
    funnel_logger.info(
        "event=%s user_id=%s stage=%s meditation_id=%s payload=%s",
        event_type,
        user_id,
        stage,
        meditation_id if meditation_id is not None else "-",
        _payload_preview(payload),
    )


async def register_event(session, user_id: int, stage: str, event_type: str, meditation_id: int | None = None, payload: str | None = None):
    session.add(
        FunnelEvent(
            user_id=user_id,
            stage=stage,
            event_type=event_type,
            meditation_id=meditation_id,
            payload=payload,
        )
    )
    log_funnel_event(user_id, stage, event_type, meditation_id, payload)


async def get_resume_mode(session, state: FunnelState) -> str | None:
    if not (state.is_active and state.stage in ACTIVE_FUNNEL_STAGES):
        return None
    config = await get_funnel_config(session)
    elapsed = msk_now() - get_state_anchor_time(state)
    if elapsed >= timedelta(hours=config.resume_after_hours):
        return "menu"
    return "direct"


async def get_or_create_funnel_state(session, user_id: int) -> FunnelState:
    state = await session.get(FunnelState, user_id)
    if not state:
        state = FunnelState(user_id=user_id, stage="idle", is_active=False)
        session.add(state)
        await session.flush()
    return state


async def get_funnel_config(session) -> FunnelConfig:
    config = await session.get(FunnelConfig, 1)
    if not config:
        config = FunnelConfig(id=1)
        session.add(config)
        await session.flush()
    return config


def parse_reminder_intervals(raw_val: str | None) -> list[int]:
    if not raw_val:
        return [1, 2, 3, 7, 14]
    try:
        parts = [int(p.strip()) for p in raw_val.split(",") if p.strip()]
        return parts if parts else [1, 2, 3, 7, 14]
    except Exception:
        return [1, 2, 3, 7, 14]


async def start_nurture_campaign(session, state: FunnelState) -> bool:
    user = await session.get(User, state.user_id)
    if user and user.crisis_detected_at:
        await cancel_nurture_campaign(session, state)
        return False
    now = msk_now()
    config = await get_funnel_config(session)
    if not config.nurture_enabled:
        await cancel_nurture_campaign(session, state)
        return False
    intervals = parse_reminder_intervals(config.reminder_intervals)
    state.followup_sequence_anchor = now
    state.reminder_count = 0
    state.next_followup_at = now + timedelta(days=intervals[0])
    return True


async def schedule_next_nurture_touch(session, state: FunnelState):
    config = await get_funnel_config(session)
    intervals = parse_reminder_intervals(config.reminder_intervals)
    if state.reminder_count < len(intervals) and state.followup_sequence_anchor:
        days_offset = intervals[state.reminder_count]
        state.next_followup_at = state.followup_sequence_anchor + timedelta(days=days_offset)
    else:
        state.is_active = False
        state.next_followup_at = None
        state.stage = "completed_unconverted"
        state.followup_sequence_anchor = None


async def cancel_nurture_campaign(session, state: FunnelState):
    state.next_followup_at = None
    state.followup_sequence_anchor = None


async def set_stage(session, state: FunnelState, stage: str, *, active: bool = True, reminder_enabled: bool = False, ai_reply: str | None = None):
    config = await get_funnel_config(session)
    now = msk_now()
    state.stage = stage
    state.is_active = active
    state.stage_changed_at = now
    state.last_user_action_at = now
    state.last_ai_reply = ai_reply
    if stage in ("meditation_card", "listening"):
        if config.meditation_timeouts_enabled:
            timeout_hours = config.meditation_timeout_hours or 2.0
            state.stage_timeout_at = now + timedelta(hours=timeout_hours)
        else:
            state.stage_timeout_at = None
        state.stage_timeout_sent = False
    else:
        state.stage_timeout_at = None
        state.stage_timeout_sent = False


async def deactivate_funnel(session, user_id: int, stage: str, completed: bool = False):
    state = await get_or_create_funnel_state(session, user_id)
    state.is_active = False
    state.stage = stage
    state.meditation_id = None
    state.next_followup_at = None
    state.reminder_count = 0
    state.followup_sequence_anchor = None
    state.last_user_action_at = msk_now()
    state.last_ai_reply = None
    if completed:
        state.completed_at = msk_now()


def funnel_entry_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, начнём", callback_data="funnel_intro_start")
    builder.button(text="Узнать подробнее о методе", callback_data="funnel_about_method")
    builder.button(text="Главное меню", callback_data="funnel_to_main")
    builder.adjust(1)
    return builder.as_markup()


def resume_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🧘 Попробовать другую практику", callback_data="funnel_resume_choice")
    builder.button(text="🎯 Узнать о стратегической сессии", callback_data="funnel_resume_sales")
    builder.button(text="📚 Полезные материалы от Виталия", callback_data="funnel_resume_materials")
    builder.adjust(1)
    return builder.as_markup()


def reminder_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Пройти практику", callback_data="menu_funnel_start")
    builder.button(text="Узнать о стратегической сессии", callback_data="funnel_resume_strategy")
    builder.button(text="Изучить все возможности бота", callback_data="funnel_to_main")
    builder.adjust(1)
    return builder.as_markup()


def meditation_card_keyboard(meditation_id: int, page: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="Начать прослушивание", callback_data=f"funnel_listen_{meditation_id}")
    builder.button(text="Вернуться к выбору", callback_data=f"funnel_choice_{page}")
    builder.button(text="Главное меню", callback_data="funnel_to_main")
    builder.adjust(1)
    return builder.as_markup()


def meditation_listen_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Я прослушал(а), готов(а) продолжить", callback_data="funnel_heard")
    builder.button(text="Выбрать другую практику", callback_data="menu_funnel_start")
    builder.adjust(1)
    return builder.as_markup()


def analysis_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Выбрать другую практику", callback_data="menu_funnel_start")
    builder.button(text="Главное меню", callback_data="funnel_to_main")
    builder.adjust(1)
    return builder.as_markup()


def analysis_bridge_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Разобраться глубже", callback_data="funnel_offer_requested")
    builder.button(text="Побуду с этим", callback_data="funnel_no_strategy_selected")
    builder.adjust(1)
    return builder.as_markup()


def crisis_contacts_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Главное меню", callback_data="funnel_to_main")
    builder.adjust(1)
    return builder.as_markup()


def build_post_analysis_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, интересно", callback_data="funnel_interest_yes")
    builder.button(text="Пока нет, спасибо", callback_data="funnel_interest_no")
    builder.adjust(1)
    return builder.as_markup()


def build_vitaly_method_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔎 Узнать подробнее", callback_data="funnel_show_sales")
    builder.button(text="Пока нет, спасибо", callback_data="funnel_interest_no")
    builder.button(text="Главное меню", callback_data="funnel_to_main")
    builder.adjust(1)
    return builder.as_markup()


def build_strategy_offer_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Записаться на сессию", callback_data="funnel_open_landing")
    builder.button(text="У меня вопросы", callback_data="funnel_show_faq")
    builder.button(text="Хочу, но что-то останавливает", callback_data="funnel_start_objections")
    builder.button(text="Подробнее о Виталии", callback_data="funnel_vitaly_method")
    builder.button(text="Выбрать другую практику", callback_data="menu_funnel_start")
    builder.button(text="Главное меню", callback_data="funnel_to_main")
    builder.adjust(1)
    return builder.as_markup()


def objection_response_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎯 Записаться на сессию", callback_data="funnel_open_landing")
    builder.button(text="⬅️ Вернуться назад", callback_data="funnel_show_sales")
    builder.button(text="Главное меню", callback_data="funnel_to_main")
    builder.adjust(1)
    return builder.as_markup()


async def build_faq_keyboard():
    text, url = await get_button_content("btn_vitaly_questions", "Задать свой вопрос Виталию", "https://t.me/NaumovVitaliy")
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, записываюсь на сессию", callback_data="funnel_open_landing")
    builder.button(text=text, url=url)
    builder.button(text="Хочу понять, что меня останавливает", callback_data="funnel_start_objections")
    builder.button(text="⬅️ Вернуться назад", callback_data="funnel_show_sales")
    builder.button(text="Главное меню", callback_data="funnel_to_main")
    builder.adjust(1)
    return builder.as_markup()


async def build_no_strategy_keyboard():
    channel_text, channel_url = await get_button_content("btn_vitaly_channel", "📢 Подписаться на канал", "https://t.me/lifeuspeh")
    builder = InlineKeyboardBuilder()
    builder.button(text=channel_text, url=channel_url)
    builder.button(text="Выбрать другую практику", callback_data="menu_funnel_start")
    builder.button(text="Главное меню", callback_data="funnel_to_main")
    builder.adjust(1)
    return builder.as_markup()


async def build_landing_keyboard():
    raise RuntimeError("Use build_landing_keyboard_for_user() instead")


async def build_landing_keyboard_for_user(
    user_id: int,
    *,
    origin_stage: str | None = None,
    return_callback: str | None = None,
):
    async with async_session_maker() as session:
        content = await session.get(Content, "btn_funnel_landing")
        text = content.button_text if content and content.button_text else "Записаться на сессию"
        url = content.link_url if content and content.link_url else "https://psiholog-naumov.ru/str_sesiya_tt"

        funnel_state = await get_or_create_funnel_state(session, user_id)
        effective_origin_stage = origin_stage if origin_stage is not None else funnel_state.stage
        token = secrets.token_urlsafe(18)
        landing = FunnelLanding(
            token=token,
            user_id=user_id,
            meditation_id=funnel_state.meditation_id,
            origin_stage=effective_origin_stage,
            amount=None,
            currency="RUB",
            status="opened",
            landing_url=url,
        )
        session.add(landing)
        await session.flush()

        tracked_url = append_query_params(
            url,
            {
                "bot_uid": str(user_id),
                "landing_token": token,
            },
        )
        landing.landing_url = tracked_url

        await register_event(
            session,
            user_id,
            funnel_state.stage,
            "landing_open_requested",
            meditation_id=funnel_state.meditation_id,
            payload=json.dumps({"token": token, "url": tracked_url}, ensure_ascii=False),
        )
        await session.commit()

    builder = InlineKeyboardBuilder()
    builder.button(text=text, url=tracked_url)
    back_callback = return_callback or {
        "strategy_offer": "funnel_show_sales",
        "strategy_faq": "funnel_show_faq",
        "objection_chat": "funnel_show_sales",
        "vitaly_method": "funnel_show_sales",
    }.get(landing.origin_stage, "funnel_to_main")
    builder.button(text="⬅️ Вернуться назад", callback_data=back_callback)
    builder.adjust(1)
    return builder.as_markup()


async def send_funnel_followup_message(session, bot: Bot, state: FunnelState):
    async with _funnel_followup_lock():
        result = await _send_funnel_followup_message_locked(session, bot, state)
        await session.commit()
        return result


async def _send_funnel_followup_message_locked(session, bot: Bot, state: FunnelState):
    async with async_session_maker() as check_session:
        config = await get_funnel_config(check_session)
        user = await check_session.get(User, state.user_id)
        nurture_enabled = bool(config.nurture_enabled)
        crisis_detected = bool(user and user.crisis_detected_at)
    if crisis_detected or not nurture_enabled:
        await cancel_nurture_campaign(session, state)
        return False
    step_num = min(5, state.reminder_count + 1)
    content_key = f"funnel_reminder_step_{step_num}"
    content = await session.get(Content, content_key)
    nurture_defaults = {
        1: "Привет! Вчера ты завершил(а) практику осознанности. Готов(а) разобрать результаты глубже на стратегической сессии?",
        2: "Привет! Виталий Наумов подготовил разбор методик, которые помогают кратно быстрее достигать внутренних изменений. Хочешь узнать больше?",
        3: "Привет! На стратегической сессии мы пошагово разберем твой запрос и построим персональный план. Записаться?",
        4: "Привет! Прошла неделя с момента твоей первой практики. Замечаешь ли уже изменения в состоянии?",
        5: "Привет! Это финальное напоминание о возможности пройти персональную стратегическую сессию с Виталием Наумовым.",
    }
    text = content.text_content if content and content.text_content else nurture_defaults.get(step_num, "Напоминание о стратегической сессии.")
    if await _before_funnel_followup_send(state.user_id):
        await cancel_nurture_campaign(session, state)
        return False
    try:
        await bot.send_message(
            state.user_id,
            text,
            reply_markup=reminder_keyboard(),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramBadRequest:
        await bot.send_message(
            state.user_id,
            text,
            reply_markup=reminder_keyboard(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    await register_event(
        session,
        state.user_id,
        state.stage,
        "followup_sent",
        meditation_id=state.meditation_id,
        payload=text[:1000],
    )
    state.reminder_count += 1
    await schedule_next_nurture_touch(session, state)
    return True


async def continue_active_funnel(target, state: FSMContext, bot=None):
    user_id = target.from_user.id
    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, user_id)
        current_stage = funnel_state.stage
        meditation_id = funnel_state.meditation_id
        ai_reply = funnel_state.last_ai_reply
        await register_event(session, user_id, current_stage, "resume_direct", meditation_id=meditation_id)
        await session.commit()

    if current_stage == "intro":
        await start_funnel_intro(target, bot)
        return
    if current_stage == "choice":
        await show_meditation_choices(target, bot=bot)
        return
    if current_stage == "meditation_card" and meditation_id:
        await show_meditation_card(target, meditation_id, 0, bot)
        return
    if current_stage == "listening" and meditation_id:
        await send_meditation(target, meditation_id, bot)
        return
    if current_stage == "waiting_reflection":
        await request_reflection(target, state, bot)
        return
    if current_stage == "post_analysis":
        if ai_reply:
            chat_id, _ = resolve_message_context(target)
            current_bot = resolve_bot(target, bot)
            await render_funnel_analysis(
                bot=current_bot,
                user_id=user_id,
                chat_id=chat_id,
                raw_response=ai_reply,
                reflection_text="",
                meditation_id=meditation_id,
            )
        return
    if current_stage == "vitaly_method":
        await show_vitaly_method(target, bot)
        return
    if current_stage == "strategy_offer":
        await show_strategy_sales(target, bot)
        return
    if current_stage == "strategy_faq":
        await show_strategy_faq(target, bot)
        return
    if current_stage == "strategy_chat":
        await state.set_state(FunnelStates.strategy_chat)
        await send_content(
            target,
            "Продолжаем диалог по стратегической сессии. Напиши сообщение, и я подхвачу контекст дальше.",
            None,
            build_strategy_offer_keyboard(),
            bot,
        )
        return
    if current_stage == "objection_chat":
        await state.set_state(FunnelStates.objection_chat)
        await send_content(
            target,
            "Продолжаем разбирать, что тебя останавливает. Напиши сообщение, и продолжим.",
            None,
            build_strategy_offer_keyboard(),
            bot,
        )
        return

    await show_resume_menu(target, bot)


async def show_main_menu(target, bot=None):
    from handlers import send_main_menu

    return await send_main_menu(target, bot=bot)


async def send_quick_nav_message(target, bot=None):
    pass


async def send_disclaimer_if_any(target, bot=None):
    pass


async def maybe_ask_profile(message: Message, state: FSMContext, next_action: str):
    async with async_session_maker() as session:
        user = await session.get(User, message.from_user.id)
        if not user or not user.name:
            await state.set_state(ProfileStates.waiting_name)
            await state.update_data(post_start_action=next_action)
            await message.answer("Как к тебе обращаться?", reply_markup=ReplyKeyboardRemove())
            return True
        if not user.gender:
            await state.set_state(ProfileStates.waiting_gender)
            await state.update_data(post_start_action=next_action)
            await message.answer("Укажи пол:", reply_markup=kb.gender_selection_keyboard())
            return True
    return False


async def continue_after_profile(target, state: FSMContext, bot=None):
    data = await state.get_data()
    next_action = data.get("post_start_action", "main_menu")
    await state.clear()
    current_bot = resolve_bot(target, bot)
    if next_action == "funnel_intro":
        await start_funnel_intro(target, bot=current_bot)
    elif next_action == "funnel_continue":
        await continue_active_funnel(target, state, bot=current_bot)
    elif next_action == "funnel_resume":
        await show_resume_menu(target, bot=current_bot)
    elif next_action == "strategy":
        await start_strategy_session(target, state, bot=current_bot)
    else:
        await show_main_menu(target, bot=current_bot)


async def show_resume_menu(target, bot=None):
    user_id = target.from_user.id
    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, user_id)
        await register_event(session, user_id, state.stage, "resume_menu_opened", meditation_id=state.meditation_id)
        await session.commit()

    content = await get_content("funnel_resume_text")
    default_text = (
        "С возвращением! 👋 Ты уже пробовал(а) нашу практику осознанности.\n\n"
        "Что бы ты хотел(а) сделать сейчас?"
    )
    text = content.text_content if content and content.text_content else default_text
    await send_content(target, text, content.media if content else None, resume_keyboard(), bot)


async def start_funnel_intro(target, bot=None):
    user_id = target.from_user.id
    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, user_id)
        await set_stage(session, state, "intro", active=True, reminder_enabled=False)
        await register_event(session, user_id, "intro", "funnel_started")
        await session.commit()

    content = await get_content("funnel_intro")
    text = content.text_content if content else "Готов(а) начать?"
    await send_content(target, text, content.media if content else None, funnel_entry_keyboard(), bot)


async def build_objection_context(user_id: int) -> str:
    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, user_id)
        meditation = await session.get(FunnelMeditation, funnel_state.meditation_id) if funnel_state.meditation_id else None
        last_reflection = await session.scalar(
            select(FunnelEvent.payload)
            .where(
                FunnelEvent.user_id == user_id,
                FunnelEvent.event_type == "reflection_submitted",
            )
            .order_by(FunnelEvent.id.desc())
        )

    context_parts = []
    if meditation:
        context_parts.append(f"Пользователь проходил практику: {meditation.title}.")
    if last_reflection:
        context_parts.append(f"Инсайты пользователя после практики: {last_reflection}")
    if funnel_state.last_ai_reply:
        context_parts.append(f"Последний AI-разбор: {funnel_state.last_ai_reply}")

    return "\n\n".join(context_parts)


async def show_post_analysis_prompt(target, bot=None):
    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, target.from_user.id)
        await set_stage(session, funnel_state, "post_analysis", active=True, reminder_enabled=True, ai_reply=funnel_state.last_ai_reply)
        await register_event(session, target.from_user.id, "post_analysis", "post_analysis_offer_opened", meditation_id=funnel_state.meditation_id)
        await session.commit()

    content = await get_content("funnel_after_analysis_offer")
    await send_content(target, content.text_content if content else "Хочешь узнать больше о стратегической сессии?", content.media if content else None, build_post_analysis_keyboard(), bot)


async def show_vitaly_method(target, bot=None):
    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, target.from_user.id)
        await set_stage(session, funnel_state, "vitaly_method", active=True, reminder_enabled=True, ai_reply=funnel_state.last_ai_reply)
        await register_event(session, target.from_user.id, "vitaly_method", "vitaly_method_opened", meditation_id=funnel_state.meditation_id)
        await session.commit()

    content = await get_content("funnel_vitaly_method")
    await send_content(target, content.text_content if content else "Информация о Виталии и методе.", content.media if content else None, build_vitaly_method_keyboard(), bot)


async def show_strategy_sales(target, bot=None):
    if await _guard_crisis_commercial_path(target, bot):
        return
    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, target.from_user.id)
        await set_stage(session, funnel_state, "strategy_offer", active=True, reminder_enabled=True, ai_reply=funnel_state.last_ai_reply)
        await register_event(session, target.from_user.id, "strategy_offer", "strategy_offer_opened", meditation_id=funnel_state.meditation_id)
        await session.commit()

    content = await get_content("funnel_strategy_sales")
    await send_content(target, content.text_content if content else "Подробности о стратегической сессии.", content.media if content else None, build_strategy_offer_keyboard(), bot)


async def show_strategy_faq(target, bot=None):
    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, target.from_user.id)
        await set_stage(session, funnel_state, "strategy_faq", active=True, reminder_enabled=True, ai_reply=funnel_state.last_ai_reply)
        await register_event(session, target.from_user.id, "strategy_faq", "faq_opened", meditation_id=funnel_state.meditation_id)
        await session.commit()

    content = await get_content("funnel_strategy_faq")
    await send_content(target, content.text_content if content else "Частые вопросы.", content.media if content else None, await build_faq_keyboard(), bot)


async def show_no_strategy_options(target, bot=None):
    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, target.from_user.id)
        user = await session.get(User, target.from_user.id)
        config = await get_funnel_config(session)
        funnel_state.stage = "refusal_declined"
        funnel_state.stage_timeout_at = None
        funnel_state.stage_timeout_sent = False
        if user and user.crisis_detected_at:
            await cancel_nurture_campaign(session, funnel_state)
            funnel_state.is_active = False
        elif config.nurture_enabled:
            if funnel_state.next_followup_at is None or funnel_state.followup_sequence_anchor is None:
                await start_nurture_campaign(session, funnel_state)
            funnel_state.is_active = True
        else:
            await cancel_nurture_campaign(session, funnel_state)
            funnel_state.is_active = False
        funnel_state.stage_changed_at = msk_now()
        funnel_state.last_user_action_at = msk_now()
        await register_event(session, target.from_user.id, "refusal_declined", "strategy_declined", meditation_id=funnel_state.meditation_id)
        await session.commit()

    content = await get_content("funnel_no_strategy")
    await send_content(target, content.text_content if content else "Понимаю, возможно, сейчас не время.", content.media if content else None, await build_no_strategy_keyboard(), bot)


async def show_meditation_choices(target, page: int = 0, bot=None, *, start_new_journey: bool = False):
    user_id = target.from_user.id
    async with async_session_maker() as session:
        config = await get_funnel_config(session)
        stmt = (
            select(FunnelMeditation)
            .where(FunnelMeditation.is_active == True)
            .order_by(FunnelMeditation.sort_order.asc(), FunnelMeditation.id.asc())
        )
        meditations = (await session.execute(stmt)).scalars().all()
        state = await get_or_create_funnel_state(session, user_id)
        state.meditation_id = None
        await set_stage(session, state, "choice", active=True, reminder_enabled=True)
        if start_new_journey:
            await register_event(session, user_id, "intro", "funnel_started")
        await register_event(session, user_id, "choice", "choice_opened")
        await session.commit()

    content = await get_content("funnel_choice_intro")
    text = content.text_content if content else "Выбери практику."

    if not meditations:
        text += "\n\nСписок медитаций пока пуст. Добавьте их через админку."
        builder = InlineKeyboardBuilder()
        builder.button(text="Главное меню", callback_data="funnel_to_main")
        builder.adjust(1)
        await send_content(target, text, content.media if content else None, builder.as_markup(), bot)
        return

    page_size = max(1, config.page_size)
    total_pages = max(1, (len(meditations) + page_size - 1) // page_size)
    page = min(max(page, 0), total_pages - 1)
    page_items = meditations[page * page_size:(page + 1) * page_size]

    lines = [text, ""]
    builder = InlineKeyboardBuilder()
    for meditation in page_items:
        lines.append(f"<b>{meditation.sort_order}. {html.escape(meditation.title)}</b> • {meditation.duration_minutes} мин")
        if meditation.short_description:
            lines.append(html.escape(meditation.short_description))
        lines.append("")
        builder.row(
            InlineKeyboardButton(
                text=f"{meditation.sort_order}. {meditation.title}",
                callback_data=f"funnel_card_{meditation.id}_{page}",
            )
        )

    nav_row = []
    if total_pages > 1:
        prev_page = page - 1 if page > 0 else total_pages - 1
        next_page = page + 1 if page < total_pages - 1 else 0
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"funnel_choice_{prev_page}"))
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"funnel_choice_{next_page}"))
        builder.row(*nav_row)

    builder.row(InlineKeyboardButton(text="Узнать о стратегической сессии", callback_data="funnel_resume_strategy"))
    builder.row(InlineKeyboardButton(text="Главное меню", callback_data="funnel_to_main"))

    await send_content(target, "\n".join(lines).strip(), content.media if content else None, builder.as_markup(), bot)


async def show_meditation_card(target, meditation_id: int, page: int = 0, bot=None):
    user_id = target.from_user.id
    async with async_session_maker() as session:
        meditation = await session.get(FunnelMeditation, meditation_id)
        if not meditation or not meditation.is_active:
            return
        state = await get_or_create_funnel_state(session, user_id)
        state.meditation_id = meditation_id
        await set_stage(session, state, "meditation_card", active=True, reminder_enabled=False)
        await register_event(session, user_id, "meditation_card", "meditation_opened", meditation_id=meditation_id)
        await session.commit()

    text = (
        f"<b>{html.escape(meditation.title)}</b>\n"
        f"Длительность: {meditation.duration_minutes} мин\n\n"
        f"{html.escape(meditation.full_description or meditation.short_description or '')}"
    )
    await send_content(target, text, None, meditation_card_keyboard(meditation_id, page), bot)


async def send_meditation(target, meditation_id: int, bot=None):
    bot = resolve_bot(target, bot)
    chat_id, message_obj = resolve_message_context(target)
    user_id = target.from_user.id

    async with async_session_maker() as session:
        meditation = await session.get(FunnelMeditation, meditation_id)
        if not meditation:
            return
        instruction_content = await session.get(Content, "funnel_listen_instruction")
        state = await get_or_create_funnel_state(session, user_id)
        state.meditation_id = meditation_id
        await set_stage(session, state, "listening", active=True, reminder_enabled=True)
        await register_event(session, user_id, "listening", "meditation_started", meditation_id=meditation_id)
        await session.commit()

    if isinstance(target, CallbackQuery):
        try:
            await message_obj.delete()
        except Exception as exc:
            logger.warning("Failed to delete previous meditation card for user_id=%s: %s", user_id, exc)

    caption = (
        f"<b>{html.escape(meditation.title)}</b>\n"
        f"Длительность: {meditation.duration_minutes} мин\n\n"
        f"{instruction_content.text_content if instruction_content else ''}"
    )
    if meditation.file_type == "audio" and meditation.file_id:
        caption += "\n\n<i>Нажми на аудио выше, чтобы начать прослушивание. После окончания нажми кнопку ниже.</i>"

    if meditation.file_type == "audio" and meditation.file_id:
        if len(caption) <= 1024:
            await bot.send_audio(chat_id, meditation.file_id, caption=caption, parse_mode="HTML", reply_markup=meditation_listen_keyboard())
        else:
            await bot.send_audio(chat_id, meditation.file_id)
            await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=meditation_listen_keyboard())
    elif meditation.file_type == "video" and meditation.file_id:
        if len(caption) <= 1024:
            await bot.send_video(chat_id, meditation.file_id, caption=caption, parse_mode="HTML", reply_markup=meditation_listen_keyboard())
        else:
            await bot.send_video(chat_id, meditation.file_id)
            await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=meditation_listen_keyboard())
    else:
        await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=meditation_listen_keyboard())


async def request_reflection(target, state: FSMContext, bot=None):
    user_id = target.from_user.id
    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, user_id)
        await set_stage(session, funnel_state, "waiting_reflection", active=True, reminder_enabled=True)
        await register_event(session, user_id, "waiting_reflection", "reflection_requested", meditation_id=funnel_state.meditation_id)
        await session.commit()

    await state.set_state(FunnelStates.waiting_reflection)
    content = await get_content("funnel_reflection_prompt")
    await send_content(target, content.text_content if content else "Опишите свои ощущения после практики.", content.media if content else None, bot=bot)


async def deliver_analysis_core(
    bot: Bot,
    user_id: int,
    chat_id: int,
    reflection_text: str,
    user_first_name: str | None = None,
    meditation_id: int | None = None,
):
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        funnel_state = await get_or_create_funnel_state(session, user_id)
        med_id = meditation_id or (funnel_state.meditation_id if funnel_state else None)
        meditation = await session.get(FunnelMeditation, med_id) if med_id else None
        dialogue_id = await allocate_global_dialogue_id(session, user_id)
        prompt = (
            f"Практика: {meditation.title if meditation else 'Не указана'}.\n\n"
            f"Опыт пользователя после медитации:\n{reflection_text}\n\n"
            "Сделай персональный разбор по методу Транскоучинг."
        )

        user_msg = DBMessage(
            user_id=user_id,
            dialogue_id=dialogue_id,
            role="user",
            content=prompt,
            topic_slug="funnel_demo",
        )
        session.add(user_msg)
        await session.flush()
        current_msg_id = user_msg.id
        await register_event(
            session,
            user_id,
            "waiting_reflection",
            "reflection_submitted",
            meditation_id=med_id,
            payload=reflection_text[:1000],
        )
        await session.commit()

    waiting_msg = await bot.send_message(chat_id, "Анализирую твой опыт...")
    effective_name = user_first_name or (user.first_name if user else "Пользователь") or "Пользователь"
    response_text = await get_ai_response_for_topic(
        user_id,
        prompt,
        effective_name,
        topic_slug="funnel_demo",
        dialogue_id=dialogue_id,
        current_message_id=current_msg_id,
        bot=bot,
        chat_id=chat_id,
        alert_user_text=reflection_text,
        alert_metadata={"practice": meditation.title if meditation else ""},
    )
    try:
        await waiting_msg.delete()
    except Exception as exc:
        logger.warning("Failed to delete analysis waiting message for user_id=%s: %s", user_id, exc)

    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, user_id)
        session.add(DBMessage(
            user_id=user_id,
            dialogue_id=dialogue_id,
            role="assistant",
            content=response_text,
            topic_slug="funnel_demo",
        ))
        await set_stage(session, funnel_state, "post_analysis", active=True, reminder_enabled=False, ai_reply=response_text)
        await register_event(
            session,
            user_id,
            "post_analysis",
            "analysis_sent",
            meditation_id=med_id,
        )
        await session.commit()

    await render_funnel_analysis(
        bot=bot,
        user_id=user_id,
        chat_id=chat_id,
        raw_response=response_text,
        reflection_text=reflection_text,
        meditation_id=med_id,
    )


async def render_funnel_analysis(
    *,
    bot: Bot,
    user_id: int,
    chat_id: int,
    raw_response: str,
    reflection_text: str,
    meditation_id: int | None,
) -> None:
    parsed = None
    parse_error = None
    try:
        parsed = parse_funnel_analysis(raw_response)
    except FunnelParseError as exc:
        parse_error = str(exc)

    crisis_active = False
    alert_event = None
    practice_name = ""
    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, user_id)
        user = await session.get(User, user_id)
        meditation = await session.get(FunnelMeditation, meditation_id) if meditation_id else None
        practice_name = meditation.title if meditation else ""
        crisis_active = bool(user and user.crisis_detected_at)
        if parsed is None:
            await cancel_nurture_campaign(session, state)
            await register_event(
                session,
                user_id,
                "post_analysis",
                "parse_failed",
                meditation_id=meditation_id,
            )
            alert_event = "parse_failed"
            await session.commit()
        elif parsed.mode == "clinical":
            await cancel_nurture_campaign(session, state)
            await register_event(
                session,
                user_id,
                "post_analysis",
                "clinical_detected",
                meditation_id=meditation_id,
            )
            alert_event = "clinical_detected"
            await session.commit()
        elif parsed.mode == "crisis":
            await cancel_nurture_campaign(session, state)
            state.stage = "crisis"
            state.is_active = False
            state.stage_timeout_at = None
            await mark_crisis_detected(session, user_id)
            await register_event(
                session,
                user_id,
                "post_analysis",
                "crisis_detected",
                meditation_id=meditation_id,
            )
            alert_event = "crisis_detected"
            await session.commit()
        elif crisis_active:
            await cancel_nurture_campaign(session, state)
        else:
            await start_nurture_campaign(session, state)
        await session.commit()

    if alert_event and bot:
        schedule_automation_events(
            bot,
            user_id=user_id,
            dialogue_id=1,
            scope="funnel",
            topic_slug="funnel_demo",
            events=(alert_event,),
            metadata={
                "user_text": reflection_text,
                "practice": practice_name,
                "topic": "ИИ-разбор гипносессии",
            },
        )

    crisis_detected_now = crisis_was_detected_in_turn(user_id)

    if parsed is None:
        fallback_content = await get_content("funnel_fallback_text")
        fallback_text = render_funnel_fallback_text(
            fallback_content.text_content if fallback_content else None,
        )
        if crisis_detected_now:
            await send_safe_response(chat_id, fallback_text, bot, None)
            await send_safe_response(
                chat_id,
                await get_crisis_contacts_text(),
                bot,
                safety_crisis_contacts_keyboard(),
            )
            return
        await send_safe_response(chat_id, fallback_text, bot, analysis_keyboard())
        logger.warning("Funnel response parse failed for user_id=%s reason=%s", user_id, parse_error)
        return

    if crisis_detected_now:
        await send_safe_response(chat_id, parsed.reply, bot, None)
        await send_safe_response(
            chat_id,
            await get_crisis_contacts_text(),
            bot,
            safety_crisis_contacts_keyboard(),
        )
        return

    if parsed.mode == "crisis":
        await send_safe_response(chat_id, parsed.reply, bot, None)
        await send_safe_response(
            chat_id,
            await get_crisis_contacts_text(),
            bot,
            safety_crisis_contacts_keyboard(),
        )
        return

    if parsed.mode == "clinical":
        await send_safe_response(chat_id, parsed.reply, bot, analysis_keyboard())
        return

    if crisis_detected_now:
        await send_safe_response(chat_id, parsed.reply, bot, None)
        await send_safe_response(
            chat_id,
            await get_crisis_contacts_text(),
            bot,
            safety_crisis_contacts_keyboard(),
        )
        return

    await send_safe_response(
        chat_id,
        parsed.reply,
        bot,
        None if parsed.offer_eligible and not crisis_active else analysis_keyboard(),
    )
    if parsed.offer_eligible and not crisis_active:
        await send_safe_response(chat_id, parsed.bridge, bot, analysis_bridge_keyboard())
        async with async_session_maker() as session:
            await register_event(
                session,
                user_id,
                "post_analysis",
                "bridge_shown",
                meditation_id=meditation_id,
            )
            await session.commit()


async def resume_funnel_reflection_after_disclaimer(
    bot: Bot,
    user_id: int,
    chat_id: int,
    reflection_text: str,
    meditation_id: int | None = None,
):
    await deliver_analysis_core(
        bot=bot,
        user_id=user_id,
        chat_id=chat_id,
        reflection_text=reflection_text,
        meditation_id=meditation_id,
    )


async def deliver_analysis(message: Message, state: FSMContext, reflection_text: str):
    user_id = message.from_user.id
    from handlers import ensure_ai_disclaimer_sent

    chat_id = message.chat.id if hasattr(message, "chat") and message.chat else user_id
    async with async_session_maker() as session:
        await ensure_ai_disclaimer_sent(session, message.bot, user_id, chat_id)
        funnel_state = await get_or_create_funnel_state(session, user_id)
        med_id = funnel_state.meditation_id if funnel_state else None

    if state:
        await state.clear()
    await deliver_analysis_core(
        bot=message.bot,
        user_id=user_id,
        chat_id=chat_id,
        reflection_text=reflection_text,
        user_first_name=getattr(message.from_user, "first_name", None),
        meditation_id=med_id,
    )


async def show_strategy_offer(target, bot=None):
    await show_strategy_sales(target, bot)


async def resume_funnel_strategy_after_disclaimer(callback, state: FSMContext, bot: Bot, payload: dict):
    msg_text = payload.get("strategy_message")
    did = payload.get("strategy_dialogue_id")
    if did and state:
        await state.update_data(strategy_dialogue_id=did)
    if state:
        await state.set_state(FunnelStates.strategy_chat)

    if msg_text:
        # Re-invoke handle_strategy_message with a synthesized message
        from types import SimpleNamespace
        chat = getattr(callback, "message", None) and getattr(callback.message, "chat", None)
        chat_id = chat.id if chat else callback.from_user.id
        async def _answer(text, **kwargs):
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        answer_func = getattr(getattr(callback, "message", None), "answer", None) or _answer

        msg = SimpleNamespace(
            from_user=callback.from_user,
            chat=chat or SimpleNamespace(id=chat_id),
            text=msg_text,
            bot=bot,
            answer=answer_func,
        )
        await handle_strategy_message(msg, state)
    else:
        await start_strategy_session(callback, state, bot=bot)


async def resume_funnel_objection_after_disclaimer(callback, state: FSMContext, bot: Bot, payload: dict):
    msg_text = payload.get("objection_message")
    did = payload.get("objection_dialogue_id")
    if did and state:
        await state.update_data(objection_dialogue_id=did)
    if state:
        await state.set_state(FunnelStates.objection_chat)

    if msg_text:
        from types import SimpleNamespace
        chat = getattr(callback, "message", None) and getattr(callback.message, "chat", None)
        chat_id = chat.id if chat else callback.from_user.id
        async def _answer(text, **kwargs):
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        answer_func = getattr(getattr(callback, "message", None), "answer", None) or _answer

        msg = SimpleNamespace(
            from_user=callback.from_user,
            chat=chat or SimpleNamespace(id=chat_id),
            text=msg_text,
            bot=bot,
            answer=answer_func,
        )
        await handle_objection_message(msg, state)
    else:
        await start_objection_dialogue(callback, state, bot=bot)


async def start_strategy_session(target, state: FSMContext, bot=None):
    user_id = target.from_user.id

    async with async_session_maker() as session:
        dialogue_id = await allocate_global_dialogue_id(session, user_id)
        funnel_state = await get_or_create_funnel_state(session, user_id)
        await set_stage(session, funnel_state, "strategy_chat", active=True, reminder_enabled=True)
        await register_event(session, user_id, "strategy_chat", "strategy_chat_started", meditation_id=funnel_state.meditation_id)
        intro = await session.get(Content, "strategy_session_intro")
        first_message = (
            f"{intro.text_content if intro else ''}\n\n"
            "Знаком(а) ли ты уже с Виталием и методом Транскоучинг? "
            "И какой запрос тебе хочется прояснить на стратегической сессии?"
        ).strip()
        session.add(DBMessage(
            user_id=user_id,
            dialogue_id=dialogue_id,
            role="assistant",
            content=first_message,
            topic_slug="strategy_session",
        ))
        await session.commit()

    await state.set_state(FunnelStates.strategy_chat)
    await state.update_data(strategy_dialogue_id=dialogue_id)
    await send_content(target, first_message, None, build_strategy_offer_keyboard(), bot)


async def open_landing(
    target,
    bot=None,
    *,
    origin_stage: str | None = None,
    return_callback: str | None = None,
):
    if await _guard_crisis_commercial_path(target, bot):
        return
    user_id = target.from_user.id
    content = await get_content("funnel_landing_intro")
    await send_content(
        target,
        content.text_content if content else "Нажми на кнопку ниже, чтобы записаться на сессию.",
        content.media if content else None,
        await build_landing_keyboard_for_user(
            user_id,
            origin_stage=origin_stage,
            return_callback=return_callback,
        ),
        bot,
    )


async def start_objection_dialogue(target, state: FSMContext, bot=None):
    user_id = target.from_user.id

    async with async_session_maker() as session:
        dialogue_id = await allocate_global_dialogue_id(session, user_id)
        funnel_state = await get_or_create_funnel_state(session, user_id)
        await set_stage(session, funnel_state, "objection_chat", active=True, reminder_enabled=True)
        await register_event(session, user_id, "objection_chat", "objection_chat_started", meditation_id=funnel_state.meditation_id)
        intro = await session.get(Content, "funnel_objection_intro")
        first_message = intro.text_content if intro and intro.text_content else "Напиши, что именно тебя останавливает."
        session.add(DBMessage(
            user_id=user_id,
            dialogue_id=dialogue_id,
            role="assistant",
            content=first_message,
            topic_slug="strategy_objections",
        ))
        await session.commit()

    await state.set_state(FunnelStates.objection_chat)
    await state.update_data(objection_dialogue_id=dialogue_id)
    await send_content(target, first_message, None, None, bot)


async def handle_strategy_message(message: Message, state: FSMContext):
    data = await state.get_data() if state else {}
    dialogue_id = data.get("strategy_dialogue_id")
    if not dialogue_id:
        async with async_session_maker() as session:
            dialogue_id = await allocate_global_dialogue_id(session, message.from_user.id)
            await session.commit()
        if state:
            await state.update_data(strategy_dialogue_id=dialogue_id)

    user_id = message.from_user.id
    chat_id = message.chat.id if hasattr(message, "chat") and message.chat else user_id

    from handlers import check_and_request_ai_disclaimer
    accepted = await check_and_request_ai_disclaimer(
        message.bot,
        user_id,
        chat_id,
        flow_type="funnel_strategy",
        payload={"strategy_message": message.text, "strategy_dialogue_id": dialogue_id},
    )
    if not accepted:
        return

    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, message.from_user.id)
        await set_stage(session, funnel_state, "strategy_chat", active=True, reminder_enabled=True)
        user_msg = DBMessage(
            user_id=message.from_user.id,
            dialogue_id=dialogue_id,
            role="user",
            content=message.text,
            topic_slug="strategy_session",
        )
        session.add(user_msg)
        await session.flush()
        current_msg_id = user_msg.id
        await register_event(
            session,
            message.from_user.id,
            "strategy_chat",
            "strategy_user_message",
            meditation_id=funnel_state.meditation_id,
            payload=message.text[:1000],
        )
        await session.commit()

    wait_msg = await message.answer("Подбираю следующий шаг...")
    response = await get_ai_response_for_topic(
        message.from_user.id,
        message.text,
        message.from_user.first_name or "Пользователь",
        topic_slug="strategy_session",
        dialogue_id=dialogue_id,
        current_message_id=current_msg_id,
        bot=message.bot,
        chat_id=message.chat.id,
        alert_user_text=message.text,
    )
    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.warning("Failed to delete strategy wait message for user_id=%s: %s", message.from_user.id, exc)

    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, message.from_user.id)
        session.add(DBMessage(
            user_id=message.from_user.id,
            dialogue_id=dialogue_id,
            role="assistant",
            content=response,
            topic_slug="strategy_session",
        ))
        await set_stage(session, funnel_state, "strategy_chat", active=True, reminder_enabled=True, ai_reply=response)
        await session.commit()

    await send_crisis_aware_ai_response(
        message.bot,
        message.from_user.id,
        message.chat.id,
        response,
        build_strategy_offer_keyboard(),
    )


async def handle_objection_message(message: Message, state: FSMContext):
    data = await state.get_data() if state else {}
    dialogue_id = data.get("objection_dialogue_id")
    if not dialogue_id:
        async with async_session_maker() as session:
            dialogue_id = await allocate_global_dialogue_id(session, message.from_user.id)
            await session.commit()
        if state:
            await state.update_data(objection_dialogue_id=dialogue_id)

    user_id = message.from_user.id
    chat_id = message.chat.id if hasattr(message, "chat") and message.chat else user_id

    from handlers import check_and_request_ai_disclaimer
    accepted = await check_and_request_ai_disclaimer(
        message.bot,
        user_id,
        chat_id,
        flow_type="funnel_objection",
        payload={"objection_message": message.text, "objection_dialogue_id": dialogue_id},
    )
    if not accepted:
        return

    context_text = await build_objection_context(message.from_user.id)

    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, message.from_user.id)
        await set_stage(session, funnel_state, "objection_chat", active=True, reminder_enabled=True)
        user_msg = DBMessage(
            user_id=message.from_user.id,
            dialogue_id=dialogue_id,
            role="user",
            content=message.text,
            topic_slug="strategy_objections",
        )
        session.add(user_msg)
        await session.flush()
        current_msg_id = user_msg.id
        await register_event(
            session,
            message.from_user.id,
            "objection_chat",
            "objection_user_message",
            meditation_id=funnel_state.meditation_id,
            payload=message.text[:1000],
        )
        await session.commit()

    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except Exception:
        pass
    wait_msg = await message.answer("💬 Готовлю лучший ответ для тебя...")
    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except Exception:
        pass
    response = await get_ai_response_for_topic(
        message.from_user.id,
        message.text,
        message.from_user.first_name or "Пользователь",
        topic_slug="strategy_objections",
        dialogue_id=dialogue_id,
        extra_system=context_text or None,
        current_message_id=current_msg_id,
        bot=message.bot,
        chat_id=message.chat.id,
        alert_user_text=message.text,
    )
    try:
        await wait_msg.delete()
    except Exception as exc:
        logger.warning("Failed to delete objection wait message for user_id=%s: %s", message.from_user.id, exc)

    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, message.from_user.id)
        session.add(DBMessage(
            user_id=message.from_user.id,
            dialogue_id=dialogue_id,
            role="assistant",
            content=response,
            topic_slug="strategy_objections",
        ))
        await set_stage(session, funnel_state, "objection_chat", active=True, reminder_enabled=True, ai_reply=response)
        await session.commit()

    await send_crisis_aware_ai_response(
        message.bot,
        message.from_user.id,
        message.chat.id,
        response,
        objection_response_keyboard(),
    )


@router.message(ProfileStates.waiting_name, ~F.text.startswith("/"))
async def profile_name_handler(message: Message, state: FSMContext):
    async with async_session_maker() as session:
        user = await session.get(User, message.from_user.id)
        user.name = message.text.strip()
        await session.commit()

    await state.set_state(ProfileStates.waiting_gender)
    await message.answer("Укажи пол:", reply_markup=kb.gender_selection_keyboard())


@router.callback_query(ProfileStates.waiting_gender, F.data.startswith("gender_"))
async def profile_gender_handler(callback: CallbackQuery, state: FSMContext):
    gender = callback.data.split("_")[-1]
    async with async_session_maker() as session:
        user = await session.get(User, callback.from_user.id)
        user.gender = gender
        await session.commit()

    await callback.answer("Пол сохранен")
    await continue_after_profile(callback, state)


@router.message(F.text.in_(["Главное меню"]))
async def quick_nav_main(message: Message, state: FSMContext):
    await state.clear()
    async with async_session_maker() as session:
        await deactivate_funnel(session, message.from_user.id, "paused")
        await register_event(session, message.from_user.id, "paused", "return_to_main")
        await session.commit()
    await show_main_menu(message)


@router.message(F.text.in_(["Практики", "Практика - гипносессия"]))
async def quick_nav_practice(message: Message, state: FSMContext):
    await state.clear()
    await show_meditation_choices(message)


@router.message(F.text == "Стратегическая сессия")
async def quick_nav_strategy(message: Message, state: FSMContext):
    await state.clear()
    await start_strategy_session(message, state)


@router.message(F.text == "Настройки")
async def quick_nav_settings(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
    if not user:
        return

    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"<b>Имя:</b> {html.escape(user.name or user.first_name or 'Не указано')}\n"
        f"<b>Пол:</b> {'👨 Мужской' if user.gender == 'male' else ('👩 Женский' if user.gender == 'female' else '❓ Не указан')}\n"
        f"<b>Возраст:</b> {user.age or 'Не указан'}\n"
        f"<b>Длина ответов:</b> {'📏 Обычный' if getattr(user, 'response_length', 'normal') != 'short' else '📏 Короткий'}\n"
    )
    await message.answer(text, parse_mode='HTML', reply_markup=kb.user_settings_keyboard(user))


@router.callback_query(F.data == "menu_funnel_start")
async def menu_funnel_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await show_meditation_choices(callback, start_new_journey=True)


@router.callback_query(F.data == "menu_strategy_session")
async def menu_strategy_session(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await start_strategy_session(callback, state)


@router.callback_query(F.data == "funnel_intro_start")
async def funnel_intro_start(callback: CallbackQuery):
    await callback.answer()
    await show_meditation_choices(callback)


@router.callback_query(F.data == "funnel_about_method")
async def funnel_about_method(callback: CallbackQuery):
    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, callback.from_user.id)
        await register_event(session, callback.from_user.id, state.stage, "about_method_opened", meditation_id=state.meditation_id)
        await session.commit()

    content = await get_content("funnel_about_method")
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, начнём", callback_data="funnel_intro_start")
    builder.button(text="Главное меню", callback_data="funnel_to_main")
    builder.adjust(1)
    await callback.answer()
    await send_content(callback, content.text_content if content else "Подробнее о методе.", content.media if content else None, builder.as_markup())


@router.callback_query(F.data.startswith("funnel_choice_"))
async def funnel_choice_page(callback: CallbackQuery):
    page = int(callback.data.split("_")[-1])
    await callback.answer()
    await show_meditation_choices(callback, page)


@router.callback_query(F.data.startswith("funnel_card_"))
async def funnel_card(callback: CallbackQuery):
    _, _, meditation_id, page = callback.data.split("_")
    await callback.answer()
    await show_meditation_card(callback, int(meditation_id), int(page))


@router.callback_query(F.data.startswith("funnel_listen_"))
async def funnel_listen(callback: CallbackQuery):
    meditation_id = int(callback.data.split("_")[-1])
    await callback.answer()
    await send_meditation(callback, meditation_id)


@router.callback_query(F.data == "funnel_heard")
async def funnel_heard(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await request_reflection(callback, state)


@router.message(FunnelStates.waiting_reflection, ~F.text.startswith("/"))
async def funnel_reflection_message(message: Message, state: FSMContext):
    user_id = message.from_user.id
    from handlers import check_and_request_ai_disclaimer

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        funnel_state = await get_or_create_funnel_state(session, user_id)
        med_id = funnel_state.meditation_id if funnel_state else None
        if not user or not user.ai_disclaimer_accepted_at or (user.ai_disclaimer_version or 0) < 1:
            chat_id = message.chat.id if hasattr(message, "chat") and message.chat else user_id
            await check_and_request_ai_disclaimer(
                message.bot,
                user_id,
                chat_id,
                flow_type="funnel_reflection",
                payload={"reflection_text": message.text, "meditation_id": med_id},
            )
            return

    await deliver_analysis(message, state, message.text)


@router.callback_query(F.data.in_(["funnel_resume_choice", "funnel_restart"]))
async def funnel_resume_choice(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await show_meditation_choices(callback, start_new_journey=True)


@router.callback_query(F.data.in_(["funnel_interest_yes", "strategy_session_start"]))
async def funnel_interest_yes(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await show_vitaly_method(callback)


@router.callback_query(F.data == "funnel_offer_requested")
async def funnel_offer_requested(callback: CallbackQuery, state: FSMContext):
    if await _guard_crisis_commercial_path(callback, state=state):
        await callback.answer()
        return
    await state.clear()
    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, callback.from_user.id)
        await register_event(
            session,
            callback.from_user.id,
            funnel_state.stage,
            "offer_requested",
            meditation_id=funnel_state.meditation_id,
        )
        await session.commit()
    schedule_automation_events(
        callback.bot,
        user_id=callback.from_user.id,
        dialogue_id=1,
        scope="funnel",
        topic_slug="funnel_demo",
        events=("offer_requested",),
        metadata={"topic": "Стратегическая сессия"},
    )
    await callback.answer()
    await show_strategy_sales(callback)


@router.callback_query(F.data == "funnel_no_strategy_selected")
async def funnel_no_strategy_selected(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await show_no_strategy_options(callback)


@router.callback_query(F.data == "funnel_interest_no")
async def funnel_interest_no(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await show_no_strategy_options(callback)


@router.callback_query(F.data == "funnel_show_sales")
async def funnel_show_sales(callback: CallbackQuery, state: FSMContext):
    if await _guard_crisis_commercial_path(callback, state=state):
        await callback.answer()
        return
    await state.clear()
    await callback.answer()
    await show_strategy_sales(callback)


@router.callback_query(F.data == "funnel_vitaly_method")
async def funnel_vitaly_method(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await show_vitaly_method(callback)


@router.callback_query(F.data == "funnel_show_faq")
async def funnel_show_faq(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await show_strategy_faq(callback)


@router.callback_query(F.data.in_(["funnel_resume_strategy"]))
async def funnel_resume_strategy(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await show_vitaly_method(callback)


@router.callback_query(F.data == "funnel_resume_sales")
async def funnel_resume_sales(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await show_strategy_sales(callback)


@router.callback_query(F.data == "funnel_resume_materials")
async def funnel_resume_materials(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    from handlers import show_books
    await show_books(callback, bot=callback.bot)


@router.callback_query(F.data == "funnel_get_book")
async def funnel_get_book(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    from handlers import show_books
    await show_books(callback, bot=callback.bot)


@router.callback_query(F.data == "funnel_remind_7d")
async def funnel_remind_7d(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with _funnel_followup_lock(), async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, callback.from_user.id)
        user = await session.get(User, callback.from_user.id)
        config = await get_funnel_config(session)
        if user and user.crisis_detected_at:
            await cancel_nurture_campaign(session, funnel_state)
            funnel_state.is_active = False
            await session.commit()
            await callback.answer("Автоматические сообщения отключены для этого профиля", show_alert=True)
            return
        if not config.remarketing_enabled:
            await callback.answer("Ремаркетинг сейчас выключен", show_alert=True)
            return
        funnel_state.stage = "remarketing_7d"
        funnel_state.is_active = True
        funnel_state.next_followup_at = msk_now() + timedelta(days=7)
        funnel_state.followup_sequence_anchor = msk_now()
        funnel_state.reminder_count = 0
        await register_event(session, callback.from_user.id, "remarketing_7d", "remarketing_7d_scheduled")
        await session.commit()
    await callback.answer()
    await callback.message.answer("Договорились! Я напомню тебе о сессии через неделю. 👋")


@router.callback_query(F.data == "funnel_start_objections")
async def funnel_start_objections(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await start_objection_dialogue(callback, state)


@router.callback_query(F.data == "funnel_open_landing")
async def funnel_open_landing(callback: CallbackQuery, state: FSMContext | None = None):
    if await _guard_crisis_commercial_path(callback, state=state):
        await callback.answer()
        return
    await callback.answer()
    async with async_session_maker() as session:
        funnel_state = await get_or_create_funnel_state(session, callback.from_user.id)
        await register_event(
            session,
            callback.from_user.id,
            funnel_state.stage,
            "signup_clicked",
            meditation_id=funnel_state.meditation_id,
        )
        await session.commit()
    schedule_automation_events(
        callback.bot,
        user_id=callback.from_user.id,
        dialogue_id=1,
        scope="funnel",
        topic_slug="funnel_demo",
        events=("signup_clicked",),
        metadata={"topic": "Стратегическая сессия"},
    )
    origin_stage = None
    return_callback = None
    if state is not None:
        try:
            data = await state.get_data()
        except Exception:
            data = None
        if isinstance(data, dict):
            origin_stage = data.get("ai_landing_origin_stage")
            return_callback = data.get("ai_landing_return_callback")
    await open_landing(
        callback,
        origin_stage=origin_stage,
        return_callback=return_callback,
    )


@router.callback_query(F.data == "funnel_to_main")
async def funnel_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with async_session_maker() as session:
        await deactivate_funnel(session, callback.from_user.id, "paused")
        await register_event(session, callback.from_user.id, "paused", "return_to_main")
        await session.commit()
    await callback.answer()
    await show_main_menu(callback)


@router.message(FunnelStates.strategy_chat, ~F.text.startswith("/"))
async def strategy_chat_message(message: Message, state: FSMContext):
    await handle_strategy_message(message, state)


@router.message(FunnelStates.objection_chat, ~F.text.startswith("/"))
async def objection_chat_message(message: Message, state: FSMContext):
    await handle_objection_message(message, state)


def funnel_admin_menu_keyboard(role: AdminRole | str | None = None):
    builder = InlineKeyboardBuilder()
    buttons = [
        ("🧘 Медитации", "admin_funnel_meds_0"),
        ("✏️ Сообщения воронки", "admin_funnel_content"),
        ("⚙️ Настройки воронки", "admin_funnel_config"),
        ("📊 Аналитика", "admin_funnel_stats_all"),
        ("💳 Лиды и оплаты", "admin_funnel_landings_all_0"),
        ("🧪 Проверка и ручное обслуживание", "admin_funnel_tools"),
    ]
    for text, callback_data in buttons:
        permission = permission_for_callback(callback_data)
        if role is None or permission is None or role_has_permission(role, permission):
            builder.button(text=text, callback_data=callback_data)
    builder.button(text="⬅️ В контент и программы", callback_data="admin_section_content")
    builder.adjust(1)
    return builder.as_markup()


def _funnel_admin_cancel_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=callback_data)]]
    )


def _funnel_admin_confirmation_keyboard(confirm_callback: str, cancel_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, продолжить", callback_data=confirm_callback)],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data=cancel_callback)],
        ]
    )


async def _ask_funnel_admin_confirmation(
    callback: CallbackQuery,
    text: str,
    confirm_callback: str,
    cancel_callback: str,
) -> None:
    await send_content(
        callback,
        f"⚠️ <b>Подтвердите действие</b>\n\n{text}",
        reply_markup=_funnel_admin_confirmation_keyboard(confirm_callback, cancel_callback),
    )
    await callback.answer()


def funnel_stats_keyboard(period_key: str):
    builder = InlineKeyboardBuilder()
    for key, (label, _) in STATS_PERIODS.items():
        text = f"✅ {label}" if key == period_key else label
        builder.button(text=text, callback_data=f"admin_funnel_stats_{key}")
    builder.button(text="💾 Экспорт CSV", callback_data=f"admin_funnel_stats_export_{period_key}")
    builder.button(text="⬅️ Назад", callback_data="admin_funnel_settings")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def funnel_landing_list_keyboard(items: list[FunnelLanding], status_filter: str, page: int, total_pages: int):
    builder = InlineKeyboardBuilder()

    filter_buttons = []
    for key, label in LANDING_STATUS_FILTERS.items():
        text = f"✅ {label}" if key == status_filter else label
        filter_buttons.append(InlineKeyboardButton(text=text, callback_data=f"admin_funnel_landings_{key}_0"))
    builder.row(*filter_buttons)

    for landing in items:
        status_icon = "✅" if landing.status == "paid" else "📝" if landing.status == "form_submitted" else "🟡" if landing.status == "opened" else "❌"
        opened_str = format_msk(landing.opened_at, "%d.%m %H:%M")
        builder.row(InlineKeyboardButton(
            text=f"{status_icon} {opened_str} • {landing.user_id}",
            callback_data=f"admin_flanding_{landing.id}_{status_filter}_{page}",
        ))

    builder.row(*kb.standard_pagination_row(
        page,
        total_pages,
        lambda target: f"admin_funnel_landings_{status_filter}_{target}",
    ))

    builder.row(InlineKeyboardButton(text="💾 Экспорт CSV", callback_data=f"admin_flandings_export_{status_filter}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_funnel_settings"))
    return builder.as_markup()


def funnel_landing_detail_keyboard(landing_id: int, user_id: int, status_filter: str, page: int):
    builder = InlineKeyboardBuilder()
    context = f"_{status_filter}_{page}"
    builder.button(text="✅ Отметить как оплачено", callback_data=f"admin_flanding_mark_paid_{landing_id}{context}")
    builder.button(text="📝 Отметить как анкету", callback_data=f"admin_flanding_mark_form_{landing_id}{context}")
    builder.button(text="🟡 Отметить как открыта", callback_data=f"admin_flanding_mark_opened_{landing_id}{context}")
    builder.button(text="❌ Отметить как не оплачена", callback_data=f"admin_flanding_mark_failed_{landing_id}{context}")
    profile_status_code = {
        "all": "all",
        "opened": "opened",
        "form_submitted": "form",
        "paid": "paid",
        "failed": "failed",
    }.get(status_filter, "all")
    profile_return = f"landing_{profile_status_code}_{page}"
    builder.button(text="👤 Профиль клиента", callback_data=f"admin_user_profile_{user_id}:{profile_return}")
    builder.button(text="📄 Данные заявки", callback_data=f"admin_flanding_payload_{landing_id}")
    builder.button(text="⬅️ К списку", callback_data=f"admin_funnel_landings_{status_filter}_{page}")
    builder.adjust(2, 2, 1, 1, 1)
    return builder.as_markup()


def funnel_tools_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Запустить воронку себе", callback_data="admin_ftool_start_self")
    builder.button(text="🔗 Ссылка запуска воронки", callback_data="admin_ftool_link")
    builder.button(text="🧘 Показать выбор практик себе", callback_data="admin_ftool_choice_self")
    builder.button(text="⏰ Тестовое напоминание себе", callback_data="admin_ftool_followup_self")
    builder.button(text="♻️ Сбросить мою воронку", callback_data="admin_ftool_reset_self")
    builder.button(text="⬅️ Назад", callback_data="admin_funnel_settings")
    builder.adjust(1)
    return builder.as_markup()


def admin_user_funnel_keyboard(user_id: int, return_callback: str = "admin_clients_list_0"):
    builder = InlineKeyboardBuilder()
    context_suffix = ""
    if ":clientctx_" in return_callback:
        context_suffix = f":{return_callback.rsplit(':', 1)[1]}"
    builder.button(text="⏰ Отправить напоминание", callback_data=f"admin_user_funnel_followup_{user_id}{context_suffix}")
    builder.button(text="♻️ Сбросить состояние", callback_data=f"admin_user_funnel_reset_{user_id}{context_suffix}")
    builder.button(text="👤 К профилю", callback_data=f"admin_user_profile_{user_id}{context_suffix}")
    builder.button(text="⬅️ К списку", callback_data=return_callback)
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data == "admin_funnel_settings")
async def admin_funnel_settings(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    await callback.answer()
    role = await get_admin_role(callback.from_user.id)
    await send_content(
        callback,
        "🌀 <b>Воронка гипносессий</b>\n\n"
        "Здесь настраиваются практики, сообщения и автоматические напоминания после практики.\n"
        "Статус и результат изменения показываются на каждом экране.",
        None,
        funnel_admin_menu_keyboard(role),
    )


FUNNEL_CONTENT_ITEMS = {
    "funnel_intro": "Приветствие сценария",
    "funnel_about_method": "Описание метода",
    "funnel_choice_intro": "Экран выбора практики",
    "funnel_reflection_prompt": "Текст после практики (запрос ощущений/инсайтов)",
    "funnel_listen_instruction": "Инструкция перед прослушиванием",
    "funnel_after_analysis_offer": "Кнопка после разбора ИИ",
    "funnel_fallback_text": "Ответ при ошибке разбора",
    "crisis_contacts": "Контакты помощи при кризисе",
    "funnel_vitaly_method": "Блок о Виталии и методе",
    "funnel_strategy_sales": "Подробный оффер сессии",
    "funnel_strategy_faq": "FAQ по стратегической сессии",
    "funnel_no_strategy": "Ветка 'Пока нет'",
    "funnel_objection_intro": "Старт диалога по возражениям",
    "funnel_landing_intro": "Подводка к странице оплаты",
    "session_completed": "Экран завершения сессии ИИ",
    "funnel_resume_text": "Сообщение возврата",
    "funnel_timeout_unstarted": "Таймаут: медитация не начата (2ч)",
    "funnel_timeout_uncompleted": "Таймаут: рефлексия не сдана (2ч)",
    "funnel_reminder_step_1": "Напоминание после практики: шаг 1 (день 1)",
    "funnel_reminder_step_2": "Напоминание после практики: шаг 2 (день 2)",
    "funnel_reminder_step_3": "Напоминание после практики: шаг 3 (день 3)",
    "funnel_reminder_step_4": "Напоминание после практики: шаг 4 (день 7)",
    "funnel_reminder_step_5": "Напоминание после практики: шаг 5 (день 14)",
    "funnel_reminder_7d_remarketing": "Ремаркетинг отказа (через 7 дней)",
    "strategy_session_intro": "Вступление в диалог с ИИ",
    "disclaimer_text": "Дисклеймер",
    "btn_funnel_landing": "Кнопка страницы оплаты",
    "btn_vitaly_channel": "Кнопка канала Виталия",
    "btn_vitaly_questions": "Кнопка вопросов Виталию",
}


def funnel_content_keyboard():
    builder = InlineKeyboardBuilder()
    for key, label in FUNNEL_CONTENT_ITEMS.items():
        builder.button(text=label, callback_data=f"admin_fcont_{key}")
    builder.button(text="⬅️ Назад", callback_data="admin_funnel_settings")
    builder.adjust(1)
    return builder.as_markup()


async def show_admin_funnel_content(target):
    await send_content(target, "Выберите сообщение сценария для редактирования.", None, funnel_content_keyboard())


@router.callback_query(F.data == "admin_funnel_content")
async def admin_funnel_content(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    await callback.answer()
    await show_admin_funnel_content(callback)


@router.callback_query(F.data.startswith("admin_fcont_"))
async def admin_funnel_content_edit(callback: CallbackQuery, state: FSMContext):
    key = callback.data.replace("admin_fcont_", "", 1)
    if key not in FUNNEL_CONTENT_ITEMS:
        await callback.answer("Не найдено", show_alert=True)
        return
    await state.clear()
    await state.set_state(FunnelAdminStates.edit_content)
    await state.update_data(content_key=key)

    prompt = "Отправьте новый текст, фото или видео."
    if key.startswith("btn_"):
        prompt = "Отправьте новый текст кнопки."

    builder = InlineKeyboardBuilder()
    content = None
    async with async_session_maker() as session:
        content = await session.get(Content, key, options=[selectinload(Content.media)])
        if content and content.media:
            builder.button(text="🗑 Удалить медиа", callback_data=f"confirm_del_fcont_media_{key}")
    builder.button(text="⬅️ Назад", callback_data="admin_funnel_content")
    builder.adjust(1)
    await callback.answer()

    info_lines = [f"<b>{FUNNEL_CONTENT_ITEMS[key]}</b>\n"]
    if content:
        if content.text_content:
            preview_text = truncate_html_preview(content.text_content, max_length=1200)
            info_lines.append(f"<b>Текущий текст:</b>\n{preview_text}\n")
        if content.button_text:
            info_lines.append(f"<b>Текст кнопки:</b> <code>{content.button_text}</code>")
        if content.link_url:
            info_lines.append(f"<b>Ссылка:</b> <code>{content.link_url}</code>")
        if content.media:
            media_types = ", ".join(m.file_type for m in content.media)
            info_lines.append(f"<b>Текущее медиа:</b> {media_types}")
    info_lines.append(f"\n{prompt}")
    msg_text = "\n".join(info_lines)

    media = content.media if content else None
    await send_content(callback, msg_text, media, builder.as_markup())


async def _execute_funnel_content_media_delete(callback: CallbackQuery, state: FSMContext | None, key: str):
    if state is not None:
        await state.clear()
    async with async_session_maker() as session:
        await session.execute(delete(ContentMedia).where(ContentMedia.content_key == key))
        await session.commit()
    invalidate_content_cache(key)
    await callback.answer("Медиа удалено", show_alert=True)
    await show_admin_funnel_content(callback)


@router.callback_query(F.data.startswith("del_fcont_media_"))
async def admin_del_fcont_media(callback: CallbackQuery, state: FSMContext):
    key = callback.data.replace("del_fcont_media_", "", 1)
    if isinstance(callback, CallbackQuery):
        if key not in FUNNEL_CONTENT_ITEMS:
            await callback.answer("Элемент не найден.", show_alert=True)
            return
        async with async_session_maker() as session:
            content = await session.get(Content, key, options=[selectinload(Content.media)])
        if not content or not content.media:
            await callback.answer("Медиа уже удалено.", show_alert=True)
            return
        await _ask_funnel_admin_confirmation(
            callback,
            f"Медиа в разделе «{html.escape(FUNNEL_CONTENT_ITEMS[key])}» будет удалено. Текст сохранится.",
            f"confirmed_del_fcont_media_{key}",
            f"admin_fcont_{key}",
        )
        return
    await _execute_funnel_content_media_delete(callback, state, key)


@router.callback_query(F.data.startswith("confirmed_del_fcont_media_"))
async def confirmed_admin_del_fcont_media(callback: CallbackQuery, state: FSMContext | None = None):
    key = callback.data.removeprefix("confirmed_del_fcont_media_")
    if key not in FUNNEL_CONTENT_ITEMS:
        await callback.answer("Элемент не найден.", show_alert=True)
        return
    await _execute_funnel_content_media_delete(callback, state, key)


@router.callback_query(F.data.startswith("confirm_del_fcont_media_"))
async def confirm_del_fcont_media(callback: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    key = callback.data.removeprefix("confirm_del_fcont_media_")
    if key not in FUNNEL_CONTENT_ITEMS:
        await callback.answer("Элемент не найден.", show_alert=True)
        return
    async with async_session_maker() as session:
        content = await session.get(Content, key, options=[selectinload(Content.media)])
    if not content or not content.media:
        await callback.answer("Медиа уже удалено.", show_alert=True)
        return
    await _ask_funnel_admin_confirmation(
        callback,
        f"Медиа в разделе «{html.escape(FUNNEL_CONTENT_ITEMS[key])}» будет удалено. Текст сохранится.",
        f"confirmed_del_fcont_media_{key}",
        f"admin_fcont_{key}",
    )


@router.message(FunnelAdminStates.edit_content, F.text, ~F.text.startswith("/"))
async def admin_funnel_content_save(message: Message, state: FSMContext):
    if (message.text or "").strip().lower() in ("/cancel", "отмена", "назад"):
        await state.clear()
        await message.answer("Редактирование отменено.")
        await show_admin_funnel_content(message)
        return
    data = await state.get_data()
    key = data["content_key"]
    text_value = message.html_text or message.caption or message.text

    async with async_session_maker() as session:
        content = await session.get(Content, key, options=[selectinload(Content.media)])
        if not content:
            content = Content(key=key)
            session.add(content)

        if key.startswith("btn_"):
            content.button_text = text_value
            await session.commit()
            invalidate_content_cache(key)
            await state.set_state(FunnelAdminStates.edit_content_url)
            await message.answer("Теперь отправьте ссылку для кнопки.")
            return

        if text_value and text_value != ".":
            content.text_content = text_value

        has_media = bool(content.media) if content.media else False
        if message.photo or message.video:
            await session.execute(delete(ContentMedia).where(ContentMedia.content_key == key))
            if message.photo:
                session.add(ContentMedia(content_key=key, file_type="photo", file_id=message.photo[-1].file_id))
            elif message.video:
                session.add(ContentMedia(content_key=key, file_type="video", file_id=message.video.file_id))
            has_media = True

        await session.commit()
        invalidate_content_cache(key)

        # Предупреждение о длине caption
        final_text = content.text_content or ""
        if has_media and len(final_text) > 1024:
            await message.answer(
                f"⚠️ Текст ({len(final_text)} символов) превышает лимит подписи к медиа (1024). "
                f"При отправке пользователю медиа и текст будут отправлены отдельными сообщениями. "
                f"Сократите текст до 1024 символов, чтобы они были в одном сообщении."
            )

    await state.clear()
    await message.answer("Контент обновлён.")
    await show_admin_funnel_content(message)


@router.message(FunnelAdminStates.edit_content, F.photo | F.video)
async def admin_funnel_content_save_media(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data["content_key"]
    text_value = message.caption or ""

    async with async_session_maker() as session:
        content = await session.get(Content, key, options=[selectinload(Content.media)])
        if not content:
            content = Content(key=key)
            session.add(content)

        if text_value and text_value != "." and not key.startswith("btn_"):
            content.text_content = text_value

        await session.execute(delete(ContentMedia).where(ContentMedia.content_key == key))
        if message.photo:
            session.add(ContentMedia(content_key=key, file_type="photo", file_id=message.photo[-1].file_id))
        elif message.video:
            session.add(ContentMedia(content_key=key, file_type="video", file_id=message.video.file_id))
        await session.commit()
        invalidate_content_cache(key)

        # Предупреждение о длине caption
        final_text = content.text_content or ""
        if len(final_text) > 1024:
            await message.answer(
                f"⚠️ Текст ({len(final_text)} символов) превышает лимит подписи к медиа (1024). "
                f"При отправке медиа и текст будут отдельными сообщениями. "
                f"Сократите текст до 1024 символов, чтобы они были в одном."
            )

    await state.clear()
    await message.answer("Контент обновлён.")
    await show_admin_funnel_content(message)


@router.message(FunnelAdminStates.edit_content_url, ~F.text.startswith("/"))
async def admin_funnel_content_url_save(message: Message, state: FSMContext):
    if message.text.strip().lower() in ("/cancel", "отмена", "назад"):
        await state.clear()
        await message.answer("Редактирование отменено.")
        await show_admin_funnel_content(message)
        return
    data = await state.get_data()
    key = data["content_key"]
    async with async_session_maker() as session:
        content = await session.get(Content, key)
        if content and message.text.strip() != ".":
            content.link_url = message.text.strip()
            await session.commit()
            invalidate_content_cache(key)

    await state.clear()
    await message.answer("Кнопка обновлена.")
    await show_admin_funnel_content(message)


def funnel_config_keyboard(config: FunnelConfig | None = None):
    timeouts_status = "✅ ВКЛ" if config is None or config.meditation_timeouts_enabled else "⛔ ВЫКЛ"
    nurture_status = "✅ ВКЛ" if config is None or config.nurture_enabled else "⛔ ВЫКЛ"
    remarketing_status = "✅ ВКЛ" if config is None or config.remarketing_enabled else "⛔ ВЫКЛ"
    builder = InlineKeyboardBuilder()
    builder.button(text="📄 Медитаций на страницу", callback_data="admin_fcfg_page")
    builder.button(text="⏱ Интервал таймаута медитации", callback_data="admin_fcfg_timeout")
    builder.button(text=f"⏱ Таймауты: {timeouts_status}", callback_data="admin_fcfg_toggle_timeouts")
    builder.button(text="📅 Интервалы после практики (дни)", callback_data="admin_fcfg_intervals")
    builder.button(text=f"📅 Напоминания после практики: {nurture_status}", callback_data="admin_fcfg_toggle_nurture")
    builder.button(text=f"🔁 Повторное предложение через 7 дней: {remarketing_status}", callback_data="admin_fcfg_toggle_remarketing")
    builder.button(text="🚪 Окно возврата (часы)", callback_data="admin_fcfg_resume")
    builder.button(text="⬅️ Назад", callback_data="admin_funnel_settings")
    builder.adjust(1)
    return builder.as_markup()


async def show_admin_funnel_config(target):
    async with async_session_maker() as session:
        config = await get_funnel_config(session)
        intervals = parse_reminder_intervals(config.reminder_intervals)
        n_touches = len(intervals)
        timeouts_status = "включены" if config.meditation_timeouts_enabled else "выключены"
        nurture_status = "включён" if config.nurture_enabled else "выключен"
        remarketing_status = "включён" if config.remarketing_enabled else "выключен"
        text = (
            f"<b>⚙️ Настройки воронки</b>\n\n"
            f"📄 Медитаций на страницу: <code>{config.page_size}</code>\n"
            f"⏱ Таймауты неактивности: <b>{timeouts_status}</b>, <code>{config.meditation_timeout_hours or 2.0} ч.</code>\n"
            f"📅 Интервалы напоминаний после практики: <code>{config.reminder_intervals or '1, 2, 3, 7, 14'}</code>\n"
            f"Напоминания после практики: <b>{nurture_status}</b>\n"
            f"Количество касаний: {n_touches} из 5\n"
            f"🔁 Ремаркетинг через 7 дней: <b>{remarketing_status}</b>\n"
            f"🚪 Окно возврата: <code>{config.resume_after_hours} ч.</code>"
        )
    await send_content(target, text, None, funnel_config_keyboard(config))


@router.callback_query(F.data == "admin_funnel_config")
async def admin_funnel_config(callback: CallbackQuery, state: FSMContext = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state:
        await state.clear()
    await callback.answer()
    await show_admin_funnel_config(callback)


@router.callback_query(F.data == "admin_fcfg_toggle_timeouts")
async def admin_fcfg_toggle_timeouts(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    async with _funnel_followup_lock(), async_session_maker() as session:
        config = await get_funnel_config(session)
        config.meditation_timeouts_enabled = not config.meditation_timeouts_enabled
        enabled = config.meditation_timeouts_enabled
        if not enabled:
            await session.execute(
                update(FunnelState)
                .where(FunnelState.stage_timeout_at != None)
                .values(stage_timeout_at=None, stage_timeout_sent=True)
            )
        await session.commit()
    await callback.answer(f"Таймауты неактивности {'включены' if enabled else 'выключены'}")
    await show_admin_funnel_config(callback)


@router.callback_query(F.data == "admin_fcfg_toggle_nurture")
async def admin_fcfg_toggle_nurture(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    async with _funnel_followup_lock(), async_session_maker() as session:
        config = await get_funnel_config(session)
        config.nurture_enabled = not config.nurture_enabled
        enabled = config.nurture_enabled
        if not enabled:
            await session.execute(
                update(FunnelState)
                .where(
                    FunnelState.next_followup_at != None,
                    FunnelState.stage != "remarketing_7d",
                )
                .values(next_followup_at=None, followup_sequence_anchor=None)
            )
        await session.commit()
    await callback.answer(f"Напоминания после практики {'включены' if enabled else 'выключены'}")
    await show_admin_funnel_config(callback)


@router.callback_query(F.data == "admin_fcfg_toggle_remarketing")
async def admin_fcfg_toggle_remarketing(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    async with _funnel_followup_lock(), async_session_maker() as session:
        config = await get_funnel_config(session)
        config.remarketing_enabled = not config.remarketing_enabled
        enabled = config.remarketing_enabled
        if not enabled:
            await session.execute(
                update(FunnelState)
                .where(
                    FunnelState.stage == "remarketing_7d",
                    FunnelState.next_followup_at != None,
                )
                .values(
                    next_followup_at=None,
                    followup_sequence_anchor=None,
                    is_active=False,
                    stage="completed_unconverted",
                )
            )
        await session.commit()
    await callback.answer(f"Ремаркетинг через 7 дней {'включён' if enabled else 'выключен'}")
    await show_admin_funnel_config(callback)


@router.callback_query(F.data == "admin_fcfg_page")
async def admin_fcfg_page(callback: CallbackQuery, state: FSMContext):
    async with async_session_maker() as session:
        config = await get_funnel_config(session)
    await state.clear()
    await state.set_state(FunnelAdminStates.set_page_size)
    await callback.answer()
    await callback.message.answer(
        f"Текущее значение: <b>{config.page_size}</b>\n\n"
        f"Введи новое количество медитаций на страницу (целое число) или /cancel:",
        parse_mode="HTML",
        reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_config"),
    )


@router.callback_query(F.data == "admin_fcfg_timeout")
async def admin_fcfg_timeout(callback: CallbackQuery, state: FSMContext):
    async with async_session_maker() as session:
        config = await get_funnel_config(session)
    await state.clear()
    await state.set_state(FunnelAdminStates.set_meditation_timeout)
    await callback.answer()
    await callback.message.answer(
        f"Текущее значение: <b>{config.meditation_timeout_hours or 2.0} ч.</b>\n\n"
        f"Введи таймаут неактивности медитации в часах (например, 2 или 1.5) или /cancel:",
        parse_mode="HTML",
        reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_config"),
    )


@router.callback_query(F.data == "admin_fcfg_intervals")
async def admin_fcfg_intervals(callback: CallbackQuery, state: FSMContext):
    async with async_session_maker() as session:
        config = await get_funnel_config(session)
    await state.clear()
    await state.set_state(FunnelAdminStates.set_nurture_intervals)
    await callback.answer()
    await callback.message.answer(
        f"Текущие интервалы: <b>{config.reminder_intervals or '1, 2, 3, 7, 14'}</b>\n\n"
        f"Введи последовательность дней через запятую (например: 1, 2, 3, 7, 14) или /cancel:",
        parse_mode="HTML",
        reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_config"),
    )


@router.callback_query(F.data == "admin_fcfg_resume")
async def admin_fcfg_resume(callback: CallbackQuery, state: FSMContext):
    async with async_session_maker() as session:
        config = await get_funnel_config(session)
    await state.clear()
    await state.set_state(FunnelAdminStates.set_resume_hours)
    await callback.answer()
    await callback.message.answer(
        f"Текущее значение: <b>{config.resume_after_hours} ч.</b>\n\n"
        f"Введи количество часов окна возврата (целое число) или /cancel:",
        parse_mode="HTML",
        reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_config"),
    )


@router.message(FunnelAdminStates.set_page_size, ~F.text.startswith("/"))
async def save_page_size(message: Message, state: FSMContext):
    if message.text.strip().lower() in ("отмена", "назад"):
        await state.clear()
        await show_admin_funnel_config(message)
        return
    try:
        val = int(message.text.strip())
        if val <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи положительное целое число или /cancel:")
        return
    async with async_session_maker() as session:
        config = await get_funnel_config(session)
        config.page_size = val
        await session.commit()
    await state.clear()
    await show_admin_funnel_config(message)


@router.message(FunnelAdminStates.set_meditation_timeout, ~F.text.startswith("/"))
async def save_meditation_timeout(message: Message, state: FSMContext):
    if message.text.strip().lower() in ("отмена", "назад"):
        await state.clear()
        await show_admin_funnel_config(message)
        return
    try:
        val = float(message.text.strip().replace(",", "."))
        if val <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи положительное число (например 2 или 1.5) или /cancel:")
        return
    async with async_session_maker() as session:
        config = await get_funnel_config(session)
        config.meditation_timeout_hours = val
        await session.commit()
    await state.clear()
    await show_admin_funnel_config(message)


def validate_nurture_intervals(text: str) -> list[int] | None:
    try:
        parts = [int(p.strip()) for p in text.strip().split(",") if p.strip()]
    except Exception:
        return None
    if not (1 <= len(parts) <= 5):
        return None
    if any(p <= 0 for p in parts):
        return None
    for i in range(len(parts) - 1):
        if parts[i] >= parts[i + 1]:
            return None
    return parts


@router.message(FunnelAdminStates.set_nurture_intervals, ~F.text.startswith("/"))
async def save_nurture_intervals(message: Message, state: FSMContext):
    if message.text.strip().lower() in ("отмена", "назад"):
        await state.clear()
        await show_admin_funnel_config(message)
        return
    valid_parts = validate_nurture_intervals(message.text)
    if not valid_parts:
        await message.answer(
            "Некорректный ввод! Введи от 1 до 5 строго возрастающих положительных чисел через запятую (например: 1, 2, 3, 7, 14) или /cancel:"
        )
        return
    norm_val = ", ".join(str(p) for p in valid_parts)
    async with async_session_maker() as session:
        config = await get_funnel_config(session)
        config.reminder_intervals = norm_val
        await session.commit()
    await state.clear()
    await show_admin_funnel_config(message)


@router.message(FunnelAdminStates.set_resume_hours, ~F.text.startswith("/"))
async def save_resume_hours(message: Message, state: FSMContext):
    if message.text.strip().lower() in ("отмена", "назад"):
        await state.clear()
        await show_admin_funnel_config(message)
        return
    try:
        val = int(message.text.strip())
        if val <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи положительное целое число или /cancel:")
        return
    async with async_session_maker() as session:
        config = await get_funnel_config(session)
        config.resume_after_hours = val
        await session.commit()
    await state.clear()
    await show_admin_funnel_config(message)


async def meditation_list_markup(page: int = 0):
    async with async_session_maker() as session:
        meditations = (
            await session.execute(select(FunnelMeditation).order_by(FunnelMeditation.sort_order.asc(), FunnelMeditation.id.asc()))
        ).scalars().all()
        config = await get_funnel_config(session)

    builder = InlineKeyboardBuilder()
    page_size = max(1, config.page_size)
    total_pages = max(1, (len(meditations) + page_size - 1) // page_size)
    page = min(max(page, 0), total_pages - 1)
    page_items = meditations[page * page_size:(page + 1) * page_size]

    for meditation in page_items:
        status = "✅" if meditation.is_active else "❌"
        builder.row(InlineKeyboardButton(
            text=f"{status} {meditation.sort_order}. {meditation.title}",
            callback_data=f"admin_fmed_{meditation.id}_{page}",
        ))

    builder.row(*kb.standard_pagination_row(
        page,
        total_pages,
        lambda target: f"admin_funnel_meds_{target}",
    ))
    builder.row(InlineKeyboardButton(text="➕ Добавить медитацию", callback_data="admin_fmed_add"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_funnel_settings"))

    text = "Список медитаций. Нажмите на элемент для управления."
    return text, builder.as_markup()


async def show_admin_meditations(target, page: int = 0):
    text, markup = await meditation_list_markup(page)
    await send_content(target, text, None, markup)


@router.callback_query(F.data.startswith("admin_funnel_meds_"))
async def admin_funnel_meds(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    page = int(callback.data.split("_")[-1])
    if state is not None:
        await state.clear()
    await callback.answer()
    await show_admin_meditations(callback, page)


def admin_meditation_detail_keyboard(meditation_id: int, page: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="⬆️ Поднять", callback_data=f"admin_fmed_up_{meditation_id}_{page}")
    builder.button(text="⬇️ Опустить", callback_data=f"admin_fmed_down_{meditation_id}_{page}")
    builder.button(text="✏️ Название", callback_data=f"admin_fmededit_title_{meditation_id}_{page}")
    builder.button(text="✏️ Длительность", callback_data=f"admin_fmededit_duration_{meditation_id}_{page}")
    builder.button(text="✏️ Короткое описание", callback_data=f"admin_fmededit_short_{meditation_id}_{page}")
    builder.button(text="✏️ Полное описание", callback_data=f"admin_fmededit_full_{meditation_id}_{page}")
    builder.button(text="🖼 Обновить аудио/видео", callback_data=f"admin_fmededit_media_{meditation_id}_{page}")
    builder.button(text="🔄 Вкл/Выкл", callback_data=f"admin_fmedtoggle_{meditation_id}_{page}")
    builder.button(text="🗑 Удалить", callback_data=f"confirm_admin_fmeddel_{meditation_id}_{page}")
    builder.button(text="⬅️ К списку", callback_data=f"admin_funnel_meds_{page}")
    builder.adjust(2, 1)
    return builder.as_markup()


async def show_meditation_admin_detail(target, med_id: int, page: int):
    async with async_session_maker() as session:
        meditation = await session.get(FunnelMeditation, med_id)
    if not meditation:
        return

    text = (
        f"<b>{html.escape(meditation.title)}</b>\n\n"
        f"Порядок: {meditation.sort_order}\n"
        f"Статус: {'Активна' if meditation.is_active else 'Отключена'}\n"
        f"Длительность: {meditation.duration_minutes} мин\n"
        f"Короткое описание: {html.escape(meditation.short_description or '-')}\n\n"
        f"Полное описание:\n{html.escape(meditation.full_description or '-')}\n\n"
        f"Медиа: {meditation.file_type or 'не загружено'}"
    )
    await send_content(target, text, None, admin_meditation_detail_keyboard(med_id, page))


@router.callback_query(F.data.regexp(r"^admin_fmed_\d+_\d+$"))
async def admin_fmed_detail(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    _, _, med_id, page = callback.data.split("_")
    await callback.answer()
    await show_meditation_admin_detail(callback, int(med_id), int(page))


@router.callback_query(F.data == "admin_fmed_add")
async def admin_fmed_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FunnelAdminStates.add_title)
    await callback.answer()
    await callback.message.answer(
        "Введи название медитации.",
        reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_meds_0"),
    )


@router.message(FunnelAdminStates.add_title, ~F.text.startswith("/"))
async def add_med_title(message: Message, state: FSMContext):
    if message.text.strip().lower() in ("/cancel", "отмена", "назад"):
        await state.clear()
        await show_admin_meditations(message)
        return
    title = message.text.strip()
    if not title:
        await message.answer(
            "Название не должно быть пустым. Введи его ещё раз.",
            reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_meds_0"),
        )
        return
    await state.update_data(new_title=title)
    await state.set_state(FunnelAdminStates.add_duration)
    await message.answer(
        "Введи длительность в минутах, например 10.",
        reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_meds_0"),
    )


@router.message(FunnelAdminStates.add_duration, ~F.text.startswith("/"))
async def add_med_duration(message: Message, state: FSMContext):
    if message.text.strip().lower() in ("/cancel", "отмена", "назад"):
        await state.clear()
        await show_admin_meditations(message)
        return
    try:
        duration = int(message.text.strip())
    except ValueError:
        await message.answer(
            "Введи положительную длительность в минутах, например 10.",
            reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_meds_0"),
        )
        return
    if duration <= 0:
        await message.answer(
            "Длительность должна быть больше нуля. Например: 10.",
            reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_meds_0"),
        )
        return
    await state.update_data(new_duration=duration)
    await state.set_state(FunnelAdminStates.add_short)
    await message.answer(
        "Введи короткое описание практики.",
        reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_meds_0"),
    )


@router.message(FunnelAdminStates.add_short, ~F.text.startswith("/"))
async def add_med_short(message: Message, state: FSMContext):
    if message.text.strip().lower() in ("/cancel", "отмена", "назад"):
        await state.clear()
        await show_admin_meditations(message)
        return
    await state.update_data(new_short=message.text.strip())
    await state.set_state(FunnelAdminStates.add_full)
    await message.answer(
        "Введи полное описание практики.",
        reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_meds_0"),
    )


@router.message(FunnelAdminStates.add_full, ~F.text.startswith("/"))
async def add_med_full(message: Message, state: FSMContext):
    if message.text.strip().lower() in ("/cancel", "отмена", "назад"):
        await state.clear()
        await show_admin_meditations(message)
        return
    await state.update_data(new_full=message.text.strip(), new_url=None)
    await state.set_state(FunnelAdminStates.add_media)
    await message.answer(
        "Теперь отправь аудио или видео практики. Если пока без медиа, отправь точку «.».",
        reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_meds_0"),
    )


@router.message(FunnelAdminStates.add_url, ~F.text.startswith("/"))
async def add_med_url(message: Message, state: FSMContext):
    url = message.text.strip()
    await state.update_data(new_url=None if url == "." else url)
    await state.set_state(FunnelAdminStates.add_media)
    await message.answer(
        "Теперь отправь аудио или видео практики. Если пока без медиа, отправь точку «.».",
        reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_meds_0"),
    )


@router.message(FunnelAdminStates.add_media, F.audio | F.video)
async def add_med_media(message: Message, state: FSMContext):
    data = await state.get_data()
    async with async_session_maker() as session:
        current_max = await session.scalar(select(func.max(FunnelMeditation.sort_order)))
        meditation = FunnelMeditation(
            title=data["new_title"],
            duration_minutes=data["new_duration"],
            short_description=data["new_short"],
            full_description=data["new_full"],
            file_id=message.audio.file_id if message.audio else message.video.file_id,
            file_type="audio" if message.audio else "video",
            sort_order=(current_max or 0) + 1,
        )
        session.add(meditation)
        await session.commit()
    await state.clear()
    await message.answer("Медитация добавлена.")
    await show_admin_meditations(message)


@router.message(FunnelAdminStates.add_media, ~F.text.startswith("/"))
async def add_med_without_media(message: Message, state: FSMContext):
    if message.text.strip().lower() in ("/cancel", "отмена", "назад"):
        await state.clear()
        await show_admin_meditations(message)
        return
    if message.text.strip() != ".":
        await message.answer(
            "Отправь аудио или видео либо точку «.».",
            reply_markup=_funnel_admin_cancel_keyboard("admin_funnel_meds_0"),
        )
        return
    data = await state.get_data()
    async with async_session_maker() as session:
        current_max = await session.scalar(select(func.max(FunnelMeditation.sort_order)))
        meditation = FunnelMeditation(
            title=data["new_title"],
            duration_minutes=data["new_duration"],
            short_description=data["new_short"],
            full_description=data["new_full"],
            sort_order=(current_max or 0) + 1,
        )
        session.add(meditation)
        await session.commit()
    await state.clear()
    await message.answer("Медитация добавлена без медиа.")
    await show_admin_meditations(message)


async def swap_meditation_order(session, meditation_id: int, direction: str):
    current = await session.get(FunnelMeditation, meditation_id)
    if not current:
        return
    if direction == "up":
        neighbor = await session.scalar(
            select(FunnelMeditation)
            .where(FunnelMeditation.sort_order < current.sort_order)
            .order_by(FunnelMeditation.sort_order.desc())
        )
    else:
        neighbor = await session.scalar(
            select(FunnelMeditation)
            .where(FunnelMeditation.sort_order > current.sort_order)
            .order_by(FunnelMeditation.sort_order.asc())
        )
    if not neighbor:
        return
    current.sort_order, neighbor.sort_order = neighbor.sort_order, current.sort_order


@router.callback_query(F.data.startswith("admin_fmed_up_"))
async def admin_fmed_up(callback: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    _, _, _, meditation_id, page = callback.data.split("_")
    async with async_session_maker() as session:
        await swap_meditation_order(session, int(meditation_id), "up")
        await session.commit()
    await callback.answer()
    await show_admin_meditations(callback, int(page))


@router.callback_query(F.data.startswith("admin_fmed_down_"))
async def admin_fmed_down(callback: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    _, _, _, meditation_id, page = callback.data.split("_")
    async with async_session_maker() as session:
        await swap_meditation_order(session, int(meditation_id), "down")
        await session.commit()
    await callback.answer()
    await show_admin_meditations(callback, int(page))


@router.callback_query(F.data.startswith("admin_fmedtoggle_"))
async def admin_fmed_toggle(callback: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    _, _, meditation_id, page = callback.data.split("_")
    async with async_session_maker() as session:
        meditation = await session.get(FunnelMeditation, int(meditation_id))
        if meditation:
            meditation.is_active = not meditation.is_active
            await session.commit()
    await callback.answer()
    await show_meditation_admin_detail(callback, int(meditation_id), int(page))


async def _execute_funnel_meditation_delete(
    callback: CallbackQuery,
    state: FSMContext | None,
    meditation_id: int,
    page: int,
):
    if state is not None:
        await state.clear()
    async with async_session_maker() as session:
        meditation = await session.get(FunnelMeditation, meditation_id)
        if meditation:
            await session.delete(meditation)
            await session.commit()
    await callback.answer("Удалено")
    await show_admin_meditations(callback, page)


@router.callback_query(F.data.startswith("admin_fmeddel_"))
async def admin_fmed_delete(callback: CallbackQuery, state: FSMContext | None = None):
    parts = callback.data.split("_")
    if len(parts) != 4:
        await callback.answer("Неверные параметры.", show_alert=True)
        return
    meditation_id, page = int(parts[2]), int(parts[3])
    if isinstance(callback, CallbackQuery):
        async with async_session_maker() as session:
            meditation = await session.get(FunnelMeditation, meditation_id)
        if not meditation:
            await callback.answer("Практика уже удалена.", show_alert=True)
            return
        await _ask_funnel_admin_confirmation(
            callback,
            f"Практика «{html.escape(meditation.title)}» будет удалена без возможности восстановления.",
            f"confirmed_admin_fmeddel_{meditation_id}_{page}",
            f"admin_fmed_{meditation_id}_{page}",
        )
        return
    await _execute_funnel_meditation_delete(callback, state, meditation_id, page)


@router.callback_query(F.data.startswith("confirmed_admin_fmeddel_"))
async def confirmed_admin_fmed_delete(callback: CallbackQuery, state: FSMContext | None = None):
    parts = callback.data.split("_")
    if len(parts) != 5:
        await callback.answer("Неверные параметры.", show_alert=True)
        return
    meditation_id, page = int(parts[3]), int(parts[4])
    await _execute_funnel_meditation_delete(callback, state, meditation_id, page)


@router.callback_query(F.data.startswith("confirm_admin_fmeddel_"))
async def confirm_admin_fmed_delete(callback: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    parts = callback.data.split("_")
    if len(parts) != 5:
        await callback.answer("Неверные параметры.", show_alert=True)
        return
    meditation_id, page = int(parts[3]), int(parts[4])
    async with async_session_maker() as session:
        meditation = await session.get(FunnelMeditation, meditation_id)
    if not meditation:
        await callback.answer("Практика уже удалена.", show_alert=True)
        return
    await _ask_funnel_admin_confirmation(
        callback,
        f"Практика «{html.escape(meditation.title)}» будет удалена без возможности восстановления.",
        f"confirmed_admin_fmeddel_{meditation_id}_{page}",
        f"admin_fmed_{meditation_id}_{page}",
    )


@router.callback_query(F.data.startswith("admin_fmededit_"))
async def admin_fmed_edit(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    field_name = parts[2]
    meditation_id = int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 0
    await state.clear()
    await state.update_data(edit_med_id=meditation_id, edit_med_field=field_name, edit_med_page=page)

    async with async_session_maker() as session:
        meditation = await session.get(FunnelMeditation, meditation_id)

    if not meditation:
        await callback.answer("Практика не найдена", show_alert=True)
        return

    FIELD_LABELS = {
        "title": "Название практики",
        "duration": "Длительность (мин)",
        "short": "Краткое описание",
        "full": "Полное описание",
        "media": "Медиафайл (аудио/видео)",
    }
    field_title = FIELD_LABELS.get(field_name, field_name)

    safe_title = html.escape(str(meditation.title or ""))
    safe_field_title = html.escape(str(field_title or ""))

    if field_name == "media":
        await state.set_state(FunnelAdminStates.edit_meditation_media)
        cur_val = f"{meditation.file_type}: {meditation.file_id}" if meditation.file_id else "Медиа отсутствует"
        safe_cur_val = html.escape(str(cur_val))
        msg_text = (
            f"✏️ <b>Редактирование медитации: {safe_title}</b>\n\n"
            f"<b>Поле:</b> {safe_field_title}\n"
            f"<b>Текущее значение:</b> {safe_cur_val}\n\n"
            f"Отправьте новое аудио/видео или точку «.», чтобы удалить медиа."
        )
        if meditation.file_id:
            try:
                if meditation.file_type == "video":
                    await callback.message.answer_video(meditation.file_id, caption="Текущее видео")
                elif meditation.file_type == "audio":
                    await callback.message.answer_audio(meditation.file_id, caption="Текущее аудио")
            except Exception:
                pass
        if hasattr(callback.message, "answer") and callable(callback.message.answer):
            await callback.message.answer(
                msg_text,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=_funnel_admin_cancel_keyboard(f"admin_fmed_{meditation_id}_{page}"),
            )
        else:
            bot = getattr(callback, "bot", getattr(callback.message, "bot", None))
            if bot:
                await bot.send_message(
                    callback.message.chat.id,
                    msg_text,
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    reply_markup=_funnel_admin_cancel_keyboard(f"admin_fmed_{meditation_id}_{page}"),
                )
    else:
        await state.set_state(FunnelAdminStates.edit_meditation_text)
        if field_name == "title":
            cur_val = meditation.title
        elif field_name == "duration":
            cur_val = f"{meditation.duration_minutes} мин"
        elif field_name == "short":
            cur_val = meditation.short_description or "Не указано"
        elif field_name == "full":
            cur_val = meditation.full_description or "Не указано"
        else:
            cur_val = "Не указано"
        safe_cur_val = html.escape(str(cur_val))
        msg_text = (
            f"✏️ <b>Редактирование медитации: {safe_title}</b>\n\n"
            f"<b>Поле:</b> {safe_field_title}\n"
            f"<b>Текущее значение:</b>\n{safe_cur_val}\n\n"
            f"Отправьте новое значение или нажмите «Отмена»."
        )
        if hasattr(callback.message, "answer") and callable(callback.message.answer):
            await callback.message.answer(
                msg_text,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=_funnel_admin_cancel_keyboard(f"admin_fmed_{meditation_id}_{page}"),
            )
        else:
            bot = getattr(callback, "bot", getattr(callback.message, "bot", None))
            if bot:
                await bot.send_message(
                    callback.message.chat.id,
                    msg_text,
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    reply_markup=_funnel_admin_cancel_keyboard(f"admin_fmed_{meditation_id}_{page}"),
                )
    await callback.answer()


@router.message(FunnelAdminStates.edit_meditation_text, ~F.text.startswith("/"))
async def admin_fmed_edit_text_save(message: Message, state: FSMContext):
    data = await state.get_data()
    med_id = data["edit_med_id"]
    field_name = data["edit_med_field"]
    page = data.get("edit_med_page", 0)
    value = message.text.strip()
    cancel_callback = f"admin_fmed_{med_id}_{page}"
    if value.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await show_meditation_admin_detail(message, med_id, page)
        return
    if field_name in {"title", "short", "full"} and not value:
        await message.answer(
            "Значение не должно быть пустым. Введите его ещё раз.",
            reply_markup=_funnel_admin_cancel_keyboard(cancel_callback),
        )
        return

    async with async_session_maker() as session:
        meditation = await session.get(FunnelMeditation, med_id)
        if meditation:
            if field_name == "title":
                meditation.title = value
            elif field_name == "duration":
                try:
                    meditation.duration_minutes = int(value)
                except ValueError:
                    await message.answer(
                        "Введите положительную длительность в минутах, например 10.",
                        reply_markup=_funnel_admin_cancel_keyboard(cancel_callback),
                    )
                    return
                if meditation.duration_minutes <= 0:
                    await message.answer(
                        "Длительность должна быть больше нуля.",
                        reply_markup=_funnel_admin_cancel_keyboard(cancel_callback),
                    )
                    return
            elif field_name == "short":
                meditation.short_description = value
            elif field_name == "full":
                meditation.full_description = value
            elif field_name == "url":
                meditation.reserve_url = None if value == "." else value
            await session.commit()

    await state.clear()
    await message.answer("Изменение сохранено.")
    await show_meditation_admin_detail(message, med_id, page)


@router.message(FunnelAdminStates.edit_meditation_media, F.audio | F.video)
async def admin_fmed_edit_media_save(message: Message, state: FSMContext):
    data = await state.get_data()
    med_id = data["edit_med_id"]
    page = data.get("edit_med_page", 0)
    async with async_session_maker() as session:
        meditation = await session.get(FunnelMeditation, med_id)
        if meditation:
            meditation.file_id = message.audio.file_id if message.audio else message.video.file_id
            meditation.file_type = "audio" if message.audio else "video"
            await session.commit()
    await state.clear()
    await message.answer("Медиа обновлено.")
    await show_meditation_admin_detail(message, med_id, page)


@router.message(FunnelAdminStates.edit_meditation_media, ~F.text.startswith("/"))
async def admin_fmed_edit_media_clear(message: Message, state: FSMContext):
    data = await state.get_data()
    med_id = data["edit_med_id"]
    page = data.get("edit_med_page", 0)
    cancel_callback = f"admin_fmed_{med_id}_{page}"
    value = message.text.strip().lower()
    if value in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await show_meditation_admin_detail(message, med_id, page)
        return
    if message.text.strip() != ".":
        await message.answer(
            "Отправьте аудио или видео либо точку «.».",
            reply_markup=_funnel_admin_cancel_keyboard(cancel_callback),
        )
        return
    async with async_session_maker() as session:
        meditation = await session.get(FunnelMeditation, med_id)
        if meditation:
            meditation.file_id = None
            meditation.file_type = None
            await session.commit()
    await state.clear()
    await message.answer("Медиа удалено.")
    await show_meditation_admin_detail(message, med_id, page)


def landing_status_condition(status_filter: str):
    if status_filter == "all":
        return None
    if status_filter == "failed":
        return FunnelLanding.status.not_in(["opened", "paid", "form_submitted"])
    return FunnelLanding.status == status_filter


def funnel_percent(part: int, total: int) -> str:
    if not total:
        return "0%"
    return f"{(part / total) * 100:.1f}%"


def _funnel_event_sort_key(event):
    return (
        getattr(event, "user_id", 0),
        getattr(event, "created_at", None) or datetime.min,
        getattr(event, "id", 0) or 0,
    )


def _funnel_prerequisites_met(journey, stage: str) -> bool:
    prerequisites = FUNNEL_ANALYTICS_PREREQUISITES.get(stage, ())
    for requirement in prerequisites:
        if isinstance(requirement, tuple):
            if not any(item in journey["stages"] or item in journey["support_events"] for item in requirement):
                return False
        elif requirement not in journey["stages"]:
            return False
    return True


def _new_funnel_journey(event) -> dict:
    return {
        "user_id": getattr(event, "user_id", None),
        "started_at": getattr(event, "created_at", None),
        "stages": {"starts": event},
        "support_events": set(),
        "events": {},
        "closed_for_reentry": False,
        "landing_journey_keys": set(),
        "landing_openings": [],
        "landing_form_events": {},
        "landing_paid_events": {},
    }


def _direct_choice_starts_new_journey(journey: dict) -> bool:
    return journey.get("closed_for_reentry") or any(
        stage in journey["stages"]
        for stage in (
            "selected",
            "reflections",
            "analyses",
            "bridge_shown",
            "offer_requested",
            "signup_clicked",
            "landing",
            "forms",
            "paid",
            "strategy",
            "objections",
        )
    )


def _funnel_landing_identity(event):
    raw_payload = getattr(event, "payload", None)
    if not raw_payload:
        return None
    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("landing_token") or payload.get("token")
    if token:
        return ("token", str(token))
    landing_id = payload.get("landing_id")
    if landing_id:
        return ("id", str(landing_id))
    return None


def _legacy_landing_identity(journey, event):
    meditation_id = getattr(event, "meditation_id", None)
    return (
        "journey",
        str(journey["user_id"]),
        str(meditation_id) if meditation_id is not None else "",
    )


def _landing_status_identity(journey, event, identity):
    if identity:
        return identity
    event_time = getattr(event, "created_at", None)
    meditation_id = getattr(event, "meditation_id", None)
    for opening_identity, opening_event in reversed(journey["landing_openings"]):
        if not opening_identity:
            continue
        if meditation_id is not None and getattr(opening_event, "meditation_id", None) != meditation_id:
            continue
        opening_time = getattr(opening_event, "created_at", None)
        if event_time is not None and opening_time is not None and opening_time > event_time:
            continue
        return opening_identity
    return _legacy_landing_identity(journey, event)


def _promote_legacy_landing_identity(journey, event, identity):
    if not identity or identity[0] == "journey":
        return
    meditation_id = getattr(event, "meditation_id", None)
    legacy_identity = _legacy_landing_identity(journey, event)
    legacy_keys = [legacy_identity]
    if meditation_id is not None:
        legacy_keys.append(("journey", str(journey["user_id"]), ""))
    for events_by_identity in (
        journey["landing_form_events"],
        journey["landing_paid_events"],
    ):
        for legacy_key in legacy_keys:
            legacy_event = events_by_identity.pop(legacy_key, None)
            if legacy_event is not None:
                events_by_identity.setdefault(identity, legacy_event)
                break


def _landing_journey_for_event(event, active_journey, journeys, landing_journeys):
    user_id = getattr(event, "user_id", None)
    identity = _funnel_landing_identity(event)
    if identity:
        journey = landing_journeys.get((user_id, identity))
        if journey:
            return journey, identity

    if active_journey and "landing" in active_journey["stages"]:
        return active_journey, identity

    meditation_id = getattr(event, "meditation_id", None)
    candidates = [
        journey
        for journey in reversed(journeys)
        if journey["user_id"] == user_id and "landing" in journey["stages"]
    ]
    if meditation_id is not None:
        for journey in candidates:
            landing_event = journey["stages"].get("landing")
            if getattr(landing_event, "meditation_id", None) == meditation_id:
                return journey, identity
    if candidates:
        return candidates[0], identity
    return None, identity


def _sync_funnel_paid_stage(journey):
    paid_events = journey["landing_paid_events"]
    if paid_events:
        journey["stages"]["paid"] = max(
            paid_events.values(),
            key=_funnel_event_sort_key,
        )
    else:
        journey["stages"].pop("paid", None)


def _sync_funnel_form_stage(journey):
    form_events = journey["landing_form_events"]
    if form_events:
        journey["stages"]["forms"] = max(
            form_events.values(),
            key=_funnel_event_sort_key,
        )
    else:
        journey["stages"].pop("forms", None)


def _apply_landing_status_event(journey, event, status, identity):
    identity = _landing_status_identity(journey, event, identity)
    _promote_legacy_landing_identity(journey, event, identity)
    journey["landing_journey_keys"].add(identity)
    if status == "paid":
        journey["landing_paid_events"][identity] = event
    elif status == "form_submitted":
        journey["landing_paid_events"].pop(identity, None)
        journey["landing_form_events"][identity] = event
    else:
        journey["landing_paid_events"].pop(identity, None)
        journey["landing_form_events"].pop(identity, None)
    _sync_funnel_form_stage(journey)
    _sync_funnel_paid_stage(journey)


def build_funnel_journeys(events, since: datetime | None = None) -> list[dict]:
    filtered_events = [
        event
        for event in events
        if getattr(event, "event_type", None) in FUNNEL_ANALYTICS_EVENT_TYPES
        and (
            since is None
            or getattr(event, "created_at", None) is None
            or getattr(event, "created_at") >= since
        )
    ]
    filtered_events.sort(key=_funnel_event_sort_key)

    journeys = []
    active_by_user = {}
    landing_journeys = {}
    for event in filtered_events:
        user_id = getattr(event, "user_id", None)
        event_type = event.event_type
        if event_type == "funnel_started":
            journey = _new_funnel_journey(event)
            journeys.append(journey)
            active_by_user[user_id] = journey
            continue

        journey = active_by_user.get(user_id)

        if event_type in FUNNEL_ANALYTICS_JOURNEY_BOUNDARY_EVENTS:
            if journey:
                journey["events"].setdefault(event_type, event)
                if event_type in FUNNEL_ANALYTICS_SUPPORT_EVENTS:
                    journey["support_events"].add(event_type)
                if event_type in FUNNEL_ANALYTICS_JOURNEY_BOUNDARY_EVENTS:
                    journey["closed_for_reentry"] = True
            continue

        if event_type == "choice_opened" and (
            journey is None or _direct_choice_starts_new_journey(journey)
        ):
            journey = _new_funnel_journey(event)
            journeys.append(journey)
            active_by_user[user_id] = journey

        status = FUNNEL_ANALYTICS_LANDING_STATUS_EVENTS.get(event_type)
        if status:
            landing_journey, identity = _landing_journey_for_event(
                event,
                journey,
                journeys,
                landing_journeys,
            )
            if not landing_journey:
                continue
            landing_journey["events"].setdefault(event_type, event)
            resolved_identity = _landing_status_identity(landing_journey, event, identity)
            if resolved_identity[0] != "journey":
                landing_journeys[(user_id, resolved_identity)] = landing_journey
            _apply_landing_status_event(landing_journey, event, status, resolved_identity)
            continue

        if not journey:
            continue
        journey["events"].setdefault(event_type, event)
        if event_type in FUNNEL_ANALYTICS_SUPPORT_EVENTS:
            journey["support_events"].add(event_type)

        stage = FUNNEL_ANALYTICS_STAGE_BY_EVENT.get(event_type)
        if not stage:
            continue
        if not _funnel_prerequisites_met(journey, stage):
            continue
        if stage == "landing":
            identity = _funnel_landing_identity(event)
            if identity:
                journey["landing_openings"].append((identity, event))
                journey["landing_journey_keys"].add(identity)
                landing_journeys[(user_id, identity)] = journey
        if stage in journey["stages"]:
            continue
        journey["stages"][stage] = event
    return journeys


def summarize_funnel_journeys(events, since: datetime | None = None) -> dict:
    journeys = build_funnel_journeys(events, since=since)
    metric_keys = [stage for stage, _ in FUNNEL_ANALYTICS_PRIMARY_STAGES]
    counts = {
        key: sum(key in journey["stages"] for journey in journeys)
        for key in metric_keys
    }
    counts.update({
        "strategy": sum("strategy" in journey["stages"] for journey in journeys),
        "followups": sum("followup_sent" in journey["events"] for journey in journeys),
        "resumes_menu": sum("resume_menu_opened" in journey["events"] for journey in journeys),
        "resumes_direct": sum("resume_direct" in journey["events"] for journey in journeys),
        "signup_eligible": sum(
            any(key in journey["stages"] for key in ("offer_requested", "strategy", "objections"))
            for journey in journeys
        ),
    })
    denominators = {
        "choices": counts["starts"],
        "selected": counts["starts"],
        "reflections": counts["selected"],
        "analyses": counts["reflections"],
        "bridge_shown": counts["analyses"],
        "offer_requested": counts["analyses"],
        "signup_clicked": counts["signup_eligible"],
        "landing": counts["signup_clicked"],
        "forms": counts["landing"],
        "paid": counts["landing"],
    }

    per_meditation: dict[int, dict[str, int]] = {}
    per_meditation_keys = (
        "started",
        "reflections",
        "analyses",
        "bridge_shown",
        "offer_requested",
        "signup_clicked",
        "landing",
        "forms",
        "paid",
        "strategy",
        "followups",
    )
    for journey in journeys:
        med_id = None
        for event in (*journey["stages"].values(), *journey["events"].values()):
            med_id = getattr(event, "meditation_id", None)
            if med_id is not None:
                break
        if med_id is None:
            continue
        metrics = per_meditation.setdefault(
            med_id,
            {key: 0 for key in per_meditation_keys},
        )
        for key in metric_keys:
            if key in journey["stages"]:
                output_key = "started" if key == "starts" else key
                if output_key in metrics:
                    metrics[output_key] += 1
        if "strategy" in journey["stages"]:
            metrics["strategy"] += 1
        if "followup_sent" in journey["events"]:
            metrics["followups"] += 1

    ranked_meditations = sorted(
        per_meditation.items(),
        key=lambda item: (item[1]["started"], item[1]["paid"], item[1]["landing"]),
        reverse=True,
    )
    return {
        **counts,
        "conversion_denominators": denominators,
        "per_meditation": ranked_meditations,
        "journeys": journeys,
    }


def _funnel_conversion_text(stats: dict, key: str) -> str:
    value = stats[key]
    denominator = stats["conversion_denominators"].get(key, 0)
    return f"{value}/{denominator} ({funnel_percent(value, denominator)})"


async def build_funnel_stats_payload(period_key: str):
    since = get_since_datetime(period_key)
    period_label = STATS_PERIODS.get(period_key, STATS_PERIODS["all"])[0]

    async with async_session_maker() as session:
        event_stmt = select(FunnelEvent).where(
            FunnelEvent.event_type.in_(FUNNEL_ANALYTICS_EVENT_TYPES)
        )
        if since:
            event_stmt = event_stmt.where(FunnelEvent.created_at >= since)
        event_stmt = event_stmt.order_by(
            FunnelEvent.user_id.asc(), FunnelEvent.created_at.asc(), FunnelEvent.id.asc()
        )
        events = (await session.execute(event_stmt)).scalars().all()
        stats = summarize_funnel_journeys(events)

        meditations = (
            await session.execute(
                select(FunnelMeditation).order_by(FunnelMeditation.sort_order.asc(), FunnelMeditation.id.asc())
            )
        ).scalars().all()
        med_titles = {m.id: m.title for m in meditations}

    starts = stats["starts"]
    choices = stats["choices"]
    selected = stats["selected"]
    reflections = stats["reflections"]
    analyses = stats["analyses"]
    strategy = stats["strategy"]
    bridge_shown = stats["bridge_shown"]
    offer_requested = stats["offer_requested"]
    signup_clicked = stats["signup_clicked"]
    landing = stats["landing"]
    forms = stats["forms"]
    paid = stats["paid"]
    followups = stats["followups"]
    resumes_menu = stats["resumes_menu"]
    resumes_direct = stats["resumes_direct"]

    lines = [
        "<b>Аналитика воронки</b>",
        f"Период: {period_label}",
        "Конверсия считается по отдельным прохождениям, начатым в выбранный период.",
        "",
        f"Стартовали воронку: {starts}",
        f"Открыли выбор практик: {choices}/{starts} ({funnel_percent(choices, starts)})",
        f"Запустили медитацию: {_funnel_conversion_text(stats, 'selected')}",
        f"Оставили инсайты: {_funnel_conversion_text(stats, 'reflections')}",
        f"Получили AI-разбор: {_funnel_conversion_text(stats, 'analyses')}",
        f"Показали мостик к предложению: {_funnel_conversion_text(stats, 'bridge_shown')}",
        f"Запросили предложение после разбора: {_funnel_conversion_text(stats, 'offer_requested')}",
        f"Перешли в чат стратсессии: {strategy}",
        f"Нажали «Записаться»: {_funnel_conversion_text(stats, 'signup_clicked')}",
        f"Открыли лендинг: {_funnel_conversion_text(stats, 'landing')}",
        f"Заполнили анкету: {_funnel_conversion_text(stats, 'forms')}",
        f"Оплатили: {_funnel_conversion_text(stats, 'paid')}",
        f"Получили напоминание: {followups}",
        f"Вернулись через resume-меню: {resumes_menu}",
        f"Продолжили напрямую: {resumes_direct}",
    ]

    ranked_meditations = stats["per_meditation"]
    if ranked_meditations:
        lines.extend(["", "<b>По медитациям</b>"])
        for med_id, metrics in ranked_meditations[:10]:
            title = med_titles.get(med_id, f"Медитация #{med_id}")
            lines.append(
                f"• {html.escape(title)}: старт {metrics['started']}, инсайты {metrics['reflections']}, "
                f"мостик {metrics['bridge_shown']}, предложение {metrics['offer_requested']}, "
                f"запись {metrics['signup_clicked']}, оплаты {metrics['paid']}"
            )

    return "\n".join(lines), {
        "period_label": period_label,
        **{key: stats[key] for key in (
            "starts",
            "choices",
            "selected",
            "reflections",
            "analyses",
            "strategy",
            "bridge_shown",
            "offer_requested",
            "signup_clicked",
            "landing",
            "forms",
            "paid",
            "followups",
            "resumes_menu",
            "resumes_direct",
            "signup_eligible",
        )},
        "conversion_denominators": stats["conversion_denominators"],
        "per_meditation": ranked_meditations,
    }


async def show_admin_funnel_stats(target, period_key: str):
    text, _ = await build_funnel_stats_payload(period_key)
    await send_content(target, text, None, funnel_stats_keyboard(period_key))


def render_funnel_stats_csv(stats: dict, period_key: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["period", stats["period_label"]])
    writer.writerow(["metric", "value", "denominator", "conversion"])
    metric_keys = (
        "starts",
        "choices",
        "selected",
        "reflections",
        "analyses",
        "bridge_shown",
        "offer_requested",
        "strategy",
        "signup_clicked",
        "landing",
        "forms",
        "paid",
        "followups",
        "resumes_menu",
        "resumes_direct",
    )
    for key in metric_keys:
        denominator = stats["conversion_denominators"].get(key, "")
        conversion = funnel_percent(stats[key], denominator) if denominator != "" else ""
        writer.writerow([key, stats[key], denominator, conversion])
    writer.writerow([])
    writer.writerow([
        "meditation_id",
        "started",
        "reflections",
        "analyses",
        "bridge_shown",
        "offer_requested",
        "signup_clicked",
        "landing",
        "forms",
        "paid",
        "strategy",
        "followups",
    ])
    for med_id, metrics in stats["per_meditation"]:
        writer.writerow([med_id, *[metrics[key] for key in (
            "started",
            "reflections",
            "analyses",
            "bridge_shown",
            "offer_requested",
            "signup_clicked",
            "landing",
            "forms",
            "paid",
            "strategy",
            "followups",
        )]])
    return buffer.getvalue()


async def export_funnel_stats_csv(target, period_key: str, bot=None):
    _, stats = await build_funnel_stats_payload(period_key)
    data = render_funnel_stats_csv(stats, period_key)

    current_bot = resolve_bot(target, bot)
    chat_id, _ = resolve_message_context(target)
    filename = f"funnel_stats_{period_key}.csv"
    await current_bot.send_document(chat_id, BufferedInputFile(data.encode("utf-8"), filename=filename))


async def show_admin_funnel_landings(target, status_filter: str = "all", page: int = 0):
    status_filter = status_filter if status_filter in LANDING_STATUS_FILTERS else "all"
    limit = 8
    async with async_session_maker() as session:
        count_stmt = select(func.count(FunnelLanding.id))
        items_stmt = select(FunnelLanding).order_by(FunnelLanding.id.desc())
        condition = landing_status_condition(status_filter)
        if condition is not None:
            count_stmt = count_stmt.where(condition)
            items_stmt = items_stmt.where(condition)

        total = (await session.scalar(count_stmt)) or 0

        total_pages = max(1, (total + limit - 1) // limit)
        page = min(max(page, 0), total_pages - 1)
        offset = page * limit
        items = (
            await session.execute(items_stmt.limit(limit).offset(offset))
        ).scalars().all()

    text = (
        f"<b>Лиды и оплаты</b>\n\n"
        f"Фильтр: {LANDING_STATUS_FILTERS[status_filter]}\n"
        f"Всего заявок: {total}\n"
        f"Страница: {page + 1}/{total_pages}"
    )
    await send_content(target, text, None, funnel_landing_list_keyboard(items, status_filter, page, total_pages))


async def show_admin_landing_detail(target, landing_id: int, status_filter: str, page: int):
    async with async_session_maker() as session:
        landing = await session.get(FunnelLanding, landing_id)
        if not landing:
            await send_content(target, "Заявка не найдена.", None, funnel_admin_menu_keyboard())
            return
        user = await session.get(User, landing.user_id)

    payload_preview = _payload_preview(landing.last_payload, limit=500)
    text = (
        f"<b>Заявка #{landing.id}</b>\n\n"
        f"Клиент: {user.first_name if user else 'Не найден'} ({landing.user_id})\n"
        f"Статус: {human_landing_status(landing.status)}\n"
        f"Практика: {landing.meditation_id or '-'}\n"
        f"Сумма: {landing.amount or '-'} {landing.currency or ''}\n"
        f"Внешний платёж: {landing.external_payment_id or '-'}\n"
        f"Открыт: {format_msk(landing.opened_at, '%d-%m-%Y %H:%M')}\n"
        f"Оплачен: {format_msk(landing.paid_at, '%d-%m-%Y %H:%M') if landing.paid_at else '-'}\n"
        f"Имя: {landing.customer_name or '-'}\n"
        f"Email: {landing.customer_email or '-'}\n"
        f"Телефон: {landing.customer_phone or '-'}\n"
        f"URL: {html.escape(landing.landing_url or '-')}\n\n"
        f"Данные заявки: <code>{html.escape(payload_preview)}</code>"
    )
    await send_content(target, text, None, funnel_landing_detail_keyboard(landing.id, landing.user_id, status_filter, page))


async def show_admin_funnel_tools(target):
    await send_content(
        target,
        "Сервисные действия для быстрого теста воронки и ручного обслуживания состояний.",
        None,
        funnel_tools_keyboard(),
    )


async def show_admin_user_funnel(
    target,
    user_id: int,
    return_callback: str = "admin_clients_list_0",
):
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        funnel_state = await get_or_create_funnel_state(session, user_id)

    text = (
        f"<b>Воронка клиента</b>\n\n"
        f"Клиент: {user.first_name if user else user_id} ({user_id})\n"
        f"Текущий этап: {human_funnel_stage(funnel_state.stage)}\n"
        f"Воронка активна: {'да' if funnel_state.is_active else 'нет'}\n"
        f"Текущая практика: {funnel_state.meditation_id or '-'}\n"
        f"Напоминаний отправлено: {funnel_state.reminder_count}\n"
        f"Последнее действие: {format_msk(get_state_anchor_time(funnel_state), '%d-%m-%Y %H:%M')}\n"
        f"Следующее напоминание: {format_msk(funnel_state.next_followup_at, '%d-%m-%Y %H:%M') if funnel_state.next_followup_at else 'не запланировано'}"
    )
    await send_content(target, text, None, admin_user_funnel_keyboard(user_id, return_callback))


@router.callback_query(F.data == "admin_funnel_tools")
async def admin_funnel_tools(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    await callback.answer()
    await show_admin_funnel_tools(callback)


@router.callback_query(F.data == "admin_ftool_start_self")
async def admin_ftool_start_self(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    await callback.answer()
    await start_funnel_intro(callback)


@router.callback_query(F.data == "admin_ftool_link")
async def admin_ftool_link(callback: CallbackQuery, bot: Bot, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    await callback.answer()
    bot_info = await bot.get_me()
    await callback.message.answer(
        f"Ссылка для запуска воронки:\nhttps://t.me/{bot_info.username}?start=funnel",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.callback_query(F.data == "admin_ftool_choice_self")
async def admin_ftool_choice_self(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    await callback.answer()
    await show_meditation_choices(callback)


@router.callback_query(F.data == "admin_ftool_followup_self")
async def admin_ftool_followup_self(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    user_id = callback.from_user.id
    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, user_id)
        if not state.is_active:
            await callback.answer("У вас нет активной воронки", show_alert=True)
            return
        try:
            sent = await send_funnel_followup_message(session, callback.bot, state)
            await session.commit()
        except Exception as exc:
            logger.exception("Failed to send self followup for admin user_id=%s: %s", user_id, exc)
            await callback.answer("Не удалось отправить напоминание", show_alert=True)
            return
        if not sent:
            await callback.answer("Напоминания после практики выключены или отправка запрещена для кризисного профиля", show_alert=True)
            return
    await callback.answer("Напоминание отправлено")
    await show_admin_funnel_tools(callback)


@router.callback_query(F.data.in_({"admin_ftool_reset_self", "admin_ftool_reset_self_confirm"}))
async def admin_ftool_reset_self(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    user_id = callback.from_user.id
    if callback.data != "admin_ftool_reset_self_confirm":
        await _ask_funnel_admin_confirmation(
            callback,
            "Состояние вашей тестовой воронки будет сброшено. Продолжить?",
            "admin_ftool_reset_self_confirm",
            "admin_funnel_tools",
        )
        return
    async with async_session_maker() as session:
        await deactivate_funnel(session, user_id, "admin_reset")
        await register_event(session, user_id, "admin_reset", "admin_reset")
        await session.commit()
    await callback.answer("Состояние сброшено")
    await show_admin_funnel_tools(callback)


@router.callback_query(F.data.regexp(r"^admin_user_funnel_\d+(?::clientctx_[0-9a-f]{16})?$"))
async def admin_user_funnel(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    return_callback = await _admin_client_context_return(callback, state)
    if return_callback is None:
        await callback.answer("Экран профиля устарел. Откройте список клиентов заново.", show_alert=True)
        return
    user_id = int(callback.data.split(":", 1)[0].split("_")[-1])
    await callback.answer()
    await show_admin_user_funnel(callback, user_id, return_callback)


@router.callback_query(F.data.startswith("admin_user_funnel_followup_"))
async def admin_user_funnel_followup(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    return_callback = await _admin_client_context_return(callback, state)
    if return_callback is None:
        await callback.answer("Экран профиля устарел. Откройте список клиентов заново.", show_alert=True)
        return
    user_id = int(callback.data.split(":", 1)[0].split("_")[-1])
    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, user_id)
        if not state.is_active:
            await callback.answer("У клиента нет активной воронки", show_alert=True)
            return
        try:
            sent = await send_funnel_followup_message(session, callback.bot, state)
            if not sent:
                await session.commit()
                await callback.answer("Напоминания после практики выключены или отправка запрещена для кризисного профиля", show_alert=True)
                return
            await register_event(session, user_id, state.stage, "admin_followup_forced", meditation_id=state.meditation_id)
            await session.commit()
        except Exception as exc:
            logger.exception("Failed to force followup for user_id=%s: %s", user_id, exc)
            await callback.answer("Не удалось отправить напоминание", show_alert=True)
            return
    await callback.answer("Напоминание отправлено")
    await show_admin_user_funnel(callback, user_id, return_callback)


@router.callback_query(F.data.startswith("admin_user_funnel_reset_"))
async def admin_user_funnel_reset(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    return_callback = await _admin_client_context_return(callback, state)
    if return_callback is None:
        await callback.answer("Экран профиля устарел. Откройте список клиентов заново.", show_alert=True)
        return
    payload = callback.data.removeprefix("admin_user_funnel_reset_")
    payload = payload.split(":", 1)[0]
    confirmed = payload.startswith("confirm_")
    user_id = int(payload.removeprefix("confirm_") if confirmed else payload)
    if not confirmed:
        context_suffix = f":{return_callback.rsplit(':', 1)[1]}" if ":clientctx_" in return_callback else ""
        await _ask_funnel_admin_confirmation(
            callback,
            "Состояние воронки этого клиента будет сброшено. Продолжить?",
            f"admin_user_funnel_reset_confirm_{user_id}{context_suffix}",
            f"admin_user_funnel_{user_id}{context_suffix}",
        )
        return
    async with async_session_maker() as session:
        await deactivate_funnel(session, user_id, "admin_reset")
        await register_event(session, user_id, "admin_reset", "admin_reset")
        await session.commit()
    await callback.answer("Состояние сброшено")
    await show_admin_user_funnel(callback, user_id, return_callback)


@router.callback_query(F.data == "admin_funnel_stats")
@router.callback_query(F.data.regexp(r"^admin_funnel_stats_(7|30|90|all)$"))
async def admin_funnel_stats(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    period_key = "all"
    if callback.data != "admin_funnel_stats":
        period_key = callback.data.replace("admin_funnel_stats_", "", 1)
    if period_key not in STATS_PERIODS:
        period_key = "all"
    await callback.answer()
    await show_admin_funnel_stats(callback, period_key)


@router.callback_query(F.data.startswith("admin_funnel_stats_export_"))
async def admin_funnel_stats_export(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    period_key = callback.data.replace("admin_funnel_stats_export_", "", 1)
    if period_key not in STATS_PERIODS:
        period_key = "all"
    await callback.answer()
    await export_funnel_stats_csv(callback, period_key)


@router.callback_query(F.data.startswith("admin_funnel_landings_"))
async def admin_funnel_landings(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    payload = callback.data.removeprefix("admin_funnel_landings_")
    status_filter, page_raw = payload.rsplit("_", 1)
    await callback.answer()
    await show_admin_funnel_landings(callback, status_filter, int(page_raw))


@router.callback_query(F.data.regexp(r"^admin_flanding_\d+_[a-z_]+_\d+$"))
async def admin_flanding_detail(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    payload = callback.data.removeprefix("admin_flanding_")
    try:
        landing_id_raw, status_and_page = payload.split("_", 1)
        status_filter, page_raw = status_and_page.rsplit("_", 1)
        landing_id = int(landing_id_raw)
        page = int(page_raw)
    except (TypeError, ValueError):
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    await callback.answer()
    await show_admin_landing_detail(callback, landing_id, status_filter, page)


@router.callback_query(F.data.startswith("admin_flanding_mark_"))
async def admin_flanding_mark(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    payload = callback.data.removeprefix("admin_flanding_mark_")
    context_match = re.fullmatch(
        r"(paid|form|opened|failed)_(\d+)_([a-z_]+)_(\d+)",
        payload,
    )
    if context_match:
        new_status, landing_id_raw, status_filter, page_raw = context_match.groups()
        try:
            landing_id = int(landing_id_raw)
            page = int(page_raw)
        except ValueError:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
    else:
        try:
            new_status, landing_id_raw = payload.rsplit("_", 1)
            landing_id = int(landing_id_raw)
        except ValueError:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        status_filter, page = "all", 0
    if new_status == "form":
        new_status = "form_submitted"
    async with async_session_maker() as session:
        landing = await session.get(FunnelLanding, int(landing_id))
        if not landing:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        landing.status = new_status
        if new_status == "paid" and not landing.paid_at:
            landing.paid_at = msk_now()
        elif new_status != "paid":
            landing.paid_at = None
        await register_event(
            session,
            landing.user_id,
            "landing_admin",
            f"landing_marked_{new_status}",
            meditation_id=landing.meditation_id,
            payload=json.dumps(
                {
                    "landing_id": landing.id,
                    "landing_token": landing.token,
                    "status": new_status,
                },
                ensure_ascii=False,
            ),
        )
        await session.commit()
    await callback.answer("Статус обновлён")
    await show_admin_landing_detail(callback, landing_id, status_filter, page)


@router.callback_query(F.data.startswith("admin_flanding_payload_"))
async def admin_flanding_payload(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    landing_id = int(callback.data.split("_")[-1])
    async with async_session_maker() as session:
        landing = await session.get(FunnelLanding, landing_id)
        if not landing:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
    payload_text = landing.last_payload or "Технические данные отсутствуют."
    file_data = payload_text.encode("utf-8")
    await callback.answer()
    await callback.message.answer_document(
        BufferedInputFile(file_data, filename=f"landing_payload_{landing_id}.txt"),
        caption=f"Технические данные заявки #{landing_id}",
    )


@router.callback_query(F.data.startswith("admin_flandings_export_"))
async def admin_flandings_export(callback: CallbackQuery, state: FSMContext | None = None):
    if not await is_admin_user(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    status_filter = callback.data.replace("admin_flandings_export_", "", 1)
    if status_filter not in LANDING_STATUS_FILTERS:
        status_filter = "all"

    async with async_session_maker() as session:
        stmt = select(FunnelLanding).order_by(FunnelLanding.id.desc())
        condition = landing_status_condition(status_filter)
        if condition is not None:
            stmt = stmt.where(condition)
        landings = (await session.execute(stmt)).scalars().all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "user_id", "meditation_id", "status", "amount", "currency", "opened_at", "paid_at", "email", "phone"])
    for landing in landings:
        writer.writerow(
            [
                landing.id,
                landing.user_id,
                landing.meditation_id,
                landing.status,
                landing.amount,
                landing.currency,
                format_msk(landing.opened_at, "%Y-%m-%d %H:%M:%S"),
                format_msk(landing.paid_at, "%Y-%m-%d %H:%M:%S") if landing.paid_at else "",
                landing.customer_email or "",
                landing.customer_phone or "",
            ]
        )

    await callback.answer()
    await callback.message.answer_document(
        BufferedInputFile(buffer.getvalue().encode("utf-8"), filename=f"funnel_landings_{status_filter}.csv"),
        caption=f"Экспорт заявок: {LANDING_STATUS_FILTERS[status_filter]}",
    )


async def process_funnel_followups(bot: Bot, single_iteration: bool = False):
    while True:
        try:
            now = msk_now()
            async with _funnel_followup_lock(), async_session_maker() as session:
                config = await get_funnel_config(session)
                # 1. Таймауты неактивности медитаций (Cases A & B)
                timeout_states = (
                    await session.execute(
                        select(FunnelState).where(
                            FunnelState.is_active == True,
                            FunnelState.stage.in_(["meditation_card", "listening"]),
                            FunnelState.stage_timeout_at != None,
                            FunnelState.stage_timeout_at <= now,
                            FunnelState.stage_timeout_sent == False,
                        )
                    )
                ).scalars().all()

                for st in timeout_states:
                    user = await session.get(User, st.user_id)
                    if user and user.crisis_detected_at:
                        st.stage_timeout_sent = True
                        st.stage_timeout_at = None
                        await cancel_nurture_campaign(session, st)
                        continue
                    if not config.meditation_timeouts_enabled:
                        st.stage_timeout_sent = True
                        st.stage_timeout_at = None
                        continue
                    if st.stage == "meditation_card":
                        content = await session.get(Content, "funnel_timeout_unstarted")
                        default_text = "Ты открыл(а) медитацию, но ещё не начал(а) практику. Готов(а) сделать первый шаг к спокойствию?"
                        text = content.text_content if content and content.text_content else default_text
                        kb_markup = meditation_card_keyboard(st.meditation_id, 0) if st.meditation_id else None
                    else:  # listening
                        content = await session.get(Content, "funnel_timeout_uncompleted")
                        default_text = "Ты начал(а) практику, но мы ещё не обсудили твои впечатления. Как всё прошло?"
                        text = content.text_content if content and content.text_content else default_text
                        kb_markup = meditation_listen_keyboard()

                    try:
                        if await _before_funnel_followup_send(st.user_id):
                            st.stage_timeout_sent = True
                            st.stage_timeout_at = None
                            continue
                        await bot.send_message(
                            st.user_id,
                            text,
                            reply_markup=kb_markup,
                            parse_mode="HTML",
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                        )
                        st.stage_timeout_sent = True
                        await register_event(
                            session,
                            st.user_id,
                            st.stage,
                            "stage_timeout_sent",
                            meditation_id=st.meditation_id,
                            payload=text[:1000],
                        )
                    except Exception as exc:
                        logger.warning("Failed to send stage timeout to user_id=%s: %s", st.user_id, exc)

                # 2. Nurture кампания и 7-дневный ремаркетинг (Cases C & D)
                followup_states = (
                    await session.execute(
                        select(FunnelState).where(
                            FunnelState.is_active == True,
                            FunnelState.next_followup_at != None,
                            FunnelState.next_followup_at <= now,
                        )
                    )
                ).scalars().all()

                for st in followup_states:
                    user = await session.get(User, st.user_id)
                    if user and user.crisis_detected_at:
                        await cancel_nurture_campaign(session, st)
                        st.is_active = False
                        continue
                    if st.stage == "remarketing_7d":
                        if not config.remarketing_enabled:
                            st.next_followup_at = None
                            st.followup_sequence_anchor = None
                            st.is_active = False
                            st.stage = "completed_unconverted"
                            continue
                        content = await session.get(Content, "funnel_reminder_7d_remarketing")
                        default_text = "Привет! Прошла неделя, как мы договаривались. Готов(а) вернуться к обсуждению стратегической сессии?"
                        text = content.text_content if content and content.text_content else default_text
                        try:
                            if await _before_funnel_followup_send(st.user_id):
                                st.next_followup_at = None
                                st.followup_sequence_anchor = None
                                st.is_active = False
                                st.stage = "completed_unconverted"
                                continue
                            await bot.send_message(
                                st.user_id,
                                text,
                                reply_markup=await build_no_strategy_keyboard(),
                                parse_mode="HTML",
                                link_preview_options=LinkPreviewOptions(is_disabled=True),
                            )
                            await register_event(
                                session,
                                st.user_id,
                                st.stage,
                                "remarketing_7d_sent",
                                meditation_id=st.meditation_id,
                                payload=text[:1000],
                            )
                        except Exception as exc:
                            logger.warning("Failed to send 7d remarketing to user_id=%s: %s", st.user_id, exc)
                        st.next_followup_at = None
                        st.is_active = False
                        st.stage = "completed_unconverted"
                        st.followup_sequence_anchor = None
                    else:
                        if not config.nurture_enabled:
                            st.next_followup_at = None
                            st.followup_sequence_anchor = None
                            continue
                        step_num = st.reminder_count + 1
                        if step_num > 5:
                            st.is_active = False
                            st.next_followup_at = None
                            st.stage = "completed_unconverted"
                            st.followup_sequence_anchor = None
                            continue

                        content_key = f"funnel_reminder_step_{step_num}"
                        content = await session.get(Content, content_key)
                        nurture_defaults = {
                            1: "Привет! Вчера ты завершил(а) практику осознанности. Готов(а) разобрать результаты глубже на стратегической сессии?",
                            2: "Привет! Виталий Наумов подготовил разбор методик, которые помогают кратно быстрее достигать внутренних изменений. Хочешь узнать больше?",
                            3: "Привет! На стратегической сессии мы пошагово разберем твой запрос и построим персональный план. Записаться?",
                            4: "Привет! Прошла неделя с момента твоей первой практики. Замечаешь ли уже изменения в состоянии?",
                            5: "Привет! Это финальное напоминание о возможности пройти персональную стратегическую сессию с Виталием Наумовым.",
                        }
                        text = content.text_content if content and content.text_content else nurture_defaults.get(step_num, "Напоминание о стратегической сессии.")
                        try:
                            if await _before_funnel_followup_send(st.user_id):
                                await cancel_nurture_campaign(session, st)
                                st.is_active = False
                                continue
                            await bot.send_message(
                                st.user_id,
                                text,
                                reply_markup=reminder_keyboard(),
                                parse_mode="HTML",
                                link_preview_options=LinkPreviewOptions(is_disabled=True),
                            )
                            await register_event(
                                session,
                                st.user_id,
                                st.stage,
                                "followup_sent",
                                meditation_id=st.meditation_id,
                                payload=text[:1000],
                            )
                            st.reminder_count += 1
                            await schedule_next_nurture_touch(session, st)
                        except TelegramRetryAfter as e:
                            await asyncio.sleep(e.retry_after)
                        except Exception as exc:
                            logger.warning("Failed to send nurture followup %s to user_id=%s: %s", step_num, st.user_id, exc)

                await session.commit()
        except Exception as exc:
            logger.exception("Funnel followup worker iteration failed: %s", exc)

        if single_iteration:
            break
        await asyncio.sleep(60)


install_admin_route_guards(router, globals())
