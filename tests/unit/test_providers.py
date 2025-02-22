"""Provider adapter contract tests against recorded upstream response shapes."""

from __future__ import annotations

import json

import httpx
import pytest

from cost_firewall.config import ProviderConfig
from cost_firewall.errors import ProviderError
from cost_firewall.models import ChatRequest, Message
from cost_firewall.providers import (
    MAX_UPSTREAM_BYTES,
    AnthropicProvider,
    OpenAICompatibleProvider,
    build_adapter,
)

REQUEST = ChatRequest(
    model="model-a",
    messages=(
        Message(role="system", content="be brief"),
        Message(role="user", content="hello"),
    ),
    max_tokens=32,
    temperature=0.2,
    idempotent=True,
)

OPENAI_BODY = {
    "id": "chatcmpl-123",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hi there"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
}

ANTHROPIC_BODY = {
    "id": "msg_123",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "hi there"}],
    "usage": {"input_tokens": 9, "output_tokens": 2},
}


def client_for(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def openai_config(**overrides: object) -> ProviderConfig:
    values: dict[str, object] = {
        "id": "openai",
        "type": "openai_compatible",
        "base_url": "https://example.test/v1",
        "api_key_env": "GATEWAY_TEST_KEY",
    }
    values.update(overrides)
    return ProviderConfig(**values)  # type: ignore[arg-type]


def anthropic_config(**overrides: object) -> ProviderConfig:
    values: dict[str, object] = {
        "id": "anthropic",
        "type": "anthropic",
        "base_url": "https://example.test",
        "api_key_env": "GATEWAY_TEST_KEY",
    }
    values.update(overrides)
    return ProviderConfig(**values)  # type: ignore[arg-type]


def test_openai_adapter_parses_usage_and_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_TEST_KEY", "test-key")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OPENAI_BODY)

    adapter = OpenAICompatibleProvider(openai_config(), client_for(httpx.MockTransport(handler)))
    response = adapter.complete(REQUEST, "gpt-4o-mini", 5.0)
    assert response.content == "hi there"
    assert (response.input_tokens, response.output_tokens) == (9, 2)
    assert response.provider_request_id == "chatcmpl-123"
    sent = seen[0]
    assert str(sent.url) == "https://example.test/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer test-key"
    body = json.loads(sent.content)
    assert body["model"] == "gpt-4o-mini"
    assert body["max_tokens"] == 32
    assert body["stream"] is False
    assert body["temperature"] == 0.2
    assert body["messages"][0] == {"role": "system", "content": "be brief"}


def test_openai_adapter_requires_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_TEST_KEY", "test-key")
    body = {**OPENAI_BODY, "usage": None}
    adapter = OpenAICompatibleProvider(
        openai_config(),
        client_for(httpx.MockTransport(lambda _: httpx.Response(200, json=body))),
    )
    with pytest.raises(ProviderError) as excinfo:
        adapter.complete(REQUEST, "gpt-4o-mini", 5.0)
    assert excinfo.value.error_class == "permanent"
    assert "usage" in excinfo.value.message


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, "transient"),
        (500, "transient"),
        (503, "transient"),
        (400, "permanent"),
        (401, "permanent"),
    ],
)
def test_openai_adapter_classifies_status(
    monkeypatch: pytest.MonkeyPatch, status: int, expected: str
) -> None:
    monkeypatch.setenv("GATEWAY_TEST_KEY", "test-key")
    adapter = OpenAICompatibleProvider(
        openai_config(),
        client_for(
            httpx.MockTransport(
                lambda _: httpx.Response(status, json={"error": {"message": "nope"}})
            )
        ),
    )
    with pytest.raises(ProviderError) as excinfo:
        adapter.complete(REQUEST, "gpt-4o-mini", 5.0)
    assert excinfo.value.error_class == expected
    assert excinfo.value.status_code == status


def test_openai_adapter_maps_timeouts_to_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_TEST_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    adapter = OpenAICompatibleProvider(openai_config(), client_for(httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as excinfo:
        adapter.complete(REQUEST, "gpt-4o-mini", 1.0)
    assert excinfo.value.error_class == "transient"


def test_missing_credentials_is_permanent_without_a_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GATEWAY_TEST_KEY", raising=False)
    called = False

    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        nonlocal called
        called = True
        return httpx.Response(200, json=OPENAI_BODY)

    adapter = OpenAICompatibleProvider(openai_config(), client_for(httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as excinfo:
        adapter.complete(REQUEST, "gpt-4o-mini", 5.0)
    assert excinfo.value.error_class == "permanent"
    assert called is False


def test_anthropic_adapter_moves_system_prompt_and_parses_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_TEST_KEY", "test-key")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ANTHROPIC_BODY)

    adapter = AnthropicProvider(anthropic_config(), client_for(httpx.MockTransport(handler)))
    response = adapter.complete(REQUEST, "claude-3-5-haiku-20241022", 5.0)
    assert response.content == "hi there"
    assert (response.input_tokens, response.output_tokens) == (9, 2)
    sent = seen[0]
    assert str(sent.url) == "https://example.test/v1/messages"
    assert sent.headers["x-api-key"] == "test-key"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(sent.content)
    assert body["system"] == "be brief"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["max_tokens"] == 32


def test_anthropic_adapter_rejects_system_only_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_TEST_KEY", "test-key")
    adapter = AnthropicProvider(
        anthropic_config(),
        client_for(httpx.MockTransport(lambda _: httpx.Response(200, json=ANTHROPIC_BODY))),
    )
    system_only = ChatRequest(
        model="m",
        messages=(Message(role="system", content="only system"),),
        max_tokens=8,
        temperature=None,
        idempotent=True,
    )
    with pytest.raises(ProviderError) as excinfo:
        adapter.complete(system_only, "claude", 5.0)
    assert excinfo.value.error_class == "permanent"


def test_upstream_error_message_is_truncated_and_does_not_leak_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_TEST_KEY", "test-key")
    long_message = "x" * 5000
    adapter = OpenAICompatibleProvider(
        openai_config(),
        client_for(
            httpx.MockTransport(
                lambda _: httpx.Response(400, json={"error": {"message": long_message}})
            )
        ),
    )
    with pytest.raises(ProviderError) as excinfo:
        adapter.complete(REQUEST, "gpt-4o-mini", 5.0)
    assert len(excinfo.value.message) < 400


def test_build_adapter_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        build_adapter(ProviderConfig(id="x", type="mystery"))


def test_fake_provider_is_deterministic() -> None:
    adapter = build_adapter(ProviderConfig(id="fake", type="fake"))
    first = adapter.complete(REQUEST, "m", 1.0)
    second = adapter.complete(REQUEST, "m", 1.0)
    assert first.content == second.content
    assert first.input_tokens == second.input_tokens
    assert first.output_tokens == second.output_tokens
    assert first.provider_request_id != second.provider_request_id


def test_oversized_upstream_body_is_a_permanent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_TEST_KEY", "test-key")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_UPSTREAM_BYTES + 1))

    adapter = OpenAICompatibleProvider(openai_config(), client_for(httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as excinfo:
        adapter.complete(REQUEST, "model-a", 5.0)
    assert excinfo.value.error_class == "permanent"
    assert "exceeded" in excinfo.value.message
