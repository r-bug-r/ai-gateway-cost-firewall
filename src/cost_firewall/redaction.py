"""Secret redaction applied before logs and optional upstream forwarding."""

from __future__ import annotations

import re
from dataclasses import dataclass

from cost_firewall.config import RedactionConfig, RedactionPattern

REDACTED = "[REDACTED:{name}]"

# Defaults cover common credential shapes. They are deliberately narrow enough to
# avoid mangling ordinary prose: a false positive silently changes user prompts.
DEFAULT_PATTERNS: tuple[RedactionPattern, ...] = (
    RedactionPattern(
        name="openai_key",
        regex=r"\bsk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_-]{16,}\b",
    ),
    RedactionPattern(
        name="anthropic_key",
        regex=r"\bsk-ant-[A-Za-z0-9_-]{16,}\b",
    ),
    RedactionPattern(
        name="github_token",
        regex=r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b",
    ),
    RedactionPattern(
        name="aws_access_key",
        regex=r"\bAKIA[0-9A-Z]{16}\b",
    ),
    RedactionPattern(
        name="jwt",
        regex=r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    ),
    RedactionPattern(
        name="bearer_token",
        regex=r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}",
    ),
    RedactionPattern(
        name="private_key_block",
        regex=r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    ),
    # An unterminated key header is still evidence of a leak; scrub the header itself.
    RedactionPattern(
        name="private_key_header",
        regex=r"-----BEGIN [A-Z ]*PRIVATE KEY-----|-----END [A-Z ]*PRIVATE KEY-----",
    ),
    RedactionPattern(
        name="email",
        regex=r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    ),
    # Grouped card-shaped numbers only (4-4-4-N); ungrouped digit runs are left alone.
    RedactionPattern(
        name="credit_card",
        regex=r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{1,4}\b",
    ),
    RedactionPattern(
        name="password_assignment",
        regex=r"(?i)\b(?:password|passwd|secret|token)\s*[:=]\s*[^\s\"']{6,}",
    ),
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class Redactor:
    def __init__(self, config: RedactionConfig | None = None):
        patterns = list(DEFAULT_PATTERNS)
        if config is not None:
            patterns.extend(config.patterns)
        self._patterns = [(pattern.name, re.compile(pattern.regex)) for pattern in patterns]

    def redact(self, text: str) -> RedactionResult:
        counts: dict[str, int] = {}
        result = text
        for name, regex in self._patterns:
            result, replaced = regex.subn(REDACTED.format(name=name), result)
            if replaced:
                counts[name] = replaced
        return RedactionResult(text=result, counts=counts)


class LogRedactor:
    """Redacts arbitrary log values, including dict/list payloads, in place-safe fashion."""

    def __init__(self, redactor: Redactor):
        self._redactor = redactor

    def redact_text(self, text: str) -> str:
        return self._redactor.redact(text).text

    def redact_value(self, value: object) -> object:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {str(k): self.redact_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_value(item) for item in value]
        return value
