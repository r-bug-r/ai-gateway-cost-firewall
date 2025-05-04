"""Command-line interface: serve, dry-run, usage, budget, trace, purge, hash-key, loadtest."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from cost_firewall.auth import hash_api_key
from cost_firewall.config import Config, ProjectConfig, load_config
from cost_firewall.errors import ConfigError, GatewayError
from cost_firewall.gateway import Gateway
from cost_firewall.ledger import Ledger, now_ms, utc_month_start_ms


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _load(path: str) -> tuple[Config, Ledger, Gateway]:
    config = load_config(path)
    ledger = Ledger(config.database.path)
    return config, ledger, Gateway(config, ledger)


def _resolve_project(config: Config, name: str | None) -> ProjectConfig:
    if name is None:
        if len(config.projects) == 1:
            return config.projects[0]
        raise ConfigError(
            f"Config defines {len(config.projects)} projects; pass --project. "
            f"Available: {', '.join(config.project_ids)}"
        )
    project = config.project(name)
    if project is None:
        raise ConfigError(f"Unknown project '{name}'. Available: {', '.join(config.project_ids)}")
    return project


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from cost_firewall.app import create_app

    config = load_config(args.config)
    ledger = Ledger(config.database.path)
    gateway = Gateway(config, ledger)
    app = create_app(config, gateway)
    host = args.host or config.server.host
    port = args.port or config.server.port
    print(f"cost-firewall listening on http://{host}:{port}", flush=True)
    print(f"ledger: {Path(config.database.path).resolve()}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level=config.server.log_level.lower())
    return 0


def _api_key(args: argparse.Namespace) -> str:
    """Prefer the environment so keys stay out of shell history and process listings."""
    key = args.api_key or os.environ.get("COST_FIREWALL_API_KEY")
    if not key:
        raise ConfigError(
            "Pass --api-key or set COST_FIREWALL_API_KEY (keeps the key out of shell history)."
        )
    return str(key)


def cmd_dry_run(args: argparse.Namespace) -> int:
    _, _, gateway = _load(args.config)
    payload: dict[str, Any] = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_tokens,
    }
    decision = gateway.dry_run(f"Bearer {_api_key(args)}", payload)
    _print_json(decision)
    return 0 if decision["allowed"] else 3


def cmd_usage(args: argparse.Namespace) -> int:
    config, _, gateway = _load(args.config)
    project = _resolve_project(config, args.project)
    since_ms = None
    if args.days is not None:
        since_ms = now_ms() - args.days * 86_400_000
    report = gateway.usage(project, since_ms=since_ms)
    if args.json:
        _print_json(report)
        return 0 if report["reconciled"] else 4
    print(f"project           {report['project_id']}")
    print(f"requests          {report['requests']}")
    print(f"  settled         {report['settled_requests']}")
    print(f"  failed          {report['failed_requests']}")
    print(f"  released        {report['released_requests']}")
    print(f"provider attempts {report['attempts']} ({report['successful_attempts']} succeeded)")
    print(f"input tokens      {report['input_tokens']}")
    print(f"output tokens     {report['output_tokens']}")
    print(f"settled cost      {report['settled_usd']}")
    print(f"avg latency (ms)  {report['avg_latency_ms']}")
    print(f"reconciled        {report['reconciled']}")
    if not report["reconciled"]:
        print("  mismatched: " + ", ".join(report["mismatched_request_ids"]))
    return 0 if report["reconciled"] else 4


def cmd_budget(args: argparse.Namespace) -> int:
    config, _, gateway = _load(args.config)
    project = _resolve_project(config, args.project)
    _print_json(gateway.budget_report(project))
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    _, _, gateway = _load(args.config)
    trace = gateway.request_trace(args.request_id)
    if trace is None:
        print(f"No request {args.request_id} in the ledger.", file=sys.stderr)
        return 1
    _print_json(trace)
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    config, ledger, _ = _load(args.config)
    project = _resolve_project(config, args.project)
    # `0` is a legitimate request ("everything before now"), so test for None.
    retention = args.retention_days
    if retention is None:
        retention = project.retention_days
    if retention is None:
        retention = 30
    if retention < 0:
        raise ConfigError("--retention-days must be zero or greater.")
    requested_cutoff = ledger.retention_cutoff_ms(retention_days=retention)
    # The ledger is the only source of truth for spend, so deleting rows inside the
    # current budget window would silently hand the project its budget back. Keep the
    # window intact unless the operator explicitly forces a hard delete.
    floor_ms = utc_month_start_ms(now_ms())
    # Cap the cutoff at the window start: rows inside the current month must survive.
    cutoff = requested_cutoff if args.force else min(requested_cutoff, floor_ms)
    deleted = ledger.purge_older_than(cutoff_ms=cutoff)
    ledger.audit(
        event="retention_purge",
        detail=(
            f"Deleted {deleted['requests']} requests, {deleted['attempts']} attempts, "
            f"{deleted['audit_events']} audit events older than {retention} days "
            f"(cutoff {cutoff}ms, forced={bool(args.force)})."
        ),
        project_id=project.id,
    )
    _print_json(
        {
            "retention_days": retention,
            "requested_cutoff_ms": requested_cutoff,
            "cutoff_ms": cutoff,
            "budget_window_protected": cutoff != requested_cutoff,
            **deleted,
        }
    )
    return 0


def cmd_hash_key(args: argparse.Namespace) -> int:
    print(hash_api_key(args.key))
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(
        f"config ok: {len(config.projects)} projects, {len(config.providers)} providers, "
        f"{len(config.routes)} routes"
    )
    for route in config.routes:
        targets = ", ".join(f"{t.provider}:{t.model}" for t in route.targets)
        print(f"  {route.model} -> {targets}")
    return 0


def cmd_loadtest(args: argparse.Namespace) -> int:
    from cost_firewall.loadtest import run_loadtest

    result = run_loadtest(
        config_path=args.config,
        api_key=_api_key(args),
        project=args.project,
        concurrency=args.concurrency,
        requests=args.requests,
        model=args.model,
        max_tokens=args.max_tokens,
        base_url=args.base_url,
        database_path=args.database,
    )
    _print_json(result)
    return 0 if result["within_budget"] and result["reconciled"] else 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cost-firewall",
        description="Self-hostable OpenAI-compatible gateway with spend budgets and redaction.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the HTTP gateway.")
    serve.add_argument("--config", default="config.toml")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=cmd_serve)

    dry = sub.add_parser("dry-run", help="Show the routing and budget decision without forwarding.")
    dry.add_argument("--config", default="config.toml")
    dry.add_argument("--api-key", default=None, help="Or set COST_FIREWALL_API_KEY.")
    dry.add_argument("--model", required=True)
    dry.add_argument("--prompt", required=True)
    dry.add_argument("--max-tokens", type=int, default=256)
    dry.set_defaults(func=cmd_dry_run)

    usage = sub.add_parser("usage", help="Print the usage report from the ledger.")
    usage.add_argument("--config", default="config.toml")
    usage.add_argument("--project", default=None)
    usage.add_argument("--days", type=int, default=None)
    usage.add_argument("--json", action="store_true")
    usage.set_defaults(func=cmd_usage)

    budget = sub.add_parser("budget", help="Print the current budget snapshot.")
    budget.add_argument("--config", default="config.toml")
    budget.add_argument("--project", default=None)
    budget.set_defaults(func=cmd_budget)

    trace = sub.add_parser("trace", help="Print one request's attempts from the ledger.")
    trace.add_argument("request_id")
    trace.add_argument("--config", default="config.toml")
    trace.set_defaults(func=cmd_trace)

    purge = sub.add_parser("purge", help="Delete ledger rows older than the retention window.")
    purge.add_argument("--config", default="config.toml")
    purge.add_argument("--project", default=None)
    purge.add_argument("--retention-days", type=int, default=None)
    purge.add_argument(
        "--force",
        action="store_true",
        help=(
            "Also delete rows inside the current budget window. This resets "
            "in-period spend accounting for the project."
        ),
    )
    purge.set_defaults(func=cmd_purge)

    hash_key = sub.add_parser("hash-key", help="Print the SHA-256 digest for a project API key.")
    hash_key.add_argument("key")
    hash_key.set_defaults(func=cmd_hash_key)

    check = sub.add_parser("check-config", help="Validate configuration and print routes.")
    check.add_argument("--config", default="config.toml")
    check.set_defaults(func=cmd_check_config)

    load = sub.add_parser("loadtest", help="Send concurrent requests and verify no overspend.")
    load.add_argument("--config", default="config.toml")
    load.add_argument("--api-key", default=None, help="Or set COST_FIREWALL_API_KEY.")
    load.add_argument("--project", default=None)
    load.add_argument("--concurrency", type=int, default=16)
    load.add_argument("--requests", type=int, default=64)
    load.add_argument("--model", default=None)
    load.add_argument("--max-tokens", type=int, default=256)
    load.add_argument("--base-url", default=None)
    load.add_argument(
        "--database",
        default=None,
        help="Ledger file to use. Defaults to a throwaway ledger so a benchmark "
        "never consumes the deployment's budget.",
    )
    load.set_defaults(func=cmd_loadtest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
        return result
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except GatewayError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
