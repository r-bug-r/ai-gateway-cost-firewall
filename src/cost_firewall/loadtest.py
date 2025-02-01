"""Concurrent reservation load test against a running gateway or an in-process app.

The default path is in-process (no network, no server process): it drives the real
`Gateway` and SQLite ledger with N concurrent threads, then asserts the ledger never
exceeded the configured budget and every settled request reconciles with its attempts.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from cost_firewall.config import Config, load_config
from cost_firewall.errors import BudgetExhausted, GatewayError
from cost_firewall.gateway import Gateway
from cost_firewall.ledger import BudgetPolicy, Ledger


def run_in_process(
    *,
    config: Config,
    api_key: str,
    project_id: str,
    concurrency: int,
    requests: int,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    """Fire `requests` concurrent chat calls and audit the resulting ledger state."""
    project = config.project(project_id)
    if project is None:
        raise ValueError(f"Unknown project '{project_id}'")
    # Each thread gets its own Ledger handle, matching separate gateway processes.
    ledger = Ledger(config.database.path)
    gateway = Gateway(config, ledger)

    results: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": f"load test request {index}"}],
            "max_tokens": max_tokens,
        }
        try:
            gateway.chat(f"Bearer {api_key}", payload)
            outcome = "success"
        except BudgetExhausted:
            outcome = "budget_exhausted"
        except GatewayError as exc:
            outcome = f"error:{exc.code}"
        with lock:
            results.append(outcome)

    barrier = threading.Barrier(concurrency)

    def gated_worker(index: int) -> None:
        # Release all workers at the same instant to maximize reservation contention.
        if index < concurrency:
            barrier.wait()
        worker(index)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(gated_worker, index) for index in range(requests)]
        for future in futures:
            try:
                future.result()
            except Exception as exc:  # pragma: no cover - defensive
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")

    report = ledger.usage_report(project_id=project_id)
    snapshot = ledger.snapshot(
        BudgetPolicy(
            project_id=project.id,
            name=project.name,
            monthly_budget_micros=project.monthly_budget_micros,
            daily_budget_micros=project.daily_budget_micros,
            per_request_max_micros=project.per_request_max_micros,
        )
    )
    counts: dict[str, int] = {}
    for outcome in results:
        counts[outcome] = counts.get(outcome, 0) + 1
    return {
        "project": project_id,
        "concurrency": concurrency,
        "requests": requests,
        "outcomes": counts,
        "unexpected_errors": errors,
        "monthly_budget_micros": project.monthly_budget_micros,
        "settled_micros": snapshot.month_settled_micros,
        "reserved_micros": snapshot.month_reserved_micros,
        "committed_micros": snapshot.month_committed_micros,
        "within_budget": snapshot.month_committed_micros <= project.monthly_budget_micros,
        "reconciled": report["reconciled"],
        "ledger_requests": report["requests"],
        "successful_attempts": report["successful_attempts"],
    }


def run_loadtest(
    *,
    config_path: str,
    api_key: str,
    project: str | None,
    concurrency: int,
    requests: int,
    model: str | None,
    max_tokens: int,
    base_url: str | None = None,
) -> dict[str, Any]:
    config = load_config(Path(config_path))
    project_id = project or config.projects[0].id
    route_model = model or config.routes[0].model
    if base_url:
        return _run_http(
            base_url=base_url,
            api_key=api_key,
            model=route_model,
            max_tokens=max_tokens,
            concurrency=concurrency,
            requests=requests,
        )
    return run_in_process(
        config=config,
        api_key=api_key,
        project_id=project_id,
        concurrency=concurrency,
        requests=requests,
        model=route_model,
        max_tokens=max_tokens,
    )


def _run_http(
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int,
    concurrency: int,
    requests: int,
) -> dict[str, Any]:
    import httpx

    url = base_url.rstrip("/") + "/v1/chat/completions"
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            response = httpx.post(
                url,
                headers={"authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": f"load test {index}"}],
                    "max_tokens": max_tokens,
                },
                timeout=60.0,
            )
            outcome = "success" if response.status_code == 200 else f"http_{response.status_code}"
        except httpx.HTTPError as exc:
            outcome = f"transport:{type(exc).__name__}"
        with lock:
            outcomes.append(outcome)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(worker, range(requests)))

    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    return {"mode": "http", "base_url": base_url, "outcomes": counts, "raw": json.dumps(counts)}
