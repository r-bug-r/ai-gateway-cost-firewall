"""CLI behavior: exit codes, JSON output, and the retention purge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cost_firewall.cli import main
from cost_firewall.ledger import Ledger

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config.example.toml"


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    """Copy of the example config with an isolated ledger path."""
    # TOML basic strings treat backslashes as escapes, so the Windows path must be
    # written with forward slashes.
    db_path = (tmp_path / "gateway.db").as_posix()
    text = EXAMPLE.read_text(encoding="utf-8").replace('path = "gateway.db"', f'path = "{db_path}"')
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_check_config_reports_routes(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "config ok: 2 projects" in out
    assert "gpt-4o-mini -> fake-primary:gpt-4o-mini" in out


def test_check_config_fails_on_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check-config", "--config", str(tmp_path / "nope.toml")]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_hash_key_matches_known_digest(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["hash-key", "demo-key-alpha"]) == 0
    assert capsys.readouterr().out.strip() == (
        "e49052cb77a35e3ebf86f7d184651dbf07b0fc8707d8ecb748fbf759b0a1d129"
    )


def test_dry_run_exits_zero_when_allowed(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COST_FIREWALL_API_KEY", "demo-key-alpha")
    code = main(
        [
            "dry-run",
            "--config",
            str(config_path),
            "--model",
            "gpt-4o-mini",
            "--prompt",
            "hello",
            "--max-tokens",
            "32",
        ]
    )
    assert code == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["allowed"] is True
    assert decision["reservation_micros"] > 0
    assert decision["note"].startswith("Cost figures are estimates")


def test_dry_run_without_key_is_a_configuration_error(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COST_FIREWALL_API_KEY", raising=False)
    code = main(
        ["dry-run", "--config", str(config_path), "--model", "gpt-4o-mini", "--prompt", "hi"]
    )
    assert code == 2
    assert "COST_FIREWALL_API_KEY" in capsys.readouterr().err


def test_dry_run_rejects_bad_key_with_exit_code_1(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COST_FIREWALL_API_KEY", "wrong")
    code = main(
        ["dry-run", "--config", str(config_path), "--model", "gpt-4o-mini", "--prompt", "hi"]
    )
    assert code == 1
    assert "invalid_api_key" in capsys.readouterr().err


def test_usage_and_budget_read_the_same_ledger(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(str(config_path.parent / "gateway.db"))
    from cost_firewall.config import load_config
    from cost_firewall.gateway import Gateway

    config = load_config(config_path)
    gateway = Gateway(config, ledger)
    gateway.chat(
        "Bearer demo-key-alpha",
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16},
    )

    assert main(["usage", "--config", str(config_path), "--project", "alpha", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["requests"] == 1
    assert report["reconciled"] is True

    assert main(["budget", "--config", str(config_path), "--project", "alpha"]) == 0
    budget = json.loads(capsys.readouterr().out)
    assert budget["month_settled_micros"] == report["settled_micros"]


def test_trace_prints_attempts(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from cost_firewall.config import load_config
    from cost_firewall.gateway import Gateway

    config = load_config(config_path)
    gateway = Gateway(config, Ledger(str(config_path.parent / "gateway.db")))
    result = gateway.chat(
        "Bearer demo-key-alpha",
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16},
    )
    assert main(["trace", result.request_id, "--config", str(config_path)]) == 0
    trace = json.loads(capsys.readouterr().out)
    assert trace["status"] == "settled"
    assert trace["attempts"][-1]["provider"] == "fake-backup"


def test_trace_unknown_id_exits_one(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["trace", "req_missing", "--config", str(config_path)]) == 1
    assert "No request" in capsys.readouterr().err


def _settle_one_request(config_path: Path) -> str:
    from cost_firewall.config import load_config
    from cost_firewall.gateway import Gateway

    gateway = Gateway(load_config(config_path), Ledger(str(config_path.parent / "gateway.db")))
    return gateway.chat(
        "Bearer demo-key-alpha",
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16},
    ).request_id


def test_purge_reports_deleted_rows(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            ["purge", "--config", str(config_path), "--project", "alpha", "--retention-days", "30"]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["retention_days"] == 30
    assert result["requests"] == 0


def test_purge_protects_the_current_budget_window(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _settle_one_request(config_path)
    # A zero-day retention asks to delete everything; the current month must survive,
    # otherwise purging would hand the project its spent budget back.
    assert (
        main(["purge", "--config", str(config_path), "--project", "alpha", "--retention-days", "0"])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["budget_window_protected"] is True
    assert result["requests"] == 0
    assert result["cutoff_ms"] < result["requested_cutoff_ms"]

    assert main(["usage", "--config", str(config_path), "--project", "alpha", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["requests"] == 1


def test_forced_purge_deletes_in_window_rows(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _settle_one_request(config_path)
    assert (
        main(
            [
                "purge",
                "--config",
                str(config_path),
                "--project",
                "alpha",
                "--retention-days",
                "0",
                "--force",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["budget_window_protected"] is False
    assert result["requests"] == 1
    assert result["attempts"] >= 1

    assert main(["usage", "--config", str(config_path), "--project", "alpha", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["settled_micros"] == 0


def test_negative_retention_is_rejected(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            ["purge", "--config", str(config_path), "--project", "alpha", "--retention-days", "-1"]
        )
        == 2
    )
    assert "zero or greater" in capsys.readouterr().err


def test_loadtest_in_process_respects_budget(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COST_FIREWALL_API_KEY", "demo-key-alpha")
    code = main(
        [
            "loadtest",
            "--config",
            str(config_path),
            "--concurrency",
            "4",
            "--requests",
            "12",
            "--max-tokens",
            "8",
            "--model",
            "gpt-4o-mini",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert result["within_budget"] is True
    assert result["reconciled"] is True
    assert result["committed_micros"] <= result["monthly_budget_micros"]
    assert code in (0, 4)
