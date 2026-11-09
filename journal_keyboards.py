from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from database import JournalEntry, JournalQuestion
from universal_tests import get_answer_options, get_question_type


def journal_question_keyboard(entry_id: int, position: int, question: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    options = question.get("options") or []
    for index, option in enumerate(options):
        label = option.get("text") if isinstance(option, dict) else str(option)
        builder.button(text=str(label), callback_data=f"j_opt:{entry_id}:{position}:{index}")
    builder.button(text="⏭ Пропустить", callback_data=f"j_skip:{entry_id}:{position}")
    builder.button(text="❌ Выйти", callback_data="j_exit")
    if options:
        builder.adjust(1)
    else:
        builder.adjust(1, 1)
    return builder.as_markup()


def journal_entry_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Открыть дневник", callback_data="journal_open_stats")
    builder.button(text="▶️ Начать рефлексию", callback_data="j_start")
    builder.button(text="⬅️ Главное меню", callback_data="back_to_main_menu_text")
    builder.adjust(1)
    return builder.as_markup()


def journal_menu_keyboard(
    has_in_progress: bool = False,
    in_progress_id: int | None = None,
    is_today_completed: bool = False,
    completed_id: int | None = None,
    has_video: bool = False,
) -> InlineKeyboardMarkup:
    """Build keyboard for main journal screen."""
    builder = InlineKeyboardBuilder()

    if has_in_progress and in_progress_id:
        builder.button(text="▶️ Продолжить", callback_data=f"j_res:{in_progress_id}")
        builder.button(text="🔄 Начать заново", callback_data=f"j_rst:{in_progress_id}")
    elif is_today_completed and completed_id:
        builder.button(text="👁 Посмотреть запись за сегодня", callback_data=f"j_h_v:{completed_id}")
    else:
        builder.button(text="▶️ Пройти рефлексию сегодня", callback_data="j_start")

    if has_video:
        builder.button(text="🎬 Видеоинструкция", callback_data="j_video")

    builder.button(text="⏰ Время напоминания", callback_data="j_time_menu")
    builder.button(text="📖 Мои прошлые записи", callback_data="j_h_p:0")
    builder.button(text="📈 Моя динамика", callback_data="j_dyn_menu")
    builder.button(text="🧘 Формирование привычек", callback_data="mode_habits")
    builder.button(text="⬅️ Главное меню", callback_data="back_to_main_menu_text")

    row_widths = [2] if has_in_progress else [1]
    remaining = 5 + (1 if has_video else 0)
    row_widths.extend([1] * remaining)
    builder.adjust(*row_widths)

    return builder.as_markup()


def journal_in_progress_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    """Keyboard shown when resuming an in-progress reflection."""
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Продолжить", callback_data=f"j_res:{entry_id}")
    builder.button(text="🔄 Начать заново", callback_data=f"j_rst:{entry_id}")
    builder.button(text="⬅️ В меню Дневника", callback_data="mode_journal")
    builder.adjust(2, 1)
    return builder.as_markup()


def journal_completed_today_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    """Keyboard shown when reflection is already finished today."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👁 Посмотреть запись за сегодня", callback_data=f"j_h_v:{entry_id}")
    builder.button(text="📈 Моя динамика", callback_data="j_dyn_menu")
    builder.button(text="⬅️ В меню Дневника", callback_data="mode_journal")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def journal_reminder_time_keyboard() -> InlineKeyboardMarkup:
    """Reminder settings keyboard: presets, manual, disable."""
    builder = InlineKeyboardBuilder()
    builder.button(text="19:00", callback_data="j_time_set:1900")
    builder.button(text="20:00", callback_data="j_time_set:2000")
    builder.button(text="21:00", callback_data="j_time_set:2100")
    builder.button(text="22:00", callback_data="j_time_set:2200")
    builder.button(text="✏️ Ввести вручную", callback_data="j_time_manual")
    builder.button(text="🔕 Отключить", callback_data="j_time_off")
    builder.button(text="⬅️ Назад в Дневник", callback_data="mode_journal")
    builder.adjust(2, 2, 1, 1, 1)
    return builder.as_markup()


def journal_reminder_message_keyboard() -> InlineKeyboardMarkup:
    """Keyboard attached to the evening reminder message."""
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Начать рефлексию", callback_data="j_start")
    builder.button(text="⏰ Напомнить позже", callback_data="j_snooze_menu")
    builder.adjust(1, 1)
    return builder.as_markup()


def journal_snooze_menu_keyboard() -> InlineKeyboardMarkup:
    """Snooze duration selection."""
    builder = InlineKeyboardBuilder()
    builder.button(text="15 минут", callback_data="j_snooze:15")
    builder.button(text="30 минут", callback_data="j_snooze:30")
    builder.button(text="1 час", callback_data="j_snooze:60")
    builder.button(text="⬅️ Отмена", callback_data="mode_journal")
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()


def journal_history_list_keyboard(
    page: int,
    total_pages: int,
    entries: list[JournalEntry],
) -> InlineKeyboardMarkup:
    """Paginated list of completed entries."""
    builder = InlineKeyboardBuilder()
    for e in entries:
        builder.button(text=f"📅 {e.reflection_date}", callback_data=f"j_h_v:{e.id}")

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"j_h_p:{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{max(total_pages, 1)}", callback_data="noop"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"j_h_p:{page + 1}"))

    builder.row(*nav_buttons)
    builder.button(text="⬅️ В меню Дневника", callback_data="mode_journal")
    return builder.as_markup()


def journal_entry_detail_keyboard(
    entry_id: int,
    has_summary: bool,
    token8: str,
) -> InlineKeyboardMarkup:
    """Detail view of a specific entry."""
    builder = InlineKeyboardBuilder()
    if not has_summary:
        builder.button(text="🔄 Повторить формирование итога", callback_data=f"j_ret:{entry_id}:{token8}")
    builder.button(text="📖 К списку записей", callback_data="j_h_p:0")
    builder.button(text="⬅️ В меню Дневника", callback_data="mode_journal")
    builder.adjust(1)
    return builder.as_markup()


def journal_dynamics_menu_keyboard(token8: str) -> InlineKeyboardMarkup:
    """Menu to pick 7 or 21 days dynamics."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📈 7 дней", callback_data=f"j_d7:{token8}")
    builder.button(text="📈 21 день", callback_data=f"j_d21:{token8}")
    builder.button(text="⬅️ Назад", callback_data="mode_journal")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def journal_admin_menu_keyboard() -> InlineKeyboardMarkup:
    """Admin root menu for journal configuration."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Управление вопросами", callback_data="j_adm_q_list")
    builder.button(text="⭐️ Настройка баллов за рефлексию", callback_data="j_adm_pts_edit")
    builder.button(text="⬅️ В админ-панель", callback_data="admin_panel")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def journal_admin_questions_keyboard(questions: list[JournalQuestion]) -> InlineKeyboardMarkup:
    """Admin question list with ordering, toggle, edit, move, delete."""
    builder = InlineKeyboardBuilder()

    for q in questions:
        status_text = "🟢 ВКЛ" if q.is_active else "⚪️ ВЫКЛ"
        type_label = {
            "text": "текст",
            "scale": "1–10",
            "choice": "варианты",
        }.get(get_question_type(q), get_question_type(q))
        option_count = len(get_answer_options(q))
        title = f"{q.order_num}. [{type_label}] {q.text[:24]}"
        if len(q.text) > 24:
            title += "..."
        builder.row(InlineKeyboardButton(text=title, callback_data=f"j_adm_q_ed:{q.id}"))
        builder.row(
            InlineKeyboardButton(text=f"{status_text} ({option_count})", callback_data=f"j_adm_q_tog:{q.id}"),
            InlineKeyboardButton(text="✏️", callback_data=f"j_adm_q_ed:{q.id}"),
            InlineKeyboardButton(text="⬆️", callback_data=f"j_adm_q_up:{q.id}"),
            InlineKeyboardButton(text="⬇️", callback_data=f"j_adm_q_dn:{q.id}"),
            InlineKeyboardButton(text="🗑", callback_data=f"j_adm_q_del_confirm:{q.id}"),
        )

    builder.row(InlineKeyboardButton(text="➕ Добавить вопрос", callback_data="j_adm_q_add"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад в настройки Дневника", callback_data="admin_journal_menu"))
    return builder.as_markup()


def journal_admin_cancel_keyboard(target: str = "j_adm_q_list") -> InlineKeyboardMarkup:
    """Cancel button for admin FSM states."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Отмена", callback_data=target)
    return builder.as_markup()
