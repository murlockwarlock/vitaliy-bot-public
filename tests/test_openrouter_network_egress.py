import os
from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest

from ai_integration import (
    build_openrouter_client,
    resolve_openrouter_proxy,
    _prepare_clients,
    _prepare_fallback_client,
)
from database import AIConfig


def test_openrouter_proxy_precedence(monkeypatch):
    """Test OPENROUTER_PROXY > HTTPS_PROXY > HTTP_PROXY > direct."""
    # 1. OPENROUTER_PROXY wins when all are set
    monkeypatch.setenv("OPENROUTER_PROXY", "http://openrouter:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://https:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://http:3128")
    assert resolve_openrouter_proxy() == "http://openrouter:3128"

    # 2. HTTPS_PROXY fallback
    monkeypatch.delenv("OPENROUTER_PROXY")
    assert resolve_openrouter_proxy() == "http://https:3128"

    # 3. HTTP_PROXY fallback
    monkeypatch.delenv("HTTPS_PROXY")
    assert resolve_openrouter_proxy() == "http://http:3128"

    # 4. Direct when none are set
    monkeypatch.delenv("HTTP_PROXY")
    assert resolve_openrouter_proxy() is None


def test_build_openrouter_client_proxied(monkeypatch):
    """Verify underlying httpx client gets explicit transport and trust_env=False."""
    monkeypatch.setenv("OPENROUTER_PROXY", "http://user:test@example.invalid:3128")
    client = build_openrouter_client("sk-test-key")
    http_client = client._client
    assert isinstance(http_client, httpx.AsyncClient)
    assert http_client.trust_env is False
    transport = getattr(http_client, "_transport", None)
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    pool = getattr(transport, "_pool", None)
    proxy_url = getattr(pool, "_proxy_url", None)
    assert proxy_url is not None
    assert "example.invalid" in str(proxy_url)
    assert "3128" in str(proxy_url)


def test_build_openrouter_client_direct(monkeypatch):
    """Verify underlying httpx client is direct with trust_env=False when no proxy configured."""
    monkeypatch.delenv("OPENROUTER_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    client = build_openrouter_client("sk-test-key")
    http_client = client._client
    assert isinstance(http_client, httpx.AsyncClient)
    assert http_client.trust_env is False
    transport = getattr(http_client, "_transport", None)
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    pool = getattr(transport, "_pool", None)
    proxy_url = getattr(pool, "_proxy_url", None)
    assert proxy_url is None


@pytest.mark.asyncio
async def test_openrouter_primary_uses_intended_proxy(monkeypatch):
    """Verify _prepare_clients applies intended proxy to primary OpenRouter client."""
    monkeypatch.setenv("OPENROUTER_PROXY", "http://primary-proxy.test:3128")

    class FakeSession:
        async def get(self, model, ident):
            return AIConfig(
                id=1,
                provider="OpenRouter",
                openrouter_api_key="sk-or-primary-key",
                openrouter_model="openai/gpt-5.6-terra",
            )

    cfg, emb, chat_client, model_name, err = await _prepare_clients(FakeSession())
    assert err is None
    assert chat_client is not None
    assert chat_client._client.trust_env is False
    transport = getattr(chat_client._client, "_transport", None)
    pool = getattr(transport, "_pool", None)
    proxy_url = getattr(pool, "_proxy_url", None)
    assert "primary-proxy.test" in str(proxy_url)
    assert "3128" in str(proxy_url)


@pytest.mark.asyncio
async def test_openrouter_fallback_uses_intended_proxy(monkeypatch):
    """Verify _prepare_fallback_client applies identical proxy to fallback OpenRouter client."""
    monkeypatch.setenv("OPENROUTER_PROXY", "http://fallback-proxy.test:3128")

    config = AIConfig(
        id=1,
        provider="DeepSeek",
        allow_fallback=True,
        fallback_provider="OpenRouter",
        fallback_model="openai/gpt-5.6-terra",
        openrouter_api_key="sk-or-fallback-key",
        openrouter_model="openai/gpt-5.6-terra",
    )

    fb_prov, fb_model, fb_client = _prepare_fallback_client(config)
    assert fb_prov == "OpenRouter"
    assert fb_client is not None
    assert fb_client._client.trust_env is False
    transport = getattr(fb_client._client, "_transport", None)
    pool = getattr(transport, "_pool", None)
    proxy_url = getattr(pool, "_proxy_url", None)
    assert "fallback-proxy.test" in str(proxy_url)
    assert "3128" in str(proxy_url)


@pytest.mark.asyncio
async def test_openrouter_proxy_failure_fails_closed_no_silent_direct(monkeypatch):
    """Simulate proxy connection failure and verify fail-closed behavior (no silent direct)."""
    monkeypatch.setenv("OPENROUTER_PROXY", "http://unreachable-proxy.local:3128")
    attempts = 0

    def mock_proxy_failure(request: httpx.Request):
        nonlocal attempts
        attempts += 1
        raise httpx.ProxyError("Unable to connect to proxy", request=request)

    mock_transport = httpx.MockTransport(mock_proxy_failure)
    client = build_openrouter_client("sk-test-key", transport=mock_transport, max_retries=0)

    with pytest.raises(openai.APIConnectionError) as exc_info:
        await client.chat.completions.create(
            model="openai/gpt-5.6-terra",
            messages=[{"role": "user", "content": "Hello"}],
        )

    assert attempts == 1  # Exactly 1 attempt, zero silent retries to direct!
    assert isinstance(exc_info.value.__cause__, httpx.ProxyError)
    assert "Unable to connect to proxy" in str(exc_info.value.__cause__)
