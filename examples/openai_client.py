"""Call the gateway with the official OpenAI Python client.

Point `base_url` at the gateway and use a project key instead of a provider key.
The client is unaware it is talking to a proxy.

    python -m pip install openai
    python examples/openai_client.py
"""

from __future__ import annotations

import os

from openai import OpenAI

client = OpenAI(
    base_url=os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8787/v1"),
    # Demo key from config.example.toml; use your own project key in real use.
    api_key=os.environ.get("GATEWAY_PROJECT_KEY", "demo-key-alpha"),
)

completion = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Explain budget reservations in one sentence."}],
    max_tokens=64,
)

print(completion.choices[0].message.content)
print("tokens:", completion.usage.prompt_tokens, "->", completion.usage.completion_tokens)
