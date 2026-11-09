import os
import sys
import socket
import pytest

# Ensure testing isolation environment variables are set immediately before any project imports
os.environ["TESTING"] = "1"
os.environ["BOT_TOKEN"] = "000000000:TEST_BOT_TOKEN"
os.environ["OWNER_IDS"] = "999999999"
os.environ["ADMIN_IDS"] = "999999999"

# -----------------------------------------------------------------------------
# GLOBAL PHYSICAL NETWORK GUARD FOR TELEGRAM API
# Prevents any live HTTP/TCP traffic to api.telegram.org during test executions.
# -----------------------------------------------------------------------------
_orig_create_connection = socket.create_connection

def _guarded_create_connection(address, *args, **kwargs):
    host = address[0] if isinstance(address, (list, tuple)) else str(address)
    if isinstance(host, str) and ("telegram.org" in host.lower() or "api.telegram" in host.lower()):
        raise RuntimeError(
            f"CRITICAL TEST NETWORK ISOLATION: Outgoing connection to Telegram ({host}) is strictly forbidden during tests!"
        )
    return _orig_create_connection(address, *args, **kwargs)

socket.create_connection = _guarded_create_connection

try:
    import aiohttp
    _orig_aiohttp_request = aiohttp.ClientSession._request

    async def _guarded_aiohttp_request(self, method, str_or_url, *args, **kwargs):
        url_str = str(str_or_url).lower()
        if "api.telegram.org" in url_str or "telegram.org" in url_str:
            raise RuntimeError(
                f"CRITICAL TEST NETWORK ISOLATION: HTTP request to Telegram API ({url_str}) is strictly forbidden during tests!"
            )
        return await _orig_aiohttp_request(self, method, str_or_url, *args, **kwargs)

    aiohttp.ClientSession._request = _guarded_aiohttp_request
except ImportError:
    pass


@pytest.fixture(autouse=True)
def ensure_test_environment_isolation(monkeypatch):
    """Enforces strict test isolation across all test runs:
    - Never loads production .env tokens
    - Forces dummy bot token and dummy admin IDs
    - Resets all alert cooldowns and captured alert buffers before and after each test
    """
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("BOT_TOKEN", "000000000:TEST_BOT_TOKEN")
    monkeypatch.setenv("OWNER_IDS", "999999999")
    monkeypatch.setenv("ADMIN_IDS", "999999999")

    from error_reporting import clear_captured_test_alerts, reset_alert_cooldowns_for_tests
    clear_captured_test_alerts()
    reset_alert_cooldowns_for_tests()

    yield

    clear_captured_test_alerts()
    reset_alert_cooldowns_for_tests()
