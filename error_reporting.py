import html
import json
import logging
import os
import re
import sys
import traceback
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence, Union
from unittest.mock import AsyncMock

from aiogram import Bot

from alert_cooldown import KeyedAlertCooldown

logger = logging.getLogger("error_reporting")

# Patterns for scrubbing secrets from error strings, URLs, and headers
_SECRET_PATTERNS = [
    (re.compile(r"([?&](?:key|token|secret)=)[^&\s'\"]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(sk-(?:proj-)?[A-Za-z0-9_\-]{8,})", re.IGNORECASE), r"sk-[REDACTED]"),
    (re.compile(r"(AIza[0-9A-Za-z-_]{20,})", re.IGNORECASE), r"AIza[REDACTED]"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{25,50}\b"), r"[REDACTED_TELEGRAM_TOKEN]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"), r"[REDACTED_API_KEY]"),
    (re.compile(r"(api[_-]?key[\"'\s:=]+)[A-Za-z0-9_\-]{8,}", re.IGNORECASE), r"\1[REDACTED]"),
    (
        re.compile(
            r"((?:password|passwd|secret|signature(?:value)?|token|access_token|client_secret|"
            r"merchant_password|merchant_pass[12]|pass[12]|authorization|shop_id|webhook_secret)\s*[:=]\s*[\"']?)[^\"'\s,}&]+",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
]


def sanitize_secret_values(text: Any) -> str:
    """Scrub known secret tokens, query params, passwords, and API keys from diagnostic strings."""
    if text is None:
        return ""
    result = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def is_testing_mode() -> bool:
    """Check if execution is under automated test harness."""
    return (
        os.getenv("TESTING", "").lower() in ("1", "true", "yes")
        or "pytest" in sys.modules
        or any("test" in arg for arg in sys.argv)
    )


# In-memory alert capture for test assertions
_captured_test_alerts: list[dict[str, Any]] = []


def get_captured_test_alerts() -> list[dict[str, Any]]:
    return list(_captured_test_alerts)


def reset_alert_cooldowns_for_tests() -> None:
    """Explicit test-only reset of all alert cooldown states."""
    _terminal_failure_cooldown.clear()
    _fallback_used_cooldown.clear()
    _default_admin_alert_cooldown.clear()


def clear_captured_test_alerts() -> None:
    _captured_test_alerts.clear()
    reset_alert_cooldowns_for_tests()


def _user_ref(user_id: Optional[int], username: Optional[str] = None, full_name: Optional[str] = None) -> str:
    """Format user reference safely: never prepend @ to full name, only to valid Telegram username."""
    if user_id is None:
        return "неизвестно"
    link = f"<a href='tg://user?id={user_id}'>профиль</a>"
    clean_username = username.lstrip("@").strip() if username else None
    clean_name = full_name.strip() if full_name else None

    if clean_username and clean_name:
        return f"{html.escape(clean_name)} (@{html.escape(clean_username)}, {link})"
    elif clean_username:
        return f"@{html.escape(clean_username)} ({link})"
    elif clean_name:
        return f"{html.escape(clean_name)} ({link})"
    return f"<code>{user_id}</code> ({link})"


def _shorten(value: str, limit: int = 1400) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]} ... [truncated]"


def exception_chain(exception: Optional[Exception], *, include_context: bool = True) -> list[Exception]:
    """Return a finite outer-to-inner exception chain without cycles."""
    chain: list[Exception] = []
    seen: set[int] = set()
    current = exception
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__
        if current is None and include_context and len(chain) > 0:
            current = chain[-1].__context__
    return chain


def root_cause_exception(exception: Optional[Exception], *, include_context: bool = True) -> Optional[Exception]:
    """Choose the deepest meaningful exception without following cycles."""
    chain = exception_chain(exception, include_context=include_context)
    if not chain:
        return None
    meaningful = [item for item in chain if str(item).strip()]
    return meaningful[-1] if meaningful else chain[-1]


def exception_summary(exception: Exception, *, include_context: bool = True) -> str:
    root = root_cause_exception(exception, include_context=include_context) or exception
    message = str(root).strip()
    return message or type(root).__name__


_ERROR_CLASS_DESCRIPTIONS = {
    "network_ssl": "Ошибка SSL/TLS при обращении к провайдеру",
    "network_connection": "Ошибка сетевого соединения с провайдером",
    "timeout": "Провайдер не ответил вовремя (timeout)",
    "auth": "Провайдер отклонил учетные данные или авторизацию",
    "rate_limit": "Провайдер ограничил частоту запросов (rate limit)",
    "insufficient_balance": "Недостаточно средств/квоты у провайдера",
    "provider_5xx": "Внутренняя ошибка сервиса провайдера (5xx)",
    "provider_rejection": "Провайдер отклонил запрос",
    "configuration": "Ошибка конфигурации приложения или ключей",
    "application_internal": "Внутренняя ошибка приложения",
    "unknown": "Неизвестная ошибка",
}


def classify_external_error(
    exception: Optional[Exception],
    provider: Optional[str] = None,
) -> tuple[str, str]:
    if exception is None:
        return "unknown", _ERROR_CLASS_DESCRIPTIONS["unknown"]

    msg = str(exception).lower()
    exc_type = type(exception).__name__.lower()

    if "ssl" in msg or "certificate" in msg:
        return "network_ssl", _ERROR_CLASS_DESCRIPTIONS["network_ssl"]
    if "timeout" in msg or "timed out" in msg:
        return "timeout", _ERROR_CLASS_DESCRIPTIONS["timeout"]
    if "connection" in msg or "connect" in exc_type:
        return "network_connection", _ERROR_CLASS_DESCRIPTIONS["network_connection"]
    if "401" in msg or "unauthorized" in msg or "invalid api key" in msg or "forbidden" in msg:
        return "auth", _ERROR_CLASS_DESCRIPTIONS["auth"]
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return "rate_limit", _ERROR_CLASS_DESCRIPTIONS["rate_limit"]
    if "quota" in msg or "insufficient_quota" in msg or "balance" in msg:
        return "insufficient_balance", _ERROR_CLASS_DESCRIPTIONS["insufficient_balance"]
    if "500" in msg or "502" in msg or "503" in msg or "504" in msg or "internal server error" in msg:
        return "provider_5xx", _ERROR_CLASS_DESCRIPTIONS["provider_5xx"]

    return "unknown", _ERROR_CLASS_DESCRIPTIONS["unknown"]


# Global keyed cooldowns
_default_admin_alert_cooldown = KeyedAlertCooldown(timedelta(minutes=15))
_terminal_failure_cooldown = KeyedAlertCooldown(timedelta(minutes=30))
_fallback_used_cooldown = KeyedAlertCooldown(timedelta(minutes=15))

_active_bot: Optional[Bot] = None


def set_active_bot(bot: Bot) -> None:
    global _active_bot
    _active_bot = bot


def get_active_bot() -> Optional[Bot]:
    return _active_bot


async def _dispatch_admin_alert_text(
    bot: Optional[Bot],
    text: str,
    *,
    alert_type: str = "alert",
    metadata: Optional[dict[str, Any]] = None,
) -> bool:
    """Centralized dispatcher for admin alerts.

    Guarantees:
    - In testing mode, strictly prevents calling live Telegram API.
    - If bot or mock is passed with AsyncMock send_message, invokes it so test assertions work.
    - Captures alerts in memory (_captured_test_alerts) during testing for inspection.
    """
    if is_testing_mode():
        logger.info("TESTING MODE: Admin alert intercepted (suppressing live Telegram API): %s", text[:80])
        _captured_test_alerts.append({
            "type": alert_type,
            "text": text,
            "metadata": metadata or {},
        })
        if bot and hasattr(bot, "send_message") and isinstance(bot.send_message, AsyncMock):
            try:
                from database import get_all_admin_ids
                admin_ids = await get_all_admin_ids()
            except Exception:
                admin_ids = {999999999}
            for admin_id in admin_ids:
                try:
                    await bot.send_message(admin_id, text, parse_mode="HTML")
                except Exception:
                    pass
        return True

    target_bot = bot or get_active_bot()
    if not target_bot:
        try:
            from config import BOT_TOKEN
            if BOT_TOKEN:
                from telegram_client import create_telegram_bot
                target_bot = create_telegram_bot(BOT_TOKEN)
        except Exception:
            target_bot = None

    if not target_bot:
        logger.warning("No bot instance available to send admin alert")
        return False

    try:
        from database import get_all_admin_ids
        admin_ids = await get_all_admin_ids()
        if not admin_ids:
            return False

        for admin_id in admin_ids:
            try:
                await target_bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception as exc:
                logger.error("Failed to send admin alert to %s: %s", admin_id, exc)
        return True
    except Exception as exc:
        logger.error("Admin alert delivery failed: %s", exc)
        return False


async def send_terminal_ai_failure_alert(
    bot: Optional[Bot] = None,
    *,
    platform: str = "Telegram",
    user_id: Optional[int] = None,
    user: Any = None,
    chat_id: Optional[int] = None,
    bot_name: Optional[str] = None,
    request_type: str = "chat",
    primary_provider: Optional[str] = None,
    primary_model: Optional[str] = None,
    fallback_provider: Optional[str] = None,
    fallback_model: Optional[str] = None,
    exception: Optional[Exception] = None,
    classification: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    ai_log_ids: Optional[Sequence[Union[int, str]]] = None,
    provider_attempts: Optional[Sequence[dict[str, Any]]] = None,
    attempts: Optional[Sequence[dict[str, Any]]] = None,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
    details: Optional[str] = None,
    stage: Optional[str] = None,
) -> bool:
    """Send realtime terminal AI failure alert to Telegram admins with 30m cooldown (PsychoBot 1:1 architecture & UX)."""
    if user is not None:
        user_id = getattr(user, "user_id", None) or getattr(user, "id", None) or user_id
        chat_id = getattr(user, "chat_id", None) or chat_id
        username = getattr(user, "username", None) or username
        full_name = getattr(user, "full_name", None) or getattr(user, "name", None) or getattr(user, "first_name", None) or full_name

    primary_provider = primary_provider or "Unknown"
    primary_model = primary_model or "—"
    eff_attempts = provider_attempts or attempts
    raw_err = error_message or details or (str(exception) if exception is not None else "Unknown error")
    cls_str = classification or "provider_failure"
    b_name = bot_name or "Vitaliy Bot"

    fingerprint = f"Telegram:{b_name}:{primary_provider}:{fallback_provider or 'none'}:{cls_str}"
    if not _terminal_failure_cooldown.should_send(fingerprint):
        return False

    err_type_str = error_type or (type(exception).__name__ if exception is not None else "AIServiceError")
    err_msg_str = sanitize_secret_values(str(raw_err))
    err_msg_short = _shorten(err_msg_str, 500)

    user_line = f"Пользователь: {_user_ref(user_id, username, full_name)}\n" if user_id is not None else ""
    id_label = f"Telegram ID: <code>{user_id}</code>\n" if user_id is not None else ""
    chat_line = f"Chat ID: <code>{chat_id}</code>\n" if chat_id is not None else ""

    fb_lines = ""
    if fallback_provider:
        fb_lines = f"Fallback: {html.escape(fallback_provider)} / {html.escape(fallback_model or '—')} ❌\n"

    attempts_block = ""
    if eff_attempts:
        lines = []
        for att in eff_attempts:
            p = att.get("provider", "Unknown")
            m = att.get("model", "—")
            st = att.get("status", "UNKNOWN")
            c = att.get("classification")
            mark = "✅" if (st in ("SUCCESS", "success", True) or str(st).upper() == "SUCCESS") else "❌"
            cls_info = f" ({c})" if c else ""
            lines.append(f"• {html.escape(str(p))} / {html.escape(str(m))} {mark}{cls_info}")
        attempts_block = "\nПопытки:\n" + "\n".join(lines) + "\n"

    if ai_log_ids:
        logs_str = ", ".join(f"#{i}" if str(i).isdigit() else str(i) for i in ai_log_ids)
        label = "AI Log:" if len(ai_log_ids) == 1 else "AI Log IDs:"
        ai_log_line = f"\n{label} {logs_str}"
    else:
        ai_log_line = "\nAI Log: не удалось сохранить"

    text = (
        f"🚨 <b>[Telegram] Сбой ИИ в текстовом диалоге</b>\n\n"
        f"Бот: <code>{html.escape(b_name)}</code>\n"
        f"{id_label}"
        f"{chat_line}"
        f"{user_line}"
        f"Primary: {html.escape(primary_provider)} / {html.escape(primary_model)} ❌\n"
        f"{fb_lines}"
        f"{attempts_block}\n"
        f"Ошибка:\n"
        f"<code>{html.escape(err_type_str)}</code> / <code>{html.escape(err_msg_short)}</code>\n\n"
        f"Классификация:\n"
        f"<code>{html.escape(cls_str)}</code>\n"
        f"{ai_log_line}"
    )

    return await _dispatch_admin_alert_text(
        bot,
        text,
        alert_type="terminal_ai_failure",
        metadata={
            "primary_provider": primary_provider,
            "primary_model": primary_model,
            "fallback_provider": fallback_provider,
            "fallback_model": fallback_model,
            "ai_log_ids": ai_log_ids,
            "classification": cls_str,
            "error_type": err_type_str,
        },
    )


async def send_ai_fallback_used_alert(
    bot: Optional[Bot] = None,
    *,
    platform: str = "Telegram",
    user_id: Optional[int] = None,
    user: Any = None,
    chat_id: Optional[int] = None,
    bot_name: Optional[str] = None,
    request_type: str = "chat",
    primary_provider: Optional[str] = None,
    primary_model: Optional[str] = None,
    failed_provider: Optional[str] = None,
    failed_model: Optional[str] = None,
    fallback_provider: str = "",
    fallback_model: Optional[str] = None,
    primary_error: Optional[Union[str, Exception]] = None,
    reason: Optional[Union[str, Exception]] = None,
    failure_reason: Optional[Union[str, Exception]] = None,
    ai_log_ids: Optional[Sequence[Union[int, str]]] = None,
    provider_attempts: Optional[Sequence[dict[str, Any]]] = None,
    attempts: Optional[Sequence[dict[str, Any]]] = None,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
) -> bool:
    """Send realtime notification that reserve AI provider was used successfully (PsychoBot 1:1 architecture & UX)."""
    if user is not None:
        user_id = getattr(user, "user_id", None) or getattr(user, "id", None) or user_id
        chat_id = getattr(user, "chat_id", None) or chat_id
        username = getattr(user, "username", None) or username
        full_name = getattr(user, "full_name", None) or getattr(user, "name", None) or getattr(user, "first_name", None) or full_name

    primary_provider = primary_provider or failed_provider or "Unknown"
    primary_model = primary_model or failed_model or "—"
    primary_error = primary_error or reason or failure_reason
    fallback_model = fallback_model or "—"
    b_name = bot_name or "Vitaliy Bot"

    fingerprint = f"fallback:Telegram:{b_name}:{primary_provider}:{fallback_provider}"
    if not _fallback_used_cooldown.should_send(fingerprint):
        return False

    id_label = f"Telegram ID: <code>{user_id}</code>\n" if user_id is not None else ""
    chat_line = f"Chat ID: <code>{chat_id}</code>\n" if chat_id is not None else ""
    user_line = f"Пользователь: {_user_ref(user_id, username, full_name)}\n" if user_id is not None else ""

    raw_err = str(primary_error) if primary_error is not None else "Не зафиксирована"
    err_str = sanitize_secret_values(str(raw_err))
    err_short = _shorten(err_str, 300)

    eff_attempts = provider_attempts or attempts
    attempts_block = ""
    if eff_attempts:
        lines = []
        for att in eff_attempts:
            p = att.get("provider", "Unknown")
            m = att.get("model", "—")
            st = att.get("status", "UNKNOWN")
            c = att.get("classification")
            mark = "✅" if (st in ("SUCCESS", "success", True) or str(st).upper() == "SUCCESS") else "❌"
            cls_info = f" ({c})" if c else ""
            lines.append(f"• {html.escape(str(p))} / {html.escape(str(m))} {mark}{cls_info}")
        attempts_block = "\nПопытки:\n" + "\n".join(lines) + "\n"

    ai_log_line = ""
    if ai_log_ids:
        logs_str = ", ".join(f"#{i}" if str(i).isdigit() else str(i) for i in ai_log_ids)
        label = "AI Log:" if len(ai_log_ids) == 1 else "AI Log IDs:"
        ai_log_line = f"\n{label} {logs_str}"

    text = (
        f"ℹ️ <b>[Telegram] Использован резервный AI-провайдер в текстовом диалоге</b>\n\n"
        f"Бот: <code>{html.escape(b_name)}</code>\n"
        f"{id_label}"
        f"{chat_line}"
        f"{user_line}"
        f"Основной: {html.escape(primary_provider)} / {html.escape(primary_model)} ❌\n"
        f"Резервный: {html.escape(fallback_provider)} / {html.escape(fallback_model)} ✅\n"
        f"{attempts_block}\n"
        f"Причина переключения:\n"
        f"<code>{html.escape(err_short)}</code>\n"
        f"{ai_log_line}"
    )

    return await _dispatch_admin_alert_text(
        bot,
        text,
        alert_type="fallback_used",
        metadata={
            "primary_provider": primary_provider,
            "primary_model": primary_model,
            "fallback_provider": fallback_provider,
            "fallback_model": fallback_model,
            "ai_log_ids": ai_log_ids,
        },
    )


async def notify_admins_about_error(
    bot: Optional[Bot] = None,
    *,
    title: str,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    stage: Optional[str] = None,
    details: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
    exception: Optional[Exception] = None,
    classification_override: Optional[str] = None,
    include_traceback: bool = True,
    cooldown_key: Optional[str] = None,
    cooldown: Optional[KeyedAlertCooldown] = None,
) -> bool:
    """General admin error notification with deduplication, secret sanitization, and test isolation."""
    try:
        active_cooldown = cooldown or _default_admin_alert_cooldown
        dedup_key = cooldown_key
        if not dedup_key:
            err_sig = type(exception).__name__ if exception else (details or title)
            dedup_key = f"{stage}:{provider}:{err_sig}"

        if not active_cooldown.should_send(dedup_key):
            logger.debug("Admin notification throttled by cooldown: %s", dedup_key)
            return False

        root_exc = root_cause_exception(exception)
        classification = classification_override
        if not classification and exception:
            classification, _ = classify_external_error(exception, provider=provider)
        classification_desc = _ERROR_CLASS_DESCRIPTIONS.get(classification or "", "")

        root_summary = sanitize_secret_values(exception_summary(root_exc or exception)) if exception else ""
        raw_trace = ""
        if exception and include_traceback:
            raw_trace = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
        trace = sanitize_secret_values(raw_trace)
        sanitized_details = sanitize_secret_values(details) if details else ""

        message_lines = [f"⚠️ <b>{html.escape(title)}</b>"]
        if stage:
            message_lines.append(f"<b>Этап:</b> <code>{html.escape(stage)}</code>")
        if user_id is not None:
            message_lines.append(f"<b>Пользователь:</b> {_user_ref(user_id, username, full_name)}")
        if provider:
            message_lines.append(f"<b>Провайдер:</b> <code>{html.escape(provider)}</code>")
        if model:
            message_lines.append(f"<b>Модель:</b> <code>{html.escape(model)}</code>")
        if classification:
            message_lines.append(f"<b>Классификация:</b> <code>{html.escape(classification)}</code> ({html.escape(classification_desc)})")
        if root_exc:
            message_lines.append(f"<b>Исключение:</b> <code>{html.escape(type(root_exc).__name__)}</code>")
        if root_summary:
            message_lines.append(f"<b>Причина:</b> <code>{html.escape(_shorten(root_summary, 800))}</code>")
        if sanitized_details and sanitized_details != root_summary:
            message_lines.append(f"<b>Детали:</b> <code>{html.escape(_shorten(sanitized_details, 800))}</code>")
        if extra:
            try:
                extra_clean = {k: sanitize_secret_values(v) for k, v in extra.items()}
                extra_json = json.dumps(extra_clean, ensure_ascii=False, indent=2, default=str)
                message_lines.append(f"<b>Контекст:</b> <pre>{html.escape(_shorten(extra_json, 600))}</pre>")
            except Exception:
                pass
        if trace and trace.strip() and trace.strip() != "NoneType: None":
            message_lines.append(f"<b>Traceback:</b>\n<pre>{html.escape(_shorten(trace, 1200))}</pre>")

        admin_text = "\n".join(message_lines)
        return await _dispatch_admin_alert_text(
            bot,
            admin_text,
            alert_type="general_error",
            metadata={"title": title, "provider": provider, "model": model, "stage": stage},
        )
    except Exception as exc:
        logger.error("Error in notify_admins_about_error (safely ignored): %s", exc, exc_info=True)
        return False
