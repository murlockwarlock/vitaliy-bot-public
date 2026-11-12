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
    "не хочу больше жить",
    "не вижу смысла жить",
    "не вижу смысла продолжать жить",
    "не вижу смысла дальше жить",
    "не вижу смысла жить дальше",
    "не хочется жить",
    "мне незачем жить",
    "не вижу смысла продолжать сил больше нет",
    "лучше бы меня не было",
    "хочу умереть",
)

SCENARIO_4_CRISIS_MARKER = "не вижу смысла продолжать сил больше нет"
SCENARIO_4_WITH_LIFE_CRISIS_MARKER = "не вижу смысла продолжать жить сил больше нет"
_SCENARIO_4_PREFIX = ("не", "вижу", "смысла")
_SCENARIO_4_MIDDLES = (
    (("продолжать",), SCENARIO_4_CRISIS_MARKER),
    (("продолжать", "жить"), SCENARIO_4_WITH_LIFE_CRISIS_MARKER),
    (("продолжать", "жить", "дальше"), SCENARIO_4_WITH_LIFE_CRISIS_MARKER),
    (("дальше", "жить"), "не вижу смысла дальше жить сил больше нет"),
    (("жить", "дальше"), "не вижу смысла жить дальше сил больше нет"),
    (("больше", "жить"), "не вижу смысла больше жить сил больше нет"),
)
_SCENARIO_4_SUFFIXES = (
    ("сил", "больше", "нет"),
    ("больше", "нет", "сил"),
    ("сил", "не", "осталось"),
    ("сил", "больше", "не", "осталось"),
    ("у", "меня", "больше", "нет", "сил"),
)
_SCENARIO_4_QUOTE_ATTRIBUTIONS = (
    ("в", "книге", "герой", "сказал"),
    ("в", "романе", "герой", "сказал"),
    ("в", "фильме", "герой", "сказал"),
    ("в", "сериале", "герой", "сказал"),
    ("герой", "сказал"),
    ("героиня", "сказала"),
    ("персонаж", "сказал"),
    ("персонаж", "сказала"),
    ("автор", "написал"),
    ("автор", "написала"),
    ("цитирую",),
    ("цитата",),
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
    if start > 0 and marker_tokens[0] == "не" and tokens[start - 1] == "не":
        return True
    if start > 0 and tokens[start - 1] == "не" and marker_tokens[0] in _CRISIS_INTENT_WORDS:
        return True
    for prefix in _CRISIS_NEGATION_PREFIXES:
        if start >= len(prefix) and tuple(tokens[start - len(prefix):start]) == prefix:
            return True
    return False


def _scenario_4_starts(tokens: list[str]) -> list[tuple[int, str]]:
    matches = []
    for middle, marker in _SCENARIO_4_MIDDLES:
        for suffix in _SCENARIO_4_SUFFIXES:
            scenario_tokens = _SCENARIO_4_PREFIX + middle + suffix
            width = len(scenario_tokens)
            for start in range(len(tokens) - width + 1):
                if tuple(tokens[start:start + width]) == scenario_tokens:
                    matches.append((start, marker))
    return matches


def _contains_token_phrase(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    return any(
        tuple(tokens[start:start + width]) == phrase
        for start in range(len(tokens) - width + 1)
    )


def _scenario_4_quoted_starts(tokens: list[str], starts: list[tuple[int, str]]) -> set[int]:
    quoted_starts = set()
    for start, _ in starts:
        context = tokens[max(0, start - 10):start]
        if any(_contains_token_phrase(context, phrase) for phrase in _SCENARIO_4_QUOTE_ATTRIBUTIONS):
            quoted_starts.add(start)
    return quoted_starts


def _matches_scenario_4(tokens: list[str]) -> bool:
    """Match the acceptance phrase unless an explicit external attribution quotes it."""
    starts = _scenario_4_starts(tokens)
    quoted_starts = _scenario_4_quoted_starts(tokens, starts)
    return any(
        start not in quoted_starts and not _is_explicitly_negated(tokens, start, marker.split())
        for start, marker in starts
    )


def _is_explicitly_quoted(tokens: list[str], start: int) -> bool:
    context = tokens[max(0, start - 10):start]
    return any(_contains_token_phrase(context, phrase) for phrase in _SCENARIO_4_QUOTE_ATTRIBUTIONS)


def find_crisis_marker(text: str | None, markers: object = None) -> str | None:
    tokens = _normalize_crisis_text(text).split()
    if not tokens:
        return None
    scenario_4_starts = _scenario_4_starts(tokens)
    quoted_scenario_4_starts = _scenario_4_quoted_starts(tokens, scenario_4_starts)
    for start, marker in scenario_4_starts:
        if (
            start not in quoted_scenario_4_starts
            and not _is_explicitly_negated(tokens, start, marker.split())
        ):
            return marker
    configured = list(DEFAULT_CRISIS_MARKERS) if markers is None else parse_crisis_markers(markers)
    configured = sorted(enumerate(configured), key=lambda item: (-len(item[1].split()), item[0]))
    for _, marker in configured:
        marker_tokens = marker.split()
        width = len(marker_tokens)
        for start in range(len(tokens) - width + 1):
            if tokens[start:start + width] != marker_tokens:
                continue
            if _is_explicitly_quoted(tokens, start):
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
    from menu_service import configured_internal_menu_markup

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user or not user.crisis_detected_at:
            return False
        contacts_text = await get_crisis_contacts_text(session)

    try:
        await bot.send_message(
            chat_id if chat_id is not None else user_id,
            contacts_text,
            reply_markup=await configured_internal_menu_markup(
                "funnel_crisis",
                crisis_contacts_keyboard(),
            ),
        )
    except Exception as exc:
        logger.error("crisis_contacts_delivery_failed user_id=%s error=%s", user_id, exc)
        return False
    return True
