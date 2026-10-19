"""Structures de donnees partagees entre les modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NewsItem:
    source: str  # "x:@Reuters", "rss:apnews.com", ...
    text: str
    url: str
    published_at: datetime
    id: str  # identifiant unique pour la deduplication


@dataclass
class Market:
    slug: str
    question: str
    description: str  # regles de resolution
    end_date: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    tick_size: float
    neg_risk: bool
    liquidity_usdc: float
    keywords: list[str]
    notes: str = ""


@dataclass
class OrderBook:
    """Carnet d'ordres d'un token (YES ou NO), maintenu par le WebSocket."""

    asset_id: str
    bids: dict[float, float] = field(default_factory=dict)  # prix -> taille (shares)
    asks: dict[float, float] = field(default_factory=dict)
    updated_at: float = 0.0

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def mid(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    @property
    def spread(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    def ask_depth_usdc(self, max_price: float) -> float:
        """USDC disponibles a l'achat jusqu'a max_price (inclus)."""
        return sum(p * s for p, s in self.asks.items() if p <= max_price + 1e-9)

    def bid_depth_shares(self, min_price: float) -> float:
        return sum(s for p, s in self.bids.items() if p >= min_price - 1e-9)


@dataclass
class Assessment:
    """Sortie validee de Claude."""

    relevant: bool
    probability_yes: float
    confidence: float
    already_priced_in: bool
    reasoning: str
    key_facts: list[str]


@dataclass
class TradeDecision:
    market_slug: str
    token_id: str
    outcome: str  # "YES" ou "NO"
    limit_price: float
    size_usdc: float
    shares: float
    edge: float
    kelly_raw: float
    reason: str


@dataclass
class Fill:
    token_id: str
    side: str  # "BUY" / "SELL"
    price: float
    shares: float
    order_id: str
    simulated: bool
