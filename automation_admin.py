"""Admin UI for Automation Handlers & Stage Analytics (Automation MVP).

Provides core event handler management (creation, activation toggling, in-place editing
of name, trigger, payload, and scope) and funnel stage analytics.
"""

from __future__ import annotations

import html
import json
import logging
from typing import Any, Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from automation_engine import (
    AutomationActionExecution,
    AutomationConversationState,
    AutomationHandler,
    AutomationStepTransition,
    get_stage_analytics,
)
from admin_roles import AdminPermission, user_has_permission
from crisis_safety import get_crisis_marker_settings, normalize_crisis_marker, serialize_crisis_markers
from database import Topic, User, async_session_maker, get_all_admin_ids

logger = logging.getLogger("automation_admin")
router = Router(name="automation_admin")


def _safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    if val is None or val == "" or str(val).lower() in ("none", "null"):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


async def _is_admin(user_id: int) -> bool:
    return await user_has_permission(user_id, AdminPermission.SYSTEM)


class AutomationAdminStates(StatesGroup):
    waiting_handler_name = State()
    waiting_trigger_event = State()
    waiting_action_text = State()
    waiting_edit_name = State()
    waiting_edit_trigger = State()
    waiting_edit_payload = State()
    waiting_recipient_id = State()
    waiting_crisis_marker_add = State()
    waiting_crisis_marker_edit = State()


def _parse_recipient_input(value: str) -> set[int]:
    if value.strip().startswith("["):
        try:
            value = " ".join(str(item) for item in json.loads(value))
        except (TypeError, ValueError):
            pass
    result = set()
    for part in value.replace(",", " ").split():
        try:
            recipient_id = int(part)
        except ValueError:
            continue
        if recipient_id:
            result.add(recipient_id)
    return result


async def _show_recipient_settings(
    callback: CallbackQuery,
    handler_id: int,
    answer_text: str | None = None,
) -> None:
    async with async_session_maker() as session:
        handler = await session.get(AutomationHandler, handler_id)
        if not handler:
            await callback.answer("Обработчик не найден.", show_alert=True)
            return
        selected = _parse_recipient_input(handler.recipient_ids_json or "")
        available = await get_all_admin_ids()

    all_ids = sorted(available | selected)
    text_lines = [
        f"👥 <b>Получатели обработчика</b>\n",
        "Нажмите на администратора, чтобы включить или выключить его. "
        "Можно также добавить любой Telegram ID вручную.",
        f"\nВыбрано: <b>{len(selected)}</b>",
    ]
    builder = InlineKeyboardBuilder()
    for recipient_id in all_ids:
        prefix = "✅" if recipient_id in selected else "⬜️"
        builder.button(
            text=f"{prefix} {recipient_id}",
            callback_data=f"automation_handler_recipient_toggle_{handler_id}_{recipient_id}",
        )
    builder.button(text="➕ Добавить Telegram ID", callback_data=f"automation_handler_recipients_add_{handler_id}")
    builder.button(text="🧹 Очистить список", callback_data=f"automation_handler_recipients_clear_ask_{handler_id}")
    builder.button(text="⬅️ К обработчику", callback_data=f"automation_handler_{handler_id}")
    builder.adjust(1)
    await callback.message.edit_text("\n".join(text_lines), reply_markup=builder.as_markup(), parse_mode="HTML")
    if answer_text:
        await callback.answer(answer_text)
    else:
        await callback.answer()

# ---------------------------------------------------------------------------
# Main Automation Menu
# ---------------------------------------------------------------------------

def _automation_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="⚡ Обработчики событий", callback_data="automation_handlers")
    builder.button(text="🛡 Кризисные маркеры", callback_data="automation_crisis_markers")
    builder.button(text="💬 Догоняющие сообщения", callback_data="admin_fu_list")
    builder.button(text="📊 Статистика этапов", callback_data="admin_automation_stage_stats")
    builder.button(text="📋 Справка по данным сценария", callback_data="automation_data_help")
    builder.button(text="⬅️ В админ-панель", callback_data="admin_panel")
    builder.adjust(1)
    return builder.as_markup()


async def _show_crisis_marker_settings(event: Message | CallbackQuery) -> None:
    async with async_session_maker() as session:
        _, enabled, markers = await get_crisis_marker_settings(session)

    status = "✅ Включена" if enabled else "⏸ Выключена"
    lines = [
        "🛡 <b>Кризисная защита по маркерам</b>",
        "",
        f"Статус: <b>{status}</b>",
        "",
        "Защита проверяет пользовательский текст до ответа ИИ. "
        "Маркеры нормализуются и ищутся как целые фразы; одиночные слова требуют контекста, "
        "чтобы не реагировать на очевидные упоминания вне личного запроса.",
        "",
    ]
    if markers:
        lines.append("<b>Маркеры:</b>")
    else:
        lines.append("<i>Маркеры не добавлены.</i>")

    builder = InlineKeyboardBuilder()
    builder.button(
        text="⏸ Отключить защиту" if enabled else "✅ Включить защиту",
        callback_data="automation_crisis_toggle",
    )
    for index, marker in enumerate(markers):
        builder.row(
            InlineKeyboardButton(text=f"✏️ {marker}", callback_data=f"automation_crisis_edit_{index}"),
            InlineKeyboardButton(text="🗑", callback_data=f"automation_crisis_delete_{index}"),
        )
    builder.button(text="➕ Добавить маркер", callback_data="automation_crisis_add")
    builder.button(text="⬅️ Меню автоматизаций", callback_data="automation_menu")
    builder.adjust(1)

    text = "\n".join(lines)
    if isinstance(event, CallbackQuery) or hasattr(event, "data"):
        if event.message:
            await event.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


def _crisis_marker_index(callback_data: str) -> int | None:
    value = callback_data.rsplit("_", 1)[-1]
    return _safe_int(value)


@router.callback_query(F.data == "automation_crisis_markers")
async def automation_crisis_markers(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    await state.clear()
    await _show_crisis_marker_settings(callback)


@router.callback_query(F.data == "automation_crisis_toggle")
async def automation_crisis_toggle(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    async with async_session_maker() as session:
        config, enabled, _ = await get_crisis_marker_settings(session)
        config.crisis_marker_protection_enabled = not enabled
        await session.commit()
    await _show_crisis_marker_settings(callback)


@router.callback_query(F.data == "automation_crisis_add")
async def automation_crisis_add(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AutomationAdminStates.waiting_crisis_marker_add)
    if callback.message:
        await callback.message.edit_text(
            "➕ <b>Новый кризисный маркер</b>\n\n"
            "Введите слово или фразу до 160 символов. Лучше использовать личное намерение, "
            "например: <code>хочу причинить себе вред</code>.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="automation_crisis_markers")]]
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("automation_crisis_edit_"))
async def automation_crisis_edit(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    index = _crisis_marker_index(callback.data)
    async with async_session_maker() as session:
        _, _, markers = await get_crisis_marker_settings(session)
    if index is None or index < 0 or index >= len(markers):
        await callback.answer("Маркер не найден.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AutomationAdminStates.waiting_crisis_marker_edit)
    await state.update_data(crisis_marker_index=index)
    if callback.message:
        await callback.message.edit_text(
            f"✏️ <b>Изменение кризисного маркера</b>\n\n"
            f"Текущее значение: <code>{html.escape(markers[index])}</code>\n"
            "Введите новое слово или фразу до 160 символов:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="automation_crisis_markers")]]
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("automation_crisis_delete_"))
async def automation_crisis_delete(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    payload = callback.data.removeprefix("automation_crisis_delete_")
    index = _safe_int(payload.rsplit("_", 1)[-1])
    if not payload.startswith("confirm_") and isinstance(callback, CallbackQuery):
        async with async_session_maker() as session:
            _, _, markers = await get_crisis_marker_settings(session)
        if index is None or index < 0 or index >= len(markers):
            await callback.answer("Маркер не найден.", show_alert=True)
            return
        if callback.message:
            await callback.message.edit_text(
                "⚠️ <b>Удалить кризисный маркер?</b>\n\n"
                f"Маркер: <code>{html.escape(markers[index])}</code>\n"
                "Защита по этому маркеру больше не будет срабатывать.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"automation_crisis_delete_confirm_{index}")],
                        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="automation_crisis_markers")],
                    ]
                ),
                parse_mode="HTML",
            )
        await callback.answer()
        return
    async with async_session_maker() as session:
        config, _, markers = await get_crisis_marker_settings(session)
        if index is None or index < 0 or index >= len(markers):
            await callback.answer("Маркер не найден.", show_alert=True)
            return
        markers.pop(index)
        config.crisis_markers_json = serialize_crisis_markers(markers)
        await session.commit()
    await _show_crisis_marker_settings(callback)


async def _save_crisis_marker_input(message: Message, state: FSMContext, *, edit: bool) -> None:
    if (message.text or "").strip().lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Изменение кризисного маркера отменено.")
        await _show_crisis_marker_settings(message)
        return
    marker = normalize_crisis_marker(message.text)
    if not marker:
        await message.answer("Введите непустой маркер длиной не более 160 символов.")
        return

    data = await state.get_data()
    index = data.get("crisis_marker_index") if edit else None
    async with async_session_maker() as session:
        config, _, markers = await get_crisis_marker_settings(session)
        if edit:
            if not isinstance(index, int) or index < 0 or index >= len(markers):
                await state.clear()
                await message.answer("Маркер не найден. Откройте настройки заново.")
                return
            duplicate = marker in markers and markers[index] != marker
            if duplicate:
                await message.answer("Такой маркер уже есть в списке.")
                return
            markers[index] = marker
        else:
            if marker in markers:
                await message.answer("Такой маркер уже есть в списке.")
                return
            if len(markers) >= 100:
                await message.answer("Можно добавить не более 100 маркеров.")
                return
            markers.append(marker)
        config.crisis_markers_json = serialize_crisis_markers(markers)
        await session.commit()

    await state.clear()
    await message.answer("✅ Маркер сохранён.")
    await _show_crisis_marker_settings(message)


@router.message(AutomationAdminStates.waiting_crisis_marker_add)
async def process_crisis_marker_add_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    await _save_crisis_marker_input(message, state, edit=False)


@router.message(AutomationAdminStates.waiting_crisis_marker_edit)
async def process_crisis_marker_edit_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    await _save_crisis_marker_input(message, state, edit=True)


@router.callback_query(F.data == "automation_menu")
async def automation_menu(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state:
        await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "⚙️ <b>Автоматизации</b>\n\n"
            "Здесь можно настроить действия, которые бот выполняет сам:\n\n"
            "<b>Обработчики событий</b> — например, отправить администратору заявку, "
            "когда пользователь оставил контакт или перешел на целевой шаг.\n\n"
            "<b>Догоняющие сообщения</b> — напомнить пользователю о диалоге после паузы.\n\n"
            "<b>Статистика этапов</b> — посмотреть, до какого места доходят пользователи.\n\n"
            "<b>Справка по данным сценария</b> — подсказка для расширенной настройки.",
            reply_markup=_automation_menu_keyboard(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "automation_data_help")
async def automation_data_help(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="automation_menu")]])
    if callback.message:
        await callback.message.edit_text(
            "📋 <b>Данные сценария</b>\n\n"
            "Нейросеть в ответах пользователю может отдавать невидимый сервисный блок данных:\n\n"
            "<code>&lt;DATA&gt;\n"
            "{\n"
            '  "current_state": {"current_step": "CONTACT_OBTAINED"},\n'
            '  "events": ["lead_captured"],\n'
            '  "metadata": {"phone": "+7999..."}\n'
            "}\n"
            "&lt;/DATA&gt;</code>\n\n"
            "Этот служебный блок автоматически вырезается перед показом пользователю и "
            "запускает настроенные действия сценария. Обычные настройки обработчиков "
            "можно выполнять без этого раздела.",
            reply_markup=kb,
            parse_mode="HTML",
        )
    await callback.answer()


# ---------------------------------------------------------------------------
# Handlers List
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "automation_handlers")
async def automation_handlers_list(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()

    async with async_session_maker() as session:
        handlers = (
            await session.execute(
                select(AutomationHandler).order_by(AutomationHandler.id)
            )
        ).scalars().all()

    builder = InlineKeyboardBuilder()
    text_lines = [
        f"⚡ <b>Обработчики событий ({len(handlers)})</b>\n",
        "Обработчик — это правило «если произошло событие/шаг, сделать действие». "
        "Он срабатывает, только если подходит область и выполнены условия.\n",
    ]

    if not handlers:
        text_lines.append("<i>Обработчики событий пока не созданы. Нажмите «➕ Новый обработчик».</i>")
    else:
        for h in handlers:
            status = "✅" if h.is_active else "⏸"
            scope_prefix = "🌐" if h.scope == "global" else "🏷"
            builder.row(
                InlineKeyboardButton(
                    text=f"{scope_prefix} {status} {h.name}",
                    callback_data=f"automation_handler_{h.id}",
                )
            )

    builder.row(InlineKeyboardButton(text="➕ Новый обработчик", callback_data="automation_handler_add"))
    builder.row(InlineKeyboardButton(text="⬅️ Меню автоматизаций", callback_data="automation_menu"))

    if callback.message:
        await callback.message.edit_text("\n".join(text_lines), reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


# ---------------------------------------------------------------------------
# Handler Details & Toggle
# ---------------------------------------------------------------------------

async def _show_handler_card(event: Message | CallbackQuery, handler_id: int):
    async with async_session_maker() as session:
        handler = await session.get(AutomationHandler, handler_id)
        if not handler:
            msg = "Обработчик не найден."
            if isinstance(event, CallbackQuery):
                await event.answer(msg, show_alert=True)
            else:
                await event.answer(msg)
            return

        total_execs = await session.scalar(
            select(func.count(AutomationActionExecution.id)).where(AutomationActionExecution.handler_id == handler_id)
        ) or 0
        delivered_execs = await session.scalar(
            select(func.count(AutomationActionExecution.id)).where(
                AutomationActionExecution.handler_id == handler_id,
                AutomationActionExecution.status == "delivered",
            )
        ) or 0

    status_str = "✅ Активен" if handler.is_active else "⏸ На паузе"
    toggle_label = "⏸ Приостановить" if handler.is_active else "✅ Включить"

    trigger_desc = f"Событие сценария: <code>{handler.event_name}</code>" if handler.event_name else (
        f"Этап воронки: <code>{handler.trigger_step}</code>" if handler.trigger_step else "Любое событие"
    )
    scope_label = {
        "global": "Все сценарии",
        "funnel": "Только воронка",
        "main": "Основной сценарий",
    }.get(handler.scope, handler.scope)

    action_label = {
        "send_admin_message": "Отправить сообщение администраторам",
        "send_user_message": "Отправить сообщение пользователю",
        "set_metadata": "Обновить метаданные диалога",
    }.get(handler.action_type, handler.action_type)
    if handler.recipient_ids_json is None:
        recipient_desc = "Все администраторы"
    else:
        recipient_ids = sorted(_parse_recipient_input(handler.recipient_ids_json))
        recipient_desc = ", ".join(str(item) for item in recipient_ids) if recipient_ids else "не выбраны"

    try:
        payload_data = json.loads(handler.action_payload_json or "{}")
    except (TypeError, ValueError):
        payload_data = {}
    payload_text = payload_data.get("text") if isinstance(payload_data, dict) else None
    payload_desc = html.escape(str(payload_text)) if payload_text else "Дополнительные настройки действия сохранены."

    text = (
        f"⚡ <b>Обработчик: {html.escape(handler.name)}</b>\n\n"
        f"<b>Статус:</b> {status_str}\n"
        f"<b>Где работает:</b> {scope_label}\n"
        f"<b>Условие срабатывания:</b> {trigger_desc}\n"
        f"<b>Действие:</b> {action_label}\n"
        f"<b>Получатели:</b> <code>{html.escape(recipient_desc)}</code>\n"
        f"<b>Срабатываний:</b> {total_execs} (доставлено: {delivered_execs})\n\n"
        f"<b>Текст действия:</b>\n{payload_desc}"
    )

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=f"Статус: {toggle_label}", callback_data=f"automation_handler_toggle_{handler.id}"))
    builder.row(InlineKeyboardButton(text="✏️ Изменить имя", callback_data=f"automation_handler_edit_name_{handler.id}"))
    builder.row(InlineKeyboardButton(text="⚡ Изменить триггер", callback_data=f"automation_handler_edit_trigger_{handler.id}"))
    builder.row(InlineKeyboardButton(text="📝 Изменить текст действия", callback_data=f"automation_handler_edit_payload_{handler.id}"))
    builder.row(InlineKeyboardButton(text="👥 Получатели", callback_data=f"automation_handler_recipients_{handler.id}"))
    builder.row(InlineKeyboardButton(text=f"🌐 Где работает: {scope_label}", callback_data=f"automation_handler_scope_toggle_{handler.id}"))
    builder.row(InlineKeyboardButton(text="🗑 Удалить обработчик", callback_data=f"automation_handler_delete_{handler.id}"))
    builder.row(InlineKeyboardButton(text="⬅️ К списку обработчиков", callback_data="automation_handlers"))

    if isinstance(event, CallbackQuery) and event.message:
        await event.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await event.answer()
    elif isinstance(event, Message):
        await event.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("automation_handler_"))
async def callback_automation_handler_router(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    await state.clear()
    parts = callback.data.split("_")

    if callback.data == "automation_handler_add":
        await state.set_state(AutomationAdminStates.waiting_handler_name)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="automation_handlers")]])
        if callback.message:
            await callback.message.edit_text(
                "➕ <b>Создание нового обработчика событий</b>\n\n"
                "Введите название обработчика (например, <i>«Уведомление о заявке на сессию»</i>):",
                reply_markup=kb,
                parse_mode="HTML",
            )
        await callback.answer()
        return

    if len(parts) >= 4 and parts[2] == "recipients" and parts[3].isdigit():
        await _show_recipient_settings(callback, int(parts[3]))
        return

    if len(parts) >= 6 and parts[2] == "recipient" and parts[3] == "toggle":
        hid = _safe_int(parts[4], None)
        recipient_id = _safe_int(parts[5], None)
        if not hid or not recipient_id:
            await callback.answer("Неверные параметры.", show_alert=True)
            return
        async with async_session_maker() as session:
            handler = await session.get(AutomationHandler, hid)
            if not handler:
                await callback.answer("Обработчик не найден.", show_alert=True)
                return
            recipient_ids = _parse_recipient_input(handler.recipient_ids_json or "")
            if recipient_id in recipient_ids:
                recipient_ids.remove(recipient_id)
            else:
                recipient_ids.add(recipient_id)
            handler.recipient_ids_json = json.dumps(sorted(recipient_ids))
            await session.commit()
        await _show_recipient_settings(callback, hid)
        return

    if len(parts) >= 5 and parts[2] == "recipients" and parts[3] == "add":
        hid = _safe_int(parts[4], None)
        if hid:
            await state.set_state(AutomationAdminStates.waiting_recipient_id)
            await state.update_data(handler_id=hid)
            if callback.message:
                await callback.message.edit_text(
                    "Введите Telegram ID получателя (целое число):",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"automation_handler_recipients_{hid}")]]
                    ),
                )
            await callback.answer()
        return

    if len(parts) >= 5 and parts[2] == "recipients" and parts[3] == "clear":
        action = parts[4]
        confirmed = action == "confirm"
        hid_part = parts[5] if action in {"ask", "confirm"} and len(parts) >= 6 else action
        hid = _safe_int(hid_part, None)
        if not hid:
            await callback.answer("Неверный ID обработчика.", show_alert=True)
            return

        async with async_session_maker() as session:
            handler = await session.get(AutomationHandler, hid)
            if not handler:
                await callback.answer("Обработчик не найден.", show_alert=True)
                return
            selected = _parse_recipient_input(handler.recipient_ids_json or "")
            has_recipients = handler.recipient_ids_json is None or bool(selected)

            if not confirmed:
                if not has_recipients:
                    await _show_recipient_settings(
                        callback,
                        hid,
                        answer_text="Список получателей уже пуст.",
                    )
                    return
                if callback.message:
                    await callback.message.edit_text(
                        "⚠️ <b>Очистить список получателей?</b>\n\n"
                        "Обработчик перестанет отправлять уведомления всем текущим получателям.",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="✅ Да, очистить", callback_data=f"automation_handler_recipients_clear_confirm_{hid}")],
                                [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"automation_handler_recipients_{hid}")],
                            ]
                        ),
                        parse_mode="HTML",
                    )
                await callback.answer()
                return

            if not has_recipients:
                await _show_recipient_settings(
                    callback,
                    hid,
                    answer_text="Список получателей уже пуст.",
                )
                return
            handler.recipient_ids_json = json.dumps([])
            await session.commit()
        await _show_recipient_settings(callback, hid)
        return

    # Edit Name
    if len(parts) >= 4 and parts[2] == "edit" and parts[3] == "name":
        hid = _safe_int(parts[4] if len(parts) > 4 else None, None)
        if hid:
            await state.set_state(AutomationAdminStates.waiting_edit_name)
            await state.update_data(handler_id=hid)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"automation_handler_{hid}")]])
            if callback.message:
                await callback.message.edit_text("Введите новое название обработчика (2-100 символов):", reply_markup=kb)
            await callback.answer()
            return

    # Edit Trigger
    if len(parts) >= 4 and parts[2] == "edit" and parts[3] == "trigger":
        hid = _safe_int(parts[4] if len(parts) > 4 else None, None)
        if hid:
            await state.set_state(AutomationAdminStates.waiting_edit_trigger)
            await state.update_data(handler_id=hid)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"automation_handler_{hid}")]])
            if callback.message:
                await callback.message.edit_text(
                    "Введите событие или шаг, после которого отправлять сообщение.\n"
                    "Например: <code>lead_captured</code> или <code>step:CONTACT_SHARED</code>:",
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            await callback.answer()
            return

    # Edit Payload
    if len(parts) >= 4 and parts[2] == "edit" and parts[3] == "payload":
        hid = _safe_int(parts[4] if len(parts) > 4 else None, None)
        if hid:
            await state.set_state(AutomationAdminStates.waiting_edit_payload)
            await state.update_data(handler_id=hid)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"automation_handler_{hid}")]])
            if callback.message:
                await callback.message.edit_text(
                    "Введите новый текст сообщения. Можно использовать подстановки:\n"
                    "Теги: <code>{user_id}</code>, <code>{event}</code>, <code>{username}</code>, "
                    "<code>{first_name}</code>, <code>{user_text}</code>, <code>{practice}</code>, "
                    "<code>{topic}</code>, <code>{time}</code>:",
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            await callback.answer()
            return

    # Scope Toggle
    if len(parts) >= 4 and parts[2] == "scope" and parts[3] == "toggle":
        hid = _safe_int(parts[4] if len(parts) > 4 else None, None)
        if hid:
            async with async_session_maker() as session:
                h = await session.get(AutomationHandler, hid)
                if h:
                    h.scope = "main" if h.scope == "global" else "global"
                    await session.commit()
                    scope_label = "все диалоги" if h.scope == "global" else "основной диалог"
                    await callback.answer(f"Область действия изменена: {scope_label}.")
            await _show_handler_card(callback, hid)
            return

    # Toggle
    if len(parts) >= 4 and parts[2] == "toggle":
        hid = _safe_int(parts[3], None)
        if not hid:
            await callback.answer("Неверный ID.", show_alert=True)
            return
        async with async_session_maker() as session:
            h = await session.get(AutomationHandler, hid)
            if h:
                h.is_active = 0 if h.is_active else 1
                await session.commit()
                st_msg = "активирован" if h.is_active else "приостановлен"
                await callback.answer(f"Обработчик {st_msg}!")
        await _show_handler_card(callback, hid)
        return

    # Delete
    if len(parts) >= 4 and parts[2] == "delete":
        confirmed = len(parts) >= 5 and parts[3] == "confirm"
        hid = _safe_int(parts[4] if confirmed else parts[3], None)
        if not hid:
            await callback.answer("Неверный ID.", show_alert=True)
            return
        if not confirmed:
            if callback.message:
                await callback.message.edit_text(
                    "⚠️ <b>Подтвердите удаление</b>\n\n"
                    "Обработчик и его настройки будут удалены без возможности отмены.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text="✅ Да, удалить",
                            callback_data=f"automation_handler_delete_confirm_{hid}",
                        )],
                        [InlineKeyboardButton(
                            text="⬅️ Отмена",
                            callback_data=f"automation_handler_{hid}",
                        )],
                    ]),
                    parse_mode="HTML",
                )
            await callback.answer()
            return
        async with async_session_maker() as session:
            h = await session.get(AutomationHandler, hid)
            if h:
                await session.delete(h)
                await session.commit()
        await callback.answer("Обработчик удален.")
        await automation_handlers_list(callback)
        return

    # Detail view: automation_handler_{id}
    hid = _safe_int(parts[-1], None)
    if not hid:
        await callback.answer("Неверный ID.", show_alert=True)
        return
    await state.clear()
    await _show_handler_card(callback, hid)


@router.message(AutomationAdminStates.waiting_edit_name)
async def process_edit_name_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if name.lower() in {"отмена", "назад", "/cancel"}:
        data = await state.get_data()
        hid = data.get("handler_id")
        await state.clear()
        await message.answer("Изменение отменено.")
        if hid:
            await _show_handler_card(message, hid)
        return
    if not (2 <= len(name) <= 100):
        await message.answer("⚠️ Название должно быть от 2 до 100 символов.")
        return
    data = await state.get_data()
    hid = data.get("handler_id")
    if hid:
        async with async_session_maker() as session:
            h = await session.get(AutomationHandler, hid)
            if h:
                h.name = name
                await session.commit()
    await state.clear()
    await message.answer(f"✅ Название обработчика успешно обновлено: «{html.escape(name)}»")
    if hid:
        await _show_handler_card(message, hid)


@router.message(AutomationAdminStates.waiting_edit_trigger)
async def process_edit_trigger_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    trigger = (message.text or "").strip()
    data = await state.get_data()
    hid = data.get("handler_id")
    if trigger.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Изменение отменено.")
        if hid:
            await _show_handler_card(message, hid)
        return
    if not trigger:
        await message.answer("Введите событие или шаг, после которого нужно отправлять сообщение.")
        return
    if hid:
        async with async_session_maker() as session:
            h = await session.get(AutomationHandler, hid)
            if h:
                if trigger.startswith("step:"):
                    h.trigger_step = trigger.split(":", 1)[1]
                    h.event_name = None
                else:
                    h.event_name = trigger
                    h.trigger_step = None
                await session.commit()
    await state.clear()
    await message.answer(f"✅ Триггер обработчика успешно обновлен: <code>{html.escape(trigger)}</code>", parse_mode="HTML")
    if hid:
        await _show_handler_card(message, hid)


@router.message(AutomationAdminStates.waiting_edit_payload)
async def process_edit_payload_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    text_val = (message.text or "").strip()
    data = await state.get_data()
    hid = data.get("handler_id")
    if text_val.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Изменение отменено.")
        if hid:
            await _show_handler_card(message, hid)
        return
    if not text_val:
        await message.answer("Текст действия не должен быть пустым.")
        return
    if hid:
        async with async_session_maker() as session:
            h = await session.get(AutomationHandler, hid)
            if h:
                if text_val.startswith("{") and text_val.endswith("}"):
                    try:
                        json.loads(text_val)
                        h.action_payload_json = text_val
                    except Exception:
                        h.action_payload_json = json.dumps({"text": text_val}, ensure_ascii=False)
                else:
                    h.action_payload_json = json.dumps({"text": text_val}, ensure_ascii=False)
                await session.commit()
    await state.clear()
    await message.answer("✅ Текст действия успешно обновлён!")
    if hid:
        await _show_handler_card(message, hid)


@router.message(AutomationAdminStates.waiting_recipient_id)
async def process_recipient_id_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    raw_value = (message.text or "").strip()
    data = await state.get_data()
    hid = data.get("handler_id")
    if raw_value.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Добавление получателя отменено.")
        if hid:
            await _show_handler_card(message, hid)
        return
    recipient_ids = _parse_recipient_input(raw_value)
    if len(recipient_ids) != 1:
        await message.answer("Введите один корректный Telegram ID, например 123456789.")
        return
    if not hid:
        await state.clear()
        return
    async with async_session_maker() as session:
        handler = await session.get(AutomationHandler, hid)
        if handler:
            current = _parse_recipient_input(handler.recipient_ids_json or "")
            current.update(recipient_ids)
            handler.recipient_ids_json = json.dumps(sorted(current))
            await session.commit()
    await state.clear()
    await message.answer("✅ Получатель добавлен.")
    await _show_handler_card(message, hid)


@router.message(AutomationAdminStates.waiting_handler_name)
async def process_handler_name_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if name.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Создание обработчика отменено.")
        return
    if not (2 <= len(name) <= 100):
        await message.answer("⚠️ Название должно быть от 2 до 100 символов:")
        return
    await state.update_data(name=name)
    await state.set_state(AutomationAdminStates.waiting_trigger_event)
    await message.answer(
        "⚡ <b>Введите событие или шаг для запуска</b>\n\n"
        "Например: <code>lead_captured</code> или <code>step:CONTACT_SHARED</code>:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="automation_handlers")]]
        ),
        parse_mode="HTML",
    )


@router.message(AutomationAdminStates.waiting_trigger_event)
async def process_handler_trigger_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    event_str = (message.text or "").strip()
    if event_str.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Создание обработчика отменено.")
        return
    if not event_str:
        await message.answer("Введите название события или шага, после которого нужно отправлять сообщение.")
        return
    await state.update_data(event_name=event_str)
    await state.set_state(AutomationAdminStates.waiting_action_text)
    await message.answer(
        "📝 <b>Введите текст сообщения администраторам</b>\n\n"
        "Теги: <code>{user_id}</code>, <code>{event}</code>, <code>{username}</code>, "
        "<code>{first_name}</code>, <code>{user_text}</code>, <code>{practice}</code>, "
        "<code>{topic}</code>, <code>{time}</code>:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="automation_handlers")]]
        ),
        parse_mode="HTML",
    )


@router.message(AutomationAdminStates.waiting_action_text)
async def process_handler_action_text_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    text_content = (message.text or "").strip()
    data = await state.get_data()
    if text_content.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Создание обработчика отменено.")
        return
    if not text_content:
        await message.answer("Текст сообщения не должен быть пустым. Введите его ещё раз.")
        return
    name = data.get("name", "Новый обработчик")
    event_name = data.get("event_name", "custom_event")

    async with async_session_maker() as session:
        handler = AutomationHandler(
            name=name,
            scope="global",
            event_name=event_name,
            action_type="send_admin_message",
            action_payload_json=json.dumps({"text": text_content}, ensure_ascii=False),
            recipient_ids_json=json.dumps([]),
            is_active=1,
        )
        session.add(handler)
        await session.commit()
        await session.refresh(handler)
        hid = handler.id

    await state.clear()
    await message.answer(f"✅ Обработчик «{html.escape(name)}» успешно создан и активирован!")
    await _show_handler_card(message, hid)


# ---------------------------------------------------------------------------
# Stage Statistics & Analytics
# ---------------------------------------------------------------------------

@router.callback_query(F.data.in_({"automation_stage_stats", "admin_automation_stage_stats"}))
async def admin_automation_stage_stats(callback: CallbackQuery):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return

    async with async_session_maker() as session:
        stats = await get_stage_analytics(session)

    stages = stats.get("stages", [])
    text_lines = [
        "📊 <b>Статистика прохождения этапов (Воронка)</b>\n",
        f"Всего активных этапов: <b>{len(stages)}</b>\n",
    ]

    if not stages:
        text_lines.append("<i>Данных по переходам этапов пока нет.</i>")
    else:
        for idx, st in enumerate(stages, start=1):
            step_name = st["step"]
            reached = st["users_reached"]
            transitions = st["transitions"]
            curr = st["users_currently_at_step"]
            text_lines.append(
                f"{idx}. <b>{html.escape(str(step_name))}</b>\n"
                f"   👥 Достигли: <b>{reached}</b> чел. (переходов: {transitions})\n"
                f"   📍 Сейчас на этапе: <b>{curr}</b> чел.\n"
            )

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню автоматизаций", callback_data="automation_menu")]])
    if callback.message:
        await callback.message.edit_text("\n".join(text_lines), reply_markup=kb, parse_mode="HTML")
    await callback.answer()
