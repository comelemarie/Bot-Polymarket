"""Compteurs et latences en memoire, vides periodiquement dans la table `metrics`."""
from __future__ import annotations

from collections import defaultdict

from core.store import Store


class Metrics:
    def __init__(self):
        self.counters: dict[str, float] = defaultdict(float)
        self.latencies: dict[str, list[float]] = defaultdict(list)

    def inc(self, name: str, value: float = 1.0) -> None:
        self.counters[name] += value

    def observe(self, name: str, seconds: float) -> None:
        self.latencies[name].append(seconds)

    def snapshot(self) -> dict[str, float]:
        out = dict(self.counters)
        for name, values in self.latencies.items():
            if values:
                s = sorted(values)
                out[f"{name}.p50"] = s[len(s) // 2]
                out[f"{name}.p95"] = s[min(len(s) - 1, int(len(s) * 0.95))]
                out[f"{name}.n"] = len(s)
        return out

    def flush(self, store: Store, ts: float) -> dict[str, float]:
        snap = self.snapshot()
        for name, value in snap.items():
            store.insert("metrics", {"ts": ts, "name": name, "value": value})
        self.latencies.clear()
        return snap
