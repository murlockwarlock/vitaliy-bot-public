import asyncio
import os
import httpx
from contextlib import asynccontextmanager
import contextvars
from dataclasses import dataclass
import json
import logging
import mimetypes
import time
import uuid
from typing import Any, Dict, List, Optional
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from database import async_session_maker, AIConfig, Message, User, Topic, JournalEntry
from logging_config import get_logger
from vector_store import get_collection
from kie_client import (
    KIEError,
    chat as kie_chat,
    chat_structured as kie_chat_structured,
    edit_image as kie_edit_image,
    generate_image as kie_generate_image,
    transcribe as kie_transcribe,
)
from ai_logs import record_ai_attempt_log
from error_reporting import (
    notify_admins_about_error,
    get_active_bot,
    send_terminal_ai_failure_alert,
    send_ai_fallback_used_alert,
    classify_external_error,
)
from automation_engine import extract_service_data, apply_automation_data
from automation_events import schedule_automation_events
from crisis_safety import check_crisis_markers, mark_crisis_detected

logger = get_logger("ai")

_active_typing_contexts: contextvars.ContextVar[Optional[set]] = contextvars.ContextVar(
    "_active_typing_contexts", default=None
)


@asynccontextmanager
async def ai_typing_action(bot: Any, chat_id: Any, interval: float = 4.0, action: str = "typing"):
    """
    Maintains a continuous Telegram chat action in the specified chat
    for the entire duration of the async context block (e.g. AI generation).
    Telegram actions expire in ~5 seconds, so we pulse every 4.0 seconds.
    Resilient against network drops, missing bot, or unawaited mock objects.
    Nesting-safe: re-entrant so outer interactive contexts keep pulsing without disruption.
    """
    if not bot or not chat_id or not hasattr(bot, "send_chat_action") or not callable(getattr(bot, "send_chat_action")):
        yield
        return

    key = (id(bot), str(chat_id), action)
    active = _active_typing_contexts.get()
    if active is None:
        active = set()
        _active_typing_contexts.set(active)

    if key in active:
        # Already active in an outer interactive context
        yield
        return

    active.add(key)
    stop_event = asyncio.Event()

    async def _pulse():
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action=action)
            except Exception as e:
                logger.debug("ai_typing_action pulse failed: %s", e)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_pulse())
    try:
        yield
    finally:
        active.discard(key)
        stop_event.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


CANONICAL_PROVIDERS = {
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "kie": "KIE",
    "openrouter": "OpenRouter",
}

DEFAULT_PROVIDER_MODELS = {
    "OpenAI": "gpt-4o-mini",
    "DeepSeek": "deepseek-chat",
    "KIE": "gemini-2.5-flash",
    "OpenRouter": "openai/gpt-5.6-terra",
}


def normalize_provider(name: Optional[str]) -> str:
    """Normalize AI provider name to canonical representation (OpenAI, DeepSeek, KIE)."""
    if not name:
        return "OpenAI"
    cleaned = str(name).strip()
    return CANONICAL_PROVIDERS.get(cleaned.lower(), cleaned)


GLOBAL_TONE_INSTRUCTION = (
    "\n\nВАЖНОЕ ПРАВИЛО ОБЩЕНИЯ:\n"
    "Всегда обращайся к пользователю строго на 'ты'. "
    "Категорически запрещено использовать 'Вы', 'Ваш', 'Вам'. "
    "Держи тон бережным, глубоким, поддерживающим и уважительным."
)


@dataclass
class StructuredAIResponse:
    text: str
    raw_payload: Any
    http_status: Optional[int]
    finish_reason: Optional[str]
    diagnostics: Optional[Dict[str, Any]] = None


async def search_relevant_chunks(query, doc_ids, client):
    if not doc_ids:
        return []

    try:
        collection = get_collection()
        if collection is None:
            return []

        # Создаем вектор запроса
        response = await client.embeddings.create(input=query, model="text-embedding-3-small")
        query_embedding = response.data[0].embedding

        # Поиск в БД с фильтрацией по ID файлов
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=3,
            where={"file_id": {"$in": doc_ids}}
        )

        return results['documents'][0] if results['documents'] else []
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []



def resolve_openrouter_proxy() -> Optional[str]:
    """Resolve proxy URL for OpenRouter with deterministic precedence:
    OPENROUTER_PROXY > HTTPS_PROXY > HTTP_PROXY > direct (None).
    """
    for env_var in ("OPENROUTER_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        val = os.getenv(env_var, "").strip()
        if val:
            return val
    return None


def build_openrouter_client(
    api_key: str,
    *,
    base_url: str = "https://openrouter.ai/api/v1",
    proxy_url: Optional[str] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    max_retries: Optional[int] = None,
) -> AsyncOpenAI:
    """Construct AsyncOpenAI client for OpenRouter with explicit deterministic egress route.

    Guarantees:
    - Never relies on implicit SDK or environment defaults.
    - Explicitly sets trust_env=False on the underlying httpx client.
    - Supports provider-specific OPENROUTER_PROXY with fallback to HTTPS_PROXY / HTTP_PROXY.
    - If proxy is configured, uses httpx.AsyncHTTPTransport(proxy=proxy_url).
    - If proxy connection fails, fails closed without silent fallback to direct production IP.
    """
    eff_proxy = proxy_url if proxy_url is not None else resolve_openrouter_proxy()
    if transport is not None:
        http_client = httpx.AsyncClient(transport=transport, trust_env=False)
    elif eff_proxy:
        http_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(proxy=eff_proxy),
            trust_env=False,
        )
    else:
        http_client = httpx.AsyncClient(trust_env=False)

    kwargs: Dict[str, Any] = {
        "api_key": api_key,
        "base_url": base_url,
        "http_client": http_client,
    }
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return AsyncOpenAI(**kwargs)


async def _prepare_clients(session):
    ai_config = await session.get(AIConfig, 1)
    if not ai_config:
        return None, None, None, None, "⚠️ AI конфигурация не найдена."

    canonical_provider = normalize_provider(ai_config.provider)
    ai_config.provider = canonical_provider

    embedding_client = None
    if ai_config.openai_api_key:
        embedding_client = AsyncOpenAI(api_key=ai_config.openai_api_key)

    if canonical_provider == 'KIE':
        if not ai_config.kie_api_key:
            return ai_config, embedding_client, None, None, "⚠️ Выбран KIE, но API ключ не установлен."
        model_name = ai_config.kie_model or DEFAULT_PROVIDER_MODELS['KIE']
        return ai_config, embedding_client, None, model_name, None

    if canonical_provider == 'DeepSeek':
        if not ai_config.deepseek_api_key:
            return ai_config, embedding_client, None, None, "⚠️ Выбран DeepSeek, но API ключ не установлен."

        chat_client = AsyncOpenAI(
            api_key=ai_config.deepseek_api_key,
            base_url="https://api.deepseek.com"
        )
        model_name = ai_config.deepseek_model or DEFAULT_PROVIDER_MODELS['DeepSeek']
        return ai_config, embedding_client, chat_client, model_name, None

    if canonical_provider == 'OpenAI':
        if not ai_config.openai_api_key:
            return ai_config, embedding_client, None, None, "⚠️ API ключ OpenAI не настроен."

        chat_client = embedding_client
        model_name = ai_config.openai_model or DEFAULT_PROVIDER_MODELS['OpenAI']
        return ai_config, embedding_client, chat_client, model_name, None

    if canonical_provider == 'OpenRouter':
        if not ai_config.openrouter_api_key:
            return ai_config, embedding_client, None, None, "⚠️ Выбран OpenRouter, но API ключ не установлен."

        from openrouter_models import resolve_openrouter_model, OpenRouterModelError
        try:
            model_name = resolve_openrouter_model(ai_config.openrouter_model)
        except OpenRouterModelError as e:
            return ai_config, embedding_client, None, None, f"⚠️ Ошибка конфигурации OpenRouter: {e}"

        chat_client = build_openrouter_client(
            api_key=ai_config.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
        )
        return ai_config, embedding_client, chat_client, model_name, None

    return ai_config, embedding_client, None, None, f"⚠️ Неизвестный AI провайдер: {canonical_provider}"


def _prepare_fallback_client(ai_config: AIConfig) -> tuple[Optional[str], Optional[str], Any]:
    """
    Prepare fallback provider, model, and client if allow_fallback is enabled.
    Returns (fallback_provider, fallback_model, fallback_client).
    """
    allow_fallback = bool(getattr(ai_config, "allow_fallback", False))
    if not allow_fallback:
        return None, None, None

    cfg_fb_prov = (getattr(ai_config, "fallback_provider", None) or "").strip()
    norm_fb_prov = normalize_provider(cfg_fb_prov)
    cfg_fb_model = getattr(ai_config, "fallback_model", None)

    if norm_fb_prov == "OpenAI" and ai_config.openai_api_key:
        fallback_provider = "OpenAI"
        fallback_model = cfg_fb_model or ai_config.openai_model or DEFAULT_PROVIDER_MODELS["OpenAI"]
        fallback_client = AsyncOpenAI(api_key=ai_config.openai_api_key)
        return fallback_provider, fallback_model, fallback_client

    if norm_fb_prov == "DeepSeek" and ai_config.deepseek_api_key:
        fallback_provider = "DeepSeek"
        fallback_model = cfg_fb_model or ai_config.deepseek_model or DEFAULT_PROVIDER_MODELS["DeepSeek"]
        fallback_client = AsyncOpenAI(api_key=ai_config.deepseek_api_key, base_url="https://api.deepseek.com")
        return fallback_provider, fallback_model, fallback_client

    if norm_fb_prov == "KIE" and ai_config.kie_api_key:
        fallback_provider = "KIE"
        fallback_model = cfg_fb_model or getattr(ai_config, "kie_model", None) or DEFAULT_PROVIDER_MODELS["KIE"]
        fallback_client = "KIE"
        return fallback_provider, fallback_model, fallback_client

    if norm_fb_prov == "OpenRouter" and getattr(ai_config, "openrouter_api_key", None):
        from openrouter_models import resolve_openrouter_fallback_model, OpenRouterModelError
        try:
            fallback_model = resolve_openrouter_fallback_model(
                cfg_fb_model,
                getattr(ai_config, "openrouter_model", None),
            )
            fallback_provider = "OpenRouter"
            fallback_client = build_openrouter_client(
                api_key=ai_config.openrouter_api_key,
                base_url="https://openrouter.ai/api/v1",
            )
            return fallback_provider, fallback_model, fallback_client
        except OpenRouterModelError as e:
            logger.warning("OpenRouter fallback configuration error: %s", e)
            return None, None, None

    logger.warning(
        "AI Fallback is enabled in admin config, but configured provider '%s' cannot be initialized (missing key, invalid model, or unrecognized provider)",
        cfg_fb_prov,
    )
    return None, None, None


def _kie_base_url(config: AIConfig) -> str:
    return (config.kie_base_url or 'https://api.kie.ai').rstrip('/')


def _kie_upload_base_url(config: AIConfig) -> str:
    return (config.kie_upload_base_url or 'https://kieai.redpandaai.co').rstrip('/')


async def _generate_chat_response(
    ai_config: AIConfig, chat_client, model_name: str, messages: list[dict]
) -> StructuredAIResponse:
    canonical_provider = normalize_provider(ai_config.provider)
    if canonical_provider == 'KIE':
        data = await kie_chat_structured(
            ai_config.kie_api_key,
            _kie_base_url(ai_config),
            model_name,
            messages,
            temperature=float(getattr(ai_config, 'temperature', 0.7) or 0.7),
        )
        return StructuredAIResponse(
            text=data["text"],
            raw_payload=data.get("raw_payload"),
            http_status=data.get("http_status"),
            finish_reason=data.get("finish_reason"),
            diagnostics=data.get("diagnostics"),
        )

    response = await chat_client.chat.completions.create(model=model_name, messages=messages)
    choice = response.choices[0]
    content = choice.message.content or ""
    finish_reason = getattr(choice, "finish_reason", None)

    try:
        raw_payload = response.model_dump()
    except Exception:
        try:
            raw_payload = response.to_dict()
        except Exception:
            raw_payload = str(response)

    # Provider SDK does not expose HTTP status code directly on model object; per spec store None
    http_status = None

    return StructuredAIResponse(
        text=content,
        raw_payload=raw_payload,
        http_status=http_status,
        finish_reason=finish_reason,
        diagnostics={"provider": ai_config.provider, "model": model_name},
    )


async def _process_ai_automation_and_clean_text(
    session,
    *,
    raw_text: str,
    user_id: int,
    dialogue_id: int,
    scope: str,
    topic_slug: Optional[str],
    bot: Optional[Any] = None,
    user_text: Optional[str] = None,
    alert_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    clean_visible_text, payloads, invalid_block_count = extract_service_data(raw_text)
    effective_bot = bot or get_active_bot()
    base_metadata = dict(alert_metadata or {})
    base_metadata["user_text"] = user_text or base_metadata.get("user_text") or ""
    for payload in payloads:
        try:
            apply_res = await apply_automation_data(
                session,
                user_id=user_id,
                dialogue_id=dialogue_id,
                scope=scope,
                topic_slug=topic_slug,
                payload=payload,
            )
            crisis_event = any(
                str(event).strip().lower() == "crisis_detected"
                for event in apply_res.events
            )
            if crisis_event:
                await mark_crisis_detected(session, user_id)
            await session.commit()
            if effective_bot and (apply_res.events or apply_res.current_step):
                metadata = dict(base_metadata)
                metadata.update(apply_res.metadata)
                metadata["user_text"] = user_text or metadata.get("user_text") or ""
                schedule_automation_events(
                    effective_bot,
                    user_id=user_id,
                    dialogue_id=dialogue_id,
                    scope=scope,
                    topic_slug=topic_slug,
                    events=apply_res.events,
                    current_step=apply_res.current_step,
                    metadata=metadata,
                )
        except Exception as exc:
            logger.warning("Error applying automation data for user_id=%s: %s", user_id, exc)
    if effective_bot and invalid_block_count:
        await session.commit()
        schedule_automation_events(
            effective_bot,
            user_id=user_id,
            dialogue_id=dialogue_id,
            scope=scope,
            topic_slug=topic_slug,
            events=("parse_failed",),
            metadata=base_metadata,
        )
    return clean_visible_text


async def _apply_crisis_marker_protection(
    session,
    *,
    user_id: int,
    user_text: str | None,
    bot: Optional[Any] = None,
    dialogue_id: int = 1,
    scope: str = "main",
    topic_slug: Optional[str] = None,
    alert_metadata: Optional[Dict[str, Any]] = None,
) -> str | None:
    matched_marker = await check_crisis_markers(session, user_id, user_text)
    if not matched_marker:
        return None

    await session.commit()
    effective_bot = bot or get_active_bot()
    if effective_bot:
        metadata = dict(alert_metadata or {})
        metadata["user_text"] = user_text or metadata.get("user_text") or ""
        metadata["crisis_marker"] = matched_marker
        schedule_automation_events(
            effective_bot,
            user_id=user_id,
            dialogue_id=dialogue_id,
            scope=scope,
            topic_slug=topic_slug,
            events=("crisis_detected",),
            metadata=metadata,
        )
    return matched_marker


async def get_standalone_ai_response(
    system_prompt: str,
    user_prompt: str,
    bot: Optional[Any] = None,
    chat_id: Optional[int] = None,
    user_id: int | None = None,
) -> str:
    """Run an isolated interpretation without leaking the dialogue history into it."""
    async with ai_typing_action(bot, chat_id):
        async with async_session_maker() as session:
            if user_id is not None:
                await _apply_crisis_marker_protection(
                    session,
                    user_id=user_id,
                    user_text=user_prompt,
                    bot=bot,
                    dialogue_id=1,
                    scope="main",
                    topic_slug=None,
                )
            ai_config, _, chat_client, model_name, error_text = await _prepare_clients(session)
            if error_text:
                raise RuntimeError(error_text)
            effective_system = f"{system_prompt}{GLOBAL_TONE_INSTRUCTION}"
            request_group_id = uuid.uuid4().hex[:12]
            start_ts = time.monotonic()
            req_messages = [{"role": "system", "content": effective_system}, {"role": "user", "content": user_prompt}]
            try:
                ai_res = await _generate_chat_response(
                    ai_config,
                    chat_client,
                    model_name,
                    req_messages,
                )
                resp = ai_res.text or ""
                latency_ms = int((time.monotonic() - start_ts) * 1000)
                payload_str = (
                    json.dumps(ai_res.raw_payload, ensure_ascii=False)
                    if isinstance(ai_res.raw_payload, (dict, list))
                    else str(ai_res.raw_payload)
                )
                await record_ai_attempt_log(
                    request_type="standalone",
                    provider=ai_config.provider or "unknown",
                    model=model_name or "unknown",
                    status="success",
                    latency_ms=latency_ms,
                    prompt_summary=user_prompt[:500] if user_prompt else None,
                    clean_text=resp,
                    raw_response=resp,
                    request_capture=req_messages,
                    provider_response_payload=payload_str,
                    http_status=ai_res.http_status,
                    finish_reason=ai_res.finish_reason,
                    diagnostics=ai_res.diagnostics,
                    request_group_id=request_group_id,
                    attempt_no=1,
                    attempt_role="primary",
                )
                return resp
            except Exception as e:
                latency_ms = int((time.monotonic() - start_ts) * 1000)
                await record_ai_attempt_log(
                    request_type="standalone",
                    provider=ai_config.provider or "unknown",
                    model=model_name or "unknown",
                    status="error",
                    latency_ms=latency_ms,
                    prompt_summary=user_prompt[:500] if user_prompt else None,
                    error_type=type(e).__name__,
                    error_message=str(e),
                    error_classification=getattr(e, "error_classification", "ProviderAPIError"),
                    http_status=getattr(e, "status_code", getattr(e, "http_status", None)),
                    request_capture=req_messages,
                    request_group_id=request_group_id,
                    attempt_no=1,
                    attempt_role="primary",
                )
                raise


async def get_ai_response_for_topic(
        user_id: int,
        user_prompt: str,
        user_name: str,
        topic_slug: str | None = None,
        dialogue_id: int | None = None,
        extra_system: str | None = None,
        current_message_id: int | None = None,
        bot: Optional[Any] = None,
        chat_id: Optional[int] = None,
        alert_user_text: str | None = None,
        alert_metadata: Optional[Dict[str, Any]] = None,
        prompt_variables: Optional[Dict[str, Any]] = None,
) -> str:
    effective_chat_id = chat_id if chat_id is not None else user_id
    async with ai_typing_action(bot, effective_chat_id):
        async with async_session_maker() as session:
            user = await session.scalar(select(User).where(User.id == user_id).options(selectinload(User.current_topic)))
            if not user:
                logger.warning("AI request for missing user_id=%s topic=%s", user_id, topic_slug or "current")
                return "⚠️ Пользователь не найден."

            active_dialogue_id = dialogue_id if dialogue_id is not None else user.current_dialogue_id
            active_topic_slug = topic_slug if topic_slug is not None else user.current_topic_slug
            await _apply_crisis_marker_protection(
                session,
                user_id=user_id,
                user_text=alert_user_text or user_prompt,
                bot=bot,
                dialogue_id=active_dialogue_id,
                scope="topic" if active_topic_slug else "main",
                topic_slug=active_topic_slug,
                alert_metadata=alert_metadata,
            )

            ai_config, embedding_client, chat_client, model_name, error_text = await _prepare_clients(session)
            if error_text:
                logger.warning("AI config error for user_id=%s topic=%s: %s", user_id, topic_slug or "current", error_text)
                return error_text

            effective_name = (user.name or user_name or user.first_name or "пользователь").strip()

            topic = None
            if topic_slug:
                topic = await session.scalar(
                    select(Topic).where(Topic.slug == topic_slug).options(selectinload(Topic.knowledge_base_files))
                )
            else:
                topic = user.current_topic
                if topic:
                    topic = await session.scalar(
                        select(Topic).where(Topic.id == topic.id).options(selectinload(Topic.knowledge_base_files))
                    )

            system_prompt_text = topic.system_prompt if topic and topic.system_prompt else "Ты полезный помощник."
            if topic_slug == "funnel_demo" and prompt_variables:
                for variable_name, variable_value in prompt_variables.items():
                    placeholder = "{" + str(variable_name) + "}"
                    if placeholder in system_prompt_text:
                        system_prompt_text = system_prompt_text.replace(
                            placeholder,
                            "" if variable_value is None else str(variable_value),
                        )

            docs_ids = [f.id for f in topic.knowledge_base_files] if topic else []

            rag_context = ""
            if docs_ids and embedding_client:
                chunks = await search_relevant_chunks(user_prompt, docs_ids, embedding_client)
                if chunks:
                    context_text = "\n\n".join(chunks)
                    rag_context = (
                        f"ВАЖНО: При ответе используй следующую информацию из Базы Знаний:\n"
                        f"--- НАЧАЛО БАЗЫ ЗНАНИЙ ---\n{context_text}\n--- КОНЕЦ БАЗЫ ЗНАНИЙ ---\n"
                        f"Если в базе нет ответа, отвечай, опираясь на свои знания, но придерживайся роли."
                    )

            from ai_dialogue_service import build_ai_user_profile_context, get_turn_aware_history

            history_msgs = await get_turn_aware_history(
                session=session,
                user_id=user_id,
                topic_slug=active_topic_slug or "",
                dialogue_id=active_dialogue_id,
                limit_first=ai_config.context_limit_first,
                limit_recent=ai_config.context_limit_recent,
                exclude_message_id=current_message_id,
            )

            messages = []

            profile_context = build_ai_user_profile_context(user)
            full_system = system_prompt_text
            if profile_context:
                full_system += f"\n\nПрофиль пользователя:\n{profile_context}"
            if rag_context:
                full_system += f"\n\n{rag_context}"
            if extra_system:
                full_system += f"\n\n{extra_system}"
            full_system += GLOBAL_TONE_INSTRUCTION

            messages.append({"role": "system", "content": full_system})

            for m in history_msgs:
                if current_message_id is not None and m.id == current_message_id:
                    continue
                if m.content:
                    messages.append({"role": m.role, "content": m.content})

            messages.append({"role": "user", "content": user_prompt})

            request_group_id = uuid.uuid4().hex[:12]
            start_ts = time.monotonic()
            primary_prov = normalize_provider(ai_config.provider)
            primary_mod = model_name or DEFAULT_PROVIDER_MODELS.get(primary_prov, "unknown")
            real_username = getattr(user, "username", None)
            try:
                logger.info(
                    "AI request: user_id=%s topic=%s provider=%s model=%s dialogue_id=%s history=%s rag_docs=%s group=%s",
                    user_id,
                    active_topic_slug or "none",
                    primary_prov,
                    primary_mod,
                    active_dialogue_id,
                    len(history_msgs),
                    len(docs_ids),
                    request_group_id,
                )
                ai_res = await _generate_chat_response(ai_config, chat_client, model_name, messages)
                latency_ms = int((time.monotonic() - start_ts) * 1000)
                logger.info(
                    "AI response success: user_id=%s topic=%s provider=%s model=%s latency=%sms",
                    user_id,
                    active_topic_slug or "none",
                    primary_prov,
                    primary_mod,
                    latency_ms,
                )
                if isinstance(ai_res, str):
                    raw_text = ai_res
                    payload_str = None
                    res_http_status = None
                    res_finish_reason = None
                    res_diagnostics = None
                else:
                    raw_text = getattr(ai_res, "text", "") or ""
                    raw_payload = getattr(ai_res, "raw_payload", None)
                    payload_str = (
                        json.dumps(raw_payload, ensure_ascii=False)
                        if isinstance(raw_payload, (dict, list))
                        else str(raw_payload) if raw_payload is not None else None
                    )
                    res_http_status = getattr(ai_res, "http_status", None)
                    res_finish_reason = getattr(ai_res, "finish_reason", None)
                    res_diagnostics = getattr(ai_res, "diagnostics", None)

                clean_text = await _process_ai_automation_and_clean_text(
                    session,
                    raw_text=raw_text,
                    user_id=user_id,
                    dialogue_id=active_dialogue_id,
                    scope="topic" if active_topic_slug else "main",
                    topic_slug=active_topic_slug,
                    bot=bot,
                    user_text=alert_user_text or user_prompt,
                    alert_metadata=alert_metadata,
                )
                await record_ai_attempt_log(
                    session=session,
                    user_id=user_id,
                    dialogue_id=active_dialogue_id,
                    topic_slug=active_topic_slug,
                    request_type="chat",
                    provider=primary_prov,
                    model=primary_mod,
                    status="success",
                    latency_ms=latency_ms,
                    prompt_summary=user_prompt[:500] if user_prompt else None,
                    clean_text=clean_text,
                    raw_response=raw_text,
                    request_capture=messages,
                    provider_response_payload=payload_str,
                    http_status=res_http_status,
                    finish_reason=res_finish_reason,
                    diagnostics=res_diagnostics,
                    request_group_id=request_group_id,
                    attempt_no=1,
                    attempt_role="primary",
                )
                await session.commit()
                return clean_text
            except Exception as e:
                latency_ms = int((time.monotonic() - start_ts) * 1000)
                primary_cls, _ = classify_external_error(e, provider=primary_prov)
                logger.error(f"AI Primary Error ({primary_prov}): {e}")
                log_id_1 = await record_ai_attempt_log(
                    session=session,
                    user_id=user_id,
                    dialogue_id=active_dialogue_id,
                    topic_slug=active_topic_slug,
                    request_type="chat",
                    provider=primary_prov,
                    model=primary_mod,
                    status="error",
                    latency_ms=latency_ms,
                    prompt_summary=user_prompt[:500] if user_prompt else None,
                    error_type=type(e).__name__,
                    error_message=str(e),
                    error_classification=primary_cls,
                    http_status=getattr(e, "status_code", getattr(e, "http_status", None)),
                    request_capture=messages,
                    request_group_id=request_group_id,
                    attempt_no=1,
                    attempt_role="primary",
                )
                await session.commit()

                primary_attempt_info = {
                    "provider": primary_prov,
                    "model": primary_mod,
                    "status": "error",
                    "classification": primary_cls,
                }

                # Strictly honor admin configuration for fallback
                fallback_provider, fallback_model, fallback_client = _prepare_fallback_client(ai_config)

                if fallback_client and fallback_provider:
                    fb_start = time.monotonic()
                    try:
                        logger.info("Attempting configured AI fallback to %s (%s)", fallback_provider, fallback_model)
                        if fallback_provider == "KIE":
                            dummy_cfg = AIConfig(
                                provider="KIE",
                                kie_api_key=ai_config.kie_api_key,
                                kie_base_url=ai_config.kie_base_url,
                                temperature=getattr(ai_config, "temperature", 0.7) or 0.7,
                            )
                            fb_ai_res = await _generate_chat_response(dummy_cfg, None, fallback_model, messages)
                        else:
                            dummy_cfg = AIConfig(provider=fallback_provider)
                            fb_ai_res = await _generate_chat_response(dummy_cfg, fallback_client, fallback_model, messages)
                        fb_latency = int((time.monotonic() - fb_start) * 1000)

                        if isinstance(fb_ai_res, str):
                            fb_raw_text = fb_ai_res
                            fb_payload_str = None
                            fb_http_status = None
                            fb_finish_reason = None
                            fb_diagnostics = None
                        else:
                            fb_raw_text = getattr(fb_ai_res, "text", "") or ""
                            fb_raw_payload = getattr(fb_ai_res, "raw_payload", None)
                            fb_payload_str = (
                                json.dumps(fb_raw_payload, ensure_ascii=False)
                                if isinstance(fb_raw_payload, (dict, list))
                                else str(fb_raw_payload) if fb_raw_payload is not None else None
                            )
                            fb_http_status = getattr(fb_ai_res, "http_status", None)
                            fb_finish_reason = getattr(fb_ai_res, "finish_reason", None)
                            fb_diagnostics = getattr(fb_ai_res, "diagnostics", None)

                        fb_clean_text = await _process_ai_automation_and_clean_text(
                            session,
                            raw_text=fb_raw_text,
                            user_id=user_id,
                            dialogue_id=active_dialogue_id,
                            scope="topic" if active_topic_slug else "main",
                            topic_slug=active_topic_slug,
                            bot=bot,
                            user_text=alert_user_text or user_prompt,
                            alert_metadata=alert_metadata,
                        )
                        log_id_2 = await record_ai_attempt_log(
                            session=session,
                            user_id=user_id,
                            dialogue_id=active_dialogue_id,
                            topic_slug=active_topic_slug,
                            request_type="chat",
                            provider=fallback_provider,
                            model=fallback_model,
                            status="success",
                            latency_ms=fb_latency,
                            prompt_summary=user_prompt[:500] if user_prompt else None,
                            clean_text=fb_clean_text,
                            raw_response=fb_raw_text,
                            request_capture=messages,
                            provider_response_payload=fb_payload_str,
                            http_status=fb_http_status,
                            finish_reason=fb_finish_reason,
                            diagnostics=fb_diagnostics,
                            request_group_id=request_group_id,
                            attempt_no=2,
                            attempt_role="fallback",
                        )
                        await session.commit()

                        ai_log_ids = [i for i in (log_id_1, log_id_2) if i is not None]
                        provider_attempts = [
                            primary_attempt_info,
                            {"provider": fallback_provider, "model": fallback_model, "status": "success"},
                        ]
                        try:
                            await send_ai_fallback_used_alert(
                                bot=bot,
                                user_id=user_id,
                                chat_id=effective_chat_id,
                                username=real_username,
                                full_name=effective_name,
                                primary_provider=primary_prov,
                                primary_model=primary_mod,
                                fallback_provider=fallback_provider,
                                fallback_model=fallback_model,
                                primary_error=str(e),
                                ai_log_ids=ai_log_ids,
                                provider_attempts=provider_attempts,
                            )
                        except Exception as alert_exc:
                            logger.warning("Failed to dispatch AI fallback alert: %s", alert_exc)

                        return fb_clean_text
                    except Exception as fb_err:
                        fb_latency = int((time.monotonic() - fb_start) * 1000)
                        fb_cls, _ = classify_external_error(fb_err, provider=fallback_provider)
                        logger.error(f"AI Fallback Error ({fallback_provider}): {fb_err}")
                        log_id_2 = await record_ai_attempt_log(
                            session=session,
                            user_id=user_id,
                            dialogue_id=active_dialogue_id,
                            topic_slug=active_topic_slug,
                            request_type="chat",
                            provider=fallback_provider,
                            model=fallback_model,
                            status="error",
                            latency_ms=fb_latency,
                            prompt_summary=user_prompt[:500] if user_prompt else None,
                            error_type=type(fb_err).__name__,
                            error_message=str(fb_err),
                            error_classification=fb_cls,
                            http_status=getattr(fb_err, "status_code", getattr(fb_err, "http_status", None)),
                            request_capture=messages,
                            request_group_id=request_group_id,
                            attempt_no=2,
                            attempt_role="fallback",
                        )
                        await session.commit()

                        ai_log_ids = [i for i in (log_id_1, log_id_2) if i is not None]
                        provider_attempts = [
                            primary_attempt_info,
                            {"provider": fallback_provider, "model": fallback_model, "status": "error", "classification": fb_cls},
                        ]
                        try:
                            await send_terminal_ai_failure_alert(
                                bot=bot,
                                user_id=user_id,
                                chat_id=effective_chat_id,
                                username=real_username,
                                full_name=effective_name,
                                primary_provider=primary_prov,
                                primary_model=primary_mod,
                                fallback_provider=fallback_provider,
                                fallback_model=fallback_model,
                                exception=fb_err,
                                classification=fb_cls,
                                error_message=f"Primary: {e} | Fallback: {fb_err}",
                                ai_log_ids=ai_log_ids,
                                provider_attempts=provider_attempts,
                            )
                        except Exception as alert_exc:
                            logger.warning("Failed to dispatch terminal AI alert: %s", alert_exc)

                        return f"Произошла ошибка при генерации ответа ({primary_prov} -> {fallback_provider}).\nКод: {fb_err}"

                # Terminal error without fallback
                ai_log_ids = [log_id_1] if log_id_1 is not None else []
                provider_attempts = [primary_attempt_info]
                try:
                    await send_terminal_ai_failure_alert(
                        bot=bot,
                        user_id=user_id,
                        chat_id=effective_chat_id,
                        username=real_username,
                        full_name=effective_name,
                        primary_provider=primary_prov,
                        primary_model=primary_mod,
                        exception=e,
                        classification=primary_cls,
                        error_message=str(e),
                        ai_log_ids=ai_log_ids,
                        provider_attempts=provider_attempts,
                    )
                except Exception as alert_exc:
                    logger.warning("Failed to dispatch terminal AI alert: %s", alert_exc)

                return f"Произошла ошибка при генерации ответа ({primary_prov}).\nКод: {e}"


async def get_ai_response(
    user_id: int,
    user_prompt: str,
    user_name: str,
    current_message_id: int | None = None,
    topic_slug: str | None = None,
    dialogue_id: int | None = None,
    bot: Optional[Any] = None,
    chat_id: Optional[int] = None,
    alert_user_text: str | None = None,
    alert_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    return await get_ai_response_for_topic(
        user_id,
        user_prompt,
        user_name,
        topic_slug=topic_slug,
        dialogue_id=dialogue_id,
        current_message_id=current_message_id,
        bot=bot,
        chat_id=chat_id,
        alert_user_text=alert_user_text,
        alert_metadata=alert_metadata,
    )


async def analyze_image(image_bytes: bytes, prompt: str) -> str:
    async with async_session_maker() as session:
        config = await session.get(AIConfig, 1)
        if not config or config.vision_provider != 'KIE' or not config.kie_api_key:
            raise RuntimeError('Для анализа изображений нужен настроенный KIE.')
        from kie_client import upload_file
        filename = 'photo.jpg'
        url = await upload_file(config.kie_api_key, _kie_upload_base_url(config), image_bytes, filename, 'images')
        return await kie_chat(
            config.kie_api_key,
            _kie_base_url(config),
            config.vision_model or 'gemini-3-flash',
            [
                {'role': 'system', 'content': 'Ты аналитический модуль технического зрения. Твоя задача — объективно, точно и детально описать, что изображено на фотографии: объекты, действия, людей, обстановку, эмоции, текст на картинке, без интерпретаций и советов.'},
                {'role': 'user', 'content': [{'type': 'text', 'text': prompt or 'Опиши детально и фактически, что изображено на фото.'}, {'type': 'image_url', 'image_url': {'url': url}}]},
            ],
            temperature=float(config.temperature or 0.7),
        )


async def generate_image(prompt: str) -> bytes:
    async with async_session_maker() as session:
        config = await session.get(AIConfig, 1)
        if not config or config.image_generation_provider != 'KIE' or not config.kie_api_key:
            raise RuntimeError('Для генерации изображений нужен настроенный KIE.')
        return await kie_generate_image(config.kie_api_key, _kie_base_url(config), config.image_generation_model, prompt)


async def edit_image(image_bytes: bytes, prompt: str) -> bytes:
    async with async_session_maker() as session:
        config = await session.get(AIConfig, 1)
        if not config or config.image_edit_provider != 'KIE' or not config.kie_api_key:
            raise RuntimeError('Для редактирования изображений нужен настроенный KIE.')
        return await kie_edit_image(config.kie_api_key, _kie_base_url(config), _kie_upload_base_url(config), config.image_edit_model, prompt, image_bytes)


async def transcribe_audio(audio_bytes: bytes, filename: str) -> str:
    async with async_session_maker() as session:
        config = await session.get(AIConfig, 1)
        if not config or config.transcription_provider != 'KIE' or not config.kie_api_key:
            raise RuntimeError('Для транскрибации нужен настроенный KIE.')
        try:
            return await kie_transcribe(config.kie_api_key, _kie_base_url(config), _kie_upload_base_url(config), config.kie_transcription_model, audio_bytes, filename)
        except KIEError:
            if not config.openai_api_key:
                raise
            logger.warning("KIE transcription failed; falling back to OpenAI Whisper", exc_info=True)
            client = AsyncOpenAI(api_key=config.openai_api_key)
            try:
                result = await client.audio.transcriptions.create(model="whisper-1", file=(filename, audio_bytes))
                return result.text
            finally:
                await client.close()


class JournalAiResult(str):
    """Result status for Journal daily summary AI operation."""
    SUCCEEDED = "succeeded"
    ALREADY_RUNNING = "already_running"
    FAILED = "failed"
    INSUFFICIENT_TOKENS = "insufficient_tokens"

    def __bool__(self) -> bool:
        return self == "succeeded"


async def _stop_journal_heartbeat(task: Optional[asyncio.Task]) -> None:
    if not task:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def generate_journal_daily_summary(
    entry_id: int,
    user_id: int,
    chat_id: int,
    bot: Optional[Any] = None,
    operation_key: Optional[str] = None,
) -> JournalAiResult:
    """
    Generate daily summary for a completed JournalEntry:
    - Strictly bounded Journal-only context;
    - Primary provider call with AILog;
    - Fallback with AILog & send_ai_fallback_used_alert;
    - Terminal failure with classify_external_error, send_terminal_ai_failure_alert & atomic refund;
    - Owner-Token CAS lease management.
    """
    from journal_service import (
        initiate_journal_ai_operation,
        run_lease_heartbeat,
        finalize_journal_ai_operation_success,
        finalize_journal_ai_operation_failure,
        build_journal_daily_summary_context,
        parse_journal_daily_summary_response,
        InsufficientTokensError,
    )

    op_key = operation_key or f"daily_summary:{entry_id}"

    async with async_session_maker() as session:
        # Require owned and completed entry before any AI operation or billing
        entry = await session.get(JournalEntry, entry_id)
        if not entry or entry.user_id != user_id or entry.status != "completed":
            logger.warning("Rejected daily summary for non-completed or unowned entry %s (user %s)", entry_id, user_id)
            return JournalAiResult(JournalAiResult.FAILED)

        try:
            op_type = "summary_retry" if (operation_key and operation_key.startswith("summary_retry:")) else "daily_summary"
            op, won = await initiate_journal_ai_operation(
                session=session,
                operation_key=op_key,
                user_id=user_id,
                operation_type=op_type,
                entry_id=entry_id,
            )
        except InsufficientTokensError:
            logger.warning("User %s has insufficient tokens for journal daily summary", user_id)
            return JournalAiResult(JournalAiResult.INSUFFICIENT_TOKENS)

        if not won:
            if op.status == "succeeded":
                return JournalAiResult(JournalAiResult.SUCCEEDED)
            if op.status == "running":
                return JournalAiResult(JournalAiResult.ALREADY_RUNNING)
            return JournalAiResult(JournalAiResult.FAILED)

        heartbeat_task = asyncio.create_task(run_lease_heartbeat(op.id, op.owner_token))

        try:
            user_prompt, sys_prompt = await build_journal_daily_summary_context(
                session, entry_id, user_id
            )
        except Exception as ctx_err:
            logger.error("Failed to build daily summary context: %s", ctx_err)
            await _stop_journal_heartbeat(heartbeat_task)
            await finalize_journal_ai_operation_failure(session, op.id, op.owner_token)
            return JournalAiResult(JournalAiResult.FAILED)

        messages = [
            {"role": "system", "content": sys_prompt + GLOBAL_TONE_INSTRUCTION},
            {"role": "user", "content": user_prompt},
        ]

        await _apply_crisis_marker_protection(
            session,
            user_id=user_id,
            user_text=user_prompt,
            bot=bot,
            dialogue_id=1,
            scope="main",
            topic_slug="journal",
            alert_metadata={"journal": "daily_summary"},
        )

        ai_config, _, chat_client, model_name, error_text = await _prepare_clients(session)
        if error_text:
            logger.error("AI configuration error: %s", error_text)
            await _stop_journal_heartbeat(heartbeat_task)
            await finalize_journal_ai_operation_failure(session, op.id, op.owner_token)
            return JournalAiResult(JournalAiResult.FAILED)

        primary_prov = normalize_provider(ai_config.provider)
        primary_mod = model_name or DEFAULT_PROVIDER_MODELS.get(primary_prov, "unknown")
        request_group_id = uuid.uuid4().hex[:12]
        start_ts = time.monotonic()

        user = await session.get(User, user_id)
        real_username = getattr(user, "username", None)
        effective_name = (getattr(user, "name", None) or getattr(user, "first_name", None) or "пользователь").strip()

        async with ai_typing_action(bot, chat_id):
            try:
                logger.info("Journal summary AI request: op_id=%s user_id=%s prov=%s", op.id, user_id, primary_prov)
                ai_res = await _generate_chat_response(ai_config, chat_client, model_name, messages)
                latency_ms = int((time.monotonic() - start_ts) * 1000)
                raw_text = ai_res.text if hasattr(ai_res, "text") else (str(ai_res) if ai_res else "")
                payload_str = (
                    json.dumps(ai_res.raw_payload, ensure_ascii=False)
                    if hasattr(ai_res, "raw_payload") and isinstance(ai_res.raw_payload, (dict, list))
                    else str(getattr(ai_res, "raw_payload", None)) if getattr(ai_res, "raw_payload", None) is not None else None
                )

                await record_ai_attempt_log(
                    session=session,
                    user_id=user_id,
                    dialogue_id=1,
                    topic_slug="journal",
                    request_type="journal_summary",
                    provider=primary_prov,
                    model=primary_mod,
                    status="success",
                    latency_ms=latency_ms,
                    prompt_summary=user_prompt[:500],
                    clean_text=raw_text,
                    raw_response=raw_text,
                    request_capture=messages,
                    provider_response_payload=payload_str,
                    http_status=getattr(ai_res, "http_status", None),
                    finish_reason=getattr(ai_res, "finish_reason", None),
                    diagnostics=getattr(ai_res, "diagnostics", None),
                    request_group_id=request_group_id,
                    attempt_no=1,
                    attempt_role="primary",
                )
                await session.commit()

                await _stop_journal_heartbeat(heartbeat_task)
                parsed_result = parse_journal_daily_summary_response(raw_text)
                success = await finalize_journal_ai_operation_success(
                    session, op.id, op.owner_token, parsed_result
                )
                return JournalAiResult(JournalAiResult.SUCCEEDED if success else JournalAiResult.FAILED)
            except Exception as e:
                latency_ms = int((time.monotonic() - start_ts) * 1000)
                primary_cls, _ = classify_external_error(e, provider=primary_prov)
                logger.error("Journal summary primary error (%s): %s", primary_prov, e)

                log_id_1 = await record_ai_attempt_log(
                    session=session,
                    user_id=user_id,
                    dialogue_id=1,
                    topic_slug="journal",
                    request_type="journal_summary",
                    provider=primary_prov,
                    model=primary_mod,
                    status="error",
                    latency_ms=latency_ms,
                    prompt_summary=user_prompt[:500],
                    error_type=type(e).__name__,
                    error_message=str(e),
                    error_classification=primary_cls,
                    http_status=getattr(e, "status_code", getattr(e, "http_status", None)),
                    request_capture=messages,
                    request_group_id=request_group_id,
                    attempt_no=1,
                    attempt_role="primary",
                )
                await session.commit()

                primary_attempt_info = {
                    "provider": primary_prov,
                    "model": primary_mod,
                    "status": "error",
                    "classification": primary_cls,
                }

                fallback_provider, fallback_model, fallback_client = _prepare_fallback_client(ai_config)

                if fallback_client and fallback_provider:
                    fb_start = time.monotonic()
                    try:
                        logger.info("Attempting journal summary fallback to %s (%s)", fallback_provider, fallback_model)
                        if fallback_provider == "KIE":
                            dummy_cfg = AIConfig(
                                provider="KIE",
                                kie_api_key=ai_config.kie_api_key,
                                kie_base_url=ai_config.kie_base_url,
                                temperature=float(getattr(ai_config, "temperature", 0.7) or 0.7),
                            )
                            fb_ai_res = await _generate_chat_response(dummy_cfg, None, fallback_model, messages)
                        else:
                            dummy_cfg = AIConfig(provider=fallback_provider)
                            fb_ai_res = await _generate_chat_response(dummy_cfg, fallback_client, fallback_model, messages)

                        fb_latency = int((time.monotonic() - fb_start) * 1000)
                        fb_raw_text = fb_ai_res.text if hasattr(fb_ai_res, "text") else (str(fb_ai_res) if fb_ai_res else "")
                        fb_payload_str = (
                            json.dumps(fb_ai_res.raw_payload, ensure_ascii=False)
                            if hasattr(fb_ai_res, "raw_payload") and isinstance(fb_ai_res.raw_payload, (dict, list))
                            else str(getattr(fb_ai_res, "raw_payload", None)) if getattr(fb_ai_res, "raw_payload", None) is not None else None
                        )

                        log_id_2 = await record_ai_attempt_log(
                            session=session,
                            user_id=user_id,
                            dialogue_id=1,
                            topic_slug="journal",
                            request_type="journal_summary",
                            provider=fallback_provider,
                            model=fallback_model,
                            status="success",
                            latency_ms=fb_latency,
                            prompt_summary=user_prompt[:500],
                            clean_text=fb_raw_text,
                            raw_response=fb_raw_text,
                            request_capture=messages,
                            provider_response_payload=fb_payload_str,
                            http_status=getattr(fb_ai_res, "http_status", None),
                            finish_reason=getattr(fb_ai_res, "finish_reason", None),
                            diagnostics=getattr(fb_ai_res, "diagnostics", None),
                            request_group_id=request_group_id,
                            attempt_no=2,
                            attempt_role="fallback",
                        )
                        await session.commit()

                        ai_log_ids = [i for i in (log_id_1, log_id_2) if i is not None]
                        provider_attempts = [
                            primary_attempt_info,
                            {"provider": fallback_provider, "model": fallback_model, "status": "success"},
                        ]
                        try:
                            await send_ai_fallback_used_alert(
                                bot=bot,
                                user_id=user_id,
                                chat_id=chat_id,
                                username=real_username,
                                full_name=effective_name,
                                request_type="journal_summary",
                                primary_provider=primary_prov,
                                primary_model=primary_mod,
                                fallback_provider=fallback_provider,
                                fallback_model=fallback_model,
                                primary_error=str(e),
                                ai_log_ids=ai_log_ids,
                                provider_attempts=provider_attempts,
                            )
                        except Exception as alert_exc:
                            logger.warning("Failed to dispatch journal AI fallback alert: %s", alert_exc)

                        await _stop_journal_heartbeat(heartbeat_task)
                        parsed_result = parse_journal_daily_summary_response(fb_raw_text)
                        success = await finalize_journal_ai_operation_success(
                            session, op.id, op.owner_token, parsed_result
                        )
                        return JournalAiResult(JournalAiResult.SUCCEEDED if success else JournalAiResult.FAILED)
                    except Exception as fb_err:
                        fb_latency = int((time.monotonic() - fb_start) * 1000)
                        fb_cls, _ = classify_external_error(fb_err, provider=fallback_provider)
                        logger.error("Journal summary fallback error (%s): %s", fallback_provider, fb_err)

                        log_id_2 = await record_ai_attempt_log(
                            session=session,
                            user_id=user_id,
                            dialogue_id=1,
                            topic_slug="journal",
                            request_type="journal_summary",
                            provider=fallback_provider,
                            model=fallback_model,
                            status="error",
                            latency_ms=fb_latency,
                            prompt_summary=user_prompt[:500],
                            error_type=type(fb_err).__name__,
                            error_message=str(fb_err),
                            error_classification=fb_cls,
                            http_status=getattr(fb_err, "status_code", getattr(fb_err, "http_status", None)),
                            request_capture=messages,
                            request_group_id=request_group_id,
                            attempt_no=2,
                            attempt_role="fallback",
                        )
                        await session.commit()

                        await _stop_journal_heartbeat(heartbeat_task)
                        await finalize_journal_ai_operation_failure(session, op.id, op.owner_token)

                        ai_log_ids = [i for i in (log_id_1, log_id_2) if i is not None]
                        provider_attempts = [
                            primary_attempt_info,
                            {"provider": fallback_provider, "model": fallback_model, "status": "error", "classification": fb_cls},
                        ]
                        try:
                            await send_terminal_ai_failure_alert(
                                bot=bot,
                                user_id=user_id,
                                chat_id=chat_id,
                                username=real_username,
                                full_name=effective_name,
                                primary_provider=primary_prov,
                                primary_model=primary_mod,
                                fallback_provider=fallback_provider,
                                fallback_model=fallback_model,
                                error_message=f"Primary: {e} | Fallback: {fb_err}",
                                ai_log_ids=ai_log_ids,
                                exception=fb_err,
                                classification=fb_cls,
                                request_type="journal_summary",
                                provider_attempts=provider_attempts,
                            )
                        except Exception as alert_exc:
                            logger.warning("Failed to dispatch journal terminal AI alert: %s", alert_exc)
                        return JournalAiResult(JournalAiResult.FAILED)
                else:
                    await _stop_journal_heartbeat(heartbeat_task)
                    await finalize_journal_ai_operation_failure(session, op.id, op.owner_token)

                    ai_log_ids = [log_id_1] if log_id_1 is not None else []
                    provider_attempts = [primary_attempt_info]
                    try:
                        await send_terminal_ai_failure_alert(
                            bot=bot,
                            user_id=user_id,
                            chat_id=chat_id,
                            username=real_username,
                            full_name=effective_name,
                            primary_provider=primary_prov,
                            primary_model=primary_mod,
                            fallback_provider="none",
                            fallback_model="none",
                            error_message=str(e),
                            details="Fallback is disabled or not configured",
                            ai_log_ids=ai_log_ids,
                            exception=e,
                            classification=primary_cls,
                            request_type="journal_summary",
                            provider_attempts=provider_attempts,
                        )
                    except Exception as alert_exc:
                        logger.warning("Failed to dispatch journal terminal AI alert: %s", alert_exc)
                    return JournalAiResult(JournalAiResult.FAILED)


async def generate_journal_dynamics_analysis(
    user_id: int,
    days: int,
    chat_id: int,
    token8: str,
    bot: Optional[Any] = None,
) -> tuple[bool, str]:
    """
    Generate dynamics analysis (7 or 21 days) for user:
    - Bounded to completed journal entries in the window;
    - Count usable completed entries BEFORE any operation or billing;
    - Stores result_text in JournalAiOperation;
    - Owner-Token CAS lease management and exactly-once token billing;
    - Full fallback provider support matching daily summary semantics.
    """
    from journal_service import (
        initiate_journal_ai_operation,
        run_lease_heartbeat,
        finalize_journal_ai_operation_success,
        finalize_journal_ai_operation_failure,
        build_journal_dynamics_context,
        InsufficientTokensError,
    )
    from time_utils import msk_now

    today_iso = msk_now().date().isoformat()
    op_key = f"dynamics_{days}:{user_id}:{today_iso}:{token8}"

    async with async_session_maker() as session:
        # 1. Build/validate context and count completed entries BEFORE billing
        user_prompt, sys_prompt, count = await build_journal_dynamics_context(session, user_id, days)
        if count < 2:
            return False, f"⚠️ Для анализа динамики за {days} дн. требуется как минимум 2 завершённые записи (сейчас: {count})."

        # 2. Only when count >= 2, create/claim JournalAiOperation and debit tokens
        try:
            op, won = await initiate_journal_ai_operation(
                session=session,
                operation_key=op_key,
                user_id=user_id,
                operation_type=f"dynamics_{days}",
                entry_id=None,
            )
        except InsufficientTokensError:
            return False, "⚠️ Недостаточно токенов для выполнения анализа динамики."

        if not won:
            if op.status == "succeeded" and op.result_text:
                return True, op.result_text
            return False, "⏳ Анализ динамики уже формируется или был обработан ранее."

        heartbeat_task = asyncio.create_task(run_lease_heartbeat(op.id, op.owner_token))

        messages = [
            {"role": "system", "content": sys_prompt + GLOBAL_TONE_INSTRUCTION},
            {"role": "user", "content": user_prompt},
        ]
        request_capture = {"period_days": days, "messages": messages}

        await _apply_crisis_marker_protection(
            session,
            user_id=user_id,
            user_text=user_prompt,
            bot=bot,
            dialogue_id=1,
            scope="main",
            topic_slug="journal",
            alert_metadata={"journal": f"dynamics_{days}"},
        )

        ai_config, _, chat_client, model_name, error_text = await _prepare_clients(session)
        if error_text:
            await _stop_journal_heartbeat(heartbeat_task)
            await finalize_journal_ai_operation_failure(session, op.id, op.owner_token)
            return False, error_text

        primary_prov = normalize_provider(ai_config.provider)
        primary_mod = model_name or DEFAULT_PROVIDER_MODELS.get(primary_prov, "unknown")
        request_group_id = uuid.uuid4().hex[:12]
        start_ts = time.monotonic()

        user = await session.get(User, user_id)
        real_username = getattr(user, "username", None)
        effective_name = (getattr(user, "name", None) or getattr(user, "first_name", None) or "пользователь").strip()

        async with ai_typing_action(bot, chat_id):
            try:
                logger.info("Journal dynamics AI request: op_id=%s user_id=%s days=%s prov=%s", op.id, user_id, days, primary_prov)
                ai_res = await _generate_chat_response(ai_config, chat_client, model_name, messages)
                latency_ms = int((time.monotonic() - start_ts) * 1000)
                raw_text = ai_res.text if hasattr(ai_res, "text") else (str(ai_res) if ai_res else "")
                payload_str = (
                    json.dumps(ai_res.raw_payload, ensure_ascii=False)
                    if hasattr(ai_res, "raw_payload") and isinstance(ai_res.raw_payload, (dict, list))
                    else str(getattr(ai_res, "raw_payload", None)) if getattr(ai_res, "raw_payload", None) is not None else None
                )

                await record_ai_attempt_log(
                    session=session,
                    user_id=user_id,
                    dialogue_id=1,
                    topic_slug="journal",
                    request_type="journal_dynamics",
                    provider=primary_prov,
                    model=primary_mod,
                    status="success",
                    latency_ms=latency_ms,
                    prompt_summary=user_prompt[:500],
                    clean_text=raw_text,
                    raw_response=raw_text,
                    request_capture=request_capture,
                    provider_response_payload=payload_str,
                    http_status=getattr(ai_res, "http_status", None),
                    finish_reason=getattr(ai_res, "finish_reason", None),
                    diagnostics={"period_days": days, **(getattr(ai_res, "diagnostics", {}) or {})},
                    request_group_id=request_group_id,
                    attempt_no=1,
                    attempt_role="primary",
                )
                await session.commit()

                await _stop_journal_heartbeat(heartbeat_task)
                finalized = await finalize_journal_ai_operation_success(session, op.id, op.owner_token, raw_text)
                if not finalized:
                    return False, "⚠️ Запрос уже завершён или был восстановлен после сбоя. Повтори действие при необходимости."
                return True, raw_text
            except Exception as e:
                latency_ms = int((time.monotonic() - start_ts) * 1000)
                primary_cls, _ = classify_external_error(e, provider=primary_prov)
                logger.error("Journal dynamics primary error (%s): %s", primary_prov, e)

                log_id_1 = await record_ai_attempt_log(
                    session=session,
                    user_id=user_id,
                    dialogue_id=1,
                    topic_slug="journal",
                    request_type="journal_dynamics",
                    provider=primary_prov,
                    model=primary_mod,
                    status="error",
                    latency_ms=latency_ms,
                    prompt_summary=user_prompt[:500],
                    error_type=type(e).__name__,
                    error_message=str(e),
                    error_classification=primary_cls,
                    http_status=getattr(e, "status_code", getattr(e, "http_status", None)),
                    request_capture=request_capture,
                    diagnostics={"period_days": days},
                    request_group_id=request_group_id,
                    attempt_no=1,
                    attempt_role="primary",
                )
                await session.commit()

                primary_attempt_info = {
                    "provider": primary_prov,
                    "model": primary_mod,
                    "status": "error",
                    "classification": primary_cls,
                }

                fallback_provider, fallback_model, fallback_client = _prepare_fallback_client(ai_config)

                if fallback_client and fallback_provider:
                    fb_start = time.monotonic()
                    try:
                        logger.info("Attempting journal dynamics fallback to %s (%s)", fallback_provider, fallback_model)
                        if fallback_provider == "KIE":
                            dummy_cfg = AIConfig(
                                provider="KIE",
                                kie_api_key=ai_config.kie_api_key,
                                kie_base_url=ai_config.kie_base_url,
                                temperature=float(getattr(ai_config, "temperature", 0.7) or 0.7),
                            )
                            fb_ai_res = await _generate_chat_response(dummy_cfg, None, fallback_model, messages)
                        else:
                            dummy_cfg = AIConfig(provider=fallback_provider)
                            fb_ai_res = await _generate_chat_response(dummy_cfg, fallback_client, fallback_model, messages)

                        fb_latency = int((time.monotonic() - fb_start) * 1000)
                        fb_raw_text = fb_ai_res.text if hasattr(fb_ai_res, "text") else (str(fb_ai_res) if fb_ai_res else "")
                        fb_payload_str = (
                            json.dumps(fb_ai_res.raw_payload, ensure_ascii=False)
                            if hasattr(fb_ai_res, "raw_payload") and isinstance(fb_ai_res.raw_payload, (dict, list))
                            else str(getattr(fb_ai_res, "raw_payload", None)) if getattr(fb_ai_res, "raw_payload", None) is not None else None
                        )

                        log_id_2 = await record_ai_attempt_log(
                            session=session,
                            user_id=user_id,
                            dialogue_id=1,
                            topic_slug="journal",
                            request_type="journal_dynamics",
                            provider=fallback_provider,
                            model=fallback_model,
                            status="success",
                            latency_ms=fb_latency,
                            prompt_summary=user_prompt[:500],
                            clean_text=fb_raw_text,
                            raw_response=fb_raw_text,
                            request_capture=request_capture,
                            provider_response_payload=fb_payload_str,
                            http_status=getattr(fb_ai_res, "http_status", None),
                            finish_reason=getattr(fb_ai_res, "finish_reason", None),
                            diagnostics={"period_days": days, **(getattr(fb_ai_res, "diagnostics", {}) or {})},
                            request_group_id=request_group_id,
                            attempt_no=2,
                            attempt_role="fallback",
                        )
                        await session.commit()

                        ai_log_ids = [i for i in (log_id_1, log_id_2) if i is not None]
                        provider_attempts = [
                            primary_attempt_info,
                            {"provider": fallback_provider, "model": fallback_model, "status": "success"},
                        ]
                        try:
                            await send_ai_fallback_used_alert(
                                bot=bot,
                                user_id=user_id,
                                chat_id=chat_id,
                                username=real_username,
                                full_name=effective_name,
                                request_type="journal_dynamics",
                                primary_provider=primary_prov,
                                primary_model=primary_mod,
                                fallback_provider=fallback_provider,
                                fallback_model=fallback_model,
                                primary_error=str(e),
                                ai_log_ids=ai_log_ids,
                                provider_attempts=provider_attempts,
                            )
                        except Exception as alert_exc:
                            logger.warning("Failed to dispatch journal dynamics AI fallback alert: %s", alert_exc)

                        await _stop_journal_heartbeat(heartbeat_task)
                        finalized = await finalize_journal_ai_operation_success(session, op.id, op.owner_token, fb_raw_text)
                        if not finalized:
                            return False, "⚠️ Запрос уже завершён или был восстановлен после сбоя. Повтори действие при необходимости."
                        return True, fb_raw_text
                    except Exception as fb_err:
                        fb_latency = int((time.monotonic() - fb_start) * 1000)
                        fb_cls, _ = classify_external_error(fb_err, provider=fallback_provider)
                        logger.error("Journal dynamics fallback error (%s): %s", fallback_provider, fb_err)

                        log_id_2 = await record_ai_attempt_log(
                            session=session,
                            user_id=user_id,
                            dialogue_id=1,
                            topic_slug="journal",
                            request_type="journal_dynamics",
                            provider=fallback_provider,
                            model=fallback_model,
                            status="error",
                            latency_ms=fb_latency,
                            prompt_summary=user_prompt[:500],
                            error_type=type(fb_err).__name__,
                            error_message=str(fb_err),
                            error_classification=fb_cls,
                            http_status=getattr(fb_err, "status_code", getattr(fb_err, "http_status", None)),
                            request_capture=request_capture,
                            diagnostics={"period_days": days},
                            request_group_id=request_group_id,
                            attempt_no=2,
                            attempt_role="fallback",
                        )
                        await session.commit()

                        await _stop_journal_heartbeat(heartbeat_task)
                        await finalize_journal_ai_operation_failure(session, op.id, op.owner_token)

                        ai_log_ids = [i for i in (log_id_1, log_id_2) if i is not None]
                        provider_attempts = [
                            primary_attempt_info,
                            {"provider": fallback_provider, "model": fallback_model, "status": "error", "classification": fb_cls},
                        ]
                        try:
                            await send_terminal_ai_failure_alert(
                                bot=bot,
                                user_id=user_id,
                                chat_id=chat_id,
                                username=real_username,
                                full_name=effective_name,
                                primary_provider=primary_prov,
                                primary_model=primary_mod,
                                fallback_provider=fallback_provider,
                                fallback_model=fallback_model,
                                error_message=f"Primary: {e} | Fallback: {fb_err}",
                                ai_log_ids=ai_log_ids,
                                exception=fb_err,
                                classification=fb_cls,
                                request_type="journal_dynamics",
                                provider_attempts=provider_attempts,
                            )
                        except Exception as alert_exc:
                            logger.warning("Failed to dispatch journal dynamics terminal AI alert: %s", alert_exc)
                        return False, "⚠️ Не удалось сформировать анализ динамики. Попробуйте позже."
                else:
                    await _stop_journal_heartbeat(heartbeat_task)
                    await finalize_journal_ai_operation_failure(session, op.id, op.owner_token)

                    ai_log_ids = [log_id_1] if log_id_1 is not None else []
                    provider_attempts = [primary_attempt_info]
                    try:
                        await send_terminal_ai_failure_alert(
                            bot=bot,
                            user_id=user_id,
                            chat_id=chat_id,
                            username=real_username,
                            full_name=effective_name,
                            primary_provider=primary_prov,
                            primary_model=primary_mod,
                            fallback_provider="none",
                            fallback_model="none",
                            error_message=str(e),
                            details="Fallback is disabled or not configured",
                            ai_log_ids=ai_log_ids,
                            exception=e,
                            classification=primary_cls,
                            request_type="journal_dynamics",
                            provider_attempts=provider_attempts,
                        )
                    except Exception as alert_exc:
                        logger.warning("Failed to dispatch journal dynamics terminal AI alert: %s", alert_exc)
                    return False, "⚠️ Не удалось сформировать анализ динамики. Попробуйте позже."
