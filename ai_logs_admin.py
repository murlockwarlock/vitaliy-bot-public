"""Admin UI for viewing, filtering, inspecting and downloading AI Logs (1:1 with PsychoBot)."""

from __future__ import annotations

import html
import io
import json
import math
import re
import zipfile
from datetime import datetime, timedelta
from typing import Optional

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import func, or_, select

import keyboards as kb
from ai_logs import (
    AILog,
    ai_log_context_label,
    build_ai_attempt_txt_file,
    format_msk,
)
from admin_html_preview import truncate_html_preview
from admin_roles import AdminPermission, user_has_permission
from database import User, async_session_maker


def _safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    if val is None or val == "" or str(val).lower() in ("none", "null"):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


async def is_admin_actor(user_id: int) -> bool:
    return await user_has_permission(user_id, AdminPermission.CLIENTS)


router = Router(name="ai_logs_admin")

AI_LOG_PERIOD_LABELS = {
    "all": "всё время",
    "today": "сегодня",
    "7d": "7 дней",
    "30d": "30 дней",
}

AI_LOG_TYPE_LABELS = {
    "all": "все типы",
    "chat": "обычные запросы",
    "followup": "догоняющие",
}

AI_LOG_STATUS_LABELS = {
    "all": "все статусы",
    "success": "успешные",
    "error": "с ошибками",
}


def _ai_log_period_start(period: str) -> Optional[datetime]:
    now_utc = datetime.utcnow()
    if period == "today":
        now_msk = now_utc + timedelta(hours=3)
        return now_msk.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)
    if period == "7d":
        return now_utc - timedelta(days=7)
    if period == "30d":
        return now_utc - timedelta(days=30)
    return None


def _apply_ai_log_filters(
    query,
    *,
    filter_user_id: Optional[int],
    period: str,
    request_type: str = "all",
    status: str = "all",
):
    if filter_user_id:
        query = query.where(AILog.user_id == filter_user_id)
    period_start = _ai_log_period_start(period)
    if period_start is not None:
        query = query.where(AILog.created_at >= period_start)
    if request_type in AI_LOG_TYPE_LABELS and request_type != "all":
        query = query.where(AILog.request_type == request_type)
    if status == "success":
        query = query.where(or_(AILog.status == "success", AILog.status == None))
    elif status == "error":
        query = query.where(AILog.status == "error")
    return query


async def show_ai_logs_list(
    event: Message | CallbackQuery,
    page: int = 0,
    filter_user_id: Optional[int] = None,
    period: str = "all",
    request_type: str = "all",
    status: str = "all",
):
    PER_PAGE = 8
    period = period if period in AI_LOG_PERIOD_LABELS else "all"
    request_type = request_type if request_type in AI_LOG_TYPE_LABELS else "all"
    status = status if status in AI_LOG_STATUS_LABELS else "all"

    async with async_session_maker() as session:
        query = _apply_ai_log_filters(
            select(AILog),
            filter_user_id=filter_user_id,
            period=period,
            request_type=request_type,
            status=status,
        )

        count_query = select(func.count()).select_from(query.subquery())
        total_count = (await session.execute(count_query)).scalar() or 0
        total_pages = math.ceil(total_count / PER_PAGE) if total_count > 0 else 1
        page = max(0, min(page, total_pages - 1))

        stmt = query.order_by(AILog.created_at.desc()).offset(page * PER_PAGE).limit(PER_PAGE)
        logs = (await session.execute(stmt)).scalars().all()

    if not logs:
        text = "📜 <b>Логи вызовов ИИ пусты.</b>"
        markup = kb.admin_ai_logs_keyboard(
            [],
            page=0,
            total_pages=1,
            filter_user_id=filter_user_id,
            period=period,
            request_type=request_type,
            status=status,
        )
        if getattr(event, "message", None) is not None:
            await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            if hasattr(event, "answer"):
                await event.answer()
        else:
            await event.answer(text, reply_markup=markup, parse_mode="HTML")
        return

    filter_text = f" пользователя <code>ID {filter_user_id}</code>" if filter_user_id else ""
    header = (
        f"📜 <b>Логи вызовов ИИ{filter_text}</b> (Стр. {page + 1}/{total_pages})\n\n"
        f"Период: <b>{AI_LOG_PERIOD_LABELS[period]}</b>\n"
        f"Тип: <b>{AI_LOG_TYPE_LABELS[request_type]}</b>\n"
        f"Статус: <b>{AI_LOG_STATUS_LABELS[status]}</b>\n"
        f"Всего вызовов: <b>{total_count}</b>\nВыберите запись:"
    )

    markup = kb.admin_ai_logs_keyboard(
        logs,
        page=page,
        total_pages=total_pages,
        filter_user_id=filter_user_id,
        period=period,
        request_type=request_type,
        status=status,
    )
    if getattr(event, "message", None) is not None:
        await event.message.edit_text(header, reply_markup=markup, parse_mode="HTML")
        if hasattr(event, "answer"):
            await event.answer()
    else:
        await event.answer(header, reply_markup=markup, parse_mode="HTML")


async def show_ai_log_detail(
    event: Message | CallbackQuery,
    log_id: int,
    page: int = 0,
    filter_user_id: Optional[int] = None,
    period: str = "all",
    request_type: str = "all",
    status: str = "all",
):
    async with async_session_maker() as session:
        log_entry = await session.get(AILog, log_id)
        if not log_entry:
            msg = "Запись лога не найдена."
            if isinstance(event, CallbackQuery) or hasattr(event, "message") or hasattr(event, "data"):
                await event.answer(msg, show_alert=True)
            else:
                await event.answer(msg)
            return

        u = await session.get(User, log_entry.user_id) if log_entry.user_id else None

        if u:
            uname = f"@{u.username}" if getattr(u, "username", None) else (getattr(u, "first_name", None) or getattr(u, "name", None) or "")
            user_info = f"<b>{html.escape(uname)}</b> (ID: {log_entry.user_id})"
        else:
            user_info = f"ID: {log_entry.user_id or 'не указан'}"

        identity_lines = [
            f"📱 <b>Платформа:</b> Telegram",
            f"👤 <b>Пользователь:</b> {user_info}",
            f"🆔 <b>Telegram ID:</b> <code>{log_entry.user_id if log_entry.user_id is not None else 'не указан'}</code>",
        ]
        if u and getattr(u, "name", None):
            identity_lines.append(f"💬 <b>Имя для общения:</b> {html.escape(u.name)}")

    lat_text = f"{log_entry.latency_ms / 1000:.2f} сек" if log_entry.latency_ms else "не измерялось"
    dt_str = format_msk(log_entry.created_at, "%d-%m-%Y %H:%M:%S МСК")
    log_type = getattr(log_entry, "request_type", "chat") or "chat"
    request_preview = log_entry.request_payload or "не зафиксирован"
    prompt_plain = request_preview[:600] + "..." if len(request_preview) > 600 else request_preview
    prompt_str = html.escape(prompt_plain)

    log_status = getattr(log_entry, "status", None) or "success"
    attempt_no = getattr(log_entry, "attempt_no", None) or 1
    attempt_role = getattr(log_entry, "attempt_role", None) or "primary"
    attempt_role_label = "primary" if attempt_role == "primary" else "fallback"
    group_id = getattr(log_entry, "request_group_id", None)
    group_line = f"🔗 <b>Группа запроса:</b> <code>{group_id}</code>\n" if group_id else ""

    if log_status == "error":
        err_type = log_entry.error_type or "UnknownError"
        err_msg = log_entry.error_message or "не указано"
        err_cls = log_entry.error_classification or "не классифицировано"
        raw_resp_preview = log_entry.provider_response_payload or log_entry.raw_response or "не зафиксирован"
        if len(raw_resp_preview) > 1000:
            raw_resp_preview = raw_resp_preview[:1000] + "\n\n[...] (Полный файл скачайте по кнопке ниже)"

        diag_info = ""
        if log_entry.diagnostics_json:
            diag_plain = log_entry.diagnostics_json[:300]
            diag_info = f"🔬 <b>Диагностика:</b>\n<code>{html.escape(diag_plain)}</code>\n\n"

        text = (
            f"❌ <b>Ошибка вызова ИИ #{log_entry.id}</b>\n\n"
            + "\n".join(identity_lines)
            + "\n"
            f"🧾 <b>Тип запроса:</b> {html.escape(AI_LOG_TYPE_LABELS.get(log_type, log_type))}\n"
            f"📍 <b>Контекст:</b> {html.escape(ai_log_context_label(log_entry))}\n"
            f"{group_line}"
            f"🤖 <b>Провайдер:</b> <b>{html.escape(log_entry.provider)}</b> (Попытка {attempt_no} / {attempt_role_label})\n"
            f"🧠 <b>Модель:</b> <code>{html.escape(log_entry.model)}</code>\n"
            f"⏱ <b>Время до ошибки:</b> <code>{lat_text}</code>\n"
            f"📅 <b>Дата вызова:</b> {dt_str}\n\n"
            f"🚨 <b>Ошибка приложения:</b>\n"
            f"<code>{html.escape(err_type)}</code>: <code>{html.escape(err_msg[:300])}</code>\n\n"
            f"🏷 <b>Классификация ошибки:</b>\n"
            f"<code>{html.escape(err_cls)}</code>\n\n"
            f"{diag_info}"
            f"📤 <b>Полный payload запроса (превью):</b>\n<code>{prompt_str}</code>\n\n"
            f"📥 <b>Сырой ответ провайдера (Raw Response / Payload):</b>\n"
            f"<code>{html.escape(raw_resp_preview)}</code>"
        )
    else:
        raw_resp = log_entry.raw_response or ""
        raw_plain = raw_resp[:1200] + "\n\n[...] (Полный сырой файл скачайте по кнопке ниже)" if len(raw_resp) > 1200 else raw_resp
        display_raw = html.escape(raw_plain)

        text = (
            f"📄 <b>Детали лога ИИ #{log_entry.id}</b>\n\n"
            + "\n".join(identity_lines)
            + "\n"
            f"🧾 <b>Тип запроса:</b> {html.escape(AI_LOG_TYPE_LABELS.get(log_type, log_type))}\n"
            f"📍 <b>Контекст:</b> {html.escape(ai_log_context_label(log_entry))}\n"
            f"{group_line}"
            f"🤖 <b>Провайдер:</b> <b>{html.escape(log_entry.provider)}</b> (Попытка {attempt_no} / {attempt_role_label})\n"
            f"🧠 <b>Модель:</b> <code>{html.escape(log_entry.model)}</code>\n"
            f"⏱ <b>Время ответа:</b> <code>{lat_text}</code>\n"
            f"📅 <b>Дата вызова:</b> {dt_str}\n\n"
            f"📤 <b>Полный payload запроса (превью):</b>\n<code>{prompt_str}</code>\n\n"
            f"📥 <b>Сырой ответ модели (Raw LLM Output):</b>\n"
            f"<code>{display_raw}</code>"
        )

    if len(text) > 3800:
        text = truncate_html_preview(
            text,
            max_length=3800,
            suffix="\n\n[...] (Полный текст урезан под лимит Telegram. Скачайте файл ниже)",
        )

    markup = kb.admin_ai_log_detail_keyboard(
        log_id,
        page=page,
        filter_user_id=filter_user_id,
        period=period,
        request_type=request_type,
        status=status,
    )
    if getattr(event, "message", None) is not None:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        if hasattr(event, "answer"):
            await event.answer()
    else:
        await event.answer(text, reply_markup=markup, parse_mode="HTML")


async def send_ai_log_file(callback: CallbackQuery, log_id: int):
    async with async_session_maker() as session:
        log_entry = await session.get(AILog, log_id)
        if not log_entry:
            await callback.answer("Запись не найдена.", show_alert=True)
            return

    content = build_ai_attempt_txt_file(log_entry)
    file_bytes = content.encode("utf-8")
    safe_provider = re.sub(r"[^A-Za-z0-9_.-]+", "_", log_entry.provider or "provider")
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", log_entry.model or "model")
    filename = f"ai_log_{log_id}_{safe_provider}_{safe_model}.txt"
    input_file = BufferedInputFile(file_bytes, filename=filename)
    await callback.message.answer_document(document=input_file, caption=f"📄 Полный сырой лог ИИ #{log_id}")
    await callback.answer()

@router.callback_query(F.data.startswith("admin_ai_logs_"))
async def callback_admin_ai_logs(callback: CallbackQuery):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    parts = callback.data.split("_")
    page = _safe_int(parts[3] if len(parts) > 3 else None, 0) or 0
    period = parts[4] if len(parts) > 4 and parts[4] in AI_LOG_PERIOD_LABELS else "all"
    request_type = parts[5] if len(parts) > 5 and parts[5] in AI_LOG_TYPE_LABELS else "all"
    status = parts[6] if len(parts) > 6 and parts[6] in AI_LOG_STATUS_LABELS else "all"
    await show_ai_logs_list(callback, page=page, period=period, request_type=request_type, status=status)


@router.callback_query(F.data.startswith("admin_user_ai_logs_"))
async def callback_admin_user_ai_logs(callback: CallbackQuery):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    parts = callback.data.split("_")
    user_id = _safe_int(parts[4] if len(parts) > 4 else None, None)
    if not user_id:
        await callback.answer("Неверный ID пользователя.", show_alert=True)
        return
    page = _safe_int(parts[5] if len(parts) > 5 else None, 0) or 0
    period = parts[6] if len(parts) > 6 and parts[6] in AI_LOG_PERIOD_LABELS else "all"
    request_type = parts[7] if len(parts) > 7 and parts[7] in AI_LOG_TYPE_LABELS else "all"
    status = parts[8] if len(parts) > 8 and parts[8] in AI_LOG_STATUS_LABELS else "all"
    await show_ai_logs_list(
        callback,
        page=page,
        filter_user_id=user_id,
        period=period,
        request_type=request_type,
        status=status,
    )


@router.callback_query(F.data.startswith("admin_ai_log_file_"))
async def callback_admin_ai_log_file_explicit(callback: CallbackQuery):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    raw_id = callback.data.rsplit("_", 1)[-1]
    log_id = _safe_int(raw_id, None)
    if not log_id:
        await callback.answer("Неверный ID лога.", show_alert=True)
        return
    await send_ai_log_file(callback, log_id)


@router.callback_query(F.data.startswith("admin_ai_log_"))
async def callback_admin_ai_log_detail(callback: CallbackQuery):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    parts = callback.data.split("_")
    if len(parts) > 3 and parts[3] == "file":
        log_id = _safe_int(parts[4] if len(parts) > 4 else None, None)
        if not log_id:
            await callback.answer("Неверный ID лога.", show_alert=True)
            return
        await send_ai_log_file(callback, log_id)
        return
    log_id = _safe_int(parts[3] if len(parts) > 3 else None, None)
    if not log_id:
        await callback.answer("Неверный ID лога.", show_alert=True)
        return
    page = _safe_int(parts[4] if len(parts) > 4 else None, 0) or 0
    raw_filter = parts[5] if len(parts) > 5 else None
    filter_user_id = _safe_int(raw_filter, None) if raw_filter not in (None, "", "0", "None") else None
    period = parts[6] if len(parts) > 6 and parts[6] in AI_LOG_PERIOD_LABELS else "all"
    request_type = parts[7] if len(parts) > 7 and parts[7] in AI_LOG_TYPE_LABELS else "all"
    status = parts[8] if len(parts) > 8 and parts[8] in AI_LOG_STATUS_LABELS else "all"
    await show_ai_log_detail(
        callback,
        log_id,
        page=page,
        filter_user_id=filter_user_id,
        period=period,
        request_type=request_type,
        status=status,
    )


@router.callback_query(F.data.startswith("export_ai_logs_"))
async def export_ai_logs_package(callback: CallbackQuery):
    if not await is_admin_actor(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    parts = callback.data.split("_")
    raw_filter = parts[3] if len(parts) > 3 else None
    filter_user_id = _safe_int(raw_filter, None) if raw_filter not in (None, "", "0", "None") else None
    period = parts[4] if len(parts) > 4 and parts[4] in AI_LOG_PERIOD_LABELS else "all"
    request_type = parts[5] if len(parts) > 5 and parts[5] in AI_LOG_TYPE_LABELS else "all"
    status = parts[6] if len(parts) > 6 and parts[6] in AI_LOG_STATUS_LABELS else "all"
    async with async_session_maker() as session:
        stmt = _apply_ai_log_filters(
            select(AILog).order_by(AILog.created_at.desc()),
            filter_user_id=filter_user_id,
            period=period,
            request_type=request_type,
            status=status,
        )
        logs = (await session.execute(stmt)).scalars().all()
    if not logs:
        await callback.answer("По выбранному фильтру логов нет.", show_alert=True)
        return

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest = []
        for log_entry in logs:
            safe_provider = re.sub(r"[^A-Za-z0-9_.-]+", "_", log_entry.provider or "provider")
            filename = f"ai_log_{log_entry.id}_{safe_provider}.txt"
            archive.writestr(filename, build_ai_attempt_txt_file(log_entry))
            manifest_entry = {
                "id": log_entry.id,
                "status": getattr(log_entry, "status", None) or "success",
                "request_group_id": getattr(log_entry, "request_group_id", None),
                "attempt_no": getattr(log_entry, "attempt_no", None) or 1,
                "attempt_role": getattr(log_entry, "attempt_role", None) or "primary",
                "created_at": log_entry.created_at.isoformat() if log_entry.created_at else None,
                "user_id": log_entry.user_id,
                "platform": "telegram",
                "provider": log_entry.provider,
                "model": log_entry.model,
                "latency_ms": log_entry.latency_ms,
                "error_type": getattr(log_entry, "error_type", None),
                "error_classification": getattr(log_entry, "error_classification", None),
                "http_status": getattr(log_entry, "http_status", None),
                "file": filename,
            }
            try:
                diagnostics = json.loads(getattr(log_entry, "diagnostics_json", None) or "{}")
            except (TypeError, ValueError):
                diagnostics = {}
            if isinstance(diagnostics, dict):
                manifest_entry["provider_code"] = diagnostics.get("provider_code")
                manifest_entry["provider_message"] = diagnostics.get("provider_message")
            manifest.append(manifest_entry)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    archive_buffer.seek(0)
    user_suffix = f"_user_{filter_user_id}" if filter_user_id else ""
    zip_filename = f"ai_logs_{period}{user_suffix}_{datetime.utcnow():%Y%m%d_%H%M%S}.zip"
    input_file = BufferedInputFile(archive_buffer.getvalue(), filename=zip_filename)
    await callback.message.answer_document(
        document=input_file,
        caption=f"📦 Логи ИИ: {len(logs)} шт., период — {AI_LOG_PERIOD_LABELS[period]}",
    )
    await callback.answer()
