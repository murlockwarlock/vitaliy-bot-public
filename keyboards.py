from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import and_, select, func
from sqlalchemy.orm import selectinload
from database import (
    async_session_maker,
    Content,
    PaymentConfig,
    PaymentProvider,
    SubscriptionPlan,
    SubscriptionConfig,
    MenuButton,
    User,
    UserTopicSession,
    Message as DBMessage,
    sync_book_menu_buttons,
)
from menu_service import (
    configured_internal_menu_markup,
    is_menu_section_enabled,
    load_menu_buttons,
    button_to_telegram,
)
from content_service import get_button as cs_get_button, get_content as cs_get_content
from admin_roles import AdminRole, permission_for_callback, role_has_permission


async def main_menu_keyboard(user_id: int | None = None):
    async with async_session_maker() as session:
        conf = await session.get(PaymentConfig, 1)
        show_program_access = bool(conf and conf.mode != 'free')
        user = await session.get(User, user_id) if user_id else None
        has_dialogue_history = False
        if user and user.current_topic_slug and user.current_dialogue_id:
            active_dialogue_id = await session.scalar(
                select(UserTopicSession.active_dialogue_id).where(
                    UserTopicSession.user_id == user_id,
                    UserTopicSession.topic_slug == user.current_topic_slug,
                )
            )
            active_dialogue_id = active_dialogue_id or user.current_dialogue_id
            has_dialogue_history = bool(await session.scalar(
                select(func.count(DBMessage.id)).where(
                    DBMessage.user_id == user_id,
                    DBMessage.topic_slug == user.current_topic_slug,
                    DBMessage.dialogue_id == active_dialogue_id,
                    DBMessage.role == "user",
                )
            ))

    builder = InlineKeyboardBuilder()
    section, configured = await load_menu_buttons("main")
    if section is not None:
        visible_count = 0
        if section.enabled:
            for button in configured:
                if button.action_value == "open_payment_menu" and not show_program_access:
                    continue
                builder.add(button_to_telegram(button))
                visible_count += 1
        if not section.enabled or visible_count == 0:
            if await is_menu_section_enabled("extended"):
                builder.button(text="🏠 Главное меню", callback_data="main_menu_action")
                builder.button(text="✨ Расширенное меню", callback_data="open_extended_menu")
            else:
                builder.button(text="🔄 Обновить меню", callback_data="main_menu_action")
    else:
        builder.button(text="🧘 Практика - гипносессия", callback_data="menu_funnel_start")
        builder.button(text="🎯 Стратегическая сессия", callback_data="menu_strategy_session")
        builder.button(text="🧠 AI Свободная сессия", callback_data="mode_free_session")
        builder.button(text="🩺 AI Диагностика + сессия", callback_data="mode_diagnostics")
        builder.button(text="⚡️ AI Экспресс диагностика", callback_data="mode_express")
        builder.button(text="🧠 Тематические сессии", callback_data="menu_thematic_sessions")
        builder.button(text="📔 Дневник саморефлексии", callback_data="mode_journal")
        builder.button(text="🧘 Формировать привычки", callback_data="mode_habits")
        if show_program_access:
            builder.button(text="💳 Подписка", callback_data="open_payment_menu")
        builder.button(text="✨ Расширенное меню", callback_data="open_extended_menu")

    if user and has_dialogue_history:
        builder.button(text="▶️ Продолжить диалог", callback_data="ai_resume_current")

    builder.adjust(1)
    return builder.as_markup()


def thematic_sessions_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⚡ Стресс и выгорание", callback_data="thematic_stress")
    builder.button(text="💎 Самооценка и синдром самозванца", callback_data="thematic_self_esteem")
    builder.button(text="👥 Отношения и границы", callback_data="thematic_relationships")
    builder.button(text="🌙 Сон и восстановление", callback_data="thematic_sleep_recovery")
    builder.button(text="🎯 Мотивация и цели", callback_data="thematic_motivation_goals")
    builder.button(text="❤️ Эмоциональный интеллект", callback_data="thematic_emotional_intelligence")
    builder.button(text="⬅️ В главное меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


async def configured_thematic_sessions_keyboard() -> InlineKeyboardMarkup:
    section, configured = await load_menu_buttons("thematic")
    if section is None:
        return thematic_sessions_keyboard()
    if not section.enabled:
        return InlineKeyboardMarkup(inline_keyboard=[])
    builder = InlineKeyboardBuilder()
    for button in configured:
        builder.add(button_to_telegram(button))
    builder.adjust(1)
    return builder.as_markup()


def ai_disclaimer_consent_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Принимаю", callback_data="cb_accept_disclaimer")
    builder.button(text="❌ Отмена", callback_data="cb_cancel_disclaimer")
    builder.adjust(1)
    return builder.as_markup()


DEFAULT_AI_FINISH_BUTTON_TEXT = "Завершить диалог"


def ai_response_keyboard(
    topic_slug: str,
    dialogue_id: int,
    finish_button_text: str | None = None,
    *args,
    finish_label: str | None = None,
    **kwargs,
) -> InlineKeyboardMarkup:
    label = finish_label or finish_button_text or DEFAULT_AI_FINISH_BUTTON_TEXT
    builder = InlineKeyboardBuilder()
    builder.button(
        text=label,
        callback_data=f"ai_finish_dialogue:{topic_slug}:{dialogue_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


async def ai_response_keyboard_for_topic(topic_slug: str, dialogue_id: int) -> InlineKeyboardMarkup:
    finish_button_text = await cs_get_button(
        "btn_ai_finish_dialogue",
        DEFAULT_AI_FINISH_BUTTON_TEXT,
    )
    return ai_response_keyboard(topic_slug, dialogue_id, finish_button_text)


async def configured_ai_response_keyboard(topic_slug: str, dialogue_id: int) -> InlineKeyboardMarkup:
    return await ai_response_keyboard_for_topic(topic_slug, dialogue_id)


def topic_finish_actions_keyboard(topic_slug: str, dialogue_id: int, summary_enabled: bool = False, has_summary: bool = None) -> InlineKeyboardMarkup:
    if has_summary is not None:
        summary_enabled = has_summary
    builder = InlineKeyboardBuilder()
    if summary_enabled:
        builder.button(text="📝 Выводы по сессии", callback_data=f"ai_gen_summary:{topic_slug}:{dialogue_id}")
    builder.button(text="▶️ Продолжить диалог", callback_data=f"ai_continue_dialogue:{topic_slug}:{dialogue_id}")
    builder.button(text="🔄 Новый разговор", callback_data=f"ai_new_dialogue:{topic_slug}")
    builder.button(text="🎯 Записаться на сессию", callback_data="funnel_open_landing")
    builder.button(text="🏠 Главное меню", callback_data="main_menu_action")
    builder.adjust(1)
    return builder.as_markup()


async def configured_topic_finish_actions_keyboard(
    topic_slug: str,
    dialogue_id: int,
    summary_enabled: bool = False,
    has_summary: bool = None,
) -> InlineKeyboardMarkup:
    fallback = topic_finish_actions_keyboard(topic_slug, dialogue_id, summary_enabled, has_summary)
    allowed_actions = {
        f"ai_continue_dialogue:{topic_slug}:{dialogue_id}",
        f"ai_new_dialogue:{topic_slug}",
        "funnel_open_landing",
        "main_menu_action",
    }
    if has_summary is not None:
        summary_enabled = has_summary
    if summary_enabled:
        allowed_actions.add(f"ai_gen_summary:{topic_slug}:{dialogue_id}")
    markup = await configured_internal_menu_markup(
        "ai_completed",
        fallback,
        substitutions={"topic_slug": topic_slug, "dialogue_id": dialogue_id},
        allowed_actions=allowed_actions,
    )
    continuation_callback = f"ai_continue_dialogue:{topic_slug}:{dialogue_id}"
    if not any(
        button.callback_data == continuation_callback
        for row in markup.inline_keyboard
        for button in row
    ):
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Продолжить диалог", callback_data=continuation_callback)],
                *markup.inline_keyboard,
            ]
        )
    return markup


def topic_summary_result_keyboard(topic_slug: str, dialogue_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Продолжить диалог", callback_data=f"ai_continue_dialogue:{topic_slug}:{dialogue_id}")
    builder.button(text="🔄 Сформировать заново", callback_data=f"ai_regen_summary:{topic_slug}:{dialogue_id}")
    builder.button(text="📔 Дневник саморефлексии", callback_data="mode_journal")
    builder.button(text="🎯 Записаться на сессию", callback_data="funnel_open_landing")
    builder.button(text="🏠 Главное меню", callback_data="main_menu_action")
    builder.adjust(1)
    return builder.as_markup()


async def configured_topic_summary_result_keyboard(topic_slug: str, dialogue_id: int) -> InlineKeyboardMarkup:
    fallback = topic_summary_result_keyboard(topic_slug, dialogue_id)
    allowed_actions = {
        f"ai_continue_dialogue:{topic_slug}:{dialogue_id}",
        f"ai_regen_summary:{topic_slug}:{dialogue_id}",
        "mode_journal",
        "funnel_open_landing",
        "main_menu_action",
    }
    return await configured_internal_menu_markup(
        "ai_completed",
        fallback,
        substitutions={"topic_slug": topic_slug, "dialogue_id": dialogue_id},
        allowed_actions=allowed_actions,
    )


def topic_start_dialogue_keyboard(topic_slug: str, dialogue_id: int, button_text: str = "Начать диалог") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=button_text, callback_data=f"ai_start_dialogue:{topic_slug}:{dialogue_id}")
    builder.button(text="⬅️ Назад", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def ai_exit_keyboard(
    finish_button_text: str | None = None,
    *,
    finish_label: str | None = None,
):
    builder = InlineKeyboardBuilder()
    builder.button(
        text=finish_label or finish_button_text or DEFAULT_AI_FINISH_BUTTON_TEXT,
        callback_data="exit_ai_mode",
    )
    return builder.as_markup()


async def ai_exit_keyboard_for_topic() -> InlineKeyboardMarkup:
    finish_button_text = await cs_get_button(
        "btn_ai_finish_dialogue",
        DEFAULT_AI_FINISH_BUTTON_TEXT,
    )
    return ai_exit_keyboard(finish_button_text)


async def extended_menu_keyboard():
    section, configured = await load_menu_buttons("extended")
    if section is not None:
        builder = InlineKeyboardBuilder()
        visible_count = 0
        if section.enabled:
            for button in configured:
                if button.action_value == "open_payment_menu":
                    async with async_session_maker() as session:
                        conf = await session.get(PaymentConfig, 1)
                    if not conf or conf.mode == "free":
                        continue
                builder.add(button_to_telegram(button))
                visible_count += 1
        if not section.enabled or visible_count == 0:
            if await is_menu_section_enabled("main"):
                builder.button(text="🏠 Главное меню", callback_data="back_to_main_menu")
                builder.button(text="🔄 Обновить меню", callback_data="open_extended_menu")
            else:
                builder.button(text="🔄 Обновить меню", callback_data="open_extended_menu")
        builder.adjust(1)
        return builder.as_markup()

    btn_academy = await cs_get_content("btn_academy")
    btn_rich = await cs_get_content("btn_rich_thinking")
    btn_transfo = await cs_get_content("btn_transfo")

    async with async_session_maker() as session:
        conf = await session.get(PaymentConfig, 1)
        show_wallet = conf.mode != 'free'

    builder = InlineKeyboardBuilder()

    if btn_academy and btn_academy.link_url:
        builder.button(text=btn_academy.button_text or "🎓 Академия Транскоучинг", web_app=WebAppInfo(url=btn_academy.link_url))

    if btn_rich and btn_rich.link_url:
        builder.button(text=btn_rich.button_text or "💎 Богатое мышление", url=btn_rich.link_url)

    if btn_transfo and btn_transfo.link_url:
        builder.button(text=btn_transfo.button_text or "🦋 Трансформации", url=btn_transfo.link_url)

    if show_wallet:
        builder.button(text="💳 Подписка", callback_data="open_payment_menu")

    builder.button(text="📚 Библиотека книг", callback_data="menu_books")
    builder.button(text="🔐 Закрытый клуб", callback_data="menu_closed_club")
    builder.button(text="🤝 Реферальная программа", callback_data="menu_referral")
    builder.button(text="👤 Виталий Наумов", callback_data="menu_vitaly")
    builder.button(text="⬅️ Назад", callback_data="back_to_main_menu_text")
    builder.adjust(1)
    return builder.as_markup()


async def closed_club_keyboard():
    section, configured = await load_menu_buttons("club")
    if section is not None:
        if not section.enabled:
            return InlineKeyboardMarkup(inline_keyboard=[])
        builder = InlineKeyboardBuilder()
        for button in configured:
            builder.add(button_to_telegram(button))
        builder.adjust(1)
        return builder.as_markup()

    keys = [
        ("btn_club_materials", "📚 Полезные материалы", None),
        ("btn_club_articles", "📰 Полезные статьи", None),
        ("btn_club_chat", "💬 Присоединиться к чату", None),
        ("btn_club_session", "📅 Записаться на сессию", None),
        ("btn_club_question", "❓ Задать вопрос", None),
        ("btn_club_masterclass", "🎭 Афиша мастер классов", None)
    ]

    builder = InlineKeyboardBuilder()
    for key, default_text, type_ in keys:
        content = await cs_get_content(key)
        text = content.button_text if content and content.button_text else default_text
        url = content.link_url if content and content.link_url else "https://google.com"

        builder.button(text=text, url=url)

    builder.button(text="⬅️ Назад", callback_data="open_extended_menu")
    builder.adjust(1)
    return builder.as_markup()


def referral_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Создать ссылку-приглашение", callback_data="ref_create_link")
    builder.button(text="👥 Посмотреть рефералов", callback_data="ref_view_list_0")
    builder.button(text="🔍 Узнать своего реферера", callback_data="ref_who_invited")
    builder.button(text="💰 Посмотреть баллы", callback_data="ref_balance")
    builder.button(text="⬅️ Назад", callback_data="open_extended_menu")
    builder.adjust(1)
    return builder.as_markup()


async def configured_referral_keyboard() -> InlineKeyboardMarkup:
    section, configured = await load_menu_buttons("referral")
    if section is None:
        return referral_keyboard()
    if not section.enabled:
        return InlineKeyboardMarkup(inline_keyboard=[])
    builder = InlineKeyboardBuilder()
    for button in configured:
        builder.add(button_to_telegram(button))
    builder.adjust(1)
    return builder.as_markup()


async def vitaly_keyboard():
    section, configured = await load_menu_buttons("vitaly")
    if section is not None:
        if not section.enabled:
            return InlineKeyboardMarkup(inline_keyboard=[])
        builder = InlineKeyboardBuilder()
        for button in configured:
            builder.add(button_to_telegram(button))
        builder.adjust(1)
        return builder.as_markup()

    c1 = await cs_get_content("btn_vitaly_consult")
    c2 = await cs_get_content("btn_vitaly_speaker")

    url1 = c1.link_url if c1 and c1.link_url else "https://google.com"
    url2 = c2.link_url if c2 and c2.link_url else "https://google.com"

    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Записаться на консультацию", url=url1)
    builder.button(text="🎤 Спикерство / Пригласить", url=url2)
    builder.button(text="⬅️ Назад", callback_data="open_extended_menu")
    builder.adjust(1)
    return builder.as_markup()


def _admin_section_keyboard(
    buttons: list[tuple[str, str]],
    role: AdminRole | str | None,
    back_callback: str | None = "admin_panel",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, callback_data in buttons:
        permission = permission_for_callback(callback_data)
        if role is None or permission is None or role_has_permission(role, permission):
            builder.row(InlineKeyboardButton(text=text, callback_data=callback_data))
    if back_callback:
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback))
    return builder.as_markup()


def admin_panel_keyboard(role: AdminRole | str | None = None):
    return _admin_section_keyboard(
        [
            ("👥 Клиенты", "admin_clients_list_0"),
            ("🤖 ИИ и знания", "admin_section_ai"),
            ("✏️ Контент и программы", "admin_section_content"),
            ("⚙️ Автоматизации", "admin_section_automation"),
            ("💳 Подписки и оплата", "admin_payment_settings"),
            ("📋 Логи", "admin_section_logs"),
            ("👮 Администраторы", "admin_manage_admins"),
        ],
        role,
        back_callback=None,
    )


def admin_clients_section_keyboard(role: AdminRole | str | None = None):
    return _admin_section_keyboard(
        [("👥 Все клиенты", "admin_clients_list_0")], role
    )


def admin_ai_section_keyboard(role: AdminRole | str | None = None):
    return _admin_section_keyboard(
        [
            ("⚙️ Настройки ИИ", "admin_ai_settings"),
            ("🤖 Провайдеры и модели", "admin_ai_keys"),
            ("💬 Темы диалогов", "admin_topics_list"),
            ("📚 База знаний", "admin_kb_page_0"),
        ],
        role,
    )


def admin_content_section_keyboard(role: AdminRole | str | None = None):
    return _admin_section_keyboard(
        [
            ("✏️ Контент и разделы", "admin_content_list"),
            ("🌀 Воронка гипносессий", "admin_funnel_settings"),
            ("🧘 Привычки", "admin_habit_settings"),
            ("📔 Дневник", "admin_journal_menu"),
            ("🧪 Универсальные тесты", "admin_test_system"),
            ("📚 Наборы тестов", "admin_test_sets_list"),
        ],
        role,
    )


def admin_automation_section_keyboard(role: AdminRole | str | None = None):
    return _admin_section_keyboard(
        [
            ("⚙️ Основные автоматизации", "automation_menu"),
            ("🔔 Напоминания неактивным", "admin_reminders_settings"),
            ("⏱ Напоминания по цепочке", "admin_followup_list"),
            ("✉️ Рассылки", "admin_mailing"),
        ],
        role,
    )


def admin_payments_section_keyboard(role: AdminRole | str | None = None):
    return admin_payment_keyboard(role)


def admin_logs_section_keyboard(role: AdminRole | str | None = None):
    return _admin_section_keyboard(
        [
            ("📋 Общие логи", "admin_logs"),
            ("🤖 Логи ИИ", "admin_ai_logs_0_all_logs"),
        ],
        role,
    )


def admin_admins_section_keyboard(role: AdminRole | str | None = None):
    return _admin_section_keyboard(
        [("👮 Управление администраторами", "admin_manage_admins")], role
    )


def back_to_admin():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]])


def back_to_extended():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="open_extended_menu")]])


def ai_settings_keyboard(current_provider: str):
    builder = InlineKeyboardBuilder()
    providers = {
        "DeepSeek": "ai_provider_DeepSeek",
        "KIE": "ai_provider_KIE",
        "OpenAI": "ai_provider_OpenAI",
        "OpenRouter": "ai_provider_OpenRouter",
    }
    for name, callback_data in providers.items():
        text = f"✅ {name}" if name == current_provider else name
        builder.button(text=text, callback_data=callback_data)
    builder.button(text="⚙️ Провайдеры и модели", callback_data="admin_ai_keys")
    builder.button(text="📜 Логи запросов ИИ", callback_data="admin_ai_logs_0_all")
    builder.button(text="⬅️ Назад", callback_data="admin_section_ai")
    builder.adjust(1)
    return builder.as_markup()


def openrouter_models_keyboard(current_model: str | None = None):
    from openrouter_models import OPENROUTER_MODELS, OPENROUTER_MODEL_SPECS, OPENROUTER_DEFAULT_MODEL
    builder = InlineKeyboardBuilder()
    eff_model = (current_model or "").strip() or OPENROUTER_DEFAULT_MODEL
    for i, model_id in enumerate(OPENROUTER_MODELS):
        spec = OPENROUTER_MODEL_SPECS[model_id]
        is_selected = (model_id == eff_model)
        text = f"✅ {spec.friendly_name}" if is_selected else spec.friendly_name
        builder.button(text=text, callback_data=f"or_mod_{i}")
    builder.button(text="⬅️ Назад", callback_data="admin_ai_keys")
    builder.adjust(1)
    return builder.as_markup()


def universal_test_answer_keyboard(options, horizontal=False, question_index=0):
    builder = InlineKeyboardBuilder()
    for option_index, option in enumerate(options):
        builder.button(
            text=str(option.text),
            callback_data=f"test_opt_{question_index}_{option_index}",
        )
    if horizontal:
        builder.adjust(min(max(len(options), 1), 8))
    else:
        builder.adjust(1)
    return builder.as_markup()


def universal_test_question_keyboard(options, horizontal=False, question_index=0):
    """Question controls shared by diagnostics and other TestSets."""
    builder = InlineKeyboardBuilder()
    for option_index, option in enumerate(options):
        builder.button(
            text=str(option.text),
            callback_data=f"test_opt_{question_index}_{option_index}",
        )
    builder.button(text="❌ Выйти", callback_data="test_exit")
    builder.adjust(min(max(len(options), 1), 8), 1) if horizontal and options else builder.adjust(1)
    return builder.as_markup()


def universal_test_setup_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🏠 В главное меню", callback_data="main_menu_action")
    builder.adjust(1)
    return builder.as_markup()


def admin_test_system_keyboard(config, has_formulas, question_count):
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{'🟢' if config.is_enabled else '⚪️'} Тестирование",
        callback_data="admin_test_toggle_enabled",
    )
    builder.button(
        text=f"{'🟢' if config.show_progress else '⚪️'} Прогресс",
        callback_data="admin_test_toggle_progress",
    )
    builder.button(text="📥 Загрузить вопросы", callback_data="admin_test_upload_questions")
    builder.button(text="📐 Загрузить формулы", callback_data="admin_test_upload_formulas")
    builder.button(
        text=f"{'🟢' if config.formulas_enabled else '⚪️'} Формулы",
        callback_data="admin_test_toggle_formulas",
    )
    builder.button(text="🔄 Что передавать", callback_data="admin_test_cycle_mode")
    builder.button(text="☑️ Выбранные переменные", callback_data="admin_test_set_selected")
    builder.button(text="📝 Задать промпт результата", callback_data="admin_test_set_prompt")
    builder.button(
        text=f"{'🟢' if config.separate_result_prompt_enabled else '⚪️'} Отдельный промпт",
        callback_data="admin_test_toggle_separate",
    )
    builder.button(text="📄 Скачать шаблон", callback_data="admin_test_template")
    if question_count:
        builder.button(text="▶️ Пройти как администратор", callback_data="admin_test_preview")
    builder.button(text="📚 Все наборы тестов", callback_data="admin_test_sets_list")
    builder.button(text="⬅️ В контент и программы", callback_data="admin_section_content")
    builder.adjust(2, 1, 1, 2, 1, 1, 1, 1, 1, 1)
    return builder.as_markup()


def admin_test_cancel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Отмена", callback_data="admin_test_cancel")
    return builder.as_markup()


def ai_keys_models_keyboard(
    context_first: int,
    context_recent: int,
    openai_model: str,
    deepseek_model: str,
    kie_model: str,
    vision_model: str,
    image_model: str,
    image_edit_model: str,
    transcription_model: str,
    openrouter_model: str = "openai/gpt-5.6-terra",
):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔑 Ключ OpenAI", callback_data="set_ai_apikey")
    builder.button(text="🔑 Ключ DeepSeek", callback_data="set_ds_apikey")
    builder.button(text="🔑 Ключ KIE", callback_data="set_kie_apikey")
    builder.button(text="🔑 Ключ OpenRouter", callback_data="set_openrouter_apikey")
    builder.button(text=f"🧠 Модель OpenAI: {openai_model}", callback_data="set_model_openai")
    builder.button(text=f"🧠 Модель DeepSeek: {deepseek_model}", callback_data="set_model_deepseek")
    builder.button(text=f"🧠 Модель KIE: {kie_model}", callback_data="set_model_kie")
    builder.button(text=f"🧠 Модель OpenRouter: {openrouter_model}", callback_data="set_model_openrouter")
    builder.button(text=f"👁️ Vision: {vision_model}", callback_data="set_kie_vision_model")
    builder.button(text=f"🖼 Генерация: {image_model}", callback_data="set_kie_image_model")
    builder.button(text=f"🎨 Редактирование: {image_edit_model}", callback_data="set_kie_image_edit_model")
    builder.button(text=f"🗣️ Аудио: {transcription_model}", callback_data="set_kie_transcription_model")
    builder.button(text="💳 Проверить кредиты KIE", callback_data="check_kie_credits")
    builder.button(text=f"📌 Первые: {context_first}", callback_data="set_mem_first")
    builder.button(text=f"🔄 Последние: {context_recent}", callback_data="set_mem_recent")
    builder.button(text="⬅️ Назад", callback_data="admin_ai_settings")
    builder.adjust(2, 2, 2, 2, 1, 1, 1, 1, 2, 1)
    return builder.as_markup()


async def books_keyboard(page=0):
    from database import Book

    page_size = 10
    async with async_session_maker() as session:
        await sync_book_menu_buttons(session)
        visible_join = and_(
            MenuButton.section_key == "books",
            MenuButton.source_type == "book",
            MenuButton.source_id == Book.id,
            MenuButton.enabled == True,
        )
        visible_total = await session.scalar(
            select(func.count(Book.id)).join(MenuButton, visible_join)
        ) or 0
        total_pages = max(1, (visible_total + page_size - 1) // page_size)
        page = max(0, min(int(page), total_pages - 1))
        book_rows = (await session.execute(
            select(Book, MenuButton)
            .join(MenuButton, visible_join)
            .order_by(MenuButton.order_num.asc(), MenuButton.id.asc())
            .offset(page * page_size)
            .limit(page_size)
        )).all()

    builder = InlineKeyboardBuilder()

    for book, menu_button in book_rows:
        builder.row(InlineKeyboardButton(text=menu_button.label, url=book.url))

    builder.row(*standard_pagination_row(page, total_pages, lambda target: f"menu_books_pg_{target}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="open_extended_menu"))

    return builder.as_markup()


def payment_main_keyboard(mode, balance, sub_end):
    builder = InlineKeyboardBuilder()

    if mode == 'token':
        builder.button(text="💰 Пополнить баланс", callback_data="pay_topup_balance")
    elif mode == 'subscription':
        text = "🔄 Продлить доступ к программе" if sub_end else "💳 Оформить доступ к программе"
        builder.button(text=text, callback_data="show_subscription_info")

    builder.button(text="⬅️ Назад", callback_data="open_extended_menu")
    builder.adjust(1)
    return builder.as_markup()


async def configured_payment_main_keyboard(mode, balance, sub_end):
    allowed_actions = {"open_extended_menu"}
    if mode == "token":
        allowed_actions.add("pay_topup_balance")
    elif mode == "subscription":
        allowed_actions.add("show_subscription_info")
    return await configured_internal_menu_markup(
        "subscription_main",
        payment_main_keyboard(mode, balance, sub_end),
        allowed_actions=allowed_actions,
    )


async def payment_providers_keyboard(is_subscription=False, plan_id=None):
    async with async_session_maker() as session:
        providers = await session.execute(select(PaymentProvider).where(PaymentProvider.is_active == True))
        providers = providers.scalars().all()

    builder = InlineKeyboardBuilder()
    for p in providers:
        # Если подписка, передаем ID плана, иначе action=token
        if is_subscription and plan_id:
            callback = f"pay_prov_sub_{p.id}_{plan_id}"
        else:
            callback = f"pay_prov_{p.id}"

        builder.button(text=f"💳 {p.name}", callback_data=callback)

    builder.button(text="⬅️ Назад", callback_data="open_payment_menu")
    builder.adjust(1)
    return builder.as_markup()


async def user_plans_keyboard(user_promos=None, current_discount=0, is_admin=False):
    """Клавиатура с тарифами для клиента."""
    async with async_session_maker() as session:
        query = select(SubscriptionPlan).where(SubscriptionPlan.is_active == True).order_by(SubscriptionPlan.price)
        plans = (await session.execute(query)).scalars().all()

    builder = InlineKeyboardBuilder()
    for p in plans:
        if p.admin_only and not is_admin:
            continue
        duration_text = f"{p.duration_value} дн." if p.duration_unit == 'days' else f"{p.duration_value} мес."
        price = p.price
        if current_discount > 0:
            price = price * (1 - current_discount / 100)
        price_text = f"{price:.0f}" if price == int(price) else f"{price:.2f}"
        trial_mark = " (пробный)" if p.is_trial else ""
        builder.button(
            text=f"{p.name} — {price_text}₽ / {duration_text}{trial_mark}",
            callback_data=f"select_plan_{p.id}"
        )

    builder.button(text="⬅️ Назад", callback_data="show_subscription_info")
    builder.adjust(1)
    return builder.as_markup()


async def admin_plans_keyboard(page=0):
    """Клавиатура со списком тарифов для админа."""
    page_size = 10
    async with async_session_maker() as session:
        total = await session.scalar(select(func.count(SubscriptionPlan.id))) or 0
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        plans = (
            await session.execute(
                select(SubscriptionPlan)
                .options(selectinload(SubscriptionPlan.upgrades_to_plan))
                .order_by(SubscriptionPlan.price)
                .limit(page_size)
                .offset(page * page_size)
            )
        ).scalars().all()

    builder = InlineKeyboardBuilder()
    for p in plans:
        status = "🔒" if getattr(p, "admin_only", False) else ("🟢" if p.is_active else "⚪️")
        duration_unit_text = "дн." if p.duration_unit == 'days' else "мес."
        plan_text = f"{p.name} ({p.duration_value} {duration_unit_text}) ({p.price:.0f} руб)"
        if p.is_trial and p.upgrades_to_plan:
            plan_text += f" ➡️ «{p.upgrades_to_plan.name}»"
        builder.row(InlineKeyboardButton(
            text=f"{status} {plan_text}",
            callback_data=f"adm_edit_plan_{p.id}",
        ))

    builder.row(*standard_pagination_row(page, total_pages, lambda target: f"admin_manage_plans_{target}"))
    builder.row(InlineKeyboardButton(text="➕ Создать новый тариф", callback_data="adm_add_plan_start"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_payment_settings"))
    return builder.as_markup()


def admin_payment_keyboard(role: AdminRole | str | None = None):
    builder = InlineKeyboardBuilder()
    actions = (
        ("📈 Тарифные планы", "admin_manage_plans"),
        ("👥 Подписчики", "admin_subs_list_0_all"),
        ("🎁 Промокоды", "admin_promocodes"),
        ("📊 Статистика и Доход", "admin_payment_stats:admin_payment_settings"),
        ("📋 История платежей", "admin_plog_0_all"),
        ("🤝 Реферальная программа", "admin_referral_settings"),
        ("⚙️ Настройки оплаты", "admin_prodamus_settings"),
        ("⬅️ Назад", "admin_panel"),
    )
    for text, callback_data in actions:
        permission = permission_for_callback(callback_data)
        if role is None or permission is None or role_has_permission(role, permission):
            builder.row(InlineKeyboardButton(text=text, callback_data=callback_data))
    return builder.as_markup()


def admin_pay_mode_keyboard(current, return_callback="admin_payment_settings"):
    builder = InlineKeyboardBuilder()
    modes = {"free": "Бесплатный", "subscription": "Подписка", "token": "Токены"}

    for k, v in modes.items():
        text = f"✅ {v}" if k == current else v
        suffix = "" if return_callback == "admin_payment_settings" else f"__{return_callback}"
        builder.button(text=text, callback_data=f"confirm_set_pay_mode_{k}{suffix}")

    builder.button(text="⬅️ Назад", callback_data=return_callback)
    builder.adjust(1)
    return builder.as_markup()


def payment_alert_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Продлить доступ к программе", callback_data="open_payment_menu")
    builder.adjust(1)
    return builder.as_markup()


def token_alert_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Пополнить баланс", callback_data="pay_topup_balance")
    builder.adjust(1)
    return builder.as_markup()


async def admin_habit_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Режимы работы", callback_data="admin_habit_modes")
    builder.button(text="📚 Управление шаблонами", callback_data="admin_habit_templates")
    builder.button(text="🏆 Настройки Геймификации", callback_data="admin_habit_game")
    builder.button(text="⬅️ Назад", callback_data="admin_section_content")
    builder.adjust(1)
    return builder.as_markup()


def habits_entry_keyboard(mode):
    builder = InlineKeyboardBuilder()
    if mode == "catalog":
        builder.button(text="📂 Выбрать из каталога", callback_data="habit_open_catalog")
    elif mode == "custom":
        builder.button(text="➕ Создать привычку", callback_data="habit_create_new")
    elif mode == "hybrid":
        builder.button(text="📂 Выбрать из каталога", callback_data="habit_open_catalog")
        builder.button(text="➕ Создать свою", callback_data="habit_create_new")
    builder.button(text="📋 Мои привычки", callback_data="habit_my_list")
    builder.button(text="🌍 Часовой пояс", callback_data="habit_timezone")
    builder.button(text="📔 Дневник саморефлексии", callback_data="mode_journal")
    builder.button(text="⬅️ В главное меню", callback_data="back_to_main_menu_text")
    builder.adjust(1)
    return builder.as_markup()


async def configured_habits_entry_keyboard(mode):
    allowed_actions = {
        "habit_my_list",
        "habit_timezone",
        "mode_journal",
        "back_to_main_menu_text",
    }
    if mode in {"catalog", "hybrid"}:
        allowed_actions.add("habit_open_catalog")
    if mode in {"custom", "hybrid"}:
        allowed_actions.add("habit_create_new")
    return await configured_internal_menu_markup(
        "habits_entry",
        habits_entry_keyboard(mode),
        allowed_actions=allowed_actions,
        dynamic_labels={
            "habit_create_new": "➕ Создать свою" if mode == "hybrid" else "➕ Создать привычку",
        },
    )


AUDIENCE_LABELS = {
    "all": "👥 Все пользователи",
    "me": "👤 Только мне (тест)",
    "active": "🟢 Активные (писали за 7 дней)",
    "inactive": "🔴 Неактивные (молчат 7+ дней)",
    "has_sub": "💳 С активной подпиской",
    "no_sub": "🚫 Без подписки",
    "no_funnel": "🌀 Не прошли воронку",
    "paid_funnel": "💰 Оплатили с лендинга",
}


def mailing_preview_keyboard(audience="all"):
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 ОТПРАВИТЬ РАССЫЛКУ", callback_data="mail_send_final")
    builder.button(text="✏️ Изменить текст", callback_data="mail_edit_text")
    builder.button(text="🖼 Изменить медиа", callback_data="mail_edit_media")
    builder.button(text="🔘 Настроить кнопку", callback_data="mail_setup_btn")
    label = AUDIENCE_LABELS.get(audience, audience)
    builder.button(text=f"🎯 Кому: {label}", callback_data="mail_set_audience")
    builder.button(text="❌ Отмена", callback_data="admin_section_automation")
    builder.adjust(1, 2, 1, 1, 1)
    return builder.as_markup()


def mailing_audience_keyboard():
    builder = InlineKeyboardBuilder()
    for key, label in AUDIENCE_LABELS.items():
        builder.button(text=label, callback_data=f"mail_audience_{key}")
    builder.button(text="⬅️ Назад", callback_data="mail_preview_back")
    builder.adjust(1)
    return builder.as_markup()

def mailing_buttons_type_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оформить доступ к программе", callback_data="mail_btn_preset_sub")
    builder.button(text="💰 Пополнить баланс", callback_data="mail_btn_preset_topup")
    builder.button(text="🔗 Своя ссылка", callback_data="mail_btn_custom_link")
    builder.button(text="🗑 Удалить кнопку", callback_data="mail_btn_clear")
    builder.button(text="⬅️ Назад", callback_data="mail_preview_back")
    builder.adjust(1)
    return builder.as_markup()


def admin_plan_edit_keyboard(plan_id, is_active, is_trial=False, allow_auto_renewal=True, admin_only=False):
    """Расширенная клавиатура редактирования тарифа."""
    builder = InlineKeyboardBuilder()
    status_text = "⚪️ Сделать неактивным" if is_active else "🟢 Сделать активным"
    admin_only_text = "🔒 Только для админов: ВКЛ" if admin_only else "🔓 Только для админов: ВЫКЛ"
    trial_status_text = "❌ Убрать пробный статус" if is_trial else "⭐️ Сделать пробным"
    renewal_text = "🔄 Автопродление: ВКЛ → ВЫКЛ" if allow_auto_renewal else "🔄 Автопродление: ВЫКЛ → ВКЛ"

    builder.button(text="✏️ Название", callback_data=f"edit_plan_field_name_{plan_id}")
    builder.button(text="📝 Описание", callback_data=f"edit_plan_field_description_{plan_id}")
    builder.button(text="💰 Цена", callback_data=f"edit_plan_field_price_{plan_id}")
    builder.button(text="⏳ Длительность (число)", callback_data=f"edit_plan_field_duration_value_{plan_id}")
    builder.button(text="📅 Длительность (ед.)", callback_data=f"edit_plan_field_duration_unit_{plan_id}")
    builder.button(text="🆔 Код тарифа для Prodamus", callback_data=f"edit_plan_field_prodamus_subscription_id_{plan_id}")
    builder.button(text="🔁 Лимит автопродлений Prodamus", callback_data=f"edit_plan_field_prodamus_autopayments_limit_{plan_id}")
    builder.adjust(2)

    builder.row(InlineKeyboardButton(text=trial_status_text, callback_data=f"toggle_plan_is_trial_{plan_id}"))
    if is_trial:
        builder.row(InlineKeyboardButton(text="🎯 Назначить тариф для перехода",
                                         callback_data=f"set_plan_upgrade_target_{plan_id}"))
        builder.row(InlineKeyboardButton(text="⏳ Повторный пробный период (0=нельзя)",
                                         callback_data=f"edit_plan_field_trial_cooldown_days_{plan_id}"))

    builder.row(InlineKeyboardButton(text=renewal_text, callback_data=f"toggle_plan_allow_auto_renewal_{plan_id}"))
    builder.row(InlineKeyboardButton(text=admin_only_text, callback_data=f"toggle_plan_admin_only_{plan_id}"))
    builder.row(InlineKeyboardButton(text=status_text, callback_data=f"toggle_plan_activity_{plan_id}"))
    builder.row(InlineKeyboardButton(text="🗑️ Удалить тариф", callback_data=f"confirm_admin_delete_plan_{plan_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ К списку тарифов", callback_data="admin_manage_plans"))

    return builder.as_markup()


def admin_clients_keyboard(
    users,
    page,
    total_pages,
    is_searching=False,
    navigation_token=None,
    back_callback="admin_panel",
):
    builder = InlineKeyboardBuilder()
    profile_suffix = (
        f":{navigation_token}"
        if navigation_token
        else f":admin_clients_list_{page}"
    )
    search_suffix = f":{navigation_token}" if navigation_token else ""

    for u in users:
        label = f"@{u.username}" if u.username else u.first_name
        if not label:
            label = str(u.id)
        if u.crisis_detected_at:
            label = f"🚨 {label}"
        builder.row(InlineKeyboardButton(
            text=label,
            callback_data=f"admin_user_profile_{u.id}{profile_suffix}",
        ))

    pagination_suffix = f":{navigation_token}" if navigation_token else ""
    builder.row(*standard_pagination_row(
        page,
        total_pages,
        lambda target: f"admin_clients_list_{target}{pagination_suffix}",
    ))
    if is_searching:
        builder.row(InlineKeyboardButton(
            text="❌ Сбросить поиск",
            callback_data=f"admin_clients_reset_search{search_suffix}",
        ))
    else:
        builder.row(InlineKeyboardButton(
            text="🔍 Поиск",
            callback_data=f"admin_clients_start_search{search_suffix}",
        ))
    back_data = (
        f"admin_clients_back:{navigation_token}"
        if navigation_token
        else back_callback
    )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_data))

    return builder.as_markup()


def admin_user_profile_keyboard(
    user_id,
    is_blocked,
    crisis_detected=False,
    return_callback="admin_clients_list_0",
    navigation_token=None,
):
    builder = InlineKeyboardBuilder()

    suffix = f":{navigation_token}" if navigation_token else ""

    block_text = "🔓 Разблокировать" if is_blocked else "🔒 Заблокировать"
    builder.button(text=block_text, callback_data=f"admin_block_toggle_{user_id}{suffix}")
    if crisis_detected:
        builder.button(text="🚨 Снять кризисный флаг", callback_data=f"admin_crisis_clear_{user_id}{suffix}")
    builder.button(text="🌀 Воронка клиента", callback_data=f"admin_user_funnel_{user_id}{suffix}")
    builder.button(text="📜 История диалога", callback_data=f"admin_history_view_{user_id}_0{suffix}")
    builder.button(text="🔄 Сбросы для тестирования", callback_data=f"admin_user_resets_{user_id}{suffix}")
    profile_return_callback = return_callback
    if navigation_token and ":clientctx_" not in profile_return_callback:
        profile_return_callback = f"{profile_return_callback}:{navigation_token}"
    builder.button(text="⬅️ К списку", callback_data=profile_return_callback)

    builder.adjust(1)
    return builder.as_markup()


def admin_history_keyboard(user_id, page, total_pages, navigation_token=None):
    builder = InlineKeyboardBuilder()
    suffix = f":{navigation_token}" if navigation_token else ""

    builder.row(InlineKeyboardButton(text="💾 Скачать историю (.txt)", callback_data=f"admin_history_download_{user_id}{suffix}"))
    builder.row(*standard_pagination_row(
        page,
        total_pages,
        lambda target: f"admin_history_view_{user_id}_{target}{suffix}",
    ))
    builder.row(InlineKeyboardButton(text="⬅️ Назад к профилю", callback_data=f"admin_user_profile_{user_id}{suffix}"))

    return builder.as_markup()


def gender_selection_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="👨 Мужской", callback_data="gender_male")
    builder.button(text="👩 Женский", callback_data="gender_female")
    builder.adjust(2)
    return builder.as_markup()


def user_settings_keyboard(user):
    builder = InlineKeyboardBuilder()

    builder.button(text="✏️ Изменить имя", callback_data="settings_change_name")

    gender_label = "👨 Мужской" if user.gender == 'male' else ("👩 Женский" if user.gender == 'female' else "❓ Не указан")
    builder.button(text=f"👤 Пол: {gender_label}", callback_data="settings_change_gender")

    age_label = user.age if user.age else "Не указан"
    builder.button(text=f"🎂 Возраст: {age_label}", callback_data="settings_change_age")

    length_label = "📏 Обычный" if getattr(user, 'response_length', 'normal') != 'short' else "📏 Короткий"
    builder.button(text=f"Длина ответов: {length_label}", callback_data="settings_toggle_length")

    builder.button(text="❌ Закрыть", callback_data="settings_close")
    builder.adjust(1)
    return builder.as_markup()


# ==================== ПОДПИСКИ И ТАРИФЫ ====================

def admin_subscriptions_keyboard():
    """Legacy alias for the unified subscriptions and payments screen."""
    return admin_payment_keyboard()


def admin_prodamus_settings_keyboard(config, back_callback: str = "admin_payment_settings"):
    """Технические настройки оплаты для админа."""
    notif_status = "✅ Включены" if config.notifications_enabled else "❌ Выключены"
    sub_status = "✅ Включены" if config.subscriptions_enabled else "❌ Выключены"
    bonus_days = config.welcome_bonus_days

    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Режим оплаты", callback_data="admin_pay_mode__admin_prodamus_settings")
    builder.button(text="🔑 Данные Prodamus", callback_data="admin_prodamus_keys_from_settings")
    builder.button(text="🏦 Способы оплаты в Telegram", callback_data="admin_pay_providers_0__admin_prodamus_settings")
    builder.button(text="💰 Баланс и стоимость сообщений", callback_data="admin_pay_prices__admin_prodamus_settings")
    builder.button(text="🌐 Приём оплат с лендинга", callback_data="admin_webhook_settings__admin_prodamus_settings")
    builder.button(text="📝 Текст «О подписке»", callback_data="edit_cont_pay:subscription_info__admin_prodamus_settings")
    builder.button(text=f"Уведомления о покупках: {notif_status}", callback_data="admin_toggle_sub_notifs__admin_prodamus_settings")
    builder.button(text=f"Система подписок: {sub_status}", callback_data="admin_toggle_subscriptions__admin_prodamus_settings")
    builder.button(text=f"🎁 Бонус при входе: {bonus_days} дн.", callback_data="admin_set_welcome_bonus__admin_prodamus_settings")
    builder.button(text="⬅️ Назад", callback_data=back_callback)
    builder.adjust(1)
    return builder.as_markup()


def admin_prodamus_keys_keyboard(back_callback: str = "admin_payment_settings"):
    """Клавиатура управления ключами Prodamus."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Ссылка на форму оплаты Prodamus", callback_data=f"set_prodamus_key_url__{back_callback}")
    builder.button(text="🔑 Секретный ключ Prodamus", callback_data=f"set_prodamus_key_secret__{back_callback}")
    builder.button(text="📚 Контент / доступы для чека", callback_data=f"set_prodamus_key_paid_content__{back_callback}")
    builder.button(text="💳 Токен оплаты Telegram (необязательно)", callback_data=f"set_prodamus_key_tg_token__{back_callback}")
    builder.button(text="⬅️ Назад", callback_data=back_callback)
    builder.adjust(1)
    return builder.as_markup()


def admin_select_duration_unit_keyboard():
    """Выбор единицы длительности тарифа."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Дни", callback_data="set_duration_unit_days")
    builder.button(text="Месяцы", callback_data="set_duration_unit_months")
    return builder.as_markup()


def subscription_info_keyboard(sub_info=None, referral_info=None):
    """Клавиатура подписки для клиента."""
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оформить / Продлить доступ", callback_data="sub_select_plan")
    if sub_info and sub_info.get('allow_auto_renewal', True):
        if sub_info.get('auto_renewal'):
            builder.button(text="❌ Отменить автопродление", callback_data="sub_toggle_renewal")
        else:
            builder.button(text="✅ Включить автопродление", callback_data="sub_toggle_renewal")
    builder.button(text="🎁 Ввести промокод", callback_data="sub_enter_promo")
    if referral_info and referral_info.get('enabled'):
        builder.button(
            text=referral_info.get('sub_btn_name', '🤝 Бонус за приглашение'),
            callback_data="menu_referral"
        )
    builder.button(text="⬅️ Главное меню", callback_data="back_to_main_menu_text")
    builder.adjust(1)
    return builder.as_markup()


async def configured_subscription_info_keyboard(sub_info=None, referral_info=None):
    fallback = subscription_info_keyboard(sub_info, referral_info=referral_info)
    section, buttons = await load_menu_buttons("subscription_info")
    if section is None:
        return fallback
    if not section.enabled:
        return InlineKeyboardMarkup(inline_keyboard=[])

    desired_toggle = None
    if sub_info and sub_info.get("allow_auto_renewal", True):
        desired_toggle = "❌ Отменить автопродление" if sub_info.get("auto_renewal") else "✅ Включить автопродление"
    toggle_candidates = [
        button for button in buttons
        if (button.legacy_callback or button.action_value) == "sub_toggle_renewal"
    ]
    selected_toggle = None
    if desired_toggle is not None:
        selected_toggle = next(
            (button for button in toggle_candidates if not button.label_override and button.label == desired_toggle),
            next((button for button in toggle_candidates if button.label_override), None),
        )
        if selected_toggle is None and toggle_candidates:
            selected_toggle = toggle_candidates[0]
    rows = []
    for button in buttons:
        slot_action = button.legacy_callback or button.action_value
        if slot_action == "sub_toggle_renewal":
            if selected_toggle is None or button is not selected_toggle:
                continue
            label = None if button.label_override else desired_toggle
        elif slot_action == "menu_referral":
            if not (referral_info and referral_info.get("enabled")):
                continue
            label = button.label if button.label_override else referral_info.get("sub_btn_name", button.label)
        else:
            label = None
        rows.append([button_to_telegram(button, label=label)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_referral_settings_keyboard(ref_config, sub_config):
    """Клавиатура настроек реферальной системы для админа."""
    builder = InlineKeyboardBuilder()
    enabled_status = "✅ Включена" if sub_config.referral_enabled else "❌ Выключена"
    mode_str = "Баллы (1 балл = 1 руб)" if sub_config.referral_reward_mode == "points" else "Бонусные дни"
    builder.button(text=f"Реферальная программа: {enabled_status}", callback_data="admin_ref_toggle_enabled")
    builder.button(text=f"Режим вознаграждения: {mode_str}", callback_data="admin_ref_toggle_mode")
    builder.button(text=f"✏️ Баллы за регистрацию: {ref_config.points_per_invite}", callback_data="admin_ref_edit_reg_points")
    builder.button(text=f"✏️ % от первой оплаты: {sub_config.referral_first_pay_percent}%", callback_data="admin_ref_edit_first_percent")
    builder.button(text=f"✏️ % от повторных оплат: {sub_config.referral_recurring_pay_percent}%", callback_data="admin_ref_edit_recurring_percent")
    builder.button(text=f"✏️ Бонусные дни: {sub_config.referral_pay_bonus_days} дн.", callback_data="admin_ref_edit_bonus_days")
    builder.button(text="⬅️ Назад", callback_data="admin_payment_settings")
    builder.adjust(1)
    return builder.as_markup()


async def payment_provider_keyboard(plan_id, final_price, intent_token: str | None = None):
    """Клавиатура выбора платёжной системы."""
    builder = InlineKeyboardBuilder()

    async with async_session_maker() as session:
        config = await session.get(SubscriptionConfig, 1)
        tg_providers = (await session.execute(
            select(PaymentProvider).where(PaymentProvider.is_active == True)
        )).scalars().all()

    prodamus_cb = f"pay_prodamus_{intent_token}" if intent_token else f"pay_prodamus_{plan_id}_{final_price}"
    tg_sub_suffix = intent_token if intent_token else str(plan_id)

    # Prodamus
    if config and config.prodamus_url and config.prodamus_secret_key:
        builder.button(text="💳 Prodamus (Карта/СБП)", callback_data=prodamus_cb)

    # Telegram Pay providers
    for p in tg_providers:
        builder.button(text=f"💳 {p.name}", callback_data=f"pay_prov_sub_{p.id}_{tg_sub_suffix}")

    builder.button(text="⬅️ Назад к выбору тарифа", callback_data="sub_select_plan")
    builder.adjust(1)
    return builder.as_markup()


async def admin_select_upgrade_plan_keyboard(current_plan_id):
    """Выбор тарифа для перехода с пробного."""
    async with async_session_maker() as session:
        plans = (await session.execute(
            select(SubscriptionPlan).where(
                SubscriptionPlan.id != current_plan_id,
                SubscriptionPlan.is_trial == False
            ).order_by(SubscriptionPlan.price)
        )).scalars().all()

    builder = InlineKeyboardBuilder()
    for p in plans:
        builder.button(text=f"{p.name} ({p.price:.0f}₽)", callback_data=f"set_upgrade_plan_{current_plan_id}_{p.id}")
    builder.button(text="❌ Убрать связь", callback_data=f"set_upgrade_plan_{current_plan_id}_none")
    builder.button(text="⬅️ Отмена", callback_data=f"adm_edit_plan_{current_plan_id}")
    builder.adjust(1)
    return builder.as_markup()


def admin_promocodes_keyboard(promos, page=0, total_pages=1):
    """Список промокодов для админа."""
    builder = InlineKeyboardBuilder()
    for p in promos:
        status = "✅" if p.is_active else "❌"
        builder.row(InlineKeyboardButton(
            text=f"{status} {p.code} ({p.discount_percent}% / {p.free_days}д)",
            callback_data=f"admin_edit_promo_{p.id}",
        ))
    builder.row(*standard_pagination_row(page, total_pages, lambda target: f"admin_promocodes_{target}"))
    builder.row(InlineKeyboardButton(text="➕ Добавить промокод", callback_data="admin_create_promo"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_payment_settings"))
    return builder.as_markup()


def admin_edit_promo_keyboard(promo_id, is_active):
    """Редактирование промокода."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Код", callback_data=f"edit_promo_field_code_{promo_id}")
    builder.button(text="💰 Скидка %", callback_data=f"edit_promo_field_discount_{promo_id}")
    builder.button(text="🎁 Бесплатные дни", callback_data=f"edit_promo_field_days_{promo_id}")
    builder.button(text="🔢 Макс. использований", callback_data=f"edit_promo_field_max_uses_{promo_id}")
    builder.button(text="📎 Привязать тарифы", callback_data=f"admin_assign_promo_{promo_id}_page_0")
    toggle_text = "⚪️ Деактивировать" if is_active else "🟢 Активировать"
    builder.button(text=toggle_text, callback_data=f"toggle_promo_active_{promo_id}")
    builder.button(text="🗑️ Удалить", callback_data=f"confirm_delete_promo_{promo_id}")
    builder.button(text="⬅️ К списку промокодов", callback_data="admin_promocodes")
    builder.adjust(2, 2, 1, 1, 1, 1)
    return builder.as_markup()


def assign_promo_to_plan_keyboard(promo_id, all_plans, assigned_plan_ids, applies_to_all, page, total_pages):
    builder = InlineKeyboardBuilder()

    all_plans_text = "✅ Применяется ко всем тарифам" if applies_to_all else "❌ Только выбранные тарифы"
    builder.row(InlineKeyboardButton(text=all_plans_text, callback_data=f"promo_toggle_all_plans_{promo_id}_{page}"))

    if not applies_to_all:
        for plan in all_plans:
            is_assigned = plan.id in assigned_plan_ids
            action = "remove" if is_assigned else "add"
            text = f"✅ {plan.name}" if is_assigned else f"⭕️ {plan.name}"
            builder.row(InlineKeyboardButton(
                text=text,
                callback_data=f"promo_plan_toggle_{action}_{promo_id}_{plan.id}_{page}",
            ))

    builder.row(*standard_pagination_row(
        page,
        total_pages,
        lambda target: f"admin_assign_promo_{promo_id}_page_{target}",
    ))

    builder.row(InlineKeyboardButton(text="⬅️ Назад к промокоду", callback_data=f"admin_edit_promo_{promo_id}"))
    return builder.as_markup()


def back_to_previous_menu(callback_data):
    """Универсальная кнопка назад."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)]
    ])


def standard_pagination_row(page: int, total_pages: int, callback_for_page) -> list[InlineKeyboardButton]:
    """Return one predictable pagination row with safe boundary buttons."""
    total_pages = max(1, int(total_pages or 1))
    page = max(0, min(int(page or 0), total_pages - 1))
    previous_callback = callback_for_page(page - 1) if page > 0 else "noop"
    next_callback = callback_for_page(page + 1) if page < total_pages - 1 else "noop"
    return [
        InlineKeyboardButton(text="⬅️", callback_data=previous_callback),
        InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"),
        InlineKeyboardButton(text="➡️", callback_data=next_callback),
    ]


def fixed_pagination_rows(page: int, total_pages: int, callback_for_page) -> list[list[InlineKeyboardButton]]:
    """Backward-compatible wrapper for the single-row admin pagination."""
    return [standard_pagination_row(page, total_pages, callback_for_page)]


def format_ai_log_button(log, max_length: int = 60) -> str:
    status = getattr(log, "status", None) or "success"
    status_marker = "❌ " if status == "error" else "✅ "
    fallback_marker = "↪️ " if getattr(log, "attempt_role", None) == "fallback" else ""
    followup_marker = "[F] " if getattr(log, "request_type", "chat") == "followup" else ""
    from ai_logs import format_msk
    timestamp = format_msk(log.created_at, "%d.%m %H:%M") if log.created_at else "--.-- --:--"
    provider = str(log.provider or "—")
    model = str(log.model or "—")
    latency = f" ({log.latency_ms / 1000:.1f}s)" if log.latency_ms else ""
    fixed = f"{fallback_marker}{status_marker}{followup_marker}{timestamp} | "
    provider_prefix = f"{provider}: "
    model_limit = max_length - len(fixed) - len(provider_prefix) - len(latency)
    if model_limit < 1:
        provider_limit = max(1, max_length - len(fixed) - len(": ") - len(latency) - 1)
        provider = provider[:max(provider_limit - 1, 1)] + "…"
        provider_prefix = f"{provider}: "
        model_limit = max(1, max_length - len(fixed) - len(provider_prefix) - len(latency))
    truncated_model = model[:model_limit - 1] + "…" if len(model) > model_limit else model
    return f"{fixed}{provider_prefix}{truncated_model}{latency}"


def admin_ai_logs_keyboard(
    logs,
    page: int,
    total_pages: int,
    filter_user_id: int | None = None,
    period: str = "all",
    request_type: str = "all",
    status: str = "all",
    back_callback: str = "ai",
):
    request_type = request_type if request_type in {"all", "chat", "followup", "standalone"} else "all"
    status = status if status in {"all", "success", "error"} else "all"
    back_callback = back_callback if back_callback in {"ai", "logs"} else "ai"
    source_suffix = f"_{back_callback}"

    def filter_suffix() -> str:
        if status != "all" or request_type != "all":
            return f"_{request_type}_{status}"
        return ""

    def list_callback(target_page: int) -> str:
        suffix = filter_suffix()
        if filter_user_id:
            return f"admin_user_ai_logs_{filter_user_id}_{target_page}_{period}{suffix}{source_suffix}"
        return f"admin_ai_logs_{target_page}_{period}{suffix}{source_suffix}"

    builder = InlineKeyboardBuilder()
    detail_source_suffix = f"_{back_callback}"
    for log in logs:
        cb = f"admin_ai_log_{log.id}_{page}_{filter_user_id or 0}_{period}_{request_type}_{status}{detail_source_suffix}"
        builder.button(text=format_ai_log_button(log), callback_data=cb)

    builder.adjust(1)
    for navigation_row in fixed_pagination_rows(page, total_pages, list_callback):
        builder.row(*navigation_row)

    period_labels = (("Сегодня", "today"), ("7 дней", "7d"), ("30 дней", "30d"), ("Все", "all"))
    suffix = filter_suffix()
    builder.row(*[
        InlineKeyboardButton(
            text=("✅ " if value == period else "") + label,
            callback_data=(
                f"admin_user_ai_logs_{filter_user_id}_0_{value}{suffix}"
                f"{source_suffix}"
                if filter_user_id else f"admin_ai_logs_0_{value}{suffix}{source_suffix}"
            ),
        )
        for label, value in period_labels
    ])
    type_labels = (("Все типы", "all"), ("Обычные", "chat"), ("Догоняющие", "followup"))
    builder.row(*[
        InlineKeyboardButton(
            text=("✅ " if value == request_type else "") + label,
            callback_data=(
                f"admin_user_ai_logs_{filter_user_id}_0_{period}_{value}_{status}"
                f"{source_suffix}"
                if filter_user_id
                else f"admin_ai_logs_0_{period}_{value}_{status}{source_suffix}"
            ),
        )
        for label, value in type_labels
    ])
    status_labels = (("Все статусы", "all"), ("✅ Успех", "success"), ("❌ Ошибки", "error"))
    builder.row(*[
        InlineKeyboardButton(
            text=("✅ " if value == status else "") + label,
            callback_data=(
                f"admin_user_ai_logs_{filter_user_id}_0_{period}_{request_type}_{value}"
                f"{source_suffix}"
                if filter_user_id
                else f"admin_ai_logs_0_{period}_{request_type}_{value}{source_suffix}"
            ),
        )
        for label, value in status_labels
    ])
    builder.row(InlineKeyboardButton(
        text="📦 Скачать пакет логов",
        callback_data=f"export_ai_logs_{filter_user_id or 0}_{period}_{request_type}_{status}",
    ))
    back_cb = f"admin_user_profile_{filter_user_id}" if filter_user_id else (
        "admin_section_logs" if back_callback == "logs" else "admin_ai_settings"
    )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb))
    return builder.as_markup()


def admin_ai_log_detail_keyboard(
    log_id: int,
    page: int = 0,
    filter_user_id: int | None = None,
    period: str = "all",
    request_type: str = "all",
    status: str = "all",
    back_callback: str = "ai",
):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📄 Скачать .txt файл лога",
        callback_data=(
            f"admin_ai_log_file_{log_id}_{filter_user_id}"
            if filter_user_id
            else f"admin_ai_log_file_{log_id}"
        ),
    )
    suffix = f"_{request_type}_{status}" if (request_type != "all" or status != "all") else ""
    back_cb = (
        f"admin_user_ai_logs_{filter_user_id}_{page}_{period}{suffix}_{back_callback}"
        if filter_user_id
        else f"admin_ai_logs_{page}_{period}{suffix}_{back_callback}"
    )
    builder.button(text="⬅️ Назад к логам", callback_data=back_cb)
    builder.adjust(1)
    return builder.as_markup()
