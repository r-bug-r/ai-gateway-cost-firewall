"""Provider adapters behind one narrow interface.

Every adapter returns `ProviderResponse` with token counts it can actually
evidence. Failures raise `ProviderError` with a classification, because the
router — not the adapter — decides whether fallback is permitted.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Protocol

import httpx

from cost_firewall.config import ProviderConfig
from cost_firewall.errors import ProviderError
from cost_firewall.models import ChatRequest, ProviderResponse
from cost_firewall.tokens import estimate_message_tokens, estimate_text_tokens

_ANTHROPIC_VERSION = "2023-06-01"
# Completions are small; a larger body is either a misconfiguration or a hostile
# upstream. Bound the read so one provider cannot exhaust gateway memory.
MAX_UPSTREAM_BYTES = 4 * 1024 * 1024


class ProviderAdapter(Protocol):
    id: str
    kind: str

    def complete(self, request: ChatRequest, model: str, timeout: float) -> ProviderResponse: ...

    def count_tokens(self, request: ChatRequest) -> int: ...


def _status_class(status: int) -> str:
    """Classify an HTTP status for fallback purposes.

    408/429/5xx are transient: the request may be retried elsewhere because a chat
    completion is idempotent. 4xx (except 408/429) are permanent request errors that
    another provider would reject identically.
    """
    if status in {408, 409, 425, 429} or status >= 500:
        return "transient"
    return "permanent"


def _post_bounded(
    provider_id: str,
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> httpx.Response:
    """POST and read at most `MAX_UPSTREAM_BYTES`, classifying transport failures."""
    try:
        with client.stream("POST", url, json=payload, headers=headers, timeout=timeout) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_UPSTREAM_BYTES:
                    raise ProviderError(
                        provider_id,
                        f"Upstream response exceeded {MAX_UPSTREAM_BYTES} bytes",
                        error_class="permanent",
                        status_code=response.status_code,
                    )
            return httpx.Response(
                response.status_code, headers=response.headers, content=bytes(body)
            )
    except httpx.TimeoutException as exc:
        raise ProviderError(
            provider_id, f"Timeout after {timeout}s", error_class="transient"
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderError(
            provider_id, f"Transport error: {type(exc).__name__}", error_class="transient"
        ) from exc


class FakeProvider:
    """Deterministic provider used by every default test and the smoke scenario.

    `fail_first` simulates transient or permanent failures without network access.
    """

    kind = "fake"

    def __init__(self, config: ProviderConfig):
        self.id = config.id
        self._config = config
        self._calls = 0

    def complete(self, request: ChatRequest, model: str, timeout: float) -> ProviderResponse:
        self._calls += 1
        if self._config.latency_ms:
            time.sleep(self._config.latency_ms / 1000)
        if self._calls <= self._config.fail_first:
            raise ProviderError(
                self.id,
                f"Simulated {self._config.fail_class} failure "
                f"(call {self._calls} of {self._config.fail_first})",
                error_class=self._config.fail_class,
                status_code=self._config.fail_status,
            )
        input_tokens = self.count_tokens(request)
        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == "user"),
            "",
        )
        if self._config.response_text is not None:
            text = self._config.response_text
        else:
            text = f"[fake:{self.id}:{model}] {last_user[:200]}".strip()
        output_tokens = max(1, min(request.max_tokens, estimate_text_tokens(text)))
        return ProviderResponse(
            content=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider_request_id=f"fake-{self.id}-{self._calls}",
        )

    def count_tokens(self, request: ChatRequest) -> int:
        return estimate_message_tokens(request.messages)


class OpenAICompatibleProvider:
    """Any `/v1/chat/completions` endpoint, including local OpenAI-compatible servers."""

    kind = "openai_compatible"

    def __init__(self, config: ProviderConfig, client: httpx.Client | None = None):
        self.id = config.id
        self._config = config
        self._client = client or httpx.Client()

    def _api_key(self) -> str | None:
        if not self._config.api_key_env:
            return None
        value = os.environ.get(self._config.api_key_env)
        if not value:
            raise ProviderError(
                self.id,
                f"Missing credentials: environment variable {self._config.api_key_env} is unset",
                error_class="permanent",
                status_code=None,
            )
        return value

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        key = self._api_key()
        if key:
            headers["authorization"] = f"Bearer {key}"
        return headers

    def complete(self, request: ChatRequest, model: str, timeout: float) -> ProviderResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        url = f"{(self._config.base_url or '').rstrip('/')}/chat/completions"
        response = self._post(url, payload, timeout)
        if response.status_code >= 400:
            raise ProviderError(
                self.id,
                _safe_error_message(response),
                error_class=_status_class(response.status_code),
                status_code=response.status_code,
            )
        return _parse_openai_response(self.id, response.json(), request)

    def count_tokens(self, request: ChatRequest) -> int:
        return estimate_message_tokens(request.messages)

    def _post(self, url: str, payload: dict[str, Any], timeout: float) -> httpx.Response:
        return _post_bounded(self.id, self._client, url, payload, self._headers(), timeout)


class AnthropicProvider:
    """Anthropic Messages API adapter (`/v1/messages`)."""

    kind = "anthropic"

    def __init__(self, config: ProviderConfig, client: httpx.Client | None = None):
        self.id = config.id
        self._config = config
        self._client = client or httpx.Client()

    def _headers(self) -> dict[str, str]:
        env_name = self._config.api_key_env
        key = os.environ.get(env_name) if env_name else None
        if not key:
            raise ProviderError(
                self.id,
                f"Missing credentials: environment variable {env_name} is unset",
                error_class="permanent",
            )
        return {
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }

    def complete(self, request: ChatRequest, model: str, timeout: float) -> ProviderResponse:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        turns = [
            {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
        ]
        if not turns:
            raise ProviderError(
                self.id,
                "Anthropic requires at least one non-system message",
                error_class="permanent",
                status_code=400,
            )
        payload: dict[str, Any] = {
            "model": model,
            "messages": turns,
            "max_tokens": request.max_tokens,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        url = f"{(self._config.base_url or '').rstrip('/')}/v1/messages"
        response = self._post(url, payload, timeout)
        if response.status_code >= 400:
            raise ProviderError(
                self.id,
                _safe_error_message(response),
                error_class=_status_class(response.status_code),
                status_code=response.status_code,
            )
        return _parse_anthropic_response(self.id, response.json(), request)

    def count_tokens(self, request: ChatRequest) -> int:
        return estimate_message_tokens(request.messages)

    def _post(self, url: str, payload: dict[str, Any], timeout: float) -> httpx.Response:
        return _post_bounded(self.id, self._client, url, payload, self._headers(), timeout)


def _safe_error_message(response: httpx.Response) -> str:
    """Extract an upstream error message without ever echoing request content."""
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return f"Upstream returned HTTP {response.status_code}"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return f"Upstream HTTP {response.status_code}: {message[:300]}"
        detail = body.get("message")
        if isinstance(detail, str) and detail:
            return f"Upstream HTTP {response.status_code}: {detail[:300]}"
    return f"Upstream returned HTTP {response.status_code}"


def _require_usage(provider: str, body: object) -> tuple[int, int]:
    """Provider-reported usage is mandatory; without it a request cannot be billed."""
    if not isinstance(body, dict):
        raise ProviderError(
            provider, "Upstream returned a non-object body", error_class="permanent"
        )
    usage = body.get("usage")
    if not isinstance(usage, dict):
        raise ProviderError(
            provider,
            "Upstream response is missing usage; refusing to bill an unverifiable request",
            error_class="permanent",
        )
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        raise ProviderError(
            provider,
            "Upstream usage fields are not integers; refusing to bill an unverifiable request",
            error_class="permanent",
        )
    if input_tokens < 0 or output_tokens < 0:
        raise ProviderError(provider, "Upstream reported negative usage", error_class="permanent")
    return input_tokens, output_tokens


def _parse_openai_response(provider: str, body: object, request: ChatRequest) -> ProviderResponse:
    input_tokens, output_tokens = _require_usage(provider, body)
    assert isinstance(body, dict)
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError(provider, "Upstream response has no choices", error_class="permanent")
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ProviderError(
            provider, "Upstream response has no text content", error_class="permanent"
        )
    request_id = body.get("id")
    return ProviderResponse(
        content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider_request_id=request_id if isinstance(request_id, str) else None,
    )


def _parse_anthropic_response(
    provider: str, body: object, request: ChatRequest
) -> ProviderResponse:
    input_tokens, output_tokens = _require_usage(provider, body)
    assert isinstance(body, dict)
    blocks = body.get("content")
    if not isinstance(blocks, list):
        raise ProviderError(
            provider, "Upstream response has no content blocks", error_class="permanent"
        )
    text = "".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )
    if not text:
        raise ProviderError(
            provider, "Upstream response has no text content", error_class="permanent"
        )
    request_id = body.get("id")
    return ProviderResponse(
        content=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider_request_id=request_id if isinstance(request_id, str) else None,
    )


def build_adapter(config: ProviderConfig, client: httpx.Client | None = None) -> ProviderAdapter:
    if config.type == "fake":
        return FakeProvider(config)
    if config.type == "openai_compatible":
        return OpenAICompatibleProvider(config, client)
    if config.type == "anthropic":
        return AnthropicProvider(config, client)
    raise ValueError(f"Unsupported provider type {config.type}")
