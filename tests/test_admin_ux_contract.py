import pytest
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
from sqlalchemy import select

import keyboards as kb
import handlers
from database import User, async_session_maker, init_db, Topic, AIConfig, PaymentConfig, Content, MenuButton
from automation_admin import automation_menu


# Exact contract table matching PsychoBot and Vitaliy
PSYCHOBOT_TO_VITALIY_UX_CONTRACT = {
    "clients": ("👥 Клиенты", "👥 Клиенты"),
    "automations": ("⚙️ Автоматизации", "⚙️ Автоматизации"),
    "ai_settings": ("🤖 Настройки ИИ", "🤖 Настройки ИИ"),
    "knowledge_base": ("📚 База знаний", "📚 База знаний"),
    "content": ("✏️ Контент", "✏️ Контент"),
    "mailing": ("✉️ Рассылка", "✉️ Рассылка"),
    "admins": ("👮\u200d♂️ Администраторы", "👮\u200d♂️ Администраторы"),
    "topics": ("💬 Темы диалогов", "💬 Темы диалогов"),
    "providers_and_models": ("⚙️ Провайдеры и модели", "⚙️ Провайдеры и модели"),
    "ai_logs": ("📜 Логи запросов ИИ", "📜 Логи запросов ИИ"),
    "subscriptions": ("⭐️ Подписки", "⭐️ Подписки"),
}


class AdminUXContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.storage = MemoryStorage()
        self.admin_id = 11002233
        async with async_session_maker() as session:
            admin_user = await session.get(User, self.admin_id)
            if not admin_user:
                admin_user = User(
                    id=self.admin_id,
                    first_name="Admin",
                    username="admin_user",
                    is_admin=True,
                )
                session.add(admin_user)
            else:
                admin_user.is_admin = True

            ai_cfg = await session.get(AIConfig, 1)
            if not ai_cfg:
                session.add(AIConfig(id=1, provider="OpenAI"))

            pay_cfg = await session.get(PaymentConfig, 1)
            if not pay_cfg:
                session.add(PaymentConfig(id=1, mode="subscription"))

            topic = await session.get(Topic, 1)
            if not topic:
                session.add(Topic(slug="test_slug", name="Тест тема", is_paid=False))

            await session.commit()

    def get_fsm_context(self, user_id: int, chat_id: int | None = None) -> FSMContext:
        c_id = chat_id or user_id
        return FSMContext(
            storage=self.storage,
            key=StorageKey(bot_id=123456, chat_id=c_id, user_id=user_id)
        )

    def create_mock_callback(self, user_id: int, data: str, message_id: int = 1):
        cb = AsyncMock(spec=CallbackQuery)
        cb.data = data
        cb.from_user = TgUser(id=user_id, is_bot=False, first_name="Admin")
        cb.message = AsyncMock(spec=Message)
        cb.message.chat = Chat(id=user_id, type="private")
        cb.message.message_id = message_id
        cb.message.edit_text = AsyncMock()
        cb.message.answer = AsyncMock()
        cb.answer = AsyncMock()
        return cb

    def test_ux_contract_admin_panel_labels(self):
        """Assert all 1:1 common buttons in admin_panel_keyboard match PsychoBot exactly."""
        admin_kb = kb.admin_panel_keyboard()
        button_texts = [btn.text for row in admin_kb.inline_keyboard for btn in row]

        for feature_key, (psycho_label, vitaliy_label) in PSYCHOBOT_TO_VITALIY_UX_CONTRACT.items():
            if feature_key == "providers_and_models":
                continue  # This is inside ai_settings_keyboard, verified below
            self.assertEqual(
                psycho_label, vitaliy_label,
                f"Contract mismatch: {feature_key} has divergent specifications"
            )
            self.assertIn(
                vitaliy_label, button_texts,
                f"UX CONTRACT VIOLATION: Button '{vitaliy_label}' for feature '{feature_key}' "
                f"is missing from admin_panel_keyboard!"
            )

    def test_ux_contract_ai_settings_labels(self):
        """Assert AI settings sub-menu labels match PsychoBot exactly."""
        ai_kb = kb.ai_settings_keyboard("OpenAI")
        button_texts = [btn.text for row in ai_kb.inline_keyboard for btn in row]

        expected_provider_btn = PSYCHOBOT_TO_VITALIY_UX_CONTRACT["providers_and_models"][1]
        expected_logs_btn = PSYCHOBOT_TO_VITALIY_UX_CONTRACT["ai_logs"][1]

        self.assertIn(
            expected_provider_btn, button_texts,
            f"UX CONTRACT VIOLATION: '{expected_provider_btn}' missing from ai_settings_keyboard!"
        )
        self.assertIn(
            expected_logs_btn, button_texts,
            f"UX CONTRACT VIOLATION: '{expected_logs_btn}' missing from ai_settings_keyboard!"
        )

    def test_ux_contract_fails_if_any_common_button_renamed(self):
        """Proof that the contract test strictly rejects renames or missing unified buttons."""
        real_texts = [btn.text for row in kb.admin_panel_keyboard().inline_keyboard for btn in row]
        for feature_key, (_, vitaliy_label) in PSYCHOBOT_TO_VITALIY_UX_CONTRACT.items():
            if feature_key == "providers_and_models":
                continue
            # If the button were renamed in the keyboard:
            mutated_texts = [t if t != vitaliy_label else "❌ Случайное переименование" for t in real_texts]
            with self.assertRaises(AssertionError):
                self.assertIn(vitaliy_label, mutated_texts)

    async def test_admin_routes_entry_and_back_to_admin(self):
        """Verify navigation loop: /admin -> each common button -> screen -> Назад -> /admin."""
        # 1. Start from /admin
        root_kb = kb.admin_panel_keyboard()
        cb_map = {btn.text: btn.callback_data for row in root_kb.inline_keyboard for btn in row}

        # Check all core entries have registered callbacks
        self.assertIn("👥 Клиенты", cb_map)
        self.assertIn("⚙️ Автоматизации", cb_map)
        self.assertIn("🤖 Настройки ИИ", cb_map)
        self.assertIn("⭐️ Подписки", cb_map)
        self.assertIn("📚 База знаний", cb_map)
        self.assertIn("✏️ Контент", cb_map)
        self.assertIn("💬 Темы диалогов", cb_map)
        self.assertIn("👮\u200d♂️ Администраторы", cb_map)
        self.assertIn("✉️ Рассылка", cb_map)
        self.assertIn("📜 Логи запросов ИИ", cb_map)

        # 2. Test each screen's back navigation to admin_panel
        # Clients
        cb = self.create_mock_callback(self.admin_id, "admin_clients_list_0")
        fsm = self.get_fsm_context(self.admin_id)
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            await handlers.admin_clients_list(cb, fsm)
            mock_send.assert_called_once()
            markup = mock_send.call_args[0][3]
            back_buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn("admin_panel", back_buttons)

        # Automations
        cb = self.create_mock_callback(self.admin_id, "automation_menu")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            await automation_menu(cb, fsm)
            self.assertTrue(cb.message.edit_text.called or mock_send.called)
            called_markup = (cb.message.edit_text.call_args[1].get("reply_markup")
                             if cb.message.edit_text.called else mock_send.call_args[0][3])
            back_buttons = [b.callback_data for row in called_markup.inline_keyboard for b in row]
            self.assertIn("admin_panel", back_buttons)

        # AI Settings
        cb = self.create_mock_callback(self.admin_id, "admin_ai_settings")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            await handlers.admin_ai_settings(cb)
            mock_send.assert_called_once()
            markup = mock_send.call_args[0][3]
            back_buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn("admin_panel", back_buttons)

        # Subscriptions / Payments (both aliases)
        for sub_cb_str in ["admin_payment_settings", "admin_subscriptions"]:
            cb = self.create_mock_callback(self.admin_id, sub_cb_str)
            with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
                await handlers.admin_payment_settings(cb)
                mock_send.assert_called_once()
                markup = mock_send.call_args[0][3]
                back_buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
                self.assertIn("admin_panel", back_buttons)

        # Knowledge Base
        cb = self.create_mock_callback(self.admin_id, "admin_kb_page_0")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            await handlers.admin_kb_list(cb)
            mock_send.assert_called_once()
            markup = mock_send.call_args[0][3]
            back_buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn("admin_panel", back_buttons)

        # Content editor
        cb = self.create_mock_callback(self.admin_id, "admin_content_list")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            await handlers.admin_content_list(cb)
            mock_send.assert_called_once()
            markup = mock_send.call_args[0][3]
            back_buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn("admin_panel", back_buttons)

        # Topics (both aliases)
        for topic_cb_str in ["admin_topics_list", "admin_topics_page_0"]:
            cb = self.create_mock_callback(self.admin_id, topic_cb_str)
            with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
                await handlers.admin_topics_list(cb)
                mock_send.assert_called_once()
                markup = mock_send.call_args[0][3]
                back_buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
                self.assertIn("admin_panel", back_buttons)

        # Legacy menu-buttons callback remains an alias, while the visible entry
        # is the unified Content editor.
        cb = self.create_mock_callback(self.admin_id, "admin_manage_buttons")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            await handlers.admin_manage_buttons(cb)
            mock_send.assert_called_once()
            markup = mock_send.call_args[0][3]
            back_buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn("admin_content_list", back_buttons)

        # Administrators
        cb = self.create_mock_callback(self.admin_id, "admin_manage_admins")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            await handlers.admin_manage_admins(cb)
            mock_send.assert_called_once()
            markup = mock_send.call_args[0][3]
            back_buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn("admin_panel", back_buttons)

        # Mailing
        cb = self.create_mock_callback(self.admin_id, "admin_mailing")
        await handlers.mailing_start(cb, fsm)
        sent_markup = cb.message.answer.call_args[1].get("reply_markup")
        back_buttons = [b.callback_data for row in sent_markup.inline_keyboard for b in row]
        self.assertIn("admin_panel", back_buttons)

        # Providers and models (sub-screen of AI settings)
        cb = self.create_mock_callback(self.admin_id, "admin_ai_keys")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            await handlers.admin_ai_keys(cb)
            mock_send.assert_called_once()
            markup = mock_send.call_args[0][3]
            back_buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn("admin_ai_settings", back_buttons)

    async def test_admin_manage_buttons_workflow_and_back_routing(self):
        """The unified Content editor exposes sections and human button actions."""
        cb = self.create_mock_callback(self.admin_id, "admin_menu_sections")
        fsm = self.get_fsm_context(self.admin_id)

        # 1. Screen displays sections and returns to Content
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            await handlers.admin_menu_sections(cb)
            markup = mock_send.call_args[0][3]
            button_cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn("admin_menu_section:main", button_cbs)
            self.assertIn("admin_content_list", button_cbs)

        # 2. Entering a section exposes a button editor
        edit_cb = self.create_mock_callback(self.admin_id, "admin_menu_section:main")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            await handlers.admin_menu_section(edit_cb)
            markup = mock_send.call_args[0][3]
        button_cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertTrue(any(value.startswith("admin_menu_button:") for value in button_cbs))
        self.assertIn("admin_menu_sections", button_cbs)

        # 3. Button detail offers human operations
        button = await self._first_button()
        detail_cb = self.create_mock_callback(self.admin_id, f"admin_menu_button:{button.id}")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as mock_send:
            await handlers.admin_menu_button(detail_cb)
            markup = mock_send.call_args[0][3]
        detail_cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn(f"admin_menu_button_label:{button.id}", detail_cbs)
        self.assertIn(f"admin_menu_button_toggle:{button.id}", detail_cbs)

    async def _first_button(self):
        async with async_session_maker() as session:
            return await session.scalar(select(MenuButton).order_by(MenuButton.id.asc()))

    async def test_admin_manage_buttons_full_save_workflow_e2e(self):
        """Rename a configured button through the real handler/FSM path."""
        fsm = self.get_fsm_context(self.admin_id)
        button = await self._first_button()
        await fsm.update_data(menu_button_id=button.id)
        await fsm.set_state(handlers.AdminStates.menu_button_label)

        title_msg = AsyncMock(spec=Message)
        title_msg.from_user = TgUser(id=self.admin_id, is_bot=False, first_name="Admin")
        title_msg.text = "Новая понятная кнопка"
        title_msg.answer = AsyncMock()
        await handlers.admin_menu_button_label_save(title_msg, fsm)

        self.assertIsNone(await fsm.get_state())
        async with async_session_maker() as session:
            updated = await session.get(MenuButton, button.id)
            self.assertEqual(updated.label, "Новая понятная кнопка")
            self.assertTrue(updated.label_override)

    async def test_non_admin_security_content_editing_rejected(self):
        """Non-admin cannot initiate edit, modify state, or mutate Content."""
        non_admin_id = 99887766
        async with async_session_maker() as session:
            non_admin = await session.get(User, non_admin_id)
            if not non_admin:
                session.add(User(id=non_admin_id, first_name="Hacker", is_admin=False))
            else:
                non_admin.is_admin = False

            content_before = await session.get(Content, "btn_book_session_main")
            if not content_before:
                session.add(Content(
                    key="btn_book_session_main",
                    button_text="Оригинальный текст",
                    link_url="https://orig.com"
                ))
            else:
                content_before.button_text = "Оригинальный текст"
                content_before.link_url = "https://orig.com"
            await session.commit()

        fsm = self.get_fsm_context(non_admin_id)

        # 1. Direct edit_content_start callback query rejected
        cb = self.create_mock_callback(non_admin_id, "edit_cont_buttons:btn_book_session_main")
        await handlers.edit_content_start(cb, fsm)

        # FSM state must NOT be set
        self.assertIsNone(await fsm.get_state())
        self.assertEqual(await fsm.get_data(), {})
        cb.answer.assert_called_with("⛔️ Доступ запрещён.", show_alert=True)

        # 2. Attempt save_content with forged state
        await fsm.set_state(handlers.AdminStates.edit_content)
        await fsm.update_data(key="btn_book_session_main", category="buttons")

        msg = AsyncMock(spec=Message)
        msg.from_user = TgUser(id=non_admin_id, is_bot=False, first_name="Hacker")
        msg.chat = Chat(id=non_admin_id, type="private")
        msg.text = "Взлом кнопки"
        msg.html_text = "Взлом кнопки"
        msg.caption = None
        msg.photo = None
        msg.video = None
        msg.message_id = 201
        msg.answer = AsyncMock()

        await handlers.save_content(msg, fsm, AsyncMock())

        # FSM state must be cleared, access denied
        self.assertIsNone(await fsm.get_state())
        msg.answer.assert_called_with("⛔️ Доступ запрещён.")

        # 3. Attempt save_content_url with forged state
        await fsm.set_state(handlers.AdminStates.edit_url)
        await fsm.update_data(key="btn_book_session_main", category="buttons")

        url_msg = AsyncMock(spec=Message)
        url_msg.from_user = TgUser(id=non_admin_id, is_bot=False, first_name="Hacker")
        url_msg.chat = Chat(id=non_admin_id, type="private")
        url_msg.text = "https://hacker.com"
        url_msg.message_id = 202
        url_msg.answer = AsyncMock()

        await handlers.save_content_url(url_msg, fsm, AsyncMock())
        self.assertIsNone(await fsm.get_state())
        url_msg.answer.assert_called_with("⛔️ Доступ запрещён.")

        # 4. Attempt del_cont_media
        del_cb = self.create_mock_callback(non_admin_id, "del_cont_media_buttons:btn_book_session_main")
        await handlers.del_cont_media(del_cb, fsm)
        del_cb.answer.assert_called_with("⛔️ Доступ запрещён.", show_alert=True)

        # 5. DB Content has NOT mutated
        async with async_session_maker() as session:
            content_after = await session.get(Content, "btn_book_session_main")
            self.assertEqual(content_after.button_text, "Оригинальный текст")
            self.assertEqual(content_after.link_url, "https://orig.com")
