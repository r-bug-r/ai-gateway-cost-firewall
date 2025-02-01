"""In-process metrics with a Prometheus text exposition."""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any

METRIC_HELP = {
    "gateway_requests_total": "Requests handled, by project, route model, and outcome.",
    "gateway_attempts_total": "Provider attempts, by provider, model, and outcome.",
    "gateway_tokens_total": "Tokens reported by providers, by project and direction.",
    "gateway_cost_micros_total": "Settled cost in micro-USD, by project and provider.",
    "gateway_rejections_total": "Requests rejected before forwarding, by reason.",
    "gateway_reservations_total": "Budget reservations, by project and outcome.",
}


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, Counter[tuple[str, ...]]] = {
            name: Counter() for name in METRIC_HELP
        }

    def inc(self, metric: str, labels: tuple[str, ...], value: int = 1) -> None:
        with self._lock:
            self._counters[metric][labels] += value

    def value(self, metric: str, labels: tuple[str, ...]) -> int:
        with self._lock:
            return self._counters[metric][labels]

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            snapshot = {name: dict(counter) for name, counter in self._counters.items()}
        for name, help_text in METRIC_HELP.items():
            counter = snapshot[name]
            lines.append(f"# HELP {name} {help_text}")
            metric_type = "counter"
            lines.append(f"# TYPE {name} {metric_type}")
            if not counter:
                continue
            for labels, value in sorted(counter.items()):
                label_text = ",".join(
                    f'{key}="{_escape(label)}"'
                    for key, label in zip(_label_names(name), labels, strict=True)
                )
                lines.append(f"{name}{{{label_text}}} {value}")
        return "\n".join(lines) + "\n"

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                name: {"|".join(labels): value for labels, value in counter.items()}
                for name, counter in self._counters.items()
            }


_LABELS = {
    "gateway_requests_total": ("project", "model", "outcome"),
    "gateway_attempts_total": ("provider", "model", "outcome"),
    "gateway_tokens_total": ("project", "direction"),
    "gateway_cost_micros_total": ("project", "provider"),
    "gateway_rejections_total": ("reason",),
    "gateway_reservations_total": ("project", "outcome"),
}


def _label_names(metric: str) -> tuple[str, ...]:
    return _LABELS[metric]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
