"""Moteur local de detection (deterministe, aucun appel a un modele).

Trois voies:
  1. news_only       : actualite pertinente sans mouvement prealable -> analyse rapide (si activee).
  2. news_with_move  : actualite + mouvement du marche (ou marche deja sous surveillance) -> analyse,
                       l'avantage residuel sera verifie au prix executable.
  3. anomaly         : mouvement sans actualite identifiee -> evenement journalise, marche mis sous
                       surveillance. JAMAIS de declenchement d'achat.
Filtres avant tout appel couteux: fiabilite de la source, fraicheur, marche negociable et exploitable
(spread, profondeur), cooldown par marche. L'etat (cooldowns, surveillances) est persiste en base.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from core.clock import Clock
from core.config import TriggerConfig
from core.models import Market, NewsItem
from core.store import Store
from ingestion.market_state import MarketState

log = logging.getLogger(__name__)


@dataclass
class Trigger:
    event_id: str
    kind: str  # news_only | news_with_move
    market: Market
    news: list[NewsItem]
    price_move: float
    created_at: float


class TriggerEngine:
    def __init__(self, cfg: TriggerConfig, state: MarketState, store: Store, clock: Clock):
        self.cfg = cfg
        self.state = state
        self.store = store
        self.clock = clock
        self.patterns: dict[str, list[re.Pattern]] = {}
        self.pending: dict[str, list[tuple[float, NewsItem]]] = {}  # voie 2 differee si voie 1 desactivee
        self.last_news_match: dict[str, float] = {}

    def set_markets(self, markets: dict[str, Market]) -> None:
        patterns = {}
        for slug, m in markets.items():
            compiled = []
            for k in m.keywords:
                try:
                    compiled.append(re.compile(k, re.IGNORECASE))
                except re.error as e:
                    log.warning("Mot-cle invalide ignore pour %s: %r (%s)", slug, k, e)
            patterns[slug] = compiled
        self.patterns = patterns
        self.pending = {s: self.pending.get(s, []) for s in markets}

    def matching_markets(self, news: NewsItem) -> list[str]:
        text = news.text
        return [s for s, pats in self.patterns.items() if any(p.search(text) for p in pats)]

    # --- etat persistant ---
    def _cooldown_active(self, slug: str, now: float) -> bool:
        last = self.store.kv_get(f"cooldown:{slug}")
        return last is not None and now - last < self.cfg.market_cooldown_s

    def _watch_active(self, slug: str, now: float) -> bool:
        until = self.store.kv_get(f"watch:{slug}")
        return until is not None and now < until

    def _record_event(self, kind: str, slug: str, news: list[NewsItem], move: float, details: dict) -> str:
        eid = f"ev-{uuid.uuid4().hex[:12]}"
        self.store.insert("events", {
            "id": eid, "kind": kind, "market_slug": slug, "created_at": self.clock.now(),
            "news_ids": [n.id for n in news], "price_move": move, "details": details,
        })
        return eid

    # --- filtres ---
    def analysis_gate(self, market: Market, now: float) -> str | None:
        ok, why = self.state.tradable(market, now)
        if not ok:
            return why
        yes, no = self.state.books[market.yes_token_id], self.state.books[market.no_token_id]
        spreads = [b.spread for b in (yes, no) if b.spread is not None]
        if not spreads or min(spreads) > self.cfg.max_spread_for_analysis:
            return f"spread trop large ({min(spreads) if spreads else 'n/a'})"
        if max(yes.ask_depth_usdc(), no.ask_depth_usdc()) < self.cfg.min_depth_usdc_for_analysis:
            return "profondeur insuffisante"
        return None

    def on_news(self, news: NewsItem) -> list[Trigger]:
        now = self.clock.now()
        age = news.age_s
        if age is not None and age > self.cfg.max_news_age_s:
            return []
        if news.reliability < self.cfg.min_source_reliability:
            return []
        triggers = []
        for slug in self.matching_markets(news):
            market = self.state.markets.get(slug)
            if market is None:
                continue
            self.last_news_match[slug] = now
            gate = self.analysis_gate(market, now)
            if gate:
                self._record_event("filtered", slug, [news], 0.0, {"reason": gate})
                continue
            if self._cooldown_active(slug, now):
                self._record_event("filtered", slug, [news], 0.0, {"reason": "cooldown"})
                continue
            move = self.state.price_move(slug, now)
            if move >= self.cfg.price_move_threshold or self._watch_active(slug, now):
                triggers.append(self._fire("news_with_move", market, [news], move, now))
            elif self.cfg.news_only_enabled:
                triggers.append(self._fire("news_only", market, [news], move, now))
            else:
                self.pending[slug].append((now, news))
        return triggers

    def on_price(self, slugs: set[str]) -> list[Trigger]:
        now = self.clock.now()
        out = []
        for slug in slugs:
            market = self.state.markets.get(slug)
            if market is None:
                continue
            move = self.state.price_move(slug, now)
            pending = [(t, n) for t, n in self.pending.get(slug, []) if now - t <= self.cfg.news_window_s]
            self.pending[slug] = pending
            if pending and move >= self.cfg.price_move_threshold:
                if not self._cooldown_active(slug, now) and self.analysis_gate(market, now) is None:
                    self.pending[slug] = []
                    out.append(self._fire("news_with_move", market, [n for _, n in pending], move, now))
                continue
            if move >= self.cfg.anomaly_move_threshold:
                recent_news = now - self.last_news_match.get(slug, -1e18) <= self.cfg.news_window_s
                last_anomaly = self.store.kv_get(f"anomaly:{slug}")
                if not recent_news and (last_anomaly is None or now - last_anomaly >= self.cfg.market_cooldown_s):
                    h = self.state.history[slug]
                    self._record_event("anomaly", slug, [], move, {"signed_move": h.signed_move(),
                                                                    "first_ask": h.first_ask()})
                    self.store.kv_set(f"anomaly:{slug}", now, now)
                    self.store.kv_set(f"watch:{slug}", now + self.cfg.anomaly_watch_s, now)
                    log.info("ANOMALIE [%s] mouvement %.3f sans actualite: surveillance %.0fs (aucun ordre)",
                             slug, move, self.cfg.anomaly_watch_s)
        return out

    def _fire(self, kind: str, market: Market, news: list[NewsItem], move: float, now: float) -> Trigger:
        details = {"watch": self._watch_active(market.slug, now),
                   "news_ages_s": [n.age_s for n in news], "sources": [n.source for n in news]}
        eid = self._record_event(kind, market.slug, news, move, details)
        self.store.kv_set(f"cooldown:{market.slug}", now, now)
        log.info("DECLENCHEMENT %s [%s] mouvement=%.3f via %s", kind, market.slug, move, news[0].source)
        return Trigger(eid, kind, market, news, move, now)
