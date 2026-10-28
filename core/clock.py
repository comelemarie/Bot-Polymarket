"""Horloge injectable: temps reel en PAPER/LIVE, temps simule en REPLAY.

Tous les composants qui raisonnent sur le temps (fraicheur, fenetres, cooldowns, budget) recoivent
une horloge au lieu d'appeler time.time(), pour que le replay soit fidele et sans fuite du futur.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone


class Clock:
    def now(self) -> float:
        """Secondes epoch UTC."""
        return time.time()

    def utcnow(self) -> datetime:
        return datetime.fromtimestamp(self.now(), tz=timezone.utc)


class ManualClock(Clock):
    def __init__(self, start: float = 0.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def set(self, t: float) -> None:
        if t < self.t:
            raise ValueError(f"le temps ne recule pas ({t} < {self.t})")
        self.t = t

    def advance(self, dt: float) -> None:
        self.t += dt


def to_epoch(dt: datetime | None) -> float | None:
    return dt.timestamp() if dt is not None else None


def from_epoch(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None else None
