from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery
from sqlalchemy import delete

import handlers
import keyboards as kb
from admin_roles import AdminPermission, AdminRole, permission_for_callback, role_has_permission
from database import BotConfig, Content, User, async_session_maker, init_db


class AdminNavigationAuditTests(IsolatedAsyncioTestCase):
    admin_id = 981000101
    content_key = "admin_navigation_audit_content"

    async def asyncSetUp(self):
        await init_db()
        self.storage = MemoryStorage()
        async with async_session_maker() as session:
            user = await session.get(User, self.admin_id)
            if user is None:
                session.add(User(id=self.admin_id, first_name="Navigation admin", is_admin=True))
            else:
                user.is_admin = True
            await session.execute(delete(Content).where(Content.key == self.content_key))
            await session.commit()

    async def asyncTearDown(self):
        async with async_session_maker() as session:
            await session.execute(delete(Content).where(Content.key == self.content_key))
            await session.commit()
        await self.storage.close()

    def state(self) -> FSMContext:
        return FSMContext(
            storage=self.storage,
            key=StorageKey(bot_id=99101, chat_id=self.admin_id, user_id=self.admin_id),
        )

    def callback(self, data: str, user_id: int | None = None):
        user_id = user_id or self.admin_id
        message = SimpleNamespace(
            chat=SimpleNamespace(id=user_id),
            message_id=1,
            content_type="text",
            answer=AsyncMock(return_value=SimpleNamespace(message_id=2)),
            edit_text=AsyncMock(),
            delete=AsyncMock(),
        )
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=user_id),
            message=message,
            bot=AsyncMock(),
            answer=AsyncMock(),
        )

    async def test_reminder_editor_returns_to_source_and_keeps_legacy_callback(self):
        state = self.state()
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as send:
            await handlers.admin_reminders_settings(self.callback("admin_reminders_settings"), state)

        markup = send.call_args.args[3]
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        contextual = "edit_cont_main:main_reminder_first"
        self.assertIn(contextual, callbacks)

        editor_callback = self.callback(contextual)
        await handlers.edit_content_start(editor_callback, state)
        data = await state.get_data()
        self.assertEqual(data["return_callback"], "admin_reminders_settings")
        editor_markup = editor_callback.message.answer.call_args.kwargs["reply_markup"]
        self.assertIn(
            "admin_reminders_settings",
            [
                button.callback_data
                for row in editor_markup.inline_keyboard
                for button in row
                if button.callback_data
            ],
        )

        await state.clear()
        legacy_callback = self.callback("edit_cont_main:main_reminder_first")
        await handlers.edit_content_start(legacy_callback, state)
        legacy_data = await state.get_data()
        self.assertIsNone(legacy_data["return_callback"])
        legacy_markup = legacy_callback.message.answer.call_args.kwargs["reply_markup"]
        self.assertIn(
            "admin_cat_main",
            [
                button.callback_data
                for row in legacy_markup.inline_keyboard
                for button in row
                if button.callback_data
            ],
        )

    async def test_content_save_uses_the_actual_reminder_entry_point(self):
        state = self.state()
        await state.set_state(handlers.AdminStates.edit_content)
        await state.update_data(
            key=self.content_key,
            category="main",
            return_callback="admin_reminders_settings",
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.admin_id),
            chat=SimpleNamespace(id=self.admin_id),
            text="Обновлённый текст",
            html_text="Обновлённый текст",
            caption=None,
            photo=None,
            video=None,
            message_id=3,
            delete=AsyncMock(),
            answer=AsyncMock(),
            bot=AsyncMock(),
        )

        with patch("handlers.clean_chat_after_input", new_callable=AsyncMock), patch(
            "handlers.send_temp_notification", new_callable=AsyncMock
        ), patch("handlers._return_from_content_editor", new_callable=AsyncMock) as returned:
            await handlers.save_content(message, state, AsyncMock())

        self.assertIsNone(await state.get_state())
        returned.assert_awaited_once_with(
            message,
            "main",
            "admin_reminders_settings",
        )
        async with async_session_maker() as session:
            content = await session.get(Content, self.content_key)
            self.assertEqual(content.text_content, "Обновлённый текст")

    async def test_reminder_cancel_clears_state_and_late_text_cannot_save(self):
        state = self.state()
        await state.set_state(handlers.AdminStates.set_reminder_first_hours)
        async with async_session_maker() as session:
            config = await session.get(BotConfig, 1)
            before = config.main_first_reminder_hours

        with patch("handlers.send_smart_content", new_callable=AsyncMock):
            await handlers.admin_reminders_settings(self.callback("admin_reminders_settings"), state)
        self.assertIsNone(await state.get_state())

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.admin_id),
            chat=SimpleNamespace(id=self.admin_id),
            text="999",
        )
        await handlers.save_reminder_first_hours(message, state, AsyncMock())

        async with async_session_maker() as session:
            config = await session.get(BotConfig, 1)
            self.assertEqual(config.main_first_reminder_hours, before)

    async def test_admin_navigation_screens_clear_input_state(self):
        cases = (
            (handlers.admin_habit_game, "admin_habit_game", handlers.AdminStates.set_game_days),
            (handlers.admin_payment_settings, "admin_payment_settings", handlers.PaymentStates.set_token_price),
            (handlers.admin_manage_plans, "admin_manage_plans", handlers.AdminStates.plan_name),
            (handlers.admin_promocodes, "admin_promocodes", handlers.AdminStates.set_promo_code),
        )
        for screen, callback_data, input_state in cases:
            state = self.state()
            await state.set_state(input_state)
            callback = self.callback(callback_data)
            with patch("handlers.send_smart_content", new_callable=AsyncMock):
                if screen is handlers.admin_promocodes:
                    await screen(callback, state)
                else:
                    await screen(callback, state)
            self.assertIsNone(await state.get_state(), callback_data)

    async def test_input_screens_offer_a_cancel_route(self):
        state = self.state()
        checks = (
            (handlers.set_game_days_start, "admin_habit_game"),
            (handlers.add_prov_start, "admin_pay_providers"),
            (handlers.edit_msg_cost, "admin_pay_prices"),
        )
        for start, expected_back in checks:
            callback = self.callback(start.__name__)
            await start(callback, state)
            markup = callback.message.answer.call_args.kwargs["reply_markup"]
            buttons = [
                button.callback_data
                for row in markup.inline_keyboard
                for button in row
                if button.callback_data
            ]
            self.assertIn(expected_back, buttons, start.__name__)
            await state.clear()

    async def test_destructive_admin_actions_require_confirmation(self):
        target_id = self.admin_id + 1
        callback = self.callback(f"confirm_ureset_dialog_{target_id}")
        await handlers.confirm_user_reset(callback)
        callback.message.edit_text.assert_awaited_once()
        markup = callback.message.edit_text.call_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn(f"ureset_confirmed_dialog_{target_id}", callbacks)
        self.assertIn(f"admin_user_resets_{target_id}", callbacks)

        self.assertEqual(
            permission_for_callback(f"confirm_ureset_dialog_{target_id}"),
            AdminPermission.SYSTEM,
        )
        self.assertEqual(
            permission_for_callback("confirm_del_cont_media_main:welcome_msg"),
            AdminPermission.CONTENT,
        )

    async def test_generated_admin_delete_callbacks_are_confirmed(self):
        admin_markup = kb.admin_panel_keyboard()
        callbacks = [
            button.callback_data
            for row in admin_markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("admin_section_automation", callbacks)
        self.assertEqual(
            permission_for_callback("confirm_admin_delete_plan_1"),
            AdminPermission.PAYMENTS,
        )
        self.assertEqual(
            permission_for_callback("confirm_del_admin_1"),
            AdminPermission.ADMIN_MANAGEMENT,
        )
        self.assertEqual(
            permission_for_callback("confirm_set_pay_mode_subscription"),
            AdminPermission.PAYMENTS,
        )

    async def test_admin_panel_routes_have_permissions_and_role_boundaries(self):
        callbacks = [
            button.callback_data
            for row in kb.admin_panel_keyboard().inline_keyboard
            for button in row
            if button.callback_data and button.callback_data not in {"ignore", "noop"}
        ]
        self.assertTrue(callbacks)
        for callback_data in callbacks:
            self.assertIsNotNone(permission_for_callback(callback_data), callback_data)
        self.assertTrue(role_has_permission(AdminRole.SUPPORT, AdminPermission.CLIENTS))
        self.assertFalse(role_has_permission(AdminRole.SUPPORT, AdminPermission.PAYMENTS))
        self.assertTrue(role_has_permission(AdminRole.CONTENT_EDITOR, AdminPermission.CONTENT))
        self.assertFalse(role_has_permission(AdminRole.CONTENT_EDITOR, AdminPermission.SYSTEM))

    async def test_mailing_send_requires_confirmation_before_queueing(self):
        state = self.state()
        callback = self.callback("mail_send_final")
        await handlers.mail_send_final(callback, state, AsyncMock())

        callback.message.edit_text.assert_awaited_once()
        markup = callback.message.edit_text.call_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("mail_send_final_confirmed", callbacks)
        self.assertIn("mail_preview_back", callbacks)
        self.assertIsNone(await state.get_state())

    async def test_mailing_button_removal_requires_confirmation(self):
        state = self.state()
        await state.update_data(mail_btn_text="Кнопка", mail_btn_action="open_payment_menu")
        callback = self.callback("mail_btn_clear")

        await handlers.mail_btn_clear(callback, state, AsyncMock())

        callback.message.edit_text.assert_awaited_once()
        callbacks = [
            button.callback_data
            for row in callback.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("mail_btn_clear_confirmed", callbacks)
        self.assertIn("mail_setup_btn", callbacks)
        data = await state.get_data()
        self.assertEqual(data["mail_btn_text"], "Кнопка")

    async def test_mailing_navigation_leaves_input_mode(self):
        state = self.state()
        await state.set_state(handlers.AdminStates.mailing_edit_text)
        callback = self.callback("mail_setup_btn")
        with patch("handlers.send_smart_content", new_callable=AsyncMock):
            await handlers.mail_setup_btn(callback, state)

        self.assertEqual(
            await state.get_state(),
            handlers.AdminStates.mailing_content.state,
        )

    async def test_crisis_clear_requires_confirmation_and_clears_pending_input(self):
        state = self.state()
        await state.set_state(handlers.AdminStates.edit_content)
        callback = MagicMock(spec=CallbackQuery)
        callback.data = f"admin_crisis_clear_{self.admin_id}"
        callback.from_user = SimpleNamespace(id=self.admin_id)
        callback.message = SimpleNamespace(edit_text=AsyncMock())
        callback.answer = AsyncMock()

        await handlers.admin_crisis_clear(callback, state)

        self.assertIsNone(await state.get_state())
        callback.message.edit_text.assert_awaited_once()
        markup = callback.message.edit_text.call_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn(f"admin_crisis_clear_confirm_{self.admin_id}", callbacks)
        self.assertIn(f"admin_user_profile_{self.admin_id}", callbacks)
        self.assertEqual(
            permission_for_callback(f"admin_crisis_clear_confirm_{self.admin_id}"),
            AdminPermission.CLIENTS,
        )

    async def test_followup_typed_cancel_returns_to_the_steps_list(self):
        import followup_admin

        state = self.state()
        await state.set_state(followup_admin.FollowupAdminStates.waiting_step_delay)
        await state.update_data(campaign_id=10, step_type="static")
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.admin_id),
            text="отмена",
            answer=AsyncMock(),
        )
        with patch.object(followup_admin, "_is_admin", new=AsyncMock(return_value=True)), patch.object(
            followup_admin, "_show_steps_list", new_callable=AsyncMock
        ) as show_steps:
            await followup_admin.process_step_delay_input(message, state)

        self.assertIsNone(await state.get_state())
        show_steps.assert_awaited_once_with(message, 10)

    async def test_meditation_detail_navigation_clears_pending_input(self):
        import funnel

        state = self.state()
        await state.set_state(funnel.FunnelAdminStates.edit_meditation_text)
        callback = self.callback("admin_fmed_10_2")
        with patch.object(funnel, "is_admin_user", new=AsyncMock(return_value=True)), patch.object(
            funnel, "show_meditation_admin_detail", new_callable=AsyncMock
        ):
            await funnel.admin_fmed_detail(callback, state)

        self.assertIsNone(await state.get_state())

    async def test_followup_step_back_clears_pending_input(self):
        import followup_admin

        state = self.state()
        await state.set_state(followup_admin.FollowupAdminStates.waiting_step_text)
        callback = self.callback("admin_fu_step_10_20")
        with patch.object(followup_admin, "_show_step_card", new_callable=AsyncMock):
            await followup_admin.callback_admin_fu_step_router(callback, state)

        self.assertIsNone(await state.get_state())

    async def test_navigation_labels_hide_internal_followup_name(self):
        labels = [
            button.text
            for row in kb.admin_automation_section_keyboard().inline_keyboard
            for button in row
            if button.text
        ]
        self.assertIn("⏱ Напоминания по цепочке", labels)
        self.assertNotIn("⏱ Цепочки Follow-up", labels)
