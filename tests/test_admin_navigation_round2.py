from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import handlers
import keyboards as kb
from database import AdminNavigationContext, BotConfig, PaymentConfig, async_session_maker, init_db
from tests.test_pr20_admin_behavioral import _callback as mock_callback
from tests.test_pr20_admin_behavioral import _callbacks, _cleanup_users, _ensure_admin, _state


ADMIN_ID = 98273901


def _callback(data: str, message_id: int = 1, chat_id: int = ADMIN_ID):
    return mock_callback(ADMIN_ID, data, chat_id=chat_id, message_id=message_id)


def _input_message(text: str):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=ADMIN_ID),
        chat=SimpleNamespace(id=ADMIN_ID),
        text=text,
        delete=AsyncMock(),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_payment_children_return_to_unified_payment_menu():
    await init_db()
    await _ensure_admin(ADMIN_ID)

    for callback_data, handler in (
        ("admin_subs_list_0_all", handlers.admin_subscribers),
        ("admin_plog_0_all", handlers.admin_payment_log),
        ("admin_payment_stats:admin_payment_settings", handlers.admin_payment_stats),
    ):
        callback = _callback(callback_data)
        await handler(callback, _state(ADMIN_ID))
        markup = callback.message.edit_text.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[-1][0].callback_data == "admin_payment_settings"


@pytest.mark.asyncio
async def test_legacy_section_callbacks_open_working_screens_directly():
    await init_db()
    await _ensure_admin(ADMIN_ID)
    for legacy_callback, expected_marker in (
        ("admin_section_clients", "Список клиентов"),
        ("admin_section_admins", "Администраторы"),
        ("admin_section_payments", "admin_manage_plans"),
    ):
        callback = _callback(legacy_callback)
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as render:
            await handlers.admin_section_menu(callback, _state(ADMIN_ID))
        text = render.call_args.args[1]
        markup = render.call_args.args[3]
        assert expected_marker in text or expected_marker in _callbacks(markup)


@pytest.mark.asyncio
async def test_root_entries_and_payment_settings_follow_one_step_back_route():
    await init_db()
    await _ensure_admin(ADMIN_ID)

    clients = _callback("admin_clients_list_0")
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as clients_render:
        await handlers.admin_clients_list(clients, _state(ADMIN_ID))
    assert "Список клиентов" in clients_render.call_args.args[1]

    admins = _callback("admin_manage_admins")
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as admins_render:
        await handlers.admin_manage_admins(admins, _state(ADMIN_ID))
    assert "Администраторы" in admins_render.call_args.args[1]

    payment = _callback("admin_payment_settings")
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as payment_render:
        await handlers.admin_payment_settings(payment, _state(ADMIN_ID))
    assert "admin_prodamus_settings" in _callbacks(payment_render.call_args.args[3])

    settings = _callback("admin_prodamus_settings")
    await handlers.admin_prodamus_settings(settings, _state(ADMIN_ID))
    prices_data = "admin_pay_prices__admin_prodamus_settings"
    prices = _callback(prices_data)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as prices_render:
        await handlers.admin_pay_prices(prices, _state(ADMIN_ID))
    assert prices_render.call_args.args[3].inline_keyboard[-1][0].callback_data == "admin_prodamus_settings"

    await _cleanup_users(ADMIN_ID)


@pytest.mark.asyncio
async def test_legacy_clients_entry_does_not_inherit_another_list_parent():
    await init_db()
    await _ensure_admin(ADMIN_ID)
    state = _state(ADMIN_ID)
    await state.update_data(admin_client_parent_callback="admin_clients_list_7")
    callback = _callback("admin_section_clients", message_id=712)

    with patch("handlers.send_smart_content", new_callable=AsyncMock) as render:
        await handlers.admin_section_menu(callback, state)

    back_data = render.call_args.args[3].inline_keyboard[-1][0].callback_data
    token = back_data.removeprefix("admin_clients_back:")
    async with async_session_maker() as session:
        context = await session.get(AdminNavigationContext, token.removeprefix("clientctx_"))
        assert context.parent_callback == "admin_panel"
    await _cleanup_users(ADMIN_ID)


@pytest.mark.asyncio
async def test_legacy_payment_menu_and_statistics_sources_are_normalized():
    await init_db()
    await _ensure_admin(ADMIN_ID)

    legacy_menu = _callback("admin_subscriptions")
    await handlers.admin_subscriptions_menu(legacy_menu, _state(ADMIN_ID))
    assert _callbacks(legacy_menu.message.edit_text.call_args.kwargs["reply_markup"]) == [
        "admin_manage_plans",
        "admin_subs_list_0_all",
        "admin_promocodes",
        "admin_payment_stats:admin_payment_settings",
        "admin_plog_0_all",
        "admin_referral_settings",
        "admin_prodamus_settings",
        "admin_panel",
    ]

    for source, expected_back in (
        ("admin_payment_settings", "admin_payment_settings"),
        ("admin_subscriptions", "admin_payment_settings"),
        ("admin_prodamus_settings", "admin_prodamus_settings"),
    ):
        callback = _callback(f"admin_payment_stats:{source}")
        await handlers.admin_payment_stats(callback, _state(ADMIN_ID))
        markup = callback.message.edit_text.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[-1][0].callback_data == expected_back


@pytest.mark.asyncio
async def test_payment_settings_exposes_technical_actions_with_source_context():
    await init_db()
    await _ensure_admin(ADMIN_ID)
    callback = _callback("admin_prodamus_settings")
    await handlers.admin_prodamus_settings(callback, _state(ADMIN_ID))
    markup = callback.message.edit_text.call_args.kwargs["reply_markup"]
    callbacks = _callbacks(markup)
    assert "admin_pay_mode__admin_prodamus_settings" in callbacks
    assert "admin_prodamus_keys_from_settings" in callbacks
    assert "admin_pay_providers_0__admin_prodamus_settings" in callbacks
    assert "admin_pay_prices__admin_prodamus_settings" in callbacks
    assert "admin_webhook_settings__admin_prodamus_settings" in callbacks
    assert "edit_cont_pay:subscription_info__admin_prodamus_settings" in callbacks
    assert callbacks[-1] == "admin_payment_settings"


def test_client_list_back_button_is_bound_to_its_navigation_token():
    token = "clientctx_0123456789abcdef"
    markup = kb.admin_clients_keyboard([], page=2, total_pages=4, navigation_token=token)
    assert markup.inline_keyboard[-1][0].callback_data == f"admin_clients_back:{token}"


@pytest.mark.asyncio
async def test_client_profile_children_keep_the_message_context_token():
    await init_db()
    await _ensure_admin(ADMIN_ID)
    client_id = 98273902
    await _cleanup_users(client_id)
    async with async_session_maker() as session:
        from database import User

        session.add(User(id=client_id, first_name="Контекстный клиент"))
        await session.commit()

    state = _state(ADMIN_ID)
    list_callback = _callback("admin_clients_list_2", message_id=713, chat_id=713)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as render:
        await handlers.admin_clients_list(list_callback, state)
    profile_data = next(
        button.callback_data
        for row in render.call_args.args[3].inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith(f"admin_user_profile_{client_id}")
    )

    profile_callback = _callback(profile_data, message_id=713, chat_id=713)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as profile_render:
        await handlers.admin_user_profile(profile_callback, state)
    profile_markup = profile_render.call_args.args[3]
    history_data = next(
        button.callback_data
        for row in profile_markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("admin_history_view_")
    )
    assert ":clientctx_" in history_data

    history_callback = _callback(history_data, message_id=713, chat_id=713)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as history_render:
        await handlers.admin_history_view(history_callback, state)
    history_markup = history_render.call_args.args[3]
    profile_back = history_markup.inline_keyboard[-1][0].callback_data
    assert profile_back.endswith(history_data.split(":", 1)[1])

    await _cleanup_users(client_id, ADMIN_ID)


@pytest.mark.asyncio
async def test_payment_editors_keep_the_immediate_parent_screen():
    await init_db()
    await _ensure_admin(ADMIN_ID)
    state = _state(ADMIN_ID)

    providers = _callback("admin_pay_providers_2__admin_prodamus_settings")
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as providers_render:
        await handlers.admin_pay_providers(providers, state)
    add_callback_data = next(
        button.callback_data
        for row in providers_render.call_args.args[3].inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("add_prov_start")
    )
    add_callback = _callback(add_callback_data)
    await handlers.add_prov_start(add_callback, state)
    cancel_data = add_callback.message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert cancel_data == "admin_pay_providers_0__admin_prodamus_settings"

    prices = _callback("admin_pay_prices__admin_prodamus_settings")
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as prices_render:
        await handlers.admin_pay_prices(prices, state)
    edit_data = prices_render.call_args.args[3].inline_keyboard[0][0].callback_data
    edit_callback = _callback(edit_data)
    await handlers.edit_msg_cost(edit_callback, state)
    price_cancel = edit_callback.message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert price_cancel == "admin_pay_prices__admin_prodamus_settings"


@pytest.mark.asyncio
async def test_payment_toggles_return_to_payment_menu_after_rerender():
    await init_db()
    await _ensure_admin(ADMIN_ID)

    for callback_prefix, handler in (
        ("admin_toggle_sub_notifs", handlers.admin_toggle_sub_notifs),
        ("admin_toggle_subscriptions", handlers.admin_toggle_subscriptions),
    ):
        state = _state(ADMIN_ID)
        settings = _callback("admin_prodamus_settings")
        await handlers.admin_prodamus_settings(settings, state)
        toggle_data = next(
            button.callback_data
            for row in settings.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data and button.callback_data.startswith(f"{callback_prefix}__")
        )

        toggle = _callback(toggle_data)
        await handler(toggle, state)
        rerendered = toggle.message.edit_reply_markup.call_args.kwargs["reply_markup"]
        back_data = rerendered.inline_keyboard[-1][0].callback_data
        assert back_data == "admin_payment_settings"

        back = _callback(back_data)
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as payment_render:
            await handlers.admin_payment_settings(back, state)
        assert "Подписки и оплата" in payment_render.call_args.args[1]


@pytest.mark.asyncio
async def test_welcome_bonus_save_returns_to_payment_menu_after_settings_rerender():
    await init_db()
    await _ensure_admin(ADMIN_ID)
    state = _state(ADMIN_ID)

    settings = _callback("admin_prodamus_settings")
    await handlers.admin_prodamus_settings(settings, state)
    welcome_data = next(
        button.callback_data
        for row in settings.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("admin_set_welcome_bonus__")
    )
    welcome = _callback(welcome_data)
    await handlers.admin_set_welcome_bonus(welcome, state)

    message = _input_message("9")
    bot = AsyncMock()
    await handlers.process_welcome_bonus_days(message, state, bot)
    rerendered = bot.edit_message_text.call_args.kwargs["reply_markup"]
    back_data = rerendered.inline_keyboard[-1][0].callback_data
    assert back_data == "admin_payment_settings"

    back = _callback(back_data)
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as payment_render:
        await handlers.admin_payment_settings(back, state)
    assert "Подписки и оплата" in payment_render.call_args.args[1]


@pytest.mark.asyncio
async def test_token_price_save_keeps_settings_parent_for_message_resume():
    await init_db()
    await _ensure_admin(ADMIN_ID)
    state = _state(ADMIN_ID)
    async with async_session_maker() as session:
        config = await session.get(PaymentConfig, 1)
        original_cost = config.cost_per_message

    try:
        prices = _callback("admin_pay_prices__admin_prodamus_settings")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as prices_render:
            await handlers.admin_pay_prices(prices, state)
        edit_data = prices_render.call_args.args[3].inline_keyboard[0][0].callback_data
        edit = _callback(edit_data)
        await handlers.edit_msg_cost(edit, state)

        message = _input_message("2.5")
        with patch("handlers.send_smart_content", new_callable=AsyncMock) as resumed_render:
            await handlers.save_token_price(message, state)
        rerendered = resumed_render.call_args.args[3]
        back_data = rerendered.inline_keyboard[-1][0].callback_data
        assert back_data == "admin_prodamus_settings"

        back = _callback(back_data)
        await handlers.admin_prodamus_settings(back, state)
        assert "Настройки оплаты" in back.message.edit_text.call_args.args[0]
    finally:
        async with async_session_maker() as session:
            config = await session.get(PaymentConfig, 1)
            config.cost_per_message = original_cost
            await session.commit()


@pytest.mark.asyncio
async def test_webhook_saves_keep_settings_parent_for_message_rerender():
    await init_db()
    await _ensure_admin(ADMIN_ID)
    async with async_session_maker() as session:
        config = await session.get(BotConfig, 1)
        original_secret = config.webhook_secret
        original_port = config.webhook_port

    try:
        for action, value, start_prefix in (
            ("secret", "route-secret", "admin_set_webhook_secret"),
            ("port", "18081", "admin_set_webhook_port"),
        ):
            state = _state(ADMIN_ID)
            webhook = _callback("admin_webhook_settings__admin_prodamus_settings")
            with patch("handlers.send_smart_content", new_callable=AsyncMock) as webhook_render:
                await handlers.admin_webhook_settings(webhook, state)
            start_data = next(
                button.callback_data
                for row in webhook_render.call_args.args[3].inline_keyboard
                for button in row
                if button.callback_data and button.callback_data.startswith(f"{start_prefix}__")
            )
            start = _callback(start_data)
            if action == "secret":
                await handlers.admin_set_webhook_secret(start, state)
            else:
                await handlers.admin_set_webhook_port(start, state)

            message = _input_message(value)
            bot = AsyncMock()
            with patch("handlers.send_smart_content", new_callable=AsyncMock) as rerender:
                if action == "secret":
                    await handlers.save_webhook_secret(message, state, bot)
                else:
                    await handlers.save_webhook_port(message, state, bot)
            rerendered = rerender.call_args.args[3]
            back_data = rerendered.inline_keyboard[-1][0].callback_data
            assert back_data == "admin_prodamus_settings"

            back = _callback(back_data)
            await handlers.admin_prodamus_settings(back, state)
            assert "Настройки оплаты" in back.message.edit_text.call_args.args[0]
    finally:
        async with async_session_maker() as session:
            config = await session.get(BotConfig, 1)
            config.webhook_secret = original_secret
            config.webhook_port = original_port
            await session.commit()


@pytest.mark.asyncio
async def test_client_context_keeps_parent_per_message():
    await init_db()
    await _ensure_admin(ADMIN_ID)
    state = _state(ADMIN_ID)
    callback = mock_callback(ADMIN_ID, "admin_clients_list_2", chat_id=720, message_id=610)

    with patch("handlers.send_smart_content", new_callable=AsyncMock) as render:
        await handlers.admin_clients_list(callback, state)

    markup = render.call_args.args[3]
    back_data = markup.inline_keyboard[-1][0].callback_data
    token = back_data.removeprefix("admin_clients_back:")
    async with async_session_maker() as session:
        context = await session.get(AdminNavigationContext, token.removeprefix("clientctx_"))
        assert context.parent_callback == "admin_panel"
        assert context.chat_id == 720
        assert context.message_id == 610

    back_callback = mock_callback(
        ADMIN_ID,
        back_data,
        chat_id=720,
        message_id=610,
    )
    with patch("handlers.send_smart_content", new_callable=AsyncMock) as panel:
        await handlers.admin_clients_back(back_callback, _state(ADMIN_ID))
    assert panel.call_args.args[3].inline_keyboard[0][0].callback_data == "admin_clients_list_0"
    await _cleanup_users(ADMIN_ID)
