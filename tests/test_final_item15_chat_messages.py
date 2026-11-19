"""End-to-end Fake Telegram behavioral suite for Item 15 of the final TZ:
Two user messages ~20 seconds apart, unanswered/in-flight overlaps, pre-disclaimer messages,
repeated identical messages, Telegram redelivery deduplication, and concurrent user messages.
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
from unittest.mock import patch

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/vitaliy_item15_e2e.db")
sys.modules.setdefault(
    "vector_store",
    SimpleNamespace(get_collection=lambda: None, get_active_path=lambda: None),
)

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
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
    SendAudio,
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
from sqlalchemy import select

import ai_integration
import ai_logs_admin
import automation_admin
import followup_admin
import funnel
import handlers
import journal_admin_handlers
import journal_handlers
import test_set_admin_handlers
import test_system_handlers
import topic_admin_handlers
from admin_html_preview import _HTMLPreviewParser
from ai_dialogue_service import (
    get_pending_ai_flow,
    get_turn_aware_history,
    reset_ai_chat_runtime_state,
)
from ai_integration import StructuredAIResponse, normalize_llm_messages
from content_service import invalidate_content_cache
from database import (
    AIConfig,
    AILog,
    Base,
    Message as DBMessage,
    PaymentConfig,
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

        if isinstance(method, SendAudio):
            self.events.append({
                "kind": "send_audio",
                "chat_id": int(method.chat_id),
                "audio": getattr(method, "audio", None),
                "title": getattr(method, "title", None),
                "performer": getattr(method, "performer", None),
                "caption": getattr(method, "caption", None),
            })
            return True  # type: ignore[return-value]

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


class Item15ChatMessagesE2ETests(unittest.IsolatedAsyncioTestCase):
    """End-to-end verification of Item 15: 20s-spaced messages, overlaps, repeats, and concurrency."""

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
        reset_ai_chat_runtime_state()
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
                ai_cfg.provider = "OpenAI"
                ai_cfg.openai_api_key = "test-key"
                ai_cfg.allow_fallback = False
            pay_cfg = await session.get(PaymentConfig, 1)
            if pay_cfg:
                pay_cfg.mode = "free"
            free_cfg = await session.scalar(
                select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == "free_session")
            )
            if free_cfg:
                free_cfg.start_dialogue_button_enabled = False
                free_cfg.pre_dialogue_test_enabled = False
            await session.commit()

    async def asyncTearDown(self) -> None:
        reset_ai_chat_runtime_state()
        handlers._ai_start_locks.clear()
        journal_handlers._quick_note_msg_locks.clear()
        journal_handlers._quick_note_handoff_locks.clear()
        for r in self._ROUTERS:
            r._parent_router = None
        await self.bot.session.close()
        await engine.dispose()

    async def _seed_user(
        self,
        user_id: int,
        *,
        name: str = "Денис",
        accepted_disclaimer: bool = True,
        balance: float = 0.0,
    ) -> None:
        async with async_session_maker() as session:
            session.add(
                User(
                    id=user_id,
                    first_name=name,
                    name=name,
                    gender="male",
                    age="34",
                    balance=balance,
                    ai_disclaimer_accepted_at=msk_now() if accepted_disclaimer else None,
                    ai_disclaimer_version=1 if accepted_disclaimer else 0,
                    ai_disclaimer_shown=accepted_disclaimer,
                )
            )
            await session.commit()

    def _fsm(self, user_id: int) -> FSMContext:
        return FSMContext(storage=self.storage, key=StorageKey(bot_id=self.bot.id, chat_id=user_id, user_id=user_id))

    def _latest_chat_message(self, chat_id: int) -> Message:
        candidates = [
            m
            for m in self.tg.messages.values()
            if m.chat.id == chat_id and m.from_user is not None and m.from_user.is_bot
        ]
        self.assertTrue(candidates, f"No active bot messages found in chat {chat_id}")
        return max(candidates, key=lambda m: m.message_id)

    def _build_text_update(
        self,
        user_id: int,
        text: str,
        *,
        message_id: int | None = None,
        msg_date: datetime | None = None,
    ) -> Update:
        self._update_seq += 1
        if message_id is None:
            self.tg._next_message_id += 1
            message_id = self.tg._next_message_id
        msg = Message(
            message_id=message_id,
            date=msg_date or datetime.now(),
            chat=Chat(id=user_id, type="private"),
            from_user=TgUser(id=user_id, is_bot=False, first_name="User"),
            text=text,
        )
        self.tg.messages[msg.message_id] = msg.as_(self.bot)
        return Update(update_id=self._update_seq, message=msg)

    async def _send_text(
        self,
        user_id: int,
        text: str,
        *,
        message_id: int | None = None,
        msg_date: datetime | None = None,
    ) -> list[dict[str, Any]]:
        before = len(self.tg.events)
        upd = self._build_text_update(user_id, text, message_id=message_id, msg_date=msg_date)
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

    async def _enter_free_session(self, user_id: int) -> None:
        await self._send_text(user_id, "/menu")
        await self._click(user_id, "mode_free_session")
        self.assertEqual(await self._fsm(user_id).get_state(), handlers.UserStates.chatting.state)

    def _bot_replies(self, chat_id: int) -> list[dict[str, Any]]:
        return [
            ev
            for ev in self.tg.events
            if ev.get("kind") == "send_message"
            and ev.get("chat_id") == chat_id
            and str(ev.get("text") or "").startswith("Ответ ")
        ]

    # =========================================================================
    # 1. TWO MESSAGES ~20 SECONDS APART (SEQUENTIAL E2E)
    # =========================================================================

    async def test_two_messages_20s_interval_sequential_e2e(self):
        user_id = 96001
        await self._seed_user(user_id)
        await self._enter_free_session(user_id)

        t0 = datetime(2026, 11, 19, 12, 0, 0)
        clock = [t0]
        llm_calls: list[list[dict[str, Any]]] = []

        async def fake_generate(ai_cfg, chat_client, model_name, messages):
            llm_calls.append([dict(m) for m in messages])
            idx = len(llm_calls)
            return StructuredAIResponse(
                text=f"Ответ {idx}: учёл «{messages[-1]['content']}»",
                raw_payload={"id": f"fake-{idx}"},
                http_status=200,
                finish_reason="stop",
            )

        msg1_text = "Первое сообщение: устал на работе и плохо сплю."
        msg2_text = "Второе сообщение через 20 секунд: ещё сильно тревожусь перед встречами."

        with patch("time_utils.msk_now", side_effect=lambda: clock[0]), \
             patch("handlers.msk_now", side_effect=lambda: clock[0]), \
             patch("ai_dialogue_service.msk_now", side_effect=lambda: clock[0]), \
             patch.object(ai_integration, "_generate_chat_response", side_effect=fake_generate):
            await self._send_text(user_id, msg1_text, msg_date=clock[0])
            clock[0] = t0 + timedelta(seconds=20)
            await self._send_text(user_id, msg2_text, msg_date=clock[0])

        self.assertEqual(len(llm_calls), 2)
        non_sys_1 = [m for m in llm_calls[0] if m["role"] != "system"]
        non_sys_2 = [m for m in llm_calls[1] if m["role"] != "system"]
        self.assertEqual(non_sys_1, [{"role": "user", "content": msg1_text}])
        self.assertEqual(
            non_sys_2,
            [
                {"role": "user", "content": msg1_text},
                {"role": "assistant", "content": f"Ответ 1: учёл «{msg1_text}»"},
                {"role": "user", "content": msg2_text},
            ],
        )

        async with async_session_maker() as session:
            db_msgs = (
                await session.execute(
                    select(DBMessage)
                    .where(DBMessage.user_id == user_id, DBMessage.topic_slug == "free_session")
                    .order_by(DBMessage.id.asc())
                )
            ).scalars().all()
            self.assertEqual(
                [(m.role, m.content) for m in db_msgs],
                [
                    ("user", msg1_text),
                    ("assistant", f"Ответ 1: учёл «{msg1_text}»"),
                    ("user", msg2_text),
                    ("assistant", f"Ответ 2: учёл «{msg2_text}»"),
                ],
            )
            logs = (
                await session.execute(
                    select(AILog).where(AILog.user_id == user_id).order_by(AILog.id.asc())
                )
            ).scalars().all()
            self.assertEqual(len(logs), 2)
            req_payload_2 = json.loads(logs[1].request_payload or "[]")
            self.assertTrue(any(m.get("role") == "user" and msg1_text in m.get("content", "") for m in req_payload_2))
            self.assertTrue(any(m.get("role") == "user" and msg2_text in m.get("content", "") for m in req_payload_2))

        replies = self._bot_replies(user_id)
        self.assertEqual(len(replies), 2)
        self.assertIsNone(self.tg.messages[replies[0]["message_id"]].reply_markup)
        self.assertIsNotNone(self.tg.messages[replies[1]["message_id"]].reply_markup)

    # =========================================================================
    # 2. SECOND MESSAGE AT +20s WHILE FIRST AI CALL IS STILL IN-FLIGHT
    # =========================================================================

    async def test_two_messages_20s_interval_while_first_ai_in_flight_e2e(self):
        user_id = 96002
        await self._seed_user(user_id)
        await self._enter_free_session(user_id)

        t0 = datetime(2026, 11, 19, 12, 0, 0)
        first_ai_started = asyncio.Event()
        release_first_ai = asyncio.Event()
        in_flight = 0
        max_in_flight = 0
        llm_calls: list[list[dict[str, Any]]] = []

        async def fake_generate(ai_cfg, chat_client, model_name, messages):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                llm_calls.append([dict(m) for m in messages])
                call_idx = len(llm_calls)
                if call_idx == 1:
                    first_ai_started.set()
                    await release_first_ai.wait()
                return StructuredAIResponse(
                    text=f"Ответ {call_idx}",
                    raw_payload={"id": f"inflight-{call_idx}"},
                    http_status=200,
                    finish_reason="stop",
                )
            finally:
                in_flight -= 1

        msg1_text = "Первое длинное сообщение"
        msg2_text = "Второе сообщение спустя 20 секунд"
        upd1 = self._build_text_update(user_id, msg1_text, msg_date=t0)
        upd2 = self._build_text_update(user_id, msg2_text, msg_date=t0 + timedelta(seconds=20))

        with patch.object(ai_integration, "_generate_chat_response", side_effect=fake_generate):
            task1 = asyncio.create_task(self.dp.feed_update(self.bot, upd1))
            await asyncio.wait_for(first_ai_started.wait(), timeout=2.0)

            # Second message arrives 20s later while first LLM call is still running
            task2 = asyncio.create_task(self.dp.feed_update(self.bot, upd2))
            await asyncio.sleep(0.05)

            # Per-user lock prevents overlapping LLM calls or DB corruption
            self.assertEqual(len(llm_calls), 1)

            release_first_ai.set()
            await asyncio.wait_for(asyncio.gather(task1, task2), timeout=3.0)

        self.assertEqual(max_in_flight, 1)
        self.assertEqual(len(llm_calls), 2)
        non_sys_2 = [m for m in llm_calls[1] if m["role"] != "system"]
        self.assertEqual(
            non_sys_2,
            [
                {"role": "user", "content": msg1_text},
                {"role": "assistant", "content": "Ответ 1"},
                {"role": "user", "content": msg2_text},
            ],
        )

        async with async_session_maker() as session:
            db_msgs = (
                await session.execute(
                    select(DBMessage)
                    .where(DBMessage.user_id == user_id, DBMessage.topic_slug == "free_session")
                    .order_by(DBMessage.id.asc())
                )
            ).scalars().all()
            self.assertEqual(
                [(m.role, m.content) for m in db_msgs],
                [
                    ("user", msg1_text),
                    ("assistant", "Ответ 1"),
                    ("user", msg2_text),
                    ("assistant", "Ответ 2"),
                ],
            )

        replies = self._bot_replies(user_id)
        self.assertEqual(len(replies), 2)
        self.assertGreaterEqual(len(self.tg.deleted_message_ids), 2)
        self.assertIsNone(self.tg.messages[replies[0]["message_id"]].reply_markup)
        self.assertIsNotNone(self.tg.messages[replies[1]["message_id"]].reply_markup)

    # =========================================================================
    # 3. UNANSWERED FIRST USER MESSAGE IN DB MERGED CLEANLY FOR SECOND LLM CALL
    # =========================================================================

    async def test_unanswered_first_message_in_db_merged_for_second_llm_turn(self):
        user_id = 96003
        await self._seed_user(user_id)
        await self._enter_free_session(user_id)

        state_data = await self._fsm(user_id).get_data()
        did = state_data["dialogue_id"]
        msg1_text = "Первое сообщение без ответа ассистента"
        msg2_text = "Второе сообщение через 20 секунд"

        async with async_session_maker() as session:
            session.add(
                DBMessage(
                    user_id=user_id,
                    dialogue_id=did,
                    topic_slug="free_session",
                    role="user",
                    content=msg1_text,
                )
            )
            await session.commit()

        llm_calls: list[list[dict[str, Any]]] = []

        async def fake_generate(ai_cfg, chat_client, model_name, messages):
            llm_calls.append([dict(m) for m in messages])
            return StructuredAIResponse(
                text="Ответ 1: ответил на оба сообщения.",
                raw_payload={"id": "merged-1"},
                http_status=200,
                finish_reason="stop",
            )

        with patch.object(ai_integration, "_generate_chat_response", side_effect=fake_generate):
            await self._send_text(user_id, msg2_text)

        self.assertEqual(len(llm_calls), 1)
        non_sys = [m for m in llm_calls[0] if m["role"] != "system"]
        self.assertEqual(len(non_sys), 1)
        self.assertEqual(non_sys[0]["role"], "user")
        self.assertIn(msg1_text, non_sys[0]["content"])
        self.assertIn(msg2_text, non_sys[0]["content"])

        # Direct check of normalize_llm_messages helper
        normalized = normalize_llm_messages([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u3"},
        ])
        self.assertEqual(
            normalized,
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u1\n\nu2"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u3"},
            ],
        )

        async with async_session_maker() as session:
            db_msgs = (
                await session.execute(
                    select(DBMessage)
                    .where(DBMessage.user_id == user_id, DBMessage.topic_slug == "free_session")
                    .order_by(DBMessage.id.asc())
                )
            ).scalars().all()
            self.assertEqual(
                [(m.role, m.content) for m in db_msgs],
                [
                    ("user", msg1_text),
                    ("user", msg2_text),
                    ("assistant", "Ответ 1: ответил на оба сообщения."),
                ],
            )

    # =========================================================================
    # 4. PRE-DISCLAIMER TWO MESSAGES PRESERVED (STRATEGY + TOPIC CHAT)
    # =========================================================================

    async def test_pre_disclaimer_two_messages_20s_apart_both_preserved(self):
        # Part A: Strategy chat before accepting AI disclaimer
        user_id = 96004
        await self._seed_user(user_id, accepted_disclaimer=False)

        await self._send_text(user_id, "/menu")
        await self._click(user_id, "menu_strategy_session")
        self.assertEqual(await self._fsm(user_id).get_state(), funnel.FunnelStates.strategy_chat.state)

        t0 = datetime(2026, 11, 19, 13, 0, 0)
        msg1_text = "Первый вопрос про стратегию"
        msg2_text = "Второй вопрос через 20 секунд"
        llm_calls: list[list[dict[str, Any]]] = []

        async def fake_generate(ai_cfg, chat_client, model_name, messages):
            llm_calls.append([dict(m) for m in messages])
            return StructuredAIResponse(
                text="Ответ после согласия",
                raw_payload={"id": "disc-1"},
                http_status=200,
                finish_reason="stop",
            )

        with patch.object(ai_integration, "_generate_chat_response", side_effect=fake_generate):
            await self._send_text(user_id, msg1_text, msg_date=t0)
            await self._send_text(user_id, msg2_text, msg_date=t0 + timedelta(seconds=20))
            self.assertEqual(len(llm_calls), 0)

            pending = await get_pending_ai_flow(user_id)
            self.assertIsNotNone(pending)
            self.assertEqual(pending.flow_type, "funnel_strategy")
            self.assertEqual(pending.payload.get("strategy_messages"), [msg1_text, msg2_text])
            self.assertEqual(pending.payload.get("strategy_message"), f"{msg1_text}\n\n{msg2_text}")

            await self._click(user_id, "cb_accept_disclaimer")

        self.assertEqual(len(llm_calls), 1)
        user_turns = [m for m in llm_calls[0] if m["role"] == "user"]
        self.assertEqual(len(user_turns), 1)
        self.assertIn(msg1_text, user_turns[0]["content"])
        self.assertIn(msg2_text, user_turns[0]["content"])
        self.assertIsNone(await get_pending_ai_flow(user_id))

        async with async_session_maker() as session:
            strat_msgs = (
                await session.execute(
                    select(DBMessage)
                    .where(DBMessage.user_id == user_id, DBMessage.topic_slug == "strategy_session")
                    .order_by(DBMessage.id.asc())
                )
            ).scalars().all()
            self.assertTrue(any(m.role == "user" and m.content == msg1_text for m in strat_msgs))
            self.assertTrue(any(m.role == "user" and m.content == msg2_text for m in strat_msgs))
            self.assertTrue(any(m.role == "assistant" and m.content == "Ответ после согласия" for m in strat_msgs))

        # Part B: Topic chat (`process_chat`) before accepting AI disclaimer
        user_id_2 = 96005
        await self._seed_user(user_id_2, accepted_disclaimer=False)
        fsm_2 = self._fsm(user_id_2)
        await fsm_2.set_state(handlers.UserStates.chatting)

        topic_m1 = "Первое сообщение в свободной теме"
        topic_m2 = "Второе сообщение в свободной теме"
        llm_calls_2: list[list[dict[str, Any]]] = []

        async def fake_generate_topic(ai_cfg, chat_client, model_name, messages):
            llm_calls_2.append([dict(m) for m in messages])
            return StructuredAIResponse(
                text="Ответ в свободной теме",
                raw_payload={"id": "disc-2"},
                http_status=200,
                finish_reason="stop",
            )

        with patch.object(ai_integration, "_generate_chat_response", side_effect=fake_generate_topic):
            await self._send_text(user_id_2, topic_m1, msg_date=t0)
            await self._send_text(user_id_2, topic_m2, msg_date=t0 + timedelta(seconds=20))
            self.assertEqual(len(llm_calls_2), 0)

            pending_2 = await get_pending_ai_flow(user_id_2)
            self.assertIsNotNone(pending_2)
            self.assertEqual(pending_2.flow_type, "topic")
            self.assertEqual(pending_2.payload.get("chat_messages"), [topic_m1, topic_m2])

            await self._click(user_id_2, "cb_accept_disclaimer")

        self.assertEqual(len(llm_calls_2), 1)
        user_turns_2 = [m for m in llm_calls_2[0] if m["role"] == "user"]
        self.assertEqual(len(user_turns_2), 1)
        self.assertIn(topic_m1, user_turns_2[0]["content"])
        self.assertIn(topic_m2, user_turns_2[0]["content"])

        async with async_session_maker() as session:
            free_msgs = (
                await session.execute(
                    select(DBMessage)
                    .where(DBMessage.user_id == user_id_2, DBMessage.topic_slug == "free_session")
                    .order_by(DBMessage.id.asc())
                )
            ).scalars().all()
            self.assertEqual(
                [(m.role, m.content) for m in free_msgs],
                [
                    ("user", topic_m1),
                    ("user", topic_m2),
                    ("assistant", "Ответ в свободной теме"),
                ],
            )

    # =========================================================================
    # 5. REPEATED MESSAGES (DISTINCT IDS) VS DUPLICATE REDELIVERY (SAME ID)
    # =========================================================================

    async def test_repeated_messages_same_text_accepted_and_duplicate_redelivery_deduplicated(self):
        # Part A: Genuine repeated message text with distinct message_ids
        user_id = 96006
        await self._seed_user(user_id)
        await self._enter_free_session(user_id)

        repeated_text = "Помоги разобраться"
        llm_calls: list[list[dict[str, Any]]] = []

        async def fake_generate(ai_cfg, chat_client, model_name, messages):
            llm_calls.append([dict(m) for m in messages])
            return StructuredAIResponse(
                text=f"Ответ {len(llm_calls)}",
                raw_payload={"id": f"rep-{len(llm_calls)}"},
                http_status=200,
                finish_reason="stop",
            )

        with patch.object(ai_integration, "_generate_chat_response", side_effect=fake_generate):
            await self._send_text(user_id, repeated_text)
            await self._send_text(user_id, repeated_text)

        self.assertEqual(len(llm_calls), 2)
        self.assertEqual(
            [m for m in llm_calls[1] if m["role"] != "system"],
            [
                {"role": "user", "content": repeated_text},
                {"role": "assistant", "content": "Ответ 1"},
                {"role": "user", "content": repeated_text},
            ],
        )

        async with async_session_maker() as session:
            db_msgs = (
                await session.execute(
                    select(DBMessage)
                    .where(DBMessage.user_id == user_id, DBMessage.topic_slug == "free_session")
                    .order_by(DBMessage.id.asc())
                )
            ).scalars().all()
            self.assertEqual(
                [(m.role, m.content) for m in db_msgs],
                [
                    ("user", repeated_text),
                    ("assistant", "Ответ 1"),
                    ("user", repeated_text),
                    ("assistant", "Ответ 2"),
                ],
            )

        # Part B: Exact duplicate Telegram redelivery (identical message_id) in token mode
        user_id_2 = 96007
        await self._seed_user(user_id_2, balance=5.0)
        async with async_session_maker() as session:
            pay_cfg = await session.get(PaymentConfig, 1)
            pay_cfg.mode = "token"
            pay_cfg.cost_per_message = 1.0
            await session.commit()

        await self._enter_free_session(user_id_2)

        dup_llm_calls: list[list[dict[str, Any]]] = []

        async def fake_generate_dup(ai_cfg, chat_client, model_name, messages):
            dup_llm_calls.append([dict(m) for m in messages])
            await asyncio.sleep(0.02)
            return StructuredAIResponse(
                text="Ответ 1",
                raw_payload={"id": "dup-1"},
                http_status=200,
                finish_reason="stop",
            )

        fixed_msg_id = 777001
        upd_a = self._build_text_update(user_id_2, "Дубликат доставки", message_id=fixed_msg_id)
        upd_b = self._build_text_update(user_id_2, "Дубликат доставки", message_id=fixed_msg_id)
        upd_c = self._build_text_update(user_id_2, "Дубликат доставки", message_id=fixed_msg_id)

        with patch.object(ai_integration, "_generate_chat_response", side_effect=fake_generate_dup):
            await asyncio.gather(
                self.dp.feed_update(self.bot, upd_a),
                self.dp.feed_update(self.bot, upd_b),
            )
            await self.dp.feed_update(self.bot, upd_c)

        self.assertEqual(len(dup_llm_calls), 1)
        async with async_session_maker() as session:
            u2 = await session.get(User, user_id_2)
            self.assertEqual(u2.balance, 4.0)
            dup_msgs = (
                await session.execute(
                    select(DBMessage)
                    .where(DBMessage.user_id == user_id_2, DBMessage.topic_slug == "free_session")
                    .order_by(DBMessage.id.asc())
                )
            ).scalars().all()
            self.assertEqual(
                [(m.role, m.content) for m in dup_msgs],
                [("user", "Дубликат доставки"), ("assistant", "Ответ 1")],
            )

    # =========================================================================
    # 6. CONCURRENT MESSAGES: SAME USER ATOMIC TOKENS & CROSS-USER PARALLELISM
    # =========================================================================

    async def test_concurrent_messages_same_user_atomic_tokens_and_cross_user_parallelism(self):
        # Part A: Same user with balance=2.0, cost=1.0 sends 3 simultaneous messages
        user_id = 96008
        await self._seed_user(user_id, balance=2.0)
        async with async_session_maker() as session:
            pay_cfg = await session.get(PaymentConfig, 1)
            pay_cfg.mode = "token"
            pay_cfg.cost_per_message = 1.0
            pay_cfg.low_balance_threshold = 0.0
            await session.commit()

        await self._enter_free_session(user_id)

        in_flight = 0
        max_in_flight = 0
        llm_calls: list[list[dict[str, Any]]] = []

        async def fake_generate(ai_cfg, chat_client, model_name, messages):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                llm_calls.append([dict(m) for m in messages])
                await asyncio.sleep(0.02)
                return StructuredAIResponse(
                    text=f"Ответ {len(llm_calls)}",
                    raw_payload={"id": f"tok-{len(llm_calls)}"},
                    http_status=200,
                    finish_reason="stop",
                )
            finally:
                in_flight -= 1

        upds = [
            self._build_text_update(user_id, "Первое"),
            self._build_text_update(user_id, "Второе"),
            self._build_text_update(user_id, "Третье"),
        ]

        with patch.object(ai_integration, "_generate_chat_response", side_effect=fake_generate):
            await asyncio.gather(*(self.dp.feed_update(self.bot, u) for u in upds))

        self.assertEqual(max_in_flight, 1)
        self.assertEqual(len(llm_calls), 2)
        first_msg = llm_calls[0][-1]["content"]
        second_msg = llm_calls[1][-1]["content"]
        self.assertNotEqual(first_msg, second_msg)
        self.assertTrue({first_msg, second_msg}.issubset({"Первое", "Второе", "Третье"}))
        non_sys_2 = [m for m in llm_calls[1] if m["role"] != "system"]
        self.assertEqual(
            non_sys_2,
            [
                {"role": "user", "content": first_msg},
                {"role": "assistant", "content": "Ответ 1"},
                {"role": "user", "content": second_msg},
            ],
        )

        async with async_session_maker() as session:
            u = await session.get(User, user_id)
            self.assertEqual(u.balance, 0.0)
            db_msgs = (
                await session.execute(
                    select(DBMessage)
                    .where(DBMessage.user_id == user_id, DBMessage.topic_slug == "free_session")
                    .order_by(DBMessage.id.asc())
                )
            ).scalars().all()
            self.assertEqual(
                [(m.role, m.content) for m in db_msgs],
                [
                    ("user", first_msg),
                    ("assistant", "Ответ 1"),
                    ("user", second_msg),
                    ("assistant", "Ответ 2"),
                ],
            )

        insufficient_alerts = [
            ev
            for ev in self.tg.events
            if ev.get("kind") == "send_message"
            and ev.get("chat_id") == user_id
            and "Недостаточно токенов" in str(ev.get("text") or "")
        ]
        self.assertEqual(len(insufficient_alerts), 1)

        # Part B: Different users process concurrently without global lock contention
        async with async_session_maker() as session:
            pay_cfg = await session.get(PaymentConfig, 1)
            pay_cfg.mode = "free"
            await session.commit()

        user_a = 96011
        user_b = 96012
        await self._seed_user(user_a, name="Алиса")
        await self._seed_user(user_b, name="Борис")
        await self._enter_free_session(user_a)
        await self._enter_free_session(user_b)

        user_a_started = asyncio.Event()
        user_b_entered = asyncio.Event()

        async def fake_cross_user_generate(ai_cfg, chat_client, model_name, messages):
            prompt_text = messages[-1]["content"]
            if prompt_text == "От Алисы":
                user_a_started.set()
                await asyncio.wait_for(user_b_entered.wait(), timeout=2.0)
                return StructuredAIResponse(text="Ответ Алисе", raw_payload={}, http_status=200, finish_reason="stop")
            user_b_entered.set()
            return StructuredAIResponse(text="Ответ Борису", raw_payload={}, http_status=200, finish_reason="stop")

        upd_alice = self._build_text_update(user_a, "От Алисы")
        upd_bob = self._build_text_update(user_b, "От Бориса")

        with patch.object(ai_integration, "_generate_chat_response", side_effect=fake_cross_user_generate):
            task_a = asyncio.create_task(self.dp.feed_update(self.bot, upd_alice))
            await asyncio.wait_for(user_a_started.wait(), timeout=2.0)
            task_b = asyncio.create_task(self.dp.feed_update(self.bot, upd_bob))
            await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=3.0)

        self.assertTrue(user_b_entered.is_set())

    # =========================================================================
    # 7. STRATEGY & OBJECTION CHATS: CONCURRENT MESSAGES IN SINGLE DIALOGUE
    # =========================================================================

    async def test_strategy_and_objection_concurrent_messages_single_dialogue_order(self):
        # Part A: strategy_chat
        user_id = 96009
        await self._seed_user(user_id)
        await self._send_text(user_id, "/menu")
        await self._click(user_id, "menu_strategy_session")
        self.assertEqual(await self._fsm(user_id).get_state(), funnel.FunnelStates.strategy_chat.state)

        strat_calls: list[list[dict[str, Any]]] = []

        async def fake_strat_generate(ai_cfg, chat_client, model_name, messages):
            strat_calls.append([dict(m) for m in messages])
            await asyncio.sleep(0.02)
            return StructuredAIResponse(
                text=f"Стратегия ответ {len(strat_calls)}",
                raw_payload={"id": f"st-{len(strat_calls)}"},
                http_status=200,
                finish_reason="stop",
            )

        s_u1 = self._build_text_update(user_id, "Стратегия 1")
        s_u2 = self._build_text_update(user_id, "Стратегия 2")
        with patch.object(ai_integration, "_generate_chat_response", side_effect=fake_strat_generate):
            await asyncio.gather(
                self.dp.feed_update(self.bot, s_u1),
                self.dp.feed_update(self.bot, s_u2),
            )

        self.assertEqual(len(strat_calls), 2)
        first_s = strat_calls[0][-1]["content"]
        second_s = strat_calls[1][-1]["content"]
        self.assertEqual({first_s, second_s}, {"Стратегия 1", "Стратегия 2"})
        non_sys_s2 = [m for m in strat_calls[1] if m["role"] != "system"]
        self.assertEqual(
            [(m["role"], m["content"]) for m in non_sys_s2[-3:]],
            [
                ("user", first_s),
                ("assistant", "Стратегия ответ 1"),
                ("user", second_s),
            ],
        )

        async with async_session_maker() as session:
            strat_db = (
                await session.execute(
                    select(DBMessage)
                    .where(DBMessage.user_id == user_id, DBMessage.topic_slug == "strategy_session")
                    .order_by(DBMessage.id.asc())
                )
            ).scalars().all()
            self.assertEqual(len({m.dialogue_id for m in strat_db}), 1)
            self.assertEqual(
                [(m.role, m.content) for m in strat_db[1:]],
                [
                    ("user", first_s),
                    ("assistant", "Стратегия ответ 1"),
                    ("user", second_s),
                    ("assistant", "Стратегия ответ 2"),
                ],
            )

        # Part B: objection_chat
        user_id_2 = 96010
        await self._seed_user(user_id_2)
        fsm_2 = self._fsm(user_id_2)
        dummy_target = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id_2, first_name="User"),
            chat=SimpleNamespace(id=user_id_2),
            bot=self.bot,
        )
        await funnel.start_objection_dialogue(dummy_target, fsm_2, bot=self.bot)
        self.assertEqual(await fsm_2.get_state(), funnel.FunnelStates.objection_chat.state)

        obj_calls: list[list[dict[str, Any]]] = []

        async def fake_obj_generate(ai_cfg, chat_client, model_name, messages):
            obj_calls.append([dict(m) for m in messages])
            await asyncio.sleep(0.02)
            return StructuredAIResponse(
                text=f"Возражение ответ {len(obj_calls)}",
                raw_payload={"id": f"ob-{len(obj_calls)}"},
                http_status=200,
                finish_reason="stop",
            )

        o_u1 = self._build_text_update(user_id_2, "Возражение 1")
        o_u2 = self._build_text_update(user_id_2, "Возражение 2")
        with patch.object(ai_integration, "_generate_chat_response", side_effect=fake_obj_generate):
            await asyncio.gather(
                self.dp.feed_update(self.bot, o_u1),
                self.dp.feed_update(self.bot, o_u2),
            )

        self.assertEqual(len(obj_calls), 2)
        first_o = obj_calls[0][-1]["content"]
        second_o = obj_calls[1][-1]["content"]
        self.assertEqual({first_o, second_o}, {"Возражение 1", "Возражение 2"})
        non_sys_o2 = [m for m in obj_calls[1] if m["role"] != "system"]
        self.assertEqual(
            [(m["role"], m["content"]) for m in non_sys_o2[-3:]],
            [
                ("user", first_o),
                ("assistant", "Возражение ответ 1"),
                ("user", second_o),
            ],
        )

        async with async_session_maker() as session:
            obj_db = (
                await session.execute(
                    select(DBMessage)
                    .where(DBMessage.user_id == user_id_2, DBMessage.topic_slug == "strategy_objections")
                    .order_by(DBMessage.id.asc())
                )
            ).scalars().all()
            self.assertEqual(len({m.dialogue_id for m in obj_db}), 1)
            self.assertEqual(
                [(m.role, m.content) for m in obj_db[1:]],
                [
                    ("user", first_o),
                    ("assistant", "Возражение ответ 1"),
                    ("user", second_o),
                    ("assistant", "Возражение ответ 2"),
                ],
            )

    # =========================================================================
    # 8. HISTORY BOUNDARY INVARIANT: STRICTLY EXCLUDES CURRENT AND FUTURE IDS
    # =========================================================================

    async def test_get_turn_aware_history_strictly_excludes_current_and_future_ids(self):
        user_id = 96013
        topic_slug = "free_session"
        dialogue_id = 1
        await self._seed_user(user_id)

        async with async_session_maker() as session:
            m1 = DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="user", content="M1")
            a1 = DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="assistant", content="A1")
            m2 = DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="user", content="M2")
            m3 = DBMessage(user_id=user_id, topic_slug=topic_slug, dialogue_id=dialogue_id, role="user", content="M3")
            for row in (m1, a1, m2, m3):
                session.add(row)
                await session.flush()
            await session.commit()

            history = await get_turn_aware_history(
                session,
                user_id,
                topic_slug,
                dialogue_id,
                exclude_message_id=m2.id,
            )
            self.assertEqual([m.id for m in history], [m1.id, a1.id])
