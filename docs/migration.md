# Migrating from a direct provider SDK

The gateway speaks the OpenAI chat-completions shape. For most applications the change is a
base URL, an API key, and one field.

## 1. Run the gateway

```bash
docker run -d --name cost-firewall -p 8787:8787 \
  -v cost-firewall-data:/data \
  -v "$PWD/config.toml:/config/config.toml:ro" \
  -e GATEWAY_OPENAI_API_KEY \
  cost-firewall
```

Or from a checkout: `cost-firewall serve --config config.toml`. Verify with
`curl -s http://127.0.0.1:8787/healthz`.

## 2. Create a project and a key

In `config.toml`:

```toml
[[projects]]
id = "my-app"
name = "My application"
api_keys = ["<output of: cost-firewall hash-key YOUR_KEY>"]
monthly_budget_usd = 20.00
daily_budget_usd = 2.00
per_request_max_usd = 0.05
requests_per_minute = 120
max_concurrency = 8
retention_days = 30
allowed_models = ["gpt-4o-mini"]
```

The plaintext key never goes in the file — only its digest. Keep the key in your
application's secret store.

## 3. Point the client at the gateway

**OpenAI Python SDK**

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    api_key=os.environ["GATEWAY_PROJECT_KEY"],
)
```

**Anything else that takes a base URL** (LangChain, LlamaIndex, Vercel AI SDK, LiteLLM,
`curl`): set the base URL to `http://127.0.0.1:8787/v1` and use the project key as the
bearer token.

**Anthropic SDK users** have two options: keep the Anthropic SDK and route the route model
`claude-haiku` to an `anthropic` provider target (the gateway translates the OpenAI-shaped
request), or switch the client to the OpenAI-compatible shape. The first keeps your code
closer to what you have.

## 4. Remove what the gateway now owns

| In your application | After migration |
| --- | --- |
| Provider API keys | Only in the gateway's environment |
| Retry and fallback loops between providers | Route targets, ordered, with classified fallback |
| Token counting for cost estimation | Gateway ledger (`usage`, `trace`) |
| Monthly spend dashboards | `GET /v1/gateway/usage` or `cost-firewall usage` |
| Secret scrubbing in logs | Gateway redaction for its own logs; keep yours for yours |

Keep your own timeouts and your own error handling: the gateway returns stable error codes
(`docs/api.md`), but it cannot know what your application should do when a request is
refused.

## 5. What will break

- **Streaming.** `stream: true` is rejected. If your UI depends on token streaming, the
  gateway is not a drop-in replacement yet.
- **Provider-specific fields.** `tools`, `response_format`, `logprobs`, and similar are
  rejected rather than dropped, so a request that works directly will fail loudly here. Add
  support deliberately or keep those calls direct.
- **Non-chat endpoints.** Embeddings, images, and audio are not proxied.
- **`429` semantics.** The gateway returns `429` for rate limits *and* `402` for budget
  exhaustion. Handle `402` as "stop and tell a human", not as "retry with backoff".
- **Latency.** One extra hop and two SQLite commits. Measured numbers are in
  `docs/operations.md`; measure on your hardware.

## 6. Verify before you cut over

1. `cost-firewall dry-run --config config.toml --model gpt-4o-mini --prompt "hello"` — check
   the route, the reservation, and that `allowed` is true.
2. Send a real request through the gateway with `max_tokens` small, then
   `cost-firewall trace <request_id>`.
3. Compare that request's `cost_usd` with the provider's dashboard for the same period.
   A difference is expected in the third decimal; a large difference means the price table
   in `config.toml` is wrong.
4. Keep the direct path available as an escape hatch, as `docs/operations.md` recommends,
   until you have reconciled one billing period.
