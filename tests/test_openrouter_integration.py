import asyncio
import datetime
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
from sqlalchemy import create_engine, delete, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from database import (
    AIConfig,
    AILog,
    Base,
    JournalAiOperation,
    JournalEntry,
    PaymentConfig,
    Topic,
    User,
    async_session_maker,
    init_db,
    _ensure_sqlite_compat_schema,
)
from ai_integration import (
    DEFAULT_PROVIDER_MODELS,
    CANONICAL_PROVIDERS,
    StructuredAIResponse,
    _prepare_clients,
    _prepare_fallback_client,
    get_ai_response_for_topic,
    get_standalone_ai_response,
    generate_journal_daily_summary,
    generate_journal_dynamics_analysis,
    normalize_provider,
)
from journal_service import finalize_journal_ai_operation_failure
from openrouter_models import (
    OPENROUTER_DEFAULT_MODEL,
    OPENROUTER_MODELS,
    OPENROUTER_MODEL_SPECS,
    OpenRouterModelError,
    get_openrouter_model_by_index,
    get_openrouter_model_index,
    is_valid_openrouter_model,
    resolve_openrouter_fallback_model,
    resolve_openrouter_model,
)
from error_reporting import clear_captured_test_alerts, get_captured_test_alerts
import keyboards as kb
import handlers


def _make_mock_cb(user_id: int, callback_data: str, bot=None) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = callback_data
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="Admin", username="admin_test")
    cb.message = MagicMock(spec=Message)
    cb.message.chat = Chat(id=user_id, type="private")
    cb.message.message_id = 999
    cb.message.text = "Menu"
    cb.message.content_type = "text"
    cb.message.edit_text = AsyncMock()
    cb.message.delete = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    if bot:
        cb.bot = bot
        cb.message.bot = bot
    else:
        cb.bot = MagicMock()
        cb.bot.send_message = AsyncMock()
        cb.message.bot = cb.bot
    return cb


def _make_mock_msg(user_id: int, text: str, bot=None) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="Admin", username="admin_test")
    msg.chat = Chat(id=user_id, type="private")
    msg.message_id = 1001
    msg.delete = AsyncMock()
    msg.answer = AsyncMock()
    if bot:
        msg.bot = bot
    else:
        msg.bot = MagicMock()
        msg.bot.send_message = AsyncMock()
    return msg


class TestOpenRouterCatalogAndNormalization(unittest.TestCase):
    def test_canonical_providers_and_normalization(self):
        self.assertIn("openrouter", CANONICAL_PROVIDERS)
        self.assertEqual(CANONICAL_PROVIDERS["openrouter"], "OpenRouter")
        self.assertEqual(normalize_provider("openrouter"), "OpenRouter")
        self.assertEqual(normalize_provider("OpenRouter"), "OpenRouter")
        self.assertEqual(normalize_provider(" OPENROUTER "), "OpenRouter")
        self.assertEqual(normalize_provider("OpenRouter\n"), "OpenRouter")

    def test_default_model_registry(self):
        self.assertIn("OpenRouter", DEFAULT_PROVIDER_MODELS)
        self.assertEqual(DEFAULT_PROVIDER_MODELS["OpenRouter"], "openai/gpt-5.6-terra")
        self.assertEqual(OPENROUTER_DEFAULT_MODEL, "openai/gpt-5.6-terra")

    def test_exact_15_curated_models(self):
        expected_models = (
            "openai/gpt-5.6-terra",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-luna-pro",
            "google/gemini-3.7-flash",
            "google/gemini-3.8-flash",
            "google/gemini-3.1-pro-preview",
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-opus-4.6",
            "anthropic/claude-haiku-4.5",
            "x-ai/grok-4.7",
            "x-ai/grok-4.6",
            "deepseek/deepseek-v4.1-flash",
            "qwen/qwen3-vl-235b-a22b-instruct",
            "moonshotai/kimi-k2.6",
            "mistralai/mistral-medium-3-5",
        )
        self.assertEqual(len(OPENROUTER_MODELS), 15)
        self.assertEqual(OPENROUTER_MODELS, expected_models)
        self.assertEqual(len(OPENROUTER_MODEL_SPECS), 15)
        for m in expected_models:
            self.assertIn(m, OPENROUTER_MODEL_SPECS)
            self.assertTrue(OPENROUTER_MODEL_SPECS[m].text)
            self.assertTrue(is_valid_openrouter_model(m))

    def test_strict_model_resolution_semantics(self):
        # 1. None / empty / whitespace -> default
        self.assertEqual(resolve_openrouter_model(None), "openai/gpt-5.6-terra")
        self.assertEqual(resolve_openrouter_model(""), "openai/gpt-5.6-terra")
        self.assertEqual(resolve_openrouter_model("   "), "openai/gpt-5.6-terra")
        self.assertEqual(resolve_openrouter_model("\t\n"), "openai/gpt-5.6-terra")

        # 2. Valid model -> exact model
        self.assertEqual(resolve_openrouter_model("google/gemini-3.8-flash"), "google/gemini-3.8-flash")
        self.assertEqual(resolve_openrouter_model(" anthropic/claude-sonnet-4.6 "), "anthropic/claude-sonnet-4.6")

        # 3. Invalid / crafted model -> OpenRouterModelError (never silently default)
        with self.assertRaises(OpenRouterModelError):
            resolve_openrouter_model("evil/provider-model")
        with self.assertRaises(OpenRouterModelError):
            resolve_openrouter_model("gpt-4o")
        with self.assertRaises(OpenRouterModelError):
            resolve_openrouter_model("claude-3-opus")

    def test_strict_fallback_model_resolution_semantics(self):
        # A. fallback_model non-empty and valid
        self.assertEqual(
            resolve_openrouter_fallback_model("anthropic/claude-opus-4.6", "openai/gpt-5.6-sol"),
            "anthropic/claude-opus-4.6",
        )
        # A. fallback_model non-empty and invalid -> Error
        with self.assertRaises(OpenRouterModelError):
            resolve_openrouter_fallback_model("crafted/unknown", "openai/gpt-5.6-sol")

        # B. fallback_model empty, primary valid
        self.assertEqual(
            resolve_openrouter_fallback_model("", "google/gemini-3.7-flash"),
            "google/gemini-3.7-flash",
        )
        self.assertEqual(
            resolve_openrouter_fallback_model(None, "x-ai/grok-4.7"),
            "x-ai/grok-4.7",
        )
        # B. fallback_model empty, primary invalid -> Error
        with self.assertRaises(OpenRouterModelError):
            resolve_openrouter_fallback_model("", "crafted/primary")

        # C. both empty -> default
        self.assertEqual(
            resolve_openrouter_fallback_model("", ""),
            "openai/gpt-5.6-terra",
        )
        self.assertEqual(
            resolve_openrouter_fallback_model(None, None),
            "openai/gpt-5.6-terra",
        )

    def test_index_lookup_and_reverse_lookup(self):
        self.assertEqual(get_openrouter_model_by_index(0), "openai/gpt-5.6-terra")
        self.assertEqual(get_openrouter_model_by_index(14), "mistralai/mistral-medium-3-5")
        self.assertIsNone(get_openrouter_model_by_index(-1))
        self.assertIsNone(get_openrouter_model_by_index(15))
        self.assertIsNone(get_openrouter_model_by_index(999))

        self.assertEqual(get_openrouter_model_index("openai/gpt-5.6-terra"), 0)
        self.assertEqual(get_openrouter_model_index("mistralai/mistral-medium-3-5"), 14)
        self.assertIsNone(get_openrouter_model_index("nonexistent/model"))

    def test_keyboard_callback_length_contract(self):
        markup = kb.openrouter_models_keyboard("google/gemini-3.7-flash")
        for row in markup.inline_keyboard:
            for btn in row:
                cb_bytes = btn.callback_data.encode("utf-8")
                self.assertLessEqual(
                    len(cb_bytes),
                    64,
                    f"Callback data '{btn.callback_data}' exceeds 64 bytes ({len(cb_bytes)} bytes)",
                )
        # Verify checkmark on selected model
        gemini_btn = next(btn for row in markup.inline_keyboard for btn in row if "Gemini 3.7" in btn.text)
        self.assertTrue(gemini_btn.text.startswith("✅"))


class TestOpenRouterDatabaseAndMigration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()

    async def test_fresh_schema_has_openrouter_columns_without_db_default(self):
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            if not conf:
                conf = AIConfig(id=1, provider="OpenAI")
                session.add(conf)
                await session.commit()

            # Verify columns exist on the model
            self.assertTrue(hasattr(conf, "openrouter_api_key"))
            self.assertTrue(hasattr(conf, "openrouter_model"))

            # Schema inspection: Column must NOT have server default
            model_cols = {c.name: c for c in AIConfig.__table__.columns}
            self.assertIn("openrouter_api_key", model_cols)
            self.assertIn("openrouter_model", model_cols)
            self.assertTrue(model_cols["openrouter_api_key"].nullable)
            self.assertTrue(model_cols["openrouter_model"].nullable)
            self.assertIsNone(model_cols["openrouter_api_key"].default)
            self.assertIsNone(model_cols["openrouter_model"].default)

    async def test_sqlite_compat_migration_adds_openrouter_columns_safely(self):
        """Simulate pre-OpenRouter SQLite database and verify generic migration adds columns safely without data loss."""
        test_db_path = "/tmp/test_pre_openrouter_migration.db"
        if os.path.exists(test_db_path):
            os.remove(test_db_path)

        sync_engine = create_engine(f"sqlite:///{test_db_path}")
        with sync_engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE ai_config (
                    id INTEGER PRIMARY KEY,
                    provider VARCHAR DEFAULT 'OpenAI',
                    openai_api_key VARCHAR,
                    openai_model VARCHAR DEFAULT 'gpt-4o',
                    deepseek_api_key VARCHAR,
                    deepseek_model VARCHAR DEFAULT 'deepseek-chat',
                    kie_api_key VARCHAR,
                    kie_model VARCHAR DEFAULT 'gemini-3-flash'
                )
            """))
            conn.execute(text("""
                INSERT INTO ai_config (id, provider, openai_api_key, openai_model, deepseek_api_key)
                VALUES (1, 'DeepSeek', 'sk-existing-openai', 'gpt-4o', 'sk-existing-ds')
            """))
            conn.commit()

        # Run async migration
        async_eng = create_async_engine(f"sqlite+aiosqlite:///{test_db_path}")
        async with async_eng.begin() as conn:
            await _ensure_sqlite_compat_schema(conn)

        # Inspect resulting columns and verify data preservation
        with sync_engine.connect() as conn:
            columns_info = conn.execute(text("PRAGMA table_info(ai_config)")).fetchall()
            col_names = {row[1] for row in columns_info}
            self.assertIn("openrouter_api_key", col_names)
            self.assertIn("openrouter_model", col_names)

            row = conn.execute(text("SELECT provider, openai_api_key, deepseek_api_key, openrouter_api_key, openrouter_model FROM ai_config WHERE id=1")).fetchone()
            self.assertEqual(row[0], "DeepSeek")
            self.assertEqual(row[1], "sk-existing-openai")
            self.assertEqual(row[2], "sk-existing-ds")
            self.assertIsNone(row[3])
            self.assertIsNone(row[4])

        await async_eng.dispose()
        if os.path.exists(test_db_path):
            os.remove(test_db_path)


class TestOpenRouterAdminUX(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.admin_id = 998877
        self.storage = MemoryStorage()
        clear_captured_test_alerts()

        async with async_session_maker() as session:
            admin_user = await session.get(User, self.admin_id)
            if not admin_user:
                admin_user = User(id=self.admin_id, first_name="Admin", is_admin=True)
                session.add(admin_user)
            else:
                admin_user.is_admin = True
            conf = await session.get(AIConfig, 1)
            if not conf:
                conf = AIConfig(id=1, provider="OpenAI", openrouter_api_key=None)
                session.add(conf)
            else:
                conf.provider = "OpenAI"
                conf.openrouter_api_key = None
            await session.commit()

    def _get_fsm(self) -> FSMContext:
        return FSMContext(
            storage=self.storage,
            key=StorageKey(bot_id=12345, chat_id=self.admin_id, user_id=self.admin_id),
        )

    async def test_ai_settings_renders_openrouter_selector_and_status(self):
        cb = _make_mock_cb(self.admin_id, "admin_ai_settings")
        await handlers.admin_ai_settings(cb)

        self.assertTrue(cb.message.edit_text.called or cb.message.answer.called)
        call_args = cb.message.edit_text.call_args or cb.message.answer.call_args
        text_content = call_args[0][0] if call_args[0] else call_args[1].get("text", "")
        self.assertIn("OpenRouter", text_content)
        self.assertIn("❌ Не установлен", text_content)

        reply_markup = call_args[1].get("reply_markup") or (call_args[0][1] if len(call_args[0]) > 1 else None)
        btn_cbs = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
        self.assertIn("ai_provider_OpenRouter", btn_cbs)

    async def test_switch_provider_to_openrouter(self):
        cb = _make_mock_cb(self.admin_id, "ai_provider_OpenRouter")
        await handlers.set_ai_provider(cb, cb.bot)

        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            self.assertEqual(conf.provider, "OpenRouter")

    async def test_key_editing_lifecycle_and_security(self):
        state = self._get_fsm()
        cb = _make_mock_cb(self.admin_id, "set_openrouter_apikey")
        await handlers.set_openrouter_apikey(cb, state)

        current_state = await state.get_state()
        self.assertEqual(current_state, handlers.AdminStates.set_openrouter_key.state)

        # Admin sends message with key
        raw_key = "  sk-or-v1-0123456789abcdef  "
        clean_key = "sk-or-v1-0123456789abcdef"
        msg = _make_mock_msg(self.admin_id, raw_key)
        await handlers.save_openrouter_key(msg, state, msg.bot)

        # 1. Secret message must be deleted
        self.assertTrue(msg.delete.called)

        # 2. State must be cleared
        self.assertIsNone(await state.get_state())

        # 3. Key stripped and saved in DB
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            self.assertEqual(conf.openrouter_api_key, clean_key)

        # 4. Telegram response/edit non-echo verification
        # Test subsequent menu refreshes with real handlers
        cb_settings = _make_mock_cb(self.admin_id, "admin_ai_settings", bot=msg.bot)
        await handlers.admin_ai_settings(cb_settings)

        cb_keys = _make_mock_cb(self.admin_id, "admin_ai_keys", bot=msg.bot)
        await handlers.admin_ai_keys(cb_keys)

        # Collect every call and argument made across bot and messages
        all_rendered_content = []
        for mock_obj in [msg.bot, cb.message, cb_settings.message, cb_keys.message, msg]:
            for call in mock_obj.mock_calls:
                all_rendered_content.append(str(call))

        for rendered in all_rendered_content:
            self.assertNotIn(clean_key, rendered, f"Secret key leaked in Telegram call: {rendered}")
            self.assertNotIn(raw_key.strip(), rendered, f"Secret key leaked in Telegram call: {rendered}")

    async def test_model_selection_and_crafted_rejection(self):
        state = self._get_fsm()
        cb = _make_mock_cb(self.admin_id, "set_model_openrouter")
        await handlers.set_model_openrouter_start(cb, state)

        # 1. Valid model selection (index 4 = google/gemini-3.8-flash)
        cb_save = _make_mock_cb(self.admin_id, "or_mod_4")
        await handlers.save_openrouter_model_cb(cb_save, cb_save.bot)

        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            self.assertEqual(conf.openrouter_model, "google/gemini-3.8-flash")

        # 2. Crafted out-of-range index rejected without mutating DB
        cb_crafted1 = _make_mock_cb(self.admin_id, "or_mod_-1")
        await handlers.save_openrouter_model_cb(cb_crafted1, cb_crafted1.bot)
        self.assertTrue(cb_crafted1.answer.called)

        cb_crafted2 = _make_mock_cb(self.admin_id, "or_mod_999")
        await handlers.save_openrouter_model_cb(cb_crafted2, cb_crafted2.bot)
        self.assertTrue(cb_crafted2.answer.called)

        cb_crafted3 = _make_mock_cb(self.admin_id, "or_mod_evil")
        await handlers.save_openrouter_model_cb(cb_crafted3, cb_crafted3.bot)
        self.assertTrue(cb_crafted3.answer.called)

        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            self.assertEqual(conf.openrouter_model, "google/gemini-3.8-flash")


class TestOpenRouterRuntimeExecution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.user_id = 771122
        clear_captured_test_alerts()
        async with async_session_maker() as session:
            user = await session.get(User, self.user_id)
            if not user:
                user = User(id=self.user_id, first_name="TestUser", username="testuser")
                session.add(user)
            await session.commit()

    async def test_missing_openrouter_key_fails_before_network(self):
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenRouter"
            conf.openrouter_api_key = None
            conf.allow_fallback = False
            await session.commit()

            cfg, emb, chat, model, err = await _prepare_clients(session)
            self.assertIsNotNone(err)
            self.assertIn("API ключ не установлен", err)
            self.assertIsNone(chat)

    async def test_invalid_configured_model_fails_before_network(self):
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenRouter"
            conf.openrouter_api_key = "sk-valid-openrouter-key"
            conf.openrouter_model = "crafted/malicious-model"
            await session.commit()

            cfg, emb, chat, model, err = await _prepare_clients(session)
            self.assertIsNotNone(err)
            self.assertIn("Ошибка конфигурации OpenRouter", err)
            self.assertIsNone(chat)

    async def test_openrouter_primary_successful_chat_execution_and_logging(self):
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenRouter"
            conf.openrouter_api_key = "sk-openrouter-secret-key"
            conf.openrouter_model = "openai/gpt-5.6-terra"
            conf.allow_fallback = False
            await session.commit()

        mock_choice = SimpleNamespace(
            message=SimpleNamespace(content="Здравствуйте! Чем могу помочь?"),
            finish_reason="stop",
        )
        mock_resp = SimpleNamespace(
            choices=[mock_choice],
            model_dump=lambda: {"id": "gen-123", "choices": [{"finish_reason": "stop"}]},
        )

        with patch("openai.resources.chat.completions.AsyncCompletions.create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_resp

            answer = await get_ai_response_for_topic(
                user_id=self.user_id,
                user_prompt="Привет!",
                user_name="Тест",
            )

            self.assertIn("Здравствуйте!", answer)
            mock_create.assert_called_once()
            call_kwargs = mock_create.call_args.kwargs
            self.assertEqual(call_kwargs["model"], "openai/gpt-5.6-terra")

        # Verify AILog
        async with async_session_maker() as session:
            log = (await session.scalars(
                select(AILog).where(AILog.user_id == self.user_id).order_by(AILog.id.desc())
            )).first()
            self.assertIsNotNone(log)
            self.assertEqual(log.provider, "OpenRouter")
            self.assertEqual(log.model, "openai/gpt-5.6-terra")
            self.assertEqual(log.status, "success")
            self.assertEqual(log.attempt_no, 1)
            self.assertEqual(log.attempt_role, "primary")
            self.assertNotIn("sk-openrouter-secret-key", str(log.request_payload))
            self.assertNotIn("sk-openrouter-secret-key", str(log.clean_text))

    async def test_openrouter_rag_isolated_from_openrouter(self):
        """When OpenRouter is chat provider, RAG embeddings ONLY use OpenAI key."""
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenRouter"
            conf.openrouter_api_key = "sk-openrouter-key"
            conf.openai_api_key = None  # No OpenAI key
            await session.commit()

            cfg, emb_client, chat_client, model, err = await _prepare_clients(session)
            self.assertIsNone(err)
            self.assertIsNone(emb_client, "Embedding client must be None when openai_api_key is absent")
            self.assertEqual(chat_client.base_url.host, "openrouter.ai")

            # With OpenAI key present:
            conf.openai_api_key = "sk-openai-for-embeddings"
            await session.commit()

            cfg, emb_client, chat_client, model, err = await _prepare_clients(session)
            self.assertIsNotNone(emb_client)
            self.assertEqual(emb_client.api_key, "sk-openai-for-embeddings")
            self.assertEqual(chat_client.base_url.host, "openrouter.ai")


class TestOpenRouterFallbackMatrix(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.user_id = 884422
        clear_captured_test_alerts()
        async with async_session_maker() as session:
            user = await session.get(User, self.user_id)
            if not user:
                session.add(User(id=self.user_id, first_name="FbUser", username="fbuser"))
            await session.execute(delete(AILog).where(AILog.user_id == self.user_id))
            await session.commit()

    async def test_openai_to_openrouter_fallback_success(self):
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenAI"
            conf.openai_api_key = "sk-openai"
            conf.openai_model = "gpt-4o"
            conf.fallback_provider = "OpenRouter"
            conf.fallback_model = "google/gemini-3.7-flash"
            conf.openrouter_api_key = "sk-openrouter"
            conf.allow_fallback = True
            await session.commit()

        call_idx = 0
        async def mock_generate_chat(cfg, client, model, messages):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                err = RuntimeError("429 rate limit exceeded from OpenAI")
                err.status_code = 429
                raise err
            return SimpleNamespace(
                text="Резервный ответ OpenRouter",
                raw_payload={"id": "or-fallback-1"},
                http_status=None,
                finish_reason="stop",
                diagnostics={"provider": "OpenRouter", "model": model},
            )

        with patch("ai_integration._generate_chat_response", side_effect=mock_generate_chat):
            ans = await get_ai_response_for_topic(self.user_id, "Тест OpenAI->OpenRouter", "User")
            self.assertIn("Резервный ответ OpenRouter", ans)
            self.assertEqual(call_idx, 2)

        alerts = get_captured_test_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "fallback_used")
        self.assertIn("OpenRouter", alerts[0]["text"])

        async with async_session_maker() as session:
            logs = (await session.scalars(
                select(AILog).where(AILog.user_id == self.user_id).order_by(AILog.id.asc())
            )).all()
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0].attempt_no, 1)
            self.assertEqual(logs[0].attempt_role, "primary")
            self.assertEqual(logs[0].status, "error")
            self.assertEqual(logs[0].provider, "OpenAI")
            self.assertEqual(logs[0].model, "gpt-4o")

            self.assertEqual(logs[1].attempt_no, 2)
            self.assertEqual(logs[1].attempt_role, "fallback")
            self.assertEqual(logs[1].status, "success")
            self.assertEqual(logs[1].provider, "OpenRouter")
            self.assertEqual(logs[1].model, "google/gemini-3.7-flash")

            self.assertIsNotNone(logs[0].request_group_id)
            self.assertEqual(logs[0].request_group_id, logs[1].request_group_id)

    async def test_openrouter_to_openai_fallback_success(self):
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenRouter"
            conf.openrouter_api_key = "sk-openrouter"
            conf.openrouter_model = "openai/gpt-5.6-terra"
            conf.fallback_provider = "OpenAI"
            conf.fallback_model = "gpt-4o-mini"
            conf.openai_api_key = "sk-openai"
            conf.allow_fallback = True
            await session.commit()

        call_idx = 0
        async def mock_generate_chat(cfg, client, model, messages):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                err = RuntimeError("500 OpenRouter upstream failure")
                err.status_code = 500
                raise err
            return SimpleNamespace(
                text="Ответ резервного OpenAI",
                raw_payload={"id": "oa-fallback-1"},
                http_status=None,
                finish_reason="stop",
                diagnostics={"provider": "OpenAI", "model": model},
            )

        with patch("ai_integration._generate_chat_response", side_effect=mock_generate_chat):
            ans = await get_ai_response_for_topic(self.user_id, "Тест OpenRouter->OpenAI", "User")
            self.assertIn("Ответ резервного OpenAI", ans)
            self.assertEqual(call_idx, 2)

        alerts = get_captured_test_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "fallback_used")

        async with async_session_maker() as session:
            logs = (await session.scalars(
                select(AILog).where(AILog.user_id == self.user_id).order_by(AILog.id.asc())
            )).all()
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0].attempt_no, 1)
            self.assertEqual(logs[0].attempt_role, "primary")
            self.assertEqual(logs[0].status, "error")
            self.assertEqual(logs[0].provider, "OpenRouter")
            self.assertEqual(logs[0].model, "openai/gpt-5.6-terra")

            self.assertEqual(logs[1].attempt_no, 2)
            self.assertEqual(logs[1].attempt_role, "fallback")
            self.assertEqual(logs[1].status, "success")
            self.assertEqual(logs[1].provider, "OpenAI")
            self.assertEqual(logs[1].model, "gpt-4o-mini")

            self.assertIsNotNone(logs[0].request_group_id)
            self.assertEqual(logs[0].request_group_id, logs[1].request_group_id)

    async def test_deepseek_to_openrouter_fallback_success(self):
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "DeepSeek"
            conf.deepseek_api_key = "sk-deepseek"
            conf.deepseek_model = "deepseek-chat"
            conf.fallback_provider = "OpenRouter"
            conf.fallback_model = "anthropic/claude-sonnet-4.6"
            conf.openrouter_api_key = "sk-openrouter"
            conf.allow_fallback = True
            await session.commit()

        call_idx = 0
        async def mock_generate_chat(cfg, client, model, messages):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                err = RuntimeError("503 DeepSeek service unavailable")
                err.status_code = 503
                raise err
            return SimpleNamespace(
                text="Резервный ответ OpenRouter от DeepSeek",
                raw_payload={"id": "or-ds-fb-1"},
                http_status=None,
                finish_reason="stop",
                diagnostics={"provider": "OpenRouter", "model": model},
            )

        with patch("ai_integration._generate_chat_response", side_effect=mock_generate_chat):
            ans = await get_ai_response_for_topic(self.user_id, "Тест DeepSeek->OpenRouter", "User")
            self.assertIn("Резервный ответ OpenRouter от DeepSeek", ans)
            self.assertEqual(call_idx, 2)

        alerts = get_captured_test_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "fallback_used")
        self.assertIn("OpenRouter", alerts[0]["text"])

        async with async_session_maker() as session:
            logs = (await session.scalars(
                select(AILog).where(AILog.user_id == self.user_id).order_by(AILog.id.asc())
            )).all()
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0].attempt_no, 1)
            self.assertEqual(logs[0].attempt_role, "primary")
            self.assertEqual(logs[0].status, "error")
            self.assertEqual(logs[0].provider, "DeepSeek")
            self.assertEqual(logs[0].model, "deepseek-chat")

            self.assertEqual(logs[1].attempt_no, 2)
            self.assertEqual(logs[1].attempt_role, "fallback")
            self.assertEqual(logs[1].status, "success")
            self.assertEqual(logs[1].provider, "OpenRouter")
            self.assertEqual(logs[1].model, "anthropic/claude-sonnet-4.6")

            self.assertIsNotNone(logs[0].request_group_id)
            self.assertEqual(logs[0].request_group_id, logs[1].request_group_id)

    async def test_kie_to_openrouter_fallback_success(self):
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "KIE"
            conf.kie_api_key = "sk-kie"
            conf.kie_model = "gemini-3-flash"
            conf.fallback_provider = "OpenRouter"
            conf.fallback_model = "deepseek/deepseek-v4.1-flash"
            conf.openrouter_api_key = "sk-openrouter"
            conf.allow_fallback = True
            await session.commit()

        call_idx = 0
        async def mock_generate_chat(cfg, client, model, messages):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                err = RuntimeError("504 KIE gateway timeout")
                err.status_code = 504
                raise err
            return SimpleNamespace(
                text="Резервный ответ OpenRouter от KIE",
                raw_payload={"id": "or-kie-fb-1"},
                http_status=None,
                finish_reason="stop",
                diagnostics={"provider": "OpenRouter", "model": model},
            )

        with patch("ai_integration._generate_chat_response", side_effect=mock_generate_chat):
            ans = await get_ai_response_for_topic(self.user_id, "Тест KIE->OpenRouter", "User")
            self.assertIn("Резервный ответ OpenRouter от KIE", ans)
            self.assertEqual(call_idx, 2)

        alerts = get_captured_test_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "fallback_used")
        self.assertIn("OpenRouter", alerts[0]["text"])

        async with async_session_maker() as session:
            logs = (await session.scalars(
                select(AILog).where(AILog.user_id == self.user_id).order_by(AILog.id.asc())
            )).all()
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0].attempt_no, 1)
            self.assertEqual(logs[0].attempt_role, "primary")
            self.assertEqual(logs[0].status, "error")
            self.assertEqual(logs[0].provider, "KIE")
            self.assertEqual(logs[0].model, "gemini-3-flash")

            self.assertEqual(logs[1].attempt_no, 2)
            self.assertEqual(logs[1].attempt_role, "fallback")
            self.assertEqual(logs[1].status, "success")
            self.assertEqual(logs[1].provider, "OpenRouter")
            self.assertEqual(logs[1].model, "deepseek/deepseek-v4.1-flash")

            self.assertIsNotNone(logs[0].request_group_id)
            self.assertEqual(logs[0].request_group_id, logs[1].request_group_id)

    async def test_openrouter_to_deepseek_fallback_success(self):
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenRouter"
            conf.openrouter_api_key = "sk-openrouter"
            conf.openrouter_model = "openai/gpt-5.6-terra"
            conf.fallback_provider = "DeepSeek"
            conf.fallback_model = "deepseek-chat"
            conf.deepseek_api_key = "sk-deepseek"
            conf.allow_fallback = True
            await session.commit()

        call_idx = 0
        async def mock_generate_chat(cfg, client, model, messages):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                err = RuntimeError("429 OpenRouter rate limited")
                err.status_code = 429
                raise err
            return SimpleNamespace(
                text="Резервный ответ DeepSeek от OpenRouter",
                raw_payload={"id": "ds-or-fb-1"},
                http_status=None,
                finish_reason="stop",
                diagnostics={"provider": "DeepSeek", "model": model},
            )

        with patch("ai_integration._generate_chat_response", side_effect=mock_generate_chat):
            ans = await get_ai_response_for_topic(self.user_id, "Тест OpenRouter->DeepSeek", "User")
            self.assertIn("Резервный ответ DeepSeek от OpenRouter", ans)
            self.assertEqual(call_idx, 2)

        alerts = get_captured_test_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "fallback_used")
        self.assertIn("DeepSeek", alerts[0]["text"])

        async with async_session_maker() as session:
            logs = (await session.scalars(
                select(AILog).where(AILog.user_id == self.user_id).order_by(AILog.id.asc())
            )).all()
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0].attempt_no, 1)
            self.assertEqual(logs[0].attempt_role, "primary")
            self.assertEqual(logs[0].status, "error")
            self.assertEqual(logs[0].provider, "OpenRouter")
            self.assertEqual(logs[0].model, "openai/gpt-5.6-terra")

            self.assertEqual(logs[1].attempt_no, 2)
            self.assertEqual(logs[1].attempt_role, "fallback")
            self.assertEqual(logs[1].status, "success")
            self.assertEqual(logs[1].provider, "DeepSeek")
            self.assertEqual(logs[1].model, "deepseek-chat")

            self.assertIsNotNone(logs[0].request_group_id)
            self.assertEqual(logs[0].request_group_id, logs[1].request_group_id)

    async def test_openrouter_to_kie_fallback_success(self):
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenRouter"
            conf.openrouter_api_key = "sk-openrouter"
            conf.openrouter_model = "openai/gpt-5.6-terra"
            conf.fallback_provider = "KIE"
            conf.fallback_model = "gemini-3-flash"
            conf.kie_api_key = "sk-kie"
            conf.allow_fallback = True
            await session.commit()

        call_idx = 0
        async def mock_generate_chat(cfg, client, model, messages):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                err = RuntimeError("502 OpenRouter bad gateway")
                err.status_code = 502
                raise err
            return SimpleNamespace(
                text="Резервный ответ KIE от OpenRouter",
                raw_payload={"id": "kie-or-fb-1"},
                http_status=None,
                finish_reason="stop",
                diagnostics={"provider": "KIE", "model": model},
            )

        with patch("ai_integration._generate_chat_response", side_effect=mock_generate_chat):
            ans = await get_ai_response_for_topic(self.user_id, "Тест OpenRouter->KIE", "User")
            self.assertIn("Резервный ответ KIE от OpenRouter", ans)
            self.assertEqual(call_idx, 2)

        alerts = get_captured_test_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "fallback_used")
        self.assertIn("KIE", alerts[0]["text"])

        async with async_session_maker() as session:
            logs = (await session.scalars(
                select(AILog).where(AILog.user_id == self.user_id).order_by(AILog.id.asc())
            )).all()
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0].attempt_no, 1)
            self.assertEqual(logs[0].attempt_role, "primary")
            self.assertEqual(logs[0].status, "error")
            self.assertEqual(logs[0].provider, "OpenRouter")
            self.assertEqual(logs[0].model, "openai/gpt-5.6-terra")

            self.assertEqual(logs[1].attempt_no, 2)
            self.assertEqual(logs[1].attempt_role, "fallback")
            self.assertEqual(logs[1].status, "success")
            self.assertEqual(logs[1].provider, "KIE")
            self.assertEqual(logs[1].model, "gemini-3-flash")

            self.assertIsNotNone(logs[0].request_group_id)
            self.assertEqual(logs[0].request_group_id, logs[1].request_group_id)

    async def test_openrouter_primary_allow_fallback_false_no_second_call(self):
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenRouter"
            conf.openrouter_api_key = "sk-openrouter"
            conf.openrouter_model = "openai/gpt-5.6-terra"
            conf.fallback_provider = "OpenAI"
            conf.fallback_model = "gpt-4o"
            conf.openai_api_key = "sk-openai"
            conf.allow_fallback = False
            await session.commit()

        call_idx = 0
        async def mock_generate_chat(cfg, client, model, messages):
            nonlocal call_idx
            call_idx += 1
            err = RuntimeError("500 OpenRouter primary error")
            err.status_code = 500
            raise err

        with patch("ai_integration._generate_chat_response", side_effect=mock_generate_chat):
            ans = await get_ai_response_for_topic(self.user_id, "Тест no fallback", "User")
            self.assertIn("Произошла ошибка при генерации ответа", ans)
            self.assertEqual(call_idx, 1, "No second provider call must occur when allow_fallback=False")

        alerts = get_captured_test_alerts()
        self.assertTrue(any(a["type"] == "terminal_ai_failure" for a in alerts))
        self.assertFalse(any(a["type"] == "fallback_used" for a in alerts))

        async with async_session_maker() as session:
            logs = (await session.scalars(
                select(AILog).where(AILog.user_id == self.user_id).order_by(AILog.id.asc())
            )).all()
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].attempt_no, 1)
            self.assertEqual(logs[0].attempt_role, "primary")
            self.assertEqual(logs[0].status, "error")
            self.assertEqual(logs[0].provider, "OpenRouter")
            self.assertEqual(logs[0].model, "openai/gpt-5.6-terra")

    async def test_invalid_openrouter_fallback_model_fails_before_network(self):
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenAI"
            conf.openai_api_key = "sk-openai"
            conf.fallback_provider = "OpenRouter"
            conf.fallback_model = "invalid/unrecognized-model"
            conf.openrouter_api_key = "sk-openrouter"
            conf.allow_fallback = True
            await session.commit()

        call_idx = 0
        async def mock_generate_chat(cfg, client, model, messages):
            nonlocal call_idx
            call_idx += 1
            err = RuntimeError("Primary failure")
            err.status_code = 500
            raise err

        with patch("ai_integration._generate_chat_response", side_effect=mock_generate_chat):
            ans = await get_ai_response_for_topic(self.user_id, "Тест", "User")
            self.assertEqual(call_idx, 1, "Fallback must not make network calls when model is invalid")

        alerts = get_captured_test_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "terminal_ai_failure")


class TestOpenRouterJournalAndStandalone(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.user_id = 665544
        clear_captured_test_alerts()
        async with async_session_maker() as session:
            user = await session.get(User, self.user_id)
            if not user:
                user = User(id=self.user_id, first_name="JournalUser", journal_points=10)
                session.add(user)
            else:
                user.journal_points = 10
            await session.execute(delete(JournalEntry).where(JournalEntry.user_id == self.user_id))
            await session.execute(delete(JournalAiOperation).where(JournalAiOperation.user_id == self.user_id))
            await session.execute(delete(AILog).where(AILog.user_id == self.user_id))
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenRouter"
            conf.openrouter_api_key = "sk-or-journal"
            conf.openrouter_model = "openai/gpt-5.6-terra"
            conf.allow_fallback = False
            await session.commit()

    async def _create_completed_journal_entries(self, count: int = 3):
        from time_utils import msk_now
        today = msk_now().date()
        async with async_session_maker() as session:
            for d in range(1, count + 1):
                session.add(
                    JournalEntry(
                        user_id=self.user_id,
                        reflection_date=today - datetime.timedelta(days=d),
                        status="completed",
                        summary_text=f"День {d}: продуктивный день",
                        questions_snapshot_json="[]",
                    )
                )
            await session.commit()

    async def test_standalone_ai_response_with_openrouter_primary(self):
        mock_choice = SimpleNamespace(
            message=SimpleNamespace(content="Интерпретация теста завершена успешно."),
            finish_reason="stop",
        )
        mock_resp = SimpleNamespace(choices=[mock_choice], model_dump=lambda: {})

        with patch("openai.resources.chat.completions.AsyncCompletions.create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_resp
            resp = await get_standalone_ai_response("Системный промпт", "Пользовательский ввод")
            self.assertIn("Интерпретация теста", resp)
            mock_create.assert_called_once()
            self.assertEqual(mock_create.call_args.kwargs["model"], "openai/gpt-5.6-terra")

    async def test_journal_daily_summary_openrouter_primary_and_fallback(self):
        # 1. Create a completed journal entry
        async with async_session_maker() as session:
            entry = JournalEntry(
                user_id=self.user_id,
                reflection_date=datetime.date(2026, 10, 8),
                status="completed",
                questions_snapshot_json="[]",
            )
            session.add(entry)
            await session.commit()
            entry_id = entry.id

        mock_summary = (
            "[[SUMMARY]]\nТы отлично справился с сегодняшними вызовами.\n[[INSIGHT]]\nВнимание к деталям."
        )

        # A. Primary OpenRouter success
        async def mock_primary_chat(cfg, client, model, messages):
            return SimpleNamespace(
                text=mock_summary,
                raw_payload={"id": "or-summary-1"},
                http_status=None,
                finish_reason="stop",
                diagnostics={"provider": "OpenRouter", "model": model},
            )

        with patch("ai_integration._generate_chat_response", side_effect=mock_primary_chat), \
             patch("journal_service.build_journal_daily_summary_context", new_callable=AsyncMock) as mock_ctx:
            mock_ctx.return_value = ("User input", "System prompt")
            res = await generate_journal_daily_summary(entry_id, self.user_id, self.user_id)
            self.assertEqual(res, "succeeded")

        # B. Fallback OpenRouter success
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenAI"
            conf.openai_api_key = "sk-openai"
            conf.fallback_provider = "OpenRouter"
            conf.fallback_model = "google/gemini-3.7-flash"
            conf.allow_fallback = True
            await session.commit()

        # Reset entry for second run
        async with async_session_maker() as session:
            entry2 = JournalEntry(
                user_id=self.user_id,
                reflection_date=datetime.date(2026, 10, 9),
                status="completed",
                questions_snapshot_json="[]",
            )
            session.add(entry2)
            await session.commit()
            entry2_id = entry2.id

        call_idx = 0
        async def mock_fallback_chat(cfg, client, model, messages):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                err = RuntimeError("OpenAI timeout")
                err.status_code = 504
                raise err
            return SimpleNamespace(
                text=mock_summary,
                raw_payload={"id": "or-fb-summary-2"},
                http_status=None,
                finish_reason="stop",
                diagnostics={"provider": "OpenRouter", "model": model},
            )

        with patch("ai_integration._generate_chat_response", side_effect=mock_fallback_chat), \
             patch("journal_service.build_journal_daily_summary_context", new_callable=AsyncMock) as mock_ctx:
            mock_ctx.return_value = ("User input", "System prompt")
            res = await generate_journal_daily_summary(entry2_id, self.user_id, self.user_id)
            self.assertEqual(res, "succeeded")
            self.assertEqual(call_idx, 2)

        alerts = get_captured_test_alerts()
        self.assertTrue(any(a["type"] == "fallback_used" and "OpenRouter" in a["text"] for a in alerts))

    async def test_journal_dynamics_openrouter_primary_7_day_success(self):
        await self._create_completed_journal_entries(3)
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenRouter"
            conf.openrouter_api_key = "sk-or-primary"
            conf.openrouter_model = "google/gemini-3.7-flash"
            conf.allow_fallback = False
            await session.commit()

        mock_dyn_response = StructuredAIResponse(
            text="7-дневный анализ динамики OpenRouter: отличная устойчивость и рост.",
            raw_payload={"id": "or-dyn-primary-7"},
            http_status=None,
            finish_reason="stop",
            diagnostics={"provider": "OpenRouter", "model": "google/gemini-3.7-flash"},
        )

        with patch("ai_integration._generate_chat_response", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = mock_dyn_response
            ok, result_text = await generate_journal_dynamics_analysis(
                user_id=self.user_id,
                days=7,
                chat_id=self.user_id,
                token8="dynprim7",
            )
            self.assertTrue(ok)
            self.assertEqual(result_text, "7-дневный анализ динамики OpenRouter: отличная устойчивость и рост.")
            mock_chat.assert_called_once()
            self.assertEqual(mock_chat.call_args[0][2], "google/gemini-3.7-flash")

        # Verify JournalAiOperation in DB
        async with async_session_maker() as session:
            op = (await session.scalars(
                select(JournalAiOperation)
                .where(JournalAiOperation.user_id == self.user_id, JournalAiOperation.operation_type == "dynamics_7")
                .order_by(JournalAiOperation.id.desc())
            )).first()
            self.assertIsNotNone(op)
            self.assertEqual(op.status, "succeeded")
            self.assertEqual(op.result_text, "7-дневный анализ динамики OpenRouter: отличная устойчивость и рост.")
            self.assertIsNone(op.lease_until)

            # Verify AILog
            logs = (await session.scalars(
                select(AILog).where(AILog.user_id == self.user_id).order_by(AILog.id.asc())
            )).all()
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].provider, "OpenRouter")
            self.assertEqual(logs[0].model, "google/gemini-3.7-flash")
            self.assertEqual(logs[0].status, "success")
            self.assertEqual(logs[0].attempt_no, 1)
            self.assertEqual(logs[0].attempt_role, "primary")

    async def test_journal_dynamics_openrouter_fallback_21_day_success(self):
        await self._create_completed_journal_entries(5)
        async with async_session_maker() as session:
            conf = await session.get(AIConfig, 1)
            conf.provider = "DeepSeek"
            conf.deepseek_api_key = "sk-ds-primary"
            conf.deepseek_model = "deepseek-chat"
            conf.fallback_provider = "OpenRouter"
            conf.fallback_model = "anthropic/claude-sonnet-4.6"
            conf.openrouter_api_key = "sk-or-fallback"
            conf.allow_fallback = True
            await session.commit()

        call_idx = 0
        async def mock_chat(cfg, client, model, messages):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                err = RuntimeError("500 DeepSeek primary server error")
                err.status_code = 500
                raise err
            return StructuredAIResponse(
                text="21-дневный анализ динамики OpenRouter Fallback: глубокие инсайты.",
                raw_payload={"id": "or-dyn-fb-21"},
                http_status=None,
                finish_reason="stop",
                diagnostics={"provider": "OpenRouter", "model": model},
            )

        with patch("ai_integration._generate_chat_response", side_effect=mock_chat):
            ok, result_text = await generate_journal_dynamics_analysis(
                user_id=self.user_id,
                days=21,
                chat_id=self.user_id,
                token8="dynfb21",
            )
            self.assertTrue(ok)
            self.assertEqual(result_text, "21-дневный анализ динамики OpenRouter Fallback: глубокие инсайты.")
            self.assertEqual(call_idx, 2)

        # Verify JournalAiOperation in DB
        async with async_session_maker() as session:
            op = (await session.scalars(
                select(JournalAiOperation)
                .where(JournalAiOperation.user_id == self.user_id, JournalAiOperation.operation_type == "dynamics_21")
                .order_by(JournalAiOperation.id.desc())
            )).first()
            self.assertIsNotNone(op)
            self.assertEqual(op.status, "succeeded")
            self.assertEqual(op.result_text, "21-дневный анализ динамики OpenRouter Fallback: глубокие инсайты.")
            self.assertIsNone(op.lease_until)

            # Verify AILog
            logs = (await session.scalars(
                select(AILog).where(AILog.user_id == self.user_id).order_by(AILog.id.asc())
            )).all()
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0].attempt_no, 1)
            self.assertEqual(logs[0].attempt_role, "primary")
            self.assertEqual(logs[0].status, "error")
            self.assertEqual(logs[0].provider, "DeepSeek")
            self.assertEqual(logs[0].model, "deepseek-chat")

            self.assertEqual(logs[1].attempt_no, 2)
            self.assertEqual(logs[1].attempt_role, "fallback")
            self.assertEqual(logs[1].status, "success")
            self.assertEqual(logs[1].provider, "OpenRouter")
            self.assertEqual(logs[1].model, "anthropic/claude-sonnet-4.6")
            self.assertEqual(logs[0].request_group_id, logs[1].request_group_id)

        # Verify fallback-used alert fires
        alerts = get_captured_test_alerts()
        fb_alerts = [a for a in alerts if a["type"] == "fallback_used"]
        self.assertEqual(len(fb_alerts), 1)
        self.assertIn("OpenRouter", fb_alerts[0]["text"])

    async def test_journal_dynamics_billable_terminal_failure_exactly_once_refund_openrouter(self):
        await self._create_completed_journal_entries(3)
        async with async_session_maker() as session:
            # 1. Setup billable user
            user = await session.get(User, self.user_id)
            user.is_admin = False
            user.balance = 50.0

            # 2. Setup paid topic and token payment mode
            topic = await session.scalar(select(Topic).where(Topic.slug == "journal"))
            if not topic:
                topic = Topic(slug="journal", name="Дневник", is_paid=True)
                session.add(topic)
            else:
                topic.is_paid = True

            pay_cfg = await session.get(PaymentConfig, 1)
            if not pay_cfg:
                pay_cfg = PaymentConfig(id=1, mode="token", cost_per_message=5.0)
                session.add(pay_cfg)
            else:
                pay_cfg.mode = "token"
                pay_cfg.cost_per_message = 5.0

            # 3. Setup AI config with Primary OpenAI and Fallback OpenRouter
            conf = await session.get(AIConfig, 1)
            conf.provider = "OpenAI"
            conf.openai_api_key = "sk-openai"
            conf.openai_model = "gpt-4o"
            conf.fallback_provider = "OpenRouter"
            conf.openrouter_api_key = "sk-openrouter"
            conf.fallback_model = "openai/gpt-5.6-terra"
            conf.allow_fallback = True
            await session.commit()

        call_idx = 0
        async def mock_chat(cfg, client, model, messages):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                err = RuntimeError("504 OpenAI Gateway Timeout")
                err.status_code = 504
                raise err
            else:
                err = RuntimeError("502 OpenRouter Bad Gateway")
                err.status_code = 502
                raise err

        with patch("ai_integration._generate_chat_response", side_effect=mock_chat):
            ok, msg = await generate_journal_dynamics_analysis(
                user_id=self.user_id,
                days=7,
                chat_id=self.user_id,
                token8="termfail7",
            )
            self.assertFalse(ok)
            self.assertEqual(call_idx, 2)

        async with async_session_maker() as session:
            # 1. Assert operation finished failed and heartbeat stopped
            op = (await session.scalars(
                select(JournalAiOperation)
                .where(JournalAiOperation.user_id == self.user_id, JournalAiOperation.operation_type == "dynamics_7")
                .order_by(JournalAiOperation.id.desc())
            )).first()
            self.assertIsNotNone(op)
            self.assertEqual(op.status, "failed")
            self.assertIsNone(op.lease_until, "Heartbeat lease must be stopped/cleared upon terminal failure")

            # 2. Assert charged exactly once and refunded exactly once
            self.assertEqual(op.token_cost, 5.0)
            self.assertIsNotNone(op.charged_at, "Operation must have charged user at initiation")
            self.assertIsNotNone(op.refunded_at, "Operation must have recorded refund at terminal failure")

            user = await session.get(User, self.user_id)
            self.assertEqual(user.balance, 50.0, "Balance must be refunded exactly once (net delta 0)")

            # 3. Assert duplicate / repeated finalization cannot produce a second refund
            dup_refund = await finalize_journal_ai_operation_failure(session, op.id, op.owner_token)
            self.assertFalse(dup_refund, "Repeated finalization must be rejected by CAS")
            user_after_dup = await session.get(User, self.user_id)
            self.assertEqual(user_after_dup.balance, 50.0, "Duplicate finalization must NOT refund user again")

            # 4. Assert wrong token CAS rejection
            wrong_token_refund = await finalize_journal_ai_operation_failure(session, op.id, "wrong-owner-token")
            self.assertFalse(wrong_token_refund, "Wrong owner token must be rejected by CAS")
            user_after_wrong = await session.get(User, self.user_id)
            self.assertEqual(user_after_wrong.balance, 50.0)

            # 5. Assert two AI attempts logged
            logs = (await session.scalars(
                select(AILog).where(AILog.user_id == self.user_id).order_by(AILog.id.asc())
            )).all()
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0].attempt_no, 1)
            self.assertEqual(logs[0].attempt_role, "primary")
            self.assertEqual(logs[0].status, "error")
            self.assertEqual(logs[0].provider, "OpenAI")
            self.assertEqual(logs[0].model, "gpt-4o")

            self.assertEqual(logs[1].attempt_no, 2)
            self.assertEqual(logs[1].attempt_role, "fallback")
            self.assertEqual(logs[1].status, "error")
            self.assertEqual(logs[1].provider, "OpenRouter")
            self.assertEqual(logs[1].model, "openai/gpt-5.6-terra")
            self.assertEqual(logs[0].request_group_id, logs[1].request_group_id)

        # 6. Assert terminal alert contains both providers/models
        alerts = get_captured_test_alerts()
        term_alerts = [a for a in alerts if a["type"] == "terminal_ai_failure"]
        self.assertEqual(len(term_alerts), 1)
        alert_text = term_alerts[0]["text"]
        self.assertIn("OpenAI", alert_text)
        self.assertIn("OpenRouter", alert_text)
        self.assertIn("gpt-4o", alert_text)
        self.assertIn("openai/gpt-5.6-terra", alert_text)
