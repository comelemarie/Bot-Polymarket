"""Structures de donnees partagees entre les modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Outcome = Literal["YES", "NO"]
Side = Literal["BUY", "SELL"]


@dataclass
class NewsItem:
    id: str  # identifiant stable de la source (ex: "x:123", "rss:<guid>")
    source: str  # "x:@Reuters", "rss:feeds.bbci.co.uk", ...
    title: str
    content: str
    url: str
    published_at: datetime | None  # None si la source ne fournit pas de date: jamais inventee
    received_at: datetime  # horodatage local UTC de reception
    reliability: float = 0.5  # fiabilite a priori de la source (configuration), 0..1
    fingerprint: str = ""  # empreinte du titre normalise (dedoublonnage multi-sources)
    corroborations: int = 0  # nombre de reprises par d'autres sources deja vues

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.content}" if self.content and self.content != self.title else self.title

    @property
    def age_s(self) -> float | None:
        """Delai publication -> reception (fraicheur a l'arrivee)."""
        if self.published_at is None:
            return None
        return (self.received_at - self.published_at).total_seconds()


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
    event_id: str = ""
    event_slug: str = ""
    tags: list[str] = field(default_factory=list)
    min_order_size: float = 5.0
    accepting_orders: bool = True

    def token(self, outcome: Outcome) -> str:
        return self.yes_token_id if outcome == "YES" else self.no_token_id

    def outcome_of(self, token_id: str) -> Outcome:
        return "YES" if token_id == self.yes_token_id else "NO"


@dataclass
class Assessment:
    """Analyse validee de Claude. Aucune donnee de ce type ne peut declencher un ordre a elle seule."""

    market_id: str
    event_id: str
    p_yes: float
    p_low: float
    p_high: float
    uncertainty: Literal["low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    key_facts: list[str]
    supporting_evidence: list[str]
    contradicting_evidence: list[str]
    source_urls: list[str]
    published_at: list[str]
    market_resolution_understanding: str
    already_priced_in: Literal["no", "partially", "fully", "unknown"]
    reasoning_summary: str
    abstain: bool
    abstain_reason: str
    analysis_id: str = ""
    latency_s: float = 0.0
    cost_usd: float = 0.0
    cached: bool = False

    @classmethod
    def abstention(cls, market_id: str, event_id: str, reason: str, analysis_id: str = "") -> "Assessment":
        return cls(
            market_id=market_id, event_id=event_id, p_yes=0.5, p_low=0.0, p_high=1.0,
            uncertainty="high", confidence="low", key_facts=[], supporting_evidence=[],
            contradicting_evidence=[], source_urls=[], published_at=[], market_resolution_understanding="",
            already_priced_in="unknown", reasoning_summary="", abstain=True, abstain_reason=reason,
            analysis_id=analysis_id,
        )


@dataclass
class TradeDecision:
    decision_id: str
    event_id: str
    market_slug: str
    token_id: str
    outcome: Outcome
    limit_price: float  # prix maximal accepte (dernier niveau de carnet inclus)
    shares: float
    expected_cost_usdc: float
    expected_fee_usdc: float
    net_edge_per_share: float
    p_conservative: float
    kelly_raw: float
    binding_constraint: str
    max_loss_usdc: float
    reason: str


@dataclass
class OrderRequest:
    client_order_id: str
    token_id: str
    side: Side
    limit_price: float
    shares: float


@dataclass
class SubmitResult:
    status: Literal["filled", "rejected", "unknown"]
    filled_shares: float = 0.0
    avg_price: float = 0.0
    fee_usdc: float = 0.0
    exchange_order_id: str = ""
    error: str = ""
    raw: dict = field(default_factory=dict)
