"""Bounded latency telemetry for the intraday execution path."""

from __future__ import annotations

import math
import threading
from collections import deque


class LatencyBook:
    """Keep a bounded, thread-safe sample window per named stage."""

    def __init__(self, window: int = 512) -> None:
        self._window = max(20, min(10_000, int(window)))
        self._samples: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def record_ms(self, stage: str, milliseconds: float) -> None:
        value = float(milliseconds)
        if not stage or not math.isfinite(value) or value < 0.0:
            return
        with self._lock:
            samples = self._samples.setdefault(stage, deque(maxlen=self._window))
            samples.append(value)

    def snapshot(self, budgets_ms: dict[str, float] | None = None) -> dict[str, object]:
        budgets = budgets_ms or {}
        with self._lock:
            copied = {name: list(values) for name, values in self._samples.items()}
        stages: dict[str, object] = {}
        for name, values in sorted(copied.items()):
            ordered = sorted(values)
            budget = budgets.get(name)
            p95 = _percentile(ordered, 0.95)
            stages[name] = {
                "count": len(values),
                "last_ms": round(values[-1], 3),
                "p50_ms": round(_percentile(ordered, 0.50), 3),
                "p95_ms": round(p95, 3),
                "max_ms": round(ordered[-1], 3),
                "budget_ms": round(float(budget), 3) if budget is not None else None,
                "within_budget": p95 <= budget if budget is not None else None,
            }
        return {"sample_window": self._window, "stages": stages}


def _percentile(ordered: list[float], quantile: float) -> float:
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]
