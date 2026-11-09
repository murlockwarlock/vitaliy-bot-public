import io
import json
import uuid
import zipfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from ai_logs import (
    AILog,
    apply_ai_log_context,
    ai_log_context_label,
    build_ai_attempt_txt_file,
    format_msk,
    record_ai_attempt_log,
)
import keyboards as kb
from ai_logs_admin import (
    callback_admin_ai_logs,
    callback_admin_ai_log_detail,
    callback_admin_ai_log_file_explicit,
    export_ai_logs_package,
    show_ai_logs_list,
    show_ai_log_detail,
    send_ai_log_file,
)
from database import Base, User, AIConfig, async_session_maker, engine, init_db
from error_reporting import sanitize_secret_values


async def _ensure_user(session, user_id: int, **kwargs):
    u = await session.get(User, user_id)
    if u is None:
        u = User(id=user_id, **kwargs)
        session.add(u)
        await session.commit()
    return u


@pytest.mark.asyncio
async def test_ai_logs_ux_inventory_and_labels_match_psychobot():
    """Verify that all UX labels, titles, buttons, pagination and formats are 1:1 with PsychoBot."""
    # 1. Admin panel / AI settings button
    ai_settings_markup = kb.ai_settings_keyboard("DeepSeek")
    all_buttons = [btn.text for row in ai_settings_markup.inline_keyboard for btn in row]
    assert "📜 Логи запросов ИИ" in all_buttons, "AI Logs button text must match PsychoBot exactly"
    ai_logs_btn = next(btn for row in ai_settings_markup.inline_keyboard for btn in row if btn.text == "📜 Логи запросов ИИ")
    assert ai_logs_btn.callback_data == "admin_ai_logs_0_all", "Callback data must match PsychoBot exactly"

    # 2. Format button for log entry
    mock_log_success = MagicMock(
        id=1,
        status="success",
        attempt_role="primary",
        request_type="chat",
        created_at=datetime(2026, 10, 5, 12, 0, 0),
        provider="OpenAI",
        model="gpt-4o",
        latency_ms=1200,
    )
    btn_text = kb.format_ai_log_button(mock_log_success)
    assert btn_text.startswith("✅ "), "Success status marker must be '✅ '"
    assert "OpenAI: gpt-4o" in btn_text
    assert "1.2s" in btn_text

    mock_log_fallback = MagicMock(
        id=2,
        status="error",
        attempt_role="fallback",
        request_type="followup",
        created_at=datetime(2026, 10, 5, 12, 5, 0),
        provider="DeepSeek",
        model="deepseek-chat",
        latency_ms=500,
    )
    btn_fallback_text = kb.format_ai_log_button(mock_log_fallback)
    assert btn_fallback_text.startswith("↪️ ❌ [F] "), "Fallback error followup marker must be '↪️ ❌ [F] '"

    # 3. Two-row stable pagination
    page_rows = kb.fixed_pagination_rows(1, 3, lambda p: f"page_{p}")
    assert len(page_rows) == 2, "Pagination must be exactly 2 rows"
    row1 = [b.text for b in page_rows[0]]
    assert row1 == ["⬅️ Назад", "2/3", "Вперёд ➡️"], "Row 1 must be [⬅️ Назад, X/Y, Вперёд ➡️]"
    row2 = [b.text for b in page_rows[1]]
    assert row2 == ["⏮ В начало", "В конец ⏭"], "Row 2 must be [⏮ В начало, В конец ⏭]"

    # 4. Detail keyboard buttons
    detail_markup = kb.admin_ai_log_detail_keyboard(1, page=0)
    detail_buttons = [b.text for row in detail_markup.inline_keyboard for b in row]
    assert "📄 Скачать .txt файл лога" in detail_buttons, "Download button must be '📄 Скачать .txt файл лога'"
    assert "⬅️ Назад к логам" in detail_buttons, "Back button must be '⬅️ Назад к логам'"

    # 5. List keyboard controls
    list_markup = kb.admin_ai_logs_keyboard([mock_log_success], page=0, total_pages=1)
    list_buttons = [b.text for row in list_markup.inline_keyboard for b in row]
    assert "📦 Скачать пакет логов" in list_buttons, "Package export button must be '📦 Скачать пакет логов'"
    assert "⬅️ Назад" in list_buttons, "Back to settings button must be '⬅️ Назад'"


from aiogram.types import CallbackQuery, Message
from handlers import cmd_admin, admin_ai_settings, admin_panel_cb


@pytest.mark.asyncio
async def test_ai_logs_e2e_saving_and_admin_view_walkthrough():
    """E2E flow: ADMIN -> /admin -> 🤖 Настройки ИИ -> 📜 Логи запросов ИИ -> list -> pagination -> filter -> open log -> download TXT -> ⬅️ К списку логов -> ⬅️ В админку."""
    await init_db()

    admin_id = 999001
    async with async_session_maker() as session:
        await _ensure_user(session, 777001, username="test_client", name="Алексей", is_admin=False)
        await _ensure_user(session, admin_id, username="admin_actor", name="Админ", is_admin=True)

        # Record multiple AI requests (12 logs total) to activate and test pagination
        log_ids = []
        for i in range(12):
            lid = await record_ai_attempt_log(
                session=session,
                user_id=777001,
                dialogue_id=1,
                topic_slug="stress",
                request_type="chat",
                provider="OpenAI",
                model="gpt-4o",
                status="success",
                latency_ms=1000 + i * 50,
                prompt_summary=f"Как справиться с тревогой? Запрос #{i+1}",
                request_capture={"messages": [{"role": "user", "content": f"Как справиться с тревогой? Запрос #{i+1}"}]},
                raw_response=f"Сделай глубокий вдох #{i+1}.",
                clean_text=f"Сделай глубокий вдох #{i+1}.",
                request_group_id=f"grp-e2e-{i:03d}",
                attempt_no=1,
                attempt_role="primary",
            )
            log_ids.append(lid)
        await session.commit()

    target_log_id = log_ids[-1]

    def _make_mock_cb(user_id, data=""):
        cb = MagicMock()
        cb.from_user.id = user_id
        cb.data = data
        cb.bot = AsyncMock()
        cb.message.chat.id = user_id
        cb.message.content_type = "text"
        cb.message.edit_text = AsyncMock()
        cb.message.answer_document = AsyncMock()
        cb.answer = AsyncMock()
        return cb

    # --- Step 1: Admin sends /admin command ---
    msg_admin = MagicMock()
    msg_admin.from_user.id = admin_id
    msg_admin.answer = AsyncMock()
    mock_state = AsyncMock()

    await cmd_admin(msg_admin, mock_state)
    msg_admin.answer.assert_called_once()
    assert "Админ-панель" in msg_admin.answer.call_args[0][0]
    admin_markup = msg_admin.answer.call_args.kwargs.get("reply_markup") or msg_admin.answer.call_args[1].get("reply_markup")
    admin_btn_cbs = [b.callback_data for row in admin_markup.inline_keyboard for b in row]
    assert "admin_ai_settings" in admin_btn_cbs, "Admin panel must have button to AI settings"

    # --- Step 2: Admin clicks '🤖 Настройки ИИ' ---
    cb_ai_settings = _make_mock_cb(admin_id, "admin_ai_settings")
    await admin_ai_settings(cb_ai_settings)
    ai_settings_call = cb_ai_settings.bot.send_message if cb_ai_settings.bot.send_message.called else cb_ai_settings.message.edit_text
    ai_settings_call.assert_called_once()
    ai_settings_text = ai_settings_call.call_args[0][1] if cb_ai_settings.bot.send_message.called else ai_settings_call.call_args[0][0]
    assert "Настройки ИИ" in ai_settings_text
    ai_markup = ai_settings_call.call_args.kwargs.get("reply_markup") or ai_settings_call.call_args[1].get("reply_markup")
    ai_btn_cbs = [b.callback_data for row in ai_markup.inline_keyboard for b in row]
    assert "admin_ai_logs_0_all" in ai_btn_cbs, "AI settings must have button to AI logs"

    # --- Step 3: Admin clicks '📜 Логи запросов ИИ' (Page 0) ---
    cb_list_p0 = _make_mock_cb(admin_id, "admin_ai_logs_0_all")
    await callback_admin_ai_logs(cb_list_p0)
    cb_list_p0.message.edit_text.assert_called_once()
    list_text_p0 = cb_list_p0.message.edit_text.call_args[0][0]
    assert "📜 <b>Логи вызовов ИИ</b>" in list_text_p0
    assert "Всего вызовов:" in list_text_p0
    markup_p0 = cb_list_p0.message.edit_text.call_args.kwargs.get("reply_markup")
    p0_buttons = [b.text for row in markup_p0.inline_keyboard for b in row]
    assert any("1/" in b for b in p0_buttons), "Must show page 1 indicator"
    assert "Вперёд ➡️" in p0_buttons, "Must show forward pagination button"

    # --- Step 4: Admin clicks 'Вперёд ➡️' (Pagination to Page 1) ---
    cb_list_p1 = _make_mock_cb(admin_id, "admin_ai_logs_1_all")
    await callback_admin_ai_logs(cb_list_p1)
    cb_list_p1.message.edit_text.assert_called_once()
    markup_p1 = cb_list_p1.message.edit_text.call_args.kwargs.get("reply_markup")
    p1_buttons = [b.text for row in markup_p1.inline_keyboard for b in row]
    assert any("2/" in b for b in p1_buttons), "Must show page 2 indicator"
    assert "⬅️ Назад" in p1_buttons

    # --- Step 5: Admin switches filters (period 7d, chat, success) ---
    cb_filter = _make_mock_cb(admin_id, "admin_ai_logs_0_7d_chat_success")
    await callback_admin_ai_logs(cb_filter)
    cb_filter.message.edit_text.assert_called_once()
    filtered_text = cb_filter.message.edit_text.call_args[0][0]
    assert "7 дней" in filtered_text
    assert "обычные запросы" in filtered_text
    assert "успешные" in filtered_text

    # --- Step 6: Admin opens detail card for target log ---
    cb_detail = _make_mock_cb(admin_id, f"admin_ai_log_{target_log_id}_0_0_all_all_all")
    await callback_admin_ai_log_detail(cb_detail)
    cb_detail.message.edit_text.assert_called_once()
    card_text = cb_detail.message.edit_text.call_args[0][0]
    assert f"📄 <b>Детали лога ИИ #{target_log_id}</b>" in card_text
    assert "777001" in card_text
    assert "@test_client" in card_text
    assert "OpenAI" in card_text
    assert "gpt-4o" in card_text
    assert f"grp-e2e-011" in card_text

    card_markup = cb_detail.message.edit_text.call_args.kwargs.get("reply_markup")
    card_buttons = {b.text: b.callback_data for row in card_markup.inline_keyboard for b in row}
    assert "📄 Скачать .txt файл лога" in card_buttons
    assert "⬅️ Назад к логам" in card_buttons

    # --- Step 7: Admin clicks '📄 Скачать .txt файл лога' ---
    cb_file = _make_mock_cb(admin_id, f"admin_ai_log_file_{target_log_id}")
    await callback_admin_ai_log_file_explicit(cb_file)
    cb_file.message.answer_document.assert_called_once()
    doc_call = cb_file.message.answer_document.call_args
    input_file = doc_call.kwargs["document"]
    assert input_file.filename == f"ai_log_{target_log_id}_OpenAI_gpt-4o.txt"
    file_bytes = input_file.data
    file_text = file_bytes.decode("utf-8")
    assert f"AI LOG RECORD #{target_log_id}" in file_text
    assert f"Request Group: grp-e2e-011" in file_text
    assert "Attempt: 1 (PRIMARY)" in file_text
    assert "Provider: OpenAI" in file_text
    assert "Model: gpt-4o" in file_text
    assert "Сделай глубокий вдох #12." in file_text

    # --- Step 8: Admin clicks '⬅️ Назад к логам' ---
    back_to_logs_cb = card_buttons["⬅️ Назад к логам"]
    cb_back_logs = _make_mock_cb(admin_id, back_to_logs_cb)
    await callback_admin_ai_logs(cb_back_logs)
    cb_back_logs.message.edit_text.assert_called_once()
    assert "📜 <b>Логи вызовов ИИ</b>" in cb_back_logs.message.edit_text.call_args[0][0]
    back_logs_markup = cb_back_logs.message.edit_text.call_args.kwargs.get("reply_markup")
    logs_back_btn = back_logs_markup.inline_keyboard[-1][0]
    assert logs_back_btn.text == "⬅️ Назад"
    assert logs_back_btn.callback_data == "admin_ai_settings"

    # --- Step 9: Admin clicks '⬅️ Назад' -> back to AI Settings ---
    cb_back_ai = _make_mock_cb(admin_id, "admin_ai_settings")
    await admin_ai_settings(cb_back_ai)
    ai_back_call = cb_back_ai.bot.send_message if cb_back_ai.bot.send_message.called else cb_back_ai.message.edit_text
    ai_back_call.assert_called_once()
    ai_back_text = ai_back_call.call_args[0][1] if cb_back_ai.bot.send_message.called else ai_back_call.call_args[0][0]
    assert "Настройки ИИ" in ai_back_text
    back_ai_markup = ai_back_call.call_args.kwargs.get("reply_markup")
    ai_back_btn = next(b for row in back_ai_markup.inline_keyboard for b in row if b.text == "⬅️ Назад")
    assert ai_back_btn.callback_data == "admin_panel"

    # --- Step 10: Admin clicks '⬅️ Назад' -> back to Admin Panel ---
    cb_back_panel = _make_mock_cb(admin_id, "admin_panel")
    await admin_panel_cb(cb_back_panel)
    panel_call = cb_back_panel.bot.send_message if cb_back_panel.bot.send_message.called else cb_back_panel.message.edit_text
    panel_call.assert_called_once()
    panel_text = panel_call.call_args[0][1] if cb_back_panel.bot.send_message.called else panel_call.call_args[0][0]
    assert "Админ-панель" in panel_text


@pytest.mark.asyncio
async def test_ai_logs_e2e_fallback_and_request_group_grouping():
    """Verify fallback sequence: Primary fails -> Fallback succeeds under same request_group_id -> ZIP export 1:1."""
    await init_db()

    group_id = f"grp-fallback-{uuid.uuid4().hex[:8]}"
    admin_id = 999001
    async with async_session_maker() as session:
        await _ensure_user(session, 888001, username="fallback_user", is_admin=False)
        await _ensure_user(session, admin_id, username="admin_actor", is_admin=True)

        # Primary attempt fails
        id_primary = await record_ai_attempt_log(
            session=session,
            user_id=888001,
            dialogue_id=2,
            topic_slug="hypnosis",
            request_type="chat",
            provider="OpenAI",
            model="gpt-4o",
            status="error",
            latency_ms=2100,
            prompt_summary="Сессия релаксации",
            error_type="RateLimitError",
            error_message="Rate limit reached for gpt-4o (429)",
            error_classification="rate_limit",
            http_status=429,
            diagnostics={"provider_code": "rate_limit_exceeded", "provider_message": "Requests exceeded quota"},
            request_group_id=group_id,
            attempt_no=1,
            attempt_role="primary",
        )

        # Fallback attempt succeeds
        id_fallback = await record_ai_attempt_log(
            session=session,
            user_id=888001,
            dialogue_id=2,
            topic_slug="hypnosis",
            request_type="chat",
            provider="DeepSeek",
            model="deepseek-chat",
            status="success",
            latency_ms=1100,
            prompt_summary="Сессия релаксации",
            raw_response="Добро пожаловать в практику.",
            clean_text="Добро пожаловать в практику.",
            request_group_id=group_id,
            attempt_no=2,
            attempt_role="fallback",
        )
        await session.commit()

    # 1. Admin views list: both attempts are visible with markers
    cb_list = MagicMock()
    cb_list.from_user.id = admin_id
    cb_list.data = "admin_ai_logs_0_all"
    cb_list.message.edit_text = AsyncMock()
    cb_list.answer = AsyncMock()

    await callback_admin_ai_logs(cb_list)
    list_markup = cb_list.message.edit_text.call_args.kwargs["reply_markup"]
    list_buttons = [b.text for row in list_markup.inline_keyboard for b in row]
    # Check that export button is exact 1:1
    assert "📦 Скачать пакет логов" in list_buttons

    # 2. Admin checks error card of primary attempt
    cb_err = MagicMock()
    cb_err.from_user.id = admin_id
    cb_err.data = f"admin_ai_log_{id_primary}_0_0_all_all_all"
    cb_err.message.edit_text = AsyncMock()
    cb_err.answer = AsyncMock()

    await callback_admin_ai_log_detail(cb_err)
    err_text = cb_err.message.edit_text.call_args[0][0]
    assert f"❌ <b>Ошибка вызова ИИ #{id_primary}</b>" in err_text
    assert "RateLimitError" in err_text
    assert f"<code>{group_id}</code>" in err_text
    assert "Попытка 1 / primary" in err_text

    # Admin downloads primary TXT
    cb_err_txt = MagicMock()
    cb_err_txt.from_user.id = admin_id
    cb_err_txt.data = f"admin_ai_log_file_{id_primary}"
    cb_err_txt.message.answer_document = AsyncMock()
    cb_err_txt.answer = AsyncMock()

    await callback_admin_ai_log_file_explicit(cb_err_txt)
    prim_doc = cb_err_txt.message.answer_document.call_args.kwargs["document"]
    prim_txt = prim_doc.data.decode("utf-8")
    assert "RateLimitError" in prim_txt
    assert f"Request Group: {group_id}" in prim_txt
    assert "Attempt: 1 (PRIMARY)" in prim_txt

    # 3. Admin checks success card of fallback attempt
    cb_fb = MagicMock()
    cb_fb.from_user.id = admin_id
    cb_fb.data = f"admin_ai_log_{id_fallback}_0_0_all_all_all"
    cb_fb.message.edit_text = AsyncMock()
    cb_fb.answer = AsyncMock()

    await callback_admin_ai_log_detail(cb_fb)
    fb_text = cb_fb.message.edit_text.call_args[0][0]
    assert f"📄 <b>Детали лога ИИ #{id_fallback}</b>" in fb_text
    assert "DeepSeek" in fb_text
    assert f"<code>{group_id}</code>" in fb_text
    assert "Попытка 2 / fallback" in fb_text

    # Admin downloads fallback TXT
    cb_fb_txt = MagicMock()
    cb_fb_txt.from_user.id = admin_id
    cb_fb_txt.data = f"admin_ai_log_file_{id_fallback}"
    cb_fb_txt.message.answer_document = AsyncMock()
    cb_fb_txt.answer = AsyncMock()

    await callback_admin_ai_log_file_explicit(cb_fb_txt)
    fb_doc = cb_fb_txt.message.answer_document.call_args.kwargs["document"]
    fb_txt = fb_doc.data.decode("utf-8")
    assert "Добро пожаловать в практику." in fb_txt
    assert f"Request Group: {group_id}" in fb_txt
    assert "Attempt: 2 (FALLBACK)" in fb_txt

    # 4. Admin exports package zip (all logs)
    cb_export = MagicMock()
    cb_export.from_user.id = admin_id
    cb_export.data = "export_ai_logs_0_all_all_all"
    cb_export.message.answer_document = AsyncMock()
    cb_export.answer = AsyncMock()

    await export_ai_logs_package(cb_export)
    cb_export.message.answer_document.assert_called_once()
    zip_doc = cb_export.message.answer_document.call_args.kwargs["document"]
    assert zip_doc.filename.startswith("ai_logs_all_") and zip_doc.filename.endswith(".zip")
    zip_bytes = zip_doc.data
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        # Check sorting: order_by created_at desc
        assert len(manifest) >= 2
        for idx in range(len(manifest) - 1):
            assert manifest[idx]["created_at"] >= manifest[idx + 1]["created_at"]

        group_entries = [m for m in manifest if m.get("request_group_id") == group_id]
        assert len(group_entries) == 2, "Manifest must contain both primary and fallback attempts for the group"
        roles = {e["attempt_role"] for e in group_entries}
        assert roles == {"primary", "fallback"}

        # Check full manifest structure matching PsychoBot
        for entry in group_entries:
            assert entry["file"] in names
            assert "id" in entry
            assert "status" in entry
            assert "attempt_no" in entry
            assert "attempt_role" in entry
            assert "created_at" in entry
            assert "user_id" in entry
            assert "platform" in entry
            assert "provider" in entry
            assert "model" in entry
            assert "latency_ms" in entry
            file_content = zf.read(entry["file"]).decode("utf-8")
            assert f"AI LOG RECORD #{entry['id']}" in file_content

        # Verify diagnostics in manifest
        err_entry = next(e for e in group_entries if e["id"] == id_primary)
        assert err_entry["provider_code"] == "rate_limit_exceeded"
        assert err_entry["provider_message"] == "Requests exceeded quota"

    # 5. Export with filter applied: status=error only
    cb_export_err = MagicMock()
    cb_export_err.from_user.id = admin_id
    cb_export_err.data = "export_ai_logs_0_all_all_error"
    cb_export_err.message.answer_document = AsyncMock()
    cb_export_err.answer = AsyncMock()

    await export_ai_logs_package(cb_export_err)
    cb_export_err.message.answer_document.assert_called_once()
    filtered_zip_doc = cb_export_err.message.answer_document.call_args.kwargs["document"]
    with zipfile.ZipFile(io.BytesIO(filtered_zip_doc.data)) as zf_err:
        err_manifest = json.loads(zf_err.read("manifest.json").decode("utf-8"))
        for item in err_manifest:
            assert item["status"] == "error", "Filtered zip must contain only error attempts"
        err_group_entries = [m for m in err_manifest if m.get("request_group_id") == group_id]
        assert len(err_group_entries) == 1
        assert err_group_entries[0]["id"] == id_primary



@pytest.mark.asyncio
async def test_ai_logs_security_rejection_for_non_admins():
    """Verify that all AI logs admin callbacks reject non-admin users with show_alert."""
    await init_db()

    non_admin_id = 123456789
    async with async_session_maker() as session:
        await _ensure_user(session, non_admin_id, username="intruder", is_admin=False)

    callbacks_to_test = [
        ("admin_ai_logs_0_all", callback_admin_ai_logs),
        ("admin_user_ai_logs_123_0_all", callback_admin_ai_logs),
        ("admin_ai_log_1_0_0_all_all_all", callback_admin_ai_log_detail),
        ("admin_ai_log_file_1", callback_admin_ai_log_file_explicit),
        ("export_ai_logs_0_all_all_all", export_ai_logs_package),
    ]

    for cb_data, handler in callbacks_to_test:
        cb = MagicMock()
        cb.from_user.id = non_admin_id
        cb.data = cb_data
        cb.answer = AsyncMock()
        cb.message.edit_text = AsyncMock()
        cb.message.answer_document = AsyncMock()

        await handler(cb)
        cb.answer.assert_called_once_with("⛔️ Доступ запрещён.", show_alert=True)
        cb.message.edit_text.assert_not_called()
        cb.message.answer_document.assert_not_called()


@pytest.mark.asyncio
async def test_ai_logs_large_payload_truncation_and_secret_sanitization():
    """Verify that 10k payloads do not break Telegram message limits and secrets are sanitized."""
    await init_db()

    admin_id = 999001
    async with async_session_maker() as session:
        await _ensure_user(session, admin_id, username="admin_actor", is_admin=True)

        huge_prompt = "Анализ сессии: " + ("Очень длинный текст " * 500)
        secret_leak = "sk-proj-1234567890abcdef1234567890abcdef1234 secret password"
        huge_response = "Ответ: " + secret_leak + (" " + "Слова практики " * 600)

        log_id = await record_ai_attempt_log(
            session=session,
            user_id=admin_id,
            request_type="chat",
            provider="OpenAI",
            model="gpt-4o",
            status="success",
            prompt_summary="Большой запрос",
            request_capture={"content": huge_prompt},
            raw_response=huge_response,
            clean_text="Ответ пользователю",
        )
        await session.commit()

    cb = MagicMock()
    cb.from_user.id = admin_id
    cb.data = f"admin_ai_log_{log_id}_0_0_all_all_all"
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await callback_admin_ai_log_detail(cb)
    rendered_text = cb.message.edit_text.call_args[0][0]

    # Check Telegram limit compliance
    assert len(rendered_text) < 4096, "Rendered Telegram message must strictly stay under 4096 characters"
    assert "[...] (Полный сырой файл скачайте по кнопке ниже)" in rendered_text

    # Check secret sanitization
    assert "sk-proj" not in rendered_text, "Secret key must be sanitized"
    assert "[REDACTED_API_KEY]" in rendered_text or "[REDACTED" in rendered_text

    # File download contains the full sanitized payload
    cb_f = MagicMock()
    cb_f.from_user.id = admin_id
    cb_f.data = f"admin_ai_log_file_{log_id}"
    cb_f.message.answer_document = AsyncMock()
    cb_f.answer = AsyncMock()

    await callback_admin_ai_log_file_explicit(cb_f)
    doc = cb_f.message.answer_document.call_args.kwargs["document"]
    txt_content = doc.data.decode("utf-8")
    assert "sk-proj" not in txt_content, "Secret key must be sanitized in downloaded file"
    assert len(txt_content) > 5000, "Full payload must be present in downloaded file"


@pytest.mark.asyncio
async def test_ai_logs_deleted_user_and_stale_log_resilience():
    """Verify that deleting the user does not crash log display, and stale log id returns clean alert."""
    await init_db()

    admin_id = 999001
    async with async_session_maker() as session:
        await _ensure_user(session, admin_id, username="admin_actor", is_admin=True)
        # Transient user
        temp_user = await _ensure_user(session, 555001, username="temp_user", name="Временный", is_admin=False)

        log_id = await record_ai_attempt_log(
            session=session,
            user_id=555001,
            request_type="chat",
            provider="KIE",
            model="gemini-3-flash",
            status="success",
            clean_text="Сообщение",
            raw_response="Сообщение",
        )

        # Delete the user from DB
        await session.delete(temp_user)
        await session.commit()

    # Detail view with deleted user
    cb = MagicMock()
    cb.from_user.id = admin_id
    cb.data = f"admin_ai_log_{log_id}_0_0_all_all_all"
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await callback_admin_ai_log_detail(cb)
    card_text = cb.message.edit_text.call_args[0][0]
    assert "555001" in card_text, "User ID should still be visible even after user deletion"

    # Nonexistent log ID (stale button)
    cb_stale = MagicMock()
    cb_stale.from_user.id = admin_id
    cb_stale.data = "admin_ai_log_999999_0_0_all_all_all"
    cb_stale.answer = AsyncMock()

    await callback_admin_ai_log_detail(cb_stale)
    cb_stale.answer.assert_called_once_with("Запись лога не найдена.", show_alert=True)


@pytest.mark.asyncio
async def test_ai_runtime_automation_stripping_and_structured_logging():
    """Verify production get_ai_response_for_topic strips <DATA> blocks, dispatches automation events, and logs structured metadata."""
    from ai_integration import StructuredAIResponse, get_ai_response_for_topic
    from automation_engine import AutomationConversationState, AutomationHandler
    from database import AILog
    from sqlalchemy import select

    await init_db()
    test_user_id = 888123

    async with async_session_maker() as session:
        await _ensure_user(session, test_user_id, username="auto_test_user")
        # Ensure AI Config exists
        cfg = await session.get(AIConfig, 1)
        if not cfg:
            cfg = AIConfig(id=1, provider="OpenAI", openai_api_key="test_key", openai_model="gpt-4o")
            session.add(cfg)
        else:
            cfg.provider = "OpenAI"
            cfg.openai_api_key = "test_key"
            cfg.fallback_enabled = 0

        # Add an automation handler for step transition
        handler = AutomationHandler(
            name="Test Auto Handler",
            scope="main",
            trigger_step="STEP_ONBOARDED",
            action_type="send_admin_message",
            action_payload_json=json.dumps({"text": "User {user_id} reached {step}"}),
            is_active=1,
        )
        session.add(handler)
        await session.commit()

    # Simulate structured AI response containing <DATA> block
    raw_ai_output = (
        "Здравствуйте! Рад познакомиться с вами.\n"
        "<DATA>{\"current_state\": {\"current_step\": \"STEP_ONBOARDED\", \"stage\": \"onboarded\"}, \"events\": [\"user_joined\"]}</DATA>\n"
        "Чем я могу помочь вам сегодня?"
    )
    mock_res = StructuredAIResponse(
        text=raw_ai_output,
        raw_payload={"choices": [{"message": {"content": raw_ai_output}}]},
        http_status=200,
        finish_reason="stop",
        diagnostics={"mock": True},
    )

    mock_bot = AsyncMock()
    with patch("ai_integration._generate_chat_response", AsyncMock(return_value=mock_res)):
        visible_text = await get_ai_response_for_topic(
            user_id=test_user_id,
            user_prompt="Привет!",
            user_name="Auto User",
            bot=mock_bot,
        )

    # 1. Verify visible text returned does NOT contain <DATA> block
    assert "<DATA>" not in visible_text, "<DATA> block must be stripped from visible text"
    assert "current_step" not in visible_text
    assert "Чем я могу помочь вам сегодня?" in visible_text

    # 2. Verify AutomationConversationState was updated
    async with async_session_maker() as session:
        conv_state = await session.scalar(
            select(AutomationConversationState).where(AutomationConversationState.user_id == test_user_id)
        )
        assert conv_state is not None, "Conversation state must be persisted"
        assert conv_state.current_step == "STEP_ONBOARDED"

        # 3. Verify AILog contains structured metadata and clean visible text
        log_entry = await session.scalar(
            select(AILog).where(AILog.user_id == test_user_id).order_by(AILog.id.desc())
        )
        assert log_entry is not None
        assert log_entry.http_status == 200
        assert log_entry.finish_reason == "stop"
        assert log_entry.clean_text == visible_text
        assert "<DATA>" not in log_entry.clean_text
