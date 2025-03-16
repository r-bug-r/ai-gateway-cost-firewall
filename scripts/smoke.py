#!/usr/bin/env python3
"""End-to-end smoke scenario over real HTTP against a real uvicorn process.

This is the release gate from the implementation plan:

1. send requests through the proxy to a fake provider;
2. exhaust a project budget and verify the next request is rejected *before*
   forwarding (no attempt row is created for it);
3. simulate a transient provider failure and verify the allowed fallback with
   separate ledger entries;
4. verify a permanent failure never falls back;
5. verify secrets do not appear in error bodies, traces, or logs;
6. verify usage reconciles with the ledger and never exceeds the budget.

It uses only the standard library and a temporary directory, so it runs in CI
with no credentials and no network access.

Usage:
    python scripts/smoke.py            # prints PASS/FAIL per check
    python scripts/smoke.py --verbose  # also streams server logs
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEMO_KEY = "smoke-key-alpha"
SECRET = "sk-live-TOPSECRET-0123456789"
# 6 input tokens + 32 output tokens at $0.15/$0.60 per 1M = 20 micro-USD reserved.
BUDGET_USD = 0.000050

CONFIG = """
[server]
host = "127.0.0.1"
port = {port}
max_body_bytes = 1048576
request_timeout_seconds = 10.0
log_prompts = false
fail_closed = true
log_level = "INFO"

[database]
path = "{db}"

[limits]
max_messages = 64
max_input_chars = 200000
default_max_output_tokens = 512
max_output_tokens = 4096
reservation_ttl_seconds = 30

[redaction]
redact_before_forward = false

[[projects]]
id = "alpha"
name = "Smoke alpha"
api_keys = ["{key_hash}"]
monthly_budget_usd = {budget_usd}
daily_budget_usd = {budget_usd}
per_request_max_usd = 0.005
requests_per_minute = 600
max_concurrency = 8
retention_days = 30
allowed_models = ["gpt-4o-mini", "strict-model"]

[[providers]]
id = "fake-primary"
type = "fake"
fail_first = 2
fail_status = 503
fail_class = "transient"

[[providers]]
id = "fake-backup"
type = "fake"

[[providers]]
id = "fake-permanent"
type = "fake"
fail_first = 1000
fail_status = 400
fail_class = "permanent"

[[routes]]
model = "gpt-4o-mini"

[[routes.targets]]
provider = "fake-primary"
model = "gpt-4o-mini"
input_usd_per_1m = 0.15
output_usd_per_1m = 0.60

[[routes.targets]]
provider = "fake-backup"
model = "gpt-4o-mini"
input_usd_per_1m = 0.15
output_usd_per_1m = 0.60

[[routes]]
model = "strict-model"

[[routes.targets]]
provider = "fake-permanent"
model = "strict-model"
input_usd_per_1m = 0.10
output_usd_per_1m = 0.40
"""

failures: list[str] = []
verbose = False


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{f' :: {detail}' if detail else ''}")
    if not ok:
        failures.append(label)


def request(
    method: str, url: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    all_headers = {"content-type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        status = exc.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def chat(
    base: str, model: str, prompt: str, *, max_tokens: int = 32, idempotent: bool = True
) -> tuple[int, dict[str, Any]]:
    status, payload = request(
        "POST",
        f"{base}/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "idempotent": idempotent,
        },
        {"authorization": f"Bearer {DEMO_KEY}"},
    )
    assert isinstance(payload, dict), payload
    return status, payload


def wait_for_ready(base: str, proc: subprocess.Popen[str], log_path: Path) -> bool:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(log_path.read_text(encoding="utf-8", errors="replace"))
            return False
        try:
            status, payload = request("GET", f"{base}/healthz")
            if status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
                return True
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(0.2)
    return False


def main() -> int:
    global verbose
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--port", type=int, default=None, help="Defaults to a free port.")
    args = parser.parse_args()
    verbose = args.verbose
    if args.port is None:
        # Bind port 0 to let the OS pick a free port, then release it for the child.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            args.port = int(probe.getsockname()[1])

    sys.path.insert(0, str(SRC))
    from cost_firewall.auth import hash_api_key

    workdir = Path(tempfile.mkdtemp(prefix="cost-firewall-smoke-"))
    config_path = workdir / "config.toml"
    config_path.write_text(
        CONFIG.format(
            port=args.port,
            db=(workdir / "gateway.db").as_posix(),
            key_hash=hash_api_key(DEMO_KEY),
            budget_usd=f"{BUDGET_USD:.6f}",
        ),
        encoding="utf-8",
    )
    log_path = workdir / "server.log"
    log_file = log_path.open("w", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cost_firewall.cli",
            "serve",
            "--config",
            str(config_path),
            "--port",
            str(args.port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{args.port}"
    try:
        if not wait_for_ready(base, proc, log_path):
            check("server starts and reports healthy", False, "see log above")
            return 1
        check("server starts and reports healthy", True, base)

        # --- 1. transient failure -> allowed fallback, two ledger attempts -----
        status, body = chat(base, "gpt-4o-mini", "hello smoke")
        gateway = body.get("gateway", {}) if isinstance(body, dict) else {}
        attempts = gateway.get("attempts", [])
        check(
            "first request succeeds through fallback",
            status == 200 and gateway.get("provider") == "fake-backup",
        )
        check(
            "fallback records both attempts with classifications",
            len(attempts) == 2
            and attempts[0]["error_class"] == "transient"
            and attempts[1]["status"] == "success",
            json.dumps(attempts),
        )
        check("only the successful attempt is billable", attempts[0]["cost_micros"] == 0)
        request_id = gateway.get("request_id", "")

        # --- 2. permanent failure never falls back -----------------------------
        status, body = chat(base, "strict-model", "permanent failure please")
        error = body.get("error", {}) if isinstance(body, dict) else {}
        check(
            "permanent upstream failure is a stable client error",
            status == 502 and error.get("code") == "upstream_error",
            f"{status} {error.get('code')}",
        )
        check(
            "permanent failure carries no fallback provider",
            "gateway" not in body or not body["gateway"].get("provider"),
        )

        # --- 3. exhaust the budget, then prove no forwarding happens -----------
        def attempt_count() -> int:
            _, report = request(
                "GET", f"{base}/v1/gateway/usage", headers={"authorization": f"Bearer {DEMO_KEY}"}
            )
            assert isinstance(report, dict)
            return int(report["attempts"])

        rejected: dict[str, Any] = {}
        successes = 0
        for _ in range(10):
            before = attempt_count()
            status, body = chat(base, "gpt-4o-mini", "budget probe")
            if status == 429:
                rejected = body
                break
            successes += 1
        after_rejection = attempt_count()
        check(
            "rejected request is never forwarded to a provider",
            after_rejection == before,
            f"attempts stayed at {after_rejection}",
        )
        check(
            "budget is eventually exhausted",
            bool(rejected),
            f"{successes} requests succeeded first",
        )
        code = rejected.get("error", {}).get("code") if isinstance(rejected, dict) else None
        check("exhaustion uses the documented error code", code == "budget_exhausted", str(code))

        # --- 4. reports reconcile and never exceed the cap ---------------------
        status, usage = request(
            "GET", f"{base}/v1/gateway/usage", headers={"authorization": f"Bearer {DEMO_KEY}"}
        )
        assert isinstance(usage, dict)
        check(
            "usage report reconciles with the ledger",
            status == 200 and usage.get("reconciled") is True,
        )
        check(
            "settled spend never exceeds the budget",
            usage.get("settled_micros", 1) <= round(BUDGET_USD * 1_000_000),
            f"{usage.get('settled_micros')} <= {round(BUDGET_USD * 1_000_000)} micro-USD",
        )
        check(
            "every failed attempt is recorded but not billed",
            usage.get("failed_requests", 0) >= 1 and usage.get("settled_requests") == successes + 1,
            f"attempts={usage.get('attempts')} settled={usage.get('settled_requests')} "
            f"failed={usage.get('failed_requests')}",
        )

        status, budget = request(
            "GET", f"{base}/v1/gateway/budget", headers={"authorization": f"Bearer {DEMO_KEY}"}
        )
        assert isinstance(budget, dict)
        check(
            "budget snapshot matches the usage report",
            budget.get("month_settled_micros") == usage.get("settled_micros"),
            f"{budget.get('month_settled_micros')} == {usage.get('settled_micros')}",
        )

        # --- 5. trace and redaction -------------------------------------------
        status, trace = request(
            "GET",
            f"{base}/v1/gateway/requests/{request_id}",
            headers={"authorization": f"Bearer {DEMO_KEY}"},
        )
        assert isinstance(trace, dict)
        check(
            "trace is readable for a settled request",
            status == 200 and trace.get("status") == "settled",
        )
        check("trace does not store the prompt", "hello smoke" not in json.dumps(trace))

        status, body = chat(base, "gpt-4o-mini", f"my key is {SECRET}")
        serialized = json.dumps(body)
        check("secret never appears in a response body", SECRET not in serialized)
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        check("secret never appears in server logs", SECRET not in log_text)
        check("api key never appears in server logs", DEMO_KEY not in log_text)

        # --- 6. metrics --------------------------------------------------------
        status, metrics = request("GET", f"{base}/metrics")
        assert isinstance(metrics, str)
        missing = [
            name
            for name in (
                "gateway_requests_total",
                "gateway_attempts_total",
                "gateway_tokens_total",
                "gateway_cost_micros_total",
                "gateway_rejections_total",
                'reason="monthly_budget_exhausted"',
            )
            if name not in metrics
        ]
        check(
            "metrics expose request, attempt, token, cost, and rejection counters",
            status == 200 and not missing,
            f"status={status} missing={missing}",
        )

        # --- 7. auth and validation -------------------------------------------
        status, body = request(
            "POST",
            f"{base}/v1/chat/completions",
            {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        check("missing credentials are rejected", status == 401)
        status, body = request(
            "POST",
            f"{base}/v1/chat/completions",
            {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            {"authorization": f"Bearer {DEMO_KEY}"},
        )
        check(
            "streaming is rejected with a documented error",
            status == 400 and body.get("error", {}).get("param") == "stream",
        )
        status, body = request(
            "POST",
            f"{base}/v1/chat/completions",
            {"model": "unknown-model", "messages": [{"role": "user", "content": "hi"}]},
            {"authorization": f"Bearer {DEMO_KEY}"},
        )
        check(
            "model allowlist is enforced",
            status == 404 and body.get("error", {}).get("code") == "model_not_found",
        )

        if verbose:
            print("---- server log ----")
            print(log_path.read_text(encoding="utf-8", errors="replace"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print()
    if failures:
        print(f"SMOKE FAILED: {len(failures)} check(s) failed")
        for item in failures:
            print(f"  - {item}")
        print(f"logs and ledger kept at {workdir}")
        return 1
    print("SMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
