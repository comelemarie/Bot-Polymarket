"""Cycle de vie des ordres: idempotence, verification finale, envoi, reconciliation.

Sequence d'une entree:
  1. Cle d'idempotence = (evenement, token): un meme evenement ne produit jamais deux entrees, meme apres
     un redemarrage ou une nouvelle decision.
  2. Refus si un ordre du marche est en etat incertain (intent/unknown) tant qu'il n'est pas reconcilie.
  3. Derniere verification du carnet (REST en PAPER/LIVE) et du prix limite.
  4. Re-evaluation complete du risque avec ce carnet et le solde du moment.
  5. Ecriture de l'INTENTION en base AVANT l'envoi (une panne pendant l'envoi laisse une trace).
  6. Envoi FOK, puis mise a jour atomique de l'ordre, de la position et des liquidites.
Une reponse perdue n'est jamais interpretee comme "non execute": l'ordre passe en UNKNOWN et la
reconciliation interroge l'etat reel (ordre, positions du compte) avant toute nouvelle tentative.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from core.clock import Clock
from core.config import ExitConfig, MarketDataConfig
from core.metrics import Metrics
from core.models import Market, OrderRequest, SubmitResult, TradeDecision
from core.state import Portfolio, Position
from core.store import Store
from execution.edge_calculator import EPS, taker_fee_usdc
from execution.risk_manager import Verdict
from ingestion.clob_rest import ClobRest
from ingestion.market_state import BookState

log = logging.getLogger(__name__)

UNRESOLVED = ("intent", "unknown")
RECONCILE_GRACE_S = 300
RiskRecheck = Callable[[BookState], Awaitable[Verdict]]


class ExecutionEngine:
    def __init__(self, mode: str, store: Store, portfolio: Portfolio, broker, rest: ClobRest | None,
                 mcfg: MarketDataConfig, exits: ExitConfig, clock: Clock, metrics: Metrics):
        self.mode = mode
        self.store = store
        self.portfolio = portfolio
        self.broker = broker
        self.rest = rest
        self.mcfg = mcfg
        self.exits = exits
        self.clock = clock
        self.metrics = metrics
        self.market_lookup: Callable[[str], Market | None] = lambda slug: None

    # --- etat ---
    def unresolved_on(self, token_ids: list[str]) -> bool:
        marks = ",".join("?" for _ in token_ids)
        return self.store.one(
            f"SELECT 1 FROM orders WHERE token_id IN ({marks}) AND status IN ('intent','unknown') LIMIT 1",
            token_ids) is not None

    def _set_order(self, cid: str, fields: dict) -> None:
        self.store.update("orders", "client_order_id", cid, {**fields, "updated_at": self.clock.now()})

    # --- verification finale du carnet ---
    async def final_book(self, token_id: str, local: BookState) -> tuple[BookState | None, str]:
        if self.mode == "replay" or not self.mcfg.verify_rest_before_order or self.rest is None:
            return (local, "") if local.synced else (None, "carnet local non synchronise")
        rb = await self.rest.fetch_book(token_id)
        if rb is None or not rb.book.synced:
            return None, "carnet REST indisponible ou invalide"
        for name, a, b in (("ask", local.best_ask, rb.book.best_ask), ("bid", local.best_bid, rb.book.best_bid)):
            if a is not None and b is not None and abs(a - b) > self.mcfg.max_rest_price_divergence:
                return None, f"divergence carnet WebSocket/REST sur best_{name} ({a} vs {b})"
        return rb.book, ""

    # --- entree ---
    async def enter(self, d: TradeDecision, market: Market, local_book: BookState, fee_rate: float,
                    recheck: RiskRecheck) -> str:
        cid = f"entry:{d.event_id}:{d.token_id}"
        if self.store.one("SELECT 1 FROM orders WHERE client_order_id=?", (cid,)):
            return "duplicate"
        if self.unresolved_on([market.yes_token_id, market.no_token_id]):
            return "blocked: ordre incertain non reconcilie"
        book, why = await self.final_book(d.token_id, local_book)
        if book is None:
            return f"aborted: {why}"
        verdict = await recheck(book)
        if verdict.decision is None:
            return f"aborted: re-verification du risque: {verdict.reason}"
        final = verdict.decision
        shares = min(d.shares, final.shares)
        limit = min(d.limit_price, final.limit_price)
        if book.best_ask is None or book.best_ask > limit + EPS:
            return f"aborted: le prix a bouge (ask {book.best_ask} > limite {limit})"
        req = OrderRequest(cid, d.token_id, "BUY", limit, shares)
        now = self.clock.now()
        self.store.insert("orders", {
            "client_order_id": cid, "decision_id": d.decision_id, "created_at": now, "updated_at": now,
            "market_slug": market.slug, "token_id": d.token_id, "side": "BUY", "purpose": "entry",
            "limit_price": limit, "shares": shares, "status": "intent", "observed_best_price": book.best_ask,
        })
        t0 = self.clock.now()
        res = await self.broker.submit_fok(req, book, fee_rate)
        self.metrics.observe("latency.order_submit", self.clock.now() - t0)
        if res.status == "filled" and res.fee_usdc == 0 and fee_rate > 0:
            # La reponse d'ordre ne detaille pas les frais: estimation par la formule officielle, signalee.
            res.fee_usdc = taker_fee_usdc(res.filled_shares, res.avg_price, fee_rate)
            res.raw = {**res.raw, "fee_estimated": True}
        self._apply_entry_result(cid, res, d, market)
        self.metrics.inc(f"orders.entry.{res.status}")
        return res.status if res.status != "rejected" else f"rejected: {res.error}"

    def _apply_entry_result(self, cid: str, res: SubmitResult, d: TradeDecision, market: Market) -> None:
        with self.store.tx():
            self._set_order(cid, {"status": res.status, "exchange_order_id": res.exchange_order_id,
                                  "filled_shares": res.filled_shares, "avg_price": res.avg_price or None,
                                  "fee_usdc": res.fee_usdc, "error": res.error or None, "raw": res.raw})
            if res.status == "filled":
                cost = res.raw.get("cost_usdc", res.filled_shares * res.avg_price)
                self.portfolio.add_fill(
                    market_slug=market.slug, token_id=d.token_id, outcome=d.outcome, shares=res.filled_shares,
                    cost_usdc=cost, ts=self.clock.now(), event_id=market.event_id, tags=market.tags,
                    decision_id=d.decision_id, max_loss_usdc=cost * self.exits.max_loss_at_entry_pct,
                    p_low=d.p_conservative if d.outcome == "YES" else None,
                    p_high=1 - d.p_conservative if d.outcome == "NO" else None,
                )
                if not self.broker.live:
                    self.broker.apply_cash(-cost)
        if res.status == "filled":
            log.warning("ENTREE %s %s: %.2f parts @ %.4f (cle %s)", market.slug, d.outcome, res.filled_shares,
                        res.avg_price, cid)
        elif res.status == "unknown":
            self.store.log_error(self.clock.now(), "execution", f"ordre {cid} en etat incertain: {res.error}")
            log.error("ORDRE INCERTAIN %s: %s. Entrees bloquees sur ce marche jusqu'a reconciliation.", cid, res.error)

    # --- sortie ---
    async def exit(self, pos: Position, market: Market, reason: str, local_book: BookState, fee_rate: float) -> str:
        if self.unresolved_on([pos.token_id]):
            return "blocked: ordre incertain non reconcilie"
        book, why = await self.final_book(pos.token_id, local_book)
        if book is None or book.best_bid is None:
            return f"aborted: {why or 'aucun acheteur'}"
        limit = max(market.tick_size, round(book.best_bid - self.exits.exit_slippage, 4))
        available = sum(s for p, s in book.bids.items() if p >= limit - EPS)
        shares = min(pos.shares, available)
        shares = int(shares * 100) / 100
        if shares < min(market.min_order_size, pos.shares) - EPS or shares <= 0:
            return f"aborted: profondeur acheteuse insuffisante ({available:.2f} parts >= {limit})"
        now = self.clock.now()
        cid = f"exit:{pos.token_id}:{reason}:{int(now * 1000)}"
        self.store.insert("orders", {
            "client_order_id": cid, "decision_id": f"exit:{reason}", "created_at": now, "updated_at": now,
            "market_slug": market.slug, "token_id": pos.token_id, "side": "SELL", "purpose": "exit",
            "limit_price": limit, "shares": shares, "status": "intent", "observed_best_price": book.best_bid,
        })
        res = await self.broker.submit_fok(OrderRequest(cid, pos.token_id, "SELL", limit, shares), book, fee_rate)
        self.metrics.inc(f"orders.exit.{res.status}")
        with self.store.tx():
            self._set_order(cid, {"status": res.status, "exchange_order_id": res.exchange_order_id,
                                  "filled_shares": res.filled_shares, "avg_price": res.avg_price or None,
                                  "fee_usdc": res.fee_usdc, "error": res.error or None, "raw": res.raw})
            if res.status == "filled":
                proceeds = res.raw.get("proceeds_usdc", res.filled_shares * res.avg_price - res.fee_usdc)
                pnl = self.portfolio.reduce(token_id=pos.token_id, shares=res.filled_shares, proceeds_usdc=proceeds,
                                            fee_usdc=res.fee_usdc, ts=self.clock.now(), reason=reason,
                                            client_order_id=cid)
                if not self.broker.live:
                    self.broker.apply_cash(proceeds)
                log.warning("SORTIE %s (%s): %.2f parts @ %.4f, PnL %.2f", market.slug, reason, res.filled_shares,
                            res.avg_price, pnl)
        return res.status

    # --- reprise et reconciliation ---
    async def recover_on_startup(self) -> None:
        rows = self.store.query("SELECT client_order_id FROM orders WHERE status='intent'")
        for r in rows:
            if self.broker.live:
                self._set_order(r["client_order_id"], {"status": "unknown", "error": "interrompu pendant l'envoi"})
            else:
                # En simulation, l'execution et sa comptabilisation sont dans la meme transaction:
                # une intention orpheline n'a donc produit aucun effet.
                self._set_order(r["client_order_id"], {"status": "cancelled", "error": "interrompu (simulation)"})
        if rows:
            log.warning("%d ordre(s) interrompu(s) au redemarrage", len(rows))
        await self.reconcile()

    async def reconcile(self) -> bool:
        """Aligne ordres et positions sur l'etat reel. Retourne True si tout est coherent."""
        if not self.broker.live:
            return True
        ok = True
        for r in self.store.query("SELECT * FROM orders WHERE status='unknown'"):
            ok &= await self._resolve_unknown(r)
        remote = await self.broker.positions()
        if remote is None:
            self.portfolio.halt("reconciliation", "positions du compte indisponibles", self.clock.now())
            return False
        local = {t: p.shares for t, p in self.portfolio.positions().items()}
        # La Data API peut avoir quelques minutes de retard sur une execution: on ne compare pas un token
        # traite recemment (sinon faux ecart et coupe-circuit injustifie).
        recent = {r["token_id"] for r in self.store.query(
            "SELECT DISTINCT token_id FROM orders WHERE updated_at >= ?", (self.clock.now() - RECONCILE_GRACE_S,))}
        for token in (set(local) | set(remote)) - recent:
            lv, rv = local.get(token, 0.0), remote.get(token, 0.0)
            if abs(lv - rv) > 0.01:
                ok = False
                msg = f"position {token[:12]}: locale {lv:.2f} vs compte {rv:.2f}"
                self.store.log_error(self.clock.now(), "reconciliation", msg)
                log.error("ECART DE RECONCILIATION: %s", msg)
                if token in local:
                    self.portfolio.set_shares(token, rv)  # le compte reel fait foi
                self.portfolio.halt("reconciliation", msg, self.clock.now())
        return ok

    async def _resolve_unknown(self, row) -> bool:
        cid = row["client_order_id"]
        res = await self.broker.lookup_order(row["exchange_order_id"]) if row["exchange_order_id"] else None
        if res is not None and res.error == "ordre au carnet":
            await self.broker.cancel(row["exchange_order_id"])  # un FOK ne doit jamais rester au carnet
            res = await self.broker.lookup_order(row["exchange_order_id"])
        if res is None or res.status == "unknown":
            log.error("Ordre %s toujours incertain: entrees bloquees sur %s", cid, row["market_slug"])
            return False
        with self.store.tx():
            self._set_order(cid, {"status": res.status, "filled_shares": res.filled_shares,
                                  "avg_price": res.avg_price or None, "error": f"reconcilie: {res.error}"})
            if res.status == "filled" and row["purpose"] == "entry":
                # Le detail des frais n'est pas connu ici: la position sera alignee sur le compte ensuite.
                m = self.market_lookup(row["market_slug"])
                self.portfolio.add_fill(market_slug=row["market_slug"], token_id=row["token_id"],
                                        outcome=m.outcome_of(row["token_id"]) if m else "?",
                                        shares=res.filled_shares, cost_usdc=res.filled_shares * res.avg_price,
                                        ts=self.clock.now(), event_id=m.event_id if m else "",
                                        tags=m.tags if m else [], decision_id=row["decision_id"])
        log.warning("Ordre %s reconcilie: %s", cid, res.status)
        return True

    async def emergency_stop(self, flatten: bool, positions_markets: list[tuple[Position, Market, BookState, float]]) -> None:
        """Arret d'urgence: annulation des ordres au carnet (LIVE) puis, si demande, sortie de toutes les positions."""
        if self.broker.live:
            try:
                await self.broker.cancel_all()
            except Exception as e:
                log.error("cancel_all a echoue: %s", e)
        if flatten:
            for pos, market, book, rate in positions_markets:
                status = await self.exit(pos, market, "emergency", book, rate)
                log.warning("Sortie d'urgence %s: %s", market.slug, status)
