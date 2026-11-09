import unittest
from unittest.mock import AsyncMock, patch

from error_reporting import (
    clear_captured_test_alerts,
    get_captured_test_alerts,
    is_testing_mode,
    notify_admins_about_error,
    send_ai_fallback_used_alert,
    send_terminal_ai_failure_alert,
    _user_ref,
)
from ai_integration import normalize_provider, CANONICAL_PROVIDERS, DEFAULT_PROVIDER_MODELS


class TestSafetyTestModeAndAlerts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_captured_test_alerts()

    async def asyncTearDown(self):
        clear_captured_test_alerts()

    def test_is_testing_mode_active(self):
        """Testing mode must be active under pytest/test execution."""
        self.assertTrue(is_testing_mode())

    def test_user_ref_formatting(self):
        """_user_ref must NEVER prepend @ to a person's name, only to username."""
        # 1. Full name only: No @ symbol
        ref_name = _user_ref(user_id=123, username=None, full_name="Тестовый Пользователь")
        self.assertNotIn("@Тестовый", ref_name)
        self.assertIn("Тестовый Пользователь", ref_name)
        self.assertIn("tg://user?id=123", ref_name)

        # 2. Username only: Has @ symbol
        ref_user = _user_ref(user_id=123, username="test_user", full_name=None)
        self.assertIn("@test_user", ref_user)

        # 3. Both full name and username: Name followed by (@username)
        ref_both = _user_ref(user_id=123, username="test_user", full_name="Тестовый Пользователь")
        self.assertIn("Тестовый Пользователь (@test_user,", ref_both)

        # 4. Neither: Shows ID
        ref_id = _user_ref(user_id=123, username=None, full_name=None)
        self.assertIn("<code>123</code>", ref_id)

    def test_provider_normalization(self):
        """normalize_provider correctly standardizes provider strings and handles casing/whitespace."""
        self.assertEqual(normalize_provider("deepseek"), "DeepSeek")
        self.assertEqual(normalize_provider("DEEPSEEK"), "DeepSeek")
        self.assertEqual(normalize_provider(" DeepSeek "), "DeepSeek")
        self.assertEqual(normalize_provider("openai"), "OpenAI")
        self.assertEqual(normalize_provider("OPENAI"), "OpenAI")
        self.assertEqual(normalize_provider("kie"), "KIE")
        self.assertEqual(normalize_provider("KIE"), "KIE")
        self.assertEqual(normalize_provider("openrouter"), "OpenRouter")
        self.assertEqual(normalize_provider("OpenRouter"), "OpenRouter")
        self.assertEqual(normalize_provider(" OPENROUTER "), "OpenRouter")
        self.assertEqual(normalize_provider(None), "OpenAI")

        self.assertEqual(DEFAULT_PROVIDER_MODELS["DeepSeek"], "deepseek-chat")
        self.assertEqual(DEFAULT_PROVIDER_MODELS["OpenAI"], "gpt-4o-mini")
        self.assertEqual(DEFAULT_PROVIDER_MODELS["KIE"], "gemini-2.5-flash")
        self.assertEqual(DEFAULT_PROVIDER_MODELS["OpenRouter"], "openai/gpt-5.6-terra")

    async def test_live_network_telegram_never_called_in_test_mode(self):
        """Even with live-looking tokens, test mode intercepts and records alerts without network requests."""
        # 1. Terminal AI Failure Alert
        res = await send_terminal_ai_failure_alert(
            bot=None,
            user_id=987654,
            username="testuser",
            full_name="Тестовый Пользователь",
            primary_provider="DeepSeek",
            primary_model="deepseek-chat",
            fallback_provider="OpenAI",
            fallback_model="gpt-4o-mini",
            classification="provider_5xx",
            error_message="500 Internal Server Error",
            ai_log_ids=[101, 102],
            provider_attempts=[
                {"provider": "DeepSeek", "model": "deepseek-chat", "status": "error", "classification": "provider_5xx"},
                {"provider": "OpenAI", "model": "gpt-4o-mini", "status": "error", "classification": "timeout"},
            ],
        )
        self.assertTrue(res)

        alerts = get_captured_test_alerts()
        self.assertEqual(len(alerts), 1)
        alert_item = alerts[0]
        self.assertEqual(alert_item["type"], "terminal_ai_failure")
        self.assertIn("🚨 <b>[Telegram] Сбой ИИ в текстовом диалоге</b>", alert_item["text"])
        self.assertIn("Бот: <code>Vitaliy Bot</code>", alert_item["text"])
        self.assertIn("Telegram ID: <code>987654</code>", alert_item["text"])
        self.assertIn("Тестовый Пользователь (@testuser,", alert_item["text"])
        self.assertIn("Primary: DeepSeek / deepseek-chat ❌", alert_item["text"])
        self.assertIn("Fallback: OpenAI / gpt-4o-mini ❌", alert_item["text"])
        self.assertIn("• DeepSeek / deepseek-chat ❌ (provider_5xx)", alert_item["text"])
        self.assertIn("• OpenAI / gpt-4o-mini ❌ (timeout)", alert_item["text"])
        self.assertIn("500 Internal Server Error", alert_item["text"])
        self.assertIn("AI Log IDs: #101, #102", alert_item["text"])

        # 2. Fallback Used Alert
        res_fb = await send_ai_fallback_used_alert(
            bot=None,
            user_id=987654,
            username="testuser",
            full_name="Тестовый Пользователь",
            primary_provider="DeepSeek",
            primary_model="deepseek-chat",
            fallback_provider="OpenAI",
            fallback_model="gpt-4o-mini",
            primary_error="Rate limited (429)",
            ai_log_ids=[101, 103],
            provider_attempts=[
                {"provider": "DeepSeek", "model": "deepseek-chat", "status": "error", "classification": "rate_limit"},
                {"provider": "OpenAI", "model": "gpt-4o-mini", "status": "success"},
            ],
        )
        self.assertTrue(res_fb)

        alerts_all = get_captured_test_alerts()
        self.assertEqual(len(alerts_all), 2)
        fb_item = alerts_all[1]
        self.assertEqual(fb_item["type"], "fallback_used")
        self.assertIn("ℹ️ <b>[Telegram] Использован резервный AI-провайдер в текстовом диалоге</b>", fb_item["text"])
        self.assertIn("Основной: DeepSeek / deepseek-chat ❌", fb_item["text"])
        self.assertIn("Резервный: OpenAI / gpt-4o-mini ✅", fb_item["text"])
        self.assertIn("• DeepSeek / deepseek-chat ❌ (rate_limit)", fb_item["text"])
        self.assertIn("• OpenAI / gpt-4o-mini ✅", fb_item["text"])
        self.assertIn("Rate limited (429)", fb_item["text"])
        self.assertIn("AI Log IDs: #101, #103", fb_item["text"])

    async def test_async_mock_bot_invoked_in_testing_mode(self):
        """When an AsyncMock bot is passed, it receives the send_message call so test assertions work."""
        mock_bot = AsyncMock()
        with patch("database.get_all_admin_ids", AsyncMock(return_value={999999999})):
            await notify_admins_about_error(
                bot=mock_bot,
                title="Test Alert",
                user_id=555,
                details="Detailed error info",
            )
        self.assertTrue(mock_bot.send_message.called)
        call_args = mock_bot.send_message.call_args[0]
        self.assertEqual(call_args[0], 999999999)
        self.assertIn("Test Alert", call_args[1])

    async def test_alert_cooldown_suppression_and_test_reset(self):
        """Within one test scenario, identical alerts are throttled; reset_alert_cooldowns_for_tests restores them."""
        from error_reporting import reset_alert_cooldowns_for_tests

        # First alert is allowed
        res1 = await send_ai_fallback_used_alert(
            user_id=12345,
            primary_provider="DeepSeek",
            fallback_provider="OpenAI",
        )
        self.assertTrue(res1)

        # Immediate duplicate alert within same scenario is suppressed by cooldown
        res2 = await send_ai_fallback_used_alert(
            user_id=12345,
            primary_provider="DeepSeek",
            fallback_provider="OpenAI",
        )
        self.assertFalse(res2)

        # After explicit test reset, the alert is permitted again
        reset_alert_cooldowns_for_tests()
        res3 = await send_ai_fallback_used_alert(
            user_id=12345,
            primary_provider="DeepSeek",
            fallback_provider="OpenAI",
        )
        self.assertTrue(res3)

    async def test_psychobot_alert_contract_parity(self):
        """Verifies exact 1:1 parity with PsychoBot alert contract table."""
        # 1. Terminal AI Failure Alert with single AI Log and Chat ID
        await send_terminal_ai_failure_alert(
            user_id=777001,
            chat_id=-100123456789,
            username="psychouser",
            full_name="Психо Пользователь",
            bot_name="Vitaliy Bot",
            primary_provider="DeepSeek",
            primary_model="deepseek-chat",
            fallback_provider="OpenAI",
            fallback_model="gpt-4o-mini",
            classification="provider_5xx",
            error_type="APIConnectionError",
            error_message="Connection timed out",
            ai_log_ids=[42],
            provider_attempts=[
                {"provider": "DeepSeek", "model": "deepseek-chat", "status": "error", "classification": "provider_5xx"},
                {"provider": "OpenAI", "model": "gpt-4o-mini", "status": "error", "classification": "timeout"},
            ],
        )
        alerts = get_captured_test_alerts()
        self.assertEqual(len(alerts), 1)
        t_text = alerts[0]["text"]

        # Contract table assertions for Terminal Failure
        self.assertIn("🚨 <b>[Telegram] Сбой ИИ в текстовом диалоге</b>", t_text)
        self.assertIn("Бот: <code>Vitaliy Bot</code>", t_text)
        self.assertIn("Telegram ID: <code>777001</code>", t_text)
        self.assertIn("Chat ID: <code>-100123456789</code>", t_text)
        self.assertIn("Пользователь: Психо Пользователь (@psychouser,", t_text)
        self.assertIn("Primary: DeepSeek / deepseek-chat ❌", t_text)
        self.assertIn("Fallback: OpenAI / gpt-4o-mini ❌", t_text)
        self.assertIn("Попытки:\n• DeepSeek / deepseek-chat ❌ (provider_5xx)\n• OpenAI / gpt-4o-mini ❌ (timeout)", t_text)
        self.assertIn("Ошибка:\n<code>APIConnectionError</code> / <code>Connection timed out</code>", t_text)
        self.assertIn("Классификация:\n<code>provider_5xx</code>", t_text)
        self.assertIn("AI Log: #42", t_text)

        clear_captured_test_alerts()

        # 2. Fallback Used Alert with multiple AI Logs and Chat ID
        await send_ai_fallback_used_alert(
            user_id=777001,
            chat_id=-100123456789,
            username="psychouser",
            full_name="Психо Пользователь",
            bot_name="Vitaliy Bot",
            primary_provider="DeepSeek",
            primary_model="deepseek-chat",
            fallback_provider="OpenAI",
            fallback_model="gpt-4o-mini",
            primary_error="Rate limited 429",
            ai_log_ids=[42, 43],
            provider_attempts=[
                {"provider": "DeepSeek", "model": "deepseek-chat", "status": "error", "classification": "rate_limit"},
                {"provider": "OpenAI", "model": "gpt-4o-mini", "status": "success"},
            ],
        )
        alerts_fb = get_captured_test_alerts()
        self.assertEqual(len(alerts_fb), 1)
        fb_text = alerts_fb[0]["text"]

        # Contract table assertions for Fallback Used
        self.assertIn("ℹ️ <b>[Telegram] Использован резервный AI-провайдер в текстовом диалоге</b>", fb_text)
        self.assertIn("Бот: <code>Vitaliy Bot</code>", fb_text)
        self.assertIn("Telegram ID: <code>777001</code>", fb_text)
        self.assertIn("Chat ID: <code>-100123456789</code>", fb_text)
        self.assertIn("Пользователь: Психо Пользователь (@psychouser,", fb_text)
        self.assertIn("Основной: DeepSeek / deepseek-chat ❌", fb_text)
        self.assertIn("Резервный: OpenAI / gpt-4o-mini ✅", fb_text)
        self.assertIn("Попытки:\n• DeepSeek / deepseek-chat ❌ (rate_limit)\n• OpenAI / gpt-4o-mini ✅", fb_text)
        self.assertIn("Причина переключения:\n<code>Rate limited 429</code>", fb_text)
        self.assertIn("AI Log IDs: #42, #43", fb_text)

    async def test_physical_network_guard_blocks_telegram_api(self):
        """Any attempted real HTTP/socket transport to api.telegram.org fails with a descriptive RuntimeError."""
        import aiohttp
        async with aiohttp.ClientSession() as session:
            with self.assertRaises(RuntimeError) as ctx:
                await session.get("https://api.telegram.org/bot123456:FAKE/getMe")
            self.assertIn("CRITICAL TEST NETWORK ISOLATION", str(ctx.exception))
            self.assertIn("api.telegram.org", str(ctx.exception))


class TestAIAlertsProductionRuntimeE2E(unittest.IsolatedAsyncioTestCase):
    """Runtime E2E verification of AI Alerts via production get_ai_response_for_topic()."""

    async def asyncSetUp(self):
        from error_reporting import clear_captured_test_alerts, reset_alert_cooldowns_for_tests
        from database import init_db
        clear_captured_test_alerts()
        reset_alert_cooldowns_for_tests()
        await init_db()

    async def asyncTearDown(self):
        from error_reporting import clear_captured_test_alerts, reset_alert_cooldowns_for_tests
        clear_captured_test_alerts()
        reset_alert_cooldowns_for_tests()

    async def _setup_test_environment(self, user_id: int, allow_fallback: bool = True):
        from database import User, AIConfig, AILog, async_session_maker
        from sqlalchemy import delete
        async with async_session_maker() as session:
            await session.execute(delete(AILog).where(AILog.user_id == user_id))
            user = await session.get(User, user_id)
            if not user:
                user = User(
                    id=user_id,
                    username="alert_user",
                    name="Алексей Тестов",
                    first_name="Алексей",
                )
                session.add(user)
            else:
                user.username = "alert_user"
                user.name = "Алексей Тестов"
                user.first_name = "Алексей"

            cfg = await session.get(AIConfig, 1)
            if not cfg:
                cfg = AIConfig(id=1)
                session.add(cfg)
            cfg.provider = "DeepSeek"
            cfg.deepseek_api_key = "sk-ds-fake"
            cfg.deepseek_model = "deepseek-chat"
            cfg.allow_fallback = 1 if allow_fallback else 0
            cfg.fallback_provider = "OpenAI"
            cfg.fallback_model = "gpt-4o-mini"
            cfg.openai_api_key = "sk-oa-fake"
            await session.commit()

    async def test_scenario_a_fallback_success(self):
        """Scenario A: Primary (DeepSeek 429 rate_limit) -> Fallback (OpenAI success)."""
        from ai_integration import get_ai_response_for_topic
        from database import AILog, async_session_maker
        from sqlalchemy import select
        from error_reporting import get_captured_test_alerts

        user_id = 880001
        chat_id = -100880001
        await self._setup_test_environment(user_id=user_id, allow_fallback=True)

        call_count = 0

        async def fake_generate_chat_response(cfg, client, model, messages):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                err = RuntimeError("429 Too Many Requests - Rate limit exceeded")
                err.status_code = 429
                raise err
            return "Ответ от OpenAI успешно сгенерирован."

        with patch("ai_integration._generate_chat_response", side_effect=fake_generate_chat_response):
            result = await get_ai_response_for_topic(
                user_id=user_id,
                user_prompt="Как снизить тревожность?",
                user_name="Алексей Тестов",
                chat_id=chat_id,
            )

        self.assertIn("Ответ от OpenAI успешно сгенерирован.", result)
        self.assertEqual(call_count, 2)

        # 1. Verify AILog entries
        async with async_session_maker() as session:
            logs = (
                await session.scalars(
                    select(AILog).where(AILog.user_id == user_id).order_by(AILog.id.asc())
                )
            ).all()
            self.assertEqual(len(logs), 2)

            log1, log2 = logs[0], logs[1]
            self.assertEqual(log1.provider, "DeepSeek")
            self.assertEqual(log1.model, "deepseek-chat")
            self.assertEqual(log1.status, "error")
            self.assertEqual(log1.error_classification, "rate_limit")
            self.assertEqual(log1.attempt_no, 1)
            self.assertEqual(log1.attempt_role, "primary")

            self.assertEqual(log2.provider, "OpenAI")
            self.assertEqual(log2.model, "gpt-4o-mini")
            self.assertEqual(log2.status, "success")
            self.assertEqual(log2.attempt_no, 2)
            self.assertEqual(log2.attempt_role, "fallback")

            self.assertIsNotNone(log1.request_group_id)
            self.assertEqual(log1.request_group_id, log2.request_group_id)

        # 2. Verify automatically generated fallback alert
        alerts = get_captured_test_alerts()
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["type"], "fallback_used")
        text = alert["text"]

        self.assertIn(f"Telegram ID: <code>{user_id}</code>", text)
        self.assertIn(f"Chat ID: <code>{chat_id}</code>", text)
        self.assertIn("@alert_user", text)
        self.assertIn("Алексей Тестов", text)
        self.assertIn("Основной: DeepSeek / deepseek-chat ❌", text)
        self.assertIn("Резервный: OpenAI / gpt-4o-mini ✅", text)
        self.assertIn("rate_limit", text)
        self.assertIn(f"#{log1.id}", text)
        self.assertIn(f"#{log2.id}", text)

    async def test_scenario_b_primary_fail_fallback_fail(self):
        """Scenario B: Primary fail -> Fallback fail triggers terminal failure alert."""
        from ai_integration import get_ai_response_for_topic
        from database import AILog, async_session_maker
        from sqlalchemy import select
        from error_reporting import get_captured_test_alerts

        user_id = 880002
        chat_id = -100880002
        await self._setup_test_environment(user_id=user_id, allow_fallback=True)

        call_count = 0

        async def fake_generate_chat_response(cfg, client, model, messages):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                err = RuntimeError("500 Internal Server Error from DeepSeek")
                err.status_code = 500
                raise err
            else:
                err = TimeoutError("OpenAI connection timed out after 30 seconds")
                raise err

        with patch("ai_integration._generate_chat_response", side_effect=fake_generate_chat_response):
            result = await get_ai_response_for_topic(
                user_id=user_id,
                user_prompt="Помоги с дыханием",
                user_name="Алексей Тестов",
                chat_id=chat_id,
            )

        self.assertIn("Произошла ошибка при генерации ответа", result)
        self.assertEqual(call_count, 2)

        # 1. Verify AILog entries
        async with async_session_maker() as session:
            logs = (
                await session.scalars(
                    select(AILog).where(AILog.user_id == user_id).order_by(AILog.id.asc())
                )
            ).all()
            self.assertEqual(len(logs), 2)
            log1, log2 = logs[0], logs[1]
            self.assertEqual(log1.status, "error")
            self.assertEqual(log1.provider, "DeepSeek")
            self.assertEqual(log1.model, "deepseek-chat")
            self.assertEqual(log1.error_classification, "provider_5xx")

            self.assertEqual(log2.status, "error")
            self.assertEqual(log2.provider, "OpenAI")
            self.assertEqual(log2.model, "gpt-4o-mini")
            self.assertEqual(log2.error_classification, "timeout")

        # 2. Verify terminal failure alert
        alerts = get_captured_test_alerts()
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["type"], "terminal_ai_failure")
        text = alert["text"]

        self.assertIn(f"Telegram ID: <code>{user_id}</code>", text)
        self.assertIn(f"Chat ID: <code>{chat_id}</code>", text)
        self.assertIn("@alert_user", text)
        self.assertIn("Алексей Тестов", text)
        self.assertIn("Primary: DeepSeek / deepseek-chat ❌", text)
        self.assertIn("Fallback: OpenAI / gpt-4o-mini ❌", text)
        self.assertIn("provider_5xx", text)
        self.assertIn("timeout", text)
        self.assertIn(f"#{log1.id}", text)
        self.assertIn(f"#{log2.id}", text)

    async def test_scenario_c_allow_fallback_false(self):
        """Scenario C: allow_fallback=False produces only 1 call, 1 AILog, terminal alert for primary only."""
        from ai_integration import get_ai_response_for_topic
        from database import AILog, async_session_maker
        from sqlalchemy import select
        from error_reporting import get_captured_test_alerts

        user_id = 880003
        chat_id = -100880003
        await self._setup_test_environment(user_id=user_id, allow_fallback=False)

        call_count = 0

        async def fake_generate_chat_response(cfg, client, model, messages):
            nonlocal call_count
            call_count += 1
            err = RuntimeError("DeepSeek primary connection refused")
            raise err

        with patch("ai_integration._generate_chat_response", side_effect=fake_generate_chat_response):
            result = await get_ai_response_for_topic(
                user_id=user_id,
                user_prompt="Тестовый вопрос",
                user_name="Алексей Тестов",
                chat_id=chat_id,
            )

        self.assertIn("Произошла ошибка при генерации ответа", result)
        # Exactly one provider call
        self.assertEqual(call_count, 1)

        # Exactly one AILog
        async with async_session_maker() as session:
            logs = (
                await session.scalars(
                    select(AILog).where(AILog.user_id == user_id).order_by(AILog.id.asc())
                )
            ).all()
            self.assertEqual(len(logs), 1)
            log1 = logs[0]
            self.assertEqual(log1.attempt_role, "primary")
            self.assertEqual(log1.attempt_no, 1)

        # Terminal alert contains only primary, no fallback
        alerts = get_captured_test_alerts()
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["type"], "terminal_ai_failure")
        text = alert["text"]

        self.assertIn(f"Telegram ID: <code>{user_id}</code>", text)
        self.assertIn(f"Chat ID: <code>{chat_id}</code>", text)
        self.assertIn("DeepSeek", text)
        self.assertNotIn("Fallback: OpenAI", text)
        self.assertIn(f"AI Log: #{log1.id}", text)
