from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaPhoto, InputMediaVideo, LinkPreviewOptions
from sqlalchemy.exc import OperationalError

from database import MenuAnchor, async_session_maker


def _target_context(target, bot):
    message = getattr(target, "message", None) or target
    chat = getattr(message, "chat", None)
    user = getattr(target, "from_user", None) or getattr(message, "from_user", None)
    return (
        bot or getattr(message, "bot", None) or getattr(target, "bot", None),
        getattr(chat, "id", None) or getattr(user, "id", None),
        getattr(user, "id", None) or getattr(chat, "id", None),
        message,
    )


async def _save_anchor(user_id: int, chat_id: int, message_id: int, kind: str, media_file_id: str | None):
    try:
        async with async_session_maker() as session:
            anchor = await session.get(MenuAnchor, user_id)
            if not anchor:
                anchor = MenuAnchor(
                    user_id=user_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    message_kind=kind,
                    media_file_id=media_file_id,
                )
                session.add(anchor)
            else:
                anchor.chat_id = chat_id
                anchor.message_id = message_id
                anchor.message_kind = kind
                anchor.media_file_id = media_file_id
            await session.commit()
    except OperationalError as exc:
        if "menu_anchors" not in str(exc).lower():
            raise


def _message_media_id(message, kind: str) -> str | None:
    items = getattr(message, kind, None)
    if not items:
        return None
    item = items[-1] if isinstance(items, (list, tuple)) else items
    return getattr(item, "file_id", None) or (item if isinstance(item, str) else None)


def _message_kind(message) -> str:
    content_type = getattr(message, "content_type", None)
    if content_type in {"text", "photo", "video"}:
        return content_type
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "video", None):
        return "video"
    return "text"


async def render_main_menu_anchor(target, *, text: str, media: list, reply_markup, bot=None):
    bot, chat_id, user_id, current_message = _target_context(target, bot)
    if not chat_id or not user_id:
        return None

    media_item = media[0] if media else None
    desired_kind = media_item.file_type if media_item else "text"
    desired_media_id = getattr(media_item, "file_id", None)
    if desired_kind not in {"photo", "video"}:
        desired_kind = "text"

    if not bot:
        current_id = getattr(current_message, "message_id", None)
        current_content_type = getattr(current_message, "content_type", None)
        current_kind = "text" if current_content_type in {None, "text"} else current_content_type
        if (
            desired_kind == "text"
            and isinstance(current_id, int)
            and current_kind == "text"
            and callable(getattr(current_message, "edit_text", None))
        ):
            await current_message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return current_id
        return None

    try:
        async with async_session_maker() as session:
            anchor = await session.get(MenuAnchor, user_id)
    except OperationalError as exc:
        if "menu_anchors" not in str(exc).lower():
            raise
        anchor = None

    is_callback_target = getattr(target, "message", None) is not None
    anchor_message_id = anchor.message_id if anchor and anchor.chat_id == chat_id else None
    anchor_kind = anchor.message_kind if anchor_message_id else None

    async def disable_old_keyboard(message_id: int):
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception:
            pass

    current_id = getattr(current_message, "message_id", None)
    if not isinstance(current_id, int):
        current_id = None
    current_kind = _message_kind(current_message)
    current_media_id = _message_media_id(current_message, current_kind)

    # A callback's current message is the live screen. When its media differs,
    # replace the media instead of editing only the old caption.
    if is_callback_target and current_id and current_kind == desired_kind:
        try:
            if desired_kind == "text" and callable(getattr(current_message, "edit_text", None)):
                await current_message.edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            elif desired_kind in {"photo", "video"} and current_media_id != desired_media_id:
                media_obj = (
                    InputMediaPhoto(media=desired_media_id, caption=text[:1024], parse_mode="HTML")
                    if desired_kind == "photo"
                    else InputMediaVideo(media=desired_media_id, caption=text[:1024], parse_mode="HTML")
                )
                await bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=current_id,
                    media=media_obj,
                    reply_markup=reply_markup,
                )
            elif desired_kind in {"photo", "video"} and callable(getattr(current_message, "edit_caption", None)):
                await current_message.edit_caption(
                    caption=text[:1024],
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            elif desired_kind == "text":
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=current_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            else:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=current_id,
                    caption=text[:1024],
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            await _save_anchor(user_id, chat_id, current_id, desired_kind, desired_media_id)
            return current_id
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                return current_id
            pass
        except Exception:
            pass

    if not is_callback_target:
        anchor_message_id = None
        anchor_kind = None

    if is_callback_target and current_id and current_kind != desired_kind:
        await disable_old_keyboard(current_id)
        if anchor_message_id and anchor_message_id != current_id:
            await disable_old_keyboard(anchor_message_id)
        anchor_message_id = None
        anchor_kind = None

    if anchor_message_id and anchor_kind in {"photo", "video"} and desired_kind in {"photo", "video"}:
        try:
            if anchor_kind != desired_kind or anchor.media_file_id != desired_media_id:
                media_obj = (
                    InputMediaPhoto(media=desired_media_id, caption=text[:1024], parse_mode="HTML")
                    if desired_kind == "photo"
                    else InputMediaVideo(media=desired_media_id, caption=text[:1024], parse_mode="HTML")
                )
                await bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=anchor_message_id,
                    media=media_obj,
                    reply_markup=reply_markup,
                )
            else:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=anchor_message_id,
                    caption=text[:1024],
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            await _save_anchor(user_id, chat_id, anchor_message_id, desired_kind, desired_media_id)
            return anchor_message_id
        except TelegramBadRequest:
            pass
        except Exception:
            pass

    if anchor_message_id and anchor_kind == "text" and desired_kind == "text":
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=anchor_message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            await _save_anchor(user_id, chat_id, anchor_message_id, "text", None)
            return anchor_message_id
        except Exception:
            pass

    if anchor_message_id:
        await disable_old_keyboard(anchor_message_id)

    if desired_kind == "photo":
        if len(text) <= 1024:
            sent = await bot.send_photo(chat_id, desired_media_id, caption=text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await bot.send_photo(chat_id, desired_media_id)
            sent = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
            desired_kind = "text"
    elif desired_kind == "video":
        if len(text) <= 1024:
            sent = await bot.send_video(chat_id, desired_media_id, caption=text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await bot.send_video(chat_id, desired_media_id)
            sent = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
            desired_kind = "text"
    else:
        sent = await bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    message_id = getattr(sent, "message_id", None)
    if isinstance(message_id, int):
        await _save_anchor(
            user_id,
            chat_id,
            message_id,
            desired_kind,
            desired_media_id if desired_kind in {"photo", "video"} else None,
        )
    return message_id
