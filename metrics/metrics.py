"""Prometheus metrics facade (degrades to no-op without prometheus-client)."""

from __future__ import annotations

from typing import Any

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

    _HAVE_PROM = True
except ImportError:  # pragma: no cover
    _HAVE_PROM = False


_METRICS = [
    "lots_fetched",
    "lots_fetch_failed",
    "lots_profitable",
    "mid_level_hits",
    "scheduler_deferred",
    "meta_events",
    "seasonal_events",
    "ws_clients",
    "history_rows",
    "deals_total",
    "notifications_sent",
    "notifications_failed",
    # AUDIT Q4: these were incremented by chart_worker but never declared,
    # so they never reached /metrics.
    "chart_attach_failed",
    "chart_queue_dropped",
    "chart_render_failed",
    "charts_attached",
]


class MetricsCollector:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and _HAVE_PROM
        self._local: dict[str, float] = {name: 0.0 for name in _METRICS}
        if self.enabled:
            self.registry = CollectorRegistry()
            self._counters = {
                name: Counter(f"stalzone_{name}", f"stalzone {name}", registry=self.registry)
                for name in _METRICS
            }
            self._gauges: dict[str, Any] = {}
            self._labeled: dict[str, Any] = {}

    def inc(self, name: str, value: float = 1.0) -> None:
        self._local[name] = self._local.get(name, 0.0) + value
        if self.enabled and name in self._counters:
            self._counters[name].inc(value)

    def inc_labeled(self, name: str, labels: dict[str, str], value: float = 1.0) -> None:
        """Counter with labels, e.g. worker_restarts{name="scheduler"}."""
        key = name + "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"
        self._local[key] = self._local.get(key, 0.0) + value
        self._local[name] = self._local.get(name, 0.0) + value
        if self.enabled:
            counter = self._labeled.get(name)
            if counter is None:
                counter = Counter(
                    f"stalzone_{name}",
                    f"stalzone {name}",
                    labelnames=sorted(labels),
                    registry=self.registry,
                )
                self._labeled[name] = counter
            counter.labels(**labels).inc(value)

    def set_gauge(self, name: str, value: float) -> None:
        self._local[name] = value
        if self.enabled:
            if name not in self._gauges:
                self._gauges[name] = Gauge(
                    f"stalzone_{name}", f"stalzone {name}", registry=self.registry
                )
            self._gauges[name].set(value)

    def get(self, name: str) -> float:
        return self._local.get(name, 0.0)

    def render(self) -> bytes:
        if not self.enabled:
            return b"# metrics disabled\n"
        return generate_latest(self.registry)
