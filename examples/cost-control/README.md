# Hard monthly cap: worked example

A runnable demonstration of the property the gateway exists for: a project cannot spend past
its cap, and the request that would cross it is refused **before** it reaches a provider.

## Files

- `config.toml` — one project, one route, a $0.25 monthly cap and a $0.02 daily cap.
- `demo.py` — starts the gateway in-process against a throwaway ledger and sends requests
  until the cap is reached, printing each decision.

```bash
python examples/cost-control/demo.py
```

The demo uses a **fake** provider, so it costs nothing and needs no credentials. Prices in
`config.toml` are illustrative; verify current provider prices before copying this file into
a deployment.

## What the demo shows

1. The first requests are allowed and settle at the provider-reported cost.
2. Once the worst-case reservation no longer fits, the next request is rejected with
   `budget_exhausted`.
3. The rejected request leaves **no attempt row** — it was never forwarded.
4. Settled spend never exceeds the cap, and the usage report reconciles.

## Applying it to a real deployment

1. Replace the fake provider with your real one (see `config.live.example.toml`).
2. Set `monthly_budget_usd` to an amount you are willing to lose entirely. It is a hard
   ceiling, not a target.
3. Set `per_request_max_usd` to the largest single call you would accept, and keep
   `limits.max_output_tokens` consistent with it.
4. Keep `fail_closed = true`. If the ledger is unreadable, the gateway refuses to forward
   rather than spend blind.
5. Reconcile monthly: compare `cost-firewall usage` against the provider invoice. Timeouts
   without usage evidence are released, not charged, so the gateway's number is normally the
   lower of the two.
