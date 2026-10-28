"""Orchestrateur commun aux modes PAPER, LIVE et REPLAY.

collecte -> normalisation/dedoublonnage -> detection locale -> analyse (Claude) -> avantage net
-> risque -> execution -> persistance. Chaque etape ecrit en base ce qu'elle a vu et decide.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
from dataclasses import asdict
from typing import Awaitable, Callable

from core.clock import Clock
from core.config import BotConfig, MarketWatch, Secrets
from core.metrics import Metrics
from core.models import Assessment, Market, NewsItem
from core.state import Portfolio
from core.store import Store
from execution.execution_engine import ExecutionEngine
from execution.risk_manager import RiskContext, RiskManager, Verdict
from ingestion.clob_rest import ClobRest
from ingestion.market_state import BookState, MarketState
from ingestion.news_filter import Trigger, TriggerEngine
from ingestion.news_normalizer import NewsDeduplicator

log = logging.getLogger("polybot")


class LiveScheduler:
    """Execute une coroutine a un instant donne (temps reel). Garde une reference aux taches."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.tasks: set[asyncio.Task] = set()

    def schedule(self, at: float, fn: Callable[[], Awaitable[None]], name: str = "") -> None:
        async def runner():
            delay = at - self.clock.now()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await fn()
            except Exception:
                log.exception("Tache %s en echec", name)

        task = asyncio.create_task(runner())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


class ReplayScheduler:
    """File chronologique: les actions differees (decision apres latence) s'intercalent avec les donnees."""

    def __init__(self):
        self.heap: list = []
        self._seq = itertools.count()

    def schedule(self, at: float, fn: Callable[[], Awaitable[None]], name: str = "") -> None:
        heapq.heappush(self.heap, (at, next(self._seq), fn, name))

    def next_time(self) -> float | None:
        return self.heap[0][0] if self.heap else None

    def pop(self):
        at, _, fn, name = heapq.heappop(self.heap)
        return at, fn, name


class Bot:
    def __init__(self, cfg: BotConfig, secrets: Secrets, mode: str, clock: Clock, store: Store, broker, analyst,
                 rest: ClobRest | None, scheduler, recorder=None, keyword_gen=None):
        self.cfg = cfg
        self.secrets = secrets
        self.mode = mode
        self.clock = clock
        self.store = store
        self.broker = broker
        self.analyst = analyst
        self.rest = rest
        self.scheduler = scheduler
        self.recorder = recorder
        self.keyword_gen = keyword_gen
        self.metrics = Metrics()
        self.portfolio = Portfolio(store)
        self.markets: dict[str, Market] = {}
        self.state = MarketState(cfg.market_data, cfg.trigger.price_window_s, cfg.trigger.max_spread_for_analysis)
        self.triggers = TriggerEngine(cfg.trigger, self.state, store, clock)
        self.dedup = NewsDeduplicator(store, cfg.news, clock)
        self.risk = RiskManager(cfg.risk, cfg.edge, cfg.exits, self.portfolio)
        self.execution = ExecutionEngine(mode, store, self.portfolio, broker, rest, cfg.market_data, cfg.exits,
                                         clock, self.metrics)
        self.execution.market_lookup = self.markets.get
        self.trade_lock = asyncio.Lock()
        self.recorded_fees: dict[str, float] = {}  # REPLAY: taux enregistres pendant la collecte
        self.on_markets_changed: Callable[[], None] = lambda: None

    # --- marches ---
    def set_markets(self, markets: dict[str, Market]) -> None:
        added, removed = self.state.set_markets(markets)
        self.markets.clear()
        self.markets.update(self.state.markets)
        self.triggers.set_markets(self.markets)
        if self.recorder:
            self.recorder.markets(self.markets, self.clock.now())
        if added or removed:
            log.info("Marches surveilles: %d (+%d / -%d tokens)", len(markets), len(added), len(removed))
            self.on_markets_changed()

    async def refresh_markets(self) -> None:
        """Liste manuelle + decouverte automatique + marches ou une position est ouverte."""
        from ingestion.market_discovery import discover_markets, phrase_to_regex
        from ingestion.polymarket_ws import load_markets

        markets = await load_markets(self.cfg.markets) if self.cfg.markets else {}
        if self.keyword_gen:
            discovered = await discover_markets(self.cfg.discovery, self.keyword_gen)
            markets = {**discovered, **markets}
        orphans = {p.market_slug for p in self.portfolio.positions().values()} - markets.keys()
        for slug in list(orphans):
            if slug in self.markets:
                markets[slug] = self.markets[slug]
                orphans.discard(slug)
        if orphans:
            watches = []
            for slug in orphans:
                phrases = (self.keyword_gen.cached(slug) if self.keyword_gen else None) or []
                watches.append(MarketWatch(slug=slug, keywords=[phrase_to_regex(k) for k in phrases]))
            markets.update(await load_markets(watches))
        self.set_markets(markets)

    async def fee_rate(self, token_id: str) -> float:
        if self.mode == "replay" or self.rest is None:
            return self.recorded_fees.get(token_id, self.cfg.fees.fallback_taker_fee_rate)
        rate, source = await self.rest.fee_rate(token_id)
        if self.recorder and source == "api":
            self.recorder("fee", {"token_id": token_id, "rate": rate}, self.clock.now())
        return rate

    # --- evenements entrants ---
    async def on_news(self, item: NewsItem) -> None:
        if self.recorder:
            self.recorder.news(item)
        self.metrics.inc("news.received")
        fresh = self.dedup.process(item)
        if fresh is None:
            self.metrics.inc("news.duplicate")
            return
        if fresh.age_s is not None:
            self.metrics.observe("latency.news_publication_to_reception", fresh.age_s)
        for trig in self.triggers.on_news(fresh):
            await self.dispatch(self.handle_trigger(trig), f"trigger {trig.market.slug}")

    async def on_book_update(self, slugs: set[str], ts: float) -> None:
        for trig in self.triggers.on_price(slugs):
            await self.dispatch(self.handle_trigger(trig), f"trigger {trig.market.slug}")

    async def dispatch(self, coro, name: str) -> None:
        """LIVE/PAPER: en tache de fond (ne bloque pas les flux). REPLAY: immediatement, dans l'ordre."""
        if self.mode == "replay":
            await coro
        else:
            self.scheduler.schedule(self.clock.now(), lambda: coro, name)

    def snapshot(self, ref_id: str, market: Market) -> None:
        for tok in (market.yes_token_id, market.no_token_id):
            b = self.state.books[tok]
            self.store.insert("market_snapshots", {
                "ref_id": ref_id, "ts": self.clock.now(), "market_slug": market.slug, "token_id": tok,
                "best_bid": b.best_bid, "best_ask": b.best_ask, "bid_depth_usdc": b.bid_depth_usdc(),
                "ask_depth_usdc": b.ask_depth_usdc(), "synced": b.synced, "levels": b.levels(),
            })

    # --- analyse puis decision ---
    async def handle_trigger(self, trig: Trigger) -> None:
        m = trig.market
        self.snapshot(trig.event_id, m)
        yes, no = self.state.books[m.yes_token_id], self.state.books[m.no_token_id]
        corrob = {n.id: self.dedup.corroborations(n.id) for n in trig.news}
        a = await self.analyst.assess(trig, yes, no, corrob)
        self.metrics.inc("analyses.abstain" if a.abstain else "analyses.ok")
        self.metrics.inc("claude.cost_usd", a.cost_usd)
        decide_at = trig.created_at + a.latency_s
        if self.mode == "replay":  # en direct ces delais s'ecoulent reellement; en replay on les simule
            decide_at += self.cfg.replay.processing_latency_s + self.cfg.replay.execution_latency_s
        self.scheduler.schedule(max(decide_at, self.clock.now()), lambda: self.decide(trig, a), f"decide {m.slug}")

    async def risk_context(self, market: Market, a: Assessment, event_id: str, yes: BookState, no: BookState,
                           fee_rates: dict[str, float]) -> RiskContext:
        now = self.clock.now()
        usdc = await self.broker.balance()
        equity = daily = peak = None
        if usdc is not None:
            equity = usdc + self.mark_value()
            daily = self.portfolio.roll_day(equity, now)
            peak = self.portfolio.update_peak(equity, now)
        return RiskContext(
            market=market, assessment=a, event_id=event_id, yes_book=yes, no_book=no, fee_rates=fee_rates,
            tradable=self.state.tradable(market, now), usdc_balance=usdc, equity=equity, daily=daily,
            peak_equity=peak, halts=self.portfolio.halts(),
            pending_orders_on_market=self.execution.unresolved_on([market.yes_token_id, market.no_token_id]), now=now,
        )

    async def decide(self, trig: Trigger, a: Assessment) -> None:
        m = trig.market
        if m.slug not in self.markets:
            return
        t_decide = self.clock.now()
        self.snapshot(f"decide:{trig.event_id}", m)
        fee_rates = {t: await self.fee_rate(t) for t in (m.yes_token_id, m.no_token_id)}
        async with self.trade_lock:
            # Invalidation de these pour une position deja ouverte sur ce marche
            if self.cfg.exits.thesis_invalidation:
                for pos in [p for p in self.portfolio.positions().values() if p.market_slug == m.slug]:
                    book = self.state.books[pos.token_id]
                    if self.risk.thesis_invalidated(pos, a, book):
                        status = await self.execution.exit(pos, m, "thesis_invalidated", book, fee_rates[pos.token_id])
                        self._record_decision(trig, a, "exit", f"these invalidee: {status}", {})
            yes, no = self.state.books[m.yes_token_id], self.state.books[m.no_token_id]
            verdict = self.risk.evaluate(await self.risk_context(m, a, trig.event_id, yes, no, fee_rates))
            if verdict.decision is None:
                self._record_decision(trig, a, "reject", verdict.reason, verdict.details)
                log.info("Pas de trade [%s]: %s", m.slug, verdict.reason)
                return
            d = verdict.decision
            self._record_decision(trig, a, "trade", d.reason, {**verdict.details, "decision": asdict(d)}, d.decision_id)

            async def recheck(book: BookState) -> Verdict:
                y = book if book.asset_id == m.yes_token_id else self.state.books[m.yes_token_id]
                n = book if book.asset_id == m.no_token_id else self.state.books[m.no_token_id]
                return self.risk.evaluate(await self.risk_context(m, a, trig.event_id, y, n, fee_rates))

            status = await self.execution.enter(d, m, self.state.books[d.token_id], fee_rates[d.token_id], recheck)
            self.metrics.observe("latency.trigger_to_order", self.clock.now() - trig.created_at)
            self.store.update("decisions", "id", d.decision_id, {"details": {
                **verdict.details, "decision": asdict(d), "execution": status, "decided_at": t_decide}})
            log.info("Execution [%s]: %s", m.slug, status)

    def _record_decision(self, trig: Trigger, a: Assessment, action: str, reason: str, details: dict,
                         decision_id: str | None = None) -> None:
        import uuid

        self.store.insert("decisions", {
            "id": decision_id or f"dec-{uuid.uuid4().hex[:12]}", "event_id": trig.event_id,
            "analysis_id": a.analysis_id or None, "market_slug": trig.market.slug, "created_at": self.clock.now(),
            "action": action, "reason": reason, "details": details,
        })

    # --- positions ---
    def mark_value(self) -> float:
        total = 0.0
        for pos in self.portfolio.positions().values():
            b = self.state.books.get(pos.token_id)
            bid = b.best_bid if b is not None and b.synced else None
            total += pos.shares * (bid if bid is not None else pos.avg_price)
        return total

    async def check_positions(self) -> None:
        now = self.clock.now()
        async with self.trade_lock:
            for pos in list(self.portfolio.positions().values()):
                m = self.markets.get(pos.market_slug)
                book = self.state.books.get(pos.token_id)
                if m is None or book is None:
                    continue
                ok, why = self.state.tradable(m, now)
                if not ok:
                    self.metrics.inc("positions.unchecked")
                    continue
                reason = self.risk.exit_reason(pos, book)
                if reason:
                    status = await self.execution.exit(pos, m, reason, book, await self.fee_rate(pos.token_id))
                    log.warning("Sortie %s [%s]: %s", reason, m.slug, status)

    async def check_resolutions(self) -> None:
        """Enregistre les resolutions (pour le suivi de performance) et solde les positions papier."""
        if self.rest is None:
            return
        slugs = {p.market_slug for p in self.portfolio.positions().values()}
        slugs |= {r["market_slug"] for r in self.store.query(
            "SELECT DISTINCT market_slug FROM analyses WHERE market_slug NOT IN (SELECT market_slug FROM outcomes) "
            "AND created_at >= ?", (self.clock.now() - 90 * 86400,))}
        for slug in slugs:
            winner = await self.rest.market_resolution(slug)
            if winner is None:
                continue
            self.store.insert("outcomes", {"market_slug": slug, "resolved_at": self.clock.now(), "winner": winner},
                              or_ignore=True)
            for pos in [p for p in self.portfolio.positions().values() if p.market_slug == slug]:
                payout = 1.0 if pos.outcome == winner else 0.0
                if self.broker.live:
                    self.store.update("positions", "token_id", pos.token_id, {"status": "resolved_pending_redeem"})
                    log.warning("Marche %s resolu (%s): position a racheter (redeem) manuellement", slug, winner)
                else:
                    with self.store.tx():
                        pnl = self.portfolio.reduce(token_id=pos.token_id, shares=pos.shares,
                                                    proceeds_usdc=pos.shares * payout, fee_usdc=0.0,
                                                    ts=self.clock.now(), reason="resolution")
                        self.broker.apply_cash(pos.shares * payout)
                    log.warning("Resolution %s (%s): position soldee, PnL %.2f", slug, winner, pnl)

    async def check_emergency(self) -> None:
        req = self.store.kv_get("emergency_request")
        if not req:
            return
        self.store.kv_delete("emergency_request")
        log.error("ARRET D'URGENCE demande (flatten=%s)", req.get("flatten"))
        targets = []
        for pos in self.portfolio.positions().values():
            m, b = self.markets.get(pos.market_slug), self.state.books.get(pos.token_id)
            if m and b:
                targets.append((pos, m, b, await self.fee_rate(pos.token_id)))
        async with self.trade_lock:
            await self.execution.emergency_stop(bool(req.get("flatten")), targets)

    async def heartbeat(self) -> dict:
        now = self.clock.now()
        usdc = await self.broker.balance()
        snap = self.metrics.flush(self.store, now)
        if usdc is not None:
            equity = usdc + self.mark_value()
            self.portfolio.roll_day(equity, now)
            self.portfolio.update_peak(equity, now)
            self.portfolio.record_equity(now, usdc, equity)
            log.info("Heartbeat: USDC %.2f | equity %.2f | %d position(s) | exposition %.2f | coupe-circuits %s",
                     usdc, equity, len(self.portfolio.positions()), self.portfolio.total_exposure(),
                     list(self.portfolio.halts()) or "aucun")
        else:
            log.error("Heartbeat: solde indisponible (entrees bloquees)")
        return snap
