from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cost_firewall.config import Config, load_config, parse_config
from cost_firewall.gateway import Gateway
from cost_firewall.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = ROOT / "config.example.toml"

ALPHA_KEY = "demo-key-alpha"
BETA_KEY = "demo-key-beta"


def base_config(**overrides: Any) -> dict[str, Any]:
    """Synthetic config used by most tests; no network, no real credentials."""
    config: dict[str, Any] = {
        "server": {"fail_closed": True, "request_timeout_seconds": 5.0},
        "database": {"path": ":memory:"},
        "limits": {
            "default_max_output_tokens": 256,
            "max_output_tokens": 1024,
            "reservation_ttl_seconds": 60,
        },
        "redaction": {"redact_before_forward": False, "patterns": []},
        "projects": [
            {
                "id": "alpha",
                "name": "Alpha",
                "api_keys": ["e49052cb77a35e3ebf86f7d184651dbf07b0fc8707d8ecb748fbf759b0a1d129"],
                "monthly_budget_usd": 1.0,
                "daily_budget_usd": None,
                "per_request_max_usd": 0.05,
                "requests_per_minute": 1000,
                "max_concurrency": 8,
                "retention_days": 30,
                "allowed_models": ["model-a", "model-b", "model-c"],
            }
        ],
        "providers": [
            {"id": "primary", "type": "fake"},
            {"id": "backup", "type": "fake"},
        ],
        "routes": [
            {
                "model": "model-a",
                "targets": [
                    {
                        "provider": "primary",
                        "model": "upstream-a",
                        "input_usd_per_1m": 1.0,
                        "output_usd_per_1m": 2.0,
                    },
                    {
                        "provider": "backup",
                        "model": "upstream-a",
                        "input_usd_per_1m": 1.0,
                        "output_usd_per_1m": 2.0,
                    },
                ],
            },
            {
                "model": "model-b",
                "targets": [
                    {
                        "provider": "backup",
                        "model": "upstream-b",
                        "input_usd_per_1m": 1.0,
                        "output_usd_per_1m": 2.0,
                    }
                ],
            },
            {
                "model": "model-c",
                "targets": [
                    {
                        "provider": "primary",
                        "model": "upstream-c",
                        "input_usd_per_1m": 1.0,
                        "output_usd_per_1m": 2.0,
                    }
                ],
            },
        ],
    }
    config.update(overrides)
    return config


def build_gateway(config_data: dict[str, Any] | None = None) -> Gateway:
    data = config_data if config_data is not None else base_config()
    config = parse_config(data)
    return Gateway(config, Ledger(":memory:"))


def build_config(config_data: dict[str, Any] | None = None) -> Config:
    data = config_data if config_data is not None else base_config()
    return parse_config(data)


@pytest.fixture()
def gateway() -> Gateway:
    return build_gateway()


@pytest.fixture()
def example_config() -> Config:
    return load_config(EXAMPLE_CONFIG)


@pytest.fixture()
def temp_config_file(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    yield path


def chat_payload(
    *,
    model: str = "model-a",
    prompt: str = "hello",
    max_tokens: int = 64,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    payload.update(extra)
    return payload


def json_dump(value: object) -> str:
    return json.dumps(value, sort_keys=True)
