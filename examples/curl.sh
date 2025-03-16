#!/usr/bin/env sh
# Minimal end-to-end walkthrough against a locally running gateway.
#
#   cost-firewall serve --config config.example.toml &
#   sh examples/curl.sh
set -eu

BASE="${GATEWAY_BASE_URL:-http://127.0.0.1:8787}"
KEY="${GATEWAY_PROJECT_KEY:-demo-key-alpha}"

echo "== health =="
curl -fsS "$BASE/healthz"; echo

echo "== dry run: what would this cost, and is it allowed? =="
curl -fsS -X POST "$BASE/v1/gateway/dry-run" \
  -H "authorization: Bearer $KEY" \
  -H "content-type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hello"}],"max_tokens":32}'; echo

echo "== completion (first attempt fails, falls back) =="
curl -fsS -X POST "$BASE/v1/chat/completions" \
  -H "authorization: Bearer $KEY" \
  -H "content-type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hello"}],"max_tokens":32}'; echo

echo "== usage =="
curl -fsS "$BASE/v1/gateway/usage" -H "authorization: Bearer $KEY"; echo

echo "== over budget: lower the cap and observe a 402 =="
echo "See docs/api.md for the full error envelope."
