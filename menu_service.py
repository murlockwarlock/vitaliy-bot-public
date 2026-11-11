"""Runtime rendering for administrator-managed menu sections and buttons."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from sqlalchemy import select

from database import Book, Content, MenuButton, MenuSection, async_session_maker, sync_book_menu_buttons


_SECTION_TARGETS = {
    "menu_closed_club": "club",
    "menu_vitaly": "vitaly",
    "menu_books": "books",
    "menu_referral": "referral",
    "menu_thematic_sessions": "thematic",
}

_MENU_CALLBACK_ACTIONS = {
    "back_to_main_menu",
    "back_to_main_menu_text",
    "funnel_no_strategy_selected",
    "funnel_offer_requested",
    "funnel_open_landing",
    "funnel_show_faq",
    "funnel_show_sales",
    "funnel_start_objections",
    "funnel_to_main",
    "funnel_vitaly_method",
    "funnel_intro_start",
    "funnel_about_method",
    "funnel_resume_choice",
    "funnel_resume_sales",
    "funnel_resume_materials",
    "funnel_resume_strategy",
    "funnel_heard",
    "funnel_interest_yes",
    "funnel_interest_no",
    "j_start",
    "j_exit",
    "journal_open_stats",
    "j_time_menu",
    "j_video",
    "quick_notes",
    "j_h_p:0",
    "j_dyn_menu",
    "mode_journal",
    "habit_open_catalog",
    "habit_create_new",
    "habit_my_list",
    "habit_timezone",
    "back_to_main_menu_text",
    "sub_select_plan",
    "sub_toggle_renewal",
    "sub_enter_promo",
    "show_subscription_info",
    "pay_topup_balance",
    "main_menu_action",
    "menu_books",
    "menu_closed_club",
    "menu_funnel_start",
    "menu_referral",
    "menu_strategy_session",
    "menu_thematic_sessions",
    "menu_vitaly",
    "mode_diagnostics",
    "mode_express",
    "mode_free_session",
    "mode_habits",
    "mode_journal",
    "open_extended_menu",
    "open_payment_menu",
    "ref_balance",
    "ref_create_link",
    "ref_view_list_0",
    "ref_who_invited",
    "thematic_emotional_intelligence",
    "thematic_motivation_goals",
    "thematic_relationships",
    "thematic_self_esteem",
    "thematic_sleep_recovery",
    "thematic_stress",
}
_MENU_CALLBACK_PATTERNS = (
    re.compile(r"^funnel_(?:card|choice|listen)_\d+(?:_\d+)?$"),
    re.compile(r"^ref_view_list_\d+$"),
    re.compile(r"^funnel_(?:card|listen)_\{(?:meditation_id|page)\}(?:_\{page\})?$"),
    re.compile(r"^funnel_choice_\{page\}$"),
    re.compile(r"^j_(?:res|rst|h_v):\{(?:entry_id|completed_id)\}$"),
    re.compile(r"^j_skip:\{entry_id\}:\{position\}$"),
    re.compile(r"^ai_(?:gen_summary|continue_dialogue):\{(?:topic_slug|dialogue_id)\}(?::\{dialogue_id\})?$"),
    re.compile(r"^ai_new_dialogue:\{topic_slug\}$"),
    re.compile(r"^j_(?:res|rst|h_v):\d+$"),
    re.compile(r"^j_skip:\d+:\d+$"),
    re.compile(r"^ai_finish_dialogue:[\w-]+:\d+$"),
    re.compile(r"^ai_(?:gen_summary|continue_dialogue|regen_summary):[\w-]+:\d+$"),
    re.compile(r"^ai_new_dialogue:[\w-]+$"),
)


def validate_menu_url(value: str, *, web_app: bool = False) -> str:
    value = (value or "").strip()
    if not value or len(value) > 2048 or any(char.isspace() for char in value):
        raise ValueError("Ссылка должна быть непустой и содержать не более 2048 символов.")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https", "tg"} or not parsed.netloc:
        raise ValueError("Ссылка должна начинаться с https://, http:// или tg://.")
    if web_app and parsed.scheme != "https":
        raise ValueError("Для веб-приложения нужна ссылка https://.")
    if parsed.username or parsed.password:
        raise ValueError("Ссылка с логином или паролем запрещена.")
    return value


def _is_allowed_menu_callback(value: str) -> bool:
    return value in _MENU_CALLBACK_ACTIONS or any(pattern.fullmatch(value) for pattern in _MENU_CALLBACK_PATTERNS)


def classify_menu_action(value: str) -> tuple[str, str]:
    normalized = "main_menu_action" if (value or "").strip().casefold() in {"меню", "главное меню", "назад"} else (value or "").strip()
    if normalized.startswith(("http://", "https://", "tg://")):
        return "url", validate_menu_url(normalized)
    if normalized.startswith("webapp:"):
        return "web_app", validate_menu_url(normalized.removeprefix("webapp:").strip(), web_app=True)
    if len(normalized.encode("utf-8")) > 64 or not _is_allowed_menu_callback(normalized):
        raise ValueError("Неизвестное действие кнопки. Выбери действие из доступных разделов.")
    return "callback", normalized


def _resolve_menu_action(value: str | None, substitutions: dict[str, object] | None = None) -> str:
    resolved = value or ""
    for name, replacement in (substitutions or {}).items():
        resolved = resolved.replace(
            "{" + str(name) + "}",
            "" if replacement is None else str(replacement),
        )
    return resolved


async def is_menu_section_enabled(section_key: str) -> bool:
    async with async_session_maker() as session:
        section = await session.get(MenuSection, section_key)
    return section is None or section.enabled


async def load_menu_buttons(section_key: str) -> tuple[MenuSection | None, list[MenuButton]]:
    """Load current enabled buttons on every render.

    This deliberately avoids an application cache: an admin change is visible
    to the next Telegram update without a restart. Buttons targeting a disabled
    section are hidden while their rows remain stored for later re-enabling.
    """
    async with async_session_maker() as session:
        section = await session.get(MenuSection, section_key)
        if not section:
            return None, []
        if section_key == "books":
            await sync_book_menu_buttons(session)
        rows = (await session.scalars(select(MenuButton).where(
            MenuButton.section_key == section_key,
            MenuButton.enabled == True,
        ).order_by(MenuButton.order_num.asc(), MenuButton.id.asc()))).all()
        target_keys = {
            _SECTION_TARGETS[action]
            for action in (button.action_value or button.legacy_callback for button in rows)
            if action in _SECTION_TARGETS
        }
        target_sections = {}
        if target_keys:
            target_sections = {
                item.key: item
                for item in (await session.scalars(select(MenuSection).where(MenuSection.key.in_(target_keys)))).all()
            }

        visible: list[MenuButton] = []
        for button in rows:
            if button.source_type == "book" and not await session.get(Book, button.source_id):
                continue
            action = button.action_value or button.legacy_callback
            target_key = _SECTION_TARGETS.get(action)
            if target_key and target_sections.get(target_key) and not target_sections[target_key].enabled:
                continue
            if button.content_key and button.action_type in {"url", "web_app"}:
                content = await session.get(Content, button.content_key)
                if not content or not content.link_url:
                    continue
                if button.action_type == "web_app":
                    try:
                        validate_menu_url(content.link_url, web_app=True)
                    except ValueError:
                        continue
                if content.button_text and not button.label_override:
                    button.label = content.button_text
                button._resolved_action_value = content.link_url
            visible.append(button)
        return section, visible


def button_to_telegram(
    button: MenuButton,
    *,
    action_value: str | None = None,
    label: str | None = None,
) -> InlineKeyboardButton:
    if button.section_key == "thematic" and button.action_type == "callback":
        # The six public thematic callbacks are stable identifiers.  The
        # currently selected topic is resolved only when Telegram sends the
        # callback, so old messages keep following the current binding.
        action_value = action_value or button.legacy_callback or button.action_value
    else:
        action_value = action_value or getattr(button, "_resolved_action_value", None) or button.action_value
    if button.action_type == "url":
        return InlineKeyboardButton(text=label or button.label, url=action_value or "https://example.invalid")
    if button.action_type == "web_app":
        return InlineKeyboardButton(
            text=label or button.label,
            web_app=WebAppInfo(url=action_value or "https://example.invalid"),
        )
    return InlineKeyboardButton(
        text=label or button.label,
        callback_data=action_value or button.legacy_callback or "back_to_main_menu",
    )


async def configured_internal_menu_markup(
    section_key: str,
    fallback: InlineKeyboardMarkup,
    *,
    substitutions: dict[str, object] | None = None,
    allowed_actions: set[str] | None = None,
    dynamic_labels: dict[str, str] | None = None,
) -> InlineKeyboardMarkup:
    """Render an admin-managed keyboard for a non-root user screen."""
    if section_key == "funnel_crisis":
        return await _configured_crisis_menu_markup(fallback)
    section, buttons = await load_menu_buttons(section_key)
    if section is None:
        return fallback
    if not section.enabled:
        return InlineKeyboardMarkup(inline_keyboard=[])

    rows = []
    for button in buttons:
        resolved_action_value = getattr(button, "_resolved_action_value", None) or button.action_value or button.legacy_callback
        action_value = _resolve_menu_action(resolved_action_value, substitutions)
        if (
            allowed_actions is not None
            and action_value not in allowed_actions
            and resolved_action_value == button.legacy_callback
        ):
            continue
        label = None
        if dynamic_labels and not button.label_override:
            label = dynamic_labels.get(action_value)
        rows.append([button_to_telegram(button, action_value=action_value, label=label)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _configured_crisis_menu_markup(fallback: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    async with async_session_maker() as session:
        button = await session.scalar(
            select(MenuButton)
            .where(MenuButton.section_key == "funnel_crisis")
            .order_by(MenuButton.order_num.asc(), MenuButton.id.asc())
        )
        if not button:
            return fallback
        label = button.label or fallback.inline_keyboard[0][0].text
        if button.content_key and not button.label_override:
            content = await session.get(Content, button.content_key)
            if content and content.button_text:
                label = content.button_text
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="funnel_to_main")]]
    )


async def dynamic_menu_markup(section_key: str) -> InlineKeyboardMarkup | None:
    section, buttons = await load_menu_buttons(section_key)
    if section is None or not section.enabled:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[button_to_telegram(button)] for button in buttons])
