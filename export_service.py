import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import Message, Topic, User, UserSubscription, FunnelState

DIALOGUE_MARKER_TEMPLATE = "--- Начало нового диалога №{dialogue_id} ---"


def serialize_human_dialogue_history(records: Iterable[tuple[int, str, str, Optional[datetime]]]) -> str:
    """Serializes dialogue messages into a human-readable string with clear boundaries.

    records: (dialogue_id, role, content, timestamp)
    """
    lines: List[str] = []
    current_dialogue_id = object()

    for dialogue_id, role, content, ts in records:
        if dialogue_id != current_dialogue_id:
            if lines:
                lines.append("")
            lines.append(DIALOGUE_MARKER_TEMPLATE.format(dialogue_id=dialogue_id))
            current_dialogue_id = dialogue_id

        ts_str = ts.strftime("%d.%m.%Y %H:%M:%S") if ts else "N/A"
        clean_role = (role or "unknown").strip().upper()
        clean_content = (content or "").rstrip("\n")
        lines.append(f"[{ts_str}] {clean_role}: {clean_content}")

    return "\n".join(lines)


async def export_user_dialogue_history(
    session: AsyncSession,
    user_id: int,
    *,
    anonymize: bool = False,
    topic_slug: Optional[str] = None,
) -> str:
    """Exports all dialogues for a given user into human-readable text."""
    query = select(Message).where(Message.user_id == user_id)
    if topic_slug:
        topic = await session.scalar(select(Topic).where(Topic.slug == topic_slug))
        if topic:
            query = query.where(Message.topic_id == topic.id)

    query = query.order_by(Message.dialogue_id.asc(), Message.id.asc())
    result = await session.execute(query)
    messages = result.scalars().all()

    records = []
    user_label = "USER" if not anonymize else "ANONYMOUS_USER"
    for m in messages:
        role_label = user_label if m.role == "user" else "BOT"
        records.append((m.dialogue_id, role_label, m.content, m.timestamp))

    header = f"=== История диалогов пользователя {user_id if not anonymize else 'ANONYMOUS'} ===\n\n"
    return header + serialize_human_dialogue_history(records)


def metadata_export_entry(
    user: User,
    *,
    anonymize: bool = False,
    anonymous_index: int = 1,
    subscription: Optional[UserSubscription] = None,
    funnel_state: Optional[FunnelState] = None,
) -> Dict[str, Any]:
    """Serializes a single user's platform metadata with optional anonymization."""
    if anonymize:
        user_info = {
            "label": f"user_{anonymous_index}",
            "is_admin": False,
            "created_at": user.created_at.isoformat() if getattr(user, 'created_at', None) else None,
        }
    else:
        user_info = {
            "label": str(user.id),
            "id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "name": getattr(user, 'name', None),
            "is_admin": user.is_admin,
            "created_at": user.created_at.isoformat() if getattr(user, 'created_at', None) else None,
            "last_activity_at": user.last_activity_at.isoformat() if getattr(user, 'last_activity_at', None) else None,
        }

    sub_info = None
    if subscription:
        sub_info = {
            "has_subscription": True,
            "start_date": subscription.start_date.isoformat() if subscription.start_date else None,
            "end_date": subscription.end_date.isoformat() if subscription.end_date else None,
            "auto_renewal": subscription.auto_renewal,
            "payment_provider": subscription.payment_provider,
        }

    funnel_info = None
    if funnel_state:
        funnel_info = {
            "stage": funnel_state.current_stage,
            "is_active": funnel_state.is_active,
            "updated_at": funnel_state.updated_at.isoformat() if getattr(funnel_state, 'updated_at', None) else None,
        }

    return {
        "user_info": user_info,
        "referral": {
            "referral_balance": user.referral_balance or 0,
            "referrer_id": user.referrer_id if not anonymize else None,
        },
        "subscription": sub_info,
        "funnel": funnel_info,
    }


async def export_multiple_users_metadata(
    session: AsyncSession,
    user_ids: Optional[Sequence[int]] = None,
    *,
    anonymize: bool = False,
) -> List[Dict[str, Any]]:
    """Exports metadata for multiple users."""
    query = select(User)
    if user_ids:
        query = query.where(User.id.in_(user_ids))
    query = query.order_by(User.id.asc())

    users = (await session.execute(query)).scalars().all()
    entries: List[Dict[str, Any]] = []

    for idx, user in enumerate(users, start=1):
        sub = await session.scalar(
            select(UserSubscription).where(UserSubscription.user_id == user.id)
        )
        funnel = await session.scalar(
            select(FunnelState).where(FunnelState.user_id == user.id)
        )
        entries.append(metadata_export_entry(
            user,
            anonymize=anonymize,
            anonymous_index=idx,
            subscription=sub,
            funnel_state=funnel,
        ))

    return entries


async def export_users_metadata_to_json(
    session: AsyncSession,
    user_ids: Optional[Sequence[int]] = None,
    *,
    anonymize: bool = False,
) -> str:
    """Exports users metadata as formatted JSON string."""
    entries = await export_multiple_users_metadata(session, user_ids=user_ids, anonymize=anonymize)
    return json.dumps(entries, ensure_ascii=False, indent=2)
