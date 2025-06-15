"""Send requests until the hard monthly cap refuses one, and show the ledger.

    python examples/cost-control/demo.py

Runs the real `Gateway` against the config next to this file, with the ledger in a
temporary directory so repeated runs start from zero spend. The provider is fake:
nothing leaves the machine and nothing costs money.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cost_firewall.config import load_config  # noqa: E402
from cost_firewall.errors import BudgetExhausted, PerRequestLimitExceeded  # noqa: E402
from cost_firewall.gateway import Gateway  # noqa: E402
from cost_firewall.ledger import Ledger  # noqa: E402
from cost_firewall.money import format_usd  # noqa: E402

CONFIG = Path(__file__).with_name("config.toml")
PROJECT = "app"
API_KEY = "demo-key-alpha"
MAX_TOKENS = 512

PAYLOAD = {
    "model": "assistant",
    "messages": [{"role": "user", "content": "Summarise the release notes."}],
    "max_tokens": MAX_TOKENS,
}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cost-control-") as workdir:
        raw = CONFIG.read_text(encoding="utf-8").replace(
            'path = "cost-control.db"',
            f'path = "{(Path(workdir) / "ledger.db").as_posix()}"',
        )
        config_path = Path(workdir) / "config.toml"
        config_path.write_text(raw, encoding="utf-8")

        config = load_config(config_path)
        project = config.project(PROJECT)
        if project is None:
            raise SystemExit(f"config has no project '{PROJECT}'")

        ledger = Ledger(config.database.path)
        gateway = Gateway(config, ledger)
        try:
            print(f"monthly cap   {format_usd(project.monthly_budget_micros)}")
            print(f"daily cap     {format_usd(project.daily_budget_micros or 0)}")
            print(f"max_tokens    {MAX_TOKENS}")
            print()

            allowed = 0
            for attempt in range(1, 100):
                try:
                    result = gateway.chat(f"Bearer {API_KEY}", PAYLOAD)
                except (BudgetExhausted, PerRequestLimitExceeded) as exc:
                    print(f"request {attempt:>2}  REFUSED  {exc.code}: {exc.message}")
                    break
                allowed += 1
                print(
                    f"request {attempt:>2}  ok       "
                    f"settled {format_usd(result.cost_micros)} "
                    f"via {result.provider} ({len(result.attempts)} attempt(s))"
                )

            usage = gateway.usage(project)
            snapshot = gateway.budget_report(project)
            print()
            print(f"allowed       {allowed}")
            print(f"settled       {usage['settled_usd']}")
            print(f"remaining     {snapshot['month_remaining_usd']}")
            print(f"reconciled    {usage['reconciled']}")

            # The refused request must not have reached a provider.
            attempts = usage["attempts"]
            print(f"attempt rows  {attempts} for {usage['requests']} requests")

            ok = (
                usage["reconciled"]
                and usage["settled_micros"] <= project.monthly_budget_micros
                and attempts == allowed
            )
            print()
            print("CAP HELD" if ok else "CAP VIOLATED")
            return 0 if ok else 1
        finally:
            ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
