"""HTTP integration tests over the real FastAPI app (in-process ASGI transport)."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ALPHA_KEY, BETA_KEY, base_config, build_gateway, chat_payload
from cost_firewall.app import create_app

AUTH = {"authorization": f"Bearer {ALPHA_KEY}"}


@pytest.fixture()
def client() -> TestClient:
    gateway = build_gateway()
    return TestClient(create_app(gateway.config, gateway))


def test_chat_completion_is_openai_shaped(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json=chat_payload(), headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] == (
        body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    )
    assert body["gateway"]["provider"] == "primary"
    assert body["gateway"]["cost_basis"] == "configured_price_table"
    assert body["gateway"]["estimated_cost_micros"] > 0


def test_missing_key_returns_401_envelope(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json=chat_payload())
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_api_key"
    assert error["type"] == "authentication_error"


def test_wrong_key_returns_401(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json=chat_payload(),
        headers={"authorization": "Bearer not-a-key"},
    )
    assert response.status_code == 401


def test_invalid_json_returns_400(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        content=b"{not json",
        headers={**AUTH, "content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_oversized_body_returns_413() -> None:
    data = base_config()
    data["server"]["max_body_bytes"] = 1024
    gateway = build_gateway(data)
    client = TestClient(create_app(gateway.config, gateway))
    response = client.post(
        "/v1/chat/completions", json=chat_payload(prompt="x" * 4000), headers=AUTH
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_declared_content_length_over_limit_returns_413() -> None:
    data = base_config()
    data["server"]["max_body_bytes"] = 1024
    gateway = build_gateway(data)
    client = TestClient(create_app(gateway.config, gateway))
    response = client.post(
        "/v1/chat/completions",
        content=json.dumps(chat_payload(prompt="x" * 4000)).encode(),
        headers={**AUTH, "content-type": "application/json"},
    )
    assert response.status_code == 413


def test_budget_exhaustion_returns_429_with_retry_after() -> None:
    data = base_config()
    data["projects"][0]["monthly_budget_usd"] = 0.000_010
    gateway = build_gateway(data)
    client = TestClient(create_app(gateway.config, gateway))
    response = client.post("/v1/chat/completions", json=chat_payload(), headers=AUTH)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.json()["error"]["code"] == "budget_exhausted"


def test_unknown_model_returns_404(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json=chat_payload(model="nope"), headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_streaming_is_rejected_with_400(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json=chat_payload(stream=True), headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "stream"


def test_dry_run_endpoint_reports_decision_without_charging(client: TestClient) -> None:
    response = client.post("/v1/gateway/dry-run", json=chat_payload(), headers=AUTH)
    assert response.status_code == 200
    decision = response.json()
    assert decision["allowed"] is True
    assert decision["targets"][0]["provider"] == "primary"
    assert decision["budget"]["month_settled_micros"] == 0

    budget = client.get("/v1/gateway/budget", headers=AUTH).json()
    assert budget["month_reserved_micros"] == 0
    assert budget["month_settled_micros"] == 0


def test_usage_and_budget_reflect_completed_requests(client: TestClient) -> None:
    client.post("/v1/chat/completions", json=chat_payload(), headers=AUTH)
    usage = client.get("/v1/gateway/usage", headers=AUTH).json()
    assert usage["requests"] == 1
    assert usage["settled_requests"] == 1
    assert usage["reconciled"] is True
    assert usage["settled_micros"] > 0

    budget = client.get("/v1/gateway/budget", headers=AUTH).json()
    assert budget["month_settled_micros"] == usage["settled_micros"]
    assert budget["month_remaining_micros"] == (
        budget["monthly_budget_micros"] - usage["settled_micros"]
    )
    assert budget["month_committed_micros"] == (
        budget["month_settled_micros"] + budget["month_reserved_micros"]
    )


def test_request_trace_returns_attempt_evidence(client: TestClient) -> None:
    chat = client.post("/v1/chat/completions", json=chat_payload(), headers=AUTH).json()
    request_id = chat["gateway"]["request_id"]
    trace = client.get(f"/v1/gateway/requests/{request_id}", headers=AUTH).json()
    assert trace["request_id"] == request_id
    assert trace["status"] == "settled"
    assert trace["attempts"][0]["provider"] == "primary"


def test_request_trace_is_scoped_to_the_project() -> None:
    data = base_config()
    data["projects"].append(
        {
            "id": "beta",
            "name": "Beta",
            "api_keys": ["f0151b84a66c52ae9d90f60e7a45a0ebc7fa3560a96f32700ab4ed9d36614a7e"],
            "monthly_budget_usd": 1.0,
            "per_request_max_usd": 0.05,
            "requests_per_minute": 100,
            "max_concurrency": 4,
            "allowed_models": ["model-a"],
        }
    )
    gateway = build_gateway(data)
    client = TestClient(create_app(gateway.config, gateway))
    chat = client.post("/v1/chat/completions", json=chat_payload(), headers=AUTH).json()
    request_id = chat["gateway"]["request_id"]
    response = client.get(
        f"/v1/gateway/requests/{request_id}",
        headers={"authorization": f"Bearer {BETA_KEY}"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "request_not_found"


def test_unknown_project_read_is_rejected(client: TestClient) -> None:
    response = client.get("/v1/gateway/usage?project=ghost", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "project"


def test_usage_days_out_of_range_is_400(client: TestClient) -> None:
    response = client.get("/v1/gateway/usage?days=9999", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "days"


def test_models_lists_only_project_allowed_routes() -> None:
    data = base_config()
    data["projects"][0]["allowed_models"] = ["model-a", "model-b"]
    gateway = build_gateway(data)
    client = TestClient(create_app(gateway.config, gateway))
    body = client.get("/v1/models", headers=AUTH).json()
    assert [item["id"] for item in body["data"]] == ["model-a", "model-b"]


def test_healthz_reports_ok(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body == {"status": "ok", "ledger": "ok"}


def test_healthz_reports_degraded_when_ledger_is_unreachable() -> None:
    gateway = build_gateway()

    def boom() -> None:
        raise OSError("disk gone")

    cast(Any, gateway.ledger).connection = boom
    client = TestClient(create_app(gateway.config, gateway))
    body = client.get("/healthz").json()
    assert body == {"status": "degraded", "ledger": "unavailable"}


def test_metrics_exposition_contains_counters(client: TestClient) -> None:
    client.post("/v1/chat/completions", json=chat_payload(), headers=AUTH)
    text = client.get("/metrics").text
    assert "# TYPE gateway_requests_total counter" in text
    assert 'gateway_requests_total{project="alpha"' in text
    assert "gateway_reservations_total" in text


def test_concurrent_http_requests_never_overspend() -> None:
    data = base_config()
    data["projects"][0]["monthly_budget_usd"] = 0.000_400
    data["projects"][0]["per_request_max_usd"] = 0.05
    data["projects"][0]["requests_per_minute"] = 10_000
    data["projects"][0]["max_concurrency"] = 32
    # ~5 input tokens and 4 max output tokens at $10/1M => 90 micros reserved.
    for target in data["routes"][0]["targets"]:
        target["input_usd_per_1m"] = 10.0
        target["output_usd_per_1m"] = 10.0
    gateway = build_gateway(data)
    client = TestClient(create_app(gateway.config, gateway))

    from concurrent.futures import ThreadPoolExecutor

    def call(_: int) -> int:
        response = cast(
            httpx.Response,
            client.post("/v1/chat/completions", json=chat_payload(max_tokens=4), headers=AUTH),
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(call, range(8)))

    assert statuses.count(200) >= 1
    assert set(statuses) <= {200, 429}
    budget = client.get("/v1/gateway/budget", headers=AUTH).json()
    assert budget["month_settled_micros"] <= budget["monthly_budget_micros"]
    assert budget["month_committed_micros"] <= budget["monthly_budget_micros"]
