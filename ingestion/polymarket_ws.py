"""Metadonnees des marches (API Gamma) + carnet d'ordres temps reel (WebSocket CLOB)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Awaitable, Callable

import httpx
import websockets

from core.config import MarketWatch
from core.models import Market, OrderBook

log = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _json_list(value) -> list:
    """Gamma renvoie certains tableaux encodes en chaine JSON."""
    if isinstance(value, str):
        return json.loads(value)
    return list(value or [])


async def load_markets(watchlist: list[MarketWatch]) -> dict[str, Market]:
    """Resout chaque slug de la whitelist via l'API Gamma."""
    markets: dict[str, Market] = {}
    async with httpx.AsyncClient(timeout=20) as http:
        for watch in watchlist:
            resp = await http.get(GAMMA_URL, params={"slug": watch.slug})
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                log.error("Marche introuvable sur Gamma: %s", watch.slug)
                continue
            m = rows[0]
            if m.get("closed") or not m.get("active", True):
                log.warning("Marche ferme/inactif ignore: %s", watch.slug)
                continue
            outcomes = [o.lower() for o in _json_list(m.get("outcomes"))]
            tokens = _json_list(m.get("clobTokenIds"))
            if len(tokens) != 2 or outcomes not in (["yes", "no"], ["no", "yes"]):
                log.error("Marche non binaire YES/NO ignore: %s (%s)", watch.slug, outcomes)
                continue
            yes_idx = outcomes.index("yes")
            markets[watch.slug] = Market(
                slug=watch.slug,
                question=m.get("question", ""),
                description=m.get("description", ""),
                end_date=m.get("endDate", ""),
                condition_id=m.get("conditionId", ""),
                yes_token_id=str(tokens[yes_idx]),
                no_token_id=str(tokens[1 - yes_idx]),
                tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
                neg_risk=bool(m.get("negRisk", False)),
                liquidity_usdc=float(m.get("liquidityNum") or m.get("liquidity") or 0),
                keywords=watch.keywords,
                notes=watch.notes,
            )
            log.info("Marche charge: %s -> %s", watch.slug, markets[watch.slug].question)
    return markets


class PriceHistory:
    """Historique glissant du mid YES pour detecter une variation de prix."""

    def __init__(self, window_s: float):
        self.window_s = window_s
        self.points: deque[tuple[float, float]] = deque()

    def add(self, mid: float, ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        self.points.append((ts, mid))
        while self.points and ts - self.points[0][0] > self.window_s:
            self.points.popleft()

    def move(self) -> float:
        """Amplitude max-min sur la fenetre (en points de probabilite)."""
        if len(self.points) < 2:
            return 0.0
        mids = [p for _, p in self.points]
        return max(mids) - min(mids)


PriceCallback = Callable[[Market], Awaitable[None]]


class PolymarketFeed:
    """Maintient les carnets YES/NO de chaque marche surveille."""

    def __init__(self, markets: dict[str, Market], price_window_s: float):
        self.markets = markets
        self.books: dict[str, OrderBook] = {}
        self.history: dict[str, PriceHistory] = {slug: PriceHistory(price_window_s) for slug in markets}
        self._by_asset: dict[str, Market] = {}
        for m in markets.values():
            for tok in (m.yes_token_id, m.no_token_id):
                self.books[tok] = OrderBook(tok)
                self._by_asset[tok] = m
        self.callbacks: list[PriceCallback] = []

    def on_price(self, cb: PriceCallback) -> None:
        self.callbacks.append(cb)

    def book(self, token_id: str) -> OrderBook:
        return self.books[token_id]

    def yes_mid(self, market: Market) -> float | None:
        return self.books[market.yes_token_id].mid

    def is_fresh(self, market: Market, max_age_s: float = 120) -> bool:
        now = time.time()
        return all(now - self.books[t].updated_at < max_age_s for t in (market.yes_token_id, market.no_token_id))

    async def run(self) -> None:
        """Boucle de connexion avec reconnexion automatique."""
        backoff = 1
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=None, max_size=2**24) as ws:
                    await ws.send(json.dumps({"assets_ids": list(self.books), "type": "market"}))
                    log.info("WebSocket Polymarket connecte (%d tokens)", len(self.books))
                    backoff = 1
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            await self._handle(raw)
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # reseau, deconnexion serveur...
                log.warning("WebSocket Polymarket coupe (%s), reconnexion dans %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")

    async def _handle(self, raw: str | bytes) -> None:
        if raw in ("PONG", b"PONG"):
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        events = data if isinstance(data, list) else [data]
        touched: set[str] = set()
        for ev in events:
            touched |= self._apply(ev)
        for slug in touched:
            market = self.markets[slug]
            mid = self.yes_mid(market)
            if mid is not None:
                self.history[slug].add(mid)
            for cb in self.callbacks:
                await cb(market)

    def _apply(self, ev: dict) -> set[str]:
        """Applique un evenement au carnet; retourne les slugs modifies."""
        etype = ev.get("event_type")
        now = time.time()
        touched: set[str] = set()
        if etype == "book":
            book = self.books.get(ev.get("asset_id", ""))
            if book is None:
                return touched
            bids = ev.get("bids") or ev.get("buys") or []
            asks = ev.get("asks") or ev.get("sells") or []
            book.bids = {float(l["price"]): float(l["size"]) for l in bids if float(l["size"]) > 0}
            book.asks = {float(l["price"]): float(l["size"]) for l in asks if float(l["size"]) > 0}
            book.updated_at = now
            touched.add(self._by_asset[book.asset_id].slug)
        elif etype == "price_change":
            # Format actuel: {"price_changes": [{asset_id, price, size, side}, ...]}
            # Ancien format: {"asset_id": ..., "changes": [{price, size, side}, ...]}
            changes = ev.get("price_changes")
            if changes is None:
                changes = [{**c, "asset_id": ev.get("asset_id")} for c in ev.get("changes", [])]
            for c in changes:
                book = self.books.get(str(c.get("asset_id")))
                if book is None:
                    continue
                side = book.bids if c.get("side", "").upper() == "BUY" else book.asks
                price, size = float(c["price"]), float(c["size"])
                if size <= 0:
                    side.pop(price, None)
                else:
                    side[price] = size
                book.updated_at = now
                touched.add(self._by_asset[book.asset_id].slug)
        return touched
