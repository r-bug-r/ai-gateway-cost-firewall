"""HTTP load probe: measures the transport path, not just the ledger.

    python scripts/http_bench.py --requests 400 --concurrency 16

Starts a real uvicorn server on a free port with `config.example.toml`, fires
`--requests` chat completions at `--concurrency` workers, then reads the usage
report and checks the two invariants that matter: settled spend never exceeds
the configured cap, and every settled request reconciled.

The number this prints is dominated by the Python client threads on the same
machine; use it to detect regressions and to compare against the in-process
`cost-firewall loadtest`, not as a capacity claim.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cost_firewall.config import load_config  # noqa: E402

PROJECT = "alpha"
API_KEY = "demo-key-alpha"
BODY = json.dumps(
    {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "load probe"}],
        "max_tokens": 32,
    }
).encode()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(base: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise SystemExit("server did not become healthy")


def post(url: str, latencies: list[float], statuses: dict[int, int], lock: threading.Lock) -> None:
    request = urllib.request.Request(
        url,
        data=BODY,
        headers={"authorization": f"Bearer {API_KEY}", "content-type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        exc.read()
        status = exc.code
    elapsed = (time.perf_counter() - started) * 1000
    with lock:
        latencies.append(elapsed)
        statuses[status] = statuses.get(status, 0) + 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=400)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--config", default="config.example.toml")
    args = parser.parse_args()

    config = load_config(ROOT / args.config)
    project = config.project(PROJECT)
    if project is None:
        raise SystemExit(f"config has no project '{PROJECT}'")
    if args.concurrency > project.max_concurrency:
        print(
            f"warning: concurrency {args.concurrency} exceeds "
            f"{PROJECT}.max_concurrency={project.max_concurrency}; expect 429s",
            file=sys.stderr,
        )

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

    # Isolated ledger and port: a previous run must not change this run's budget.
    workdir = tempfile.TemporaryDirectory(prefix="cost-firewall-bench-")
    config_path = Path(workdir.name) / "config.toml"
    config_path.write_text(
        (ROOT / args.config)
        .read_text(encoding="utf-8")
        .replace(
            'path = "gateway.db"',
            # TOML basic strings treat backslashes as escapes; use POSIX form.
            f'path = "{(Path(workdir.name) / "bench.db").as_posix()}"',
        ),
        encoding="utf-8",
    )
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cost_firewall.cli",
            "serve",
            "--config",
            str(config_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_ready(base)
        url = f"{base}/v1/chat/completions"
        latencies: list[float] = []
        statuses: dict[int, int] = {}
        lock = threading.Lock()
        started = time.perf_counter()

        def worker(count: int) -> None:
            for _ in range(count):
                post(url, latencies, statuses, lock)

        base_count, extra = divmod(args.requests, args.concurrency)
        threads = [
            threading.Thread(target=worker, args=(base_count + (1 if i < extra else 0),))
            for i in range(args.concurrency)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wall = time.perf_counter() - started

        usage_request = urllib.request.Request(
            f"{base}/v1/gateway/usage",
            headers={"authorization": f"Bearer {API_KEY}"},
        )
        with urllib.request.urlopen(usage_request, timeout=10) as response:
            usage = json.loads(response.read())
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        # Windows may still hold the SQLite handle for a moment after exit.
        with contextlib.suppress(PermissionError):
            workdir.cleanup()

    ordered = sorted(latencies)
    p50 = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    print(f"requests      {args.requests} at concurrency {args.concurrency}")
    print(f"wall          {wall:.2f}s ({args.requests / wall:.1f} req/s)")
    print(f"statuses      {statuses}")
    print(f"latency       p50 {p50:.0f}ms  p95 {p95:.0f}ms  max {ordered[-1]:.0f}ms")
    print(f"reconciled    {usage['reconciled']}")
    print(f"settled       {usage['settled_usd']} over {usage['settled_requests']} requests")

    ok = True
    if not usage["reconciled"]:
        print("FAIL unreconciled rows", file=sys.stderr)
        ok = False
    if usage["settled_micros"] > project.monthly_budget_micros:
        print("FAIL settled spend exceeded the monthly cap", file=sys.stderr)
        ok = False
    if statuses.get(200) != args.requests:
        print("note: not every request succeeded (see statuses)", file=sys.stderr)
    print("BENCH OK" if ok else "BENCH FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
