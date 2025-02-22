"""Configuration validation and example-config sanity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import base_config
from cost_firewall.config import load_config, parse_config
from cost_firewall.errors import ConfigError


def test_example_config_loads(example_config: Any) -> None:
    assert example_config.server.port == 8787
    assert {p.id for p in example_config.projects} == {"alpha", "beta"}
    assert "gpt-4o-mini" in {r.model for r in example_config.routes}
    assert example_config.providers[0].type == "fake"


def test_example_config_has_no_plaintext_keys(example_config: Any) -> None:
    for project in example_config.projects:
        for digest in project.api_key_hashes:
            assert len(digest) == 64
            assert digest == digest.lower()
            assert all(c in "0123456789abcdef" for c in digest)


def test_example_config_route_targets_reference_known_providers(example_config: Any) -> None:
    provider_ids = {p.id for p in example_config.providers}
    for route in example_config.routes:
        for target in route.targets:
            assert target.provider in provider_ids


def test_duplicate_project_id_is_rejected() -> None:
    data = base_config()
    data["projects"].append(dict(data["projects"][0]))
    with pytest.raises(ConfigError, match=r"[Dd]uplicate project id"):
        parse_config(data)


def test_duplicate_api_key_hash_is_rejected() -> None:
    data = base_config()
    second = dict(data["projects"][0])
    second["id"] = "other"
    data["projects"].append(second)
    with pytest.raises(ConfigError, match="API key"):
        parse_config(data)


def test_unknown_provider_reference_is_rejected() -> None:
    data = base_config()
    data["routes"][0]["targets"][0]["provider"] = "ghost"
    with pytest.raises(ConfigError, match="unknown provider"):
        parse_config(data)


def test_missing_route_provider_is_rejected() -> None:
    data = base_config()
    data["providers"] = [{"id": "primary", "type": "fake"}]
    with pytest.raises(ConfigError, match="unknown provider"):
        parse_config(data)


def test_route_with_no_targets_is_rejected() -> None:
    data = base_config()
    data["routes"][0]["targets"] = []
    with pytest.raises(ConfigError):
        parse_config(data)


def test_negative_budget_is_rejected() -> None:
    data = base_config()
    data["projects"][0]["monthly_budget_usd"] = -1
    with pytest.raises(ConfigError, match="monthly_budget_usd"):
        parse_config(data)


def test_negative_price_is_rejected() -> None:
    data = base_config()
    data["routes"][0]["targets"][0]["input_usd_per_1m"] = -0.5
    with pytest.raises(ConfigError):
        parse_config(data)


def test_allowed_models_must_exist_in_routes() -> None:
    data = base_config()
    data["projects"][0]["allowed_models"] = ["does-not-exist"]
    with pytest.raises(ConfigError, match="has no route"):
        parse_config(data)


def test_project_requires_a_key() -> None:
    data = base_config()
    data["projects"][0]["api_keys"] = []
    with pytest.raises(ConfigError, match="api_keys"):
        parse_config(data)


def test_missing_config_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Cannot read config file"):
        load_config(tmp_path / "nope.toml")


def test_malformed_toml_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("[server\nport = 1", encoding="utf-8")
    with pytest.raises(ConfigError, match="TOML"):
        load_config(path)


def test_fail_closed_defaults_to_true() -> None:
    config = parse_config(base_config(server={}))
    assert config.server.fail_closed is True


def test_redaction_before_forward_defaults_to_false() -> None:
    config = parse_config(base_config(redaction={}))
    assert config.redaction.redact_before_forward is False
