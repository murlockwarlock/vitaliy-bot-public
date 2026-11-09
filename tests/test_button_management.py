from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import content_service
import database as database_module
import handlers
import keyboards
import menu_service
from database import (
    Book,
    Content,
    MenuButton,
    MenuSection,
    User,
    async_session_maker,
    init_db,
    sync_book_menu_buttons,
)


class FakeCallback:
    def __init__(self, user_id: int, data: str):
        self.from_user = SimpleNamespace(id=user_id, first_name="Администратор")
        self.data = data
        self.answer = AsyncMock()
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
            edit_text=AsyncMock(),
            bot=self.bot,
        )

    def model_copy(self, *, update: dict):
        clone = FakeCallback(self.from_user.id, update.get("data", self.data))
        clone.bot = self.bot
        clone.message = self.message
        clone.message.bot = self.bot
        return clone


class ButtonManagementTests(unittest.IsolatedAsyncioTestCase):
    admin_id = 806750628
    ordinary_id = 806750629

    async def asyncSetUp(self):
        self._original_runtime = {
            (database_module, "engine"): database_module.engine,
            (database_module, "async_session_maker"): database_module.async_session_maker,
            (handlers, "async_session_maker"): handlers.async_session_maker,
            (keyboards, "async_session_maker"): keyboards.async_session_maker,
            (menu_service, "async_session_maker"): menu_service.async_session_maker,
            (content_service, "async_session_maker"): content_service.async_session_maker,
            (self.__class__, "test_session_maker"): async_session_maker,
        }
        fd, self._test_db_path = tempfile.mkstemp(prefix="vitaliy-buttons-", suffix=".db")
        os.close(fd)
        self._test_engine = create_async_engine(f"sqlite+aiosqlite:///{self._test_db_path}", echo=False)
        self._test_session_maker = sessionmaker(
            self._test_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        database_module.engine = self._test_engine
        database_module.async_session_maker = self._test_session_maker
        handlers.async_session_maker = self._test_session_maker
        keyboards.async_session_maker = self._test_session_maker
        menu_service.async_session_maker = self._test_session_maker
        content_service.async_session_maker = self._test_session_maker
        globals()["async_session_maker"] = self._test_session_maker
        content_service.invalidate_content_cache()
        await init_db()
        async with async_session_maker() as session:
            session.add_all([
                User(id=self.admin_id, first_name="Менеджер", is_admin=True),
                User(id=self.ordinary_id, first_name="Клиент", is_admin=False),
            ])
            try:
                await session.commit()
            except Exception:
                await session.rollback()
        async with async_session_maker() as session:
            self.original_buttons = {
                button.id: (
                    button.label,
                    button.enabled,
                    button.order_num,
                    button.action_type,
                    button.action_value,
                    button.label_override,
                )
                for button in (await session.scalars(select(MenuButton))).all()
            }
            self.original_sections = {
                section.key: (section.enabled, section.order_num)
                for section in (await session.scalars(select(MenuSection))).all()
            }

    async def asyncTearDown(self):
        async with async_session_maker() as session:
            for button_id, values in self.original_buttons.items():
                button = await session.get(MenuButton, button_id)
                if button:
                    (
                        button.label,
                        button.enabled,
                        button.order_num,
                        button.action_type,
                        button.action_value,
                        button.label_override,
                    ) = values
            for key, values in self.original_sections.items():
                section = await session.get(MenuSection, key)
                if section:
                    section.enabled, section.order_num = values
            await session.commit()
        await self._test_engine.dispose()
        for (owner, name), value in self._original_runtime.items():
            if owner is self.__class__:
                globals()["async_session_maker"] = value
            else:
                setattr(owner, name, value)
        try:
            os.unlink(self._test_db_path)
        except FileNotFoundError:
            pass

    async def _state(self, user_id: int) -> FSMContext:
        storage = MemoryStorage()
        return FSMContext(storage, StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))

    async def _button(self, section_key: str, order: int = 0) -> MenuButton:
        async with async_session_maker() as session:
            return await session.scalar(select(MenuButton).where(
                MenuButton.section_key == section_key,
                MenuButton.order_num == order,
            ))

    async def test_admin_renames_main_button_and_legacy_action_survives(self):
        button = await self._button("main", 0)
        old_action = button.action_value
        state = await self._state(self.admin_id)
        await state.update_data(menu_button_id=button.id)
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.admin_id),
            text="🎧 Практики для спокойствия",
            answer=AsyncMock(),
            bot=SimpleNamespace(),
        )

        await handlers.admin_menu_button_label_save(message, state)

        markup = await keyboards.main_menu_keyboard()
        row = next(item for row in markup.inline_keyboard for item in row if item.text == "🎧 Практики для спокойствия")
        self.assertEqual(row.callback_data, old_action)
        async with async_session_maker() as session:
            persisted = await session.get(MenuButton, button.id)
            self.assertEqual(persisted.label, "🎧 Практики для спокойствия")

    async def test_admin_hides_and_reenables_extended_button_immediately(self):
        button = await self._button("extended", 5)
        old_action = button.action_value
        callback = FakeCallback(self.admin_id, f"admin_menu_button_toggle:{button.id}")
        await handlers.admin_menu_button_toggle(callback)
        hidden = await keyboards.extended_menu_keyboard()
        self.assertNotIn(button.label, [item.text for row in hidden.inline_keyboard for item in row])

        await handlers.admin_menu_button_toggle(FakeCallback(self.admin_id, f"admin_menu_button_toggle:{button.id}"))
        visible = await keyboards.extended_menu_keyboard()
        matching = [item for row in visible.inline_keyboard for item in row if item.text == button.label]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].callback_data, old_action)

    async def test_real_extended_handler_sends_current_client_keyboard(self):
        button = await self._button("extended", 5)
        await handlers.admin_menu_button_toggle(FakeCallback(self.admin_id, f"admin_menu_button_toggle:{button.id}"))
        client_callback = FakeCallback(self.ordinary_id, "open_extended_menu")
        await handlers.back_to_extended_menu(client_callback)
        hidden_markup = client_callback.bot.send_message.await_args.kwargs["reply_markup"]
        hidden_labels = [item.text for row in hidden_markup.inline_keyboard for item in row]
        self.assertNotIn(button.label, hidden_labels)

        await handlers.admin_menu_button_toggle(FakeCallback(self.admin_id, f"admin_menu_button_toggle:{button.id}"))
        client_callback = FakeCallback(self.ordinary_id, "open_extended_menu")
        await handlers.back_to_extended_menu(client_callback)
        visible_markup = client_callback.bot.send_message.await_args.kwargs["reply_markup"]
        visible_labels = [item.text for row in visible_markup.inline_keyboard for item in row]
        self.assertIn(button.label, visible_labels)

    async def test_concurrent_toggles_are_atomic(self):
        button = await self._button("main", 1)
        original = button.enabled
        await asyncio.gather(
            handlers.admin_menu_button_toggle(FakeCallback(self.admin_id, f"admin_menu_button_toggle:{button.id}")),
            handlers.admin_menu_button_toggle(FakeCallback(self.admin_id, f"admin_menu_button_toggle:{button.id}")),
        )
        async with async_session_maker() as session:
            current = await session.get(MenuButton, button.id)
            self.assertEqual(current.enabled, original)

    async def test_admin_reorders_buttons_without_changing_their_actions(self):
        button = await self._button("main", 4)
        action = button.action_value
        state = await self._state(self.admin_id)
        await state.update_data(menu_button_id=button.id)
        await state.set_state(handlers.AdminStates.menu_button_order)
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.admin_id),
            text="1",
            answer=AsyncMock(),
        )
        await handlers.admin_menu_button_order_save(message, state)
        markup = await keyboards.main_menu_keyboard()
        first = markup.inline_keyboard[0][0]
        self.assertEqual(first.callback_data, action)

    async def test_section_disable_hides_section_entry_and_reenable_restores_it(self):
        callback = FakeCallback(self.admin_id, "admin_menu_section_toggle:club")
        await handlers.admin_menu_section_toggle(callback)
        extended = await keyboards.extended_menu_keyboard()
        self.assertNotIn("🔐 Закрытый клуб", [item.text for row in extended.inline_keyboard for item in row])
        club = await keyboards.closed_club_keyboard()
        self.assertEqual(club.inline_keyboard, [])

        await handlers.admin_menu_section_toggle(FakeCallback(self.admin_id, "admin_menu_section_toggle:club"))
        extended = await keyboards.extended_menu_keyboard()
        self.assertIn("🔐 Закрытый клуб", [item.text for row in extended.inline_keyboard for item in row])

        await handlers.admin_menu_section_toggle(FakeCallback(self.admin_id, "admin_menu_section_toggle:books"))
        extended = await keyboards.extended_menu_keyboard()
        self.assertNotIn("📚 Библиотека книг", [item.text for row in extended.inline_keyboard for item in row])
        await handlers.admin_menu_section_toggle(FakeCallback(self.admin_id, "admin_menu_section_toggle:books"))

        await handlers.admin_menu_section_toggle(FakeCallback(self.admin_id, "admin_menu_section_toggle:club"))
        stale_client_callback = FakeCallback(self.ordinary_id, "menu_closed_club")
        await handlers.show_closed_club(stale_client_callback, stale_client_callback.bot)
        self.assertIn("недоступен", stale_client_callback.bot.send_message.await_args.args[1])
        await handlers.admin_menu_section_toggle(FakeCallback(self.admin_id, "admin_menu_section_toggle:club"))

    async def test_all_configured_sections_render_current_button_state(self):
        builders = {
            "main": keyboards.main_menu_keyboard,
            "extended": keyboards.extended_menu_keyboard,
            "thematic": keyboards.configured_thematic_sessions_keyboard,
            "vitaly": keyboards.vitaly_keyboard,
            "club": keyboards.closed_club_keyboard,
            "referral": keyboards.configured_referral_keyboard,
        }
        for section_key, renderer in builders.items():
            markup = await renderer()
            async with async_session_maker() as session:
                enabled = (await session.scalars(select(MenuButton).where(
                    MenuButton.section_key == section_key,
                    MenuButton.enabled == True,
                ).order_by(MenuButton.order_num.asc()))).all()
            rendered_labels = [item.text for row in markup.inline_keyboard for item in row]
            for button in enabled:
                if button.action_value == "open_payment_menu":
                    continue
                expected_label = button.label
                if button.content_key and not button.label_override:
                    async with async_session_maker() as session:
                        content = await session.get(Content, button.content_key)
                    if content and content.button_text:
                        expected_label = content.button_text
                self.assertIn(expected_label, rendered_labels)

    async def test_non_admin_cannot_change_button(self):
        button = await self._button("main", 0)
        callback = FakeCallback(self.ordinary_id, f"admin_menu_button_toggle:{button.id}")
        await handlers.admin_menu_button_toggle(callback)
        async with async_session_maker() as session:
            current = await session.get(MenuButton, button.id)
            self.assertEqual(current.enabled, button.enabled)
        callback.answer.assert_awaited()

        state = await self._state(self.ordinary_id)
        await state.update_data(menu_button_id=button.id)
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.ordinary_id),
            text="Чужое название",
            answer=AsyncMock(),
        )
        await handlers.admin_menu_button_label_save(message, state)
        message.answer.assert_awaited_once_with("⛔️ Доступ запрещён.")
        self.assertIsNone(await state.get_state())

    async def test_non_admin_cannot_disable_menu_section(self):
        callback = FakeCallback(self.ordinary_id, "admin_menu_section_toggle:books")
        await handlers.admin_menu_section_toggle(callback)
        async with async_session_maker() as session:
            section = await session.get(MenuSection, "books")
            self.assertTrue(section.enabled)
        callback.answer.assert_awaited_once_with("⛔️ Доступ запрещён.", show_alert=True)

    async def test_legacy_admin_callback_is_kept_as_alias(self):
        callback = FakeCallback(self.admin_id, "admin_manage_buttons")
        await handlers.admin_manage_buttons(callback)
        callback.answer.assert_awaited_once_with(
            "Откройте раздел «Контент», чтобы управлять кнопками."
        )

    async def test_content_editor_label_still_reaches_runtime_menu(self):
        content_key = "btn_transfo"
        async with async_session_maker() as session:
            content = await session.get(Content, content_key)
            original = content.button_text
            content.button_text = "Трансформации для роста"
            await session.commit()
        try:
            markup = await keyboards.extended_menu_keyboard()
            labels = [item.text for row in markup.inline_keyboard for item in row]
            self.assertIn("Трансформации для роста", labels)
        finally:
            async with async_session_maker() as session:
                content = await session.get(Content, content_key)
                content.button_text = original
                await session.commit()

    async def test_explicit_menu_rename_wins_over_legacy_content_edit(self):
        button = await self._button("extended", 2)
        state = await self._state(self.admin_id)
        await state.update_data(menu_button_id=button.id)
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.admin_id),
            text="Новая подпись раздела",
            answer=AsyncMock(),
        )
        await handlers.admin_menu_button_label_save(message, state)
        async with async_session_maker() as session:
            content = await session.get(Content, button.content_key)
            original = content.button_text
            content.button_text = "Старая админская подпись"
            await session.commit()
        try:
            markup = await keyboards.extended_menu_keyboard()
            labels = [item.text for row in markup.inline_keyboard for item in row]
            self.assertIn("Новая подпись раздела", labels)
            self.assertNotIn("Старая админская подпись", labels)
        finally:
            async with async_session_maker() as session:
                content = await session.get(Content, button.content_key)
                content.button_text = original
                await session.commit()

    async def test_admin_content_entry_uses_single_human_menu_editor(self):
        callback = FakeCallback(self.admin_id, "admin_content_list")
        await handlers.admin_content_list(callback)
        markup = callback.bot.send_message.await_args.kwargs["reply_markup"]
        labels = [item.text for row in markup.inline_keyboard for item in row]
        callbacks = [item.callback_data for row in markup.inline_keyboard for item in row]
        self.assertIn("🧭 Меню и разделы", labels)
        self.assertIn("admin_menu_sections", callbacks)
        self.assertNotIn("🎛️ Кнопки меню", labels)

        sections_callback = FakeCallback(self.admin_id, "admin_menu_sections")
        await handlers.admin_menu_sections(sections_callback)
        sections_markup = sections_callback.bot.send_message.await_args.kwargs["reply_markup"]
        section_labels = [item.text for row in sections_markup.inline_keyboard for item in row]
        self.assertTrue(any(text.startswith("Библиотека книг") for text in section_labels))

    async def test_back_from_rename_clears_fsm_and_later_text_does_not_rename(self):
        button = await self._button("main", 0)
        original_label = button.label
        state = await self._state(self.admin_id)

        await handlers.admin_menu_button_label_start(
            FakeCallback(self.admin_id, f"admin_menu_button_label:{button.id}"),
            state,
        )
        self.assertEqual(await state.get_state(), handlers.AdminStates.menu_button_label.state)

        await handlers.admin_menu_button(
            FakeCallback(self.admin_id, f"admin_menu_button:{button.id}"),
            state=state,
        )
        self.assertIsNone(await state.get_state())

        ordinary_message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.admin_id),
            text="Обычный текст после отмены",
            answer=AsyncMock(),
        )
        await handlers.admin_menu_button_label_save(ordinary_message, state)

        async with async_session_maker() as session:
            current = await session.get(MenuButton, button.id)
        self.assertEqual(current.label, original_label)
        ordinary_message.answer.assert_not_awaited()

    async def test_back_from_legacy_order_clears_fsm_and_other_admin_sections_cancel_input(self):
        button = await self._button("main", 1)
        state = await self._state(self.admin_id)

        await handlers.admin_menu_button_order_start(
            FakeCallback(self.admin_id, f"admin_menu_button_order:{button.id}"),
            state,
        )
        self.assertEqual(await state.get_state(), handlers.AdminStates.menu_button_order.state)

        await handlers.admin_menu_button(
            FakeCallback(self.admin_id, f"admin_menu_button:{button.id}"),
            state=state,
        )
        self.assertIsNone(await state.get_state())

        await state.set_state(handlers.AdminStates.menu_button_order)
        await state.update_data(menu_button_id=button.id)
        await handlers.admin_content_list(
            FakeCallback(self.admin_id, "admin_content_list"),
            state=state,
        )
        self.assertIsNone(await state.get_state())

        await state.set_state(handlers.AdminStates.menu_button_order)
        await state.update_data(menu_button_id=button.id)
        await handlers.admin_menu_sections(
            FakeCallback(self.admin_id, "admin_menu_sections"),
            state=state,
        )
        self.assertIsNone(await state.get_state())

    async def test_sections_screen_is_compact_and_has_no_section_reorder_controls(self):
        callback = FakeCallback(self.admin_id, "admin_menu_sections")
        await handlers.admin_menu_sections(callback)
        markup = callback.bot.send_message.await_args.kwargs["reply_markup"]
        section_items = [
            item
            for row in markup.inline_keyboard
            for item in row
            if item.callback_data and item.callback_data.startswith("admin_menu_section:")
        ]
        self.assertTrue(section_items)
        self.assertTrue(all(len(row) == 1 for row in markup.inline_keyboard))
        self.assertFalse(any(
            item.callback_data and item.callback_data.startswith("admin_menu_section_move:")
            for row in markup.inline_keyboard
            for item in row
        ))
        self.assertTrue(all("(" not in item.text and ")" not in item.text for item in section_items))

    async def test_section_screen_marks_visible_and_hidden_buttons(self):
        button = await self._button("extended", 5)
        await handlers.admin_menu_button_toggle(
            FakeCallback(self.admin_id, f"admin_menu_button_toggle:{button.id}")
        )
        try:
            callback = FakeCallback(self.admin_id, "admin_menu_section:extended")
            await handlers.admin_menu_section(callback)
            markup = callback.bot.send_message.await_args.kwargs["reply_markup"]
            button_items = [
                item
                for row in markup.inline_keyboard
                for item in row
                if item.callback_data and item.callback_data.startswith("admin_menu_button:")
            ]
            self.assertTrue(any(item.text.startswith("🙈 ") for item in button_items))
            self.assertTrue(any(item.text.startswith("👁 ") for item in button_items))
        finally:
            await handlers.admin_menu_button_toggle(
                FakeCallback(self.admin_id, f"admin_menu_button_toggle:{button.id}")
            )

    async def test_button_editor_uses_tap_actions_and_shows_position(self):
        button = await self._button("main", 1)
        callback = FakeCallback(self.admin_id, f"admin_menu_button:{button.id}")
        await handlers.admin_menu_button(callback)
        markup = callback.bot.send_message.await_args.kwargs["reply_markup"]
        callbacks = [
            item.callback_data
            for row in markup.inline_keyboard
            for item in row
            if item.callback_data
        ]
        labels = [
            item.text
            for row in markup.inline_keyboard
            for item in row
        ]
        message_text = callback.bot.send_message.await_args.args[1]
        self.assertIn(f"admin_menu_button_label:{button.id}", callbacks)
        self.assertIn(f"admin_menu_button_toggle:{button.id}", callbacks)
        self.assertIn(f"admin_menu_button_move:{button.id}:up", callbacks)
        self.assertIn(f"admin_menu_button_move:{button.id}:down", callbacks)
        self.assertIn(f"admin_menu_section:{button.section_key}", callbacks)
        self.assertIn("⬆️ Выше", labels)
        self.assertIn("⬇️ Ниже", labels)
        self.assertNotIn("Изменить порядок", labels)
        self.assertIn("Позиция:", message_text)
        self.assertIn("Видна клиенту", message_text)

    async def test_toggle_returns_to_updated_button_editor(self):
        button = await self._button("main", 0)
        callback = FakeCallback(self.admin_id, f"admin_menu_button_toggle:{button.id}")
        await handlers.admin_menu_button_toggle(callback)
        try:
            message_text = callback.bot.send_message.await_args.args[1]
            callbacks = [
                item.callback_data
                for row in callback.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
                for item in row
                if item.callback_data
            ]
            self.assertIn("Скрыта от клиентов", message_text)
            self.assertIn(f"admin_menu_button_move:{button.id}:up", callbacks)
        finally:
            await handlers.admin_menu_button_toggle(
                FakeCallback(self.admin_id, f"admin_menu_button_toggle:{button.id}")
            )

    async def test_tap_reorder_returns_updated_editor_and_preserves_action(self):
        button = await self._button("main", 1)
        old_order = button.order_num
        old_action = button.action_value
        callback = FakeCallback(self.admin_id, f"admin_menu_button_move:{button.id}:down")
        await handlers.admin_menu_button_move(callback)
        async with async_session_maker() as session:
            moved = await session.get(MenuButton, button.id)
            self.assertEqual(moved.order_num, old_order + 1)
            self.assertEqual(moved.action_value, old_action)
        message_text = callback.bot.send_message.await_args.args[1]
        self.assertIn(f"Позиция: {old_order + 2} из", message_text)
        self.assertIn("⬆️ Выше", [
            item.text
            for row in callback.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
            for item in row
        ])

    async def test_rename_returns_to_updated_button_editor(self):
        button = await self._button("main", 0)
        state = await self._state(self.admin_id)
        await state.update_data(menu_button_id=button.id)
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.admin_id),
            text="Новое название кнопки",
            answer=AsyncMock(),
        )
        await handlers.admin_menu_button_label_save(message, state)
        response = message.answer.await_args
        self.assertIn("Новое название кнопки", response.args[0])
        callbacks = [
            item.callback_data
            for row in response.kwargs["reply_markup"].inline_keyboard
            for item in row
            if item.callback_data
        ]
        self.assertIn(f"admin_menu_section:{button.section_key}", callbacks)
        self.assertIn(f"admin_menu_button_move:{button.id}:up", callbacks)
        self.assertEqual(await state.get_state(), None)

    async def test_settings_survive_database_reinitialization(self):
        button = await self._button("vitaly", 0)
        async with async_session_maker() as session:
            row = await session.get(MenuButton, button.id)
            row.label = "Записаться к психологу"
            row.enabled = False
            await session.commit()
        await init_db()
        async with async_session_maker() as session:
            persisted = await session.get(MenuButton, button.id)
            self.assertEqual(persisted.label, "Записаться к психологу")
            self.assertFalse(persisted.enabled)

    async def test_disabled_root_sections_keep_safe_navigation_and_old_callbacks(self):
        await handlers.admin_menu_section_toggle(FakeCallback(self.admin_id, "admin_menu_section_toggle:main"))
        try:
            main_markup = await keyboards.main_menu_keyboard()
            main_buttons = [item for row in main_markup.inline_keyboard for item in row]
            self.assertTrue(main_buttons)
            self.assertIn("open_extended_menu", {item.callback_data for item in main_buttons})

            stale_main = FakeCallback(self.ordinary_id, "back_to_main_menu")
            await handlers.back_to_main(stale_main, stale_main.bot)
            sent_main_markup = stale_main.bot.send_message.await_args.kwargs["reply_markup"]
            self.assertTrue(sent_main_markup.inline_keyboard)
        finally:
            await handlers.admin_menu_section_toggle(FakeCallback(self.admin_id, "admin_menu_section_toggle:main"))

        await handlers.admin_menu_section_toggle(FakeCallback(self.admin_id, "admin_menu_section_toggle:extended"))
        try:
            extended_markup = await keyboards.extended_menu_keyboard()
            extended_buttons = [item for row in extended_markup.inline_keyboard for item in row]
            self.assertTrue(extended_buttons)
            self.assertIn("back_to_main_menu", {item.callback_data for item in extended_buttons})

            stale_extended = FakeCallback(self.ordinary_id, "open_extended_menu")
            await handlers.back_to_extended_menu(stale_extended)
            sent_extended_markup = stale_extended.bot.send_message.await_args.kwargs["reply_markup"]
            self.assertTrue(sent_extended_markup.inline_keyboard)
        finally:
            await handlers.admin_menu_section_toggle(FakeCallback(self.admin_id, "admin_menu_section_toggle:extended"))

    async def test_books_use_admin_menu_registry_for_rename_hide_and_restore(self):
        book_id = None
        menu_button_id = None
        async with async_session_maker() as session:
            book = Book(title="Книга для проверки меню", url="https://example.test/book")
            session.add(book)
            await session.commit()
            book_id = book.id

        try:
            admin_sections = FakeCallback(self.admin_id, "admin_menu_section:books")
            await handlers.admin_menu_section(admin_sections)
            admin_markup = admin_sections.bot.send_message.await_args.kwargs["reply_markup"]
            admin_labels = [item.text for row in admin_markup.inline_keyboard for item in row]
            self.assertTrue(any("Книга для проверки меню" in label for label in admin_labels))

            async with async_session_maker() as session:
                menu_button = await session.scalar(select(MenuButton).where(
                    MenuButton.section_key == "books",
                    MenuButton.source_type == "book",
                    MenuButton.source_id == book_id,
                ))
                self.assertIsNotNone(menu_button)
                menu_button_id = menu_button.id

            state = await self._state(self.admin_id)
            await handlers.admin_menu_button_label_start(
                FakeCallback(self.admin_id, f"admin_menu_button_label:{menu_button_id}"),
                state,
            )
            rename_message = SimpleNamespace(
                from_user=SimpleNamespace(id=self.admin_id),
                text="Книга: новое название",
                answer=AsyncMock(),
            )
            await handlers.admin_menu_button_label_save(rename_message, state)

            books_markup = await keyboards.books_keyboard()
            book_buttons = [item for row in books_markup.inline_keyboard for item in row]
            renamed = next(item for item in book_buttons if item.text == "Книга: новое название")
            self.assertEqual(renamed.url, "https://example.test/book")

            await handlers.admin_menu_button_toggle(
                FakeCallback(self.admin_id, f"admin_menu_button_toggle:{menu_button_id}")
            )
            hidden_markup = await keyboards.books_keyboard()
            hidden_labels = [item.text for row in hidden_markup.inline_keyboard for item in row]
            self.assertNotIn("Книга: новое название", hidden_labels)

            stale_books = FakeCallback(self.ordinary_id, "menu_books")
            await handlers.show_books(stale_books, stale_books.bot)
            stale_markup = stale_books.bot.send_message.await_args.kwargs["reply_markup"]
            self.assertNotIn("Книга: новое название", [item.text for row in stale_markup.inline_keyboard for item in row])

            await handlers.admin_menu_button_toggle(
                FakeCallback(self.admin_id, f"admin_menu_button_toggle:{menu_button_id}")
            )
            restored_markup = await keyboards.books_keyboard()
            restored_labels = [item.text for row in restored_markup.inline_keyboard for item in row]
            self.assertIn("Книга: новое название", restored_labels)
        finally:
            async with async_session_maker() as session:
                if menu_button_id:
                    menu_button = await session.get(MenuButton, menu_button_id)
                    if menu_button:
                        await session.delete(menu_button)
                if book_id:
                    book = await session.get(Book, book_id)
                    if book:
                        await session.delete(book)
                await session.commit()

    async def test_existing_button_names_are_not_changed_by_reinitialization(self):
        button = await self._button("main", 0)
        async with async_session_maker() as session:
            row = await session.get(MenuButton, button.id)
            row.label = "Подпись, которую уже видят клиенты"
            row.label_override = True
            await session.commit()
        await init_db()
        async with async_session_maker() as session:
            persisted = await session.get(MenuButton, button.id)
            self.assertEqual(persisted.label, "Подпись, которую уже видят клиенты")
            self.assertTrue(persisted.label_override)

    async def test_books_order_and_pagination_use_only_visible_registry_rows(self):
        book_ids = []
        menu_button_ids = []
        try:
            async with async_session_maker() as session:
                books = [
                    Book(title=f"Книга-пагинация-{index:02d}", url=f"https://example.test/page/{index}")
                    for index in range(22)
                ]
                session.add_all(books)
                await session.commit()
                book_ids = [book.id for book in books]

            async with async_session_maker() as session:
                await sync_book_menu_buttons(session)
                rows = (await session.scalars(select(MenuButton).where(
                    MenuButton.section_key == "books",
                    MenuButton.source_type == "book",
                    MenuButton.source_id.in_(book_ids),
                ))).all()
                menu_button_ids = [row.id for row in rows]
                target_id = book_ids[0]
                target_button_id = next(row.id for row in rows if row.source_id == target_id)

            order_state = await self._state(self.admin_id)
            await handlers.admin_menu_button_order_start(
                FakeCallback(self.admin_id, f"admin_menu_button_order:{target_button_id}"),
                order_state,
            )
            await handlers.admin_menu_button_order_save(
                SimpleNamespace(
                    from_user=SimpleNamespace(id=self.admin_id),
                    text="1",
                    answer=AsyncMock(),
                ),
                order_state,
            )

            for hidden_id in book_ids[-3:]:
                async with async_session_maker() as session:
                    menu_button_id = await session.scalar(select(MenuButton.id).where(
                        MenuButton.section_key == "books",
                        MenuButton.source_type == "book",
                        MenuButton.source_id == hidden_id,
                    ))
                await handlers.admin_menu_button_toggle(
                    FakeCallback(self.admin_id, f"admin_menu_button_toggle:{menu_button_id}")
                )

            page_one = await keyboards.books_keyboard(page=0)
            page_two = await keyboards.books_keyboard(page=1)
            clamped_page = await keyboards.books_keyboard(page=2)
            page_one_books = [item for row in page_one.inline_keyboard for item in row if item.url]
            page_two_books = [item for row in page_two.inline_keyboard for item in row if item.url]
            clamped_books = [item for row in clamped_page.inline_keyboard for item in row if item.url]

            self.assertEqual(len(page_one_books), 10)
            self.assertEqual(len(page_two_books), 9)
            self.assertEqual([item.text for item in clamped_books], [item.text for item in page_two_books])
            self.assertEqual(page_one_books[0].text, "Книга-пагинация-00")
            self.assertNotIn("Книга-пагинация-19", [item.text for item in page_one_books + page_two_books])
            self.assertNotIn("Книга-пагинация-20", [item.text for item in page_one_books + page_two_books])
            self.assertNotIn("Книга-пагинация-21", [item.text for item in page_one_books + page_two_books])
            page_indicators = [
                item.text
                for markup in (page_one, page_two)
                for row in markup.inline_keyboard
                for item in row
                if item.callback_data == "ignore"
            ]
            self.assertEqual(page_indicators, ["1/2", "2/2"])
        finally:
            async with async_session_maker() as session:
                for button_id in menu_button_ids:
                    button = await session.get(MenuButton, button_id)
                    if button:
                        await session.delete(button)
                for book_id in book_ids:
                    book = await session.get(Book, book_id)
                    if book:
                        await session.delete(book)
                await session.commit()

    async def test_book_sync_is_unique_under_concurrent_requests(self):
        async with async_session_maker() as session:
            book = Book(title="Книга конкурентной синхронизации", url="https://example.test/concurrent")
            session.add(book)
            await session.commit()
            book_id = book.id

        async def worker():
            async with async_session_maker() as session:
                await sync_book_menu_buttons(session)

        try:
            await asyncio.gather(*(worker() for _ in range(8)))
            async with async_session_maker() as session:
                rows = (await session.scalars(select(MenuButton).where(
                    MenuButton.section_key == "books",
                    MenuButton.source_type == "book",
                    MenuButton.source_id == book_id,
                ))).all()
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0].enabled)
        finally:
            async with async_session_maker() as session:
                for row in (await session.scalars(select(MenuButton).where(
                    MenuButton.section_key == "books",
                    MenuButton.source_type == "book",
                    MenuButton.source_id == book_id,
                ))).all():
                    await session.delete(row)
                book = await session.get(Book, book_id)
                if book:
                    await session.delete(book)
                await session.commit()

    async def test_deleted_book_cannot_leave_or_restore_a_ghost_button(self):
        async with async_session_maker() as session:
            book = Book(title="Книга, которую удалят", url="https://example.test/deleted")
            session.add(book)
            await session.commit()
            book_id = book.id
        async with async_session_maker() as session:
            await sync_book_menu_buttons(session)
            menu_button_id = await session.scalar(select(MenuButton.id).where(
                MenuButton.section_key == "books",
                MenuButton.source_type == "book",
                MenuButton.source_id == book_id,
            ))

        await handlers.delete_book(FakeCallback(self.admin_id, f"del_book_{book_id}_0"))
        books_markup = await keyboards.books_keyboard()
        self.assertNotIn(
            "Книга, которую удалят",
            [item.text for row in books_markup.inline_keyboard for item in row],
        )

        admin_books = FakeCallback(self.admin_id, "admin_menu_section:books")
        await handlers.admin_menu_section(admin_books)
        admin_labels = [item.text for row in admin_books.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard for item in row]
        self.assertNotIn("Книга, которую удалят", admin_labels)

        restore_attempt = FakeCallback(self.admin_id, f"admin_menu_button_toggle:{menu_button_id}")
        await handlers.admin_menu_button_toggle(restore_attempt)
        restore_attempt.answer.assert_awaited_once_with(
            "Книга удалена. Кнопка оставлена выключенной.",
            show_alert=True,
        )
        async with async_session_maker() as session:
            ghost = await session.get(MenuButton, menu_button_id)
            self.assertFalse(ghost.enabled)
            await session.delete(ghost)
            await session.commit()

    async def test_admin_cannot_disable_last_root_menu_and_db_stays_safe(self):
        await handlers.admin_menu_section_toggle(FakeCallback(self.admin_id, "admin_menu_section_toggle:main"))
        try:
            async with async_session_maker() as session:
                main = await session.get(MenuSection, "main")
                extended = await session.get(MenuSection, "extended")
                self.assertFalse(main.enabled)
                self.assertTrue(extended.enabled)

            blocked = FakeCallback(self.admin_id, "admin_menu_section_toggle:extended")
            await handlers.admin_menu_section_toggle(blocked)
            blocked.answer.assert_awaited_once_with(
                "Нельзя выключить оба основных меню. Оставьте включённым главное или расширенное меню.",
                show_alert=True,
            )

            async with async_session_maker() as session:
                main = await session.get(MenuSection, "main")
                extended = await session.get(MenuSection, "extended")
                self.assertFalse(main.enabled)
                self.assertTrue(extended.enabled)

            main_markup = await keyboards.main_menu_keyboard()
            extended_markup = await keyboards.extended_menu_keyboard()
            self.assertTrue(main_markup.inline_keyboard)
            self.assertTrue(extended_markup.inline_keyboard)
            self.assertIn(
                "open_extended_menu",
                {
                    item.callback_data
                    for row in main_markup.inline_keyboard
                    for item in row
                    if item.callback_data
                },
            )
        finally:
            await handlers.admin_menu_section_toggle(FakeCallback(self.admin_id, "admin_menu_section_toggle:main"))

    async def test_concurrent_root_disable_requests_leave_one_menu_enabled(self):
        callbacks = [
            FakeCallback(self.admin_id, "admin_menu_section_toggle:main"),
            FakeCallback(self.admin_id, "admin_menu_section_toggle:extended"),
        ]
        try:
            await asyncio.gather(*(handlers.admin_menu_section_toggle(callback) for callback in callbacks))
            async with async_session_maker() as session:
                states = {
                    section.key: section.enabled
                    for section in (await session.scalars(
                        select(MenuSection).where(MenuSection.key.in_(("main", "extended")))
                    )).all()
                }
            self.assertEqual(sum(bool(value) for value in states.values()), 1)
            self.assertEqual(
                sum(callback.answer.await_count for callback in callbacks),
                1,
                "Exactly one concurrent disable request must be rejected",
            )
        finally:
            async with async_session_maker() as session:
                for key in ("main", "extended"):
                    section = await session.get(MenuSection, key)
                    section.enabled = True
                await session.commit()


if __name__ == "__main__":
    unittest.main()
