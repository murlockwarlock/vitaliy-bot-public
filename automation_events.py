import asyncio
import html
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from aiogram import Bot
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from automation_engine import (
    AutomationActionExecution,
    AutomationConversationState,
    AutomationHandler,
    merge_metadata,
)
from config import IS_TESTING
from database import Topic, User, async_session_maker, get_all_admin_ids
from time_utils import msk_now

logger = logging.getLogger("automation_events")

SAFETY_ALERT_EVENTS = {
    "crisis_detected",
    "clinical_detected",
    "parse_failed",
    "offer_requested",
    "signup_clicked",
}
_pending_automation_tasks: set[asyncio.Task] = set()
_pending_test_automation_jobs: list[tuple[Bot, dict[str, Any]]] = []


def _parse_recipient_ids(value: Any) -> set[int]:
    if value is None:
        return set()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = value.replace(",", " ").split()
    if not isinstance(value, (list, tuple, set)):
        return set()
    result = set()
    for item in value:
        try:
            recipient_id = int(item)
        except (TypeError, ValueError):
            continue
        if recipient_id:
            result.add(recipient_id)
    return result


def _render_alert_template(template: str, context: dict[str, Any]) -> str:
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace("{" + key + "}", html.escape(str(value or "")))
    return rendered


async def _dispatch_automation_events_in_new_session(bot: Bot, kwargs: dict[str, Any]) -> None:
    session = async_session_maker()
    try:
        await dispatch_automation_events(bot, session, **kwargs)
    finally:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        await session.close()


def schedule_automation_events(bot: Bot, **kwargs: Any) -> None:
    if IS_TESTING:
        _pending_test_automation_jobs.append((bot, kwargs))
        return
    task = asyncio.create_task(_dispatch_automation_events_in_new_session(bot, kwargs))
    _pending_automation_tasks.add(task)

    def _finish(completed_task: asyncio.Task) -> None:
        _pending_automation_tasks.discard(completed_task)
        if completed_task.cancelled():
            return
        error = completed_task.exception()
        if error:
            logger.error(
                "Background automation dispatch failed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(_finish)


async def wait_for_pending_automation_events() -> None:
    if _pending_test_automation_jobs:
        jobs = list(_pending_test_automation_jobs)
        _pending_test_automation_jobs.clear()
        results = await asyncio.gather(
            *(_dispatch_automation_events_in_new_session(bot, kwargs) for bot, kwargs in jobs),
            return_exceptions=True,
        )
        for error in results:
            if isinstance(error, Exception):
                logger.error("Test automation dispatch failed: %s", error)

    current_loop = asyncio.get_running_loop()
    current_loop_tasks = []
    for task in tuple(_pending_automation_tasks):
        task_loop = task.get_loop()
        if task_loop is not current_loop:
            _pending_automation_tasks.discard(task)
            if not task.done() and not task_loop.is_closed():
                task.cancel()
            continue
        if not task.done():
            current_loop_tasks.append(task)
    if not current_loop_tasks:
        return
    await asyncio.gather(*current_loop_tasks, return_exceptions=True)


async def dispatch_automation_events(
    bot: Bot,
    session: AsyncSession,
    *,
    user_id: int,
    dialogue_id: int = 1,
    scope: str = "main",
    topic_slug: Optional[str] = None,
    current_step: Optional[str] = None,
    events: Sequence[str] = (),
    metadata: Optional[Dict[str, Any]] = None,
) -> List[int]:
    """Matches active automation handlers and executes actions idempotently.

    - Protects every action execution with a unique key.
    - If worker/process restarts or multiple events fire concurrently,
      each action runs at most once.
    - Returns list of executed handler IDs.
    """
    if not events and not current_step:
        return []

    metadata = dict(metadata or {})
    user = await session.get(User, user_id)
    topic_name = None
    if topic_slug:
        topic_name = await session.scalar(select(Topic.name).where(Topic.slug == topic_slug))

    # Find matching active handlers
    query = select(AutomationHandler).where(AutomationHandler.is_active == 1)
    handlers = (await session.execute(query)).scalars().all()

    executed_handler_ids: List[int] = []

    for handler in handlers:
        if handler.topic_slug and handler.topic_slug != topic_slug:
            continue

        matched_event: Optional[str] = None
        normalized_events = {str(event).strip().lower(): str(event).strip() for event in events if str(event).strip()}
        if handler.event_name and handler.event_name.strip().lower() in normalized_events:
            matched_event = normalized_events[handler.event_name.strip().lower()]
        elif handler.trigger_step and handler.trigger_step == current_step:
            matched_event = f"step:{current_step}"

        if not matched_event:
            continue

        normalized_matched_event = matched_event.strip().lower()
        event_scope = scope
        event_dialogue_id = dialogue_id
        if normalized_matched_event == "crisis_detected" and topic_slug == "funnel_demo":
            event_scope = "funnel"
            event_dialogue_id = 1

        if handler.scope != 'global' and handler.scope != event_scope:
            continue

        # Idempotency check via unique execution_key
        event_key = "crisis_detected" if normalized_matched_event == "crisis_detected" else matched_event
        if normalized_matched_event == "crisis_detected":
            crisis_episode_id = user.crisis_detected_at.isoformat() if user and user.crisis_detected_at else "legacy"
            event_key = f"{event_key}:{crisis_episode_id}"
        exec_key = f"auto-exec:{handler.id}:{user_id}:{event_dialogue_id}:{event_key}"

        execution = await session.scalar(
            select(AutomationActionExecution).where(AutomationActionExecution.execution_key == exec_key)
        )

        now_dt = datetime.utcnow()
        claim_token = uuid.uuid4().hex
        if execution:
            if execution.status in ("delivered", "claimed"):
                logger.debug("Automation action %s already %s, skipping.", exec_key, execution.status)
                continue

            if execution.status == "delivering":
                cutoff = now_dt - timedelta(seconds=300)
                last_ts = execution.updated_at or execution.created_at
                if last_ts and last_ts >= cutoff:
                    # In-flight fresh lease held by another worker
                    continue
                # Stale lease recovery (> 300s): True CAS transition to new delivering lease
                cond = (
                    (AutomationActionExecution.id == execution.id)
                    & (AutomationActionExecution.status == "delivering")
                )
                if execution.claim_token is not None:
                    cond = cond & (AutomationActionExecution.claim_token == execution.claim_token)
                elif execution.updated_at is not None:
                    cond = cond & (AutomationActionExecution.updated_at == execution.updated_at)

                res = await session.execute(
                    update(AutomationActionExecution)
                    .where(cond)
                    .values(
                        status="delivering",
                        claim_token=claim_token,
                        updated_at=now_dt,
                        error_message="stale_lease_recovered",
                    )
                )
                if not (res.rowcount and res.rowcount > 0):
                    continue
                await session.commit()
            elif execution.status == "failed":
                # Allow retry for failed status via True CAS
                cond = (
                    (AutomationActionExecution.id == execution.id)
                    & (AutomationActionExecution.status == "failed")
                )
                if execution.claim_token is not None:
                    cond = cond & (AutomationActionExecution.claim_token == execution.claim_token)
                elif execution.updated_at is not None:
                    cond = cond & (AutomationActionExecution.updated_at == execution.updated_at)

                res = await session.execute(
                    update(AutomationActionExecution)
                    .where(cond)
                    .values(
                        status="delivering",
                        claim_token=claim_token,
                        updated_at=now_dt,
                        error_message=None,
                    )
                )
                if not (res.rowcount and res.rowcount > 0):
                    continue
                await session.commit()
            else:
                continue
        else:
            execution = AutomationActionExecution(
                execution_key=exec_key,
                user_id=user_id,
                handler_id=handler.id,
                event_name=matched_event,
                status="delivering",
                claim_token=claim_token,
                created_at=now_dt,
                updated_at=now_dt,
            )
            try:
                async with session.begin_nested():
                    session.add(execution)
                    await session.flush()
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.debug("Automation action %s already claimed/executed concurrently, skipping.", exec_key)
                continue
            except Exception as exc:
                await session.rollback()
                logger.warning("Error claiming AutomationActionExecution: %s", exc)
                continue

        # Parse action payload
        action_payload: Dict[str, Any] = {}
        try:
            if handler.action_payload_json:
                action_payload = json.loads(handler.action_payload_json)
        except Exception:
            pass

        # Execute action
        # Crash-window semantics:
        # The claim record is committed as 'delivering' BEFORE invoking external side effects.
        # If process crashes after bot.send_message() but before status='delivered' is committed,
        # the stale lease will expire after 300s and recover. This guarantees at-least-once execution.
        try:
            if handler.action_type == "send_admin_message":
                text_template = action_payload.get("text", f"🔔 Автоматизация [{handler.name}]: событие {matched_event} для пользователя {user_id}")
                template_context = {
                    "user_id": user_id,
                    "event": matched_event,
                    "step": current_step or "",
                    "scope": event_scope,
                    "username": f"@{user.username}" if user and user.username else "",
                    "first_name": (user.first_name or user.name) if user else "",
                    "user_text": metadata.get("user_text") or metadata.get("text") or "",
                    "practice": metadata.get("practice") or metadata.get("practice_name") or "",
                    "topic": metadata.get("topic") or topic_name or topic_slug or "",
                    "time": metadata.get("time") or msk_now().strftime("%d.%m.%Y %H:%M:%S"),
                }
                rendered_text = _render_alert_template(text_template, template_context)

                raw_recipients = getattr(handler, "recipient_ids_json", None)
                if raw_recipients is not None:
                    configured_recipients = _parse_recipient_ids(raw_recipients)
                else:
                    configured_recipients = _parse_recipient_ids(action_payload.get("recipient_ids"))
                    if not configured_recipients and matched_event.strip().lower() not in SAFETY_ALERT_EVENTS:
                        configured_recipients = await get_all_admin_ids()

                if not configured_recipients:
                    raise RuntimeError("no_alert_recipients_configured")

                delivered_count = 0
                failed_recipients = []
                for aid in configured_recipients:
                    try:
                        await bot.send_message(aid, rendered_text, parse_mode="HTML")
                        delivered_count += 1
                    except Exception as err:
                        failed_recipients.append(aid)
                        logger.error("Failed to send automation alert to recipient %s: %s", aid, err)
                if failed_recipients and delivered_count:
                    logger.warning(
                        "Automation alert partially delivered event=%s delivered=%s failed=%s",
                        matched_event,
                        delivered_count,
                        failed_recipients,
                    )
                if not delivered_count:
                    raise RuntimeError(f"alert_delivery_failed:{failed_recipients}")

            elif handler.action_type == "send_user_message":
                from crisis_safety import automatic_send_allowed

                if not await automatic_send_allowed(session, user_id, f"automation:{matched_event}"):
                    logger.info(
                        "automation_user_message_skipped user_id=%s event=%s",
                        user_id,
                        matched_event,
                    )
                else:
                    text_template = action_payload.get("text", "")
                    rendered_text = _render_alert_template(
                        text_template,
                        {
                            "user_id": user_id,
                            "event": matched_event,
                            "step": current_step or "",
                            "scope": event_scope,
                            "username": f"@{user.username}" if user and user.username else "",
                            "first_name": (user.first_name or user.name) if user else "",
                            "user_text": metadata.get("user_text") or metadata.get("text") or "",
                            "practice": metadata.get("practice") or metadata.get("practice_name") or "",
                            "topic": metadata.get("topic") or topic_name or topic_slug or "",
                            "time": metadata.get("time") or msk_now().strftime("%d.%m.%Y %H:%M:%S"),
                        },
                    )
                    if rendered_text and bot:
                        await bot.send_message(user_id, rendered_text, parse_mode="HTML")

            elif handler.action_type == "set_metadata":
                new_meta = action_payload.get("metadata", {})
                if new_meta:
                    conv_state = await session.scalar(
                        select(AutomationConversationState).where(
                            AutomationConversationState.user_id == user_id,
                            AutomationConversationState.dialogue_id == event_dialogue_id,
                            AutomationConversationState.scope == event_scope,
                            AutomationConversationState.topic_slug == topic_slug,
                        )
                    )
                    if conv_state:
                        existing = {}
                        try:
                            existing = json.loads(conv_state.metadata_json or '{}')
                        except Exception:
                            pass
                        merged = merge_metadata(existing, new_meta)
                        conv_state.metadata_json = json.dumps(merged, ensure_ascii=False)

            await session.execute(
                update(AutomationActionExecution)
                .where(
                    AutomationActionExecution.execution_key == exec_key,
                    AutomationActionExecution.claim_token == claim_token,
                )
                .values(
                    status="delivered",
                    error_message=None,
                    updated_at=datetime.utcnow(),
                )
            )
            await session.commit()

            executed_handler_ids.append(handler.id)
            logger.info("Successfully executed automation handler id=%s name='%s' key=%s", handler.id, handler.name, exec_key)
        except Exception as exc:
            logger.error("Error executing action for handler id=%s: %s", handler.id, exc, exc_info=True)
            try:
                await session.execute(
                    update(AutomationActionExecution)
                    .where(
                        AutomationActionExecution.execution_key == exec_key,
                        AutomationActionExecution.claim_token == claim_token,
                    )
                    .values(
                        status="failed",
                        error_message=str(exc)[:1000],
                        updated_at=datetime.utcnow(),
                    )
                )
                await session.commit()
            except Exception:
                await session.rollback()

    return executed_handler_ids
