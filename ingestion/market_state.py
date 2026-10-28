"""Etat coherent et partage des marches: carnets, synchronisation, prix et historique.

Un carnet est SYNCHRONISE si:
  - un instantane complet ("book") a ete recu depuis la derniere (re)connexion,
  - chaque mise a jour appliquee depuis est coherente avec les meilleurs prix annonces par le serveur
    (champs best_bid/best_ask des entrees price_change),
  - il n'est ni croise (bid >= ask) ni hors bornes (prix hors ]0,1[).
Un marche calme reste synchronise: l'absence de mise a jour n'est pas une perte de coherence.
Toute incoherence desynchronise le carnet: les decisions d'execution sont suspendues jusqu'au
prochain instantane (resynchronisation demandee a la couche WebSocket).
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from core.config import MarketDataConfig
from core.models import Market

log = logging.getLogger(__name__)

EPS = 1e-9


@dataclass
class BookState:
    asset_id: str
    bids: dict[float, float] = field(default_factory=dict)  # prix -> taille (parts)
    asks: dict[float, float] = field(default_factory=dict)
    synced: bool = False
    desync_reason: str = "en attente d'instantane"
    last_snapshot_at: float | None = None
    last_update_at: float | None = None
    last_hash: str = ""

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def mid(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        return (b + a) / 2 if b is not None and a is not None else None

    @property
    def spread(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        return a - b if b is not None and a is not None else None

    def asks_up_to(self, max_price: float) -> list[tuple[float, float]]:
        return sorted((p, s) for p, s in self.asks.items() if p <= max_price + EPS)

    def bids_down_to(self, min_price: float) -> list[tuple[float, float]]:
        return sorted(((p, s) for p, s in self.bids.items() if p >= min_price - EPS), reverse=True)

    def ask_depth_usdc(self, max_price: float = 1.0) -> float:
        return sum(p * s for p, s in self.asks_up_to(max_price))

    def bid_depth_usdc(self, min_price: float = 0.0) -> float:
        return sum(p * s for p, s in self.bids_down_to(min_price))

    def levels(self, n: int = 10) -> dict:
        return {"bids": sorted(self.bids.items(), reverse=True)[:n], "asks": sorted(self.asks.items())[:n]}

    def invalidate(self, reason: str) -> None:
        if self.synced:
            log.warning("Carnet %s desynchronise: %s", self.asset_id[:12], reason)
        self.synced = False
        self.desync_reason = reason

    def integrity_error(self) -> str | None:
        for p, s in list(self.bids.items()) + list(self.asks.items()):
            if not (0 < p < 1):
                return f"prix hors bornes {p}"
            if s < 0:
                return f"taille negative {s}"
        b, a = self.best_bid, self.best_ask
        if b is not None and a is not None and b >= a - EPS:
            return f"carnet croise (bid {b} >= ask {a})"
        return None

    def apply_snapshot(self, bids: list[dict], asks: list[dict], ts: float, book_hash: str = "") -> None:
        self.bids = {float(l["price"]): float(l["size"]) for l in bids if float(l["size"]) > 0}
        self.asks = {float(l["price"]): float(l["size"]) for l in asks if float(l["size"]) > 0}
        self.last_snapshot_at = self.last_update_at = ts
        self.last_hash = book_hash
        err = self.integrity_error()
        if err:
            self.invalidate(f"instantane invalide: {err}")
        else:
            self.synced, self.desync_reason = True, ""

    def apply_change(self, price: float, size: float, side: str, ts: float,
                     server_best_bid: float | None = None, server_best_ask: float | None = None,
                     validate: bool = True) -> bool:
        """Applique une mise a jour de niveau (taille absolue). Retourne False si le carnet devient incoherent."""
        if self.last_snapshot_at is None:
            return False  # mise a jour avant tout instantane: ignoree, le carnet reste non synchronise
        book = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.last_update_at = ts
        err = self.integrity_error()
        if err is None and validate:
            err = _compare(self.best_bid, server_best_bid, "bid") or _compare(self.best_ask, server_best_ask, "ask")
        if err:
            self.invalidate(err)
            return False
        return True


def _compare(local: float | None, server: float | None, name: str) -> str | None:
    if server is None:
        return None
    if local is None:
        # Cote vide: le serveur peut annoncer 0 (bid) ou 1 (ask), ou rien.
        return None if server in (0.0, 1.0) else f"best_{name} serveur {server} mais cote local vide"
    if abs(local - server) > 1e-6:
        return f"best_{name} local {local} != serveur {server} (message perdu ?)"
    return None


class PriceHistory:
    """Mid YES sur une fenetre glissante, n'enregistre que des points ou le carnet est sync et le spread etroit.

    Un mid issu d'un carnet a spread large n'est pas un prix negociable: l'elargissement du spread ne doit
    pas etre confondu avec un mouvement de marche.
    """

    def __init__(self, window_s: float):
        self.window_s = window_s
        self.points: deque[tuple[float, float, float | None]] = deque()  # (ts, mid, best_ask)

    def add(self, ts: float, mid: float, best_ask: float | None) -> None:
        self.points.append((ts, mid, best_ask))
        self._trim(ts)

    def _trim(self, now: float) -> None:
        while self.points and now - self.points[0][0] > self.window_s:
            self.points.popleft()

    def move(self, now: float | None = None) -> float:
        if now is not None:
            self._trim(now)
        if len(self.points) < 2:
            return 0.0
        mids = [p[1] for p in self.points]
        return max(mids) - min(mids)

    def signed_move(self) -> float:
        return self.points[-1][1] - self.points[0][1] if len(self.points) >= 2 else 0.0

    def first_ask(self) -> float | None:
        return self.points[0][2] if self.points else None


class MarketState:
    def __init__(self, cfg: MarketDataConfig, price_window_s: float, max_spread_for_move: float):
        self.cfg = cfg
        self.price_window_s = price_window_s
        self.max_spread_for_move = max_spread_for_move
        self.markets: dict[str, Market] = {}
        self.books: dict[str, BookState] = {}
        self.history: dict[str, PriceHistory] = {}
        self._by_asset: dict[str, Market] = {}
        self.connected: set[str] = set()  # tokens dont la connexion est active

    def set_markets(self, markets: dict[str, Market]) -> tuple[set[str], set[str]]:
        old = set(self.books)
        self.markets = dict(markets)
        self._by_asset, books = {}, {}
        for m in self.markets.values():
            for tok in (m.yes_token_id, m.no_token_id):
                books[tok] = self.books.get(tok) or BookState(tok)
                self._by_asset[tok] = m
        self.books = books
        self.history = {s: self.history.get(s) or PriceHistory(self.price_window_s) for s in self.markets}
        new = set(self.books)
        return new - old, old - new

    def market_of(self, token_id: str) -> Market | None:
        return self._by_asset.get(token_id)

    # --- cycle de vie des connexions ---
    def on_connected(self, assets: list[str]) -> None:
        for a in assets:
            self.connected.add(a)
            if a in self.books:
                self.books[a].invalidate("reconnexion: en attente d'instantane")
                self.books[a].last_snapshot_at = None

    def on_disconnected(self, assets: list[str]) -> None:
        for a in assets:
            self.connected.discard(a)
            if a in self.books:
                self.books[a].invalidate("connexion perdue")

    # --- application des messages ---
    def apply_event(self, ev: dict, ts: float) -> tuple[set[str], set[str]]:
        """Retourne (slugs dont le carnet a change, tokens desynchronises par ce message)."""
        etype = ev.get("event_type")
        touched: set[str] = set()
        desynced: set[str] = set()
        if etype == "book":
            book = self.books.get(str(ev.get("asset_id", "")))
            if book is not None:
                book.apply_snapshot(ev.get("bids") or ev.get("buys") or [], ev.get("asks") or ev.get("sells") or [],
                                    ts, str(ev.get("hash", "")))
                if not book.synced:
                    desynced.add(book.asset_id)
                touched.add(self._by_asset[book.asset_id].slug)
        elif etype == "price_change":
            changes = ev.get("price_changes")
            legacy = changes is None
            if legacy:  # ancien format (avant sept. 2025), sans best_bid/best_ask par entree
                changes = [{**c, "asset_id": ev.get("asset_id")} for c in ev.get("changes", [])]
            for c in changes:
                book = self.books.get(str(c.get("asset_id")))
                if book is None:
                    continue
                try:
                    price, size = float(c["price"]), float(c["size"])
                except (KeyError, TypeError, ValueError):
                    book.invalidate("message price_change malforme")
                    desynced.add(book.asset_id)
                    continue
                ok = book.apply_change(
                    price, size, str(c.get("side", "")), ts,
                    _opt_float(c.get("best_bid")), _opt_float(c.get("best_ask")),
                    validate=self.cfg.validate_ws_best_prices and not legacy,
                )
                if not ok:
                    desynced.add(book.asset_id)
                touched.add(self._by_asset[book.asset_id].slug)
        elif etype == "tick_size_change":
            m = self._by_asset.get(str(ev.get("asset_id", "")))
            new_tick = _opt_float(ev.get("new_tick_size"))
            if m and new_tick:
                log.info("Tick de %s: %s -> %s", m.slug, m.tick_size, new_tick)
                m.tick_size = new_tick
        elif etype == "market_resolved":
            for tok in [ev.get("asset_id"), *(ev.get("assets_ids") or [])]:
                m = self._by_asset.get(str(tok))
                if m:
                    m.accepting_orders = False
                    log.warning("Marche resolu, plus negociable: %s", m.slug)
        for slug in touched:
            self._record_price(slug, ts)
        return touched, desynced

    def _record_price(self, slug: str, ts: float) -> None:
        m = self.markets.get(slug)
        if m is None:
            return
        yes = self.books[m.yes_token_id]
        if yes.synced and yes.mid is not None and yes.spread is not None and yes.spread <= self.max_spread_for_move:
            self.history[slug].add(ts, yes.mid, yes.best_ask)

    # --- requetes ---
    def price_move(self, slug: str, now: float | None = None) -> float:
        h = self.history.get(slug)
        return h.move(now) if h else 0.0

    def tradable(self, market: Market, now: float) -> tuple[bool, str]:
        if not market.accepting_orders:
            return False, "marche ferme ou n'acceptant plus d'ordres"
        for tok in (market.yes_token_id, market.no_token_id):
            b = self.books.get(tok)
            if b is None:
                return False, "carnet inconnu"
            if tok not in self.connected:
                return False, "connexion WebSocket inactive"
            if not b.synced:
                return False, f"carnet non synchronise ({b.desync_reason})"
            if b.last_snapshot_at is None or now - b.last_snapshot_at > 2 * self.cfg.resync_interval_s:
                return False, "instantane trop ancien (resynchronisation en retard)"
            err = b.integrity_error()
            if err:
                return False, err
        return True, ""


def _opt_float(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
