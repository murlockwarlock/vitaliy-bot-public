import os

import deploy_telegram_healthcheck as healthcheck


class _Session:
    async def close(self):
        return None


class _SuccessfulBot:
    session = _Session()

    async def get_me(self):
        return type("TelegramUser", (), {"id": 123})()


class _FailingBot:
    session = _Session()

    async def get_me(self):
        raise RuntimeError("token and proxy must never be logged")


def test_healthcheck_success_uses_runtime_token_and_proxy(monkeypatch, capsys):
    runtime_env = {
        "BOT_TOKEN": "runtime-token",
        "TELEGRAM_PROXY": "http://proxy.example:8443",
    }
    calls = []

    def factory(token):
        calls.append((token, os.environ.get("TELEGRAM_PROXY")))
        return _SuccessfulBot()

    monkeypatch.setattr(healthcheck, "HEALTHCHECK_TIMEOUT_SECONDS", 0.1)
    assert healthcheck.run_healthcheck(
        "vitaliy_bot",
        ".",
        runtime_env_reader=lambda _: runtime_env,
        bot_factory=factory,
    )

    assert calls == [("runtime-token", "http://proxy.example:8443")]
    assert capsys.readouterr().out == "Telegram via configured proxy: OK\n"


def test_healthcheck_uses_app_runtime_token_when_pm2_snapshot_omits_it(monkeypatch, capsys):
    runtime_env = {"TELEGRAM_PROXY": "http://proxy.example:8443"}
    calls = []

    def factory(token):
        calls.append(token)
        return _SuccessfulBot()

    monkeypatch.setattr(healthcheck, "_load_app_runtime_token", lambda _: "config-token")
    assert healthcheck.run_healthcheck(
        "vitaliy_bot",
        ".",
        runtime_env_reader=lambda _: runtime_env,
        bot_factory=factory,
    )

    assert calls == ["config-token"]
    assert capsys.readouterr().out == "Telegram via configured proxy: OK\n"


def test_healthcheck_failure_retries_and_redacts_client_error(monkeypatch, capsys):
    runtime_env = {
        "BOT_TOKEN": "runtime-token",
        "TELEGRAM_PROXY": "http://proxy.example:8443",
    }
    calls = []

    def factory(token):
        calls.append(token)
        return _FailingBot()

    monkeypatch.setattr(healthcheck, "HEALTHCHECK_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(healthcheck, "HEALTHCHECK_RETRY_DELAY_SECONDS", 0)
    assert not healthcheck.run_healthcheck(
        "vitaliy_bot",
        ".",
        runtime_env_reader=lambda _: runtime_env,
        bot_factory=factory,
    )

    output = capsys.readouterr().out
    assert calls == ["runtime-token", "runtime-token"]
    assert output == "Telegram via configured proxy: FAIL (client_error)\n"
    assert "runtime-token" not in output
    assert "proxy.example" not in output
