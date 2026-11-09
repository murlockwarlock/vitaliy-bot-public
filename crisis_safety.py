import json
import logging
import re
import unicodedata
from contextvars import ContextVar
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, update

from time_utils import msk_now

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("crisis_safety")

_crisis_turn_user_id: ContextVar[int | None] = ContextVar("crisis_turn_user_id", default=None)

DEFAULT_CRISIS_CONTACTS = (
    "Если тебе сейчас очень тяжело — пожалуйста, не оставайся с этим один. "
    "Позвони тем, кто может помочь прямо сейчас:\n\n"
    "📞 112 — экстренная помощь\n"
    "📞 8-800-100-49-94 — кризисная линия доверия для взрослых и детей\n"
    "📞 +7 495 989-50-50 — телефон психологической помощи МЧС России\n"
    "💬 @NaumovVitaliy — служба заботы, там ответит живой человек"
)

DEFAULT_CRISIS_MARKERS = (
    "хочу покончить с собой",
    "покончить с собой",
    "планирую суицид",
    "совершить суицид",
    "убить себя",
    "причинить себе вред",
    "сделать себе больно",
    "не хочу жить",
    "не вижу смысла жить",
    "лучше бы меня не было",
    "хочу умереть",
)

_CRISIS_CONTEXT_WORDS = {
    "больно",
    "вред",
    "думаю",
    "планирую",
    "помогите",
    "помощь",
    "попытка",
    "пытался",
    "пыталась",
    "собираюсь",
    "суицид",
    "убить",
    "умереть",
}
_CRISIS_INTENT_WORDS = {
    "буду",
    "думаю",
    "могу",
    "планирую",
    "собираюсь",
    "хочу",
}
_CRISIS_NEGATION_PREFIXES = (
    ("не", "хочу"),
    ("не", "могу"),
    ("не", "буду"),
    ("не", "думаю"),
    ("не", "собираюсь"),
    ("не", "планирую"),
    ("никогда", "не"),
)


def crisis_contacts_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Главное меню", callback_data="funnel_to_main")
    builder.adjust(1)
    return builder.as_markup()


def _normalize_crisis_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).lower().replace("ё", "е")
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def normalize_crisis_marker(value: str | None) -> str | None:
    normalized = _normalize_crisis_text(value)
    if not normalized or len(normalized) > 160:
        return None
    return normalized


def parse_crisis_markers(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return list(DEFAULT_CRISIS_MARKERS)
    if not isinstance(value, (list, tuple)):
        return list(DEFAULT_CRISIS_MARKERS)

    result = []
    seen = set()
    for item in value:
        marker = normalize_crisis_marker(item)
        if marker and marker not in seen:
            seen.add(marker)
            result.append(marker)
    return result


def serialize_crisis_markers(markers: object) -> str:
    return json.dumps(parse_crisis_markers(markers), ensure_ascii=False)


def get_configured_crisis_markers(config: object | None) -> list[str]:
    raw_markers = getattr(config, "crisis_markers_json", None)
    if raw_markers is None:
        return list(DEFAULT_CRISIS_MARKERS)
    return parse_crisis_markers(raw_markers)


def _is_explicitly_negated(tokens: list[str], start: int, marker_tokens: list[str]) -> bool:
    if start > 0 and tokens[start - 1] == "не" and marker_tokens[0] in _CRISIS_INTENT_WORDS:
        return True
    for prefix in _CRISIS_NEGATION_PREFIXES:
        if start >= len(prefix) and tuple(tokens[start - len(prefix):start]) == prefix:
            return True
    return False


def find_crisis_marker(text: str | None, markers: object = None) -> str | None:
    tokens = _normalize_crisis_text(text).split()
    if not tokens:
        return None
    configured = list(DEFAULT_CRISIS_MARKERS) if markers is None else parse_crisis_markers(markers)
    configured = sorted(enumerate(configured), key=lambda item: (-len(item[1].split()), item[0]))
    for _, marker in configured:
        marker_tokens = marker.split()
        width = len(marker_tokens)
        for start in range(len(tokens) - width + 1):
            if tokens[start:start + width] != marker_tokens:
                continue
            if _is_explicitly_negated(tokens, start, marker_tokens):
                continue
            if width == 1 and len(tokens) > 1:
                window = tokens[max(0, start - 5):start] + tokens[start + 1:start + 6]
                if not any(word in _CRISIS_CONTEXT_WORDS for word in window):
                    continue
            return marker
    return None


async def get_crisis_marker_settings(session: "AsyncSession"):
    from database import BotConfig

    config = await session.get(BotConfig, 1)
    if not config:
        config = BotConfig(id=1)
        session.add(config)
        await session.flush()
    enabled = getattr(config, "crisis_marker_protection_enabled", True) is not False
    return config, enabled, get_configured_crisis_markers(config)


async def check_crisis_markers(
    session: "AsyncSession",
    user_id: int,
    text: str | None,
) -> str | None:
    config, enabled, markers = await get_crisis_marker_settings(session)
    if not enabled:
        return None
    matched_marker = find_crisis_marker(text, markers)
    if not matched_marker:
        return None
    from database import User

    user = await session.get(User, user_id)
    if not user or user.crisis_detected_at:
        return None
    return matched_marker if await mark_crisis_detected(session, user_id, signal_existing=False) else None


async def mark_crisis_detected(
    session: "AsyncSession",
    user_id: int,
    *,
    signal_existing: bool = True,
) -> bool:
    from database import User

    user = await session.get(User, user_id)
    if not user:
        return False
    if user.crisis_detected_at:
        if signal_existing:
            _crisis_turn_user_id.set(user_id)
        return False
    detected_at = msk_now()
    result = await session.execute(
        update(User)
        .where(User.id == user_id, User.crisis_detected_at.is_(None))
        .values(crisis_detected_at=detected_at)
    )
    if not result.rowcount:
        if signal_existing:
            _crisis_turn_user_id.set(user_id)
        return False
    user.crisis_detected_at = detected_at
    _crisis_turn_user_id.set(user_id)
    logger.warning("crisis_flag_set user_id=%s", user_id)
    return True


def crisis_was_detected_in_turn(user_id: int) -> bool:
    detected_user_id = _crisis_turn_user_id.get()
    if detected_user_id != user_id:
        return False
    _crisis_turn_user_id.set(None)
    return True


async def is_crisis_active(session: "AsyncSession", user_id: int) -> bool:
    from database import User

    return bool(await session.scalar(select(User.crisis_detected_at).where(User.id == user_id)))


async def automatic_send_allowed(session: "AsyncSession", user_id: int, source: str) -> bool:
    if await is_crisis_active(session, user_id):
        logger.info(
            "automatic_message_skipped user_id=%s source=%s reason=crisis_flag",
            user_id,
            source,
        )
        return False
    return True


async def get_crisis_contacts_text(session: "AsyncSession | None" = None) -> str:
    from database import Content, async_session_maker

    if session is not None:
        content = await session.get(Content, "crisis_contacts")
        return content.text_content if content and content.text_content else DEFAULT_CRISIS_CONTACTS

    async with async_session_maker() as owned_session:
        return await get_crisis_contacts_text(owned_session)


async def send_crisis_contacts_if_active(bot, user_id: int, chat_id: int | None = None) -> bool:
    from database import User, async_session_maker

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user or not user.crisis_detected_at:
            return False
        contacts_text = await get_crisis_contacts_text(session)

    try:
        await bot.send_message(
            chat_id if chat_id is not None else user_id,
            contacts_text,
            reply_markup=crisis_contacts_keyboard(),
        )
    except Exception as exc:
        logger.error("crisis_contacts_delivery_failed user_id=%s error=%s", user_id, exc)
        return False
    return True
