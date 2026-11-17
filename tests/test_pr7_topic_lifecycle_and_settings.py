"""End-to-end Fake Telegram behavioral suite for PR #7:
Topic entry lifecycle, pre-dialogue tests, start/new-dialogue non-silent guarantees,
diagnostics migration safety, underscore TestSet binding, and Topic.name management.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Dict, Optional
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/vitaliy_pr7_e2e.db")
sys.modules.setdefault(
    "vector_store",
    SimpleNamespace(get_collection=lambda: None, get_active_path=lambda: None),
)

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendAnimation,
    SendChatAction,
    SendDocument,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    SendVideo,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Update,
    User as TgUser,
)
from sqlalchemy import func, select

import ai_logs_admin
import automation_admin
import database
import followup_admin
import funnel
import handlers
import journal_admin_handlers
import journal_handlers
import keyboards as kb
import test_set_admin_handlers
import test_system_handlers
import topic_admin_handlers
from admin_html_preview import _HTMLPreviewParser
from admin_roles import AdminRole
from content_service import invalidate_content_cache
from database import (
    AIConfig,
    Base,
    Message as DBMessage,
    PaymentConfig,
    TestAttempt,
    TestQuestion,
    TestSet,
    Topic,
    TopicDialogueConfig,
    User,
    async_session_maker,
    engine,
    init_db,
)
from time_utils import msk_now


def _validate_html_strict(text: str | None, method: TelegramMethod[Any]) -> None:
    if not text:
        return
    parser = _HTMLPreviewParser()
    try:
        parser.feed(text)
        parser.finish()
    except ValueError as exc:
        raise TelegramBadRequest(method=method, message=f"Bad Request: can't parse entities: {exc}") from exc


def _extract_buttons(markup: InlineKeyboardMarkup | None) -> list[tuple[str, str | None, str | None]]:
    if not markup or not getattr(markup, "inline_keyboard", None):
        return []
    result: list[tuple[str, str | None, str | None]] = []
    for row in markup.inline_keyboard:
        for btn in row:
            result.append((btn.text, getattr(btn, "callback_data", None), getattr(btn, "url", None)))
    return result


class FakeTelegramSession(BaseSession):
    """In-memory Telegram Bot API session executing real aiogram methods with zero network I/O."""

    def __init__(self) -> None:
        super().__init__()
        self._next_message_id = 100
        self.messages: dict[int, Message] = {}
        self.events: list[dict[str, Any]] = []
        self.callback_answers: list[dict[str, Any]] = []
        self.deleted_message_ids: list[int] = []

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        if False:
            yield b""

    def _bot_user(self, bot: Bot) -> TgUser:
        return TgUser(id=bot.id, is_bot=True, first_name="Vitaliy Test Bot", username="vitaliy_test_bot")

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: Optional[int] = None,
    ) -> TelegramType:
        if isinstance(method, GetMe):
            return self._bot_user(bot)  # type: ignore[return-value]

        if isinstance(method, SendChatAction):
            self.events.append({"kind": "chat_action", "chat_id": int(method.chat_id), "action": str(method.action)})
            return True  # type: ignore[return-value]

        if isinstance(method, AnswerCallbackQuery):
            item = {
                "callback_query_id": method.callback_query_id,
                "text": method.text,
                "show_alert": bool(method.show_alert),
            }
            self.callback_answers.append(item)
            self.events.append({"kind": "callback_answer", **item})
            return True  # type: ignore[return-value]

        if isinstance(method, DeleteMessage):
            mid = int(method.message_id)
            self.deleted_message_ids.append(mid)
            self.messages.pop(mid, None)
            self.events.append({"kind": "delete_message", "chat_id": int(method.chat_id), "message_id": mid})
            return True  # type: ignore[return-value]

        if isinstance(method, SendMessage):
            parse_mode = str(method.parse_mode or "")
            if parse_mode.upper() == "HTML":
                _validate_html_strict(method.text, method)
            self._next_message_id += 1
            mid = self._next_message_id
            chat_id = int(method.chat_id)
            markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
            msg = Message(
                message_id=mid,
                date=datetime.now(),
                chat=Chat(id=chat_id, type="private"),
                from_user=self._bot_user(bot),
                text=method.text,
                reply_markup=markup,
            ).as_(bot)
            self.messages[mid] = msg
            self.events.append({
                "kind": "send_message",
                "chat_id": chat_id,
                "message_id": mid,
                "text": method.text,
                "buttons": _extract_buttons(markup),
            })
            return msg  # type: ignore[return-value]

        if isinstance(method, (SendPhoto, SendVideo, SendAnimation, SendDocument)):
            parse_mode = str(getattr(method, "parse_mode", None) or "")
            caption = getattr(method, "caption", None)
            if parse_mode.upper() == "HTML":
                _validate_html_strict(caption, method)
            self._next_message_id += 1
            mid = self._next_message_id
            chat_id = int(method.chat_id)
            markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
            extra: dict[str, Any] = {}
            if isinstance(method, SendPhoto):
                extra["photo"] = [PhotoSize(file_id="fake_photo", file_unique_id=f"u_{mid}", width=100, height=100)]
            msg = Message(
                message_id=mid,
                date=datetime.now(),
                chat=Chat(id=chat_id, type="private"),
                from_user=self._bot_user(bot),
                caption=caption,
                reply_markup=markup,
                **extra,
            ).as_(bot)
            self.messages[mid] = msg
            self.events.append({
                "kind": type(method).__name__.lower(),
                "chat_id": chat_id,
                "message_id": mid,
                "text": caption or "",
                "buttons": _extract_buttons(markup),
            })
            return msg  # type: ignore[return-value]

        if isinstance(method, SendMediaGroup):
            chat_id = int(method.chat_id)
            out: list[Message] = []
            for idx, item in enumerate(method.media):
                self._next_message_id += 1
                mid = self._next_message_id
                cap = getattr(item, "caption", None)
                msg = Message(
                    message_id=mid,
                    date=datetime.now(),
                    chat=Chat(id=chat_id, type="private"),
                    from_user=self._bot_user(bot),
                    caption=cap,
                    photo=[PhotoSize(file_id=f"mg_{idx}", file_unique_id=f"mgu_{mid}", width=100, height=100)],
                ).as_(bot)
                self.messages[mid] = msg
                out.append(msg)
            self.events.append({"kind": "send_media_group", "chat_id": chat_id, "count": len(out)})
            return out  # type: ignore[return-value]

        if isinstance(method, EditMessageText):
            parse_mode = str(method.parse_mode or "")
            if parse_mode.upper() == "HTML":
                _validate_html_strict(method.text, method)
            mid = int(method.message_id or 0)
            chat_id = int(method.chat_id or 0)
            existing = self.messages.get(mid)
            if existing is None:
                raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
            if existing.photo is not None and existing.text is None:
                raise TelegramBadRequest(method=method, message="Bad Request: there is no text in the message to edit")
            markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
            if existing.text == method.text and _extract_buttons(existing.reply_markup) == _extract_buttons(markup):
                raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")
            updated = Message(
                message_id=mid,
                date=existing.date,
                chat=Chat(id=chat_id or existing.chat.id, type="private"),
                from_user=self._bot_user(bot),
                text=method.text,
                reply_markup=markup,
            ).as_(bot)
            self.messages[mid] = updated
            self.events.append({
                "kind": "edit_text",
                "chat_id": updated.chat.id,
                "message_id": mid,
                "text": method.text,
                "buttons": _extract_buttons(markup),
            })
            return updated  # type: ignore[return-value]

        if isinstance(method, EditMessageCaption):
            mid = int(method.message_id or 0)
            existing = self.messages.get(mid)
            if existing is None:
                raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
            markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
            updated = existing.model_copy(update={"caption": method.caption, "reply_markup": markup}).as_(bot)
            self.messages[mid] = updated
            self.events.append({
                "kind": "edit_caption",
                "chat_id": existing.chat.id,
                "message_id": mid,
                "text": method.caption or "",
                "buttons": _extract_buttons(markup),
            })
            return updated  # type: ignore[return-value]

        if isinstance(method, EditMessageReplyMarkup):
            mid = int(method.message_id or 0)
            existing = self.messages.get(mid)
            if existing is None:
                raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
            markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
            if _extract_buttons(existing.reply_markup) == _extract_buttons(markup):
                raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")
            updated = existing.model_copy(update={"reply_markup": markup}).as_(bot)
            self.messages[mid] = updated
            self.events.append({
                "kind": "edit_reply_markup",
                "chat_id": existing.chat.id,
                "message_id": mid,
                "buttons": _extract_buttons(markup),
            })
            return updated  # type: ignore[return-value]

        return True  # type: ignore[return-value]


class TopicLifecycleAndSettingsE2ETests(unittest.IsolatedAsyncioTestCase):
    """End-to-end behavioral verification through real aiogram Dispatcher, FSM, and SQLite."""

    _ROUTERS = [
        funnel.router,
        test_system_handlers.router,
        topic_admin_handlers.router,
        test_set_admin_handlers.router,
        followup_admin.router,
        ai_logs_admin.router,
        automation_admin.router,
        journal_admin_handlers.router,
        journal_handlers.router,
        handlers.router,
    ]

    async def asyncSetUp(self) -> None:
        handlers._ai_start_locks.clear()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await init_db()
        invalidate_content_cache()

        for r in self._ROUTERS:
            r._parent_router = None

        self.tg = FakeTelegramSession()
        self.bot = Bot(token="123456:TEST_TOKEN", session=self.tg)
        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)
        for r in self._ROUTERS:
            self.dp.include_router(r)

        self._update_seq = 1

        async with async_session_maker() as session:
            ai_cfg = await session.get(AIConfig, 1)
            if not ai_cfg:
                session.add(AIConfig(id=1, provider="OpenAI", openai_api_key="test-key"))
            else:
                ai_cfg.openai_api_key = "test-key"
            pay_cfg = await session.get(PaymentConfig, 1)
            if pay_cfg:
                pay_cfg.mode = "free"
            await session.commit()

    async def asyncTearDown(self) -> None:
        handlers._ai_start_locks.clear()
        for r in self._ROUTERS:
            r._parent_router = None
        await self.bot.session.close()

    async def _seed_user(
        self,
        user_id: int,
        *,
        name: str = "Денис",
        is_admin: bool = False,
        admin_role: str | None = None,
        accepted_disclaimer: bool = True,
    ) -> None:
        async with async_session_maker() as session:
            session.add(
                User(
                    id=user_id,
                    first_name=name,
                    name=name,
                    gender="male",
                    age=34,
                    is_admin=is_admin,
                    admin_role=admin_role,
                    ai_disclaimer_accepted_at=msk_now() if accepted_disclaimer else None,
                    ai_disclaimer_version=1 if accepted_disclaimer else 0,
                    ai_disclaimer_shown=accepted_disclaimer,
                )
            )
            await session.commit()

    async def _state_of(self, user_id: int) -> str | None:
        key = StorageKey(bot_id=self.bot.id, chat_id=user_id, user_id=user_id)
        return await self.storage.get_state(key)

    async def _state_data(self, user_id: int) -> dict[str, Any]:
        key = StorageKey(bot_id=self.bot.id, chat_id=user_id, user_id=user_id)
        return await self.storage.get_data(key)

    def _latest_chat_message(self, chat_id: int) -> Message:
        candidates = [
            m
            for m in self.tg.messages.values()
            if m.chat.id == chat_id and m.from_user is not None and m.from_user.is_bot
        ]
        self.assertTrue(candidates, f"No active bot messages found in chat {chat_id}")
        return max(candidates, key=lambda m: m.message_id)

    async def _send_text(self, user_id: int, text: str) -> list[dict[str, Any]]:
        before = len(self.tg.events)
        self._update_seq += 1
        self.tg._next_message_id += 1
        msg = Message(
            message_id=self.tg._next_message_id,
            date=datetime.now(),
            chat=Chat(id=user_id, type="private"),
            from_user=TgUser(id=user_id, is_bot=False, first_name="User"),
            text=text,
        )
        self.tg.messages[msg.message_id] = msg.as_(self.bot)
        upd = Update(update_id=self._update_seq, message=msg)
        await self.dp.feed_update(self.bot, upd)
        return self.tg.events[before:]

    async def _click(self, user_id: int, callback_data: str, message_id: int | None = None) -> list[dict[str, Any]]:
        before = len(self.tg.events)
        self._update_seq += 1
        if message_id is not None and message_id in self.tg.messages:
            target_msg = self.tg.messages[message_id]
        else:
            matching = [
                m
                for m in self.tg.messages.values()
                if m.chat.id == user_id
                and any(cb == callback_data for _, cb, _ in _extract_buttons(m.reply_markup))
            ]
            target_msg = max(matching, key=lambda m: m.message_id) if matching else self._latest_chat_message(user_id)

        cb = CallbackQuery(
            id=f"cbq_{self._update_seq}",
            from_user=TgUser(id=user_id, is_bot=False, first_name="User"),
            chat_instance=f"ci_{user_id}",
            message=target_msg,
            data=callback_data,
        )
        upd = Update(update_id=self._update_seq, callback_query=cb)
        await self.dp.feed_update(self.bot, upd)
        return self.tg.events[before:]

    # =========================================================================
    # 1. EXPRESS (12 QUESTIONS): FULL E2E LIFECYCLE
    # =========================================================================

    async def test_express_12_questions_full_e2e_lifecycle(self):
        user_id = 700101
        await self._seed_user(user_id)

        async with async_session_maker() as session:
            profile_set = await session.scalar(select(TestSet).where(TestSet.slug == "express_psychological_profile"))
            self.assertIsNotNone(profile_set)
            for idx in range(1, 13):
                is_text_only = idx in {11, 12}
                session.add(
                    TestQuestion(
                        test_set_id=profile_set.id,
                        order_num=idx,
                        text=f"Профильный вопрос {idx}",
                        question_type="text" if is_text_only else "choice",
                        allow_custom_answer=True,
                        answer_options_json=None
                        if is_text_only
                        else json.dumps([
                            {"text": f"Вариант А{idx}", "value": 1},
                            {"text": f"Вариант Б{idx}", "value": 2},
                        ], ensure_ascii=False),
                        is_active=True,
                    )
                )
            cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "express"))
            cfg.welcome_text = "🧩 Глубокий разбор мышления\n\n12 вопросов о твоём стиле мышления."
            cfg.reset_dialogue_on_entry = True
            cfg.start_dialogue_button_enabled = True
            cfg.start_dialogue_button_text = "Начать разбор"
            cfg.prerecorded_first_message = None
            cfg.pre_dialogue_test_enabled = True
            cfg.test_set_id = profile_set.id
            cfg.summary_generation_enabled = True
            await session.commit()

        # Step 1: Open /menu -> click mode_express -> welcome + start button (no pre-test yet)
        await self._send_text(user_id, "/menu")
        await self._click(user_id, "mode_express")
        welcome_msg = self._latest_chat_message(user_id)
        self.assertIn("🧩 Глубокий разбор мышления", welcome_msg.text or "")
        w1_btns = [(t, cb) for t, cb, _ in _extract_buttons(welcome_msg.reply_markup)]
        self.assertEqual([t for t, _ in w1_btns], ["Начать разбор", "⬅️ Назад"])
        self.assertTrue(w1_btns[0][1].startswith("ai_start_dialogue:express:"))
        self.assertEqual(w1_btns[1][1], "back_to_main_menu")
        first_start_cb = w1_btns[0][1]
        first_did = int(first_start_cb.rsplit(":", 1)[1])
        self.assertIsNone(await self._state_of(user_id))
        async with async_session_maker() as session:
            self.assertEqual(await session.scalar(select(func.count(TestAttempt.id))), 0)

        # Step 2: Click Back -> returns to main menu without starting test
        await self._click(user_id, "back_to_main_menu", message_id=welcome_msg.message_id)
        menu_msg = self._latest_chat_message(user_id)
        menu_cbs = [cb for _, cb, _ in _extract_buttons(menu_msg.reply_markup)]
        self.assertIn("mode_express", menu_cbs)
        self.assertIsNone(await self._state_of(user_id))

        # Step 3: Re-enter mode_express -> allocates new dialogue_id; stale button from previous entry is rejected
        await self._click(user_id, "mode_express", message_id=menu_msg.message_id)
        welcome_msg_2 = self._latest_chat_message(user_id)
        w2_btns = [(t, cb) for t, cb, _ in _extract_buttons(welcome_msg_2.reply_markup)]
        self.assertEqual([t for t, _ in w2_btns], ["Начать разбор", "⬅️ Назад"])
        active_start_cb = w2_btns[0][1]
        active_did = int(active_start_cb.rsplit(":", 1)[1])
        self.assertGreater(active_did, first_did)

        stale_events = await self._click(user_id, first_start_cb, message_id=welcome_msg_2.message_id)
        stale_alerts = [e for e in stale_events if e["kind"] == "callback_answer" and e["show_alert"]]
        self.assertEqual(len(stale_alerts), 1)
        self.assertIn("устарела", stale_alerts[0]["text"])
        self.assertIsNone(await self._state_of(user_id))

        # Step 4: Click "Начать разбор" + immediate double-click -> welcome markup cleared, Q1 sent ONCE
        start_events = await self._click(user_id, active_start_cb, message_id=welcome_msg_2.message_id)
        self.assertIsNone(self.tg.messages[welcome_msg_2.message_id].reply_markup)
        q1_sends = [e for e in start_events if e["kind"] == "send_message" and "Вопрос 1 из 12" in e["text"]]
        self.assertEqual(len(q1_sends), 1)
        self.assertEqual(await self._state_of(user_id), "UniversalTestStates:answering")

        dup_events = await self._click(user_id, active_start_cb, message_id=welcome_msg_2.message_id)
        self.assertEqual([e for e in dup_events if e["kind"] == "send_message"], [])
        self.assertTrue(any(e["kind"] == "callback_answer" and "уже запущен" in (e["text"] or "") for e in dup_events))

        # Step 5: Answer Q1 via button -> Q2 appears -> Exit unfinished test -> Re-enter -> resumes at Q2
        q1_msg = self._latest_chat_message(user_id)
        self.assertEqual(
            [(t, cb) for t, cb, _ in _extract_buttons(q1_msg.reply_markup)],
            [
                ("Вариант А1", "test_opt_0_0"),
                ("Вариант Б1", "test_opt_0_1"),
                ("❌ Выйти", "test_exit"),
            ],
        )
        await self._click(user_id, "test_opt_0_0", message_id=q1_msg.message_id)
        self.assertIsNone(self.tg.messages[q1_msg.message_id].reply_markup)
        q2_msg = self._latest_chat_message(user_id)
        self.assertIn("Вопрос 2 из 12", q2_msg.text or "")

        await self._click(user_id, "test_exit", message_id=q2_msg.message_id)
        self.assertIsNone(await self._state_of(user_id))
        after_exit_menu = self._latest_chat_message(user_id)
        self.assertIn("mode_express", [cb for _, cb, _ in _extract_buttons(after_exit_menu.reply_markup)])

        # Re-enter while pre-test is unfinished -> immediately restores Question 2 of 12 (no reset, no welcome)
        reenter_events = await self._click(user_id, "mode_express", message_id=after_exit_menu.message_id)
        self.assertEqual(await self._state_of(user_id), "UniversalTestStates:answering")
        resumed_q2 = self._latest_chat_message(user_id)
        self.assertIn("Вопрос 2 из 12", resumed_q2.text or "")
        self.assertFalse(any("Глубокий разбор мышления" in e.get("text", "") for e in reenter_events if e["kind"] in {"send_message", "edit_text"}))

        # Step 6: Complete Q2..Q12 -> AI breakdown sent directly, NO welcome repeated, enters chatting
        ai_mock = AsyncMock(side_effect=[
            "🔋 Твой психологический портрет готов. Что откликается больше всего?",
            "Отлично, давай разберём этот сценарий шаг за шагом.",
        ])
        with patch("test_system_handlers.get_ai_response_for_topic", new=ai_mock), \
             patch("handlers.get_ai_response_for_topic", new=ai_mock):
            for idx in range(2, 11):
                cur_q = self._latest_chat_message(user_id)
                self.assertIn(f"Вопрос {idx} из 12", cur_q.text or "")
                await self._click(user_id, f"test_opt_{idx - 1}_0", message_id=cur_q.message_id)

            q11_msg = self._latest_chat_message(user_id)
            self.assertIn("Вопрос 11 из 12", q11_msg.text or "")
            await self._send_text(user_id, "Спокойный, системный, честный")

            q12_msg = self._latest_chat_message(user_id)
            self.assertIn("Вопрос 12 из 12", q12_msg.text or "")
            finish_events = await self._send_text(user_id, "Жить в согласии со своими ценностями")

            # Assert NO welcome text or Start button sent after test completion
            sent_texts_after_q12 = [e["text"] for e in finish_events if e["kind"] == "send_message"]
            self.assertFalse(any("Глубокий разбор мышления" in t for t in sent_texts_after_q12))
            self.assertTrue(any("Твой психологический портрет готов" in t for t in sent_texts_after_q12))

            portrait_msg = self._latest_chat_message(user_id)
            self.assertIn("Твой психологический портрет готов", portrait_msg.text or "")
            self.assertEqual(
                [cb for _, cb, _ in _extract_buttons(portrait_msg.reply_markup)],
                [f"ai_finish_dialogue:express:{active_did}"],
            )
            self.assertEqual(await self._state_of(user_id), "UserStates:chatting")
            sdata = await self._state_data(user_id)
            self.assertEqual(sdata.get("current_topic_slug"), "express")
            self.assertEqual(sdata.get("dialogue_id"), active_did)

            # Step 7: Working dialogue turn clears previous finish button and attaches it to new reply
            await self._send_text(user_id, "Откликается тема планирования и гибкости")
            self.assertIsNone(self.tg.messages[portrait_msg.message_id].reply_markup)
            chat_reply = self._latest_chat_message(user_id)
            self.assertIn("разберём этот сценарий", chat_reply.text or "")
            self.assertEqual(
                [cb for _, cb, _ in _extract_buttons(chat_reply.reply_markup)],
                [f"ai_finish_dialogue:express:{active_did}"],
            )

        # Step 8: Finish dialogue -> summary & new dialogue options -> New dialogue starts Q1 of next session
        await self._click(user_id, f"ai_finish_dialogue:express:{active_did}", message_id=chat_reply.message_id)
        self.assertIsNone(await self._state_of(user_id))
        completed_msg = self._latest_chat_message(user_id)
        completed_cbs = [cb for _, cb, _ in _extract_buttons(completed_msg.reply_markup)]
        self.assertIn(f"ai_gen_summary:express:{active_did}", completed_cbs)
        self.assertIn(f"ai_continue_dialogue:express:{active_did}", completed_cbs)
        self.assertIn("ai_new_dialogue:express", completed_cbs)

        new_dlg_events = await self._click(user_id, "ai_new_dialogue:express", message_id=completed_msg.message_id)
        self.assertIsNone(self.tg.messages[completed_msg.message_id].reply_markup)
        self.assertEqual(await self._state_of(user_id), "UniversalTestStates:answering")
        self.assertTrue(any(e["kind"] == "send_message" and "Вопрос 1 из 12" in e["text"] for e in new_dlg_events))

    # =========================================================================
    # 2. DIAGNOSTICS (7 QUESTIONS) & EXPLICIT ADMIN ACTIVATION / PRESERVATION
    # =========================================================================

    async def test_diagnostics_7_questions_explicit_activation_and_full_e2e_lifecycle(self):
        user_id = 700102
        admin_id = 700900
        await self._seed_user(user_id)
        await self._seed_user(admin_id, is_admin=True, admin_role=AdminRole.CONTENT_EDITOR.value)

        async with async_session_maker() as session:
            for idx in range(1, 6):
                session.add(TestQuestion(test_set_id=1, order_num=idx, text=f"Старый вопрос {idx}", is_active=True))

            ts3 = TestSet(
                id=3,
                title="Экспресс-тест блоков",
                slug="express_blocks",
                description="7 вопросов о текущем состоянии перед ИИ-сессией",
                is_enabled=True,
                is_archived=False,
                show_progress=True,
            )
            session.add(ts3)
            await session.flush()
            for idx in range(1, 8):
                session.add(
                    TestQuestion(
                        test_set_id=3,
                        order_num=idx,
                        text=f"Вопрос диагностики блоков {idx}",
                        question_type="choice",
                        answer_options_json=json.dumps([
                            {"text": f"Ответ 1.{idx}", "value": 1},
                            {"text": f"Ответ 2.{idx}", "value": 2},
                        ], ensure_ascii=False),
                        is_active=True,
                    )
                )

            diag_cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "diagnostics"))
            diag_cfg.welcome_text = "⚡ Экспресс-тест блоков\n\n7 коротких вопросов о твоём состоянии."
            diag_cfg.start_dialogue_button_enabled = True
            diag_cfg.start_dialogue_button_text = "Пройти тест"
            diag_cfg.prerecorded_first_message = None
            diag_cfg.pre_dialogue_test_enabled = False
            diag_cfg.test_set_id = 3
            diag_cfg.summary_generation_enabled = True
            await session.commit()

        # init_db() MUST NOT guess admin intent by TestSet #3 / slug / button text: pre_dialogue_test_enabled stays False!
        await init_db()
        await init_db()
        async with async_session_maker() as session:
            diag_cfg_after = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "diagnostics"))
            self.assertEqual(diag_cfg_after.test_set_id, 3)
            self.assertFalse(diag_cfg_after.pre_dialogue_test_enabled)
            self.assertTrue(diag_cfg_after.summary_generation_enabled)

        # Admin explicitly enables pre-test or selects ready TestSet #3 in UI -> pre_dialogue_test_enabled becomes True and survives init_db()
        await self._send_text(admin_id, "/admin")
        await self._click(admin_id, "cfg_set_testset_diagnostics_3")
        adm_cfg_msg = self._latest_chat_message(admin_id)
        self.assertIn("7. <b>Пре-диалоговый тест:</b> Включен", adm_cfg_msg.text or "")
        self.assertIn("7/7 вопр., готов", adm_cfg_msg.text or "")

        await init_db()
        async with async_session_maker() as session:
            diag_cfg_enabled = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "diagnostics"))
            self.assertEqual(diag_cfg_enabled.test_set_id, 3)
            self.assertTrue(diag_cfg_enabled.pre_dialogue_test_enabled)

        # Walk full client E2E flow on diagnostics
        await self._send_text(user_id, "/menu")
        await self._click(user_id, "mode_diagnostics")
        welcome_msg = self._latest_chat_message(user_id)
        self.assertIn("⚡ Экспресс-тест блоков", welcome_msg.text or "")
        diag_btns = [(t, cb) for t, cb, _ in _extract_buttons(welcome_msg.reply_markup)]
        self.assertEqual([t for t, _ in diag_btns], ["Пройти тест", "⬅️ Назад"])
        diag_start_cb = diag_btns[0][1]
        self.assertTrue(diag_start_cb.startswith("ai_start_dialogue:diagnostics:"))
        diag_did = int(diag_start_cb.rsplit(":", 1)[1])
        self.assertIsNone(await self._state_of(user_id))

        # Click "Пройти тест" -> Question 1 of 7 appears immediately (never hangs!)
        await self._click(user_id, diag_start_cb, message_id=welcome_msg.message_id)
        self.assertIsNone(self.tg.messages[welcome_msg.message_id].reply_markup)
        self.assertEqual(await self._state_of(user_id), "UniversalTestStates:answering")

        with patch(
            "test_system_handlers.get_ai_response_for_topic",
            new=AsyncMock(return_value="Главный затык сейчас — нехватка фокуса. С чего начнём?"),
        ):
            for idx in range(1, 8):
                q_msg = self._latest_chat_message(user_id)
                self.assertIn(f"Вопрос {idx} из 7", q_msg.text or "")
                events = await self._click(user_id, f"test_opt_{idx - 1}_1", message_id=q_msg.message_id)

        sent_after_q7 = [e["text"] for e in events if e["kind"] == "send_message"]
        self.assertFalse(any("Экспресс-тест блоков" in t for t in sent_after_q7))
        result_msg = self._latest_chat_message(user_id)
        self.assertIn("Главный затык сейчас", result_msg.text or "")
        self.assertEqual(
            [cb for _, cb, _ in _extract_buttons(result_msg.reply_markup)],
            [f"ai_finish_dialogue:diagnostics:{diag_did}"],
        )
        self.assertEqual(await self._state_of(user_id), "UserStates:chatting")

    async def test_admin_settings_preserved_across_manual_disable_testset_reset_and_repeat_init_db(self):
        """Targeted regression for admin config durability across repeated init_db() calls on diagnostics & express."""
        admin_id = 700910
        user_id = 700110
        await self._seed_user(admin_id, is_admin=True, admin_role=AdminRole.CONTENT_EDITOR.value)
        await self._seed_user(user_id)

        async with async_session_maker() as session:
            diag_ready_set = TestSet(
                id=40,
                title="Кастомный диагностический набор",
                slug="custom_diag_ready",
                is_enabled=True,
                is_archived=False,
            )
            session.add(diag_ready_set)
            profile_set = await session.scalar(select(TestSet).where(TestSet.slug == "express_psychological_profile"))
            await session.flush()

            # Make BOTH default TestSet #1 (7 q), custom TestSet #40 (7 q), and default profile_set (12 q) ready,
            # so if init_db() had ANY bug restoring default sets or re-enabling ready sets, it would trigger immediately!
            for idx in range(1, 8):
                session.add(TestQuestion(test_set_id=1, order_num=idx, text=f"DefaultDiagQ{idx}", is_active=True))
                session.add(TestQuestion(test_set_id=40, order_num=idx, text=f"CustomDiagQ{idx}", is_active=True))
            for idx in range(1, 13):
                session.add(TestQuestion(test_set_id=profile_set.id, order_num=idx, text=f"ProfileQ{idx}", is_active=True))

            for slug, set_id in (("diagnostics", 40), ("express", profile_set.id)):
                cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == slug))
                cfg.welcome_text = f"Кастомное приветствие {slug}"
                cfg.reset_dialogue_on_entry = False
                cfg.start_dialogue_button_enabled = True
                cfg.start_dialogue_button_text = f"Старт {slug}"
                cfg.prerecorded_first_message = f"Первое сообщение {slug}"
                cfg.pre_dialogue_test_enabled = True
                cfg.test_set_id = set_id
                cfg.summary_generation_enabled = True
                cfg.summary_prompt = f"Промпт итогов {slug}"
            await session.commit()
            profile_set_id = profile_set.id

        # 1. Manual disable of pre-test by admin -> repeated init_db() MUST NOT re-enable pre-test or overwrite settings
        await self._send_text(admin_id, "/admin")
        await self._click(admin_id, "admin_topic_dialogue_cfg_diagnostics")
        await self._click(admin_id, "cfg_toggle_pretest_diagnostics")
        self.assertIn("Выключен (набор выбран, но тест выключен)", self._latest_chat_message(admin_id).text or "")

        await self._click(admin_id, "admin_topic_dialogue_cfg_express")
        await self._click(admin_id, "cfg_toggle_pretest_express")
        self.assertIn("Выключен (набор выбран, но тест выключен)", self._latest_chat_message(admin_id).text or "")

        await init_db()
        await init_db()

        async with async_session_maker() as session:
            for slug, expected_set_id in (("diagnostics", 40), ("express", profile_set_id)):
                cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == slug))
                self.assertEqual(cfg.test_set_id, expected_set_id)
                self.assertFalse(cfg.pre_dialogue_test_enabled)
                self.assertEqual(cfg.welcome_text, f"Кастомное приветствие {slug}")
                self.assertFalse(cfg.reset_dialogue_on_entry)
                self.assertTrue(cfg.start_dialogue_button_enabled)
                self.assertEqual(cfg.start_dialogue_button_text, f"Старт {slug}")
                self.assertEqual(cfg.prerecorded_first_message, f"Первое сообщение {slug}")
                self.assertTrue(cfg.summary_generation_enabled)
                self.assertEqual(cfg.summary_prompt, f"Промпт итогов {slug}")

        # Verify runtime behavior with disabled pre-test: clicking start goes straight to chatting, zero TestAttempts
        for mode_cb, slug in (("mode_diagnostics", "diagnostics"), ("mode_express", "express")):
            await self._send_text(user_id, "/menu")
            await self._click(user_id, mode_cb)
            w_msg = self._latest_chat_message(user_id)
            start_cb = [cb for _, cb, _ in _extract_buttons(w_msg.reply_markup) if cb and cb.startswith(f"ai_start_dialogue:{slug}:")][0]
            await self._click(user_id, start_cb, message_id=w_msg.message_id)
            self.assertEqual(await self._state_of(user_id), "UserStates:chatting")
            self.assertIn(f"Первое сообщение {slug}", self._latest_chat_message(user_id).text or "")
        async with async_session_maker() as session:
            self.assertEqual(await session.scalar(select(func.count(TestAttempt.id))), 0)

        # 2. Manual TestSet reset (cfg_set_testset_*_0) -> repeated init_db() MUST NOT restore default TestSet or enable pre-test
        await self._click(admin_id, "cfg_set_testset_diagnostics_0")
        self.assertIn("8. <b>Набор тестов (TestSet):</b> Не выбран", self._latest_chat_message(admin_id).text or "")
        await self._click(admin_id, "cfg_set_testset_express_0")
        self.assertIn("8. <b>Набор тестов (TestSet):</b> Не выбран", self._latest_chat_message(admin_id).text or "")

        await init_db()
        await init_db()

        async with async_session_maker() as session:
            for slug in ("diagnostics", "express"):
                cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == slug))
                self.assertIsNone(cfg.test_set_id)
                self.assertFalse(cfg.pre_dialogue_test_enabled)
                self.assertEqual(cfg.welcome_text, f"Кастомное приветствие {slug}")
                self.assertFalse(cfg.reset_dialogue_on_entry)
                self.assertTrue(cfg.start_dialogue_button_enabled)
                self.assertEqual(cfg.start_dialogue_button_text, f"Старт {slug}")
                self.assertEqual(cfg.prerecorded_first_message, f"Первое сообщение {slug}")
                self.assertTrue(cfg.summary_generation_enabled)
                self.assertEqual(cfg.summary_prompt, f"Промпт итогов {slug}")

    # =========================================================================
    # 3. FREE_SESSION & EMPTY PRERECORDED: NO SILENT BRANCHES ON START / NEW
    # =========================================================================

    async def test_free_session_and_empty_prerecorded_no_silent_branches(self):
        user_id = 700103
        await self._seed_user(user_id)

        async with async_session_maker() as session:
            cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "free_session"))
            cfg.welcome_text = "💬 ИИ-Виталий\n\nЦифровой аватар для коучинговой сессии."
            cfg.start_dialogue_button_enabled = True
            cfg.start_dialogue_button_text = "Начать разговор"
            cfg.prerecorded_first_message = None  # Critical case: empty prerecorded_first_message!
            cfg.pre_dialogue_test_enabled = False
            cfg.summary_generation_enabled = True
            await session.commit()

        await self._send_text(user_id, "/menu")
        await self._click(user_id, "mode_free_session")
        welcome_msg = self._latest_chat_message(user_id)
        self.assertIn("💬 ИИ-Виталий", welcome_msg.text or "")
        fs_btns = [(t, cb) for t, cb, _ in _extract_buttons(welcome_msg.reply_markup)]
        self.assertEqual([t for t, _ in fs_btns], ["Начать разговор", "⬅️ Назад"])
        fs_start_cb = fs_btns[0][1]
        self.assertTrue(fs_start_cb.startswith("ai_start_dialogue:free_session:"))
        fs_did = int(fs_start_cb.rsplit(":", 1)[1])

        # Click "Начать разговор" with empty prerecorded_first_message -> MUST send non-silent invitation + finish button
        click_events = await self._click(user_id, fs_start_cb, message_id=welcome_msg.message_id)
        self.assertIsNone(self.tg.messages[welcome_msg.message_id].reply_markup)
        # Welcome message text itself was NOT overwritten or deleted
        self.assertIn("💬 ИИ-Виталий", self.tg.messages[welcome_msg.message_id].text or "")
        sent = [e for e in click_events if e["kind"] == "send_message"]
        self.assertEqual(len(sent), 1)
        self.assertIn("Напиши", sent[0]["text"])
        self.assertEqual([cb for _, cb, _ in sent[0]["buttons"]], [f"ai_finish_dialogue:free_session:{fs_did}"])
        self.assertEqual(await self._state_of(user_id), "UserStates:chatting")

        # Double-click on same start button -> idempotent, zero duplicate messages
        dup_events = await self._click(user_id, fs_start_cb, message_id=welcome_msg.message_id)
        self.assertEqual([e for e in dup_events if e["kind"] == "send_message"], [])

        # Send message -> Finish -> Click "Новый разговор" with empty prerecorded_first_message -> MUST NOT be silent
        with patch("handlers.get_ai_response_for_topic", new=AsyncMock(return_value="Слышу тебя. Что здесь самое важное?")):
            await self._send_text(user_id, "Хочу навести порядок в задачах")

        reply_msg = self._latest_chat_message(user_id)
        await self._click(user_id, f"ai_finish_dialogue:free_session:{fs_did}", message_id=reply_msg.message_id)
        comp_msg = self._latest_chat_message(user_id)
        new_events = await self._click(user_id, "ai_new_dialogue:free_session", message_id=comp_msg.message_id)
        new_sent = [e for e in new_events if e["kind"] == "send_message"]
        self.assertEqual(len(new_sent), 1)
        self.assertIn("Начинаем новый разговор", new_sent[0]["text"])
        new_finish_cb = new_sent[0]["buttons"][0][1]
        self.assertTrue(new_finish_cb.startswith("ai_finish_dialogue:free_session:"))
        self.assertGreater(int(new_finish_cb.rsplit(":", 1)[1]), fs_did)
        self.assertEqual(await self._state_of(user_id), "UserStates:chatting")

    # =========================================================================
    # 4. STRATEGY_SESSION & THEMATIC SESSIONS (INCLUDING RE-ENTRY)
    # =========================================================================

    async def test_strategy_session_and_thematic_sessions_full_e2e_lifecycle(self):
        user_id = 700104
        await self._seed_user(user_id)

        async with async_session_maker() as session:
            sleep_cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "sleep_recovery"))
            sleep_cfg.welcome_text = "🌙 Сон и восстановление. Посмотрим, что мешает отдыхать."
            sleep_cfg.reset_dialogue_on_entry = False
            sleep_cfg.start_dialogue_button_enabled = True
            sleep_cfg.start_dialogue_button_text = "Начать"
            sleep_cfg.prerecorded_first_message = "Что сейчас происходит со сном?"
            await session.commit()

        # Part A: Strategy session from main menu
        await self._send_text(user_id, "/menu")
        await self._click(user_id, "menu_strategy_session")
        self.assertEqual(await self._state_of(user_id), "FunnelStates:strategy_chat")
        strat_msg = self._latest_chat_message(user_id)
        self.assertIn("Транскоучинг", strat_msg.text or "")
        strat_cbs = [cb for _, cb, _ in _extract_buttons(strat_msg.reply_markup)]
        self.assertIn("funnel_open_landing", strat_cbs)
        self.assertIn("funnel_to_main", strat_cbs)

        with patch("funnel.get_ai_response_for_topic", new=AsyncMock(return_value="Понял твой запрос по масштабированию.")):
            await self._send_text(user_id, "Хочу выйти из операционки")
        strat_reply = self._latest_chat_message(user_id)
        self.assertIn("масштабированию", strat_reply.text or "")

        await self._click(user_id, "funnel_to_main", message_id=strat_reply.message_id)
        self.assertIsNone(await self._state_of(user_id))

        # Part B: Thematic sessions menu -> Back -> Re-open -> sleep_recovery -> Start -> Re-entry with reset=False
        await self._click(user_id, "menu_thematic_sessions")
        them_msg = self._latest_chat_message(user_id)
        them_cbs = [cb for _, cb, _ in _extract_buttons(them_msg.reply_markup)]
        self.assertIn("thematic_sleep_recovery", them_cbs)
        self.assertIn("back_to_main_menu", them_cbs)

        await self._click(user_id, "back_to_main_menu", message_id=them_msg.message_id)
        self.assertIn("menu_thematic_sessions", [cb for _, cb, _ in _extract_buttons(self._latest_chat_message(user_id).reply_markup)])

        await self._click(user_id, "menu_thematic_sessions")
        await self._click(user_id, "thematic_sleep_recovery")
        sleep_welcome = self._latest_chat_message(user_id)
        self.assertIn("🌙 Сон и восстановление", sleep_welcome.text or "")
        start_cb = [cb for _, cb, _ in _extract_buttons(sleep_welcome.reply_markup)][0]
        self.assertTrue(start_cb.startswith("ai_start_dialogue:sleep_recovery:"))

        ev1 = await self._click(user_id, start_cb, message_id=sleep_welcome.message_id)
        sent1 = [e for e in ev1 if e["kind"] == "send_message"]
        self.assertEqual(len(sent1), 1)
        self.assertEqual(sent1[0]["text"], "Что сейчас происходит со сном?")
        self.assertTrue(sent1[0]["buttons"][0][1].startswith("ai_finish_dialogue:sleep_recovery:"))

        # Exit via /menu and re-enter sleep_recovery (reset_dialogue_on_entry=False -> same dialogue_id, prerecorded already delivered)
        await self._send_text(user_id, "/menu")
        await self._click(user_id, "menu_thematic_sessions")
        await self._click(user_id, "thematic_sleep_recovery")
        sleep_welcome_2 = self._latest_chat_message(user_id)
        start_cb_2 = [cb for _, cb, _ in _extract_buttons(sleep_welcome_2.reply_markup)][0]
        self.assertEqual(start_cb_2, start_cb)

        ev2 = await self._click(user_id, start_cb_2, message_id=sleep_welcome_2.message_id)
        sent2 = [e for e in ev2 if e["kind"] == "send_message"]
        self.assertEqual(len(sent2), 1)
        self.assertIn("Продолжаем разговор", sent2[0]["text"])
        self.assertEqual(await self._state_of(user_id), "UserStates:chatting")

    # =========================================================================
    # 5. TESTSET SELECTION: UNDERSCORE SLUGS, READINESS & EXPLICIT ACTIVATION
    # =========================================================================

    async def test_testset_selection_underscore_slugs_readiness_and_clear_errors(self):
        admin_id = 700901
        await self._seed_user(admin_id, is_admin=True, admin_role=AdminRole.CONTENT_EDITOR.value)

        async with async_session_maker() as session:
            empty_set = TestSet(id=50, title="Пустой набор", slug="empty_set", is_enabled=True, is_archived=False)
            partial_diag = TestSet(id=51, title="Неполный 5", slug="partial_diag", is_enabled=True, is_archived=False)
            ready_diag = TestSet(id=52, title="Готовый 7", slug="ready_diag", is_enabled=True, is_archived=False)
            session.add_all([empty_set, partial_diag, ready_diag])
            await session.flush()
            for idx in range(1, 6):
                session.add(TestQuestion(test_set_id=51, order_num=idx, text=f"Q{idx}", is_active=True))
            for idx in range(1, 8):
                session.add(TestQuestion(test_set_id=52, order_num=idx, text=f"ReadyQ{idx}", is_active=True))
            diag_cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "diagnostics"))
            diag_cfg.pre_dialogue_test_enabled = False
            diag_cfg.test_set_id = None
            await session.commit()

        await self._send_text(admin_id, "/admin")

        # 1. Underscore slug `sleep_recovery` with empty set -> clear error, NO ValueError
        ev_empty = await self._click(admin_id, "cfg_set_testset_sleep_recovery_50")
        alerts_empty = [e for e in ev_empty if e["kind"] == "callback_answer" and e["show_alert"]]
        self.assertEqual(len(alerts_empty), 1)
        self.assertIn("не готова", alerts_empty[0]["text"])

        # 2. Underscore slug `sleep_recovery` picker + ready set (id=52) -> succeeds cleanly, no ValueError
        ev_picker = await self._click(admin_id, "cfg_pick_testset_sleep_recovery")
        self.assertTrue(any(e["kind"] == "edit_text" for e in ev_picker))
        picker_cbs = [cb for _, cb, _ in _extract_buttons(self._latest_chat_message(admin_id).reply_markup)]
        self.assertIn("cfg_set_testset_sleep_recovery_52", picker_cbs)
        ev_underscore_ok = await self._click(admin_id, "cfg_set_testset_sleep_recovery_52")
        self.assertFalse(any(e["kind"] == "callback_answer" and e["show_alert"] for e in ev_underscore_ok))
        async with async_session_maker() as session:
            sr_cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "sleep_recovery"))
            self.assertEqual(sr_cfg.test_set_id, 52)

        # Reset underscore slug `cfg_set_testset_sleep_recovery_0` -> clears test_set_id cleanly
        await self._click(admin_id, "cfg_set_testset_sleep_recovery_0")
        async with async_session_maker() as session:
            sr_cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "sleep_recovery"))
            self.assertIsNone(sr_cfg.test_set_id)

        # 3. Partial set (5/7) on diagnostics -> rejected with exact count error
        ev_partial = await self._click(admin_id, "cfg_set_testset_diagnostics_51")
        alerts_partial = [e for e in ev_partial if e["kind"] == "callback_answer" and e["show_alert"]]
        self.assertEqual(len(alerts_partial), 1)
        self.assertIn("5 из 7", alerts_partial[0]["text"])

        # 4. Ready set (7/7) on diagnostics -> explicitly enables pre_dialogue_test_enabled=True and shows state
        await self._click(admin_id, "cfg_set_testset_diagnostics_52")
        cfg_screen = self._latest_chat_message(admin_id)
        self.assertIn("7. <b>Пре-диалоговый тест:</b> Включен", cfg_screen.text or "")
        self.assertIn("Готовый 7 (ID: 52, 7/7 вопр., готов)", cfg_screen.text or "")
        async with async_session_maker() as session:
            diag_cfg = await session.scalar(select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "diagnostics"))
            self.assertEqual(diag_cfg.test_set_id, 52)
            self.assertTrue(diag_cfg.pre_dialogue_test_enabled)

    # =========================================================================
    # 6. TOPIC RENAMING (IMMUTABLE SLUG), SLUG PLACEMENT, ESCAPING & RBAC
    # =========================================================================

    async def test_topic_renaming_slug_placement_escaping_and_permissions(self):
        editor_id = 700902
        support_id = 700903
        hacker_id = 700904
        client_id = 700905
        await self._seed_user(editor_id, is_admin=True, admin_role=AdminRole.CONTENT_EDITOR.value)
        await self._seed_user(support_id, is_admin=True, admin_role=AdminRole.SUPPORT.value)
        await self._seed_user(hacker_id, is_admin=False)
        await self._seed_user(client_id, is_admin=False)

        async with async_session_maker() as session:
            session.add(Topic(slug="sleep", name="Сон и восстановление", system_prompt="Legacy prompt", is_paid=False))
            session.add(DBMessage(user_id=client_id, topic_slug="express", dialogue_id=1, role="user", content="Важная история"))
            await session.commit()

        # 1. RBAC: support & non-admin denied on topic edit & rename callbacks
        await self._send_text(support_id, "/admin")
        sup_ev = await self._click(support_id, "rename_topic_express")
        self.assertTrue(any(e["kind"] == "callback_answer" and e["show_alert"] for e in sup_ev))

        await self._send_text(hacker_id, "/menu")
        hack_ev = await self._click(hacker_id, "rename_topic_express")
        self.assertTrue(any(e["kind"] == "callback_answer" and e["show_alert"] for e in hack_ev))

        # 2. Slug placement: shown on main `edit_topic_strategy_session`, NOT in `admin_topic_dialogue_cfg_strategy_session`
        await self._send_text(editor_id, "/admin")
        await self._click(editor_id, "admin_section_ai")
        await self._click(editor_id, "admin_topics_list")
        topics_list_msg = self._latest_chat_message(editor_id)
        topics_list_cbs = [cb for _, cb, _ in _extract_buttons(topics_list_msg.reply_markup)]
        self.assertFalse(any(cb and "sync" in cb for cb in topics_list_cbs))

        await self._click(editor_id, "edit_topic_strategy_session")
        main_card = self._latest_chat_message(editor_id)
        self.assertIn("<code>strategy_session</code>", main_card.text or "")
        main_card_cbs = [cb for _, cb, _ in _extract_buttons(main_card.reply_markup)]
        self.assertIn("rename_topic_strategy_session", main_card_cbs)
        self.assertFalse(any(cb and "sync" in cb for cb in main_card_cbs))

        await self._click(editor_id, "admin_topic_dialogue_cfg_strategy_session", message_id=main_card.message_id)
        dlg_cfg_card = self._latest_chat_message(editor_id)
        self.assertIn("Параметры диалога темы: Стратегическая сессия", dlg_cfg_card.text or "")
        self.assertNotIn("strategy_session", dlg_cfg_card.text or "")

        # 3. Manual rename of Topic.name with immutable slug + HTML escaping + Cancel route + durability across init_db()
        await self._click(editor_id, "edit_topic_express", message_id=dlg_cfg_card.message_id)
        express_card = self._latest_chat_message(editor_id)
        self.assertIn("<code>express</code>", express_card.text or "")
        await self._click(editor_id, "rename_topic_express", message_id=express_card.message_id)
        self.assertEqual(await self._state_of(editor_id), "AdminStates:edit_topic_name")

        # Cancel via Back clears FSM state
        rename_prompt = self._latest_chat_message(editor_id)
        await self._click(editor_id, "edit_topic_express", message_id=rename_prompt.message_id)
        self.assertIsNone(await self._state_of(editor_id))

        # Re-open rename and save HTML-special title
        await self._click(editor_id, "rename_topic_express")
        await self._send_text(editor_id, "🧩 Разбор <AI & Мышление>")
        self.assertIsNone(await self._state_of(editor_id))
        updated_card = self._latest_chat_message(editor_id)
        self.assertIn("🧩 Разбор &lt;AI &amp; Мышление&gt;", updated_card.text or "")
        self.assertIn("<code>express</code>", updated_card.text or "")

        # Repeat init_db(): custom Topic.name, immutable slug, user history, and duplicate topics remain untouched
        await init_db()
        async with async_session_maker() as session:
            expr_db = await session.scalar(select(Topic).where(Topic.slug == "express"))
            legacy_sleep = await session.scalar(select(Topic).where(Topic.slug == "sleep"))
            hist_count = await session.scalar(
                select(func.count(DBMessage.id)).where(DBMessage.user_id == client_id, DBMessage.topic_slug == "express")
            )
            self.assertEqual(expr_db.slug, "express")
            self.assertEqual(expr_db.name, "🧩 Разбор <AI & Мышление>")
            self.assertIsNotNone(legacy_sleep)
            self.assertEqual(hist_count, 1)

    # =========================================================================
    # 7. USER & ADMIN MENU SMOKE PASS (ZERO SILENT RESPONSES)
    # =========================================================================

    async def test_smoke_all_main_user_and_admin_menu_entries_never_silent(self):
        user_id = 700106
        owner_id = 999999999
        await self._seed_user(user_id)
        await self._seed_user(owner_id, is_admin=True, admin_role=AdminRole.OWNER.value)

        user_callbacks = [
            "menu_funnel_start",
            "menu_strategy_session",
            "mode_free_session",
            "mode_diagnostics",
            "mode_express",
            "menu_thematic_sessions",
            "mode_journal",
            "mode_habits",
            "open_extended_menu",
            "menu_books",
            "menu_closed_club",
            "menu_referral",
            "menu_vitaly",
        ]
        await self._send_text(user_id, "/menu")
        for cb_data in user_callbacks:
            events = await self._click(user_id, cb_data)
            visible_effects = [
                e for e in events
                if e["kind"] in {"send_message", "edit_text", "sendphoto", "edit_caption"}
                or (e["kind"] == "callback_answer" and e.get("text"))
            ]
            self.assertTrue(visible_effects, f"User callback '{cb_data}' produced silence!")

        admin_callbacks = [
            "admin_clients_list_0",
            "admin_panel",
            "admin_section_ai",
            "admin_ai_settings",
            "admin_ai_keys",
            "admin_topics_list",
            "admin_kb_page_0",
            "admin_section_content",
            "admin_content_list",
            "admin_funnel_settings",
            "admin_habit_settings",
            "admin_journal_menu",
            "admin_test_system",
            "admin_test_sets_list",
            "admin_section_automation",
            "automation_menu",
            "admin_reminders_settings",
            "admin_followup_list",
            "admin_mailing",
            "admin_payment_settings",
            "admin_section_logs",
            "admin_logs",
            "admin_ai_logs_0_all_logs",
            "admin_manage_admins",
        ]
        await self._send_text(owner_id, "/admin")
        for cb_data in admin_callbacks:
            events = await self._click(owner_id, cb_data)
            visible_effects = [
                e for e in events
                if e["kind"] in {"send_message", "edit_text", "sendphoto", "edit_caption", "senddocument"}
                or (e["kind"] == "callback_answer" and e.get("text"))
            ]
            self.assertTrue(visible_effects, f"Admin callback '{cb_data}' produced silence!")
