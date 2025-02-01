"""Canonical request/response models for the supported API subset."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cost_firewall.errors import ValidationError

ALLOWED_ROLES = frozenset({"system", "user", "assistant"})
# `idempotent` is a documented gateway extension, not part of the OpenAI schema:
# it is the client's statement that a retry on another provider cannot duplicate
# a side effect. It defaults to true because chat completions are side-effect free.
SUPPORTED_FIELDS = frozenset(
    {"model", "messages", "max_tokens", "temperature", "stream", "idempotent"}
)


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: tuple[Message, ...]
    max_tokens: int
    temperature: float | None = None
    # Chat completions have no side effects, so a transient failure may be retried
    # on a different provider. This flag is the single source of truth for fallback.
    idempotent: bool = True


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    input_tokens: int
    output_tokens: int
    provider_request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttemptOutcome:
    attempt: int
    provider: str
    upstream_model: str
    status: str
    success: bool
    error_class: str | None
    input_tokens: int
    output_tokens: int
    cost_micros: int
    latency_ms: int


@dataclass(frozen=True)
class ChatResult:
    request_id: str
    response: ProviderResponse
    provider: str
    upstream_model: str
    cost_micros: int
    attempts: tuple[AttemptOutcome, ...]


def parse_chat_request(
    payload: object,
    *,
    max_messages: int,
    max_input_chars: int,
    max_output_tokens: int,
    default_max_output_tokens: int,
) -> ChatRequest:
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object.")

    unsupported = sorted(set(payload) - SUPPORTED_FIELDS)
    if unsupported:
        raise ValidationError(
            f"Unsupported request field(s): {', '.join(unsupported)}. "
            "Supported fields: model, messages, max_tokens, temperature, stream, idempotent.",
            param=unsupported[0],
        )

    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValidationError("'model' must be a non-empty string.", param="model")

    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ValidationError("'messages' must be a non-empty array.", param="messages")
    if len(raw_messages) > max_messages:
        raise ValidationError(
            f"'messages' exceeds the {max_messages} message limit.", param="messages"
        )

    messages: list[Message] = []
    total_chars = 0
    for index, raw in enumerate(raw_messages):
        if not isinstance(raw, dict):
            raise ValidationError(f"messages[{index}] must be an object.", param="messages")
        role = raw.get("role")
        if role not in ALLOWED_ROLES:
            raise ValidationError(
                f"messages[{index}].role must be one of: system, user, assistant.",
                param=f"messages[{index}].role",
            )
        content = raw.get("content")
        if not isinstance(content, str):
            raise ValidationError(
                f"messages[{index}].content must be a string; multimodal content is not supported.",
                param=f"messages[{index}].content",
            )
        total_chars += len(content)
        messages.append(Message(role=str(role), content=content))
    if total_chars > max_input_chars:
        raise ValidationError(
            f"Input text exceeds the {max_input_chars} character limit.", param="messages"
        )

    stream = payload.get("stream", False)
    if stream is not False:
        raise ValidationError(
            "Streaming responses are not supported by this gateway version.",
            param="stream",
        )

    max_tokens = payload.get("max_tokens", default_max_output_tokens)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValidationError("'max_tokens' must be a positive integer.", param="max_tokens")
    if max_tokens > max_output_tokens:
        raise ValidationError(f"'max_tokens' must be <= {max_output_tokens}.", param="max_tokens")

    temperature = payload.get("temperature")
    if temperature is not None:
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValidationError("'temperature' must be a number.", param="temperature")
        temperature = float(temperature)
        if not 0.0 <= temperature <= 2.0:
            raise ValidationError("'temperature' must be between 0 and 2.", param="temperature")

    idempotent = payload.get("idempotent", True)
    if not isinstance(idempotent, bool):
        raise ValidationError("'idempotent' must be a boolean.", param="idempotent")

    return ChatRequest(
        model=model,
        messages=tuple(messages),
        max_tokens=max_tokens,
        idempotent=idempotent,
        temperature=temperature,
    )
