import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Base

logger = logging.getLogger("automation_engine")

DATA_BLOCK_RE = re.compile(
    r"(?P<xml><DATA>(?P<xml_content>.*?)</DATA>)|(?P<square>\[DATA\](?P<square_content>.*?)\[/DATA\])",
    re.DOTALL | re.IGNORECASE,
)
SELF_CLOSING_DATA_RE = re.compile(r"<DATA\s*/>", re.IGNORECASE)
UNMATCHED_DATA_OPENER_RE = re.compile(
    r"(?P<fence>(?:```[a-zA-Z0-9_-]*\s*)?)(?P<tag><DATA>(?!.*</DATA>))",
    re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True)
class AutomationApplyResult:
    state_changed: bool
    previous_step: Optional[str]
    current_step: Optional[str]
    events: Tuple[str, ...]
    metadata: Dict[str, Any]


class AutomationConversationState(Base):
    """Current automation state per user and dialogue scope."""
    __tablename__ = 'automation_conversation_states'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    dialogue_id = Column(Integer, default=1, nullable=False, index=True)
    scope = Column(String, default='main', nullable=False, index=True)  # 'main', 'topic', 'funnel', 'global'
    topic_slug = Column(String, nullable=True, index=True)
    current_step = Column(String, nullable=True, index=True)
    metadata_json = Column(Text, default='{}', nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint('user_id', 'dialogue_id', 'scope', 'topic_slug', name='uq_automation_state_scope'),
    )


class AutomationStepTransition(Base):
    """Append-only audit trail of state transitions for analytics and funnel drop-off tracking."""
    __tablename__ = 'automation_step_transitions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    dialogue_id = Column(Integer, default=1, nullable=False)
    scope = Column(String, default='main', nullable=False, index=True)
    topic_slug = Column(String, nullable=True, index=True)
    previous_step = Column(String, nullable=True)
    current_step = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class AutomationHandler(Base):
    """Generic event-driven action handler."""
    __tablename__ = 'automation_handlers'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    scope = Column(String, default='global', nullable=False, index=True)  # 'global', 'main', 'topic', 'funnel'
    topic_slug = Column(String, nullable=True, index=True)
    event_name = Column(String, nullable=True, index=True)
    trigger_step = Column(String, nullable=True, index=True)
    action_type = Column(String, nullable=False)  # 'send_admin_message', 'send_user_message', 'set_metadata'
    action_payload_json = Column(Text, default='{}', nullable=False)
    recipient_ids_json = Column(Text, nullable=True)
    is_active = Column(Integer, default=1, nullable=False)  # 1 = active, 0 = inactive
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AutomationActionExecution(Base):
    """Idempotency log for executed automation actions."""
    __tablename__ = 'automation_action_executions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_key = Column(String, unique=True, nullable=False, index=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    handler_id = Column(Integer, ForeignKey('automation_handlers.id', ondelete='CASCADE'), nullable=False, index=True)
    event_name = Column(String, nullable=True)
    status = Column(String, default='claimed', nullable=False)
    claim_token = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)


def extract_service_data(text: Optional[str]) -> Tuple[str, List[Dict[str, Any]], int]:
    """Extracts <DATA> JSON blocks and strips them from user-visible text.

    Returns: (clean_visible_text, list_of_payload_dicts, invalid_block_count)
    Never breaks conversation flow even if JSON is corrupted.
    """
    raw_text = text or ""
    blocks: List[Dict[str, Any]] = []
    invalid_count = 0

    for match in DATA_BLOCK_RE.finditer(raw_text):
        content = match.group("xml_content") or match.group("square_content") or ""
        content = content.strip()
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                blocks.append(payload)
            else:
                invalid_count += 1
        except Exception:
            invalid_count += 1

    # Strip out data blocks
    visible = DATA_BLOCK_RE.sub("", raw_text)
    visible = SELF_CLOSING_DATA_RE.sub("", visible)

    unmatched = UNMATCHED_DATA_OPENER_RE.search(visible)
    if unmatched:
        visible = visible[:unmatched.start()]
        invalid_count += 1

    visible = re.sub(r"</?DATA(?:\s[^>]*)?>|\[/?DATA\]", "", visible, flags=re.IGNORECASE)
    visible = re.sub(r"[ \t]+\n", "\n", visible)
    visible = re.sub(r"\n{3,}", "\n\n", visible).strip()

    return visible, blocks, invalid_count


def merge_metadata(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merges dictionary metadata."""
    result = dict(existing)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_metadata(result[key], value)
        else:
            result[key] = value
    return result


async def apply_automation_data(
    session: AsyncSession,
    *,
    user_id: int,
    dialogue_id: int = 1,
    scope: str = "main",
    topic_slug: Optional[str] = None,
    payload: Dict[str, Any],
) -> AutomationApplyResult:
    """Applies extracted automation data to conversation state and records step transitions.

    - current_step is updated if changed, appending an immutable transition record.
    - metadata is deeply merged.
    - events are returned for downstream handler dispatch.
    """
    state = await session.scalar(
        select(AutomationConversationState).where(
            AutomationConversationState.user_id == user_id,
            AutomationConversationState.dialogue_id == dialogue_id,
            AutomationConversationState.scope == scope,
            AutomationConversationState.topic_slug == topic_slug,
        )
    )

    if not state:
        state = AutomationConversationState(
            user_id=user_id,
            dialogue_id=dialogue_id,
            scope=scope,
            topic_slug=topic_slug,
            current_step=None,
            metadata_json='{}',
            updated_at=datetime.utcnow(),
        )
        session.add(state)
        await session.flush()

    previous_step = state.current_step

    # Extract new step from payload
    incoming_step: Optional[str] = None
    current_state_dict = payload.get("current_state")
    if isinstance(current_state_dict, dict):
        step_val = current_state_dict.get("current_step") or current_state_dict.get("step")
        if step_val:
            incoming_step = str(step_val).strip()
    elif "current_step" in payload:
        incoming_step = str(payload["current_step"]).strip()

    state_changed = False
    if incoming_step and incoming_step != previous_step:
        state.current_step = incoming_step
        state_changed = True
        # Record append-only transition for analytics
        transition = AutomationStepTransition(
            user_id=user_id,
            dialogue_id=dialogue_id,
            scope=scope,
            topic_slug=topic_slug,
            previous_step=previous_step,
            current_step=incoming_step,
            created_at=datetime.utcnow(),
        )
        session.add(transition)

    # Merge incoming metadata
    existing_meta = {}
    try:
        existing_meta = json.loads(state.metadata_json or '{}')
    except Exception:
        pass

    incoming_meta = payload.get("metadata")
    if isinstance(incoming_meta, dict):
        merged = merge_metadata(existing_meta, incoming_meta)
        state.metadata_json = json.dumps(merged, ensure_ascii=False)
        existing_meta = merged

    state.updated_at = datetime.utcnow()

    # Extract events
    raw_events = payload.get("events") or []
    events_list: List[str] = []
    if isinstance(raw_events, list):
        for e in raw_events:
            if isinstance(e, str) and e.strip():
                events_list.append(e.strip())
            elif isinstance(e, dict) and e.get("name"):
                events_list.append(str(e["name"]).strip())

    return AutomationApplyResult(
        state_changed=state_changed,
        previous_step=previous_step,
        current_step=state.current_step,
        events=tuple(events_list),
        metadata=existing_meta,
    )


async def get_stage_analytics(
    session: AsyncSession,
    *,
    scope: Optional[str] = None,
    topic_slug: Optional[str] = None,
) -> Dict[str, Any]:
    """Calculates funnel/stage analytics:

    - Total transitions count
    - Count of users who reached each step
    - Count of users currently sitting on each step
    """
    q_transitions = select(
        AutomationStepTransition.current_step,
        func.count(AutomationStepTransition.id).label("transitions_count"),
        func.count(func.distinct(AutomationStepTransition.user_id)).label("unique_users_reached"),
    )
    if scope:
        q_transitions = q_transitions.where(AutomationStepTransition.scope == scope)
    if topic_slug:
        q_transitions = q_transitions.where(AutomationStepTransition.topic_slug == topic_slug)
    q_transitions = q_transitions.group_by(AutomationStepTransition.current_step)

    res_transitions = await session.execute(q_transitions)
    reached_stats = {
        row.current_step: {
            "transitions": row.transitions_count,
            "users_reached": row.unique_users_reached,
        }
        for row in res_transitions
    }

    q_current = select(
        AutomationConversationState.current_step,
        func.count(AutomationConversationState.user_id).label("current_users"),
    ).where(AutomationConversationState.current_step.is_not(None))
    if scope:
        q_current = q_current.where(AutomationConversationState.scope == scope)
    if topic_slug:
        q_current = q_current.where(AutomationConversationState.topic_slug == topic_slug)
    q_current = q_current.group_by(AutomationConversationState.current_step)

    res_current = await session.execute(q_current)
    current_stats = {row.current_step: row.current_users for row in res_current}

    all_steps = sorted(set(reached_stats.keys()) | set(current_stats.keys()))
    stages = []
    for step in all_steps:
        r = reached_stats.get(step, {"transitions": 0, "users_reached": 0})
        stages.append({
            "step": step,
            "users_reached": r["users_reached"],
            "transitions": r["transitions"],
            "users_currently_at_step": current_stats.get(step, 0),
        })

    return {
        "scope": scope or "all",
        "topic_slug": topic_slug or "all",
        "total_active_stages": len(stages),
        "stages": stages,
    }
