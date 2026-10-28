"""Courtier simule (PAPER et REPLAY): FOK contre le carnet fourni, frais Polymarket inclus.

N'importe et n'instancie AUCUN client d'ordres reel. Ne simule ni l'impact de nos propres ordres sur le
carnet ni la file d'attente: le resultat est un majorant optimiste de l'execution reelle.
"""
from __future__ import annotations

import uuid

from core.models import OrderRequest, SubmitResult
from core.state import Portfolio
from execution.edge_calculator import quote_buy, quote_sell
from ingestion.market_state import BookState


class PaperBroker:
    live = False

    def __init__(self, portfolio: Portfolio, bankroll: float):
        self.portfolio = portfolio
        if portfolio.paper_usdc is None:
            portfolio.set_paper_usdc(bankroll)

    async def start(self) -> None:
        return None

    async def balance(self) -> float | None:
        return self.portfolio.paper_usdc

    async def submit_fok(self, req: OrderRequest, book: BookState, fee_rate: float) -> SubmitResult:
        oid = f"paper-{uuid.uuid4().hex[:10]}"
        if req.side == "BUY":
            q = quote_buy(book.asks_up_to(req.limit_price), req.shares, 0.0, fee_rate, 0.0)
            if q is None:
                return SubmitResult("rejected", error="FOK annule: profondeur insuffisante au prix limite",
                                    exchange_order_id=oid)
            cash = self.portfolio.paper_usdc or 0.0
            if q.cost_usdc > cash + 1e-9:
                return SubmitResult("rejected", error="solde papier insuffisant", exchange_order_id=oid)
            # filled_shares = parts recues (frais preleves en parts), avg_price = USDC payes / parts recues
            return SubmitResult("filled", q.received_shares, q.cost_usdc / q.received_shares, q.fee_usdc, oid,
                                raw={"cost_usdc": q.cost_usdc, "paid_shares": q.shares, "vwap": q.vwap})
        s = quote_sell(book.bids_down_to(req.limit_price), req.shares, fee_rate)
        if s is None:
            return SubmitResult("rejected", error="FOK annule: profondeur insuffisante au prix limite",
                                exchange_order_id=oid)
        return SubmitResult("filled", s.shares, s.vwap, s.fee_usdc, oid, raw={"proceeds_usdc": s.proceeds_usdc})

    def apply_cash(self, delta_usdc: float) -> None:
        self.portfolio.set_paper_usdc((self.portfolio.paper_usdc or 0.0) + delta_usdc)

    async def lookup_order(self, exchange_order_id: str) -> SubmitResult | None:
        return None

    async def positions(self) -> dict[str, float] | None:
        return {t: p.shares for t, p in self.portfolio.positions().items()}

    async def cancel_all(self) -> None:
        return None
