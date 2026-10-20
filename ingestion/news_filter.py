"""Filtre local: decide quand une actualite merite un appel a Claude.

Un marche est declenche quand, dans la meme fenetre de temps:
  1. une actualite contient un de ses mots-cles (regex, insensible a la casse), ET
  2. son prix YES a bouge d'au moins `price_move_threshold` (si `require_price_move`).
Un cooldown par marche evite de rappeler Claude en boucle sur le meme evenement.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from core.config import TriggerConfig
from core.models import Market, NewsItem

log = logging.getLogger(__name__)

MAX_NEWS_AGE_S = 3600  # une actu publiee il y a plus d'1h n'est plus "chaude"


@dataclass
class Trigger:
    market: Market
    news: list[NewsItem]
    price_move: float


class TriggerEngine:
    def __init__(
        self,
        markets: dict[str, Market],
        cfg: TriggerConfig,
        price_move: Callable[[str], float],
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.price_move = price_move
        self.clock = clock
        self.markets: dict[str, Market] = {}
        self.patterns: dict[str, list[re.Pattern]] = {}
        self.pending: dict[str, list[tuple[float, NewsItem]]] = {}
        self.last_trigger: dict[str, float] = {}
        self.seen_news: dict[str, None] = {}  # ordonne par insertion, purge des plus anciens
        self.set_markets(markets)

    def set_markets(self, markets: dict[str, Market]) -> None:
        """Remplace la liste des marches surveilles (conserve l'etat des marches deja connus)."""
        patterns = {}
        for slug, m in markets.items():
            compiled = []
            for k in m.keywords:
                try:
                    compiled.append(re.compile(k, re.IGNORECASE))
                except re.error as e:
                    log.warning("Mot-cle invalide ignore pour %s: %r (%s)", slug, k, e)
            patterns[slug] = compiled
        self.markets = dict(markets)
        self.patterns = patterns
        self.pending = {slug: self.pending.get(slug, []) for slug in markets}

    def matching_markets(self, news: NewsItem) -> list[str]:
        return [slug for slug, pats in self.patterns.items() if any(p.search(news.text) for p in pats)]

    def on_news(self, news: NewsItem) -> list[Trigger]:
        if news.id in self.seen_news:
            return []
        self.seen_news[news.id] = None
        if len(self.seen_news) > 50_000:
            self.seen_news = dict.fromkeys(list(self.seen_news)[-25_000:])
        age = (datetime.now(timezone.utc) - news.published_at).total_seconds()
        if age > MAX_NEWS_AGE_S:
            return []
        triggers = []
        for slug in self.matching_markets(news):
            log.info("Mot-cle detecte [%s] via %s: %s", slug, news.source, news.text[:120])
            self.pending[slug].append((self.clock(), news))
            trig = self._check(slug)
            if trig:
                triggers.append(trig)
        return triggers

    def on_price(self, slug: str) -> Trigger | None:
        if not self.pending.get(slug):
            return None
        return self._check(slug)

    def _check(self, slug: str) -> Trigger | None:
        if slug not in self.markets:
            return None
        now = self.clock()
        pending = [(ts, n) for ts, n in self.pending[slug] if now - ts <= self.cfg.news_window_s]
        self.pending[slug] = pending
        if not pending:
            return None
        if now - self.last_trigger.get(slug, 0) < self.cfg.market_cooldown_s:
            return None
        move = self.price_move(slug)
        if self.cfg.require_price_move and move < self.cfg.price_move_threshold:
            return None
        self.last_trigger[slug] = now
        self.pending[slug] = []
        log.info("DECLENCHEMENT [%s] variation=%.3f, %d actu(s)", slug, move, len(pending))
        return Trigger(self.markets[slug], [n for _, n in pending], move)
