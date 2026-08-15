"""Redaction: patterns, secret shapes, opt-in forwarding, log sanitization."""

from __future__ import annotations

import pytest

from conftest import base_config
from cost_firewall.config import RedactionConfig, RedactionPattern, parse_config
from cost_firewall.errors import ConfigError
from cost_firewall.gateway import _redacted_request
from cost_firewall.models import ChatRequest, Message
from cost_firewall.redaction import LogRedactor, Redactor


@pytest.fixture()
def redactor() -> Redactor:
    return Redactor(RedactionConfig(redact_before_forward=False))


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("my key is sk-abcdefghijklmnopqrstuvwxyz012345", "sk-"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "Bearer"),
        ("card 4111 1111 1111 1111 expires soon", "4111"),
        ("mail me at alice@example.com", "alice@example.com"),
        ("aws AKIAIOSFODNN7EXAMPLE here", "AKIA"),
        ("token ghp_0123456789abcdefghijklmnopqrstuvwxyz", "ghp_"),
        ("-----BEGIN RSA PRIVATE KEY-----", "BEGIN RSA PRIVATE KEY"),
    ],
)
def test_secret_shapes_are_redacted(redactor: Redactor, text: str, needle: str) -> None:
    result = redactor.redact(text)
    assert needle not in result.text
    assert "[REDACTED:" in result.text
    assert result.total >= 1


def test_redaction_is_idempotent(redactor: Redactor) -> None:
    once = redactor.redact("key sk-abcdefghijklmnopqrstuvwxyz012345 end").text
    twice = redactor.redact(once)
    assert once == twice.text
    assert twice.total == 0


def test_plain_text_is_unchanged(redactor: Redactor) -> None:
    text = "Summarize this paragraph about quarterly planning."
    result = redactor.redact(text)
    assert result.text == text
    assert result.counts == {}


def test_custom_patterns_extend_builtin_rules() -> None:
    redactor = Redactor(
        RedactionConfig(
            redact_before_forward=False,
            patterns=(RedactionPattern(name="employee_id", regex=r"EMP-\d{6}"),),
        )
    )
    result = redactor.redact("badge EMP-123456")
    assert result.text == "badge [REDACTED:employee_id]"
    assert result.counts == {"employee_id": 1}


def test_message_redaction_preserves_roles_and_order(redactor: Redactor) -> None:
    request = ChatRequest(
        model="model-a",
        messages=(
            Message(role="system", content="be terse"),
            Message(role="user", content="key sk-abcdefghijklmnopqrstuvwxyz012345"),
        ),
        max_tokens=16,
        temperature=None,
        idempotent=True,
    )
    redacted, counts = _redacted_request(request, redactor)
    assert counts == {"openai_key": 1}
    assert [message.role for message in redacted.messages] == ["system", "user"]
    assert redacted.messages[0].content == "be terse"
    assert "sk-" not in redacted.messages[1].content
    assert redacted.model == request.model
    assert redacted.max_tokens == request.max_tokens


def test_log_redactor_scrubs_nested_structures(redactor: Redactor) -> None:
    log_redactor = LogRedactor(redactor)
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": "key sk-abcdefghijklmnopqrstuvwxyz012345"}],
        "count": 3,
    }
    scrubbed = log_redactor.redact_value(payload)
    assert isinstance(scrubbed, dict)
    assert "sk-" not in str(scrubbed)
    assert scrubbed["count"] == 3
    assert scrubbed["messages"][0]["role"] == "user"


def test_invalid_custom_pattern_is_rejected_at_config_load() -> None:
    with pytest.raises(ConfigError):
        parse_config(
            {
                **base_config(),
                "redaction": {
                    "redact_before_forward": False,
                    "patterns": [{"name": "bad", "regex": "("}],
                },
            }
        )
