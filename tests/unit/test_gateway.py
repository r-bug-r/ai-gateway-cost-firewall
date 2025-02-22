"""Gateway behavior: routing, fallback classification, budget gate, settlement."""

from __future__ import annotations

from typing import Any, cast

import pytest

from conftest import ALPHA_KEY, base_config, build_gateway, chat_payload
from cost_firewall.errors import (
    BudgetExhausted,
    ModelNotFound,
    PerRequestLimitExceeded,
    UpstreamFailure,
    ValidationError,
)
from cost_firewall.gateway import Gateway
from cost_firewall.ledger import BudgetPolicy

AUTH = f"Bearer {ALPHA_KEY}"


def test_successful_request_settles_ledger_and_returns_usage(gateway: Gateway) -> None:
    result = gateway.chat(AUTH, chat_payload())
    assert result.provider == "primary"
    assert result.response.output_tokens > 0
    assert result.cost_micros > 0
    trace = gateway.ledger.request_trace(result.request_id)
    assert trace is not None
    assert trace["status"] == "settled"
    assert trace["settled_micros"] == result.cost_micros
    assert len(trace["attempts"]) == 1
    assert trace["attempts"][0]["status"] == "success"


def test_transient_failure_falls_back_with_separate_attempt_rows() -> None:
    data = base_config()
    data["providers"] = [
        {"id": "primary", "type": "fake", "fail_first": 1, "fail_class": "transient"},
        {"id": "backup", "type": "fake"},
    ]
    gateway = build_gateway(data)
    result = gateway.chat(AUTH, chat_payload())
    assert result.provider == "backup"
    trace = gateway.ledger.request_trace(result.request_id)
    assert trace is not None
    assert [a["status"] for a in trace["attempts"]] == ["transient", "success"]
    assert [a["provider"] for a in trace["attempts"]] == ["primary", "backup"]
    assert trace["attempts"][0]["cost_micros"] == 0
    # Only the successful attempt is billed, and settlement matches that attempt.
    assert trace["settled_micros"] == trace["attempts"][1]["cost_micros"]


def test_permanent_failure_does_not_fall_back() -> None:
    data = base_config()
    data["providers"] = [
        {"id": "primary", "type": "fake", "fail_first": 5, "fail_class": "permanent"},
        {"id": "backup", "type": "fake"},
    ]
    gateway = build_gateway(data)
    with pytest.raises(UpstreamFailure):
        gateway.chat(AUTH, chat_payload())
    report = gateway.ledger.usage_report(project_id="alpha")
    assert report["failed_requests"] == 1
    assert report["successful_attempts"] == 0
    assert report["settled_micros"] == 0


def test_single_target_route_has_no_fallback() -> None:
    data = base_config()
    data["providers"] = [
        {"id": "primary", "type": "fake"},
        {"id": "backup", "type": "fake", "fail_first": 5, "fail_class": "transient"},
    ]
    gateway = build_gateway(data)
    with pytest.raises(UpstreamFailure):
        gateway.chat(AUTH, chat_payload(model="model-b"))
    report = gateway.ledger.usage_report(project_id="alpha")
    assert report["attempts"] == 1
    assert report["failed_requests"] == 1
    assert report["settled_micros"] == 0


def test_non_idempotent_request_never_falls_back() -> None:
    """A client that declares a side effect must not be retried elsewhere."""
    data = base_config()
    data["providers"] = [
        {"id": "primary", "type": "fake", "fail_first": 5, "fail_class": "transient"},
        {"id": "backup", "type": "fake"},
    ]
    gateway = build_gateway(data)
    with pytest.raises(UpstreamFailure):
        gateway.chat(AUTH, chat_payload(model="model-a", idempotent=False))
    report = gateway.ledger.usage_report(project_id="alpha")
    assert report["attempts"] == 1
    assert report["failed_requests"] == 1

    decision = gateway.dry_run(AUTH, chat_payload(model="model-a", idempotent=False))
    assert decision["fallback_allowed"] is False
    assert decision["fallback_reason"] == "request is not idempotent; fallback is not permitted"


def test_validation_rejects_non_boolean_idempotent(gateway: Gateway) -> None:
    with pytest.raises(ValidationError) as excinfo:
        gateway.chat(AUTH, chat_payload(idempotent="yes"))
    assert excinfo.value.param == "idempotent"


def test_budget_exhaustion_rejects_before_forwarding() -> None:
    data = base_config()
    data["projects"][0]["monthly_budget_usd"] = 0.000_100  # 100 micros
    data["projects"][0]["per_request_max_usd"] = 0.05
    gateway = build_gateway(data)
    # First request's reservation exceeds the entire budget.
    with pytest.raises(BudgetExhausted) as excinfo:
        gateway.chat(AUTH, chat_payload(max_tokens=64))
    assert excinfo.value.status_code == 429
    assert "monthly_budget_exhausted" in excinfo.value.message
    report = gateway.ledger.usage_report(project_id="alpha")
    assert report["requests"] == 0  # nothing was reserved or forwarded


def test_budget_is_consumed_until_next_request_is_blocked() -> None:
    data = base_config()
    data["projects"][0]["monthly_budget_usd"] = 0.000_200  # 200 micros
    data["projects"][0]["per_request_max_usd"] = 0.05
    data["routes"][0]["targets"][0]["input_usd_per_1m"] = 10.0
    data["routes"][0]["targets"][0]["output_usd_per_1m"] = 10.0
    data["routes"][0]["targets"][1]["input_usd_per_1m"] = 10.0
    data["routes"][0]["targets"][1]["output_usd_per_1m"] = 10.0
    gateway = build_gateway(data)
    gateway.chat(AUTH, chat_payload(prompt="abcd", max_tokens=8))
    with pytest.raises(BudgetExhausted):
        gateway.chat(AUTH, chat_payload(prompt="abcd", max_tokens=8))


def test_per_request_limit_rejects_expensive_request() -> None:
    data = base_config()
    data["projects"][0]["per_request_max_usd"] = 0.000_001  # 1 micro
    gateway = build_gateway(data)
    with pytest.raises(PerRequestLimitExceeded) as excinfo:
        gateway.chat(AUTH, chat_payload(max_tokens=512))
    assert excinfo.value.code == "per_request_limit_exceeded"
    assert excinfo.value.status_code == 400


def test_model_not_allowed_for_project_is_rejected(gateway: Gateway) -> None:
    with pytest.raises(ModelNotFound):
        gateway.chat(AUTH, chat_payload(model="unrouted-model"))


def test_project_allowlist_is_enforced() -> None:
    data = base_config()
    data["projects"][0]["allowed_models"] = ["model-a"]
    gateway = build_gateway(data)
    with pytest.raises(ModelNotFound):
        gateway.chat(AUTH, chat_payload(model="model-b"))
    gateway.chat(AUTH, chat_payload(model="model-a"))


def test_dry_run_does_not_reserve_or_forward(gateway: Gateway) -> None:
    decision = gateway.dry_run(AUTH, chat_payload())
    assert decision["allowed"] is True
    assert decision["fallback_allowed"] is True
    assert decision["targets"][0]["provider"] == "primary"
    report = gateway.ledger.usage_report(project_id="alpha")
    assert report["requests"] == 0
    assert (
        gateway.ledger.snapshot(
            BudgetPolicy("alpha", "Alpha", 1_000_000, None, 50_000)
        ).month_reserved_micros
        == 0
    )


def test_dry_run_reports_blocking_reason_when_budget_is_gone() -> None:
    data = base_config()
    data["projects"][0]["monthly_budget_usd"] = 0.000_010
    gateway = build_gateway(data)
    decision = gateway.dry_run(AUTH, chat_payload(max_tokens=128))
    assert decision["allowed"] is False
    assert decision["blocking_reason"] == "monthly_budget_exhausted"


def test_fail_closed_when_ledger_unavailable() -> None:
    data = base_config()
    gateway = build_gateway(data)

    def boom(*_: Any, **__: Any) -> None:
        from cost_firewall.ledger import BudgetUnavailable

        raise BudgetUnavailable("disk gone")

    cast(Any, gateway.ledger).reserve = boom
    with pytest.raises(Exception) as excinfo:
        gateway.chat(AUTH, chat_payload())
    assert "fail closed" in str(excinfo.value).lower()
    assert gateway.ledger.usage_report(project_id="alpha")["requests"] == 0


def test_fail_open_forwards_and_audits_when_configured() -> None:
    data = base_config()
    data["server"]["fail_closed"] = False
    gateway = build_gateway(data)

    def boom(*_: Any, **__: Any) -> None:
        from cost_firewall.ledger import BudgetUnavailable

        raise BudgetUnavailable("disk gone")

    cast(Any, gateway.ledger).reserve = boom
    result = gateway.chat(AUTH, chat_payload())
    assert result.response.content
    with gateway.ledger.connection() as conn:
        rows = conn.execute(
            "SELECT event FROM audit_events WHERE event = 'budget_fail_open'"
        ).fetchall()
    assert len(rows) == 1


def test_validation_rejects_unsupported_fields(gateway: Gateway) -> None:
    with pytest.raises(ValidationError) as excinfo:
        gateway.chat(AUTH, chat_payload(tools=[{"type": "function"}]))
    assert excinfo.value.param == "tools"


def test_validation_rejects_streaming(gateway: Gateway) -> None:
    with pytest.raises(ValidationError) as excinfo:
        gateway.chat(AUTH, chat_payload(stream=True))
    assert excinfo.value.param == "stream"


def test_validation_rejects_oversized_max_tokens(gateway: Gateway) -> None:
    with pytest.raises(ValidationError):
        gateway.chat(AUTH, chat_payload(max_tokens=100_000))


def test_rate_limit_rejects_then_recovers(gateway: Gateway) -> None:
    gateway._rate_limiters["alpha"] = type(gateway._rate_limiters["alpha"])(limit=2)
    from cost_firewall.errors import RateLimited

    gateway.chat(AUTH, chat_payload())
    gateway.chat(AUTH, chat_payload())
    with pytest.raises(RateLimited) as excinfo:
        gateway.chat(AUTH, chat_payload())
    assert excinfo.value.retry_after is not None


def test_concurrency_limit_rejects_over_lapping_requests() -> None:
    data = base_config()
    data["projects"][0]["max_concurrency"] = 1
    data["providers"][0]["latency_ms"] = 150
    gateway = build_gateway(data)
    from concurrent.futures import ThreadPoolExecutor

    from cost_firewall.errors import ConcurrencyLimited

    def call() -> str:
        try:
            gateway.chat(AUTH, chat_payload())
            return "ok"
        except ConcurrencyLimited:
            return "limited"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: call(), range(2)))
    assert sorted(outcomes) == ["limited", "ok"]


def test_usage_report_reconciles_after_mixed_outcomes() -> None:
    data = base_config()
    data["providers"] = [
        {"id": "primary", "type": "fake", "fail_first": 1, "fail_class": "transient"},
        {"id": "backup", "type": "fake"},
    ]
    gateway = build_gateway(data)
    gateway.chat(AUTH, chat_payload())
    gateway.chat(AUTH, chat_payload())
    report = gateway.usage(project=None)
    assert report["reconciled"] is True
    assert report["requests"] == 2
    assert report["settled_requests"] == 2
    assert report["attempts"] == 3


def test_settled_cost_never_exceeds_the_reservation(gateway: Gateway) -> None:
    """The reservation is a worst case: estimated input plus the full output cap."""
    for index in range(5):
        payload = chat_payload(prompt=f"request {index}", max_tokens=128)
        request = gateway._parse(payload)
        plan = gateway.plan(gateway.config.projects[0], request)
        result = gateway.chat(AUTH, payload)
        assert result.cost_micros <= plan.reservation_micros


def test_request_trace_is_none_for_unknown_request(gateway: Gateway) -> None:
    assert gateway.request_trace("req_missing") is None
