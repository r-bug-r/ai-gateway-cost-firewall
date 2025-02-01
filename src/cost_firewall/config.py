"""Strict TOML configuration loading.

Invalid configuration is a startup failure. Nothing here is per-request work.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from cost_firewall.errors import ConfigError
from cost_firewall.money import decimal_from_config, usd_to_micros

PROVIDER_TYPES = frozenset({"fake", "openai_compatible", "anthropic"})
ERROR_CLASSES = frozenset({"transient", "permanent"})
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    max_body_bytes: int = 1_048_576
    request_timeout_seconds: float = 30.0
    log_prompts: bool = False
    log_level: str = "INFO"
    fail_closed: bool = True


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path


@dataclass(frozen=True)
class LimitsConfig:
    max_messages: int = 64
    max_input_chars: int = 200_000
    default_max_output_tokens: int = 512
    max_output_tokens: int = 4096
    reservation_ttl_seconds: int = 120


@dataclass(frozen=True)
class RedactionPattern:
    name: str
    regex: str


@dataclass(frozen=True)
class RedactionConfig:
    redact_before_forward: bool = False
    patterns: tuple[RedactionPattern, ...] = ()


@dataclass(frozen=True)
class ProjectConfig:
    id: str
    name: str
    api_key_hashes: tuple[str, ...]
    monthly_budget_micros: int
    daily_budget_micros: int | None
    per_request_max_micros: int
    requests_per_minute: int
    max_concurrency: int
    allowed_models: tuple[str, ...] = ()
    retention_days: int | None = 30


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    type: str
    base_url: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = 30.0
    latency_ms: int = 0
    fail_first: int = 0
    fail_status: int = 500
    fail_class: str = "transient"
    response_text: str | None = None


@dataclass(frozen=True)
class TargetConfig:
    provider: str
    model: str
    input_usd_per_1m: Decimal
    output_usd_per_1m: Decimal


@dataclass(frozen=True)
class RouteConfig:
    model: str
    targets: tuple[TargetConfig, ...]


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    database: DatabaseConfig
    limits: LimitsConfig
    redaction: RedactionConfig
    projects: tuple[ProjectConfig, ...]
    providers: tuple[ProviderConfig, ...]
    routes: tuple[RouteConfig, ...]
    source: Path | None = None
    _project_index: dict[str, ProjectConfig] = field(default_factory=dict, repr=False)
    _route_index: dict[str, RouteConfig] = field(default_factory=dict, repr=False)
    _provider_index: dict[str, ProviderConfig] = field(default_factory=dict, repr=False)

    def project(self, project_id: str) -> ProjectConfig | None:
        return self._project_index.get(project_id)

    def route(self, model: str) -> RouteConfig | None:
        return self._route_index.get(model)

    def provider(self, provider_id: str) -> ProviderConfig | None:
        return self._provider_index.get(provider_id)

    @property
    def project_ids(self) -> tuple[str, ...]:
        return tuple(p.id for p in self.projects)


def _table(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a table")
    return value


def _get_str(table: dict[str, Any], key: str, path: str, *, default: str | None = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{path}.{key} must be a non-empty string")
    return value


def _get_int(
    table: dict[str, Any], key: str, path: str, *, default: int | None = None, minimum: int = 0
) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}.{key} must be an integer")
    if value < minimum:
        raise ConfigError(f"{path}.{key} must be >= {minimum}")
    return value


def _get_number(
    table: dict[str, Any], key: str, path: str, *, default: float | None = None
) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}.{key} must be a number")
    if value <= 0:
        raise ConfigError(f"{path}.{key} must be > 0")
    return float(value)


def _get_bool(table: dict[str, Any], key: str, path: str, *, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{path}.{key} must be a boolean")
    return value


def _usd(table: dict[str, Any], key: str, path: str, *, default: float | None = None) -> int:
    raw = table.get(key, default)
    if raw is None:
        raise ConfigError(f"{path}.{key} is required")
    try:
        amount = decimal_from_config(raw, field=f"{path}.{key}")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if amount < 0:
        raise ConfigError(f"{path}.{key} must be >= 0")
    micros = usd_to_micros(amount)
    if micros <= 0:
        raise ConfigError(f"{path}.{key} must be at least $0.000001")
    return micros


def _price(table: dict[str, Any], key: str, path: str) -> Decimal:
    raw = table.get(key)
    if raw is None:
        raise ConfigError(f"{path}.{key} is required")
    try:
        amount = decimal_from_config(raw, field=f"{path}.{key}")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if amount < 0:
        raise ConfigError(f"{path}.{key} must be >= 0")
    return amount


def _api_key_hashes(values: object, path: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not values:
        raise ConfigError(f"{path} must be a non-empty array of SHA-256 key hashes")
    hashes: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise ConfigError(f"{path}[{index}] must be a string")
        normalized = value.strip().lower().removeprefix("sha256:")
        if not _HASH_RE.match(normalized):
            raise ConfigError(
                f"{path}[{index}] must be a 64-character SHA-256 hex digest "
                "(generate one with `cost-firewall hash-key <key>`)"
            )
        hashes.append(normalized)
    return tuple(hashes)


def _parse_server(raw: object) -> ServerConfig:
    table = _table(raw, "server")
    path = "server"
    return ServerConfig(
        host=_get_str(table, "host", path, default="127.0.0.1"),
        port=_get_int(table, "port", path, default=8787, minimum=1),
        max_body_bytes=_get_int(table, "max_body_bytes", path, default=1_048_576, minimum=1024),
        request_timeout_seconds=_get_number(table, "request_timeout_seconds", path, default=30.0),
        log_prompts=_get_bool(table, "log_prompts", path, default=False),
        log_level=_get_str(table, "log_level", path, default="INFO"),
        fail_closed=_get_bool(table, "fail_closed", path, default=True),
    )


def _parse_database(raw: object) -> DatabaseConfig:
    table = _table(raw, "database")
    path = _get_str(table, "path", "database", default="gateway.db")
    return DatabaseConfig(path=Path(path))


def _parse_limits(raw: object) -> LimitsConfig:
    table = _table(raw or {}, "limits")
    path = "limits"
    limits = LimitsConfig(
        max_messages=_get_int(table, "max_messages", path, default=64, minimum=1),
        max_input_chars=_get_int(table, "max_input_chars", path, default=200_000, minimum=1),
        default_max_output_tokens=_get_int(
            table, "default_max_output_tokens", path, default=512, minimum=1
        ),
        max_output_tokens=_get_int(table, "max_output_tokens", path, default=4096, minimum=1),
        reservation_ttl_seconds=_get_int(
            table, "reservation_ttl_seconds", path, default=120, minimum=1
        ),
    )
    if limits.default_max_output_tokens > limits.max_output_tokens:
        raise ConfigError("limits.default_max_output_tokens must be <= limits.max_output_tokens")
    return limits


def _parse_redaction(raw: object) -> RedactionConfig:
    table = _table(raw or {}, "redaction")
    patterns_raw = table.get("patterns", [])
    if not isinstance(patterns_raw, list):
        raise ConfigError("redaction.patterns must be an array of tables")
    patterns: list[RedactionPattern] = []
    seen: set[str] = set()
    for index, entry in enumerate(patterns_raw):
        path = f"redaction.patterns[{index}]"
        item = _table(entry, path)
        name = _get_str(item, "name", path)
        regex = _get_str(item, "regex", path)
        if name in seen:
            raise ConfigError(f"{path}.name '{name}' is duplicated")
        seen.add(name)
        try:
            re.compile(regex)
        except re.error as exc:
            raise ConfigError(f"{path}.regex is invalid: {exc}") from exc
        patterns.append(RedactionPattern(name=name, regex=regex))
    return RedactionConfig(
        redact_before_forward=_get_bool(table, "redact_before_forward", "redaction", default=False),
        patterns=tuple(patterns),
    )


def _parse_project(raw: object, index: int) -> ProjectConfig:
    path = f"projects[{index}]"
    table = _table(raw, path)
    project_id = _get_str(table, "id", path)
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$", project_id):
        raise ConfigError(f"{path}.id must match [a-zA-Z0-9][a-zA-Z0-9_-]{{0,63}}")
    allowed = table.get("allowed_models", [])
    if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
        raise ConfigError(f"{path}.allowed_models must be an array of strings")
    daily_raw = table.get("daily_budget_usd")
    retention_raw = table.get("retention_days", 30)
    if retention_raw is not None and (
        isinstance(retention_raw, bool) or not isinstance(retention_raw, int) or retention_raw < 1
    ):
        raise ConfigError(f"{path}.retention_days must be a positive integer or omitted")
    return ProjectConfig(
        id=project_id,
        name=_get_str(table, "name", path, default=project_id),
        api_key_hashes=_api_key_hashes(table.get("api_keys"), f"{path}.api_keys"),
        monthly_budget_micros=_usd(table, "monthly_budget_usd", path),
        daily_budget_micros=None if daily_raw is None else _usd(table, "daily_budget_usd", path),
        per_request_max_micros=_usd(table, "per_request_max_usd", path),
        requests_per_minute=_get_int(table, "requests_per_minute", path, minimum=1),
        max_concurrency=_get_int(table, "max_concurrency", path, minimum=1),
        allowed_models=tuple(allowed),
        retention_days=retention_raw,
    )


def _parse_provider(raw: object, index: int) -> ProviderConfig:
    path = f"providers[{index}]"
    table = _table(raw, path)
    provider_type = _get_str(table, "type", path)
    if provider_type not in PROVIDER_TYPES:
        raise ConfigError(f"{path}.type must be one of: {', '.join(sorted(PROVIDER_TYPES))}")
    base_url = table.get("base_url")
    if base_url is not None and not isinstance(base_url, str):
        raise ConfigError(f"{path}.base_url must be a string")
    if provider_type in {"openai_compatible", "anthropic"} and not base_url:
        raise ConfigError(f"{path}.base_url is required for type '{provider_type}'")
    api_key_env = table.get("api_key_env")
    if api_key_env is not None and not isinstance(api_key_env, str):
        raise ConfigError(f"{path}.api_key_env must be a string")
    fail_class = table.get("fail_class", "transient")
    if fail_class not in ERROR_CLASSES:
        raise ConfigError(f"{path}.fail_class must be one of: transient, permanent")
    response_text = table.get("response_text")
    if response_text is not None and not isinstance(response_text, str):
        raise ConfigError(f"{path}.response_text must be a string")
    return ProviderConfig(
        id=_get_str(table, "id", path),
        type=provider_type,
        base_url=base_url,
        api_key_env=api_key_env,
        timeout_seconds=_get_number(table, "timeout_seconds", path, default=30.0),
        latency_ms=_get_int(table, "latency_ms", path, default=0, minimum=0),
        fail_first=_get_int(table, "fail_first", path, default=0, minimum=0),
        fail_status=_get_int(table, "fail_status", path, default=500, minimum=400),
        fail_class=str(fail_class),
        response_text=response_text,
    )


def _parse_route(raw: object, index: int) -> RouteConfig:
    path = f"routes[{index}]"
    table = _table(raw, path)
    targets_raw = table.get("targets")
    if not isinstance(targets_raw, list) or not targets_raw:
        raise ConfigError(f"{path}.targets must be a non-empty array")
    targets: list[TargetConfig] = []
    for target_index, target_raw in enumerate(targets_raw):
        target_path = f"{path}.targets[{target_index}]"
        target = _table(target_raw, target_path)
        targets.append(
            TargetConfig(
                provider=_get_str(target, "provider", target_path),
                model=_get_str(target, "model", target_path),
                input_usd_per_1m=_price(target, "input_usd_per_1m", target_path),
                output_usd_per_1m=_price(target, "output_usd_per_1m", target_path),
            )
        )
    return RouteConfig(model=_get_str(table, "model", path), targets=tuple(targets))


def parse_config(data: object, *, source: Path | None = None) -> Config:
    root = _table(data, "config")
    projects_raw = root.get("projects")
    if not isinstance(projects_raw, list) or not projects_raw:
        raise ConfigError("config.projects must be a non-empty array")
    providers_raw = root.get("providers")
    if not isinstance(providers_raw, list) or not providers_raw:
        raise ConfigError("config.providers must be a non-empty array")
    routes_raw = root.get("routes")
    if not isinstance(routes_raw, list) or not routes_raw:
        raise ConfigError("config.routes must be a non-empty array")

    projects = tuple(_parse_project(raw, index) for index, raw in enumerate(projects_raw))
    providers = tuple(_parse_provider(raw, index) for index, raw in enumerate(providers_raw))
    routes = tuple(_parse_route(raw, index) for index, raw in enumerate(routes_raw))

    project_index: dict[str, ProjectConfig] = {}
    key_owner: dict[str, str] = {}
    for project in projects:
        if project.id in project_index:
            raise ConfigError(f"Duplicate project id '{project.id}'")
        project_index[project.id] = project
        # A shared key would make tenant resolution order-dependent, so the same key
        # hash may belong to exactly one project.
        for digest in project.api_key_hashes:
            owner = key_owner.get(digest)
            if owner is not None:
                raise ConfigError(f"API key hash is assigned to both '{owner}' and '{project.id}'")
            key_owner[digest] = project.id
    provider_index: dict[str, ProviderConfig] = {}
    for provider in providers:
        if provider.id in provider_index:
            raise ConfigError(f"Duplicate provider id '{provider.id}'")
        provider_index[provider.id] = provider
    route_index: dict[str, RouteConfig] = {}
    for route in routes:
        if route.model in route_index:
            raise ConfigError(f"Duplicate route model '{route.model}'")
        for target in route.targets:
            if target.provider not in provider_index:
                raise ConfigError(
                    f"Route '{route.model}' references unknown provider '{target.provider}'"
                )
        route_index[route.model] = route

    for project in projects:
        for model in project.allowed_models:
            if model not in route_index:
                raise ConfigError(
                    f"Project '{project.id}' allows model '{model}', which has no route"
                )

    return Config(
        server=_parse_server(root.get("server", {})),
        database=_parse_database(root.get("database", {})),
        limits=_parse_limits(root.get("limits", {})),
        redaction=_parse_redaction(root.get("redaction", {})),
        projects=projects,
        providers=providers,
        routes=routes,
        source=source,
        _project_index=project_index,
        _route_index=route_index,
        _provider_index=provider_index,
    )


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {config_path}: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc
    return parse_config(data, source=config_path)
