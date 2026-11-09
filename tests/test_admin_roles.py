from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Chat, Message, User as TelegramUser
from sqlalchemy import delete, select

import handlers
import keyboards as kb
from admin_roles import (
    AdminPermission,
    AdminRole,
    get_admin_role,
    role_has_permission,
    user_has_permission,
)
from config import OWNER_IDS
from database import Content, ContentMedia, Habit, Message as DBMessage, User, async_session_maker, get_all_admin_ids, init_db


FULL_ADMIN_ID = 731000001
CONTENT_EDITOR_ID = 731000002
SUPPORT_ID = 731000003


def _callbacks(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def _callback(user_id: int, data: str):
    callback = AsyncMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = TelegramUser(id=user_id, is_bot=False, first_name="Admin")
    callback.answer = AsyncMock()
    callback.message = AsyncMock(spec=Message)
    callback.message.chat = Chat(id=user_id, type="private")
    callback.message.message_id = 1
    return callback


async def _save_admin(user_id: int, role: AdminRole | None):
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None:
            user = User(id=user_id, first_name=f"Admin {user_id}")
            session.add(user)
        user.is_admin = True
        user.admin_role = role.value if role else None
        await session.commit()


async def _load_role(user_id: int):
    async with async_session_maker() as session:
        return await session.scalar(select(User.admin_role).where(User.id == user_id))


class AdminRolesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        await _save_admin(FULL_ADMIN_ID, AdminRole.FULL_ADMIN)
        await _save_admin(CONTENT_EDITOR_ID, AdminRole.CONTENT_EDITOR)
        await _save_admin(SUPPORT_ID, AdminRole.SUPPORT)

    async def test_role_permissions_and_role_filtered_admin_menu(self):
        assert await get_admin_role(FULL_ADMIN_ID) == AdminRole.FULL_ADMIN
        assert await get_admin_role(CONTENT_EDITOR_ID) == AdminRole.CONTENT_EDITOR
        assert await get_admin_role(SUPPORT_ID) == AdminRole.SUPPORT
        assert await get_admin_role(OWNER_IDS[0]) == AdminRole.OWNER

        assert role_has_permission(AdminRole.OWNER, AdminPermission.PAYMENTS)
        assert role_has_permission(AdminRole.FULL_ADMIN, AdminPermission.SYSTEM)
        assert role_has_permission(AdminRole.CONTENT_EDITOR, AdminPermission.CONTENT)
        assert not role_has_permission(AdminRole.CONTENT_EDITOR, AdminPermission.CLIENTS)
        assert role_has_permission(AdminRole.SUPPORT, AdminPermission.CLIENTS)
        assert not role_has_permission(AdminRole.SUPPORT, AdminPermission.MAILING)

        for permission in AdminPermission:
            assert await user_has_permission(OWNER_IDS[0], permission)
            assert await user_has_permission(FULL_ADMIN_ID, permission)
        assert await user_has_permission(CONTENT_EDITOR_ID, AdminPermission.PANEL)
        assert await user_has_permission(CONTENT_EDITOR_ID, AdminPermission.CONTENT)
        assert not await user_has_permission(CONTENT_EDITOR_ID, AdminPermission.CLIENTS)
        assert not await user_has_permission(CONTENT_EDITOR_ID, AdminPermission.PAYMENTS)
        assert await user_has_permission(SUPPORT_ID, AdminPermission.PANEL)
        assert await user_has_permission(SUPPORT_ID, AdminPermission.CLIENTS)
        assert not await user_has_permission(SUPPORT_ID, AdminPermission.CONTENT)
        assert not await user_has_permission(SUPPORT_ID, AdminPermission.SYSTEM)

        content_callbacks = _callbacks(kb.admin_panel_keyboard(AdminRole.CONTENT_EDITOR))
        assert "admin_content_list" in content_callbacks
        assert "admin_topics_list" in content_callbacks
        assert "admin_test_sets_list" in content_callbacks
        assert "admin_payment_settings" not in content_callbacks
        assert "admin_mailing" not in content_callbacks
        assert "admin_clients_list_0" not in content_callbacks

        support_callbacks = _callbacks(kb.admin_panel_keyboard(AdminRole.SUPPORT))
        assert "admin_clients_list_0" in support_callbacks
        assert "admin_ai_logs_0_all" in support_callbacks
        assert "admin_content_list" not in support_callbacks
        assert "admin_payment_settings" not in support_callbacks
        assert "admin_mailing" not in support_callbacks
        assert "automation_menu" not in support_callbacks

    async def test_legacy_admin_migration_keeps_full_access(self):
        await _save_admin(FULL_ADMIN_ID, None)
        assert await _load_role(FULL_ADMIN_ID) is None

        await init_db()

        assert await _load_role(FULL_ADMIN_ID) == AdminRole.FULL_ADMIN.value
        assert await user_has_permission(FULL_ADMIN_ID, AdminPermission.PAYMENTS)

    async def test_direct_payment_callback_is_denied_to_content_editor(self):
        callback = _callback(CONTENT_EDITOR_ID, "admin_payment_settings")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as send:
            await handlers.admin_payment_settings(callback)

        callback.answer.assert_awaited_once()
        assert callback.answer.call_args.kwargs["show_alert"] is True
        send.assert_not_awaited()

    async def test_direct_content_callback_is_denied_to_support(self):
        callback = _callback(SUPPORT_ID, "admin_content_list")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as send:
            await handlers.admin_content_list(callback)

        callback.answer.assert_awaited_once()
        assert callback.answer.call_args.kwargs["show_alert"] is True
        send.assert_not_awaited()

    async def test_direct_content_media_delete_is_denied_to_support(self):
        content_key = "role_guard_media"
        async with async_session_maker() as session:
            content = await session.get(Content, content_key)
            if content is None:
                session.add(Content(key=content_key, text_content="Protected media"))
            await session.execute(delete(ContentMedia).where(ContentMedia.content_key == content_key))
            session.add(ContentMedia(content_key=content_key, file_type="photo", file_id="protected"))
            await session.commit()

        callback = _callback(SUPPORT_ID, f"del_cont_media_main:{content_key}")
        with patch("handlers.edit_content_start", new_callable=AsyncMock) as edit:
            await handlers.del_cont_media(callback, AsyncMock())

        callback.answer.assert_awaited_once()
        assert callback.answer.call_args.kwargs["show_alert"] is True
        edit.assert_not_awaited()
        async with async_session_maker() as session:
            media = (
                await session.execute(
                    select(ContentMedia).where(ContentMedia.content_key == content_key)
                )
            ).scalars().all()
        assert len(media) == 1

    async def test_support_cannot_run_destructive_client_resets(self):
        target_id = 731000005
        async with async_session_maker() as session:
            target = await session.get(User, target_id)
            if target is None:
                session.add(User(id=target_id, first_name="Reset target"))
            await session.execute(delete(DBMessage).where(DBMessage.user_id == target_id))
            await session.execute(delete(Habit).where(Habit.user_id == target_id))
            session.add(DBMessage(user_id=target_id, role="user", content="Protected history"))
            session.add(Habit(user_id=target_id, name="Protected habit"))
            await session.commit()

        for callback_data, handler in (
            (f"ureset_dialog_{target_id}", handlers.ureset_dialog),
            (f"ureset_habits_{target_id}", handlers.ureset_habits),
        ):
            callback = _callback(SUPPORT_ID, callback_data)
            with patch("handlers.admin_user_resets", new_callable=AsyncMock) as refresh:
                await handler(callback)
            callback.answer.assert_awaited_once()
            assert callback.answer.call_args.kwargs["show_alert"] is True
            refresh.assert_not_awaited()

        async with async_session_maker() as session:
            messages = (
                await session.execute(select(DBMessage).where(DBMessage.user_id == target_id))
            ).scalars().all()
            habits = (
                await session.execute(select(Habit).where(Habit.user_id == target_id))
            ).scalars().all()
        assert len(messages) == 1
        assert len(habits) == 1

    async def test_self_role_escalation_is_rejected(self):
        callback = _callback(FULL_ADMIN_ID, f"admin_role_set_{FULL_ADMIN_ID}_content_editor")
        await handlers.set_admin_role(callback)

        callback.answer.assert_awaited_once()
        assert callback.answer.call_args.kwargs["show_alert"] is True
        assert await _load_role(FULL_ADMIN_ID) == AdminRole.FULL_ADMIN.value

    async def test_owner_can_assign_restricted_role(self):
        target_id = 731000004
        async with async_session_maker() as session:
            session.add(User(id=target_id, first_name="New support"))
            await session.commit()

        callback = _callback(OWNER_IDS[0], f"admin_role_set_{target_id}_support")
        with patch("handlers.admin_manage_admins", new_callable=AsyncMock):
            await handlers.set_admin_role(callback)

        callback.answer.assert_awaited_once()
        assert await get_admin_role(target_id) == AdminRole.SUPPORT

    async def test_owner_cannot_be_removed(self):
        owner_id = OWNER_IDS[0]
        callback = _callback(FULL_ADMIN_ID, f"del_admin_{owner_id}")
        await handlers.delete_admin_handler(callback, AsyncMock())

        callback.answer.assert_awaited_once()
        assert callback.answer.call_args.kwargs["show_alert"] is True
        assert await get_admin_role(owner_id) == AdminRole.OWNER

    async def test_admin_registry_and_critical_access_remain_available(self):
        admin_ids = await get_all_admin_ids()
        assert {FULL_ADMIN_ID, CONTENT_EDITOR_ID, SUPPORT_ID}.issubset(admin_ids)
        assert await handlers.is_admin_actor(FULL_ADMIN_ID)
        assert await handlers.is_admin_actor(SUPPORT_ID)
        assert not await user_has_permission(SUPPORT_ID, AdminPermission.PAYMENTS)
