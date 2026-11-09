"""Admin UI for Follow-up Campaigns & Steps (Full CRUD & Parity with PsychoBot)."""

from __future__ import annotations

import html
import logging
import math
from typing import Any, Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from admin_roles import AdminPermission, user_has_permission
from keyboards import standard_pagination_row
from database import Topic, User, async_session_maker
from followups import (
    FollowupCampaign,
    FollowupDelivery,
    FollowupStep,
    send_followup_step_to_user,
)

logger = logging.getLogger("followup_admin")
router = Router(name="followup_admin")


def _safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    if val is None or val == "" or str(val).lower() in ("none", "null"):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


async def _is_admin(user_id: int) -> bool:
    return await user_has_permission(user_id, AdminPermission.SYSTEM)


class FollowupAdminStates(StatesGroup):
    waiting_campaign_name = State()
    waiting_rename_campaign = State()
    waiting_step_delay = State()
    waiting_step_text = State()
    waiting_step_ai_instruction = State()
    waiting_step_button = State()
    waiting_quiet_hours = State()


# ---------------------------------------------------------------------------
# Campaign List
# ---------------------------------------------------------------------------

@router.callback_query(F.data.in_({"admin_followup_list", "admin_fu_list"}))
@router.callback_query(F.data.startswith("admin_followup_list_"))
@router.callback_query(F.data.startswith("admin_fu_list_"))
async def admin_followup_list(callback: CallbackQuery, state: FSMContext | None = None):
    """Lists all follow-up campaigns with status and quick controls."""
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()

    try:
        page = int(callback.data.rsplit("_", 1)[1]) if callback.data.rsplit("_", 1)[-1].isdigit() else 0
    except ValueError:
        page = 0
    page_size = 10
    async with async_session_maker() as session:
        base_query = select(FollowupCampaign).order_by(FollowupCampaign.id)
        total = await session.scalar(select(func.count()).select_from(base_query.subquery())) or 0
        total_pages = max(1, math.ceil(total / page_size)) if total else 1
        page = max(0, min(page, total_pages - 1))
        campaigns = (
            await session.execute(
                base_query.options(selectinload(FollowupCampaign.steps))
                .offset(page * page_size)
                .limit(page_size)
            )
        ).scalars().all()

        kb = InlineKeyboardBuilder()
        text_lines = [
            "💬 <b>Напоминания неактивным</b>\n",
            "Цепочки сообщений для пользователей, которые надолго прервали диалог.\n",
            f"Страница {page + 1} из {total_pages}.\n",
        ]

        if not campaigns:
            text_lines.append("<i>Цепочки ещё не созданы. Нажмите «➕ Новая цепочка», чтобы добавить.</i>")
        else:
            for c in campaigns:
                status_icon = "✅" if c.is_active else "⏸"
                scope_label = "Все диалоги" if c.scope == "global" else (
                    "Основной диалог" if c.scope == "main" else "Отдельная тема"
                )
                step_count = len(c.steps)
                text_lines.append(
                    f"{status_icon} <b>{html.escape(c.name)}</b> ({scope_label}, шагов: {step_count})"
                )
                kb.row(InlineKeyboardButton(
                    text=f"{status_icon} {c.name}",
                    callback_data=f"admin_fu_campaign_{c.id}",
                ))

        kb.row(*standard_pagination_row(
            page,
            total_pages,
            lambda target: f"admin_fu_list_{target}",
        ))
        kb.row(
            InlineKeyboardButton(text="➕ Новая цепочка", callback_data="admin_fu_add")
        )
        kb.row(
            InlineKeyboardButton(text="⬅️ В раздел автоматизаций", callback_data="admin_section_automation")
        )

        text = "\n".join(text_lines)
        if callback.message:
            await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


# ---------------------------------------------------------------------------
# Create Campaign
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin_fu_add")
async def callback_admin_fu_add(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    await state.clear()
    await state.set_state(FollowupAdminStates.waiting_campaign_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_fu_list")]])
    if callback.message:
        await callback.message.edit_text(
            "➕ <b>Создание новой цепочки напоминаний</b>\n\n"
            "Введите название цепочки (например, <i>«Возврат после первой практики»</i>):\n"
            "После создания цепочка будет выключена, пока вы не добавите шаги.",
            reply_markup=kb,
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(FollowupAdminStates.waiting_campaign_name)
async def process_campaign_name_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if name.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Создание цепочки отменено.")
        return
    if not (2 <= len(name) <= 100):
        await message.answer("⚠️ Название должно быть от 2 до 100 символов. Попробуйте еще раз:")
        return

    async with async_session_maker() as session:
        campaign = FollowupCampaign(
            name=name,
            scope="global",
            is_active=0,
            quiet_hours_start=23,
            quiet_hours_end=9,
        )
        session.add(campaign)
        await session.commit()
        await session.refresh(campaign)
        cid = campaign.id

    await state.clear()
    await message.answer(f"✅ Цепочка «{html.escape(name)}» успешно создана (выключена). Добавьте шаги ниже:")
    # Show campaign card
    await _show_campaign_card(message, cid)


# ---------------------------------------------------------------------------
# View Campaign Detail
# ---------------------------------------------------------------------------

async def _show_campaign_card(event: Message | CallbackQuery, campaign_id: int):
    async with async_session_maker() as session:
        campaign = await session.scalar(
            select(FollowupCampaign)
            .where(FollowupCampaign.id == campaign_id)
            .options(selectinload(FollowupCampaign.steps))
        )
        if not campaign:
            msg = "Цепочка не найдена или была удалена."
            if isinstance(event, CallbackQuery):
                await event.answer(msg, show_alert=True)
            else:
                await event.answer(msg)
            return

        total_deliveries = await session.scalar(
            select(func.count(FollowupDelivery.id)).where(FollowupDelivery.campaign_id == campaign_id)
        ) or 0
        success_deliveries = await session.scalar(
            select(func.count(FollowupDelivery.id)).where(
                FollowupDelivery.campaign_id == campaign_id,
                FollowupDelivery.status == "delivered",
            )
        ) or 0

        status_text = "✅ Включена" if campaign.is_active else "⏸ На паузе"
        toggle_label = "⏸ Приостановить" if campaign.is_active else "✅ Включить"

        scope_desc = "Все диалоги" if campaign.scope == "global" else (
            "Основной диалог" if campaign.scope == "main" else "Отдельная тема"
        )

        steps_sorted = sorted(campaign.steps, key=lambda s: s.step_number)
        step_lines = []
        if not steps_sorted:
            step_lines.append("<i>Шагов пока нет. Нажмите «🪜 Шаги», чтобы добавить.</i>")
        else:
            for s in steps_sorted:
                type_label = "Персональный текст" if s.step_type == "ai" else "Готовый текст"
                preview = (s.message_text or s.ai_instruction or "")[:40]
                step_lines.append(f"• Шаг {s.step_number} (+{s.delay_minutes} мин, {type_label}): «{html.escape(preview)}…»")

        text = (
            f"📌 <b>Цепочка: {html.escape(campaign.name)}</b>\n\n"
            f"<b>Статус:</b> {status_text}\n"
            f"<b>Область действия:</b> {scope_desc}\n"
            f"<b>Тихие часы по московскому времени:</b> с {campaign.quiet_hours_start}:00 до {campaign.quiet_hours_end}:00\n"
            f"<b>Всего доставок:</b> {total_deliveries} (успешно: {success_deliveries})\n\n"
            f"<b>Шаги цепочки ({len(steps_sorted)}):</b>\n"
            + "\n".join(step_lines)
        )

        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text=f"Статус: {toggle_label}", callback_data=f"admin_fu_toggle_{campaign.id}"))
        kb.row(InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"admin_fu_rename_{campaign.id}"))
        kb.row(InlineKeyboardButton(text="💬 Область / Темы", callback_data=f"admin_fu_topics_{campaign.id}"))
        kb.row(InlineKeyboardButton(text=f"🪜 Шаги ({len(steps_sorted)})", callback_data=f"admin_fu_steps_{campaign.id}"))
        kb.row(InlineKeyboardButton(text="🌙 Тихие часы", callback_data=f"admin_fu_quiet_{campaign.id}"))
        if steps_sorted:
            kb.row(InlineKeyboardButton(text="🧪 Проверить шаг 1 на себе", callback_data=f"admin_fu_self_test_{campaign.id}"))
        kb.row(InlineKeyboardButton(text="🗑 Удалить цепочку", callback_data=f"admin_fu_delete_ask_{campaign.id}"))
        kb.row(InlineKeyboardButton(text="⬅️ К списку цепочек", callback_data="admin_fu_list"))

        if isinstance(event, CallbackQuery) and event.message:
            await event.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
            await event.answer()
        elif isinstance(event, Message):
            await event.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_fu_campaign_"))
@router.callback_query(F.data.startswith("admin_followup_view_"))
async def admin_followup_view(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    raw_id = callback.data.split("_")[-1]
    campaign_id = _safe_int(raw_id, None)
    if not campaign_id:
        await callback.answer("Неверный ID кампании.", show_alert=True)
        return
    await _show_campaign_card(callback, campaign_id)


# ---------------------------------------------------------------------------
# Toggle Campaign
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("admin_fu_toggle_"))
@router.callback_query(F.data.startswith("admin_followup_toggle_"))
async def admin_followup_toggle(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    raw_id = callback.data.split("_")[-1]
    campaign_id = _safe_int(raw_id, None)
    if not campaign_id:
        await callback.answer("Неверный ID кампании.", show_alert=True)
        return

    async with async_session_maker() as session:
        campaign = await session.scalar(
            select(FollowupCampaign).where(FollowupCampaign.id == campaign_id).options(selectinload(FollowupCampaign.steps))
        )
        if not campaign:
            await callback.answer("Кампания не найдена.", show_alert=True)
            return

        if not campaign.is_active and not campaign.steps:
            await callback.answer("⚠️ Нельзя включить цепочку без единого шага. Сначала добавьте шаг.", show_alert=True)
            return

        campaign.is_active = 0 if campaign.is_active else 1
        await session.commit()
        status_msg = "включена" if campaign.is_active else "поставлена на паузу"
        await callback.answer(f"Кампания {status_msg}!", show_alert=True)

    await _show_campaign_card(callback, campaign_id)


# ---------------------------------------------------------------------------
# Rename Campaign
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("admin_fu_rename_"))
async def callback_admin_fu_rename(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    campaign_id = _safe_int(callback.data.split("_")[-1], None)
    if not campaign_id:
        await callback.answer("Неверный ID.", show_alert=True)
        return
    await state.clear()
    await state.set_state(FollowupAdminStates.waiting_rename_campaign)
    await state.update_data(campaign_id=campaign_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_campaign_{campaign_id}")]]
    )
    if callback.message:
        await callback.message.edit_text("Введите новое название для цепочки:", reply_markup=kb)
    await callback.answer()


@router.message(FollowupAdminStates.waiting_rename_campaign)
async def process_rename_campaign_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    name = (message.text or "").strip()
    if name.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Переименование отменено.")
        if campaign_id:
            await _show_campaign_card(message, campaign_id)
        return
    if not (2 <= len(name) <= 100):
        await message.answer("⚠️ Название должно быть от 2 до 100 символов. Попробуйте еще раз:")
        return

    async with async_session_maker() as session:
        campaign = await session.get(FollowupCampaign, campaign_id)
        if campaign:
            campaign.name = name
            await session.commit()
    await state.clear()
    await message.answer(f"✅ Название обновлено: «{html.escape(name)}»")
    if campaign_id:
        await _show_campaign_card(message, campaign_id)


# ---------------------------------------------------------------------------
# Scope & Topics Selection
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("admin_fu_topics_"))
async def callback_admin_fu_topics(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    campaign_id = _safe_int(callback.data.split("_")[-1], None)
    if not campaign_id:
        await callback.answer("Неверный ID.", show_alert=True)
        return

    async with async_session_maker() as session:
        campaign = await session.get(FollowupCampaign, campaign_id)
        if not campaign:
            await callback.answer("Кампания не найдена.", show_alert=True)
            return
        topics = (await session.execute(select(Topic).order_by(Topic.id))).scalars().all()

    kb = InlineKeyboardBuilder()
    cur_scope = campaign.scope or "global"
    cur_topic = campaign.topic_slug

    kb.row(
        InlineKeyboardButton(
            text=f"{'✅ ' if cur_scope == 'global' else ''}🌐 Все темы (глобально)",
            callback_data=f"admin_fu_scope_all_{campaign_id}",
        )
    )
    kb.row(
        InlineKeyboardButton(
            text=f"{'✅ ' if cur_scope == 'main' else ''}💬 Только основной диалог",
            callback_data=f"admin_fu_scope_main_{campaign_id}",
        )
    )
    for t in topics:
        is_sel = cur_scope == "topic" and cur_topic == t.slug
        kb.row(
            InlineKeyboardButton(
                text=f"{'✅ ' if is_sel else ''}🏷 {t.name}",
                callback_data=f"admin_fu_scope_top_{campaign_id}_{t.slug}",
            )
        )
    kb.row(InlineKeyboardButton(text="⬅️ Назад к цепочке", callback_data=f"admin_fu_campaign_{campaign_id}"))

    if callback.message:
        await callback.message.edit_text(
            f"💬 <b>Область действия цепочки «{html.escape(campaign.name)}»</b>\n\n"
            "Выберите, для каких диалогов должны срабатывать напоминания этой цепочки:",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_fu_scope_all_"))
async def callback_admin_fu_scope_all(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    cid = _safe_int(callback.data.split("_")[-1], None)
    if cid:
        async with async_session_maker() as session:
            c = await session.get(FollowupCampaign, cid)
            if c:
                c.scope = "global"
                c.topic_slug = None
                await session.commit()
    await callback_admin_fu_topics(callback, state=state)


@router.callback_query(F.data.startswith("admin_fu_scope_main_"))
async def callback_admin_fu_scope_main(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    cid = _safe_int(callback.data.split("_")[-1], None)
    if cid:
        async with async_session_maker() as session:
            c = await session.get(FollowupCampaign, cid)
            if c:
                c.scope = "main"
                c.topic_slug = None
                await session.commit()
    await callback_admin_fu_topics(callback, state=state)


@router.callback_query(F.data.startswith("admin_fu_scope_top_"))
async def callback_admin_fu_scope_topic(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    parts = callback.data.split("_")
    cid = _safe_int(parts[4] if len(parts) > 4 else None, None)
    slug = parts[5] if len(parts) > 5 else None
    if cid and slug:
        async with async_session_maker() as session:
            c = await session.get(FollowupCampaign, cid)
            if c:
                c.scope = "topic"
                c.topic_slug = slug
                await session.commit()
    await callback_admin_fu_topics(callback, state=state)


# ---------------------------------------------------------------------------
# Quiet Hours
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("admin_fu_quiet_"))
async def callback_admin_fu_quiet(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    parts = callback.data.split("_")
    if len(parts) >= 6 and parts[3] == "set":
        cid = _safe_int(parts[4], None)
        h_start = _safe_int(parts[5], 23)
        h_end = _safe_int(parts[6] if len(parts) > 6 else 9, 9)
        if cid:
            async with async_session_maker() as session:
                c = await session.get(FollowupCampaign, cid)
                if c:
                    c.quiet_hours_start = h_start or 0
                    c.quiet_hours_end = h_end or 0
                    await session.commit()
            await callback.answer("Тихие часы обновлены!")
            await _show_campaign_card(callback, cid)
            return

    cid = _safe_int(parts[-1], None)
    if not cid:
        await callback.answer("Неверный ID.", show_alert=True)
        return

    async with async_session_maker() as session:
        campaign = await session.get(FollowupCampaign, cid)
        if not campaign:
            await callback.answer("Кампания не найдена.", show_alert=True)
            return

    kb = InlineKeyboardBuilder()
    cur = f"{campaign.quiet_hours_start}-{campaign.quiet_hours_end}"
    kb.row(InlineKeyboardButton(text=f"{'✅ ' if cur == '23-9' else ''}🌙 23:00 – 09:00 (стандарт)", callback_data=f"admin_fu_quiet_set_{cid}_23_9"))
    kb.row(InlineKeyboardButton(text=f"{'✅ ' if cur == '22-8' else ''}🌙 22:00 – 08:00", callback_data=f"admin_fu_quiet_set_{cid}_22_8"))
    kb.row(InlineKeyboardButton(text=f"{'✅ ' if cur == '0-8' else ''}🌙 00:00 – 08:00", callback_data=f"admin_fu_quiet_set_{cid}_0_8"))
    kb.row(InlineKeyboardButton(text=f"{'✅ ' if cur == '0-0' else ''}☀️ Без тихих часов (круглосуточно)", callback_data=f"admin_fu_quiet_set_{cid}_0_0"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад к цепочке", callback_data=f"admin_fu_campaign_{cid}"))

    if callback.message:
        await callback.message.edit_text(
            f"🌙 <b>Тихие часы цепочки «{html.escape(campaign.name)}»</b>\n\n"
            f"Текущие тихие часы: <b>с {campaign.quiet_hours_start}:00 до {campaign.quiet_hours_end}:00 по московскому времени</b>.\n"
            "В это время бот не отправляет напоминания, чтобы не тревожить пользователей ночью.\n\n"
            "Выберите режим:",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    await callback.answer()


# ---------------------------------------------------------------------------
# Steps List & CRUD
# ---------------------------------------------------------------------------

async def _show_steps_list(target: CallbackQuery | Message, cid: int) -> bool:
    async with async_session_maker() as session:
        campaign = await session.scalar(
            select(FollowupCampaign).where(FollowupCampaign.id == cid).options(selectinload(FollowupCampaign.steps))
        )
        if not campaign:
            return False

    steps_sorted = sorted(campaign.steps, key=lambda s: s.step_number)
    kb = InlineKeyboardBuilder()

    text_lines = [
        f"🪜 <b>Шаги цепочки «{html.escape(campaign.name)}» ({len(steps_sorted)})</b>\n",
        "Шаги отправляются строго последовательно. Каждый шаг отправляется после паузы.",
        "Шаг 2 не будет отправлен, пока не завершен шаг 1.\n",
    ]

    if not steps_sorted:
        text_lines.append("<i>Шагов пока нет. Создайте первый шаг ниже:</i>")
    else:
        for s in steps_sorted:
            type_label = "🤖 Персональный текст" if s.step_type == "ai" else "📝 Готовый текст"
            preview = (s.message_text or s.ai_instruction or "")[:35]
            text_lines.append(f"• <b>Шаг {s.step_number}</b> (+{s.delay_minutes} мин, {type_label}): «{html.escape(preview)}…»")
            kb.row(
                InlineKeyboardButton(
                    text=f"Шаг {s.step_number} (+{s.delay_minutes} мин): {preview}…",
                    callback_data=f"admin_fu_step_{cid}_{s.id}",
                )
            )

    kb.row(
        InlineKeyboardButton(text="➕ Добавить шаг (Текст)", callback_data=f"admin_fu_step_add_{cid}_static"),
        InlineKeyboardButton(text="➕ Добавить шаг (текст от ИИ)", callback_data=f"admin_fu_step_add_{cid}_ai"),
    )
    kb.row(InlineKeyboardButton(text="⬅️ Назад к цепочке", callback_data=f"admin_fu_campaign_{cid}"))

    if isinstance(target, CallbackQuery):
        if target.message:
            await target.message.edit_text("\n".join(text_lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        await target.answer("\n".join(text_lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    return True


@router.callback_query(F.data.startswith("admin_fu_steps_"))
async def callback_admin_fu_steps(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    cid = _safe_int(callback.data.split("_")[-1], None)
    if not cid:
        await callback.answer("Неверный ID.", show_alert=True)
        return
    if not await _show_steps_list(callback, cid):
        await callback.answer("Цепочка не найдена.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("admin_fu_step_add_"))
async def callback_admin_fu_step_add(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    parts = callback.data.split("_")
    cid = _safe_int(parts[4] if len(parts) > 4 else None, None)
    step_type = parts[5] if len(parts) > 5 else "static"
    if not cid:
        await callback.answer("Неверный ID кампании.", show_alert=True)
        return

    await state.clear()
    await state.set_state(FollowupAdminStates.waiting_step_delay)
    await state.update_data(campaign_id=cid, step_type=step_type)

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_steps_{cid}")]])
    type_desc = "персональным текстом от ИИ" if step_type == "ai" else "готовым текстом"
    if callback.message:
        await callback.message.edit_text(
            f"➕ <b>Добавление шага ({type_desc})</b>\n\n"
            "Введите задержку в минутах перед отправкой этого шага (например, <code>60</code> для 1 часа, <code>1440</code> для 1 дня):",
            reply_markup=kb,
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(FollowupAdminStates.waiting_step_delay)
async def process_step_delay_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    raw_val = (message.text or "").strip()
    data = await state.get_data()
    cancel_callback = f"admin_fu_steps_{data.get('campaign_id')}"
    if raw_val.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Изменение шага отменено." if data.get("editing") else "Добавление шага отменено.")
        if data.get("editing") and data.get("campaign_id") and data.get("step_id"):
            await _show_step_card(message, data["campaign_id"], data["step_id"])
        elif data.get("campaign_id"):
            await _show_steps_list(message, data["campaign_id"])
        return
    delay = _safe_int(raw_val, None)
    if delay is None or delay < 1 or delay > 43200:
        await message.answer(
            "⚠️ Введите целое число минут от 1 до 43200 (до 30 дней).",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=cancel_callback)]]
            ),
        )
        return

    if data.get("editing"):
        sid = data.get("step_id")
        cid = data.get("campaign_id")
        async with async_session_maker() as session:
            step = await session.get(FollowupStep, sid)
            if step:
                step.delay_minutes = delay
                await session.commit()
        await state.clear()
        await message.answer(f"✅ Задержка шага успешно изменена на {delay} мин.!")
        if cid and sid:
            await _show_step_card(message, cid, sid)
        return

    await state.update_data(delay_minutes=delay)
    step_type = data.get("step_type", "static")

    if step_type == "ai":
        await state.set_state(FollowupAdminStates.waiting_step_ai_instruction)
        await message.answer(
            "🧠 <b>Введите инструкцию для ИИ</b>\n\n"
            "Например: <i>«Напомни пользователю о незавершенной медитации и мягко спроси, как его самочувствие»</i>:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_steps_{data.get('campaign_id')}")]]
            ),
        )
    else:
        await state.set_state(FollowupAdminStates.waiting_step_text)
        await message.answer(
            "📝 <b>Введите текст напоминания</b>\n\n"
            "Можно использовать тег <code>{name}</code> для подстановки имени пользователя:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_steps_{data.get('campaign_id')}")]]
            ),
        )


@router.message(FollowupAdminStates.waiting_step_text)
async def process_step_text_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    text_content = (message.text or "").strip()
    data = await state.get_data()
    if text_content.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Изменение текста отменено.")
        if data.get("editing") and data.get("campaign_id") and data.get("step_id"):
            await _show_step_card(message, data["campaign_id"], data["step_id"])
        elif data.get("campaign_id"):
            await _show_steps_list(message, data["campaign_id"])
        return
    if len(text_content) < 2:
        await message.answer(
            "⚠️ Текст слишком короткий. Введите текст сообщения:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_steps_{data.get('campaign_id')}")]]
            ),
        )
        return

    cid = data.get("campaign_id")

    if data.get("editing"):
        sid = data.get("step_id")
        async with async_session_maker() as session:
            step = await session.get(FollowupStep, sid)
            if step:
                step.message_text = text_content
                await session.commit()
        await state.clear()
        await message.answer("✅ Текст шага успешно обновлен!")
        if cid and sid:
            await _show_step_card(message, cid, sid)
        return

    delay = data.get("delay_minutes", 60)
    async with async_session_maker() as session:
        cur_max = await session.scalar(
            select(func.max(FollowupStep.step_number)).where(FollowupStep.campaign_id == cid)
        ) or 0
        step = FollowupStep(
            campaign_id=cid,
            step_number=cur_max + 1,
            delay_minutes=delay,
            step_type="static",
            message_text=text_content,
        )
        session.add(step)
        await session.commit()
        await session.refresh(step)
        step_id = step.id

    await state.clear()
    await message.answer(f"✅ Шаг #{cur_max + 1} успешно добавлен!")
    await _show_step_card(message, cid, step_id)


@router.message(FollowupAdminStates.waiting_step_ai_instruction)
async def process_step_ai_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    instruction = (message.text or "").strip()
    data = await state.get_data()
    if instruction.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Изменение инструкции отменено.")
        if data.get("editing") and data.get("campaign_id") and data.get("step_id"):
            await _show_step_card(message, data["campaign_id"], data["step_id"])
        elif data.get("campaign_id"):
            await _show_steps_list(message, data["campaign_id"])
        return
    if len(instruction) < 3:
        await message.answer(
            "⚠️ Инструкция слишком короткая. Введите её ещё раз:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_steps_{data.get('campaign_id')}")]]
            ),
        )
        return

    cid = data.get("campaign_id")

    if data.get("editing"):
        sid = data.get("step_id")
        async with async_session_maker() as session:
            step = await session.get(FollowupStep, sid)
            if step:
                step.ai_instruction = instruction
                await session.commit()
        await state.clear()
        await message.answer("✅ Инструкция для ИИ успешно обновлена!")
        if cid and sid:
            await _show_step_card(message, cid, sid)
        return

    delay = data.get("delay_minutes", 60)
    async with async_session_maker() as session:
        cur_max = await session.scalar(
            select(func.max(FollowupStep.step_number)).where(FollowupStep.campaign_id == cid)
        ) or 0
        step = FollowupStep(
            campaign_id=cid,
            step_number=cur_max + 1,
            delay_minutes=delay,
            step_type="ai",
            ai_instruction=instruction,
            message_text="Привет! Напоминаю о нашей практике. Буду рад продолжить!",
        )
        session.add(step)
        await session.commit()
        await session.refresh(step)
        step_id = step.id

    await state.clear()
    await message.answer(f"✅ Шаг с текстом от ИИ №{cur_max + 1} успешно добавлен!")
    await _show_step_card(message, cid, step_id)


async def _show_step_card(event: Message | CallbackQuery, campaign_id: int, step_id: int):
    async with async_session_maker() as session:
        step = await session.get(FollowupStep, step_id)
        if not step:
            msg = "Шаг не найден."
            if isinstance(event, CallbackQuery):
                await event.answer(msg, show_alert=True)
            else:
                await event.answer(msg)
            return

        btn_info = f"«{step.button_text}»" if step.button_text else "не настроена"
        content_preview = step.ai_instruction if step.step_type == "ai" else (step.message_text or "—")

        text = (
            f"🪜 <b>Шаг #{step.step_number}</b>\n\n"
            f"<b>Тип:</b> {'🤖 Персональный текст' if step.step_type == 'ai' else '📝 Готовый текст'}\n"
            f"<b>Задержка:</b> +{step.delay_minutes} мин после предыдущего шага\n"
            f"<b>Кнопка:</b> {html.escape(btn_info)}\n\n"
            f"<b>{'Инструкция для персонального текста' if step.step_type == 'ai' else 'Текст сообщения'}:</b>\n"
            f"{html.escape(content_preview)}"
        )

        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🧪 Отправить мне в чат", callback_data=f"admin_fu_step_test_{campaign_id}_{step.id}"))
        kb.row(InlineKeyboardButton(text="✏️ Изменить задержку", callback_data=f"admin_fu_step_edit_delay_{campaign_id}_{step.id}"))
        kb.row(InlineKeyboardButton(text="✏️ Изменить текст / промпт", callback_data=f"admin_fu_step_edit_text_{campaign_id}_{step.id}"))
        kb.row(InlineKeyboardButton(text="✏️ Изменить кнопку", callback_data=f"admin_fu_step_edit_btn_{campaign_id}_{step.id}"))
        kb.row(InlineKeyboardButton(text="🗑 Удалить шаг", callback_data=f"admin_fu_step_delete_{campaign_id}_{step.id}"))
        kb.row(InlineKeyboardButton(text="⬅️ К списку шагов", callback_data=f"admin_fu_steps_{campaign_id}"))

        if isinstance(event, CallbackQuery) and event.message:
            await event.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
            await event.answer()
        elif isinstance(event, Message):
            await event.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_fu_step_"))
async def callback_admin_fu_step_router(callback: CallbackQuery, state: FSMContext):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    await state.clear()
    parts = callback.data.split("_")

    # admin_fu_step_{cid}_{sid}
    if len(parts) == 5 and parts[2] == "step":
        cid = _safe_int(parts[3], None)
        sid = _safe_int(parts[4], None)
        if cid and sid:
            await state.clear()
            await _show_step_card(callback, cid, sid)
            return

    # admin_fu_step_test_{cid}_{sid} or admin_followup_test_{sid}
    if "test" in parts:
        sid = _safe_int(parts[-1], None)
        if not sid:
            await callback.answer("Неверный ID шага.", show_alert=True)
            return
        async with async_session_maker() as session:
            step = await session.get(FollowupStep, sid)
            if not step:
                await callback.answer("Шаг не найден.", show_alert=True)
                return
            admin_user = await session.get(User, callback.from_user.id)
            if not admin_user:
                admin_user = User(id=callback.from_user.id, first_name=callback.from_user.first_name)
            if callback.bot:
                ok, st, err = await send_followup_step_to_user(
                    callback.bot,
                    session,
                    step=step,
                    user=admin_user,
                    dialogue_id=getattr(admin_user, 'current_dialogue_id', 1) or 1,
                )
                if ok:
                    await callback.answer("✅ Тестовый шаг отправлен вам в чат!", show_alert=True)
                else:
                    await callback.answer(f"❌ Ошибка отправки: {err}", show_alert=True)
            return

    # admin_fu_step_delete_{cid}_{sid}
    if "delete" in parts and "yes" not in parts:
        cid = _safe_int(parts[-2], None)
        sid = _safe_int(parts[-1], None)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="❌ Да, удалить", callback_data=f"admin_fu_step_delete_yes_{cid}_{sid}"),
                    InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_step_{cid}_{sid}"),
                ]
            ]
        )
        if callback.message:
            await callback.message.edit_text("Вы уверены, что хотите удалить этот шаг?", reply_markup=kb)
        await callback.answer()
        return

    # admin_fu_step_delete_yes_{cid}_{sid}
    if "delete" in parts and "yes" in parts:
        cid = _safe_int(parts[-2], None)
        sid = _safe_int(parts[-1], None)
        if cid and sid:
            async with async_session_maker() as session:
                step = await session.get(FollowupStep, sid)
                if step:
                    await session.delete(step)
                    await session.commit()
            await callback.answer("Шаг удален.")
            await callback_admin_fu_steps(callback, state=state)
            return

    # admin_fu_step_edit_delay_{cid}_{sid}
    if "delay" in parts:
        cid = _safe_int(parts[-2], None)
        sid = _safe_int(parts[-1], None)
        await state.set_state(FollowupAdminStates.waiting_step_delay)
        await state.update_data(campaign_id=cid, step_id=sid, editing=True)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_step_{cid}_{sid}")]])
        if callback.message:
            await callback.message.edit_text("Введите новую задержку в минутах:", reply_markup=kb)
        await callback.answer()
        return

    # admin_fu_step_edit_text_{cid}_{sid}
    if "text" in parts:
        cid = _safe_int(parts[-2], None)
        sid = _safe_int(parts[-1], None)
        is_ai = False
        if sid:
            async with async_session_maker() as session:
                step = await session.get(FollowupStep, sid)
                if step and step.step_type == "ai":
                    is_ai = True
        if is_ai:
            await state.set_state(FollowupAdminStates.waiting_step_ai_instruction)
            prompt_text = "Введите новую инструкцию для ИИ:"
        else:
            await state.set_state(FollowupAdminStates.waiting_step_text)
            prompt_text = "Введите новый текст напоминания:"

        await state.update_data(campaign_id=cid, step_id=sid, editing=True, step_type="ai" if is_ai else "static")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_step_{cid}_{sid}")]])
        if callback.message:
            await callback.message.edit_text(prompt_text, reply_markup=kb)
        await callback.answer()
        return

    if "rm" in parts and "btn" in parts:
        cid = _safe_int(parts[-2], None)
        sid = _safe_int(parts[-1], None)
        if cid and sid and "ask" in parts:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin_fu_step_rm_btn_confirm_{cid}_{sid}")],
                    [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_step_{cid}_{sid}")],
                ]
            )
            if callback.message:
                await callback.message.edit_text("Удалить кнопку у этого шага?", reply_markup=kb)
            await callback.answer()
            return
        if cid and sid:
            async with async_session_maker() as session:
                step = await session.get(FollowupStep, sid)
                if step:
                    step.button_text = None
                    step.button_action = None
                    await session.commit()
            await callback.answer("Кнопка удалена.")
            await _show_step_card(callback, cid, sid)
            return

    # admin_fu_step_edit_btn_{cid}_{sid}
    if "btn" in parts:
        cid = _safe_int(parts[-2], None)
        sid = _safe_int(parts[-1], None)
        await state.set_state(FollowupAdminStates.waiting_step_button)
        await state.update_data(campaign_id=cid, step_id=sid)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🧹 Удалить кнопку", callback_data=f"admin_fu_step_rm_btn_ask_{cid}_{sid}")],
                [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_step_{cid}_{sid}")],
            ]
        )
        if callback.message:
            await callback.message.edit_text(
                "Введите текст кнопки и действие через знак <code>|</code>:\n"
                "Например: <code>Продолжить|open_extended_menu</code> или <code>Сайт|https://example.com</code>:",
                reply_markup=kb,
                parse_mode="HTML",
            )
        await callback.answer()
        return

@router.message(FollowupAdminStates.waiting_step_button)
async def process_step_button_input(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    text_val = (message.text or "").strip()
    data = await state.get_data()
    if text_val.lower() in {"отмена", "назад", "/cancel"}:
        await state.clear()
        await message.answer("Изменение кнопки отменено.")
        if data.get("campaign_id") and data.get("step_id"):
            await _show_step_card(message, data["campaign_id"], data["step_id"])
        return
    if "|" not in text_val:
        await message.answer(
            "⚠️ Укажите текст кнопки и ссылку или действие через знак <code>|</code>. "
            "Например: <code>Продолжить|open_extended_menu</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_step_{data.get('campaign_id')}_{data.get('step_id')}")]]
            ),
        )
        return

    b_text, b_action = [s.strip() for s in text_val.split("|", 1)]
    cid = data.get("campaign_id")
    sid = data.get("step_id")

    if sid:
        async with async_session_maker() as session:
            step = await session.get(FollowupStep, sid)
            if step:
                step.button_text = b_text
                step.button_action = b_action
                await session.commit()

    await state.clear()
    await message.answer("✅ Кнопка успешно сохранена!")
    if cid and sid:
        await _show_step_card(message, cid, sid)


# ---------------------------------------------------------------------------
# Delete Campaign
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("admin_fu_delete_ask_"))
async def callback_admin_fu_delete_ask(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    cid = _safe_int(callback.data.split("_")[-1], None)
    if not cid:
        await callback.answer("Неверный ID.", show_alert=True)
        return

    async with async_session_maker() as session:
        campaign = await session.get(FollowupCampaign, cid)
        if not campaign:
            await callback.answer("Кампания не найдена.", show_alert=True)
            return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ Да, удалить навсегда", callback_data=f"admin_fu_delete_yes_{cid}"),
                InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_fu_campaign_{cid}"),
            ]
        ]
    )
    if callback.message:
        await callback.message.edit_text(
            f"⚠️ <b>Удаление цепочки «{html.escape(campaign.name)}»</b>\n\n"
            "Все шаги и история доставок этой цепочки будут безвозвратно удалены. Продолжить?",
            reply_markup=kb,
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_fu_delete_yes_"))
async def callback_admin_fu_delete_yes(callback: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    cid = _safe_int(callback.data.split("_")[-1], None)
    if not cid:
        await callback.answer("Неверный ID.", show_alert=True)
        return

    async with async_session_maker() as session:
        campaign = await session.get(FollowupCampaign, cid)
        if campaign:
            await session.delete(campaign)
            await session.commit()
    await callback.answer("Цепочка успешно удалена.")
    await admin_followup_list(callback)


# ---------------------------------------------------------------------------
# Self-test whole campaign
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("admin_fu_self_test_"))
@router.callback_query(F.data.startswith("admin_followup_test_"))
async def admin_followup_test(callback: CallbackQuery, state: FSMContext | None = None):
    """Send test follow-up step directly to the admin."""
    if not await _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    raw_id = callback.data.split("_")[-1]
    item_id = _safe_int(raw_id, None)
    if not item_id:
        await callback.answer("Неверный идентификатор.", show_alert=True)
        return

    async with async_session_maker() as session:
        # Check if item_id is step or campaign
        step = await session.get(FollowupStep, item_id)
        if not step:
            # Check if campaign
            campaign = await session.scalar(
                select(FollowupCampaign).where(FollowupCampaign.id == item_id).options(selectinload(FollowupCampaign.steps))
            )
            if campaign and campaign.steps:
                step = sorted(campaign.steps, key=lambda s: s.step_number)[0]
            else:
                await callback.answer("Шаг для проверки не найден.", show_alert=True)
                return

        admin_user = await session.get(User, callback.from_user.id)
        if not admin_user:
            admin_user = User(id=callback.from_user.id, first_name=callback.from_user.first_name)

        if callback.bot:
            ok, status, err = await send_followup_step_to_user(
                callback.bot,
                session,
                step=step,
                user=admin_user,
                dialogue_id=getattr(admin_user, 'current_dialogue_id', 1) or 1,
            )
            if ok:
                await callback.answer(f"✅ Тестовый шаг #{step.step_number} отправлен вам в чат!", show_alert=True)
            else:
                await callback.answer(f"❌ Ошибка отправки: {err}", show_alert=True)
        else:
            await callback.answer("Бот недоступен.", show_alert=True)
