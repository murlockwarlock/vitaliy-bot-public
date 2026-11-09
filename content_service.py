"""Canonical Content Runtime Service for Vitaliy Bot.

Provides a unified runtime API for all dynamic user-facing texts, buttons, and links.
Guarantees that database content always takes precedence over code fallbacks,
and that admin edits immediately reflect in runtime without requiring a bot restart.
"""

from __future__ import annotations

import logging
import time
from typing import Any, NamedTuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import Content, ContentMedia, async_session_maker

logger = logging.getLogger(__name__)


class CachedContent(NamedTuple):
    key: str
    text_content: str | None
    button_text: str | None
    link_url: str | None
    content_order: str | None
    cached_at: float


# In-memory runtime cache with short TTL (2.0s) so external process/session DB changes
# are automatically picked up while in-process edits are instant via invalidate_content_cache.
_CONTENT_CACHE: dict[str, tuple[CachedContent | None, float]] = {}
CACHE_TTL_SECONDS: float = 2.0


def invalidate_content_cache(key: str | None = None) -> None:
    """Invalidate cached content items so changes immediately reflect in runtime."""
    global _CONTENT_CACHE
    if key is None:
        _CONTENT_CACHE.clear()
    else:
        _CONTENT_CACHE.pop(key, None)


async def get_content(
    key: str,
    *,
    fallback_text: str | None = None,
    fallback_btn: str | None = None,
    session: AsyncSession | None = None,
    with_media: bool = True,
    force_refresh: bool = False,
) -> Content | None:
    """Fetch content item by key.

    If session is provided or force_refresh is True, queries the DB directly.
    Otherwise uses short-TTL in-memory cache to balance performance with multi-process safety.
    """
    global _CONTENT_CACHE
    now = time.time()

    if session is not None or with_media or force_refresh:
        async def _query(s: AsyncSession) -> Content | None:
            opts = [selectinload(Content.media)] if with_media else []
            return await s.get(Content, key, options=opts)

        if session is not None:
            item = await _query(session)
        else:
            async with async_session_maker() as s:
                item = await _query(s)

        if not with_media and session is None:
            if item is not None:
                cached = CachedContent(
                    key=item.key,
                    text_content=item.text_content,
                    button_text=item.button_text,
                    link_url=item.link_url,
                    content_order=item.content_order,
                    cached_at=now,
                )
                _CONTENT_CACHE[key] = (cached, now)
            else:
                _CONTENT_CACHE[key] = (None, now)
    else:
        if key in _CONTENT_CACHE:
            cached_tuple, cached_at = _CONTENT_CACHE[key]
            if now - cached_at < CACHE_TTL_SECONDS:
                if cached_tuple is None:
                    item = None
                else:
                    item = Content(
                        key=cached_tuple.key,
                        text_content=cached_tuple.text_content,
                        button_text=cached_tuple.button_text,
                        link_url=cached_tuple.link_url,
                        content_order=cached_tuple.content_order,
                    )
            else:
                async with async_session_maker() as s:
                    item = await s.get(Content, key)
                if item is not None:
                    cached = CachedContent(
                        key=item.key,
                        text_content=item.text_content,
                        button_text=item.button_text,
                        link_url=item.link_url,
                        content_order=item.content_order,
                        cached_at=now,
                    )
                    _CONTENT_CACHE[key] = (cached, now)
                else:
                    _CONTENT_CACHE[key] = (None, now)
        else:
            async with async_session_maker() as s:
                item = await s.get(Content, key)
            if item is not None:
                cached = CachedContent(
                    key=item.key,
                    text_content=item.text_content,
                    button_text=item.button_text,
                    link_url=item.link_url,
                    content_order=item.content_order,
                    cached_at=now,
                )
                _CONTENT_CACHE[key] = (cached, now)
            else:
                _CONTENT_CACHE[key] = (None, now)

    if item is None and (fallback_text is not None or fallback_btn is not None):
        return Content(key=key, text_content=fallback_text, button_text=fallback_btn)
    return item


async def get_text(
    key: str,
    fallback: str = "",
    *,
    session: AsyncSession | None = None,
    force_refresh: bool = False,
) -> str:
    """Return text content for key.

    Database value ALWAYS takes precedence over fallback.
    Fallback is only used if row does not exist or text_content is empty.
    """
    item = await get_content(key, session=session, with_media=False, force_refresh=force_refresh)
    if item is not None and item.text_content and item.text_content.strip():
        return item.text_content
    return fallback


async def get_button(
    key: str,
    fallback: str = "",
    *,
    session: AsyncSession | None = None,
    force_refresh: bool = False,
) -> str:
    """Return button text for key with fallback."""
    item = await get_content(key, session=session, with_media=False, force_refresh=force_refresh)
    if item is not None and item.button_text and item.button_text.strip():
        return item.button_text
    return fallback


async def get_link(
    key: str,
    fallback: str = "",
    *,
    session: AsyncSession | None = None,
    force_refresh: bool = False,
) -> str:
    """Return link URL for key with fallback."""
    item = await get_content(key, session=session, with_media=False, force_refresh=force_refresh)
    if item is not None and item.link_url and item.link_url.strip():
        return item.link_url
    return fallback


async def save_content_item(
    key: str,
    *,
    text_content: str | None = None,
    button_text: str | None = None,
    link_url: str | None = None,
    content_order: str | None = None,
    session: AsyncSession | None = None,
) -> Content:
    """Persist content update to DB and invalidate runtime cache immediately."""
    async def _save(s: AsyncSession, should_commit: bool) -> Content:
        item = await s.get(Content, key, options=[selectinload(Content.media)])
        if item is None:
            item = Content(key=key)
            s.add(item)
        if text_content is not None:
            item.text_content = text_content
        if button_text is not None:
            item.button_text = button_text
        if link_url is not None:
            item.link_url = link_url
        if content_order is not None:
            item.content_order = content_order
        if should_commit:
            await s.commit()
            await s.refresh(item)
        else:
            await s.flush()
        return item

    if session is not None:
        saved = await _save(session, should_commit=False)
    else:
        async with async_session_maker() as s:
            saved = await _save(s, should_commit=True)

    invalidate_content_cache(key)
    return saved
