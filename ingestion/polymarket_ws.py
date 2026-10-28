"""Metadonnees des marches (API Gamma) + flux WebSocket du carnet d'ordres (CLOB, canal "market").

Protocole (documentation Polymarket, format price_change en vigueur depuis le 15/09/2025):
  abonnement  {"assets_ids": [...], "type": "market", "custom_feature_enabled": true}
  evenements  book (instantane), price_change (price_changes[] avec best_bid/best_ask/hash par entree),
              tick_size_change, last_trade_price, et avec custom_feature_enabled: best_bid_ask,
              new_market, market_resolved.
  maintien    envoi de "PING" toutes les 10 s.
Il n'y a pas de numero de sequence: les messages perdus sont detectes par comparaison du carnet
reconstruit avec les meilleurs prix annonces, et corriges par reconnexion (nouvel instantane).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import httpx
import websockets

from core.clock import Clock
from core.config import MarketDataConfig, MarketWatch
from core.models import Market
from ingestion.market_state import MarketState

log = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

Recorder = Callable[[str, object, float], None]
UpdateCallback = Callable[[set[str], float], Awaitable[None]]


def _json_list(value) -> list:
    """Gamma renvoie certains tableaux encodes en chaine JSON."""
    if isinstance(value, str):
        return json.loads(value)
    return list(value or [])


def parse_market(m: dict, keywords: list[str], notes: str = "", event: dict | None = None,
                 tags: list[str] | None = None) -> Market | None:
    """Construit un Market depuis une ligne Gamma, ou None si non tradable (ferme, non binaire...)."""
    slug = m.get("slug", "?")
    if m.get("closed") or not m.get("active", True) or m.get("acceptingOrders") is False:
        return None
    try:
        outcomes = [o.lower() for o in _json_list(m.get("outcomes"))]
        tokens = _json_list(m.get("clobTokenIds"))
    except (ValueError, TypeError):
        return None
    if len(tokens) != 2 or outcomes not in (["yes", "no"], ["no", "yes"]):
        log.debug("Marche non binaire YES/NO ignore: %s (%s)", slug, outcomes)
        return None
    if event is None:
        events = m.get("events") or []
        event = events[0] if events and isinstance(events[0], dict) else {}
    if tags is None:
        tags = sorted({str(t.get("slug", "")).lower() for t in (event.get("tags") or []) if isinstance(t, dict)} - {""})
    yes_idx = outcomes.index("yes")
    return Market(
        slug=slug,
        question=m.get("question", ""),
        description=m.get("description", ""),
        end_date=m.get("endDate", ""),
        condition_id=m.get("conditionId", ""),
        yes_token_id=str(tokens[yes_idx]),
        no_token_id=str(tokens[1 - yes_idx]),
        tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
        neg_risk=bool(m.get("negRisk", False)),
        liquidity_usdc=float(m.get("liquidityNum") or m.get("liquidity") or 0),
        keywords=keywords,
        notes=notes,
        event_id=str(event.get("id", "")),
        event_slug=str(event.get("slug", "")),
        tags=tags,
        min_order_size=float(m.get("orderMinSize") or 5),
        accepting_orders=m.get("acceptingOrders", True) is not False,
    )


async def load_markets(watchlist: list[MarketWatch]) -> dict[str, Market]:
    """Resout chaque slug de la liste manuelle via l'API Gamma."""
    markets: dict[str, Market] = {}
    async with httpx.AsyncClient(timeout=20) as http:
        for watch in watchlist:
            resp = await http.get(GAMMA_URL, params={"slug": watch.slug})
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                log.error("Marche introuvable sur Gamma: %s", watch.slug)
                continue
            market = parse_market(rows[0], watch.keywords, watch.notes)
            if market is None:
                log.warning("Marche ferme, inactif ou non binaire ignore: %s", watch.slug)
                continue
            markets[watch.slug] = market
            log.info("Marche charge: %s -> %s", watch.slug, market.question)
    return markets


def parse_raw(raw: str | bytes) -> list[dict] | None:
    if raw in ("PONG", b"PONG"):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    events = data if isinstance(data, list) else [data]
    return [e for e in events if isinstance(e, dict)]


class PolymarketFeed:
    """Connexions WebSocket (par paquets de tokens) alimentant un MarketState."""

    ASSETS_PER_CONNECTION = 200

    def __init__(self, state: MarketState, cfg: MarketDataConfig, clock: Clock,
                 on_update: UpdateCallback | None = None, recorder: Recorder | None = None):
        self.state = state
        self.cfg = cfg
        self.clock = clock
        self.on_update = on_update
        self.recorder = recorder
        self._resubscribe = asyncio.Event()

    def request_resubscribe(self) -> None:
        self._resubscribe.set()

    async def handle_raw(self, raw: str | bytes, ts: float) -> set[str]:
        """Applique un message brut. Retourne les tokens desynchronises."""
        events = parse_raw(raw)
        if events is None:
            return set()
        if self.recorder:
            self.recorder("ws", raw if isinstance(raw, str) else raw.decode(), ts)
        touched, desynced = set(), set()
        for ev in events:
            t, d = self.state.apply_event(ev, ts)
            touched |= t
            desynced |= d
        if touched and self.on_update:
            await self.on_update(touched, ts)
        return desynced

    async def run(self) -> None:
        while True:
            self._resubscribe.clear()
            assets = list(self.state.books)
            chunks = [assets[i:i + self.ASSETS_PER_CONNECTION]
                      for i in range(0, len(assets), self.ASSETS_PER_CONNECTION)]
            log.info("Abonnement WebSocket: %d marches, %d tokens, %d connexion(s)",
                     len(self.state.markets), len(assets), len(chunks))
            tasks = [asyncio.create_task(self._connection(i, chunk)) for i, chunk in enumerate(chunks)]
            try:
                await self._resubscribe.wait()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _connection(self, idx: int, assets: list[str]) -> None:
        backoff = 1.0
        while True:
            connected_at = None
            reason = ""
            try:
                async with websockets.connect(WS_URL, ping_interval=None, max_size=2**24, open_timeout=20) as ws:
                    connected_at = self.clock.now()
                    self.state.on_connected(assets)
                    if self.recorder:
                        self.recorder("ws_connect", assets, connected_at)
                    await ws.send(json.dumps({"assets_ids": assets, "type": "market", "custom_feature_enabled": True}))
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        reason = await self._read(ws, connected_at)
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                reason = f"erreur: {e}"
            finally:
                now = self.clock.now()
                self.state.on_disconnected(assets)
                if self.recorder:
                    self.recorder("ws_disconnect", assets, now)
            # Une connexion qui tombe vite (ou se desynchronise aussitot) espace les tentatives.
            stable = connected_at is not None and self.clock.now() - connected_at > 60
            backoff = 1.0 if stable else min(backoff * 2, 60)
            log.warning("WebSocket #%d ferme (%s), reconnexion dans %.0fs", idx, reason, backoff)
            await asyncio.sleep(backoff)

    async def _read(self, ws, connected_at: float) -> str:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.cfg.max_silence_s)
            except asyncio.TimeoutError:
                return f"silence > {self.cfg.max_silence_s:.0f}s"
            desynced = await self.handle_raw(raw, self.clock.now())
            if desynced:
                return f"resynchronisation de {len(desynced)} carnet(s)"
            if self.clock.now() - connected_at > self.cfg.resync_interval_s:
                return "resynchronisation periodique"

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")

