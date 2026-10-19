"""Moteur de risque deterministe (aucune IA ici).

Regle d'or: Claude fournit une probabilite, ce module decide seul du sens, de la taille et du prix.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from core.config import RiskConfig
from core.models import Assessment, Market, OrderBook, TradeDecision
from core.state import DailyStats, Position

log = logging.getLogger(__name__)

MIN_SHARES = 5.0  # taille minimale d'ordre usuelle sur le CLOB Polymarket


def kelly_fraction(p_true: float, price: float) -> float:
    """Kelly pour un contrat binaire achete a `price` qui paie 1 si gagnant.

    f* = (p - price) / (1 - price). Retourne 0 si l'esperance est negative.
    """
    if not 0 < price < 1:
        return 0.0
    return max(0.0, (p_true - price) / (1 - price))


def floor_to_tick(price: float, tick: float) -> float:
    return round(math.floor(price / tick + 1e-9) * tick, 6)


def shrink_to_market(p_model: float, p_market: float, confidence: float) -> float:
    """Ramene l'estimation de Claude vers le prix du marche selon sa confiance.

    confidence=1 -> on croit Claude; confidence=0 -> on garde le prix du marche.
    """
    return p_market + confidence * (p_model - p_market)


@dataclass
class Verdict:
    decision: TradeDecision | None
    reason: str


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def check_daily_halt(self, daily: DailyStats, equity: float) -> bool:
        """Coupe-circuit: plus de nouvelles positions si la perte du jour depasse le seuil."""
        if not daily.halted and equity < daily.start_equity * (1 - self.cfg.max_daily_loss_pct):
            daily.halted = True
            log.error(
                "COUPE-CIRCUIT: equity %.2f < %.2f (perte max journaliere). Entrees bloquees jusqu'a demain.",
                equity, daily.start_equity * (1 - self.cfg.max_daily_loss_pct),
            )
        return daily.halted

    def evaluate(
        self,
        market: Market,
        assessment: Assessment,
        yes_book: OrderBook,
        no_book: OrderBook,
        usdc_balance: float,
        equity: float,
        market_exposure: float,
        total_exposure: float,
        daily: DailyStats,
    ) -> Verdict:
        c = self.cfg
        if self.check_daily_halt(daily, equity):
            return Verdict(None, "coupe-circuit journalier actif")
        if not assessment.relevant:
            return Verdict(None, "actualite jugee non pertinente")
        if assessment.already_priced_in:
            return Verdict(None, "information deja dans le prix")
        if assessment.confidence < c.min_confidence:
            return Verdict(None, f"confiance {assessment.confidence:.2f} < {c.min_confidence}")

        yes_mid = yes_book.mid
        if yes_mid is None:
            return Verdict(None, "carnet YES vide")
        p_yes = shrink_to_market(assessment.probability_yes, yes_mid, assessment.confidence)

        # Choix du cote avec le meilleur edge apres prise en compte du prix d'achat reel (ask).
        best: tuple[float, str, OrderBook, float, float] | None = None
        for outcome, book, p_true in (("YES", yes_book, p_yes), ("NO", no_book, 1 - p_yes)):
            ask = book.best_ask
            if ask is None or book.spread is None or book.spread > c.max_spread:
                continue
            edge = p_true - ask
            if best is None or edge > best[0]:
                best = (edge, outcome, book, p_true, ask)
        if best is None:
            return Verdict(None, "aucun carnet exploitable (vide ou spread trop large)")
        edge, outcome, book, p_true, ask = best
        if edge < c.min_edge:
            return Verdict(None, f"edge {edge:.3f} < {c.min_edge} ({outcome} ask {ask:.3f}, p {p_true:.3f})")

        k_raw = kelly_fraction(p_true, ask)
        kelly_usdc = c.kelly_fraction * k_raw * equity

        # Prix limite: on accepte un peu de slippage, sans jamais manger tout l'edge minimum.
        limit = floor_to_tick(min(ask + c.max_slippage, p_true - c.min_edge, 0.99), market.tick_size)
        if limit < ask - 1e-9:
            return Verdict(None, "prix limite sous le meilleur ask")

        caps = {
            "kelly": kelly_usdc,
            "max_par_marche": c.max_exposure_per_market_pct * equity - market_exposure,
            "max_total": c.max_total_exposure_pct * equity - total_exposure,
            "cash_hors_reserve": usdc_balance - c.min_usdc_reserve,
            "profondeur_carnet": book.ask_depth_usdc(limit) * c.max_book_take_pct,
        }
        binding = min(caps, key=caps.get)
        size_usdc = caps[binding]
        if size_usdc < c.min_order_usdc:
            return Verdict(None, f"taille {size_usdc:.2f} USDC < minimum (limitee par {binding})")
        shares = math.floor(size_usdc / limit * 100) / 100
        if shares < MIN_SHARES:
            return Verdict(None, f"{shares} shares < minimum {MIN_SHARES}")

        token_id = market.yes_token_id if outcome == "YES" else market.no_token_id
        return Verdict(
            TradeDecision(
                market_slug=market.slug,
                token_id=token_id,
                outcome=outcome,
                limit_price=limit,
                size_usdc=round(shares * limit, 2),
                shares=shares,
                edge=edge,
                kelly_raw=k_raw,
                reason=f"edge {edge:.3f}, kelly brut {k_raw:.3f}, contrainte active: {binding}",
            ),
            "ok",
        )

    def exit_signal(self, position: Position, best_bid: float | None) -> str | None:
        """Stop-loss / take-profit sur le prix de sortie reel (meilleur bid)."""
        if best_bid is None:
            return None
        if best_bid <= position.avg_price * (1 - self.cfg.stop_loss_pct):
            return f"stop-loss (bid {best_bid:.3f} vs entree {position.avg_price:.3f})"
        if best_bid >= self.cfg.take_profit_price:
            return f"take-profit (bid {best_bid:.3f})"
        return None
