from unittest.mock import Mock, patch

import telegram_client


def test_create_telegram_bot_uses_proxy(monkeypatch):
    monkeypatch.setenv("TELEGRAM_PROXY", "http://relay.example:3128")
    session = Mock()

    with patch.object(telegram_client, "AiohttpSession", return_value=session) as session_factory:
        with patch.object(telegram_client, "Bot") as bot_factory:
            telegram_client.create_telegram_bot("1234567890:test-token")

    session_factory.assert_called_once_with(proxy="http://relay.example:3128")
    assert bot_factory.call_count == 1
    call_kwargs = bot_factory.call_args.kwargs
    assert call_kwargs["token"] == "1234567890:test-token"
    assert call_kwargs["session"] == session
    assert call_kwargs["default"].link_preview_is_disabled is True


def test_create_telegram_bot_disables_link_previews_by_default():
    bot = telegram_client.create_telegram_bot("1234567890:test-token")
    assert bot.default is not None
    assert bot.default.link_preview_is_disabled is True
    assert bot.default.link_preview.is_disabled is True
