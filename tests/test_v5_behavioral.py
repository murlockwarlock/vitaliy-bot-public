import asyncio
import json
import os
import re
import secrets
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta
from sqlalchemy import select, update, delete
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, ReplyKeyboardRemove, InlineKeyboardMarkup

# Configure test environment before database imports
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/vitaliy_v5_test.db")
sys.modules.setdefault("vector_store", SimpleNamespace(get_collection=lambda: None, get_active_path=lambda: None))

from database import (
    Base, engine, async_session_maker, init_db, User, PromoCode, PaymentIntent,
    UserSubscription, SubscriptionPlan, SubscriptionConfig, ReferralConfig,
    ReferralPaymentLog, Content, ContentMedia, FunnelState, FunnelMeditation,
    FunnelConfig, FunnelEvent, FunnelLanding, LandingNotificationClaim,
    PaymentProvider, Transaction, ProdamusPayment, AIConfig
)
import handlers
import funnel
from time_utils import msk_now


class MockBot:
    def __init__(self):
        self.sent_messages = []
        self.edited_messages = []
        self.sent_photos = []
        self.sent_videos = []
        self.sent_audios = []
        self.chat_actions = []
        self.deleted_messages = []
        self.sent_invoices = []
        self.answered_pre_checkouts = []
        self.commands_set = []

    async def get_me(self):
        return SimpleNamespace(username="test_bot", id=123456789, first_name="TestBot")

    async def send_message(self, chat_id, text, **kwargs):
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=len(self.sent_messages) + 1,
            text=text,
            kwargs=kwargs,
            reply_markup=kwargs.get("reply_markup"),
            caption=kwargs.get("caption"),
            parse_mode=kwargs.get("parse_mode")
        )
        self.sent_messages.append(msg)
        return msg

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        entry = SimpleNamespace(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            kwargs=kwargs,
            reply_markup=kwargs.get("reply_markup")
        )
        self.edited_messages.append(entry)
        return entry

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        photo_entry = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=len(self.sent_messages) + 1,
            photo=photo,
            caption=caption,
            reply_markup=kwargs.get("reply_markup"),
            kwargs=kwargs
        )
        self.sent_photos.append(photo_entry)
        self.sent_messages.append(photo_entry)
        return photo_entry

    async def send_video(self, chat_id, video, caption=None, **kwargs):
        video_entry = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=len(self.sent_messages) + 1,
            video=video,
            caption=caption,
            reply_markup=kwargs.get("reply_markup"),
            kwargs=kwargs
        )
        self.sent_videos.append(video_entry)
        self.sent_messages.append(video_entry)
        return video_entry

    async def send_audio(self, chat_id, audio, caption=None, **kwargs):
        audio_entry = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=len(self.sent_messages) + 1,
            audio=audio,
            caption=caption,
            reply_markup=kwargs.get("reply_markup"),
            kwargs=kwargs
        )
        self.sent_audios.append(audio_entry)
        self.sent_messages.append(audio_entry)
        return audio_entry

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.chat_actions.append((chat_id, action, kwargs))
        return True

    async def delete_message(self, chat_id, message_id):
        self.deleted_messages.append((chat_id, message_id))
        return True

    async def send_invoice(self, chat_id, title, description, payload, provider_token, currency, prices, **kwargs):
        invoice_entry = SimpleNamespace(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=payload,
            provider_token=provider_token,
            currency=currency,
            prices=prices,
            kwargs=kwargs,
            message_id=len(self.sent_messages) + 1
        )
        self.sent_invoices.append(invoice_entry)
        self.sent_messages.append(invoice_entry)
        return invoice_entry

    async def answer_pre_checkout_query(self, pre_checkout_query_id, ok, error_message=None):
        self.answered_pre_checkouts.append((pre_checkout_query_id, ok, error_message))
        return True

    async def set_my_commands(self, commands, scope=None):
        self.commands_set.append((commands, scope))
        return True


class BehavioralV5Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await init_db()

    # =========================================================================
    # TC-01 to TC-10: Guardrail 11 - Customer Paths
    # =========================================================================

    async def test_path_menu_to_funnel(self):
        bot = MockBot()
        user_id = 1001
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            await session.commit()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
            answer=AsyncMock()
        )
        await funnel.start_funnel_intro(msg, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertIsNotNone(st)
            self.assertEqual(st.stage, "intro")
            self.assertTrue(st.is_active)

    async def test_path_intro_to_choice(self):
        bot = MockBot()
        user_id = 1002
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            session.add(FunnelMeditation(id=1, title="Практика спокойствия", duration_minutes=10, is_active=True))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        await funnel.show_meditation_choices(cb, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "choice")

    async def test_path_choice_to_card(self):
        bot = MockBot()
        user_id = 1003
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            session.add(FunnelMeditation(id=1, title="Практика спокойствия", duration_minutes=10, is_active=True))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        await funnel.show_meditation_card(cb, meditation_id=1, page=0, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "meditation_card")
            self.assertEqual(st.meditation_id, 1)
            self.assertIsNotNone(st.stage_timeout_at)  # 2h timeout set

    async def test_path_card_to_choice_back(self):
        bot = MockBot()
        user_id = 1004
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            session.add(FunnelMeditation(id=1, title="Практика спокойствия", duration_minutes=10, is_active=True))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        await funnel.show_meditation_choices(cb, page=0, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "choice")

    async def test_path_card_to_listening(self):
        bot = MockBot()
        user_id = 1005
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            session.add(FunnelMeditation(id=1, title="Практика спокойствия", duration_minutes=10, is_active=True, file_type="audio", file_id="audio_123"))
            await session.commit()

        bot.send_audio = AsyncMock()
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        await funnel.send_meditation(cb, meditation_id=1, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "listening")
            self.assertIsNotNone(st.stage_timeout_at)  # 2h timeout active

    async def test_path_listening_to_reflection(self):
        bot = MockBot()
        user_id = 1006
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        state = AsyncMock()
        await funnel.request_reflection(cb, state=state, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "waiting_reflection")
            self.assertIsNone(st.stage_timeout_at)  # timeout cleared outside meditation_card/listening

    async def test_path_reflection_to_ai_result(self):
        bot = MockBot()
        user_id = 1007
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser", current_dialogue_id=1))
            st = FunnelState(user_id=user_id, stage="waiting_reflection", is_active=True)
            session.add(st)
            await session.commit()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            chat=SimpleNamespace(id=user_id),
            text="Я почувствовал глубокое расслабление и прилив сил.",
            bot=bot,
            answer=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
        )
        state = AsyncMock()

        with patch(
            "funnel.get_ai_response_for_topic",
            new=AsyncMock(return_value=(
                '{"mode":"normal","has_experience":true,"has_question":false,'
                '"is_skeptic":false,"is_empty":false,"show_offer":false,'
                '"reply":"Отличный инсайт! Продолжай в том же духе.","bridge":""}'
            )),
        ):
            await funnel.deliver_analysis(msg, state, reflection_text=msg.text)

        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "post_analysis")
            self.assertIsNotNone(st.followup_sequence_anchor)
            self.assertIsNotNone(st.next_followup_at)  # Nurture campaign started
            self.assertEqual(st.reminder_count, 0)

    async def test_path_ai_result_to_block_6(self):
        bot = MockBot()
        user_id = 1008
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        await funnel.show_vitaly_method(cb, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "vitaly_method")

    async def test_path_block_6_to_block_7(self):
        bot = MockBot()
        user_id = 1009
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            await session.commit()

        # 1. Assert source markup
        markup = funnel.build_vitaly_method_keyboard()
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        target_btn = next((b for b in buttons if b.callback_data == "funnel_show_sales"), None)
        self.assertIsNotNone(target_btn)
        self.assertEqual(target_btn.text, "🔎 Узнать подробнее")

        # 2. Invoke registered callback handler
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            data=target_btn.callback_data,
            answer=AsyncMock()
        )
        await funnel.funnel_show_sales(cb, AsyncMock())
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "strategy_offer")

    async def test_path_offer_to_faq(self):
        bot = MockBot()
        user_id = 1010
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        await funnel.show_strategy_faq(cb, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "strategy_faq")

    async def test_path_faq_to_offer(self):
        bot = MockBot()
        user_id = 1011
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            await session.commit()

        # 1. Assert source markup
        markup = await funnel.build_faq_keyboard()
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        back_btn = next((b for b in buttons if b.callback_data == "funnel_show_sales"), None)
        self.assertIsNotNone(back_btn)
        self.assertEqual(back_btn.text, "⬅️ Вернуться назад")

        # 2. Invoke registered callback handler
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            data=back_btn.callback_data,
            answer=AsyncMock()
        )
        await funnel.funnel_show_sales(cb, AsyncMock())
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "strategy_offer")

    async def test_path_offer_to_objection_intro(self):
        bot = MockBot()
        user_id = 1012
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser", current_dialogue_id=1))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        state = AsyncMock()
        await funnel.start_objection_dialogue(cb, state=state, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "objection_chat")

    async def test_path_objection_intro_zero_buttons(self):
        bot = MockBot()
        user_id = 1013
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser", current_dialogue_id=1))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        state = AsyncMock()
        await funnel.start_objection_dialogue(cb, state=state, bot=bot)
        # Assert zero buttons
        self.assertEqual(len(bot.sent_messages), 1)
        self.assertIsNone(bot.sent_messages[0].reply_markup)

    async def test_path_objection_ai_result_exactly_two_buttons(self):
        bot = MockBot()
        user_id = 1014
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser", current_dialogue_id=1, ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            st = FunnelState(user_id=user_id, stage="objection_chat", is_active=True)
            session.add(st)
            await session.commit()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            chat=SimpleNamespace(id=user_id),
            text="Я сомневаюсь, поможет ли мне эта программа.",
            bot=bot,
            answer=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
        )
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"objection_dialogue_id": 1})

        with patch("funnel.get_ai_response_for_topic", new=AsyncMock(return_value="Сомнения естественны. Давай разберем...")):
            await funnel.handle_objection_message(msg, state)

        # Check last message sent
        last_msg = bot.sent_messages[-1]
        markup = last_msg.reply_markup
        self.assertIsNotNone(markup)
        # Count inline buttons
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 3)
        self.assertEqual(buttons[0].text, "🎯 Записаться на сессию")
        self.assertEqual(buttons[0].callback_data, "funnel_open_landing")
        self.assertEqual(buttons[1].text, "⬅️ Вернуться назад")
        self.assertEqual(buttons[1].callback_data, "funnel_show_sales")
        self.assertEqual(buttons[2].text, "Главное меню")
        self.assertEqual(buttons[2].callback_data, "funnel_to_main")

    async def test_path_objection_result_to_offer(self):
        bot = MockBot()
        user_id = 1015
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        await funnel.show_strategy_sales(cb, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "strategy_offer")

    async def test_path_offer_to_landing(self):
        bot = MockBot()
        user_id = 1016
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            session.add(FunnelState(user_id=user_id, stage="strategy_offer", is_active=True))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        await funnel.open_landing(cb, bot=bot)
        async with async_session_maker() as session:
            landings = (await session.execute(select(FunnelLanding).where(FunnelLanding.user_id == user_id))).scalars().all()
            self.assertEqual(len(landings), 1)
            self.assertEqual(landings[0].status, "opened")

    async def test_path_landing_to_offer(self):
        bot = MockBot()
        user_id = 1017
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            session.add(FunnelState(user_id=user_id, stage="strategy_offer", is_active=True))
            await session.commit()

        # 1. Assert source markup
        markup = await funnel.build_landing_keyboard_for_user(user_id)
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        back_btn = next((b for b in buttons if b.callback_data == "funnel_show_sales"), None)
        self.assertIsNotNone(back_btn)
        self.assertEqual(back_btn.text, "⬅️ Вернуться назад")

        # 2. Invoke registered callback handler
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            data=back_btn.callback_data,
            answer=AsyncMock()
        )
        await funnel.funnel_show_sales(cb, AsyncMock())
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "strategy_offer")

    async def test_path_refusal_exactly_three_buttons(self):
        markup = await funnel.build_no_strategy_keyboard()
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 3)

    async def test_path_refusal_channel(self):
        markup = await funnel.build_no_strategy_keyboard()
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        channel_btn = buttons[0]
        self.assertIsNotNone(channel_btn.url)
        self.assertIn("t.me", channel_btn.url)

    async def test_path_refusal_book(self):
        bot = MockBot()
        user_id = 1019
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            data="funnel_get_book",
            answer=AsyncMock()
        )
        state = AsyncMock()
        with patch("handlers.show_books", new=AsyncMock()) as mock_books:
            await funnel.funnel_get_book(cb, state=state)
            mock_books.assert_awaited_once()

    async def test_path_refusal_7day_reminder(self):
        bot = MockBot()
        user_id = 1020
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, answer=AsyncMock()),
            bot=bot,
            data="funnel_remind_7d",
            answer=AsyncMock()
        )
        state = AsyncMock()
        await funnel.funnel_remind_7d(cb, state=state)

        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "remarketing_7d")
            self.assertTrue(st.is_active)
            self.assertIsNotNone(st.next_followup_at)
            self.assertEqual(st.reminder_count, 0)

    async def test_path_resume_exact_three_buttons_and_destinations(self):
        markup = funnel.resume_keyboard()
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 3)

        # Exact callback data and destinations
        callbacks = [btn.callback_data for btn in buttons]
        self.assertEqual(callbacks, ["funnel_resume_choice", "funnel_resume_sales", "funnel_resume_materials"])

        # Test destination 1: funnel_resume_sales -> show_strategy_sales (Block 7)
        bot = MockBot()
        cb_sales = SimpleNamespace(
            from_user=SimpleNamespace(id=1021, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=1021), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        async with async_session_maker() as session:
            session.add(User(id=1021, first_name="TestUser"))
            await session.commit()

        await funnel.funnel_resume_sales(cb_sales, AsyncMock())
        async with async_session_maker() as session:
            st = await session.get(FunnelState, 1021)
            self.assertEqual(st.stage, "strategy_offer")  # Block 7!

        # Test legacy callback: funnel_resume_strategy -> show_vitaly_method (Block 6)
        cb_strat = SimpleNamespace(
            from_user=SimpleNamespace(id=1022, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=1022), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        async with async_session_maker() as session:
            session.add(User(id=1022, first_name="TestUser"))
            await session.commit()

        await funnel.funnel_resume_strategy(cb_strat, AsyncMock())
        async with async_session_maker() as session:
            st2 = await session.get(FunnelState, 1022)
            self.assertEqual(st2.stage, "vitaly_method")  # Block 6 preserved!

    # =========================================================================
    # TC-11 to TC-13: Guardrail 12 - Broad Tests Made Concrete
    # =========================================================================

    async def test_telegram_command_scopes(self):
        import main
        bot = MockBot()
        await main.set_commands(bot)
        self.assertGreaterEqual(len(bot.commands_set), 1)
        # Check default commands
        default_cmds = [cmd.command for cmd in bot.commands_set[0][0]]
        self.assertIn("start", default_cmds)
        self.assertIn("menu", default_cmds)
        self.assertIn("settings", default_cmds)
        self.assertIn("subscription", default_cmds)

    async def test_cmd_menu_opens_main_menu(self):
        bot = MockBot()
        user_id = 1030
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            await session.commit()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            chat=SimpleNamespace(id=user_id),
            bot=bot
        )
        await handlers.cmd_menu(msg, bot)
        # Verify no placeholder message is created and deleted (0 deleted messages)
        self.assertEqual(len(bot.deleted_messages), 0)
        # Verify 2 messages: Message 1 (Welcome + ReplyKeyboardRemove) and Message 2 (Text-only main menu)
        self.assertEqual(len(bot.sent_messages), 2)
        from aiogram.types import ReplyKeyboardRemove
        self.assertIsInstance(bot.sent_messages[0].reply_markup, ReplyKeyboardRemove)
        self.assertIsNotNone(bot.sent_messages[1].reply_markup)

    async def test_cmd_settings_opens_profile(self):
        bot = MockBot()
        user_id = 1031
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            await session.commit()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            chat=SimpleNamespace(id=user_id),
            bot=bot
        )
        state = AsyncMock()
        await handlers.cmd_settings(msg, state, bot)
        self.assertEqual(len(bot.sent_messages), 1)
        self.assertTrue("Профиль" in bot.sent_messages[0].text or "Настройки" in bot.sent_messages[0].text)

    async def test_cmd_subscription_opens_subscription(self):
        bot = MockBot()
        user_id = 1032
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            # Seed custom subscription_info text
            content = await session.get(Content, "subscription_info")
            if not content:
                session.add(Content(key="subscription_info", text_content="Специальный текст подписки"))
            else:
                content.text_content = "Специальный текст подписки"
            await session.commit()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            chat=SimpleNamespace(id=user_id),
            bot=bot
        )
        await handlers.cmd_subscription(msg, bot)
        self.assertEqual(len(bot.sent_messages), 1)
        self.assertIn("Специальный текст подписки", bot.sent_messages[0].text)
        # Verify main menu return button present
        markup = bot.sent_messages[0].reply_markup
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for btn in buttons]
        self.assertIn("back_to_main_menu_text", callbacks)

    async def test_admin_content_btn_transfo_edit(self):
        async with async_session_maker() as session:
            admin = await session.get(User, 1)
            if not admin:
                session.add(User(id=1, first_name="Admin", is_admin=True))
            else:
                admin.is_admin = True
            content = await session.get(Content, "btn_transfo")
            if not content:
                session.add(Content(key="btn_transfo", button_text="Старый текст"))
            else:
                content.button_text = "Старый текст"
            await session.commit()

        # Simulate saving text for btn_transfo
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"key": "btn_transfo", "category": "main"})
        msg = SimpleNamespace(
            text="Новый Транскоучинг",
            html_text="Новый Транскоучинг",
            caption=None,
            photo=None,
            video=None,
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=1)
        )
        bot = MockBot()
        await handlers.save_content(msg, state, bot)

        # Check that extended_menu_keyboard uses the new button text!
        import keyboards as kb
        kb_markup = await kb.extended_menu_keyboard()
        buttons = [btn for row in kb_markup.inline_keyboard for btn in row]
        texts = [btn.text for btn in buttons]
        self.assertIn("Новый Транскоучинг", texts)

    async def test_admin_normal_and_funnel_content_media_deletion(self):
        async with async_session_maker() as session:
            admin = await session.get(User, 1)
            if not admin:
                session.add(User(id=1, first_name="Admin", is_admin=True))
            else:
                admin.is_admin = True
            session.add(Content(key="test_cont_media", text_content="Текст с медиа"))
            session.add(ContentMedia(content_key="test_cont_media", file_type="photo", file_id="p123"))
            session.add(ContentMedia(content_key="funnel_intro", file_type="photo", file_id="p456"))
            await session.commit()

        # Normal content media deletion
        cb_norm = SimpleNamespace(
            data="del_cont_media_main:test_cont_media",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock()
        )
        with patch("handlers.edit_content_start", new=AsyncMock()):
            await handlers.del_cont_media(cb_norm, AsyncMock())

        # Funnel content media deletion
        cb_funnel = SimpleNamespace(
            data="del_fcont_media_funnel_intro",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock()
        )
        with patch("funnel.show_admin_funnel_content", new=AsyncMock()):
            await funnel.admin_del_fcont_media(cb_funnel, AsyncMock())

        async with async_session_maker() as session:
            m1 = (await session.execute(select(ContentMedia).where(ContentMedia.content_key == "test_cont_media"))).scalars().all()
            m2 = (await session.execute(select(ContentMedia).where(ContentMedia.content_key == "funnel_intro"))).scalars().all()
            self.assertEqual(len(m1), 0)
            self.assertEqual(len(m2), 0)

    async def test_referral_raw_link_code_tag(self):
        bot = MockBot()
        user_id = 1040
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ReferralUser"))
            await session.commit()

        answer_mock = AsyncMock()
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="ReferralUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock(), answer=answer_mock),
            bot=bot,
            answer=AsyncMock()
        )
        await handlers.ref_create_link(cb, bot)
        answer_mock.assert_called_once()
        sent_text = answer_mock.call_args[0][0]
        self.assertIn(f"<code>https://t.me/test_bot?start={user_id}</code>", sent_text)

    async def test_webhook_admin_notifications(self):
        import landing_webhook
        bot = MockBot()
        admin_id = 9991
        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            await session.commit()

        payload_form = {
            "event": "form_filled",  # Raw external event normalized to form_submitted
            "user_id": 1050,
            "name": "Тестовый",
            "email": "test@example.invalid",
            "phone": "TEST_PHONE
        }
        with patch("landing_webhook.get_all_admin_ids", new=AsyncMock(return_value=[admin_id])):
            await landing_webhook.handle_incoming_webhook(payload_form, bot)

        self.assertEqual(len(bot.sent_messages), 2)
        self.assertEqual(bot.sent_messages[0].chat.id, 1050)
        self.assertIn("Твоя анкета успешно принята", bot.sent_messages[0].text)
        self.assertEqual(bot.sent_messages[1].chat.id, admin_id)
        self.assertIn("Заполнена анкета", bot.sent_messages[1].text)
        self.assertIn("test@example.invalid", bot.sent_messages[1].text)

    # =========================================================================
    # TC-14: Guardrail 8 - Concurrency-safe AI Disclaimer CAS
    # =========================================================================

    async def test_ai_disclaimer_concurrent_cas(self):
        bot = MockBot()
        user_id = 1060
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="AITestUser", ai_disclaimer_shown=False))
            await session.commit()

        async def call_cas():
            async with async_session_maker() as session:
                await handlers.ensure_ai_disclaimer_sent(session, bot, user_id, user_id)

        # Run two concurrent calls
        await asyncio.gather(call_cas(), call_cas())

        # Assert exactly ONE disclaimer was sent
        disclaimers = [m for m in bot.sent_messages if "консультацию" in (m.text or "").lower() or "кризис" in (m.text or "").lower()]
        self.assertEqual(len(disclaimers), 1)

        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertTrue(u.ai_disclaimer_shown)

    # =========================================================================
    # TC-15: Guardrail 13 - Atomic Referral Registration
    # =========================================================================

    async def test_atomic_referral_registration_concurrent(self):
        bot = MockBot()
        referrer_id = 1070
        new_user_id = 1071
        async with async_session_maker() as session:
            session.add(User(id=referrer_id, first_name="Referrer", referral_balance=0))
            sub_c = await session.get(SubscriptionConfig, 1)
            sub_c.referral_enabled = True
            ref_c = await session.get(ReferralConfig, 1)
            ref_c.points_per_invite = 50
            await session.commit()

        async def register(i):
            msg = SimpleNamespace(
                from_user=SimpleNamespace(id=new_user_id, username=f"newbie_{i}", first_name="Newbie"),
                chat=SimpleNamespace(id=new_user_id),
                bot=bot,
                answer=AsyncMock()
            )
            command = SimpleNamespace(args=f"{referrer_id}")
            state = AsyncMock()
            await handlers.cmd_start(msg, state, command)

        # Run two concurrent start requests
        await asyncio.gather(register(1), register(2))

        async with async_session_maker() as session:
            new_u = await session.get(User, new_user_id)
            self.assertEqual(new_u.referrer_id, referrer_id)
            ref_u = await session.get(User, referrer_id)
            # Exactly 50 points granted once! Not doubled!
            self.assertEqual(ref_u.referral_balance, 50)

    # =========================================================================
    # TC-16: Guardrail 2 & 3 - Double-spend protection & Pricing Snapshot
    # =========================================================================

    async def test_payment_intent_pricing_snapshot_and_expiry(self):
        bot = MockBot()
        user_id = 1080
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="PayingUser", referral_balance=100))
            session.add(SubscriptionPlan(id=1, name="Месяц", price=1000.0, duration_value=1, duration_unit="months", is_active=True))
            await session.commit()

        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            plan = await session.get(SubscriptionPlan, 1)
            price_info = handlers.calculate_checkout_price(u, plan, points_to_use=50)
            self.assertEqual(price_info["gross_amount"], 1000.0)
            self.assertEqual(price_info["points_reserved"], 50)
            self.assertEqual(price_info["final_amount"], 950.0)

            # Create PaymentIntent
            intent = await handlers.create_payment_intent(
                session, user_id=user_id, plan_id=1, provider="telegram_pay", points_to_use=50
            )
            self.assertEqual(intent.final_amount, 950.0)
            self.assertEqual(intent.status, "pending")
            token = intent.token

        # Pre-checkout validation
        async with async_session_maker() as session:
            # 1. Valid pending intent passes
            cur_intent = (await session.execute(select(PaymentIntent).where(PaymentIntent.token == token))).scalar_one()
            self.assertEqual(cur_intent.status, "pending")
            self.assertGreater(cur_intent.expires_at, msk_now())

            # 2. Expire intent and test rejection
            cur_intent.expires_at = msk_now() - timedelta(minutes=1)
            await session.commit()

            # Try settle expired intent
            with self.assertRaisesRegex(ValueError, "expired"):
                await handlers.settle_payment_intent(session, bot, intent=cur_intent, payment_charge_id="tg_123")

    # =========================================================================
    # TC-17: Guardrail 4 - Referral Subsequent Payments (Cash Only)
    # =========================================================================

    async def test_referral_subsequent_cash_payments(self):
        bot = MockBot()
        referrer_id = 1090
        referee_id = 1091
        async with async_session_maker() as session:
            session.add(User(id=referrer_id, first_name="RefBoss", referral_balance=0))
            session.add(User(id=referee_id, first_name="Customer", referrer_id=referrer_id))
            sub_c = await session.get(SubscriptionConfig, 1)
            sub_c.referral_enabled = True
            sub_c.referral_reward_mode = "points"
            sub_c.referral_first_pay_percent = 20     # 20% on 1st payment
            sub_c.referral_recurring_pay_percent = 10  # 10% on subsequent payments
            await session.commit()

        # 1st payment: 1000 руб -> 200 points
        async with async_session_maker() as session:
            await handlers.grant_referral_payment_reward(
                session, bot, paying_user_id=referee_id, amount=1000.0, source_payment_key="pay_1"
            )
            await session.commit()

        async with async_session_maker() as session:
            boss = await session.get(User, referrer_id)
            self.assertEqual(boss.referral_balance, 200)

        # 2nd payment: 2000 руб -> 10% = 200 points
        async with async_session_maker() as session:
            await handlers.grant_referral_payment_reward(
                session, bot, paying_user_id=referee_id, amount=2000.0, source_payment_key="pay_2"
            )
            await session.commit()

        async with async_session_maker() as session:
            boss = await session.get(User, referrer_id)
            self.assertEqual(boss.referral_balance, 400)  # 200 + 200

        # Duplicate webhook with same source_payment_key -> no double reward
        async with async_session_maker() as session:
            await handlers.grant_referral_payment_reward(
                session, bot, paying_user_id=referee_id, amount=2000.0, source_payment_key="pay_2"
            )
            await session.commit()

        async with async_session_maker() as session:
            boss = await session.get(User, referrer_id)
            self.assertEqual(boss.referral_balance, 400)

        # Zero-cash checkout -> 0 reward
        async with async_session_maker() as session:
            await handlers.grant_referral_payment_reward(
                session, bot, paying_user_id=referee_id, amount=0.0, source_payment_key="pay_zero"
            )
            await session.commit()

        async with async_session_maker() as session:
            boss = await session.get(User, referrer_id)
            self.assertEqual(boss.referral_balance, 400)

    # =========================================================================
    # TC-18: Guardrail 14 - Nurture campaign decoupled from generic set_stage
    # =========================================================================

    async def test_nurture_scheduling_decoupled_from_set_stage(self):
        user_id = 1100
        anchor_time = datetime(2026, 10, 1, 12, 0)
        followup_time = datetime(2026, 10, 2, 12, 0)

        async with async_session_maker() as session:
            st = FunnelState(
                user_id=user_id,
                stage="post_analysis",
                is_active=True,
                followup_sequence_anchor=anchor_time,
                next_followup_at=followup_time,
                reminder_count=1
            )
            session.add(st)
            await session.commit()

        # Call generic set_stage to navigate to strategy_offer
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            await funnel.set_stage(session, st, "strategy_offer", active=True)
            await session.commit()

        # Assert anchor, count, and next_followup were NOT restarted!
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "strategy_offer")
            self.assertEqual(st.followup_sequence_anchor, anchor_time)
            self.assertEqual(st.next_followup_at, followup_time)
            self.assertEqual(st.reminder_count, 1)

    # =========================================================================
    # TC-19: Guardrail 10 - Tone of Voice Verification
    # =========================================================================

    async def test_tone_of_voice_defaults(self):
        # Verify that default seeded content does not contain formal "вы / вас / вам / вами"
        async with async_session_maker() as session:
            contents = (await session.execute(select(Content))).scalars().all()
            for c in contents:
                if c.key.startswith("test_") or c.key == "subscription_info":
                    continue
                text = (c.text_content or "").lower()
                for formal_word in [" вы ", " вас ", " вам ", " вами "]:
                    # Exception: direct quotes or admin text if any
                    self.assertNotIn(formal_word, f" {text} ", f"Found formal word '{formal_word.strip()}' in default content key '{c.key}'")


    # =========================================================================
    # PR #1 Review Defect Regression Tests (P0 & P1)
    # =========================================================================

    async def test_block_6_keyboard_exact_labels_and_callbacks(self):
        markup = funnel.build_vitaly_method_keyboard()
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 3)
        self.assertEqual(buttons[0].text, "🔎 Узнать подробнее")
        self.assertEqual(buttons[0].callback_data, "funnel_show_sales")
        self.assertEqual(buttons[1].text, "Пока нет, спасибо")
        self.assertEqual(buttons[1].callback_data, "funnel_interest_no")
        self.assertEqual(buttons[2].text, "Главное меню")
        self.assertEqual(buttons[2].callback_data, "funnel_to_main")

    async def test_block_7_keyboard_exact_labels_count_callbacks(self):
        markup = funnel.build_strategy_offer_keyboard()
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 6)
        callbacks = [btn.callback_data for btn in buttons]
        self.assertIn("funnel_open_landing", callbacks)
        self.assertIn("funnel_show_faq", callbacks)
        self.assertIn("funnel_start_objections", callbacks)
        self.assertIn("funnel_vitaly_method", callbacks)
        self.assertIn("menu_funnel_start", callbacks)
        self.assertIn("funnel_to_main", callbacks)

    async def test_faq_back_button_exact_label_and_handler(self):
        markup = await funnel.build_faq_keyboard()
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        back_btn = next((b for b in buttons if b.callback_data == "funnel_show_sales"), None)
        self.assertIsNotNone(back_btn)
        self.assertEqual(back_btn.text, "⬅️ Вернуться назад")

        bot = MockBot()
        user_id = 2003
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            await session.commit()
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            data=back_btn.callback_data,
            answer=AsyncMock()
        )
        await funnel.funnel_show_sales(cb, AsyncMock())
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "strategy_offer")

    async def test_landing_card_back_button_and_tracked_url(self):
        user_id = 2004
        markup = await funnel.build_landing_keyboard_for_user(user_id)
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 2)
        booking_btn = buttons[0]
        self.assertTrue(booking_btn.url.startswith("https://psiholog-naumov.ru/str_sesiya_tt"))
        self.assertIn(f"bot_uid={user_id}", booking_btn.url)
        self.assertIn("landing_token=", booking_btn.url)

        back_btn = buttons[1]
        self.assertEqual(back_btn.text, "⬅️ Вернуться назад")
        self.assertEqual(back_btn.callback_data, "funnel_to_main")

        bot = MockBot()
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            await session.commit()
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            data=back_btn.callback_data,
            answer=AsyncMock()
        )
        await funnel.funnel_to_main(cb, AsyncMock())
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertFalse(st.is_active)

    async def test_objection_ai_result_keyboard_labels_and_callbacks(self):
        markup = funnel.objection_response_keyboard()
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 3)
        self.assertEqual(buttons[0].text, "🎯 Записаться на сессию")
        self.assertEqual(buttons[0].callback_data, "funnel_open_landing")
        self.assertEqual(buttons[1].text, "⬅️ Вернуться назад")
        self.assertEqual(buttons[1].callback_data, "funnel_show_sales")
        self.assertEqual(buttons[2].text, "Главное меню")
        self.assertEqual(buttons[2].callback_data, "funnel_to_main")

    async def test_objection_generation_ux_typing_action_and_wait_text(self):
        bot = MockBot()
        user_id = 2006
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser", current_dialogue_id=1, ai_disclaimer_accepted_at=msk_now(), ai_disclaimer_version=1))
            session.add(FunnelState(user_id=user_id, stage="objection_chat", is_active=True))
            await session.commit()

        wait_msg = SimpleNamespace(delete=AsyncMock())
        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            chat=SimpleNamespace(id=user_id),
            text="Слишком дорого",
            bot=bot,
            answer=AsyncMock(return_value=wait_msg)
        )
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"objection_dialogue_id": 1})

        with patch("funnel.get_ai_response_for_topic", new=AsyncMock(return_value="Понимаю твое сомнение...")):
            await funnel.handle_objection_message(msg, state)

        self.assertTrue(any(a[0] == user_id and a[1] == "typing" for a in bot.chat_actions))
        msg.answer.assert_called_with("💬 Готовлю лучший ответ для тебя...")

    async def test_refusal_keeps_nurture_campaign_when_enabled(self):
        bot = MockBot()
        user_id = 2007
        due_time = msk_now() + timedelta(hours=1)
        anchor_time = msk_now()

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            session.add(FunnelState(
                user_id=user_id,
                stage="strategy_offer",
                is_active=True,
                followup_sequence_anchor=anchor_time,
                next_followup_at=due_time,
                reminder_count=0
            ))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        # Click refusal: пока нет, спасибо
        await funnel.show_no_strategy_options(cb, bot=bot)

        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "refusal_declined")
            self.assertTrue(st.is_active)
            self.assertEqual(st.next_followup_at, due_time)
            self.assertEqual(st.followup_sequence_anchor, anchor_time)

        # Run follow-up dispatcher at the scheduled nurture due time.
        with patch("funnel.msk_now", return_value=due_time + timedelta(minutes=5)):
            await funnel.process_funnel_followups(bot, single_iteration=True)

        user_msgs = [m for m in bot.sent_messages if getattr(m.chat, "id", None) == user_id]
        self.assertEqual(len(user_msgs), 2)

        # Now test clicking ⏰ Напомнить мне через неделю
        cb_remind = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TestUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=2, answer=AsyncMock()),
            bot=bot,
            data="funnel_remind_7d",
            answer=AsyncMock()
        )
        await funnel.funnel_remind_7d(cb_remind, AsyncMock())
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "remarketing_7d")
            self.assertTrue(st.is_active)
            self.assertIsNotNone(st.next_followup_at)

    async def test_final_nurture_touch_deactivates_funnel_and_unblocks_main_bot(self):
        bot = MockBot()
        user_id = 2008
        now = msk_now()
        anchor = now - timedelta(hours=10)

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser"))
            f_conf = await session.get(FunnelConfig, 1)
            if not f_conf:
                session.add(FunnelConfig(id=1, reminder_intervals="1"))
            else:
                f_conf.reminder_intervals = "1"
            st = FunnelState(
                user_id=user_id,
                stage="post_analysis",
                is_active=True,
                followup_sequence_anchor=anchor,
                next_followup_at=now - timedelta(minutes=1),
                reminder_count=0
            )
            session.add(st)
            await session.commit()

        await funnel.process_funnel_followups(bot, single_iteration=True)

        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertFalse(st.is_active)
            self.assertIsNone(st.next_followup_at)
            self.assertEqual(st.stage, "completed_unconverted")

        sent_before = len(bot.sent_messages)
        await funnel.process_funnel_followups(bot, single_iteration=True)
        self.assertEqual(len(bot.sent_messages), sent_before)

        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertFalse(st.is_active)

    async def test_validate_nurture_intervals_matrix(self):
        self.assertEqual(funnel.validate_nurture_intervals("1,2,3,7,14"), [1, 2, 3, 7, 14])
        self.assertEqual(funnel.validate_nurture_intervals("1,4,9"), [1, 4, 9])
        self.assertIsNone(funnel.validate_nurture_intervals("1,1,3"))
        self.assertIsNone(funnel.validate_nurture_intervals("3,2,5"))
        self.assertIsNone(funnel.validate_nurture_intervals("0,2"))
        self.assertIsNone(funnel.validate_nurture_intervals("-1,2"))
        self.assertIsNone(funnel.validate_nurture_intervals("1,2,3,4,5,6"))

        async with async_session_maker() as session:
            cfg = await session.get(FunnelConfig, 1)
            original = cfg.reminder_intervals if cfg else "1,2,3,7,14"

        msg = SimpleNamespace(
            text="1,1,3",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=1)
        )
        state = AsyncMock()
        await funnel.save_nurture_intervals(msg, state)
        self.assertIn("строго возрастающих", msg.answer.call_args[0][0])

        async with async_session_maker() as session:
            cfg2 = await session.get(FunnelConfig, 1)
            self.assertEqual(cfg2.reminder_intervals, original)

        bot = MockBot()
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=1),
                message_id=1,
                edit_text=AsyncMock(side_effect=lambda text, **kw: bot.send_message(1, text, **kw)),
                content_type="text"
            ),
            bot=bot,
            answer=AsyncMock()
        )
        await funnel.show_admin_funnel_config(cb)
        self.assertGreaterEqual(len(bot.sent_messages), 1)
        sent_text = bot.sent_messages[-1].text
        self.assertIn("Количество касаний:", sent_text)
        self.assertIn("из 5", sent_text)

    async def test_legacy_followup_artifacts_retired(self):
        self.assertFalse(hasattr(funnel, "REMINDER_STAGE_KEYS"))
        self.assertFalse(hasattr(funnel, "get_followup_text"))
        self.assertFalse(hasattr(funnel, "schedule_followup"))

    async def test_automatic_checkout_referral_points_pricing_snapshot(self):
        user_id = 2011
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser", referral_balance=250))
            session.add(SubscriptionPlan(id=11, name="Месяц", price=1000.0, duration_value=1, duration_unit="months", is_active=True))
            await session.commit()

        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            plan = await session.get(SubscriptionPlan, 11)
            price_info = handlers.calculate_checkout_price(u, plan)
            self.assertEqual(price_info["points_reserved"], 250)
            self.assertEqual(price_info["final_amount"], 750.0)

            intent = await handlers.create_payment_intent(session, user_id=user_id, plan_id=11, provider="telegram_pay")
            self.assertEqual(intent.points_reserved, 250)
            self.assertEqual(intent.final_amount, 750.0)
            self.assertEqual(intent.gross_amount, 1000.0)

    async def test_concurrent_payment_intent_reservation_accounting(self):
        user_id = 2012
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TestUser", referral_balance=1000))
            session.add(SubscriptionPlan(id=12, name="План 800", price=800.0, duration_value=1, duration_unit="months", is_active=True))
            await session.commit()

        barrier = asyncio.Barrier(2)

        async def worker(idx):
            async with async_session_maker() as session:
                await barrier.wait()
                intent = await handlers.create_payment_intent(session, user_id=user_id, plan_id=12, provider="telegram_pay")
                await session.commit()
                return intent

        res = await asyncio.gather(worker(1), worker(2))

        async with async_session_maker() as session:
            intents = (await session.execute(
                select(PaymentIntent).where(PaymentIntent.user_id == user_id).order_by(PaymentIntent.id)
            )).scalars().all()
            self.assertEqual(len(intents), 2)
            reserved_points = sorted([intents[0].points_reserved, intents[1].points_reserved])
            self.assertEqual(reserved_points, [200, 800])
            total_reserved = sum(i.points_reserved for i in intents if i.status == "pending")
            u = await session.get(User, user_id)
            self.assertLessEqual(total_reserved, u.referral_balance)
            self.assertEqual(total_reserved, 1000)

    async def test_real_reservation_accounting_concurrent_intents(self):
        await self.test_concurrent_payment_intent_reservation_accounting()

    async def test_zero_cash_checkout_instant_settlement(self):
        bot = MockBot()
        user_id = 2013
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ZeroUser", referral_balance=1000))
            session.add(SubscriptionPlan(id=13, name="План 500", price=500.0, duration_value=1, duration_unit="months", is_active=True))
            await session.commit()

        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(session, user_id=user_id, plan_id=13, provider="telegram_pay", bot=bot)
            self.assertEqual(intent.points_reserved, 500)
            self.assertEqual(intent.final_amount, 0.0)
            self.assertEqual(intent.status, "paid")
            self.assertIsNotNone(intent.paid_at)

        # Assert subscription exists before any explicit settle call
        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertEqual(u.referral_balance, 500)
            sub = (await session.execute(select(UserSubscription).where(UserSubscription.user_id == user_id))).scalar_one_or_none()
            self.assertIsNotNone(sub)
            self.assertGreater(sub.end_date, msk_now())
            txs = (await session.execute(select(Transaction).where(Transaction.user_id == user_id))).scalars().all()
            self.assertEqual(len(txs), 1)

        # Repeated settlement of same zero-cash intent MUST NOT duplicate effects
        async with async_session_maker() as session:
            intent = (await session.execute(select(PaymentIntent).where(PaymentIntent.user_id == user_id))).scalar_one()
            await handlers.settle_payment_intent(session, bot, intent=intent, payment_charge_id="zero_1")
            await session.commit()

        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertEqual(u.referral_balance, 500)
            txs2 = (await session.execute(select(Transaction).where(Transaction.user_id == user_id))).scalars().all()
            self.assertEqual(len(txs2), 1)

    async def test_zero_cash_settlement_idempotency(self):
        await self.test_zero_cash_checkout_instant_settlement()

    async def test_prodamus_webhook_payment_intent_end_to_end(self):
        import prodamus_webhooks
        bot = MockBot()
        referrer_id = 20140
        user_id = 20141

        async with async_session_maker() as session:
            session.add(User(id=referrer_id, first_name="Boss", referral_balance=0))
            session.add(User(id=user_id, first_name="Client", referrer_id=referrer_id, referral_balance=200))
            session.add(SubscriptionPlan(id=14, name="План 1000", price=1000.0, duration_value=1, duration_unit="months", is_active=True))
            sub_c = await session.get(SubscriptionConfig, 1)
            sub_c.referral_enabled = True
            sub_c.referral_reward_mode = "points"
            sub_c.referral_first_pay_percent = 10
            await session.commit()

        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(session, user_id=user_id, plan_id=14, provider="prodamus")
            self.assertEqual(intent.points_reserved, 200)
            self.assertEqual(intent.final_amount, 800.0)
            token = intent.token
            await session.commit()

        order_num = f"pi_{token}"
        payload = {
            "order_num": order_num,
            "order_id": order_num,
            "sum": "800.00",
            "payment_status": "success",
            "payment_status_description": "Успешная оплата",
            "customer_extra": f"{user_id}:14"
        }

        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            resp = await prodamus_webhooks.process_prodamus_payment(payload, bot=bot)
            self.assertEqual(resp.status, 200)

        async with async_session_maker() as session:
            cur_intent = (await session.execute(select(PaymentIntent).where(PaymentIntent.token == token))).scalar_one()
            self.assertEqual(cur_intent.status, "paid")
            self.assertIsNotNone(cur_intent.paid_at)

            u = await session.get(User, user_id)
            self.assertEqual(u.referral_balance, 0)

            sub = (await session.execute(select(UserSubscription).where(UserSubscription.user_id == user_id))).scalar_one()
            self.assertGreater(sub.end_date, msk_now())

            boss = await session.get(User, referrer_id)
            self.assertEqual(boss.referral_balance, 80)

        # Repeated webhook -> no duplicate financial side effects
        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            resp2 = await prodamus_webhooks.process_prodamus_payment(payload, bot=bot)
            self.assertEqual(resp2.status, 200)

        async with async_session_maker() as session:
            u2 = await session.get(User, user_id)
            self.assertEqual(u2.referral_balance, 0)
            boss2 = await session.get(User, referrer_id)
            self.assertEqual(boss2.referral_balance, 80)

    async def test_landing_webhook_form_submitted_deactivates_funnel(self):
        import landing_webhook
        bot = MockBot()
        user_id = 2016
        admin_id = 9992

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="Lead", username="lead_user"))
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            session.add(FunnelState(user_id=user_id, stage="strategy_offer", is_active=True))
            session.add(FunnelLanding(user_id=user_id, token="tok_lead", status="opened"))
            await session.commit()

        payload = {
            "event": "form_submitted",
            "user_id": user_id,
            "landing_token": "tok_lead",
            "name": "Lead User",
            "phone": "TEST_PHONE,
            "email": "test@example.invalid"
        }
        with patch("landing_webhook.get_all_admin_ids", new=AsyncMock(return_value=[admin_id])):
            await landing_webhook.handle_incoming_webhook(payload, bot)

        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertFalse(st.is_active)
            self.assertIsNone(st.next_followup_at)

        payload_paid = {
            "event": "paid",
            "user_id": user_id,
            "landing_token": "tok_lead",
            "amount": "5000"
        }
        with patch("landing_webhook.get_all_admin_ids", new=AsyncMock(return_value=[admin_id])):
            await landing_webhook.handle_incoming_webhook(payload_paid, bot)

        async with async_session_maker() as session:
            st2 = await session.get(FunnelState, user_id)
            self.assertFalse(st2.is_active)

    async def test_landing_webhook_notification_deduplication(self):
        import landing_webhook
        bot = MockBot()
        user_id = 2017
        admin_id = 9993

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="DedupUser", username="dedup_user"))
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            session.add(FunnelLanding(user_id=user_id, token="tok_dedup", status="opened"))
            await session.commit()

        payload_form = {
            "event": "form_filled",
            "user_id": user_id,
            "landing_token": "tok_dedup",
            "name": "Dedup User",
            "phone": "TEST_PHONE,
            "email": "test@example.invalid"
        }

        with patch("landing_webhook.get_all_admin_ids", new=AsyncMock(return_value=[admin_id])):
            await landing_webhook.handle_incoming_webhook(payload_form, bot)
            self.assertEqual(len(bot.sent_messages), 2)

            await landing_webhook.handle_incoming_webhook(payload_form, bot)
            self.assertEqual(len(bot.sent_messages), 2)

            payload_paid = {
                "event": "paid",
                "user_id": user_id,
                "landing_token": "tok_dedup",
                "amount": "10000"
            }
            await landing_webhook.handle_incoming_webhook(payload_paid, bot)
            self.assertEqual(len(bot.sent_messages), 4)

            await landing_webhook.handle_incoming_webhook(payload_paid, bot)
            self.assertEqual(len(bot.sent_messages), 4)

    async def test_landing_webhook_admin_notification_includes_username_and_id(self):
        import landing_webhook
        bot = MockBot()
        user_id = 2018
        admin_id = 9994

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="CoolUser", username="cool_user_tg"))
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            await session.commit()

        payload = {
            "event": "form_submitted",
            "user_id": user_id,
            "name": "Cool User",
            "phone": "TEST_PHONE,
            "email": "test@example.invalid",
            "landing_token": "tok_cool"
        }
        with patch("landing_webhook.get_all_admin_ids", new=AsyncMock(return_value=[admin_id])):
            await landing_webhook.handle_incoming_webhook(payload, bot)

        admin_msg = next((m for m in bot.sent_messages if getattr(m.chat, "id", None) == admin_id), None)
        self.assertIsNotNone(admin_msg)
        text = admin_msg.text
        self.assertIn("@cool_user_tg", text)
        self.assertIn(str(user_id), text)
        self.assertIn("Cool User", text)
        self.assertIn("TEST_PHONE, text)
        self.assertIn("test@example.invalid", text)
        self.assertIn("tok_cool", text)

    async def test_canonical_start_welcome_and_keyboard_removal(self):
        bot = MockBot()
        user_id = 2019

        async def _fake_send_1(text, **kw):
            return await bot.send_message(user_id, text, **kw)

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="BrandNew", username="brand_new"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
            answer=_fake_send_1
        )
        command = SimpleNamespace(args=None)
        state = AsyncMock()

        # Step 1: /start for brand-new user
        await handlers.cmd_start(msg, state, command)

        # Assert welcome_msg was sent with ReplyKeyboardRemove
        self.assertGreaterEqual(len(bot.sent_messages), 1)
        welcome_msg = bot.sent_messages[0]
        from aiogram.types import ReplyKeyboardRemove
        self.assertIsInstance(welcome_msg.reply_markup, ReplyKeyboardRemove)

        # Assert FSM state transitioned to waiting_name (asking profile)
        state.set_state.assert_called_with(funnel.ProfileStates.waiting_name)

        # Step 2: Test /start funnel deeplink for brand-new user -> does not bypass onboarding
        user_id_funnel = 2020
        async def _fake_send_2(text, **kw):
            return await bot.send_message(user_id_funnel, text, **kw)

        msg_funnel = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id_funnel, first_name="FunnelNew", username="funnel_new"),
            chat=SimpleNamespace(id=user_id_funnel),
            bot=bot,
            answer=_fake_send_2
        )
        command_funnel = SimpleNamespace(args="funnel")
        state_funnel = AsyncMock()

        await handlers.cmd_start(msg_funnel, state_funnel, command_funnel)
        state_funnel.set_state.assert_called_with(funnel.ProfileStates.waiting_name)
        state_funnel.update_data.assert_called_with(post_start_action="funnel_intro")

    async def test_admin_welcome_editor_and_menu_rename(self):
        self.assertIn("welcome_msg", handlers.CATEGORIES["main"])
        self.assertIn("start_msg", handlers.CATEGORIES["main"])
        self.assertEqual(handlers.HUMAN_NAMES["welcome_msg"], "Приветственное сообщение")
        self.assertEqual(handlers.HUMAN_NAMES["start_msg"], "Текст главного меню")

    async def test_admin_content_editor_shows_current_persisted_content(self):
        bot = MockBot()
        async with async_session_maker() as session:
            admin = await session.get(User, 1)
            if not admin:
                session.add(User(id=1, first_name="Admin", is_admin=True))
            else:
                admin.is_admin = True
            c = await session.get(Content, "btn_club_materials")
            if not c:
                session.add(Content(key="btn_club_materials", button_text="Мои материалы", link_url="https://materials.com", text_content="Текст материалов"))
            else:
                c.button_text = "Мои материалы"
                c.link_url = "https://materials.com"
                c.text_content = "Текст материалов"
            await session.commit()

        cb = SimpleNamespace(
            data="edit_cont_main:btn_club_materials",
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        state = AsyncMock()
        await handlers.edit_content_start(cb, state)

        self.assertGreaterEqual(len(bot.sent_messages), 1)
        sent = bot.sent_messages[-1].text
        self.assertIn("Мои материалы", sent)
        self.assertIn("https://materials.com", sent)

    async def test_admin_funnel_content_editor_shows_current_content(self):
        bot = MockBot()
        async with async_session_maker() as session:
            admin = await session.get(User, 1)
            if not admin:
                session.add(User(id=1, first_name="Admin", is_admin=True))
            else:
                admin.is_admin = True
            c = await session.get(Content, "funnel_intro")
            if not c:
                session.add(Content(key="funnel_intro", text_content="Текущий текст интро"))
            else:
                c.text_content = "Текущий текст интро"
            await session.commit()

        cb = SimpleNamespace(
            data="admin_fcont_funnel_intro",
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=1),
                message_id=1,
                delete=AsyncMock(),
                edit_text=AsyncMock(side_effect=lambda text, **kw: bot.send_message(1, text, **kw)),
                content_type="text"
            ),
            bot=bot,
            answer=AsyncMock()
        )
        state = AsyncMock()
        await funnel.admin_funnel_content_edit(cb, state)

        self.assertGreaterEqual(len(bot.sent_messages), 1)
        sent = bot.sent_messages[-1].text
        self.assertIn("Текущий текст интро", sent)

    async def test_funnel_reflection_label_exact_text(self):
        label = funnel.FUNNEL_CONTENT_ITEMS.get("funnel_reflection_prompt")
        self.assertIsNotNone(label)
        self.assertEqual(label, "Текст после практики (запрос ощущений/инсайтов)")

    async def test_admin_meditation_editor_shows_current_value_and_refreshes_detail(self):
        bot = MockBot()
        med_id = 99
        async with async_session_maker() as session:
            admin = await session.get(User, 1)
            if not admin:
                session.add(User(id=1, first_name="Admin", is_admin=True))
            else:
                admin.is_admin = True
            session.add(FunnelMeditation(id=med_id, title="Старая медитация", duration_minutes=15, is_active=True))
            await session.commit()

        async def _fake_send_admin(text, **kw):
            return await bot.send_message(1, text, **kw)

        cb = SimpleNamespace(
            data=f"admin_fmededit_title_{med_id}_0",
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=1),
                message_id=1,
                delete=AsyncMock(),
                answer=_fake_send_admin
            ),
            bot=bot,
            answer=AsyncMock()
        )
        state = AsyncMock()
        await funnel.admin_fmed_edit(cb, state)
        self.assertGreaterEqual(len(bot.sent_messages), 1)
        self.assertIn("Старая медитация", bot.sent_messages[-1].text)

        msg = SimpleNamespace(
            text="Новая медитация",
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            bot=bot,
            answer=AsyncMock()
        )
        state.get_data = AsyncMock(return_value={"edit_med_id": med_id, "edit_med_field": "title", "edit_med_page": 0})
        with patch("funnel.show_meditation_admin_detail", new=AsyncMock()) as mock_detail:
            await funnel.admin_fmed_edit_text_save(msg, state)
            mock_detail.assert_awaited_once_with(msg, med_id, 0)

        async with async_session_maker() as session:
            med = await session.get(FunnelMeditation, med_id)
            self.assertEqual(med.title, "Новая медитация")

    async def test_no_runtime_calls_to_quick_nav_keyboard(self):
        import inspect, ast
        funnel_src = inspect.getsource(funnel)
        handlers_src = inspect.getsource(handlers)
        self.assertNotIn("reply_markup=quick_nav_keyboard()", funnel_src)
        self.assertNotIn("reply_markup=quick_nav_keyboard()", handlers_src)
        tree = ast.parse(funnel_src)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "quick_nav_keyboard"]
        self.assertEqual(len(calls), 0)

    async def test_ai_disclaimer_content_key_canonical_and_editable(self):
        bot = MockBot()
        user_id = 2025
        custom_disclaimer = "⚠️ Персональный дисклеймер: общение с ИИ ведется в ознакомительных целях."

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="AITest", ai_disclaimer_shown=False))
            disc = await session.get(Content, "disclaimer_text")
            if not disc:
                session.add(Content(key="disclaimer_text", text_content=custom_disclaimer))
            else:
                disc.text_content = custom_disclaimer
            await session.commit()

        async with async_session_maker() as session:
            sent = await handlers.ensure_ai_disclaimer_sent(session, bot, user_id, user_id)
            self.assertTrue(sent)

        disclaimer_msgs = [m for m in bot.sent_messages if getattr(m.chat, "id", None) == user_id]
        self.assertEqual(len(disclaimer_msgs), 1)
        self.assertEqual(disclaimer_msgs[0].text, custom_disclaimer)

    async def test_ai_disclaimer_send_failure_retries_cas(self):
        bot = MockBot()
        user_id = 2026
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="FailUser", ai_disclaimer_shown=False))
            await session.commit()

        with patch.object(bot, "send_message", side_effect=Exception("Telegram network error")):
            async with async_session_maker() as session:
                sent = await handlers.ensure_ai_disclaimer_sent(session, bot, user_id, user_id)
                self.assertFalse(sent)

        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertFalse(u.ai_disclaimer_shown)

        async with async_session_maker() as session:
            sent2 = await handlers.ensure_ai_disclaimer_sent(session, bot, user_id, user_id)
            self.assertTrue(sent2)

        async with async_session_maker() as session:
            u2 = await session.get(User, user_id)
            self.assertTrue(u2.ai_disclaimer_shown)

    async def test_referral_registration_validation_matrix(self):
        bot = MockBot()
        ref_boss_id = 20280
        async with async_session_maker() as session:
            session.add(User(id=ref_boss_id, first_name="Boss", referral_balance=0))
            sub_c = await session.get(SubscriptionConfig, 1)
            sub_c.referral_enabled = True
            ref_c = await session.get(ReferralConfig, 1)
            ref_c.points_per_invite = 50
            await session.commit()

        # 1. Non-existent referrer -> no referrer_id assigned, no reward
        msg_nonexistent = SimpleNamespace(
            from_user=SimpleNamespace(id=20281, username="u1", first_name="User1"),
            chat=SimpleNamespace(id=20281),
            bot=bot,
            answer=AsyncMock()
        )
        await handlers.cmd_start(msg_nonexistent, AsyncMock(), SimpleNamespace(args="99999999"))
        async with async_session_maker() as session:
            u1 = await session.get(User, 20281)
            self.assertIsNone(u1.referrer_id)

        # 2. Self-referral -> no referrer_id, no reward
        msg_self = SimpleNamespace(
            from_user=SimpleNamespace(id=ref_boss_id, username="boss", first_name="Boss"),
            chat=SimpleNamespace(id=ref_boss_id),
            bot=bot,
            answer=AsyncMock()
        )
        await handlers.cmd_start(msg_self, AsyncMock(), SimpleNamespace(args=str(ref_boss_id)))
        async with async_session_maker() as session:
            boss = await session.get(User, ref_boss_id)
            self.assertIsNone(boss.referrer_id)
            self.assertEqual(boss.referral_balance, 0)

        # 3. New valid referral -> assigned and rewarded
        msg_valid = SimpleNamespace(
            from_user=SimpleNamespace(id=20282, username="u2", first_name="User2"),
            chat=SimpleNamespace(id=20282),
            bot=bot,
            answer=AsyncMock()
        )
        await handlers.cmd_start(msg_valid, AsyncMock(), SimpleNamespace(args=str(ref_boss_id)))
        async with async_session_maker() as session:
            u2 = await session.get(User, 20282)
            self.assertEqual(u2.referrer_id, ref_boss_id)
            boss = await session.get(User, ref_boss_id)
            self.assertEqual(boss.referral_balance, 50)

        # 4. Repeated /start for same user -> no duplicate reward
        await handlers.cmd_start(msg_valid, AsyncMock(), SimpleNamespace(args=str(ref_boss_id)))
        async with async_session_maker() as session:
            boss = await session.get(User, ref_boss_id)
            self.assertEqual(boss.referral_balance, 50)

        # 5. Existing user with referrer -> cannot be overwritten
        other_boss_id = 20289
        async with async_session_maker() as session:
            session.add(User(id=other_boss_id, first_name="OtherBoss"))
            await session.commit()
        await handlers.cmd_start(msg_valid, AsyncMock(), SimpleNamespace(args=str(other_boss_id)))
        async with async_session_maker() as session:
            u2_check = await session.get(User, 20282)
            self.assertEqual(u2_check.referrer_id, ref_boss_id)

    async def test_refusal_channel_exact_migrated_url(self):
        async with async_session_maker() as session:
            c = await session.get(Content, "btn_vitaly_channel")
            self.assertIsNotNone(c)
            self.assertEqual(c.link_url, "https://t.me/lifeuspeh")

        markup = await funnel.build_no_strategy_keyboard()
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        channel_btn = buttons[0]
        self.assertEqual(channel_btn.url, "https://t.me/lifeuspeh")

    async def test_meditation_inactivity_timeouts(self):
        bot = MockBot()
        user_id = 2030
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TimeoutUser"))
            session.add(FunnelMeditation(id=30, title="Медитация таймаута", duration_minutes=10, is_active=True, file_type="audio", file_id="a30"))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TimeoutUser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        await funnel.show_meditation_card(cb, meditation_id=30, page=0, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "meditation_card")
            self.assertIsNotNone(st.stage_timeout_at)

        await funnel.send_meditation(cb, meditation_id=30, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "listening")
            self.assertIsNotNone(st.stage_timeout_at)

        state = AsyncMock()
        await funnel.request_reflection(cb, state=state, bot=bot)
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "waiting_reflection")
            self.assertIsNone(st.stage_timeout_at)

    async def test_reserve_url_removal(self):
        markup = funnel.meditation_listen_keyboard()
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        for btn in buttons:
            self.assertIsNone(btn.url)

    async def test_admin_referral_back_clears_fsm(self):
        bot = MockBot()
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=1, delete=AsyncMock()),
            bot=bot,
            answer=AsyncMock()
        )
        state = AsyncMock()
        await handlers.admin_referral_settings(cb, state=state)
        state.clear.assert_awaited_once()

    # =========================================================================
    # Second Review Defect Regression Tests
    # =========================================================================

    async def test_atomic_payment_settlement_concurrency(self):
        bot = MockBot()
        referrer_id = 3001
        user_id = 3002
        async with async_session_maker() as session:
            session.add(User(id=referrer_id, first_name="Referrer", referral_balance=0))
            session.add(User(id=user_id, first_name="Buyer", referral_balance=300, referrer_id=referrer_id))
            session.add(SubscriptionPlan(id=30, name="Plan 1000", price=1000.0, duration_value=1, duration_unit="months", is_active=True))
            sub_c = await session.get(SubscriptionConfig, 1)
            sub_c.referral_enabled = True
            sub_c.referral_reward_mode = "points"
            sub_c.referral_first_pay_percent = 10
            sub_c.notifications_enabled = True
            session.add(User(id=999001, first_name="Admin", is_admin=True))
            await session.commit()

        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(session, user_id=user_id, plan_id=30, provider="prodamus")
            self.assertEqual(intent.points_reserved, 300)
            self.assertEqual(intent.final_amount, 700.0)
            token = intent.token
            intent_id = intent.id
            await session.commit()

        barrier = asyncio.Barrier(2)
        async def settle_worker(idx):
            async with async_session_maker() as session:
                it = await session.get(PaymentIntent, intent_id)
                await barrier.wait()
                await handlers.settle_payment_intent(session, bot, it, payment_charge_id=f"charge_{idx}")
                await session.commit()

        await asyncio.gather(settle_worker(1), settle_worker(2))

        async with async_session_maker() as session:
            txs = (await session.execute(select(Transaction).where(Transaction.user_id == user_id))).scalars().all()
            self.assertEqual(len(txs), 1)

            u = await session.get(User, user_id)
            self.assertEqual(u.referral_balance, 0)

            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            self.assertIsNotNone(sub)
            now = msk_now()
            self.assertLess((sub.end_date - now).days, 35)

            ref = await session.get(User, referrer_id)
            self.assertEqual(ref.referral_balance, 70)
            logs = (await session.execute(select(ReferralPaymentLog).where(ReferralPaymentLog.referred_user_id == user_id))).scalars().all()
            self.assertEqual(len(logs), 1)

            user_notifs = [m for m in bot.sent_messages if getattr(m, "chat", None) and m.chat.id == user_id]
            admin_notifs = [m for m in bot.sent_messages if getattr(m, "chat", None) and m.chat.id == 999001]
            self.assertEqual(len(user_notifs), 1)
            self.assertEqual(len(admin_notifs), 1)

    async def test_prodamus_amount_mismatch_rejected(self):
        import prodamus_webhooks
        bot = MockBot()
        user_id = 4001
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="User4001"))
            session.add(SubscriptionPlan(id=40, name="Plan 800", price=800.0, duration_value=1, duration_unit="months", is_active=True))
            await session.commit()

        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(session, user_id=user_id, plan_id=40, provider="prodamus")
            token = intent.token
            await session.commit()

        payload = {
            "order_id": f"pi_{token}",
            "sum": "1.00",
            "payment_status": "success",
        }
        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            resp = await prodamus_webhooks.process_prodamus_payment(payload, bot=bot)
            self.assertEqual(resp.status, 200)

        async with async_session_maker() as session:
            it = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(it.status, "pending")
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            self.assertIsNone(sub)

    async def test_telegram_amount_and_currency_mismatch_rejected(self):
        bot = MockBot()
        user_id = 4002
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="User4002"))
            session.add(SubscriptionPlan(id=41, name="Plan 800", price=800.0, duration_value=1, duration_unit="months", is_active=True))
            await session.commit()

        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(session, user_id=user_id, plan_id=41, provider="telegram_pay")
            token = intent.token
            await session.commit()

        pq_mismatch = SimpleNamespace(
            id="pq_1",
            invoice_payload=f"pi_{token}",
            total_amount=100,
            currency="RUB",
            from_user=SimpleNamespace(id=user_id),
        )
        await handlers.process_pre_checkout(pq_mismatch, bot=bot)
        self.assertEqual(bot.answered_pre_checkouts[-1], ("pq_1", False, "Неверная сумма счета."))

        pq_curr = SimpleNamespace(
            id="pq_2",
            invoice_payload=f"pi_{token}",
            total_amount=80000,
            currency="USD",
            from_user=SimpleNamespace(id=user_id),
        )
        await handlers.process_pre_checkout(pq_curr, bot=bot)
        self.assertEqual(bot.answered_pre_checkouts[-1], ("pq_2", False, "Неверная валюта счета."))

        pq_user = SimpleNamespace(
            id="pq_3",
            invoice_payload=f"pi_{token}",
            total_amount=80000,
            currency="RUB",
            from_user=SimpleNamespace(id=999999),
        )
        await handlers.process_pre_checkout(pq_user, bot=bot)
        self.assertEqual(bot.answered_pre_checkouts[-1], ("pq_3", False, "Счет выставлен другому пользователю."))

        sp_mismatch = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            successful_payment=SimpleNamespace(
                invoice_payload=f"pi_{token}",
                total_amount=100,
                currency="RUB",
                provider_payment_charge_id="tg_charge_1",
            )
        )
        await handlers.process_successful_payment(sp_mismatch, bot=bot)
        async with async_session_maker() as session:
            it = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(it.status, "pending")
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            self.assertIsNone(sub)

    async def test_prodamus_failed_payment_releases_points(self):
        import prodamus_webhooks
        bot = MockBot()
        user_id = 5001
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="User5001", referral_balance=1000))
            session.add(SubscriptionPlan(id=50, name="Plan 800", price=800.0, duration_value=1, duration_unit="months", is_active=True))
            await session.commit()

        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(session, user_id=user_id, plan_id=50, provider="prodamus")
            self.assertEqual(intent.points_reserved, 800)
            token = intent.token
            await session.commit()

        async with async_session_maker() as session:
            reserved = await handlers.get_active_reserved_points(session, user_id)
            self.assertEqual(reserved, 800)

        payload = {
            "order_id": f"pi_{token}",
            "payment_status": "failed",
        }
        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            resp = await prodamus_webhooks.process_prodamus_payment(payload, bot=bot)
            self.assertEqual(resp.status, 200)

        async with async_session_maker() as session:
            it = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(it.status, "cancelled")
            reserved = await handlers.get_active_reserved_points(session, user_id)
            self.assertEqual(reserved, 0)
            u = await session.get(User, user_id)
            self.assertEqual(u.referral_balance, 1000)

        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            resp2 = await prodamus_webhooks.process_prodamus_payment(payload, bot=bot)
            self.assertEqual(resp2.status, 200)

        async with async_session_maker() as session:
            intent2 = await handlers.create_payment_intent(session, user_id=user_id, plan_id=50, provider="prodamus")
            self.assertEqual(intent2.points_reserved, 800)
            self.assertEqual(intent2.final_amount, 0.0)

    async def test_prodamus_subscription_metadata_persistence_and_recurring_resolution(self):
        import prodamus_webhooks
        bot = MockBot()
        user_id = 6001
        subscription_id = 987654
        profile_id = 112233

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="User6001"))
            session.add(SubscriptionPlan(id=60, name="Plan Auto", price=1000.0, duration_value=1, duration_unit="months", allow_auto_renewal=True, is_active=True))
            await session.commit()

        async with async_session_maker() as session:
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            self.assertIsNone(sub)

        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(session, user_id=user_id, plan_id=60, provider="prodamus")
            token = intent.token
            await session.commit()

        first_payload = {
            "order_id": f"pi_{token}",
            "sum": "1000.00",
            "payment_status": "success",
            "subscription": {
                "id": subscription_id,
                "profile": profile_id,
                "action_code": "payment",
            }
        }
        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            resp = await prodamus_webhooks.process_prodamus_payment(first_payload, bot=bot)
            self.assertEqual(resp.status, 200)

        async with async_session_maker() as session:
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            self.assertIsNotNone(sub)
            self.assertEqual(sub.prodamus_subscription_id, subscription_id)
            self.assertEqual(sub.prodamus_profile_id, profile_id)
            self.assertTrue(sub.auto_renewal)
            old_end = sub.end_date

        recurring_payload = {
            "order_id": f"rec_{secrets.token_hex(8)}",
            "sum": "1000.00",
            "payment_status": "success",
            "subscription": {
                "id": subscription_id,
                "action_code": "auto_payment",
                "profile": profile_id,
            }
        }
        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            resp2 = await prodamus_webhooks.process_prodamus_payment(recurring_payload, bot=bot)
            self.assertEqual(resp2.status, 200)

        async with async_session_maker() as session:
            sub2 = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            self.assertGreater(sub2.end_date, old_end)

    async def test_prodamus_recurring_payment_no_runtime_error(self):
        import prodamus_webhooks
        bot = MockBot()
        user_id = 7001
        subscription_id = 556677

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="User7001"))
            session.add(SubscriptionPlan(id=70, name="Plan Recurring", price=500.0, duration_value=1, duration_unit="months", allow_auto_renewal=True, is_active=True))
            session.add(UserSubscription(
                user_id=user_id,
                plan_id=70,
                start_date=msk_now(),
                end_date=msk_now() + timedelta(days=1),
                prodamus_subscription_id=subscription_id,
                auto_renewal=True,
                payment_provider="Prodamus",
            ))
            sub_c = await session.get(SubscriptionConfig, 1)
            sub_c.notifications_enabled = True
            session.add(User(id=999002, first_name="Admin2", is_admin=True))
            await session.commit()

        payload = {
            "sum": "500.00",
            "payment_status": "success",
            "payment_id": "rec_pmt_7001",
            "subscription": {
                "id": subscription_id,
                "action_code": "auto_payment",
            }
        }
        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            resp = await prodamus_webhooks.process_prodamus_payment(payload, bot=bot)
            self.assertEqual(resp.status, 200)

        user_msgs = [m for m in bot.sent_messages if getattr(m, "chat", None) and m.chat.id == user_id]
        admin_msgs = [m for m in bot.sent_messages if getattr(m, "chat", None) and m.chat.id == 999002]
        self.assertGreaterEqual(len(user_msgs), 1)
        self.assertGreaterEqual(len(admin_msgs), 1)

    async def test_payment_provider_mapping_in_settlement(self):
        bot = MockBot()
        admin_id = 999003
        async with async_session_maker() as session:
            session.add(User(id=8001, first_name="UserTG"))
            session.add(User(id=8002, first_name="UserProd"))
            session.add(User(id=8003, first_name="UserPts"))
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            session.add(SubscriptionPlan(id=80, name="Plan 80", price=500.0, duration_value=1, duration_unit="months", is_active=True))
            sub_c = await session.get(SubscriptionConfig, 1)
            sub_c.notifications_enabled = True
            await session.commit()

        # 1. Telegram
        async with async_session_maker() as session:
            intent_tg = await handlers.create_payment_intent(session, user_id=8001, plan_id=80, provider="telegram_pay")
            await handlers.settle_payment_intent(session, bot, intent_tg)
            await session.commit()
            sub_tg = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == 8001))
            self.assertEqual(sub_tg.payment_provider, "TelegramPay")
            tg_admin_msg = next(m for m in reversed(bot.sent_messages) if m.chat.id == admin_id)
            self.assertIn("🔔 Новый платеж (TelegramPay)!", tg_admin_msg.text)

        # 2. Prodamus
        async with async_session_maker() as session:
            intent_prod = await handlers.create_payment_intent(session, user_id=8002, plan_id=80, provider="prodamus")
            await handlers.settle_payment_intent(session, bot, intent_prod)
            await session.commit()
            sub_prod = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == 8002))
            self.assertEqual(sub_prod.payment_provider, "Prodamus")
            prod_admin_msg = next(m for m in reversed(bot.sent_messages) if m.chat.id == admin_id)
            self.assertIn("🔔 Новый платеж (Prodamus)!", prod_admin_msg.text)

        # 3. Points (zero cash)
        async with async_session_maker() as session:
            u3 = await session.get(User, 8003)
            u3.referral_balance = 500
            await session.commit()
            intent_pts = await handlers.create_payment_intent(session, user_id=8003, plan_id=80, provider="telegram_pay")
            self.assertEqual(intent_pts.provider, "zero_cash")
            await handlers.settle_payment_intent(session, bot, intent_pts)
            await session.commit()
            sub_pts = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == 8003))
            self.assertEqual(sub_pts.payment_provider, "Points")
            pts_admin_msg = next(m for m in reversed(bot.sent_messages) if m.chat.id == admin_id)
            self.assertIn("🔔 Новый платеж (Points)!", pts_admin_msg.text)

    async def test_checkout_persisted_price_snapshot_survives_mutation(self):
        bot = MockBot()
        user_id = 9001
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="SnapshotUser", referral_balance=250))
            session.add(SubscriptionPlan(id=90, name="Plan 1000", price=1000.0, duration_value=1, duration_unit="months", is_active=True))
            session.add(PaymentProvider(id=1, name="Pay Provider", provider_token="test_token", is_active=True))
            sub_c = await session.get(SubscriptionConfig, 1)
            sub_c.prodamus_url = "https://demo.prodamus.ru"
            sub_c.prodamus_secret_key = "secret"
            await session.commit()

        cb = SimpleNamespace(
            data="select_plan_90",
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, edit_text=AsyncMock()),
            bot=bot,
            answer=AsyncMock(),
        )
        await handlers.process_plan_selection(cb)

        async with async_session_maker() as session:
            intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.user_id == user_id, PaymentIntent.status == "pending"))
            self.assertIsNotNone(intent)
            self.assertEqual(intent.final_amount, 750.0)
            self.assertEqual(intent.points_reserved, 250)
            token = intent.token

            u = await session.get(User, user_id)
            u.referral_balance = 0
            await session.commit()

        cb_tg = SimpleNamespace(
            data=f"pay_prov_sub_1_{token}",
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=2),
            bot=bot,
            answer=AsyncMock(),
        )
        state = AsyncMock()
        await handlers.process_payment_invoice(cb_tg, state=state, bot=bot)

        invoice = bot.sent_invoices[-1]
        self.assertEqual(invoice.prices[0].amount, 75000)
        self.assertEqual(invoice.payload, f"pi_{token}")

    async def test_profile_gender_handler_destination_identity_customer_not_bot(self):
        bot = MockBot()
        customer_id = 9501
        bot_id = 999999
        async with async_session_maker() as session:
            session.add(User(id=customer_id, first_name="RealCustomer"))
            await session.commit()

        state = AsyncMock()
        state.get_data.return_value = {"post_start_action": "funnel_intro"}

        cb = SimpleNamespace(
            data="gender_male",
            from_user=SimpleNamespace(id=customer_id, first_name="RealCustomer"),
            message=SimpleNamespace(
                from_user=SimpleNamespace(id=bot_id, first_name="Bot"),
                chat=SimpleNamespace(id=customer_id),
                message_id=10,
                delete=AsyncMock(),
                content_type="text",
                bot=bot,
            ),
            bot=bot,
            answer=AsyncMock(),
        )

        await funnel.profile_gender_handler(cb, state)

        async with async_session_maker() as session:
            customer_st = await session.get(FunnelState, customer_id)
            self.assertIsNotNone(customer_st)
            self.assertEqual(customer_st.user_id, customer_id)
            self.assertEqual(customer_st.stage, "intro")

            bot_st = await session.get(FunnelState, bot_id)
            self.assertIsNone(bot_st)

    async def test_landing_webhook_concurrency_dedupe_and_failure_retry(self):
        import landing_webhook
        bot = MockBot()
        customer_id = 9601
        admin_id = 999004
        async with async_session_maker() as session:
            session.add(User(id=customer_id, first_name="LandUser"))
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            await session.commit()

        barrier = asyncio.Barrier(2)
        async def form_worker(idx):
            await barrier.wait()
            payload = {
                "token": "land_tok_1",
                "user_id": customer_id,
                "status": "form_submitted",
                "name": "Applicant",
            }
            return await landing_webhook.handle_incoming_webhook(payload, bot=bot)

        res = await asyncio.gather(form_worker(1), form_worker(2))
        self.assertEqual(len(res), 2)

        user_msgs = [m for m in bot.sent_messages if getattr(m, "chat", None) and m.chat.id == customer_id and "анкета" in m.text.lower()]
        admin_msgs = [m for m in bot.sent_messages if getattr(m, "chat", None) and m.chat.id == admin_id and "анкета" in m.text.lower()]
        self.assertEqual(len(user_msgs), 1)
        self.assertEqual(len(admin_msgs), 1)

        async with async_session_maker() as session:
            land = await session.scalar(select(FunnelLanding).where(FunnelLanding.token == "land_tok_1"))
            self.assertTrue(land.form_notified)

        # Failure test: if delivery fails, paid_notified remains False
        failing_bot = MockBot()
        failing_bot.send_message = AsyncMock(side_effect=RuntimeError("Telegram network failure"))
        paid_payload = {
            "token": "land_tok_2",
            "user_id": customer_id,
            "status": "paid",
            "amount": 5000,
        }
        await landing_webhook.handle_incoming_webhook(paid_payload, bot=failing_bot)
        async with async_session_maker() as session:
            land2 = await session.scalar(select(FunnelLanding).where(FunnelLanding.token == "land_tok_2"))
            self.assertFalse(land2.paid_notified)

        working_bot = MockBot()
        await landing_webhook.handle_incoming_webhook(paid_payload, bot=working_bot)
        async with async_session_maker() as session:
            land2_after = await session.scalar(select(FunnelLanding).where(FunnelLanding.token == "land_tok_2"))
            self.assertTrue(land2_after.paid_notified)

    async def test_payment_snapshot_completeness_and_promo_resilience(self):
        user_id = 9701
        promo_code = "PROMO25"
        async with async_session_maker() as session:
            user = User(id=user_id, first_name="PromoUser", referral_balance=100)
            session.add(user)
            plan = SubscriptionPlan(id=97, name="Plan 1000", price=1000.0, duration_value=1, duration_unit="months", is_active=True)
            session.add(plan)
            promo = PromoCode(code=promo_code, discount_percent=20, applies_to_all_plans=True, is_active=True)
            session.add(promo)
            user.promo_codes.append(promo)
            await session.commit()
            promo_id = promo.id

        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(session, user_id=user_id, plan_id=97, provider="telegram_pay")
            self.assertEqual(intent.promo_code_id, promo_id)
            self.assertEqual(intent.gross_amount, 1000.0)
            self.assertEqual(intent.promo_discount_rub, 200.0)
            self.assertEqual(intent.points_reserved, 100)
            self.assertEqual(intent.final_amount, 700.0)
            token = intent.token
            await session.commit()

        async with async_session_maker() as session:
            pr = await session.get(PromoCode, promo_id)
            pr.discount_percent = 99
            pr.is_active = False
            await session.commit()

        async with async_session_maker() as session:
            saved_intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(saved_intent.promo_code_id, promo_id)
            self.assertEqual(saved_intent.gross_amount, 1000.0)
            self.assertEqual(saved_intent.promo_discount_rub, 200.0)
            self.assertEqual(saved_intent.points_reserved, 100)
            self.assertEqual(saved_intent.final_amount, 700.0)

        # Real PromoCode deletion resilience test
        async with async_session_maker() as session:
            pr = await session.get(PromoCode, promo_id)
            await session.delete(pr)
            await session.commit()

        async with async_session_maker() as session:
            saved_intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(saved_intent.gross_amount, 1000.0)
            self.assertEqual(saved_intent.promo_discount_rub, 200.0)
            self.assertEqual(saved_intent.points_reserved, 100)
            self.assertEqual(saved_intent.final_amount, 700.0)

    async def test_handle_prodamus_webhook_production_http_path(self):
        import prodamus_webhooks
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        bot = MockBot()
        secret = "test_prodamus_secret_123"
        user_id = 9801
        plan_id = 98

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="HttpUser", referral_balance=500))
            session.add(SubscriptionPlan(id=plan_id, name="HttpPlan", price=1000.0, duration_value=1, duration_unit="months", is_active=True))
            cfg = await session.get(SubscriptionConfig, 1)
            cfg.prodamus_secret_key = secret
            cfg.prodamus_url = "https://demo.prodamus.ru"
            await session.commit()

        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(session, user_id=user_id, plan_id=plan_id, provider="prodamus")
            token = intent.token
            await session.commit()

        app = web.Application()
        app["bot"] = bot
        app.router.add_post("/webhook/prodamus", prodamus_webhooks.handle_prodamus_webhook)
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            # 1. Invalid signature
            bad_resp = await client.post("/webhook/prodamus", json={"order_id": f"pi_{token}"}, headers={"Sign": "invalidsig"})
            self.assertEqual(bad_resp.status, 400)

            # 2. Amount mismatch with valid signature
            mismatch_data = {
                "order_id": f"pi_{token}",
                "sum": "1.00",
                "payment_status": "success",
            }
            sig_mismatch = prodamus_webhooks.create_prodamus_signature(mismatch_data, secret)
            resp_mismatch = await client.post("/webhook/prodamus", json=mismatch_data, headers={"Sign": sig_mismatch})
            self.assertEqual(resp_mismatch.status, 200)
            async with async_session_maker() as session:
                it = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
                self.assertEqual(it.status, "pending")

            # 3. Successful PI with valid signature
            success_data = {
                "order_id": f"pi_{token}",
                "sum": "500.00",
                "payment_status": "success",
            }
            sig_success = prodamus_webhooks.create_prodamus_signature(success_data, secret)
            resp_success = await client.post("/webhook/prodamus", json=success_data, headers={"Sign": sig_success})
            self.assertEqual(resp_success.status, 200)
            async with async_session_maker() as session:
                it = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
                self.assertEqual(it.status, "paid")
                sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
                self.assertIsNotNone(sub)

            # 4. Duplicate webhook
            resp_dup = await client.post("/webhook/prodamus", json=success_data, headers={"Sign": sig_success})
            self.assertEqual(resp_dup.status, 200)

            # 5. Failed payment release via HTTP
            async with async_session_maker() as session:
                intent_fail = await handlers.create_payment_intent(session, user_id=user_id, plan_id=plan_id, provider="prodamus")
                fail_token = intent_fail.token
                await session.commit()
            fail_data = {
                "order_id": f"pi_{fail_token}",
                "payment_status": "failed",
            }
            sig_fail = prodamus_webhooks.create_prodamus_signature(fail_data, secret)
            resp_fail = await client.post("/webhook/prodamus", json=fail_data, headers={"Sign": sig_fail})
            self.assertEqual(resp_fail.status, 200)
            async with async_session_maker() as session:
                it_fail = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == fail_token))
                self.assertEqual(it_fail.status, "cancelled")
                res_pts = await handlers.get_active_reserved_points(session, user_id)
                self.assertEqual(res_pts, 0)
        finally:
            await client.close()

    # =========================================================================
    # Explicit Citations from Walkthrough Traceability
    # =========================================================================

    async def test_objection_generation_ux_typing_and_wait_text(self):
        await self.test_objection_generation_ux_typing_action_and_wait_text()

    async def test_refusal_declined_allows_nurture_when_enabled(self):
        await self.test_refusal_keeps_nurture_campaign_when_enabled()

    async def test_automatic_referral_points_reservation_in_checkout(self):
        await self.test_automatic_checkout_referral_points_pricing_snapshot()

    async def test_start_funnel_opens_funnel_not_main_menu(self):
        bot = MockBot()
        user_id = 9911
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="StartFunnelUser", name="StartFunnelUser", gender="male"))
            await session.commit()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="StartFunnelUser", username="start_user"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
            answer=AsyncMock(),
        )
        state = AsyncMock()
        command = SimpleNamespace(args="funnel")
        await handlers.cmd_start(msg, state=state, command=command)

        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertIsNotNone(st)
            self.assertEqual(st.stage, "intro")

    async def test_ai_disclaimer_concurrency_and_retry_on_failure(self):
        await self.test_ai_disclaimer_concurrent_cas()

    async def test_referral_validation_matrix_pre_assignment(self):
        await self.test_referral_registration_validation_matrix()

    async def test_exact_labels_demanded_by_specification(self):
        await self.test_block_6_keyboard_exact_labels_and_callbacks()
        await self.test_block_7_keyboard_exact_labels_count_callbacks()
        await self.test_faq_back_button_exact_label_and_handler()
        await self.test_objection_ai_result_keyboard_labels_and_callbacks()

    async def test_mock_bot_expanded_instrumentation(self):
        bot = MockBot()
        me = await bot.get_me()
        self.assertEqual(me.username, "test_bot")
        await bot.send_message(1, "hello")
        await bot.edit_message_text("updated", chat_id=1, message_id=1)
        await bot.send_photo(1, "photo1")
        await bot.send_video(1, "video1")
        await bot.send_audio(1, "audio1")
        await bot.send_chat_action(1, "typing")
        await bot.send_invoice(1, "title", "desc", "payload", "tok", "rub", [])
        await bot.answer_pre_checkout_query("pq1", True)
        await bot.set_my_commands([])
        await bot.delete_message(1, 1)

        self.assertGreaterEqual(len(bot.sent_messages), 1)
        self.assertGreaterEqual(len(bot.edited_messages), 1)
        self.assertGreaterEqual(len(bot.sent_photos), 1)
        self.assertGreaterEqual(len(bot.sent_videos), 1)
        self.assertGreaterEqual(len(bot.sent_audios), 1)
        self.assertGreaterEqual(len(bot.chat_actions), 1)
        self.assertGreaterEqual(len(bot.sent_invoices), 1)
        self.assertGreaterEqual(len(bot.answered_pre_checkouts), 1)
        self.assertGreaterEqual(len(bot.commands_set), 1)
        self.assertGreaterEqual(len(bot.deleted_messages), 1)

    async def test_prodamus_intent_remains_payable_after_other_plan_selected(self):
        import prodamus_webhooks
        bot = MockBot()
        user_id = 10101
        plan_a_id = 101
        plan_b_id = 102

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="MultiPlanUser", referral_balance=300))
            session.add(SubscriptionPlan(id=plan_a_id, name="Plan A", price=1000.0, duration_value=1, duration_unit="months", is_active=True))
            session.add(SubscriptionPlan(id=plan_b_id, name="Plan B", price=500.0, duration_value=1, duration_unit="months", is_active=True))
            cfg = await session.get(SubscriptionConfig, 1)
            cfg.prodamus_secret_key = "secret_10101"
            await session.commit()

        # A. User creates Prodamus payment for Intent A
        async with async_session_maker() as session:
            intent_a = await handlers.create_payment_intent(session, user_id=user_id, plan_id=plan_a_id, provider="prodamus")
            self.assertEqual(intent_a.points_reserved, 300)
            self.assertEqual(intent_a.final_amount, 700.0)
            token_a = intent_a.token
            await session.commit()

        # B. User returns and selects another plan (Plan B)
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="MultiPlanUser", username="multi"),
            data=f"select_plan_{plan_b_id}",
            bot=bot,
            message=SimpleNamespace(
                chat=SimpleNamespace(id=user_id),
                message_id=50,
                delete=AsyncMock(),
                edit_text=AsyncMock(),
                content_type="text",
            ),
            answer=AsyncMock(),
        )
        await handlers.process_plan_selection(cb)

        # C. Intent A MUST remain provider-payable and its reserved points remain reserved
        async with async_session_maker() as session:
            ia = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token_a))
            self.assertEqual(ia.status, "pending")
            self.assertEqual(ia.provider, "prodamus")
            active_reserved = await handlers.get_active_reserved_points(session, user_id)
            self.assertGreaterEqual(active_reserved, 300)

        # D & E. Customer later pays Intent A via Prodamus webhook
        payload = {
            "order_id": f"pi_{token_a}",
            "sum": "700.00",
            "payment_status": "success",
        }
        resp = await prodamus_webhooks.process_prodamus_payment(payload, bot=bot)
        self.assertEqual(resp.status, 200)

        # F. Customer receives access exactly once
        async with async_session_maker() as session:
            ia = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token_a))
            self.assertEqual(ia.status, "paid")
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            self.assertIsNotNone(sub)
            self.assertEqual(sub.plan_id, plan_a_id)

    async def test_prodamus_does_not_inherit_local_ttl_with_time_travel(self):
        import prodamus_webhooks
        bot = MockBot()
        user_id = 10201
        plan_id = 103

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TimeTraveler", referral_balance=0))
            session.add(SubscriptionPlan(id=plan_id, name="TTL Plan", price=800.0, duration_value=1, duration_unit="months", allow_auto_renewal=False, is_active=True))
            cfg = await session.get(SubscriptionConfig, 1)
            cfg.prodamus_url = "https://pay.prodamus.ru/test/"
            cfg.prodamus_secret_key = "secret_10201"
            await session.commit()

        # 1. Select plan creates pending_choice intent with 30-min TTL
        cb_plan = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TimeTraveler", username="tt"),
            data=f"select_plan_{plan_id}",
            bot=bot,
            message=SimpleNamespace(
                chat=SimpleNamespace(id=user_id),
                message_id=60,
                delete=AsyncMock(),
                edit_text=AsyncMock(),
                content_type="text",
            ),
            answer=AsyncMock(),
        )
        await handlers.process_plan_selection(cb_plan)

        async with async_session_maker() as session:
            intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.user_id == user_id, PaymentIntent.status == "pending"))
            self.assertIsNotNone(intent)
            self.assertIsNotNone(intent.expires_at)
            token = intent.token

        # 2. Choose Prodamus: expires_at must be cleared to None
        cb_prod = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="TimeTraveler", username="tt"),
            data=f"pay_prodamus_{token}",
            bot=bot,
            message=SimpleNamespace(
                chat=SimpleNamespace(id=user_id),
                message_id=61,
                delete=AsyncMock(),
                edit_text=AsyncMock(),
                content_type="text",
            ),
            answer=AsyncMock(),
        )
        await handlers.pay_prodamus(cb_prod)

        async with async_session_maker() as session:
            intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(intent.provider, "prodamus")
            self.assertIsNone(intent.expires_at)

        # 3. Advance time by 45 minutes and process provider webhook
        future_time = msk_now() + timedelta(minutes=45)
        with patch("handlers.msk_now", return_value=future_time), patch("prodamus_webhooks.msk_now", return_value=future_time):
            payload = {
                "order_id": f"pi_{token}",
                "sum": "800.00",
                "payment_status": "success",
            }
            resp = await prodamus_webhooks.process_prodamus_payment(payload, bot=bot)
            self.assertEqual(resp.status, 200)

        # 4. Settlement still succeeds
        async with async_session_maker() as session:
            intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(intent.status, "paid")
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            self.assertIsNotNone(sub)
            self.assertEqual(sub.plan_id, plan_id)

    async def test_provider_payment_generation_rejects_invalid_intents(self):
        bot = MockBot()
        user_id = 10301
        other_user_id = 10399
        plan_id = 104

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ValidUser"))
            session.add(User(id=other_user_id, first_name="OtherUser"))
            session.add(SubscriptionPlan(id=plan_id, name="Plan 104", price=500.0, duration_value=1, duration_unit="months", is_active=True))
            await session.merge(PaymentProvider(id=1, name="Pay Provider", provider_token="test_token", is_active=True))
            await session.commit()

        for invalid_status in ("paid", "settling", "cancelled", "expired"):
            async with async_session_maker() as session:
                token = f"tok_inv_{invalid_status}_{secrets.token_hex(4)}"
                session.add(PaymentIntent(
                    token=token,
                    user_id=user_id,
                    plan_id=plan_id,
                    gross_amount=500.0,
                    final_amount=500.0,
                    provider="pending_choice",
                    status=invalid_status,
                ))
                await session.commit()

            # Test Prodamus rejection
            alerts = []
            cb_prod = SimpleNamespace(
                from_user=SimpleNamespace(id=user_id),
                data=f"pay_prodamus_{token}",
                bot=bot,
                message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
                answer=AsyncMock(side_effect=lambda text, show_alert=False: alerts.append((text, show_alert))),
            )
            await handlers.pay_prodamus(cb_prod)
            self.assertTrue(any(show_alert for _, show_alert in alerts))

            # Test Telegram invoice rejection
            alerts.clear()
            cb_tg = SimpleNamespace(
                from_user=SimpleNamespace(id=user_id),
                data=f"pay_prov_sub_1_{token}",
                bot=bot,
                message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
                answer=AsyncMock(side_effect=lambda text, show_alert=False: alerts.append((text, show_alert))),
            )
            state = AsyncMock()
            await handlers.process_payment_invoice(cb_tg, state=state, bot=bot)
            self.assertTrue(any(show_alert for _, show_alert in alerts))

        # Test rejection when owned by another user
        async with async_session_maker() as session:
            tok_foreign = f"tok_foreign_{secrets.token_hex(4)}"
            session.add(PaymentIntent(
                token=tok_foreign,
                user_id=user_id,
                plan_id=plan_id,
                gross_amount=500.0,
                final_amount=500.0,
                provider="pending_choice",
                status="pending",
            ))
            await session.commit()

        foreign_alerts = []
        cb_other = SimpleNamespace(
            from_user=SimpleNamespace(id=other_user_id),
            data=f"pay_prodamus_{tok_foreign}",
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=other_user_id), message_id=1, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
            answer=AsyncMock(side_effect=lambda text, show_alert=False: foreign_alerts.append((text, show_alert))),
        )
        await handlers.pay_prodamus(cb_other)
        self.assertTrue(any("другому" in (text or "") for text, _ in foreign_alerts))

    async def test_provider_event_ordering_and_late_success_alert(self):
        import prodamus_webhooks
        from database import ProdamusPayment
        bot = MockBot()
        user_id = 10401
        admin_id = 10499
        plan_id = 105

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="OrderUser"))
            session.add(User(id=admin_id, first_name="AlertAdmin", is_admin=True))
            session.add(SubscriptionPlan(id=plan_id, name="OrderPlan", price=1000.0, duration_value=1, duration_unit="months", is_active=True))
            cfg = await session.get(SubscriptionConfig, 1)
            cfg.prodamus_secret_key = "secret_10401"
            cfg.notifications_enabled = True
            await session.commit()

        # 1. failed -> duplicate failed: idempotent cancellation
        async with async_session_maker() as session:
            intent_fail = await handlers.create_payment_intent(session, user_id=user_id, plan_id=plan_id, provider="prodamus")
            tok_fail = intent_fail.token
            await session.commit()

        fail_payload = {"order_id": f"pi_{tok_fail}", "sum": "1000.00", "payment_status": "failed"}
        r1 = await prodamus_webhooks.process_prodamus_payment(fail_payload, bot=bot)
        r2 = await prodamus_webhooks.process_prodamus_payment(fail_payload, bot=bot)
        self.assertEqual(r1.status, 200)
        self.assertEqual(r2.status, 200)
        async with async_session_maker() as session:
            it = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == tok_fail))
            self.assertEqual(it.status, "cancelled")

        # 2. success -> duplicate success: idempotent paid result
        async with async_session_maker() as session:
            intent_succ = await handlers.create_payment_intent(session, user_id=user_id, plan_id=plan_id, provider="prodamus")
            tok_succ = intent_succ.token
            await session.commit()

        succ_payload = {"order_id": f"pi_{tok_succ}", "sum": "1000.00", "payment_status": "success"}
        r_succ1 = await prodamus_webhooks.process_prodamus_payment(succ_payload, bot=bot)
        r_succ2 = await prodamus_webhooks.process_prodamus_payment(succ_payload, bot=bot)
        self.assertEqual(r_succ1.status, 200)
        self.assertEqual(r_succ2.status, 200)
        async with async_session_maker() as session:
            it = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == tok_succ))
            self.assertEqual(it.status, "paid")

        # 3. success -> later failed: paid state remains paid
        late_fail_payload = {"order_id": f"pi_{tok_succ}", "sum": "1000.00", "payment_status": "failed"}
        r_late_fail = await prodamus_webhooks.process_prodamus_payment(late_fail_payload, bot=bot)
        self.assertEqual(r_late_fail.status, 200)
        async with async_session_maker() as session:
            it = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == tok_succ))
            self.assertEqual(it.status, "paid")

        # 4. definitive failed/cancelled -> later success: alerts admins and records late_success_review
        async with async_session_maker() as session:
            intent_cancel = await handlers.create_payment_intent(session, user_id=user_id, plan_id=plan_id, provider="prodamus")
            intent_cancel.status = "cancelled"
            tok_cancel = intent_cancel.token
            await session.commit()

        late_succ_payload = {"order_id": f"pi_{tok_cancel}", "sum": "1000.00", "payment_status": "success"}
        bot.sent_messages.clear()
        r_late_succ = await prodamus_webhooks.process_prodamus_payment(late_succ_payload, bot=bot)
        self.assertEqual(r_late_succ.status, 200)

        # Check payment record status
        async with async_session_maker() as session:
            pm = await session.scalar(select(ProdamusPayment).where(ProdamusPayment.order_id == f"pi_{tok_cancel}"))
            self.assertIsNotNone(pm)
            self.assertEqual(pm.status, "late_success_review")

        # Check administrator alert containing all 5 required fields
        admin_alert = next((m for m in bot.sent_messages if getattr(m, "chat", None) and m.chat.id == admin_id and "Поздняя успешная оплата" in m.text), None)
        self.assertIsNotNone(admin_alert)
        self.assertIn(tok_cancel, admin_alert.text)
        self.assertIn(f"pi_{tok_cancel}", admin_alert.text)
        self.assertIn(str(user_id), admin_alert.text)
        self.assertIn("1000.00", admin_alert.text)
        self.assertIn("success", admin_alert.text)

    async def test_sqlite_lock_pre_opened_transaction_concurrency(self):
        user_id = 10501
        plan_id = 106

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="LockUser", referral_balance=1000))
            session.add(SubscriptionPlan(id=plan_id, name="LockPlan", price=800.0, duration_value=1, duration_unit="months", is_active=True))
            await session.commit()

        barrier = asyncio.Barrier(2)

        async def worker(idx):
            async with async_session_maker() as session:
                # Pre-opened transaction: execute a SELECT first
                await session.execute(select(User).where(User.id == user_id))
                await barrier.wait()
                intent = await handlers.create_payment_intent(session, user_id=user_id, plan_id=plan_id, provider="telegram_pay")
                await session.commit()
                return intent

        intents = await asyncio.gather(worker(1), worker(2))
        self.assertEqual(len(intents), 2)

        async with async_session_maker() as session:
            active_reserved = await handlers.get_active_reserved_points(session, user_id)
            # Active reserved points must NEVER exceed referral_balance (1000)
            self.assertEqual(active_reserved, 1000)
            res_list = sorted([it.points_reserved for it in intents])
            self.assertEqual(res_list, [200, 800])

    async def test_landing_notification_per_recipient_partial_failure_retry(self):
        import landing_webhook
        customer_id = 10601
        admin_a = 10602
        admin_b = 10603

        async with async_session_maker() as session:
            session.add(User(id=customer_id, first_name="LandCustomer"))
            session.add(User(id=admin_a, first_name="AdminA", is_admin=True))
            session.add(User(id=admin_b, first_name="AdminB", is_admin=True))
            await session.commit()

        fail_admin_b = True

        class FlakyBot(MockBot):
            async def send_message(self, chat_id, text, **kwargs):
                if chat_id == admin_b and fail_admin_b:
                    raise RuntimeError("Admin B network failure")
                return await super().send_message(chat_id, text, **kwargs)

        flaky_bot = FlakyBot()
        payload = {
            "token": "tok_per_rec_test",
            "user_id": customer_id,
            "status": "paid",
            "amount": 2500,
            "currency": "RUB",
        }

        # First attempt: Admin B fails
        await landing_webhook.handle_incoming_webhook(payload, bot=flaky_bot)

        cust_msgs = [m for m in flaky_bot.sent_messages if getattr(m, "chat", None) and m.chat.id == customer_id]
        adm_a_msgs = [m for m in flaky_bot.sent_messages if getattr(m, "chat", None) and m.chat.id == admin_a]
        adm_b_msgs = [m for m in flaky_bot.sent_messages if getattr(m, "chat", None) and m.chat.id == admin_b]

        self.assertEqual(len(cust_msgs), 1)
        self.assertEqual(len(adm_a_msgs), 1)
        self.assertEqual(len(adm_b_msgs), 0)

        async with async_session_maker() as session:
            land = await session.scalar(select(FunnelLanding).where(FunnelLanding.token == "tok_per_rec_test"))
            self.assertFalse(land.paid_notified)

        # Second attempt: Admin B recovered
        fail_admin_b = False
        await landing_webhook.handle_incoming_webhook(payload, bot=flaky_bot)

        cust_msgs_2 = [m for m in flaky_bot.sent_messages if getattr(m, "chat", None) and m.chat.id == customer_id]
        adm_a_msgs_2 = [m for m in flaky_bot.sent_messages if getattr(m, "chat", None) and m.chat.id == admin_a]
        adm_b_msgs_2 = [m for m in flaky_bot.sent_messages if getattr(m, "chat", None) and m.chat.id == admin_b]

        # Customer and Admin A received 0 additional messages
        self.assertEqual(len(cust_msgs_2), 1)
        self.assertEqual(len(adm_a_msgs_2), 1)
        # Admin B received exactly 1 retry message
        self.assertEqual(len(adm_b_msgs_2), 1)

        async with async_session_maker() as session:
            land = await session.scalar(select(FunnelLanding).where(FunnelLanding.token == "tok_per_rec_test"))
            self.assertTrue(land.paid_notified)

    async def test_ai_disclaimer_db_concurrency_safe(self):
        from database import AiDisclaimerClaim
        bot = MockBot()
        user_id = 10701

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="AiDiscUser", ai_disclaimer_shown=False))
            await session.commit()

        barrier = asyncio.Barrier(2)

        async def worker(idx):
            await barrier.wait()
            return await handlers.ensure_ai_disclaimer_sent(bot, user_id)

        results = await asyncio.gather(worker(1), worker(2))
        # Exactly one worker succeeded in sending
        self.assertEqual(sorted(results), [False, True])

        disc_msgs = [m for m in bot.sent_messages if getattr(m, "chat", None) and m.chat.id == user_id]
        self.assertEqual(len(disc_msgs), 1)

        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertTrue(u.ai_disclaimer_shown)
            claim = await session.get(AiDisclaimerClaim, user_id)
            self.assertEqual(claim.status, "delivered")

        # Subsequent call does not resend
        res_after = await handlers.ensure_ai_disclaimer_sent(bot, user_id)
        self.assertFalse(res_after)
        self.assertEqual(len([m for m in bot.sent_messages if getattr(m, "chat", None) and m.chat.id == user_id]), 1)

        # Regression test for walkthrough evidence: failed send -> DB claim released -> next call retries successfully
        user_id_fail = 10702
        async with async_session_maker() as session:
            session.add(User(id=user_id_fail, first_name="FailUser", ai_disclaimer_shown=False))
            await session.commit()

        failing_bot = MockBot()
        failing_bot.send_message = AsyncMock(side_effect=Exception("Network error on Telegram send"))
        res_fail = await handlers.ensure_ai_disclaimer_sent(failing_bot, user_id_fail)
        self.assertFalse(res_fail)

        async with async_session_maker() as session:
            u_fail = await session.get(User, user_id_fail)
            self.assertFalse(u_fail.ai_disclaimer_shown)
            claim_fail = await session.get(AiDisclaimerClaim, user_id_fail)
            self.assertIsNone(claim_fail)

        # Next call with working bot retries and succeeds
        res_retry = await handlers.ensure_ai_disclaimer_sent(bot, user_id_fail)
        self.assertTrue(res_retry)
        async with async_session_maker() as session:
            u_retry = await session.get(User, user_id_fail)
            self.assertTrue(u_retry.ai_disclaimer_shown)
            claim_retry = await session.get(AiDisclaimerClaim, user_id_fail)
            self.assertEqual(claim_retry.status, "delivered")

    async def test_zero_cash_customer_ux_select_plan_callback(self):
        bot = MockBot()
        user_id = 10801
        plan_id = 108

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ZeroUser", referral_balance=2000))
            session.add(SubscriptionPlan(id=plan_id, name="ZeroPlan", price=800.0, duration_value=1, duration_unit="months", is_active=True))
            await session.commit()

        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="ZeroUser", username="zero"),
            data=f"select_plan_{plan_id}",
            bot=bot,
            message=SimpleNamespace(
                chat=SimpleNamespace(id=user_id),
                message_id=77,
                delete=AsyncMock(),
                edit_text=AsyncMock(),
                content_type="text",
            ),
            answer=AsyncMock(),
        )

        await handlers.process_plan_selection(cb)

        # Check message sent to user: EXACTLY ONE customer success message sent
        user_msgs = [m for m in bot.sent_messages if getattr(m, "chat", None) and m.chat.id == user_id]
        self.assertEqual(len(user_msgs), 1)
        success_msg = user_msgs[0]
        self.assertIn("успешно активирован", success_msg.text)
        self.assertIn("0.00 RUB", success_msg.text)
        self.assertIn("В главное меню", str(success_msg.reply_markup))

        # Check NO payment provider selection buttons
        all_markup_str = str(success_msg.reply_markup)
        self.assertNotIn("pay_prov_", all_markup_str)
        self.assertNotIn("pay_prodamus_", all_markup_str)
        self.assertNotIn("Выбери способ оплаты", success_msg.text)

        # Check subscription active
        async with async_session_maker() as session:
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            self.assertIsNotNone(sub)
            self.assertEqual(sub.plan_id, plan_id)

    async def test_payment_audit_fields_persistence(self):
        bot = MockBot()
        user_id = 10901
        plan_id = 109

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="AuditUser"))
            session.add(SubscriptionPlan(id=plan_id, name="AuditPlan", price=600.0, duration_value=1, duration_unit="months", is_active=True))
            await session.commit()

        # Telegram Pay persistence
        async with async_session_maker() as session:
            intent_tg = await handlers.create_payment_intent(session, user_id=user_id, plan_id=plan_id, provider="telegram_pay")
            await handlers.settle_payment_intent(session, bot, intent_tg, payment_charge_id="tg_charge_unique_123")
            await session.commit()

        async with async_session_maker() as session:
            it = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == intent_tg.token))
            self.assertEqual(it.provider_payment_id, "tg_charge_unique_123")

        # Prodamus persistence
        async with async_session_maker() as session:
            intent_pr = await handlers.create_payment_intent(session, user_id=user_id, plan_id=plan_id, provider="prodamus")
            await handlers.settle_payment_intent(session, bot, intent_pr, payment_charge_id="prodamus_pay_key_456")
            await session.commit()

        async with async_session_maker() as session:
            it = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == intent_pr.token))
            self.assertEqual(it.provider_payment_id, "prodamus_pay_key_456")

        # Duplicate callback must NOT overwrite provider_payment_id
        async with async_session_maker() as session:
            it_fresh = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == intent_pr.token))
            it_fresh.status = "pending"  # test attempt to settle with different id
            await handlers.settle_payment_intent(session, bot, it_fresh, payment_charge_id="overwriting_attempt_789")
            await session.commit()

        async with async_session_maker() as session:
            it_final = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == intent_pr.token))
            self.assertEqual(it_final.provider_payment_id, "prodamus_pay_key_456")

    async def test_provider_claim_concurrency_race_with_barrier(self):
        """
        Required real concurrency test with a barrier:
        same PaymentIntent -> Telegram callback and Prodamus callback concurrently
        -> exactly ONE external payment instrument exists
        -> intent.provider equals that provider
        -> no second provider payment can be created.
        """
        bot = MockBot()
        user_id = 11001
        plan_id = 110
        prov_id = 1

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="RaceUser"))
            session.add(SubscriptionPlan(id=plan_id, name="RacePlan", price=500.0, duration_value=1, duration_unit="months", allow_auto_renewal=False, is_active=True))
            session.add(PaymentProvider(id=prov_id, name="TestProv", provider_token="test_tok_123"))
            cfg = await session.get(SubscriptionConfig, 1)
            cfg.prodamus_url = "https://pay.prodamus.ru/test/"
            cfg.prodamus_secret_key = "secret_race"
            await session.commit()

        # Create intent in pending_choice
        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(session, user_id, plan_id, provider="pending_choice", bot=bot)
            token = intent.token

        barrier = asyncio.Barrier(2)

        async def call_telegram():
            cb_tg = SimpleNamespace(
                from_user=SimpleNamespace(id=user_id, first_name="RaceUser", username="race"),
                data=f"pay_prov_sub_{prov_id}_{token}",
                bot=bot,
                message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=101, delete=AsyncMock()),
                answer=AsyncMock(),
            )
            state = AsyncMock()
            await barrier.wait()
            await handlers.process_payment_invoice(cb_tg, state, bot)

        async def call_prodamus():
            cb_prod = SimpleNamespace(
                from_user=SimpleNamespace(id=user_id, first_name="RaceUser", username="race"),
                data=f"pay_prodamus_{token}",
                bot=bot,
                message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=102, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
                answer=AsyncMock(),
            )
            await barrier.wait()
            await handlers.pay_prodamus(cb_prod)

        await asyncio.gather(call_telegram(), call_prodamus())

        # Exactly ONE external payment instrument exists
        invoices = [m for m in bot.sent_invoices if getattr(m, "payload", None) == f"pi_{token}"]
        async with async_session_maker() as session:
            prodamus_pmts = (await session.execute(
                select(ProdamusPayment).where(ProdamusPayment.order_id == f"pi_{token}")
            )).scalars().all()
            final_intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))

        total_instruments = len(invoices) + len(prodamus_pmts)
        self.assertEqual(total_instruments, 1)
        self.assertIn(final_intent.provider, ("telegram_pay", "prodamus"))
        if final_intent.provider == "telegram_pay":
            self.assertEqual(len(invoices), 1)
            self.assertEqual(len(prodamus_pmts), 0)
        else:
            self.assertEqual(len(invoices), 0)
            self.assertEqual(len(prodamus_pmts), 1)

        # Confirm no second provider payment can be created subsequently
        if final_intent.provider == "telegram_pay":
            cb_prod_late = SimpleNamespace(
                from_user=SimpleNamespace(id=user_id, first_name="RaceUser", username="race"),
                data=f"pay_prodamus_{token}",
                bot=bot,
                message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=103, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
                answer=AsyncMock(),
            )
            await handlers.pay_prodamus(cb_prod_late)
            cb_prod_late.answer.assert_called_with("Для этого заказа уже выбран другой способ оплаты.", show_alert=True)
        else:
            cb_tg_late = SimpleNamespace(
                from_user=SimpleNamespace(id=user_id, first_name="RaceUser", username="race"),
                data=f"pay_prov_sub_{prov_id}_{token}",
                bot=bot,
                message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=104, delete=AsyncMock()),
                answer=AsyncMock(),
            )
            await handlers.process_payment_invoice(cb_tg_late, AsyncMock(), bot)
            cb_tg_late.answer.assert_called_with("Для этого заказа уже выбран Prodamus. Сформируй новый заказ.", show_alert=True)

    async def test_select_another_plan_vs_provider_claim_race(self):
        """
        Protect the race: select another plan vs provider claim
        so an intent cannot be locally cancelled at the same moment another request hands it to a provider.
        """
        bot = MockBot()
        user_id = 11050
        plan1_id = 111
        plan2_id = 112
        prov_id = 1

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="RaceUser2"))
            session.add(SubscriptionPlan(id=plan1_id, name="Plan 1", price=500.0, duration_value=1, duration_unit="months", is_active=True))
            session.add(SubscriptionPlan(id=plan2_id, name="Plan 2", price=700.0, duration_value=1, duration_unit="months", is_active=True))
            session.add(PaymentProvider(id=prov_id, name="TestProv", provider_token="test_tok_123"))
            await session.commit()

        # Step 1: User selected plan 1 -> intent1 in pending_choice
        cb_plan1 = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="RaceUser2", username="race2"),
            data=f"select_plan_{plan1_id}",
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=110, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
            answer=AsyncMock(),
        )
        await handlers.process_plan_selection(cb_plan1)

        async with async_session_maker() as session:
            intent1 = await session.scalar(select(PaymentIntent).where(PaymentIntent.user_id == user_id, PaymentIntent.plan_id == plan1_id, PaymentIntent.status == "pending"))
            token1 = intent1.token

        barrier = asyncio.Barrier(2)

        # Worker A selects another plan (plan2)
        async def call_select_plan2():
            cb_plan2 = SimpleNamespace(
                from_user=SimpleNamespace(id=user_id, first_name="RaceUser2", username="race2"),
                data=f"select_plan_{plan2_id}",
                bot=bot,
                message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=111, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
                answer=AsyncMock(),
            )
            await barrier.wait()
            await handlers.process_plan_selection(cb_plan2)

        # Worker B claims intent1 for Telegram Pay
        async def call_claim_intent1():
            cb_tg = SimpleNamespace(
                from_user=SimpleNamespace(id=user_id, first_name="RaceUser2", username="race2"),
                data=f"pay_prov_sub_{prov_id}_{token1}",
                bot=bot,
                message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=112, delete=AsyncMock()),
                answer=AsyncMock(),
            )
            await barrier.wait()
            await handlers.process_payment_invoice(cb_tg, AsyncMock(), bot)

        await asyncio.gather(call_select_plan2(), call_claim_intent1())

        async with async_session_maker() as session:
            intent1_after = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token1))

        invoices1 = [m for m in bot.sent_invoices if getattr(m, "payload", None) == f"pi_{token1}"]
        if intent1_after.status == "cancelled":
            self.assertEqual(len(invoices1), 0)
        else:
            self.assertEqual(intent1_after.provider, "telegram_pay")
            self.assertEqual(intent1_after.status, "pending")
            self.assertEqual(len(invoices1), 1)

    async def test_provider_match_enforced_at_payment_time(self):
        """
        P0: Telegram pre_checkout and successful_payment must require intent.provider == "telegram_pay".
        Prodamus pi_<token> success must require intent.provider == "prodamus".
        Wrong provider arrives -> do not settle, record wrong_provider_review, alert admins.
        """
        bot = MockBot()
        user_id = 11101
        admin_id = 999111
        plan_id = 113

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="MatchUser"))
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            session.add(SubscriptionPlan(id=plan_id, name="MatchPlan", price=500.0, duration_value=1, duration_unit="months", is_active=True))
            await session.commit()

        # 1. Telegram pre_checkout rejected for prodamus intent
        async with async_session_maker() as session:
            intent_prod = await handlers.create_payment_intent(session, user_id, plan_id, provider="prodamus", bot=bot)
            token_prod = intent_prod.token

        pcq = SimpleNamespace(
            id="pcq_wrong_prov_1",
            from_user=SimpleNamespace(id=user_id),
            invoice_payload=f"pi_{token_prod}",
            total_amount=50000,
            currency="RUB",
        )
        await handlers.process_pre_checkout(pcq, bot)
        self.assertEqual(bot.answered_pre_checkouts[-1], ("pcq_wrong_prov_1", False, "Неверный провайдер оплаты."))

        # 2. Telegram successful_payment rejected for prodamus intent
        msg_wrong_tg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            successful_payment=SimpleNamespace(
                invoice_payload=f"pi_{token_prod}",
                total_amount=50000,
                currency="RUB",
                provider_payment_charge_id="tg_charge_wrong",
            ),
        )
        await handlers.process_successful_payment(msg_wrong_tg, bot)
        async with async_session_maker() as session:
            it = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token_prod))
            self.assertNotEqual(it.status, "paid")

        # 3. Prodamus success webhook rejected for telegram_pay intent:
        # records wrong_provider_review, alerts admins, does NOT settle
        async with async_session_maker() as session:
            intent_tg = await handlers.create_payment_intent(session, user_id, plan_id, provider="telegram_pay", bot=bot)
            token_tg = intent_tg.token

        import prodamus_webhooks
        payload_wrong_prod = {
            "order_id": f"pi_{token_tg}",
            "sum": "500.00",
            "payment_status": "success",
        }
        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            resp = await prodamus_webhooks.process_prodamus_payment(payload_wrong_prod, bot=bot)
            self.assertEqual(resp.status, 200)

        async with async_session_maker() as session:
            it_tg_after = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token_tg))
            self.assertNotEqual(it_tg_after.status, "paid")
            prod_pmt = await session.scalar(select(ProdamusPayment).where(ProdamusPayment.order_id == f"pi_{token_tg}"))
            self.assertIsNotNone(prod_pmt)
            self.assertEqual(prod_pmt.status, "wrong_provider_review")

        admin_msgs = [m for m in bot.sent_messages if getattr(m, "chat", None) and m.chat.id == admin_id and "Оплата через Prodamus для заказа другого провайдера" in m.text]
        self.assertGreaterEqual(len(admin_msgs), 1)

    async def test_provider_issuance_idempotency_and_failure_release(self):
        """
        P0:
        - Telegram double-click test: creates exactly ONE invoice.
        - Prodamus double-click test: does not insert duplicate ProdamusPayment.order_id and no IntegrityError.
        - Provider send failure releases/cancels intent so points are freed.
        """
        bot = MockBot()
        user_id = 11201
        plan_id = 114
        prov_id = 1

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="IdemUser", referral_balance=300))
            session.add(SubscriptionPlan(id=plan_id, name="IdemPlan", price=500.0, duration_value=1, duration_unit="months", allow_auto_renewal=False, is_active=True))
            session.add(PaymentProvider(id=prov_id, name="TestProv", provider_token="test_tok_123"))
            cfg = await session.get(SubscriptionConfig, 1)
            cfg.prodamus_url = "https://pay.prodamus.ru/test/"
            cfg.prodamus_secret_key = "secret_idem"
            await session.commit()

        # 1. Telegram double-click test
        async with async_session_maker() as session:
            intent_tg = await handlers.create_payment_intent(session, user_id, plan_id, provider="pending_choice", bot=bot)
            token_tg = intent_tg.token

        cb_tg1 = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="IdemUser", username="idem"),
            data=f"pay_prov_sub_{prov_id}_{token_tg}",
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=121, delete=AsyncMock()),
            answer=AsyncMock(),
        )
        cb_tg2 = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="IdemUser", username="idem"),
            data=f"pay_prov_sub_{prov_id}_{token_tg}",
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=122, delete=AsyncMock()),
            answer=AsyncMock(),
        )
        await handlers.process_payment_invoice(cb_tg1, AsyncMock(), bot)
        await handlers.process_payment_invoice(cb_tg2, AsyncMock(), bot)

        invoices = [m for m in bot.sent_invoices if getattr(m, "payload", None) == f"pi_{token_tg}"]
        self.assertEqual(len(invoices), 1)

        # 2. Prodamus double-click test (no IntegrityError, 1 ProdamusPayment)
        async with async_session_maker() as session:
            intent_prod = await handlers.create_payment_intent(session, user_id, plan_id, provider="pending_choice", bot=bot)
            token_prod = intent_prod.token

        cb_pr1 = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="IdemUser", username="idem"),
            data=f"pay_prodamus_{token_prod}",
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=123, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
            answer=AsyncMock(),
        )
        cb_pr2 = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="IdemUser", username="idem"),
            data=f"pay_prodamus_{token_prod}",
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=124, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
            answer=AsyncMock(),
        )
        await handlers.pay_prodamus(cb_pr1)
        await handlers.pay_prodamus(cb_pr2)  # must not crash with IntegrityError

        async with async_session_maker() as session:
            pmts = (await session.execute(
                select(ProdamusPayment).where(ProdamusPayment.order_id == f"pi_{token_prod}")
            )).scalars().all()
            self.assertEqual(len(pmts), 1)

        # 3. Provider-send failure releases/recoverable checkout test
        user_id_fail = 11202
        async with async_session_maker() as session:
            session.add(User(id=user_id_fail, first_name="FailIdemUser", referral_balance=200))
            await session.commit()

        async with async_session_maker() as session:
            intent_fail = await handlers.create_payment_intent(session, user_id_fail, plan_id, provider="pending_choice", points_to_use=200, bot=bot)
            token_fail = intent_fail.token
            reserved_before = await handlers.get_active_reserved_points(session, user_id_fail)
            self.assertEqual(reserved_before, 200)

        failing_bot = MockBot()
        failing_bot.send_invoice = AsyncMock(side_effect=Exception("Telegram invoice send failed"))

        cb_fail = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id_fail, first_name="FailIdemUser", username="idem"),
            data=f"pay_prov_sub_{prov_id}_{token_fail}",
            bot=failing_bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id_fail), message_id=125, delete=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await handlers.process_payment_invoice(cb_fail, AsyncMock(), failing_bot)

        # Check intent cancelled and reserved points released
        async with async_session_maker() as session:
            it_f = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token_fail))
            self.assertEqual(it_f.status, "cancelled")
            reserved_after = await handlers.get_active_reserved_points(session, user_id_fail)
            self.assertEqual(reserved_after, 0)

    async def test_telegram_pre_checkout_ttl_boundary_and_charge_race(self):
        """
        P0:
        pre-checkout approved 1 second before expiry
        -> successful_payment 2 seconds later
        -> customer gets access exactly once
        -> one Transaction
        -> intent paid.
        Also test:
        pre-checkout after expiry -> rejected -> no charge/access.
        """
        bot = MockBot()
        user_id = 11301
        plan_id = 115

        now = msk_now()
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="TtlUser"))
            session.add(SubscriptionPlan(id=plan_id, name="TtlPlan", price=500.0, duration_value=1, duration_unit="months", is_active=True))
            await session.commit()

        # Case 1: Pre-checkout 1s before expiry -> payment 2s later
        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(session, user_id, plan_id, provider="telegram_pay", bot=bot)
            intent.expires_at = now + timedelta(seconds=1)
            await session.commit()
            token = intent.token

        pcq = SimpleNamespace(
            id="pcq_ttl_1",
            from_user=SimpleNamespace(id=user_id),
            invoice_payload=f"pi_{token}",
            total_amount=50000,
            currency="RUB",
        )
        with patch("handlers.msk_now", return_value=now):
            await handlers.process_pre_checkout(pcq, bot)
            self.assertEqual(bot.answered_pre_checkouts[-1], ("pcq_ttl_1", True, None))

        # Check intent is authorized
        async with async_session_maker() as session:
            it_auth = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(it_auth.status, "authorized")
            self.assertIsNotNone(it_auth.pre_checkout_approved_at)

        # 2 seconds later (original expires_at in the past!): successful_payment arrives
        payment_time = now + timedelta(seconds=2)
        msg_pay = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            successful_payment=SimpleNamespace(
                invoice_payload=f"pi_{token}",
                total_amount=50000,
                currency="RUB",
                provider_payment_charge_id="tg_charge_boundary",
            ),
        )
        with patch("handlers.msk_now", return_value=payment_time):
            await handlers.process_successful_payment(msg_pay, bot)

        async with async_session_maker() as session:
            it_paid = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(it_paid.status, "paid")
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            self.assertIsNotNone(sub)
            txns = (await session.execute(select(Transaction).where(Transaction.user_id == user_id))).scalars().all()
            self.assertEqual(len(txns), 1)

        # Case 2: Pre-checkout after expiry -> rejected
        async with async_session_maker() as session:
            intent_late = await handlers.create_payment_intent(session, user_id, plan_id, provider="telegram_pay", bot=bot)
            intent_late.expires_at = now - timedelta(seconds=10)
            await session.commit()
            token_late = intent_late.token

        pcq_late = SimpleNamespace(
            id="pcq_ttl_late",
            from_user=SimpleNamespace(id=user_id),
            invoice_payload=f"pi_{token_late}",
            total_amount=50000,
            currency="RUB",
        )
        with patch("handlers.msk_now", return_value=now):
            await handlers.process_pre_checkout(pcq_late, bot)
            self.assertEqual(bot.answered_pre_checkouts[-1][1], False)

        async with async_session_maker() as session:
            it_late = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token_late))
            self.assertEqual(it_late.status, "expired")

    async def test_recurring_prodamus_payment_identity_distinct_cycles(self):
        """
        P0:
        same subscription:
        month #1 auto_payment -> extends once + referral reward once.
        exact duplicate delivery of month #1 -> zero additional effects.
        month #2 auto_payment with a different provider payment identity, but still no order_id
        -> extends again + subsequent-payment reward once.
        duplicate month #2 -> zero additional effects.
        """
        import prodamus_webhooks
        bot = MockBot()
        referrer_id = 11400
        user_id = 11401
        plan_id = 116
        subscription_id = 889900
        now = msk_now()

        async with async_session_maker() as session:
            session.add(User(id=referrer_id, first_name="Referrer", referral_balance=0))
            session.add(User(id=user_id, first_name="PayingUser", referrer_id=referrer_id))
            session.add(SubscriptionPlan(id=plan_id, name="SubPlan", price=1000.0, duration_value=1, duration_unit="months", allow_auto_renewal=True, is_active=True))
            session.add(UserSubscription(
                user_id=user_id,
                plan_id=plan_id,
                start_date=now,
                end_date=now + timedelta(days=30),
                prodamus_subscription_id=subscription_id,
                auto_renewal=True,
                payment_provider="Prodamus",
            ))
            cfg = await session.get(SubscriptionConfig, 1)
            cfg.referral_enabled = True
            cfg.referral_reward_mode = "points"
            cfg.referral_first_pay_percent = 20
            cfg.referral_recurring_pay_percent = 10
            await session.commit()

        # Month #1 auto_payment (no order_id, has payment_id = "pmt_cycle_1")
        payload_m1 = {
            "sum": "1000.00",
            "payment_status": "success",
            "payment_id": "pmt_cycle_1",
            "subscription": {
                "id": subscription_id,
                "action_code": "auto_payment",
            }
        }
        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            r1 = await prodamus_webhooks.process_prodamus_payment(payload_m1, bot=bot)
            self.assertEqual(r1.status, 200)

        async with async_session_maker() as session:
            sub1 = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            ref1 = await session.get(User, referrer_id)
            end_m1 = sub1.end_date
            # 1000 * 20% = 200 points for 1st payment
            self.assertEqual(ref1.referral_balance, 200)

        # Exact duplicate delivery of month #1 -> zero additional effects
        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            r1_dup = await prodamus_webhooks.process_prodamus_payment(payload_m1, bot=bot)
            self.assertEqual(r1_dup.status, 200)

        async with async_session_maker() as session:
            sub1_dup = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            ref1_dup = await session.get(User, referrer_id)
            self.assertEqual(sub1_dup.end_date, end_m1)
            self.assertEqual(ref1_dup.referral_balance, 200)

        # Month #2 auto_payment (no order_id, has different payment_id = "pmt_cycle_2")
        payload_m2 = {
            "sum": "1000.00",
            "payment_status": "success",
            "payment_id": "pmt_cycle_2",
            "subscription": {
                "id": subscription_id,
                "action_code": "auto_payment",
            }
        }
        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            r2 = await prodamus_webhooks.process_prodamus_payment(payload_m2, bot=bot)
            self.assertEqual(r2.status, 200)

        async with async_session_maker() as session:
            sub2 = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            ref2 = await session.get(User, referrer_id)
            self.assertGreater(sub2.end_date, end_m1)
            # 200 + 1000 * 10% (subsequent recurring percent) = 300 points
            self.assertEqual(ref2.referral_balance, 300)

        # Exact duplicate of month #2 -> zero additional effects
        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            r2_dup = await prodamus_webhooks.process_prodamus_payment(payload_m2, bot=bot)
            self.assertEqual(r2_dup.status, 200)

        async with async_session_maker() as session:
            sub2_dup = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            ref2_dup = await session.get(User, referrer_id)
            self.assertEqual(sub2_dup.end_date, sub2.end_date)
            self.assertEqual(ref2_dup.referral_balance, 300)

    async def test_customer_facing_copy_informal_ty_static_audit(self):
        # 1. Fallback welcome message in handlers.py
        with open("handlers.py", "r", encoding="utf-8") as f:
            handlers_code = f.read()

        self.assertIn("Я буду общаться на ты, чтобы наш контакт был ближе и проще.", handlers_code)
        self.assertNotIn("Я буду общаться на ты — так проще и доверительнее.", handlers_code)

        # 2. Check no customer-facing formal strings in user flow files
        forbidden_customer_strings = [
            "Выберите раздел:",
            "Ваш доступ к образовательной программе истёк",
            "Выберите полезную привычку",
            "Выберите готовую или создайте свою",
            "Ваши привычки:\n",
            "Ваша награда:",
            "Готовы начать?",
            "Укажите пол:",
            "Выберите способ оплаты доступа",
            "🤖 Режим активирован. Напишите ваш запрос.",
            "Введите промокод:",
            "Введите сумму пополнения в рублях",
            "Минимальная сумма 10 рублей. Введите число.",
            "Выберите ваш пол:",
            "Введите ваш возраст числом",
            "Пожалуйста, введите возраст",
            "Пожалуйста, введите реальный возраст.",
        ]

        for s in forbidden_customer_strings:
            self.assertNotIn(s, handlers_code, f"Found formal string '{s}' in handlers.py")

        # 3. Expanded audit: Check customer handler functions in handlers.py for formal Russian
        import ast
        tree = ast.parse(handlers_code, filename="handlers.py")
        customer_handler_names = {
            "process_token_amount",
            "sub_enter_promo",
            "settings_change_gender",
            "process_gender",
            "settings_change_age",
            "process_age",
            "open_ai_menu_option",
            "pay_topup_balance",
        }
        formal_markers = ["ваш", "ваша", "ваше", "ваши", "вам", "вас", "выберите", "введите"]

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in customer_handler_names:
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            text_lower = sub.value.lower()
                            for marker in formal_markers:
                                words = re.findall(r"\b[а-яёa-z0-9_]+\b", text_lower)
                                self.assertNotIn(
                                    marker,
                                    words,
                                    f"Customer handler {node.name} contains formal marker '{marker}' in text: {sub.value}"
                                )

        with open("funnel.py", "r", encoding="utf-8") as f:
            funnel_code = f.read()
        self.assertNotIn("Готовы начать?", funnel_code)
        self.assertNotIn("Укажите пол:", funnel_code)
        self.assertNotIn("Выберите практику.", funnel_code)

        with open("subscription_scheduler.py", "r", encoding="utf-8") as f:
            sched_code = f.read()
        self.assertNotIn("Ваш пробный период истекает", sched_code)
        self.assertNotIn("Выберите тариф образовательной программы для продолжения.", sched_code)

        with open("universal_tests.py", "r", encoding="utf-8") as f:
            univ_code = f.read()
        self.assertNotIn("Напишите свой ответ или выберите из предложенных ниже.", univ_code)
        self.assertNotIn("Напишите ответ сообщением.", univ_code)
        self.assertIn("Напиши свой ответ или выбери из предложенных ниже.", univ_code)
        self.assertIn("Напиши ответ сообщением.", univ_code)

    async def test_prodamus_config_missing_does_not_claim_intent_leaves_pending_choice(self):
        """
        Regression test:
        pending_choice intent with reserved points
        -> Prodamus config missing (e.g. prodamus_url is None)
        -> click Prodamus
        -> provider remains pending_choice
        -> status remains pending
        -> reserved points unchanged
        -> no ProdamusPayment
        -> Telegram provider can still successfully claim same intent.
        """
        bot = MockBot()
        user_id = 11501
        plan_id = 117
        prov_id = 1

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ConfigUser", referral_balance=300))
            session.add(SubscriptionPlan(
                id=plan_id,
                name="Plan With Missing Prodamus Config",
                price=1000.0,
                duration_value=1,
                duration_unit="months",
                allow_auto_renewal=False,
                is_active=True,
            ))
            session.add(PaymentProvider(id=prov_id, name="TelegramPayProv", provider_token="test_tg_token_11501"))
            cfg = await session.get(SubscriptionConfig, 1)
            cfg.prodamus_url = None
            cfg.prodamus_secret_key = "secret_key_11501"
            await session.commit()

        # Step 1: Create pending_choice intent with points reserved
        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(
                session,
                user_id=user_id,
                plan_id=plan_id,
                provider="pending_choice",
                points_to_use=300,
                bot=bot,
            )
            token = intent.token
            self.assertEqual(intent.provider, "pending_choice")
            self.assertEqual(intent.status, "pending")
            self.assertEqual(intent.points_reserved, 300)
            self.assertEqual(intent.final_amount, 700.0)

        # Step 2: Click Prodamus with missing config
        cb_prod = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="ConfigUser", username="conf"),
            data=f"pay_prodamus_{token}",
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=201, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
            answer=AsyncMock(),
        )
        await handlers.pay_prodamus(cb_prod)
        cb_prod.answer.assert_called_with("Prodamus не настроен полностью.", show_alert=True)

        # Step 3: Assert provider remains pending_choice, status pending, points reserved unchanged, no ProdamusPayment
        async with async_session_maker() as session:
            intent_after = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(intent_after.provider, "pending_choice")
            self.assertEqual(intent_after.status, "pending")
            self.assertEqual(intent_after.points_reserved, 300)
            self.assertIsNotNone(intent_after.expires_at)
            pmt = await session.scalar(select(ProdamusPayment).where(ProdamusPayment.order_id == f"pi_{token}"))
            self.assertIsNone(pmt)

        # Step 4: Telegram provider can still successfully claim the same intent
        cb_tg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="ConfigUser", username="conf"),
            data=f"pay_prov_sub_{prov_id}_{token}",
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=202, delete=AsyncMock()),
            answer=AsyncMock(),
        )
        state = AsyncMock()
        await handlers.process_payment_invoice(cb_tg, state, bot)

        # Verify Telegram claimed intent and sent invoice
        async with async_session_maker() as session:
            intent_final = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(intent_final.provider, "telegram_pay")
            self.assertEqual(intent_final.status, "pending")

        sent_invoices = [m for m in bot.sent_invoices if getattr(m, "payload", None) == f"pi_{token}"]
        self.assertEqual(len(sent_invoices), 1)

    async def test_prodamus_missing_subscription_id_does_not_claim_intent_leaves_pending_choice(self):
        """
        Regression test:
        auto-renewal plan without required Prodamus subscription ID
        -> click Prodamus
        -> provider remains pending_choice
        -> status remains pending
        -> reserved points unchanged
        -> no ProdamusPayment
        -> Telegram provider can still successfully claim same intent.
        """
        bot = MockBot()
        user_id = 11502
        plan_id = 118
        prov_id = 1

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="NoSubIdUser", referral_balance=250))
            session.add(SubscriptionPlan(
                id=plan_id,
                name="Auto-Renewal Plan Without Sub ID",
                price=800.0,
                duration_value=1,
                duration_unit="months",
                allow_auto_renewal=True,
                prodamus_subscription_id=None,
                is_active=True,
            ))
            session.add(PaymentProvider(id=prov_id, name="TelegramPayProv", provider_token="test_tg_token_11502"))
            cfg = await session.get(SubscriptionConfig, 1)
            cfg.prodamus_url = "https://pay.prodamus.ru/test/"
            cfg.prodamus_secret_key = "secret_key_11502"
            await session.commit()

        # Step 1: Create pending_choice intent with points reserved
        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(
                session,
                user_id=user_id,
                plan_id=plan_id,
                provider="pending_choice",
                points_to_use=250,
                bot=bot,
            )
            token = intent.token
            self.assertEqual(intent.provider, "pending_choice")
            self.assertEqual(intent.status, "pending")
            self.assertEqual(intent.points_reserved, 250)

        # Step 2: Click Prodamus with missing subscription ID
        cb_prod = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="NoSubIdUser", username="nosub"),
            data=f"pay_prodamus_{token}",
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=203, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
            answer=AsyncMock(),
        )
        await handlers.pay_prodamus(cb_prod)
        cb_prod.answer.assert_called_with("Для тарифа не задан код подписки в Prodamus.", show_alert=True)

        # Step 3: Assert provider remains pending_choice, status pending, points reserved unchanged, no ProdamusPayment
        async with async_session_maker() as session:
            intent_after = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(intent_after.provider, "pending_choice")
            self.assertEqual(intent_after.status, "pending")
            self.assertEqual(intent_after.points_reserved, 250)
            pmt = await session.scalar(select(ProdamusPayment).where(ProdamusPayment.order_id == f"pi_{token}"))
            self.assertIsNone(pmt)

        # Step 4: Telegram provider can still successfully claim the same intent
        cb_tg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="NoSubIdUser", username="nosub"),
            data=f"pay_prov_sub_{prov_id}_{token}",
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=204, delete=AsyncMock()),
            answer=AsyncMock(),
        )
        state = AsyncMock()
        await handlers.process_payment_invoice(cb_tg, state, bot)

        # Verify Telegram claimed intent and sent invoice
        async with async_session_maker() as session:
            intent_final = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
            self.assertEqual(intent_final.provider, "telegram_pay")
            self.assertEqual(intent_final.status, "pending")

        sent_invoices = [m for m in bot.sent_invoices if getattr(m, "payload", None) == f"pi_{token}"]
        self.assertEqual(len(sent_invoices), 1)

    async def test_recurring_prodamus_payment_without_identity_rejected(self):
        """
        Exact test:
        subscription exists
        payload:
        - action_code=auto_payment
        - subscription_id present
        - NO order_id
        - NO payment_id/transaction id
        - NO stable date/cycle

        Result:
        - no subscription extension;
        - no referral reward;
        - admin alert;
        - no fabricated `sub-{id}-auto_payment` successful payment.
        """
        import prodamus_webhooks
        bot = MockBot()
        referrer_id = 11510
        user_id = 11511
        admin_id = 99901
        plan_id = 119
        subscription_id = 771122
        now = msk_now()
        initial_end_date = now + timedelta(days=30)

        async with async_session_maker() as session:
            session.add(User(id=admin_id, first_name="Admin", is_admin=True))
            session.add(User(id=referrer_id, first_name="Referrer", referral_balance=100))
            session.add(User(id=user_id, first_name="Subscriber", referrer_id=referrer_id))
            session.add(SubscriptionPlan(
                id=plan_id,
                name="SubRecPlan",
                price=1000.0,
                duration_value=1,
                duration_unit="months",
                allow_auto_renewal=True,
                is_active=True,
            ))
            session.add(UserSubscription(
                user_id=user_id,
                plan_id=plan_id,
                start_date=now,
                end_date=initial_end_date,
                prodamus_subscription_id=subscription_id,
                auto_renewal=True,
                payment_provider="Prodamus",
            ))
            cfg = await session.get(SubscriptionConfig, 1)
            cfg.referral_enabled = True
            cfg.referral_reward_mode = "points"
            cfg.referral_recurring_pay_percent = 10
            await session.commit()

        # Payload with action_code=auto_payment and subscription.id, but NO order_id, NO payment_id, NO date/cycle
        payload_no_id = {
            "sum": "1000.00",
            "payment_status": "success",
            "subscription": {
                "id": subscription_id,
                "action_code": "auto_payment",
            }
        }
        with patch("prodamus_webhooks.verify_prodamus_signature", return_value=True):
            resp = await prodamus_webhooks.process_prodamus_payment(payload_no_id, bot=bot)
            self.assertEqual(resp.status, 200)

        # Assert:
        # 1. No subscription extension
        # 2. No referral reward
        # 3. No fabricated completed payment record
        async with async_session_maker() as session:
            sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
            self.assertEqual(sub.end_date, initial_end_date)

            ref = await session.get(User, referrer_id)
            self.assertEqual(ref.referral_balance, 100)

            fabricated_pmt = await session.scalar(
                select(ProdamusPayment).where(ProdamusPayment.order_id == f"sub-{subscription_id}-auto_payment")
            )
            self.assertIsNone(fabricated_pmt)

            completed_pmts = (await session.execute(
                select(ProdamusPayment).where(ProdamusPayment.user_id == user_id, ProdamusPayment.status == "completed")
            )).scalars().all()
            self.assertEqual(len(completed_pmts), 0)

        # 4. Admin alert sent
        alert_msg = next(
            (m for m in bot.sent_messages if getattr(m, "chat", None) and m.chat.id == admin_id and "Security Alert: Prodamus Missing Payment Identity" in m.text),
            None
        )
        self.assertIsNotNone(alert_msg)

    async def test_concurrent_pay_prodamus_idempotency_with_barrier(self):
        """
        Two concurrent callbacks for the SAME already-Prodamus-claimed intent
        race to insert the ProdamusPayment row.
        Uses asyncio.Barrier(2) to ensure both observe existing_pmt is None
        before either commits.
        Assert:
        - asyncio.gather completes without exceptions;
        - exactly one ProdamusPayment row exists;
        - order_id == pi_<token>;
        - PaymentIntent provider == prodamus;
        - status == pending;
        - points reservation unchanged.
        """
        bot = MockBot()
        user_id = 11601
        plan_id = 120

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ConcurrentProdUser", referral_balance=300))
            session.add(SubscriptionPlan(
                id=plan_id,
                name="Concurrent Prodamus Plan",
                price=800.0,
                duration_value=1,
                duration_unit="months",
                allow_auto_renewal=False,
                is_active=True,
            ))
            cfg = await session.get(SubscriptionConfig, 1)
            cfg.prodamus_url = "https://pay.prodamus.ru/test/"
            cfg.prodamus_secret_key = "secret_concurrent_prod"
            await session.commit()

        async with async_session_maker() as session:
            intent = await handlers.create_payment_intent(
                session,
                user_id=user_id,
                plan_id=plan_id,
                provider="prodamus",
                points_to_use=300,
                bot=bot,
            )
            token = intent.token
            self.assertEqual(intent.provider, "prodamus")
            self.assertEqual(intent.status, "pending")
            self.assertEqual(intent.points_reserved, 300)

        barrier = asyncio.Barrier(2)
        real_async_session_maker = handlers.async_session_maker

        class BarrierSessionWrapper:
            def __init__(self, raw_session):
                self._session = raw_session
                self._waited = False

            async def __aenter__(self):
                self._s = await self._session.__aenter__()
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return await self._session.__aexit__(exc_type, exc_val, exc_tb)

            def __getattr__(self, name):
                return getattr(self._s, name)

            async def scalar(self, statement, *args, **kwargs):
                sql_str = str(statement)
                if not self._waited and "prodamus_payments" in sql_str:
                    self._waited = True
                    try:
                        await barrier.wait()
                    except asyncio.BrokenBarrierError:
                        pass
                return await self._s.scalar(statement, *args, **kwargs)

        def session_factory():
            return BarrierSessionWrapper(real_async_session_maker())

        cb1 = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="ConcurrentProdUser", username="cpu"),
            data=f"pay_prodamus_{token}",
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=301, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
            answer=AsyncMock(),
        )
        cb2 = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="ConcurrentProdUser", username="cpu"),
            data=f"pay_prodamus_{token}",
            bot=bot,
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=302, delete=AsyncMock(), edit_text=AsyncMock(), content_type="text"),
            answer=AsyncMock(),
        )

        with patch("handlers.async_session_maker", side_effect=session_factory):
            await asyncio.gather(handlers.pay_prodamus(cb1), handlers.pay_prodamus(cb2))

        # Assertions
        async with async_session_maker() as session:
            pmts = (await session.execute(
                select(ProdamusPayment).where(ProdamusPayment.order_id == f"pi_{token}")
            )).scalars().all()
            final_intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))

        self.assertEqual(len(pmts), 1)
        self.assertEqual(pmts[0].order_id, f"pi_{token}")
        self.assertEqual(final_intent.provider, "prodamus")
        self.assertEqual(final_intent.status, "pending")
        self.assertEqual(final_intent.points_reserved, 300)

    async def test_start_with_completed_profile_sends_welcome_and_separate_text_menu(self):
        bot = MockBot()
        user_id = 8101
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="CompletedUser", name="CompletedUser", gender="male"))
            await session.commit()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="CompletedUser", username="completed_user"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )
        command = SimpleNamespace(args=None)
        state = AsyncMock()

        await handlers.cmd_start(msg, state, command)

        # Profile is completed, so onboarding is bypassed and user receives:
        # Message 1: Welcome message with ReplyKeyboardRemove
        # Message 2: Separate text-only main menu with inline keyboard
        self.assertEqual(len(bot.sent_messages), 2)
        from aiogram.types import ReplyKeyboardRemove, InlineKeyboardMarkup
        msg1 = bot.sent_messages[0]
        msg2 = bot.sent_messages[1]

        self.assertIsInstance(msg1.reply_markup, ReplyKeyboardRemove)
        self.assertIsInstance(msg2.reply_markup, InlineKeyboardMarkup)
        # Message 2 has no photo
        self.assertIsNone(getattr(msg2, "photo", None))
        self.assertEqual(len(bot.deleted_messages), 0)

    async def test_menu_cmd_behaves_consistently_without_placeholder_deletion(self):
        bot = MockBot()
        user_id = 8102
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="MenuUser", name="MenuUser", gender="female"))
            await session.commit()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="MenuUser"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )
        state = AsyncMock()
        await handlers.cmd_menu(msg, bot, state)

        # 0 placeholder messages deleted
        self.assertEqual(len(bot.deleted_messages), 0)
        # Exactly 2 messages sent: Welcome (with ReplyKeyboardRemove) + Main Menu (InlineKeyboardMarkup)
        self.assertEqual(len(bot.sent_messages), 2)
        from aiogram.types import ReplyKeyboardRemove, InlineKeyboardMarkup
        self.assertIsInstance(bot.sent_messages[0].reply_markup, ReplyKeyboardRemove)
        self.assertIsInstance(bot.sent_messages[1].reply_markup, InlineKeyboardMarkup)

    async def test_welcome_media_prioritizes_welcome_msg_over_start_msg_without_duplication(self):
        bot = MockBot()
        user_id = 8103
        from content_service import invalidate_content_cache
        invalidate_content_cache()
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="PhotoUser", name="PhotoUser", gender="male"))
            w_cont = await session.get(Content, "welcome_msg")
            if not w_cont:
                w_cont = Content(key="welcome_msg", text_content="Привет из базы!")
                session.add(w_cont)
            else:
                w_cont.text_content = "Привет из базы!"
            s_cont = await session.get(Content, "start_msg")
            if not s_cont:
                s_cont = Content(key="start_msg", text_content="Меню из базы!")
                session.add(s_cont)
            else:
                s_cont.text_content = "Меню из базы!"

            # Clean any old media
            await session.execute(delete(ContentMedia).where(ContentMedia.content_key.in_(["welcome_msg", "start_msg"])))
            # Add media to BOTH welcome_msg and start_msg
            session.add(ContentMedia(content_key="welcome_msg", file_type="photo", file_id="photo_welcome_primary"))
            session.add(ContentMedia(content_key="start_msg", file_type="photo", file_id="photo_start_secondary"))
            await session.commit()

        invalidate_content_cache()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="PhotoUser"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )
        await handlers.cmd_start(msg, AsyncMock(), SimpleNamespace(args=None))

        # Exactly 1 photo was sent across the entire bot session
        self.assertEqual(len(bot.sent_photos), 1)
        # That photo is from welcome_msg (primary), NOT start_msg
        self.assertEqual(bot.sent_photos[0].photo, "photo_welcome_primary")
        self.assertIn("Привет из базы!", bot.sent_photos[0].caption)
        from aiogram.types import ReplyKeyboardRemove, InlineKeyboardMarkup
        self.assertIsInstance(bot.sent_photos[0].reply_markup, ReplyKeyboardRemove)

        # Message 2 is strictly text-only
        self.assertEqual(len(bot.sent_messages), 2)
        menu_msg = bot.sent_messages[1]
        self.assertEqual(menu_msg.text, "Меню из базы!")
        self.assertIsInstance(menu_msg.reply_markup, InlineKeyboardMarkup)
        self.assertIsNone(getattr(menu_msg, "photo", None))

    async def test_welcome_media_falls_back_to_start_msg_when_welcome_msg_has_no_media(self):
        bot = MockBot()
        user_id = 8104
        from content_service import invalidate_content_cache
        invalidate_content_cache()
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="FallbackUser", name="FallbackUser", gender="female"))
            w_cont = await session.get(Content, "welcome_msg")
            if not w_cont:
                session.add(Content(key="welcome_msg", text_content="Текст приветствия"))
            else:
                w_cont.text_content = "Текст приветствия"
            s_cont = await session.get(Content, "start_msg")
            if not s_cont:
                session.add(Content(key="start_msg", text_content="Текст меню"))
            else:
                s_cont.text_content = "Текст меню"

            await session.execute(delete(ContentMedia).where(ContentMedia.content_key.in_(["welcome_msg", "start_msg"])))
            # Only start_msg has media (legacy configuration)
            session.add(ContentMedia(content_key="start_msg", file_type="photo", file_id="photo_legacy_start"))
            await session.commit()

        invalidate_content_cache()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="FallbackUser"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )
        await handlers.cmd_start(msg, AsyncMock(), SimpleNamespace(args=None))

        # Photo from start_msg was used for welcome message
        self.assertEqual(len(bot.sent_photos), 1)
        self.assertEqual(bot.sent_photos[0].photo, "photo_legacy_start")
        self.assertIn("Текст приветствия", bot.sent_photos[0].caption)
        from aiogram.types import ReplyKeyboardRemove, InlineKeyboardMarkup
        self.assertIsInstance(bot.sent_photos[0].reply_markup, ReplyKeyboardRemove)

        # Main menu message remains text-only
        self.assertEqual(len(bot.sent_messages), 2)
        menu_msg = bot.sent_messages[1]
        self.assertEqual(menu_msg.text, "Текст меню")
        self.assertIsInstance(menu_msg.reply_markup, InlineKeyboardMarkup)

    async def test_graceful_text_degradation_when_no_media_configured(self):
        bot = MockBot()
        user_id = 8105
        from content_service import invalidate_content_cache
        invalidate_content_cache()
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="NoMediaUser", name="NoMediaUser", gender="female"))
            await session.execute(delete(ContentMedia).where(ContentMedia.content_key.in_(["welcome_msg", "start_msg"])))
            await session.commit()

        invalidate_content_cache()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="NoMediaUser"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )
        await handlers.cmd_start(msg, AsyncMock(), SimpleNamespace(args=None))

        self.assertEqual(len(bot.sent_photos), 0)
        self.assertEqual(len(bot.sent_messages), 2)
        from aiogram.types import ReplyKeyboardRemove, InlineKeyboardMarkup
        self.assertIsInstance(bot.sent_messages[0].reply_markup, ReplyKeyboardRemove)
        self.assertIsInstance(bot.sent_messages[1].reply_markup, InlineKeyboardMarkup)

    def _create_mock_cb(self, user_id, data, message, bot=None):
        cb = CallbackQuery.model_construct(
            id=f"cb_{data}",
            from_user=SimpleNamespace(id=user_id, first_name="User", username="user"),
            chat_instance="ci",
            data=data,
            message=message,
            bot=bot,
        )
        object.__setattr__(cb, "answer", AsyncMock())
        return cb

    async def test_text_menu_navigation_edits_in_place_without_resending_media_or_deleting(self):
        bot = MockBot()
        user_id = 8106
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="NavUser"))
            await session.commit()

        mock_msg = SimpleNamespace(
            chat=SimpleNamespace(id=user_id),
            message_id=50,
            content_type="text",
            text="Предыдущий текст подраздела",
            delete=AsyncMock(),
            edit_text=AsyncMock(),
        )
        cb = self._create_mock_cb(user_id, "back_to_main_menu_text", mock_msg, bot=bot)

        await handlers.back_to_main(cb, bot)

        # Message was edited in place
        mock_msg.edit_text.assert_called_once()
        # Message was NOT deleted
        mock_msg.delete.assert_not_called()
        # No new messages sent
        self.assertEqual(len(bot.sent_messages), 0)
        self.assertEqual(len(bot.sent_photos), 0)
        # Callback acknowledged
        cb.answer.assert_called_once()

    async def test_repeated_navigation_handles_message_is_not_modified_as_harmless_noop(self):
        bot = MockBot()
        user_id = 8107
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="RepeatedNavUser"))
            await session.commit()

        mock_msg = SimpleNamespace(
            chat=SimpleNamespace(id=user_id),
            message_id=51,
            content_type="text",
            text="Главное меню",
            delete=AsyncMock(),
            edit_text=AsyncMock(side_effect=TelegramBadRequest(method="editMessageText", message="Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message")),
        )
        cb = self._create_mock_cb(user_id, "back_to_main_menu", mock_msg, bot=bot)

        # Clicking back_to_main again when already on main menu
        await handlers.back_to_main(cb, bot)

        # edit_text was called and raised message is not modified
        mock_msg.edit_text.assert_called_once()
        # Harmless no-op: message was NOT deleted and NOT resent!
        mock_msg.delete.assert_not_called()
        self.assertEqual(len(bot.sent_messages), 0)
        self.assertEqual(len(bot.deleted_messages), 0)

    async def test_main_menu_return_callbacks_coverage(self):
        bot = MockBot()
        user_id = 8108
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="CallbackUser"))
            await session.commit()

        # 1. main_menu_action
        msg1 = SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=61, content_type="text", text="Section", delete=AsyncMock(), edit_text=AsyncMock())
        cb1 = self._create_mock_cb(user_id, "main_menu_action", msg1, bot=bot)
        state1 = AsyncMock()
        await handlers.cb_main_menu_action(cb1, state1, bot)
        msg1.edit_text.assert_called_once()
        msg1.delete.assert_not_called()
        cb1.answer.assert_called_once()

        # 2. exit_ai_mode
        msg2 = SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=62, content_type="text", text="AI chat", delete=AsyncMock(), edit_text=AsyncMock())
        cb2 = self._create_mock_cb(user_id, "exit_ai_mode", msg2, bot=bot)
        state2 = AsyncMock()
        await handlers.exit_ai_mode_callback(cb2, state2, bot)
        msg2.edit_text.assert_called_once()
        msg2.delete.assert_not_called()
        cb2.answer.assert_called_once()

        # 3. cb_cancel_disclaimer
        msg3 = SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=63, content_type="text", text="Disclaimer text", delete=AsyncMock(), edit_text=AsyncMock())
        cb3 = self._create_mock_cb(user_id, "cb_cancel_disclaimer", msg3, bot=bot)
        state3 = AsyncMock()
        await handlers.cb_cancel_disclaimer(cb3, state3, bot)
        msg3.edit_text.assert_called_once()
        msg3.delete.assert_not_called()
        cb3.answer.assert_called_once()

        # 4. funnel_to_main (funnel.py)
        msg4 = SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=64, content_type="text", text="Funnel step", delete=AsyncMock(), edit_text=AsyncMock())
        cb4 = self._create_mock_cb(user_id, "funnel_to_main", msg4, bot=bot)
        state4 = AsyncMock()
        await funnel.funnel_to_main(cb4, state4)
        msg4.edit_text.assert_called_once()
        msg4.delete.assert_not_called()
        cb4.answer.assert_called_once()

        # 5. Diagnostics exit ("❌ Выход")
        msg5 = SimpleNamespace(from_user=SimpleNamespace(id=user_id), chat=SimpleNamespace(id=user_id), text="❌ Выход", bot=bot)
        state5 = AsyncMock()
        await handlers.process_diagnostics(msg5, state5, bot)
        self.assertEqual(len(bot.sent_messages), 1)
        from aiogram.types import InlineKeyboardMarkup
        self.assertIsInstance(bot.sent_messages[-1].reply_markup, InlineKeyboardMarkup)

        # 6. Chat exit ("❌ Выход")
        msg6 = SimpleNamespace(from_user=SimpleNamespace(id=user_id), chat=SimpleNamespace(id=user_id), text="❌ Выход", bot=bot)
        state6 = AsyncMock()
        await handlers.process_chat(msg6, state6, bot)
        self.assertEqual(len(bot.sent_messages), 2)
        self.assertIsInstance(bot.sent_messages[-1].reply_markup, InlineKeyboardMarkup)

    async def test_navigation_from_legacy_photo_message_falls_back_safely_to_delete_and_send(self):
        bot = MockBot()
        user_id = 8109
        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="PhotoFallbackUser"))
            await session.commit()

        # Previous message was a photo (content_type="photo")
        mock_photo_msg = SimpleNamespace(
            chat=SimpleNamespace(id=user_id),
            message_id=70,
            content_type="photo",
            photo=[SimpleNamespace(file_id="p1")],
            delete=AsyncMock(),
            edit_text=AsyncMock(side_effect=TelegramBadRequest(method="editMessageText", message="Bad Request: there is no text in the message to edit")),
        )
        cb = self._create_mock_cb(user_id, "back_to_main_menu_text", mock_photo_msg, bot=bot)

        await handlers.back_to_main(cb, bot)

        # Safe fallback: a new text-only main menu is sent without relying on
        # editing a legacy media message.
        mock_photo_msg.delete.assert_not_called()
        self.assertEqual(len(bot.sent_messages), 1)
        from aiogram.types import InlineKeyboardMarkup
        self.assertIsInstance(bot.sent_messages[0].reply_markup, InlineKeyboardMarkup)

    async def test_referral_deep_links_and_onboarding_remain_intact(self):
        bot = MockBot()
        referrer_id = 8110
        new_user_id = 8111
        async with async_session_maker() as session:
            session.add(User(id=referrer_id, first_name="Referrer"))
            await session.commit()

        async def _fake_send(text, **kw):
            return await bot.send_message(new_user_id, text, **kw)

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=new_user_id, first_name="NewRefUser", username="new_ref"),
            chat=SimpleNamespace(id=new_user_id),
            bot=bot,
            answer=_fake_send,
        )
        command = SimpleNamespace(args=str(referrer_id))
        state = AsyncMock()

        await handlers.cmd_start(msg, state, command)

        # Welcome message sent with ReplyKeyboardRemove
        self.assertGreaterEqual(len(bot.sent_messages), 1)
        from aiogram.types import ReplyKeyboardRemove
        self.assertIsInstance(bot.sent_messages[0].reply_markup, ReplyKeyboardRemove)

        # Onboarding NOT bypassed: user is asked for name
        state.set_state.assert_called_with(funnel.ProfileStates.waiting_name)

        async with async_session_maker() as session:
            new_u = await session.get(User, new_user_id)
            self.assertEqual(new_u.referrer_id, referrer_id)

    async def test_funnel_resume_mode_preserves_routing_without_forcing_main_menu(self):
        bot = MockBot()
        user_id = 8112
        async with async_session_maker() as session:
            user = User(id=user_id, first_name="ResumeUser", name="ResumeUser", gender="female")
            session.add(user)
            fstate = FunnelState(user_id=user_id, stage="meditation_choice", is_active=True)
            session.add(fstate)
            await session.commit()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="ResumeUser"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )
        state = AsyncMock()
        await handlers.cmd_start(msg, state, SimpleNamespace(args=None))

        # Welcome message sent first
        self.assertGreaterEqual(len(bot.sent_messages), 1)
        from aiogram.types import ReplyKeyboardRemove
        self.assertIsInstance(bot.sent_messages[0].reply_markup, ReplyKeyboardRemove)
        # Resumed funnel menu was shown, NOT forced to main menu
        async with async_session_maker() as session:
            st = await session.get(FunnelState, user_id)
            self.assertEqual(st.stage, "meditation_choice")

    async def test_start_with_long_welcome_text_and_photo_in_start_msg_delivers_three_messages(self):
        from content_service import invalidate_content_cache
        invalidate_content_cache()
        bot = MockBot()
        user_id = 8112
        long_welcome = "Добро пожаловать в нашу систему практик и медитаций! " * 30  # ~1650 chars > 1024
        self.assertGreater(len(long_welcome), 1024)

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="LongUser", name="LongUser", gender="female"))
            # welcome_msg has long text and NO media
            w_msg = await session.get(Content, "welcome_msg")
            if not w_msg:
                session.add(Content(key="welcome_msg", text_content=long_welcome))
            else:
                w_msg.text_content = long_welcome

            # start_msg has photo media
            s_msg = await session.get(Content, "start_msg")
            if not s_msg:
                session.add(Content(key="start_msg", text_content="Главное меню"))
            else:
                s_msg.text_content = "Главное меню"

            await session.execute(delete(ContentMedia).where(ContentMedia.content_key.in_(["welcome_msg", "start_msg"])))
            session.add(ContentMedia(content_key="start_msg", file_id="photo_from_start_only", file_type="photo"))
            await session.commit()
        invalidate_content_cache()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="LongUser", username="long_user"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )
        state = AsyncMock()
        await handlers.cmd_start(msg, state, SimpleNamespace(args=None))

        # Must deliver 3 messages:
        # 1. Standalone photo (from start_msg fallback) without long caption
        # 2. Complete welcome text message with ReplyKeyboardRemove
        # 3. Text-only main menu with main_menu_keyboard
        self.assertEqual(len(bot.sent_messages), 3)
        self.assertEqual(len(bot.sent_photos), 1)
        self.assertEqual(len(bot.deleted_messages), 0)

        photo_msg = bot.sent_messages[0]
        text_msg = bot.sent_messages[1]
        menu_msg = bot.sent_messages[2]

        self.assertEqual(photo_msg.photo, "photo_from_start_only")
        self.assertIsNone(photo_msg.caption)

        self.assertEqual(text_msg.text, long_welcome)
        self.assertIsInstance(text_msg.reply_markup, ReplyKeyboardRemove)

        self.assertEqual(menu_msg.text, "Главное меню")
        self.assertIsInstance(menu_msg.reply_markup, InlineKeyboardMarkup)

        # Database content is preserved without truncation
        async with async_session_maker() as session:
            stored_w = await session.get(Content, "welcome_msg")
            self.assertEqual(stored_w.text_content, long_welcome)
        invalidate_content_cache()

    async def test_menu_with_long_welcome_text_and_photo_in_start_msg_delivers_three_messages(self):
        from content_service import invalidate_content_cache
        invalidate_content_cache()
        bot = MockBot()
        user_id = 8113
        long_welcome = "Приветствуем в меню бота! Вот подробная информация: " * 25  # ~1300 chars > 1024
        self.assertGreater(len(long_welcome), 1024)

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="MenuLongUser", name="MenuLongUser", gender="male"))
            w_msg = await session.get(Content, "welcome_msg")
            if not w_msg:
                session.add(Content(key="welcome_msg", text_content=long_welcome))
            else:
                w_msg.text_content = long_welcome

            s_msg = await session.get(Content, "start_msg")
            if not s_msg:
                session.add(Content(key="start_msg", text_content="Навигационное меню"))
            else:
                s_msg.text_content = "Навигационное меню"

            await session.execute(delete(ContentMedia).where(ContentMedia.content_key.in_(["welcome_msg", "start_msg"])))
            session.add(ContentMedia(content_key="start_msg", file_id="photo_for_menu_test", file_type="photo"))
            await session.commit()
        invalidate_content_cache()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="MenuLongUser", username="menulong"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )
        await handlers.cmd_menu(msg, bot)

        # 3 messages: photo, full welcome text with ReplyKeyboardRemove, text menu
        self.assertEqual(len(bot.sent_messages), 3)
        self.assertEqual(len(bot.sent_photos), 1)
        self.assertEqual(len(bot.deleted_messages), 0)

        photo_msg = bot.sent_messages[0]
        text_msg = bot.sent_messages[1]
        menu_msg = bot.sent_messages[2]

        self.assertEqual(photo_msg.photo, "photo_for_menu_test")
        self.assertIsNone(photo_msg.caption)

        self.assertEqual(text_msg.text, long_welcome)
        self.assertIsInstance(text_msg.reply_markup, ReplyKeyboardRemove)

        self.assertEqual(menu_msg.text, "Навигационное меню")
        self.assertIsInstance(menu_msg.reply_markup, InlineKeyboardMarkup)
        invalidate_content_cache()

    async def test_start_with_short_welcome_text_and_photo_in_start_msg_delivers_two_messages(self):
        from content_service import invalidate_content_cache
        invalidate_content_cache()
        bot = MockBot()
        user_id = 8114
        short_welcome = "Краткое приветствие бота."
        self.assertLessEqual(len(short_welcome), 1024)

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ShortUser", name="ShortUser", gender="female"))
            w_msg = await session.get(Content, "welcome_msg")
            if not w_msg:
                session.add(Content(key="welcome_msg", text_content=short_welcome))
            else:
                w_msg.text_content = short_welcome

            s_msg = await session.get(Content, "start_msg")
            if not s_msg:
                session.add(Content(key="start_msg", text_content="Основное меню"))
            else:
                s_msg.text_content = "Основное меню"

            await session.execute(delete(ContentMedia).where(ContentMedia.content_key.in_(["welcome_msg", "start_msg"])))
            session.add(ContentMedia(content_key="start_msg", file_id="photo_short_start", file_type="photo"))
            await session.commit()
        invalidate_content_cache()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="ShortUser", username="shortuser"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )
        state = AsyncMock()
        await handlers.cmd_start(msg, state, SimpleNamespace(args=None))

        # Normal 2-message delivery:
        # 1. Photo with caption=short_welcome and ReplyKeyboardRemove
        # 2. Text-only main menu
        self.assertEqual(len(bot.sent_messages), 2)
        self.assertEqual(len(bot.sent_photos), 1)
        self.assertEqual(len(bot.deleted_messages), 0)

        photo_msg = bot.sent_messages[0]
        menu_msg = bot.sent_messages[1]

        self.assertEqual(photo_msg.photo, "photo_short_start")
        self.assertEqual(photo_msg.caption, short_welcome)
        self.assertIsInstance(photo_msg.reply_markup, ReplyKeyboardRemove)

        self.assertEqual(menu_msg.text, "Основное меню")
        self.assertIsInstance(menu_msg.reply_markup, InlineKeyboardMarkup)
        invalidate_content_cache()

    async def test_menu_with_short_welcome_text_and_photo_in_start_msg_delivers_two_messages(self):
        from content_service import invalidate_content_cache
        invalidate_content_cache()
        bot = MockBot()
        user_id = 8115
        short_welcome = "Короткое описание для команды меню."
        self.assertLessEqual(len(short_welcome), 1024)

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ShortMenuUser", name="ShortMenuUser", gender="male"))
            w_msg = await session.get(Content, "welcome_msg")
            if not w_msg:
                session.add(Content(key="welcome_msg", text_content=short_welcome))
            else:
                w_msg.text_content = short_welcome

            s_msg = await session.get(Content, "start_msg")
            if not s_msg:
                session.add(Content(key="start_msg", text_content="Выбор разделов"))
            else:
                s_msg.text_content = "Выбор разделов"

            await session.execute(delete(ContentMedia).where(ContentMedia.content_key.in_(["welcome_msg", "start_msg"])))
            session.add(ContentMedia(content_key="start_msg", file_id="photo_short_menu", file_type="photo"))
            await session.commit()
        invalidate_content_cache()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="ShortMenuUser", username="shortmenu"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )
        await handlers.cmd_menu(msg, bot)

        self.assertEqual(len(bot.sent_messages), 2)
        self.assertEqual(len(bot.sent_photos), 1)
        self.assertEqual(len(bot.deleted_messages), 0)

        photo_msg = bot.sent_messages[0]
        menu_msg = bot.sent_messages[1]

        self.assertEqual(photo_msg.photo, "photo_short_menu")
        self.assertEqual(photo_msg.caption, short_welcome)
        self.assertIsInstance(photo_msg.reply_markup, ReplyKeyboardRemove)

        self.assertEqual(menu_msg.text, "Выбор разделов")
        self.assertIsInstance(menu_msg.reply_markup, InlineKeyboardMarkup)
        invalidate_content_cache()

    async def test_welcome_media_reactive_fallback_on_telegram_caption_too_long(self):
        from content_service import invalidate_content_cache
        invalidate_content_cache()
        bot = MockBot()
        user_id = 8116
        welcome_text = "Приветственный текст средней длины для проверки реактивного отката."

        async with async_session_maker() as session:
            session.add(User(id=user_id, first_name="ErrUser", name="ErrUser", gender="male"))
            w_msg = await session.get(Content, "welcome_msg")
            if not w_msg:
                session.add(Content(key="welcome_msg", text_content=welcome_text))
            else:
                w_msg.text_content = welcome_text

            s_msg = await session.get(Content, "start_msg")
            if not s_msg:
                session.add(Content(key="start_msg", text_content="Главное меню"))
            else:
                s_msg.text_content = "Главное меню"

            await session.execute(delete(ContentMedia).where(ContentMedia.content_key.in_(["welcome_msg", "start_msg"])))
            session.add(ContentMedia(content_key="start_msg", file_id="photo_reactive_test", file_type="photo"))
            await session.commit()
        invalidate_content_cache()

        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, first_name="ErrUser", username="erruser"),
            chat=SimpleNamespace(id=user_id),
            bot=bot,
        )

        original_send_photo = bot.send_photo
        attempt = 0

        async def _mock_send_photo(chat_id, photo, caption=None, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt == 1 and caption is not None:
                raise TelegramBadRequest(method="send_photo", message="Bad Request: MEDIA_CAPTION_TOO_LONG")
            return await original_send_photo(chat_id, photo, caption=caption, **kwargs)

        bot.send_photo = _mock_send_photo

        await handlers.send_canonical_welcome_message(msg, bot=bot)

        # Reactive fallback sent photo without caption, then sent text message with ReplyKeyboardRemove
        self.assertEqual(len(bot.sent_photos), 1)
        self.assertIsNone(bot.sent_photos[0].caption)
        self.assertEqual(len(bot.sent_messages), 2)
        text_entry = bot.sent_messages[1]
        self.assertEqual(text_entry.text, welcome_text)
        self.assertIsInstance(text_entry.reply_markup, ReplyKeyboardRemove)
        invalidate_content_cache()

    async def test_walkthrough_test_name_traceability_verification(self):
        walkthrough_paths = [
            "/Users/ivankorakin/.gemini/antigravity-ide/brain/4fc0380a-d8e2-433b-bc5e-d1aa638da9bc/walkthrough.md",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "walkthrough.md"),
        ]

        import ast
        import glob
        test_dir = os.path.dirname(__file__)
        all_test_names = {m for m in dir(self) if m.startswith("test_")}
        for py_file in glob.glob(os.path.join(test_dir, "test_*.py")):
            module_name = os.path.splitext(os.path.basename(py_file))[0]
            all_test_names.add(module_name)
            try:
                with open(py_file, "r", encoding="utf-8") as pf:
                    tree = ast.parse(pf.read(), filename=py_file)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.name.startswith("test_"):
                            all_test_names.add(node.name)
            except Exception:
                pass

        for w_path in walkthrough_paths:
            if not os.path.exists(w_path):
                continue
            with open(w_path, "r", encoding="utf-8") as f:
                content = f.read()

            cited_tests = set(re.findall(r"test_[a-zA-Z0-9_]+", content))
            missing = cited_tests - all_test_names
            self.assertEqual(missing, set(), f"Missing tests cited in {w_path}: {missing}")


if __name__ == "__main__":
    unittest.main()
