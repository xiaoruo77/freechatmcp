"""Readiness checks and restart timing without Windows service dependencies."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


_CONNECTIONS_METRIC = re.compile(
    r"^cloudflared_tunnel_ha_connections(?:\{[^}]*\})?\s+(\S+)(?:\s+\S+)?$"
)


@dataclass(frozen=True)
class TunnelObservation:
    alive: bool
    ready: bool
    reason: str
    active_connections: float | None = None


def observe_metrics(metrics: str) -> TunnelObservation:
    """A responding metrics server is alive; only a connected tunnel is ready."""
    for line in metrics.splitlines():
        match = _CONNECTIONS_METRIC.fullmatch(line.strip())
        if not match:
            continue
        try:
            connections = float(match.group(1))
        except ValueError:
            return TunnelObservation(True, False, "invalid_connections_metric")
        if not math.isfinite(connections) or connections < 0:
            return TunnelObservation(True, False, "invalid_connections_metric")
        return TunnelObservation(
            True,
            connections > 0,
            "connected" if connections > 0 else "no_edge_connections",
            connections,
        )
    return TunnelObservation(True, False, "missing_connections_metric")


@dataclass(frozen=True)
class ReadinessDecision:
    ready: bool
    restart_allowed: bool
    events: tuple[str, ...]


class TunnelReadinessPolicy:
    """Debounce readiness loss and rate-limit health-triggered child restarts."""

    def __init__(self, grace_seconds: float = 180.0, cooldown_seconds: float = 900.0):
        self.grace_seconds = grace_seconds
        self.cooldown_seconds = cooldown_seconds
        self.degraded_since: float | None = None
        self.last_restart_at: float | None = None
        self.previous: TunnelObservation | None = None

    def child_started(self, now: float, *, restarted: bool) -> None:
        if restarted:
            self.last_restart_at = now
        self.degraded_since = None

    def evaluate(self, observation: TunnelObservation, now: float) -> ReadinessDecision:
        events: list[str] = []
        if observation.ready:
            if self.previous is not None and not self.previous.ready:
                events.append("tunnel_recovered alive=true ready=true")
            self.degraded_since = None
        else:
            if self.degraded_since is None:
                self.degraded_since = now
                events.append(self._degraded_event("tunnel_degraded", observation))
            elif self.previous is not None and (
                observation.alive != self.previous.alive
                or observation.reason != self.previous.reason
            ):
                events.append(self._degraded_event("tunnel_degraded_changed", observation))
        restart_allowed = (
            not observation.ready
            and self.degraded_since is not None
            and now - self.degraded_since >= self.grace_seconds
            and (
                self.last_restart_at is None
                or now - self.last_restart_at >= self.cooldown_seconds
            )
        )
        self.previous = observation
        return ReadinessDecision(observation.ready, restart_allowed, tuple(events))

    @staticmethod
    def _degraded_event(event: str, observation: TunnelObservation) -> str:
        return (
            f"{event} alive={str(observation.alive).lower()} ready=false"
            f" reason={observation.reason}"
        )