"""Operator-facing read endpoints: dashboard, ledger list, policy, exports.

These are the endpoints the dashboard consumes. The tests pin the properties the
UI relies on: authorization is per project, no prompt text is reachable, exports
agree with the JSON report, and a refusal is visible as a ledger row.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from conftest import ALPHA_KEY, BETA_KEY, base_config, build_gateway, chat_payload
from cost_firewall.app import create_app

AUTH = {"authorization": f"Bearer {ALPHA_KEY}"}
BETA_AUTH = {"authorization": f"Bearer {BETA_KEY}"}


@pytest.fixture()
def client() -> TestClient:
    gateway = build_gateway()
    return TestClient(create_app(gateway.config, gateway))


def test_dashboard_shell_is_served_without_credentials(client: TestClient) -> None:
    response = client.get("/dashboard/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    # The shell must not contain data: no key material, no ledger values.
    assert ALPHA_KEY not in body
    assert "Skip to main content" in body
    # Assets resolve relatively so a reverse proxy under a prefix still works.
    assert 'href="./app.css"' in body
    assert 'src="./app.js"' in body


def test_dashboard_is_locked_down_by_csp(client: TestClient) -> None:
    response = client.get("/dashboard/")
    policy = response.headers["content-security-policy"]
    # The page holds a project key in memory, so injected script must be unable
    # to run: no inline script, no eval, no third-party origin.
    assert "script-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    # The shell must satisfy the policy it ships with: no inline script or style.
    assert "style=" not in response.text
    assert response.text.count("<script") == 1


def test_api_responses_are_not_given_the_dashboard_policy(client: TestClient) -> None:
    # API clients are not browsers; adding a document policy to JSON responses
    # would be noise, and would break nothing but confuse debugging.
    response = client.get("/healthz")
    assert "content-security-policy" not in response.headers


def test_dashboard_assets_are_local(client: TestClient) -> None:
    css = client.get("/dashboard/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    # No external fetch: a local-only deployment must not call a CDN.
    assert "http://" not in css.text
    assert "https://" not in css.text
    script = client.get("/dashboard/app.js")
    assert script.status_code == 200
    assert "https://" not in script.text


def test_dashboard_root_redirects_to_trailing_slash(client: TestClient) -> None:
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard/"


def test_ledger_list_requires_a_key(client: TestClient) -> None:
    assert client.get("/v1/gateway/requests").status_code == 401


def test_ledger_list_is_scoped_to_the_key_project() -> None:
    data = base_config()
    data["projects"].append(
        {
            "id": "beta",
            "name": "Beta",
            "api_keys": ["f0151b84a66c52ae9d90f60e7a45a0ebc7fa3560a96f32700ab4ed9d36614a7e"],
            "monthly_budget_usd": 1.0,
            "per_request_max_usd": 0.05,
            "requests_per_minute": 1000,
            "max_concurrency": 8,
        }
    )
    gateway = build_gateway(data)
    client = TestClient(create_app(gateway.config, gateway))
    client.post("/v1/chat/completions", json=chat_payload(), headers=AUTH)
    alpha = client.get("/v1/gateway/requests", headers=AUTH).json()
    beta = client.get("/v1/gateway/requests", headers=BETA_AUTH).json()
    assert alpha["project"] == "alpha"
    assert len(alpha["requests"]) == 1
    assert beta["project"] == "beta"
    assert beta["requests"] == []
    # Reading another project's ledger needs a key for that project.
    cross = client.get("/v1/gateway/requests", params={"project": "beta"}, headers=AUTH)
    assert cross.status_code in (400, 401, 403)


def test_ledger_list_never_returns_prompt_text(client: TestClient) -> None:
    secret = "contact me at operator@example.com about sk-abcdefghijklmnopqrstuvwxyz012345"
    client.post("/v1/chat/completions", json=chat_payload(prompt=secret), headers=AUTH)
    body = client.get("/v1/gateway/requests", headers=AUTH).text
    assert "operator@example.com" not in body
    assert "sk-abcdef" not in body

    request_id = client.get("/v1/gateway/requests", headers=AUTH).json()["requests"][0][
        "request_id"
    ]
    trace = client.get(f"/v1/gateway/requests/{request_id}", headers=AUTH).text
    assert "operator@example.com" not in trace
    assert "sk-abcdef" not in trace


def test_refusal_appears_in_the_ledger_as_a_block() -> None:
    # A rate limit of 1/minute makes the second request a refusal.
    data = base_config()
    data["projects"][0]["requests_per_minute"] = 1
    gateway = build_gateway(data)
    app_client = TestClient(create_app(gateway.config, gateway))
    allowed = app_client.post("/v1/chat/completions", json=chat_payload(), headers=AUTH)
    assert allowed.status_code == 200
    blocked = app_client.post("/v1/chat/completions", json=chat_payload(), headers=AUTH)
    assert blocked.status_code == 429

    rows = app_client.get("/v1/gateway/requests", headers=AUTH).json()["requests"]
    assert [row["outcome"] for row in rows] == ["blocked", "settled"]
    block = rows[0]
    # The routing plan is retained for the forwarded row, absent for the refusal.
    assert rows[1]["decision"]["targets"] == ["primary", "backup"]
    assert block["decision"] is None
    assert block["status"] == "failed"
    assert block["error_code"] == "rate_limited"
    assert block["attempts"] == 0
    assert block["settled_micros"] == 0
    assert block["last_provider"] is None

    trace = app_client.get(f"/v1/gateway/requests/{block['request_id']}", headers=AUTH).json()
    assert trace["attempts"] == []


def test_policy_view_describes_enforced_limits(client: TestClient) -> None:
    body = client.get("/v1/gateway/policy", headers=AUTH).json()
    assert body["project"] == "alpha"
    assert body["monthly_budget_usd"].startswith("$")
    assert body["requests_per_minute"] >= 1
    assert body["max_concurrency"] >= 1
    assert body["routes"], "the simulator needs at least one route"
    target = body["routes"][0]["targets"][0]
    assert set(target) == {
        "provider",
        "model",
        "input_usd_per_million",
        "output_usd_per_million",
    }
    # Credential material must not be reachable from the policy view.
    assert ALPHA_KEY not in str(body)
    assert "api_key" not in str(body).lower()


def test_whoami_lists_only_projects_the_key_can_read(client: TestClient) -> None:
    body = client.get("/v1/gateway/whoami", headers=AUTH).json()
    assert body["project"] == "alpha"
    assert "alpha" in body["readable_projects"]
    assert "beta" not in body["readable_projects"]


def test_provider_health_reports_observed_attempts(client: TestClient) -> None:
    empty = client.get("/v1/gateway/providers", headers=AUTH).json()
    assert empty["providers"] == []

    client.post("/v1/chat/completions", json=chat_payload(), headers=AUTH)
    body = client.get("/v1/gateway/providers", headers=AUTH).json()
    assert body["window_days"] == 7
    primary = next(row for row in body["providers"] if row["provider"] == "primary")
    assert primary["attempts"] == 1
    assert primary["successes"] == 1
    assert primary["failures"] == 0


def test_usage_csv_matches_the_json_report(client: TestClient) -> None:
    client.post("/v1/chat/completions", json=chat_payload(), headers=AUTH)
    report = client.get("/v1/gateway/usage", headers=AUTH).json()
    response = client.get("/v1/gateway/usage.csv", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["project_id"] == "alpha"
    assert int(row["settled_micros"]) == report["settled_micros"]
    assert int(row["requests"]) == report["requests"]
    assert row["settled_usd"] == report["settled_usd"]
    assert row["reconciled"] == "True"


def test_report_window_validation_is_explicit(client: TestClient) -> None:
    for path in ("/v1/gateway/usage", "/v1/gateway/requests", "/v1/gateway/usage.csv"):
        response = client.get(path, params={"days": 0}, headers=AUTH)
        assert response.status_code == 400, path
        assert response.json()["error"]["param"] == "days"

    limit = client.get("/v1/gateway/requests", params={"limit": 500}, headers=AUTH)
    assert limit.status_code == 400
    assert limit.json()["error"]["param"] == "limit"


def test_redaction_counts_are_audited_without_the_matched_value(client: TestClient) -> None:
    data = base_config()
    data["redaction"]["redact_before_forward"] = True
    gateway = build_gateway(data)
    app_client = TestClient(create_app(gateway.config, gateway))
    prompt = "mail operator@example.com and use sk-abcdefghijklmnopqrstuvwxyz012345"
    app_client.post("/v1/chat/completions", json=chat_payload(prompt=prompt), headers=AUTH)

    request_id = app_client.get("/v1/gateway/requests", headers=AUTH).json()["requests"][0][
        "request_id"
    ]
    trace = app_client.get(f"/v1/gateway/requests/{request_id}", headers=AUTH).json()
    events = {event["event"]: event["detail"] for event in trace["audit"]}
    assert "redaction_applied" in events
    detail = events["redaction_applied"]
    assert "email=1" in detail
    assert "openai_key=1" in detail
    # The audit trail names the pattern, never the secret it matched.
    assert "operator@example.com" not in detail
    assert "sk-abcdef" not in detail
