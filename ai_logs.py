import html
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from database import AILog, Base, async_session_maker
from error_reporting import sanitize_secret_values

logger = logging.getLogger("ai_logs")

AI_PLATFORM_TELEGRAM = "telegram"
AI_CONTEXT_MAIN = "main"
AI_CONTEXT_TOPIC = "topic"


def apply_ai_log_context(
    log: Any,
    *,
    platform: str = "telegram",
    topic_id: Optional[int] = None,
    topic_slug: Optional[str] = None,
    topic_name: Optional[str] = None,
) -> None:
    log.platform = platform
    if topic_slug or (topic_id is not None and topic_id != 0):
        log.context_kind = AI_CONTEXT_TOPIC
        log.topic_id = topic_id
        log.topic_slug = topic_slug
        log.topic_name_snapshot = topic_name or topic_slug
    else:
        log.context_kind = AI_CONTEXT_MAIN
        log.topic_id = None
        log.topic_slug = None
        log.topic_name_snapshot = None


def ai_log_context_label(log: Any) -> str:
    context_kind = getattr(log, "context_kind", None)
    if context_kind == AI_CONTEXT_MAIN:
        return "Основной диалог"
    if context_kind == AI_CONTEXT_TOPIC:
        name = (getattr(log, "topic_name_snapshot", None) or getattr(log, "topic_slug", None) or "").strip()
        if name:
            return f"Тема диалога — «{name}»"
        t_id = getattr(log, "topic_id", None)
        if t_id is not None:
            return f"Тема диалога — ID {t_id}"
    slug = getattr(log, "topic_slug", None)
    if slug:
        return f"Тема диалога — «{slug}»"
    return "Основной диалог"


def format_msk(dt: Optional[datetime], fmt: str = "%d.%m %H:%M") -> str:
    if dt is None:
        return "--.-- --:--"
    # MSK is UTC+3
    msk_dt = dt + timedelta(hours=3)
    return msk_dt.strftime(fmt)


def build_ai_attempt_txt_file(log_entry: Any) -> str:
    """Format full, downloadable raw diagnostic log file matching PsychoBot 1:1 format."""
    status = (getattr(log_entry, "status", None) or "success").upper()
    req_group = getattr(log_entry, "request_group_id", None) or "не зафиксирован"
    attempt = getattr(log_entry, "attempt_no", None) or 1
    role = (getattr(log_entry, "attempt_role", None) or "primary").upper()

    created_at = getattr(log_entry, "created_at", None)
    ts_str = str(created_at) if created_at else "не зафиксирован"

    platform = getattr(log_entry, "platform", None) or "telegram"
    user_id = getattr(log_entry, "user_id", None)
    disp_user_id = str(user_id) if user_id is not None else "не указан"

    dialogue_id = getattr(log_entry, "dialogue_id", None) or "не зафиксирован"
    topic_str = ai_log_context_label(log_entry)

    provider = getattr(log_entry, "provider", None) or "не указан"
    model = getattr(log_entry, "model", None) or "не указан"
    lat = getattr(log_entry, "latency_ms", None)
    lat_str = f"{lat} ms ({lat / 1000:.2f} сек)" if lat is not None else "не измерялось"

    http_status = getattr(log_entry, "http_status", None)
    http_str = str(http_status) if http_status is not None else "не зафиксирован"

    error_type = getattr(log_entry, "error_type", None) or "не зафиксировано"
    error_cls = getattr(log_entry, "error_classification", None) or "не зафиксировано"
    error_msg = getattr(log_entry, "error_message", None) or "не зафиксировано"
    diagnostics = getattr(log_entry, "diagnostics_json", None) or "не зафиксировано"
    finish_reason = getattr(log_entry, "finish_reason", None) or "не зафиксирован"

    request_payload = getattr(log_entry, "request_payload", None) or "<none>"
    raw_provider_resp = (
        getattr(log_entry, "provider_response_payload", None)
        or getattr(log_entry, "raw_response", None)
        or "<none>"
    )

    if status == "ERROR":
        app_error = f"{error_type}: {error_msg}"
        clean_text = getattr(log_entry, "clean_text", None) or "не зафиксирован"
    else:
        app_error = "не зафиксировано"
        clean_text = (
            getattr(log_entry, "clean_text", None)
            or getattr(log_entry, "raw_response", None)
            or "не зафиксирован"
        )

    req_payload_content = (
        request_payload
        if (request_payload and request_payload.strip() and request_payload != "<none>")
        else "не зафиксирован"
    )

    return (
        f"========================================\n"
        f"AI LOG RECORD #{getattr(log_entry, 'id', 0)}\n"
        f"========================================\n\n"
        f"Status: {status}\n"
        f"Request Group: {req_group}\n"
        f"Attempt: {attempt} ({role})\n"
        f"Platform: {platform}\n"
        f"User ID: {disp_user_id}\n"
        f"Timestamp: {ts_str}\n"
        f"Dialogue: {dialogue_id}\n"
        f"Topic: {topic_str}\n\n"
        f"Provider: {provider}\n"
        f"Model: {model}\n"
        f"Latency: {lat_str}\n\n"
        f"HTTP Status: {http_str}\n"
        f"Error Type: {error_type}\n"
        f"Error Classification: {error_cls}\n"
        f"Error Message: {error_msg}\n"
        f"Diagnostics: {diagnostics}\n"
        f"Finish Reason: {finish_reason}\n\n"
        f"========================================\n"
        f"📤 [1] FULL REQUEST PAYLOAD:\n"
        f"----------------------------------------\n"
        f"{req_payload_content}\n\n"
        f"========================================\n"
        f"🤖 [2] RAW RESPONSE FROM LLM:\n"
        f"----------------------------------------\n"
        f"{raw_provider_resp}\n\n"
        f"========================================\n"
        f"🚨 [3] APPLICATION ERROR:\n"
        f"----------------------------------------\n"
        f"{app_error}\n\n"
        f"========================================\n"
        f"💬 [3] CLEAN TEXT SENT TO USER:\n"
        f"----------------------------------------\n"
        f"{clean_text}\n"
    )


async def record_ai_attempt_log(
    *,
    user_id: Optional[int] = None,
    dialogue_id: Optional[int] = None,
    topic_slug: Optional[str] = None,
    topic_id: Optional[int] = None,
    topic_name: Optional[str] = None,
    platform: str = "telegram",
    context_kind: Optional[str] = None,
    request_type: str = "chat",
    provider: str,
    model: str,
    request_group_id: Optional[str] = None,
    attempt_no: int = 1,
    attempt_role: str = "primary",
    status: str = "success",
    latency_ms: Optional[int] = None,
    prompt_summary: Optional[str] = None,
    request_capture: Optional[Any] = None,
    raw_response: Optional[str] = None,
    clean_text: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    error_classification: Optional[str] = None,
    http_status: Optional[int] = None,
    finish_reason: Optional[str] = None,
    diagnostics: Optional[Any] = None,
    provider_response_payload: Optional[str] = None,
    session: Optional[Any] = None,
) -> Optional[int]:
    """Safely records structured AI attempt in AILog table.

    CRITICAL: Best-effort only. If database insertion fails for any reason,
    it logs a warning and returns None without raising, preserving user flow.
    Secrets are always sanitized.
    """
    try:
        # Sanitize payload
        payload_str: Optional[str] = None
        if request_capture is not None:
            try:
                if isinstance(request_capture, (dict, list)):
                    payload_str = sanitize_secret_values(
                        json.dumps(request_capture, ensure_ascii=False, indent=2, default=str)
                    )
                else:
                    payload_str = sanitize_secret_values(str(request_capture))
            except Exception:
                payload_str = sanitize_secret_values(str(request_capture))

        sanitized_raw_resp = sanitize_secret_values(raw_response) if raw_response is not None else None
        sanitized_clean_text = sanitize_secret_values(clean_text) if clean_text is not None else None
        sanitized_err_msg = sanitize_secret_values(error_message) if error_message is not None else None
        sanitized_prompt_sum = sanitize_secret_values(prompt_summary) if prompt_summary is not None else None

        diag_str: Optional[str] = None
        if diagnostics is not None:
            try:
                if isinstance(diagnostics, (dict, list)):
                    diag_str = sanitize_secret_values(json.dumps(diagnostics, ensure_ascii=False, default=str))
                else:
                    diag_str = sanitize_secret_values(str(diagnostics))
            except Exception:
                diag_str = sanitize_secret_values(str(diagnostics))

        provider_resp_str: Optional[str] = None
        if provider_response_payload is not None:
            provider_resp_str = sanitize_secret_values(str(provider_response_payload))

        eff_context_kind = context_kind
        if not eff_context_kind:
            eff_context_kind = AI_CONTEXT_TOPIC if (topic_slug or (topic_id not in (None, 0))) else AI_CONTEXT_MAIN

        entry = AILog(
            user_id=user_id,
            dialogue_id=dialogue_id,
            topic_slug=topic_slug,
            topic_id=topic_id,
            topic_name_snapshot=topic_name or topic_slug,
            platform=platform,
            context_kind=eff_context_kind,
            request_type=request_type,
            provider=provider or "unknown",
            model=model or "unknown",
            request_group_id=request_group_id,
            attempt_no=attempt_no,
            attempt_role=attempt_role,
            status=status,
            latency_ms=latency_ms,
            prompt_summary=sanitized_prompt_sum,
            request_payload=payload_str,
            raw_response=sanitized_raw_resp,
            clean_text=sanitized_clean_text,
            error_type=error_type,
            error_message=sanitized_err_msg,
            error_classification=error_classification,
            http_status=http_status,
            finish_reason=finish_reason,
            diagnostics_json=diag_str,
            provider_response_payload=provider_resp_str,
            created_at=datetime.utcnow(),
        )

        if session is not None:
            try:
                async with session.begin_nested():
                    session.add(entry)
                    await session.flush()
                return entry.id
            except Exception as exc:
                logger.warning("Failed to record AILog in nested transaction (suppressed): %s", exc)
                return None
        else:
            async with async_session_maker() as new_session:
                new_session.add(entry)
                await new_session.commit()
                return entry.id
    except Exception as exc:
        logger.warning("Failed to record AILog (suppressed to preserve conversation flow): %s", exc)
        return None
