"""Moteur de risque deterministe: autorite finale sur l'entree et la taille des positions.

Aucune sortie du modele ne peut modifier ces regles: les limites viennent exclusivement de la configuration
validee au demarrage. Toute information manquante (solde, exposition, carnet) entraine un refus.
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field

from core.config import EdgeConfig, ExitConfig, RiskConfig
from core.models import Assessment, Market, TradeDecision
from core.state import DailyStats, Portfolio, Position
from execution.edge_calculator import (
    conservative_interval,
    kelly_fraction,
    quote_buy,
    shares_for_budget,
    side_candidates,
)
from ingestion.market_state import BookState

log = logging.getLogger(__name__)

_CONF_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class RiskContext:
    market: Market
    assessment: Assessment
    event_id: str
    yes_book: BookState
    no_book: BookState
    fee_rates: dict[str, float]
    tradable: tuple[bool, str]
    usdc_balance: float | None
    equity: float | None
    daily: DailyStats | None
    peak_equity: float | None
    halts: dict[str, str]
    pending_orders_on_market: bool
    now: float


@dataclass
class Verdict:
    decision: TradeDecision | None
    reason: str
    details: dict = field(default_factory=dict)


class RiskManager:
    def __init__(self, risk: RiskConfig, edge: EdgeConfig, exits: ExitConfig, portfolio: Portfolio):
        self.cfg = risk
        self.edge = edge
        self.exits = exits
        self.portfolio = portfolio

    def account_halts(self, equity: float, daily: DailyStats, peak: float, now: float) -> list[str]:
        """Coupe-circuits de compte. Les declenche (persistants) si necessaire et les retourne."""
        reasons = []
        if equity < daily.start_equity * (1 - self.cfg.max_daily_loss_pct):
            if not daily.halted:
                self.portfolio.set_daily_halted(now)
                log.error("COUPE-CIRCUIT journalier: equity %.2f < %.2f", equity,
                          daily.start_equity * (1 - self.cfg.max_daily_loss_pct))
            reasons.append("perte journaliere maximale atteinte")
        elif daily.halted:
            reasons.append("perte journaliere maximale atteinte plus tot dans la journee")
        if peak > 0 and equity < peak * (1 - self.cfg.max_drawdown_pct):
            self.portfolio.halt("drawdown", f"equity {equity:.2f} < pic {peak:.2f} - {self.cfg.max_drawdown_pct:.0%}", now)
            reasons.append("drawdown maximal atteint")
        return reasons

    def evaluate(self, ctx: RiskContext) -> Verdict:
        c, a, m = self.cfg, ctx.assessment, ctx.market
        d: dict = {"assessment_id": a.analysis_id}

        def reject(reason: str) -> Verdict:
            return Verdict(None, reason, d)

        # 1. Blocages inconditionnels
        if ctx.halts:
            return reject("coupe-circuit actif: " + "; ".join(f"{k} ({v})" for k, v in ctx.halts.items()))
        ok, why = ctx.tradable
        if not ok:
            return reject(f"marche non negociable: {why}")
        if ctx.pending_orders_on_market:
            return reject("ordre en etat incertain sur ce marche: reconciliation requise")
        if ctx.usdc_balance is None or ctx.equity is None or ctx.daily is None or ctx.peak_equity is None:
            return reject("solde ou equity inconnus")
        halts = self.account_halts(ctx.equity, ctx.daily, ctx.peak_equity, ctx.now)
        if halts:
            return reject("; ".join(halts))

        # 2. Qualite de l'analyse
        if a.abstain:
            return reject(f"abstention de l'analyse: {a.abstain_reason}")
        if _CONF_ORDER[a.confidence] < _CONF_ORDER[c.min_confidence]:
            return reject(f"qualite des elements '{a.confidence}' < minimum '{c.min_confidence}'")
        if a.already_priced_in == "fully":
            return reject("information jugee deja integree au prix")
        size_mult = c.uncertainty_size_multiplier[a.uncertainty]
        if size_mult <= 0:
            return reject(f"incertitude '{a.uncertainty}': mise nulle par configuration")

        # 3. Avantage net sur prix executables (carnet de chaque cote, frais, slippage, borne prudente)
        lo, hi = conservative_interval(a, self.edge)
        d["interval"] = [lo, hi]
        cands = side_candidates(a, ctx.yes_book.asks_up_to(1.0), ctx.no_book.asks_up_to(1.0),
                                m.yes_token_id, m.no_token_id, ctx.fee_rates, self.edge)
        d["candidates"] = [{"outcome": s.outcome, "p_cons": s.p_conservative, "best_marginal_edge": s.best_marginal_edge,
                            "max_shares": s.max_shares, "fee_rate": s.fee_rate} for s in cands]
        cand = next((s for s in cands if s.max_shares > 0), None)
        if cand is None:
            best = cands[0].best_marginal_edge if cands else None
            return reject(f"avantage net insuffisant (meilleur marginal {best if best is None else round(best, 4)} "
                          f"< {self.edge.min_net_edge})")
        levels = [(p, s) for p, s in cand.levels if p <= cand.worst_price + 1e-9]

        # 4. Taille: Kelly fractionne puis plafonds
        full = quote_buy(levels, cand.max_shares, cand.p_conservative, cand.fee_rate, self.edge.slippage_buffer,
                         cand.outcome, cand.token_id)
        k_raw = kelly_fraction(cand.p_conservative, full.effective_cost_per_received_share)
        equity = ctx.equity
        caps = {
            "kelly": c.kelly_fraction * k_raw * size_mult * equity,
            "max_position": c.max_position_usdc,
            "max_par_marche": c.max_exposure_per_market_pct * equity - self.portfolio.exposure_for_market(m.slug),
            "max_par_evenement": (c.max_exposure_per_event_pct * equity - self.portfolio.exposure_for_event(m.event_id)
                                  if m.event_id else math.inf),
            "max_total": c.max_total_exposure_pct * equity - self.portfolio.total_exposure(),
            "cash_hors_reserve": ctx.usdc_balance - c.min_usdc_reserve,
        }
        for tag in m.tags:
            if tag not in c.ignored_theme_tags:
                caps[f"max_theme:{tag}"] = c.max_exposure_per_theme_pct * equity - self.portfolio.exposure_for_tag(tag)
        binding = min(caps, key=caps.get)
        budget = caps[binding]
        d["caps"] = {k: round(v, 2) for k, v in caps.items() if math.isfinite(v)}
        d["kelly_raw"] = k_raw
        if budget < c.min_order_usdc:
            return reject(f"taille autorisee {budget:.2f} USDC < minimum {c.min_order_usdc} (contrainte: {binding})")
        max_book_shares = cand.max_shares * c.max_book_take_pct
        shares = shares_for_budget(levels, budget, max_book_shares)
        if shares * cand.levels[0][0] < budget - 1e-6 and shares >= max_book_shares - 0.01:
            binding = "profondeur_carnet"
        if shares < m.min_order_size:
            return reject(f"{shares} parts < taille minimale du marche {m.min_order_size} (contrainte: {binding})")
        q = quote_buy(levels, shares, cand.p_conservative, cand.fee_rate, self.edge.slippage_buffer,
                      cand.outcome, cand.token_id)
        if q is None or q.cost_usdc < c.min_order_usdc:
            return reject(f"ordre trop petit ({0 if q is None else q.cost_usdc:.2f} USDC)")
        if q.net_edge_per_share < self.edge.min_net_edge - 1e-9:
            return reject(f"avantage net final {q.net_edge_per_share:.4f} < {self.edge.min_net_edge}")

        d["quote"] = {"shares": q.shares, "cost": q.cost_usdc, "vwap": q.vwap, "worst": q.worst_price,
                      "fee": q.fee_usdc, "received": q.received_shares, "ev_gross": q.ev_gross_usdc,
                      "ev_net": q.ev_net_usdc}
        return Verdict(
            TradeDecision(
                decision_id=f"dec-{uuid.uuid4().hex[:12]}",
                event_id=ctx.event_id,
                market_slug=m.slug,
                token_id=cand.token_id,
                outcome=cand.outcome,
                limit_price=q.worst_price,
                shares=q.shares,
                expected_cost_usdc=q.cost_usdc,
                expected_fee_usdc=q.fee_usdc,
                net_edge_per_share=q.net_edge_per_share,
                p_conservative=cand.p_conservative,
                kelly_raw=k_raw,
                binding_constraint=binding,
                max_loss_usdc=q.cost_usdc * self.exits.max_loss_at_entry_pct,
                reason=(f"{cand.outcome}: p prudente {cand.p_conservative:.3f}, VWAP {q.vwap:.3f}, "
                        f"avantage net {q.net_edge_per_share:.3f}/part, contrainte {binding}"),
            ),
            "ok",
            d,
        )

    # --- sorties ---
    def exit_reason(self, pos: Position, held_book: BookState) -> str | None:
        """Stop de prix, perte maximale fixee a l'entree, prise de profit. Evalue sur le meilleur bid reel."""
        bid = held_book.best_bid
        if bid is None:
            return None
        if bid <= pos.avg_price * (1 - self.exits.stop_loss_pct):
            return "stop_price"
        if pos.max_loss_usdc is not None and pos.cost_usdc - pos.shares * bid >= pos.max_loss_usdc:
            return "max_loss"
        if bid >= self.exits.take_profit_price:
            return "take_profit"
        return None

    def thesis_invalidated(self, pos: Position, a: Assessment, held_book: BookState) -> bool:
        """Vrai si meme la borne HAUTE de la nouvelle estimation pour le cote detenu est sous le bid actuel:
        vendre maintenant vaut plus que garder, dans l'hypothese la plus favorable."""
        if a.abstain or held_book.best_bid is None:
            return False
        lo, hi = conservative_interval(a, self.edge)
        p_held_high = hi if pos.outcome == "YES" else 1 - lo
        return p_held_high < held_book.best_bid
