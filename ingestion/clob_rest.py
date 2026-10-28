"""Endpoints publics (sans cle) utilises en PAPER comme en LIVE.

- GET {CLOB}/book?token_id=       carnet complet: verification juste avant chaque ordre
- GET {CLOB}/fee-rate?token_id=   taux de frais taker (champ "base_fee", en points de base, lu ainsi par
                                  py-clob-client). Frais = parts * taux * p * (1 - p).
- GET {GAMMA}/markets?slug=       etat de resolution (closed, outcomePrices)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from core.clock import Clock
from core.config import FeesConfig
from ingestion.market_state import BookState

log = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com/markets"


@dataclass
class RestBook:
    book: BookState
    min_order_size: float | None
    tick_size: float | None


class ClobRest:
    def __init__(self, fees: FeesConfig, clock: Clock, timeout_s: float = 10):
        self.fees = fees
        self.clock = clock
        self.http = httpx.AsyncClient(timeout=timeout_s)
        self._fee_cache: dict[str, tuple[float, float]] = {}

    async def close(self) -> None:
        await self.http.aclose()

    async def fetch_book(self, token_id: str) -> RestBook | None:
        try:
            resp = await self.http.get(f"{CLOB_HOST}/book", params={"token_id": token_id})
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Carnet REST indisponible pour %s: %s", token_id[:12], e)
            return None
        book = BookState(token_id)
        book.apply_snapshot(data.get("bids") or [], data.get("asks") or [], self.clock.now(), str(data.get("hash", "")))
        return RestBook(book, _f(data.get("min_order_size")), _f(data.get("tick_size")))

    async def fee_rate(self, token_id: str) -> tuple[float, str]:
        """(taux, provenance). En cas d'echec: taux de repli prudent de la configuration."""
        cached = self._fee_cache.get(token_id)
        if cached and self.clock.now() - cached[1] < self.fees.cache_ttl_s:
            return cached[0], "api(cache)"
        if not self.fees.fetch_from_api:
            return self.fees.fallback_taker_fee_rate, "config"
        try:
            resp = await self.http.get(f"{CLOB_HOST}/fee-rate", params={"token_id": token_id})
            resp.raise_for_status()
            bps = resp.json().get("base_fee")
            if bps is None:
                raise ValueError("champ base_fee absent")
            rate = float(bps) / 10_000
        except (httpx.HTTPError, ValueError, TypeError) as e:
            log.warning("Taux de frais indisponible pour %s (%s): repli prudent %.3f",
                        token_id[:12], e, self.fees.fallback_taker_fee_rate)
            return self.fees.fallback_taker_fee_rate, "fallback"
        self._fee_cache[token_id] = (rate, self.clock.now())
        return rate, "api"

    async def market_resolution(self, slug: str) -> str | None:
        """'YES' / 'NO' si le marche est resolu de facon non ambigue, sinon None."""
        try:
            resp = await self.http.get(GAMMA_URL, params={"slug": slug})
            resp.raise_for_status()
            rows = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        return resolution_from_gamma(rows[0]) if rows else None


def resolution_from_gamma(m: dict) -> str | None:
    import json

    if not m.get("closed"):
        return None
    try:
        outcomes = [o.lower() for o in (json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m["outcomes"])]
        prices = [float(p) for p in (json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str)
                                     else m["outcomePrices"])]
    except (KeyError, TypeError, ValueError):
        return None
    winners = [o for o, p in zip(outcomes, prices) if p >= 0.999]
    if len(winners) != 1 or winners[0] not in ("yes", "no"):
        return None  # annule, 50/50 ou pas encore finalise
    return winners[0].upper()


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
