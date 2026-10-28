"""Calcul deterministe de l'avantage net (aucune IA ici).

Modele: contrat binaire qui paie 1 USDC par part gagnante, 0 sinon.
- Achat de `s` parts sur les niveaux d'ask q_i (VWAP reel, pas le mid).
- Frais taker Polymarket: fee_usdc = s_i * r * q_i * (1 - q_i), preleves EN PARTS a l'achat
  (on recoit s_i * (1 - r * (1 - q_i)) parts) et en USDC a la vente.
- Tampon de slippage: cout supplementaire par part payee (latence, carnet qui bouge entre decision et execution).
EV nette d'un achat = p * parts_recues - cout_USDC - slippage * parts_payees.
Pour une part au prix q: avantage net marginal = p * (1 - r * (1 - q)) - q - slippage.
Frais et slippage ne sont comptes qu'une fois chacun.

YES et NO sont evalues sur LEUR propre carnet d'ask: on ne suppose jamais prix_NO = 1 - prix_YES.
La probabilite utilisee est la borne prudente de l'intervalle (basse pour YES, 1 - haute pour NO).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from core.config import EdgeConfig
from core.models import Assessment, Outcome

EPS = 1e-9


def taker_fee_usdc(shares: float, price: float, rate: float) -> float:
    return shares * rate * price * (1 - price)


def marginal_buy_edge(p: float, price: float, fee_rate: float, slippage: float) -> float:
    return p * (1 - fee_rate * (1 - price)) - price - slippage


def kelly_fraction(p: float, cost_per_share: float) -> float:
    """Kelly binaire f = (p - c) / (1 - c) pour un cout tout compris c par part recue. 0 si EV <= 0."""
    if not 0 < cost_per_share < 1:
        return 0.0
    return max(0.0, (p - cost_per_share) / (1 - cost_per_share))


def conservative_interval(a: Assessment, cfg: EdgeConfig) -> tuple[float, float]:
    """Intervalle [bas, haut] elargi au minimum impose par l'incertitude declaree.

    Le modele peut fournir un intervalle trop etroit: on ne retient jamais moins que la demi-largeur
    minimale configuree pour sa categorie d'incertitude.
    """
    hw = cfg.min_half_width[a.uncertainty]
    lo = min(a.p_low, a.p_yes - hw)
    hi = max(a.p_high, a.p_yes + hw)
    return max(0.0, lo), min(1.0, hi)


@dataclass
class BuyQuote:
    outcome: Outcome
    token_id: str
    p_used: float
    shares: float  # parts payees
    cost_usdc: float
    vwap: float
    worst_price: float
    fee_usdc: float
    received_shares: float
    slippage_usdc: float

    @property
    def ev_gross_usdc(self) -> float:
        return (self.p_used - self.vwap) * self.shares

    @property
    def ev_net_usdc(self) -> float:
        return self.p_used * self.received_shares - self.cost_usdc - self.slippage_usdc

    @property
    def net_edge_per_share(self) -> float:
        return self.ev_net_usdc / self.shares if self.shares else 0.0

    @property
    def effective_cost_per_received_share(self) -> float:
        return (self.cost_usdc + self.slippage_usdc) / self.received_shares if self.received_shares else 1.0


def quote_buy(levels: list[tuple[float, float]], shares: float, p: float, fee_rate: float, slippage: float,
              outcome: Outcome = "YES", token_id: str = "") -> BuyQuote | None:
    """Execution simulee de `shares` contre les asks tries par prix croissant. None si profondeur insuffisante."""
    remaining, cost, fee, received, worst = shares, 0.0, 0.0, 0.0, 0.0
    for price, size in levels:
        if remaining <= EPS:
            break
        take = min(remaining, size)
        cost += take * price
        f = taker_fee_usdc(take, price, fee_rate)
        fee += f
        received += take - f / price
        worst = price
        remaining -= take
    if remaining > EPS or shares <= 0:
        return None
    return BuyQuote(outcome, token_id, p, shares, cost, cost / shares, worst, fee, received, slippage * shares)


def max_shares_with_edge(levels: list[tuple[float, float]], p: float, fee_rate: float, slippage: float,
                         min_edge: float) -> tuple[float, float]:
    """Plus grand nombre de parts dont CHAQUE part a un avantage net marginal >= min_edge.

    L'avantage marginal decroit avec le prix: on prend les niveaux tant qu'ils respectent le seuil.
    Retourne (parts, prix du dernier niveau inclus).
    """
    shares, worst = 0.0, 0.0
    for price, size in levels:
        if marginal_buy_edge(p, price, fee_rate, slippage) < min_edge:
            break
        shares += size
        worst = price
    return shares, worst


def shares_for_budget(levels: list[tuple[float, float]], budget_usdc: float, max_shares: float) -> float:
    """Parts achetables avec `budget_usdc` (cout hors frais, ceux-ci etant preleves en parts), sans depasser max_shares."""
    shares, spent = 0.0, 0.0
    for price, size in levels:
        if shares >= max_shares - EPS:
            break
        take = min(size, max_shares - shares)
        if spent + take * price > budget_usdc:
            take = max(0.0, (budget_usdc - spent) / price)
            shares += take
            break
        shares += take
        spent += take * price
    return math.floor(shares * 100 + EPS) / 100  # precision de taille du CLOB: 2 decimales


@dataclass
class SideCandidate:
    outcome: Outcome
    token_id: str
    p_conservative: float
    fee_rate: float
    levels: list[tuple[float, float]]
    max_shares: float
    worst_price: float
    best_marginal_edge: float


def side_candidates(a: Assessment, yes_asks: list[tuple[float, float]], no_asks: list[tuple[float, float]],
                    yes_token: str, no_token: str, fee_rates: dict[str, float], cfg: EdgeConfig) -> list[SideCandidate]:
    lo, hi = conservative_interval(a, cfg)
    out = []
    for outcome, token, p_cons, levels in (("YES", yes_token, lo, yes_asks), ("NO", no_token, 1 - hi, no_asks)):
        if not levels:
            continue
        rate = fee_rates[token]
        best = marginal_buy_edge(p_cons, levels[0][0], rate, cfg.slippage_buffer)
        max_s, worst = max_shares_with_edge(levels, p_cons, rate, cfg.slippage_buffer, cfg.min_net_edge)
        out.append(SideCandidate(outcome, token, p_cons, rate, levels, max_s, worst, best))
    return sorted(out, key=lambda c: c.best_marginal_edge, reverse=True)


@dataclass
class SellQuote:
    shares: float
    proceeds_usdc: float  # net de frais
    fee_usdc: float
    vwap: float
    worst_price: float


def quote_sell(bid_levels: list[tuple[float, float]], shares: float, fee_rate: float) -> SellQuote | None:
    """Vente de `shares` contre les bids tries par prix decroissant. None si profondeur insuffisante."""
    remaining, gross, fee, worst = shares, 0.0, 0.0, 0.0
    for price, size in bid_levels:
        if remaining <= EPS:
            break
        take = min(remaining, size)
        gross += take * price
        fee += taker_fee_usdc(take, price, fee_rate)
        worst = price
        remaining -= take
    if remaining > EPS or shares <= 0:
        return None
    return SellQuote(shares, gross - fee, fee, gross / shares, worst)
