"""Boucle principale: ingestion -> filtre local -> Claude -> moteur de risque -> execution."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Awaitable, Callable

from brain.claude_analyst import ClaudeAnalyst
from core.config import BotConfig, Secrets, load_config, load_secrets
from core.models import Market, NewsItem
from core.state import PortfolioState
from execution.polygon_clob import ClobExecutor
from execution.risk_manager import RiskManager, floor_to_tick
from ingestion.news_filter import Trigger, TriggerEngine
from ingestion.polymarket_ws import PolymarketFeed, load_markets
from ingestion.rss_feed import poll_rss
from ingestion.twitter_stream import run_x

log = logging.getLogger("polybot")
HEARTBEAT_FILE = Path("state/heartbeat")


def setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = RotatingFileHandler("logs/bot.log", maxBytes=10_000_000, backupCount=10, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])
    for noisy in ("httpx", "websockets", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def supervised(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    """Relance une tache qui plante, sans faire tomber les autres."""
    backoff = 5
    while True:
        try:
            await factory()
            log.warning("Tache %s terminee, relance", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Tache %s plantee, relance dans %ss", name, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300)


class Bot:
    def __init__(self, cfg: BotConfig, secrets: Secrets, markets: dict[str, Market]):
        self.cfg = cfg
        self.secrets = secrets
        self.markets = markets
        self.state = PortfolioState()
        self.feed = PolymarketFeed(markets, cfg.trigger.price_window_s)
        self.triggers = TriggerEngine(markets, cfg.trigger, lambda slug: self.feed.history[slug].move())
        self.analyst = ClaudeAnalyst(cfg.claude, secrets.anthropic_api_key)
        self.risk = RiskManager(cfg.risk)
        self.executor = ClobExecutor(secrets, cfg.dry_run, self.state, cfg.paper_bankroll_usdc)
        self.news_queue: asyncio.Queue[NewsItem] = asyncio.Queue(maxsize=10_000)
        self.trade_lock = asyncio.Lock()  # une seule decision d'ordre a la fois (exposition coherente)
        self.feed.on_price(self._on_price)
        self._background: set[asyncio.Task] = set()

    def _spawn(self, trig: Trigger) -> None:
        """Lance l'analyse en tache de fond (ne bloque pas le WebSocket ni la file d'actus)."""
        task = asyncio.create_task(self._safe_handle(trig))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _safe_handle(self, trig: Trigger) -> None:
        try:
            await self.handle_trigger(trig)
        except Exception:
            log.exception("Erreur pendant le traitement du declenchement %s", trig.market.slug)

    # --- valorisation ---
    def mark_value(self) -> float:
        total = 0.0
        for pos in self.state.positions.values():
            book = self.feed.books.get(pos.token_id)
            bid = book.best_bid if book else None
            total += pos.shares * (bid if bid is not None else pos.avg_price)
        return total

    async def equity(self) -> tuple[float, float]:
        usdc = await self.executor.usdc_balance()
        return usdc, usdc + self.mark_value()

    # --- evenements ---
    async def _on_price(self, market: Market) -> None:
        trig = self.triggers.on_price(market.slug)
        if trig:
            self._spawn(trig)

    async def consume_news(self) -> None:
        while True:
            news = await self.news_queue.get()
            for trig in self.triggers.on_news(news):
                self._spawn(trig)

    async def handle_trigger(self, trig: Trigger) -> None:
        market = trig.market
        if not self.feed.is_fresh(market):
            log.warning("Carnet de %s pas a jour, declenchement ignore", market.slug)
            return
        yes_book, no_book = self.feed.book(market.yes_token_id), self.feed.book(market.no_token_id)
        assessment = await self.analyst.assess(market, trig.news, yes_book, no_book)
        self.state.journal(
            "assessment",
            market=market.slug,
            news=[{"source": n.source, "text": n.text, "url": n.url} for n in trig.news],
            price_move=trig.price_move,
            yes_mid=yes_book.mid,
            assessment=asdict(assessment) if assessment else None,
        )
        if assessment is None:
            return

        async with self.trade_lock:
            usdc, equity = await self.equity()
            daily = self.state.roll_day(equity)
            # Carnets relus apres l'appel a Claude: le prix a pu bouger pendant l'analyse.
            yes_book, no_book = self.feed.book(market.yes_token_id), self.feed.book(market.no_token_id)
            verdict = self.risk.evaluate(
                market, assessment, yes_book, no_book,
                usdc_balance=usdc,
                equity=equity,
                market_exposure=self.state.exposure_for_market(market.slug),
                total_exposure=self.state.total_exposure(),
                daily=daily,
            )
            self.state.save()
            self.state.journal("risk", market=market.slug, reason=verdict.reason,
                               decision=asdict(verdict.decision) if verdict.decision else None)
            if verdict.decision is None:
                log.info("Pas de trade sur %s: %s", market.slug, verdict.reason)
                return
            d = verdict.decision
            log.info("TRADE %s %s: %.2f shares @ <=%.3f (%.2f USDC) | %s",
                     market.slug, d.outcome, d.shares, d.limit_price, d.size_usdc, d.reason)
            fill = await self.executor.buy(d.token_id, d.limit_price, d.shares, self.feed.book(d.token_id))
            if fill:
                self.state.add_fill(market.slug, d.token_id, d.outcome, fill.price, fill.shares)
                self.state.journal("fill", market=market.slug, fill=asdict(fill))

    # --- surveillance des positions (stop-loss / take-profit) ---
    async def monitor_positions(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.risk.position_check_interval_s)
            async with self.trade_lock:
                for token_id, pos in list(self.state.positions.items()):
                    market = self.markets.get(pos.market_slug)
                    book = self.feed.books.get(token_id)
                    if market is None or book is None or not self.feed.is_fresh(market):
                        continue
                    reason = self.risk.exit_signal(pos, book.best_bid)
                    if not reason:
                        continue
                    log.warning("SORTIE %s %s: %s", pos.market_slug, pos.outcome, reason)
                    limit = floor_to_tick(max(book.best_bid - self.cfg.risk.max_slippage, market.tick_size),
                                          market.tick_size)
                    fill = await self.executor.sell(token_id, limit, pos.shares, book)
                    if fill:
                        pnl = self.state.reduce(token_id, fill.price, fill.shares)
                        self.state.journal("exit", market=pos.market_slug, reason=reason,
                                           fill=asdict(fill), pnl=pnl)
                        log.warning("Position fermee, PnL realise %.2f USDC", pnl)
                    else:
                        log.error("Sortie impossible pour %s (carnet trop fin), nouvel essai au prochain cycle",
                                  pos.market_slug)

    async def heartbeat(self) -> None:
        while True:
            HEARTBEAT_FILE.parent.mkdir(exist_ok=True)
            HEARTBEAT_FILE.write_text(str(int(time.time())))
            usdc, equity = await self.equity()
            self.state.roll_day(equity)
            log.info("Heartbeat: USDC %.2f | equity %.2f | %d position(s) | exposition %.2f",
                     usdc, equity, len(self.state.positions), self.state.total_exposure())
            await asyncio.sleep(300)

    async def run(self) -> None:
        await self.executor.start()
        tasks = [
            supervised("polymarket_ws", self.feed.run),
            supervised("news_consumer", self.consume_news),
            supervised("positions", self.monitor_positions),
            supervised("heartbeat", self.heartbeat),
        ]
        if self.cfg.news.rss.enabled and self.cfg.news.rss.feeds:
            tasks.append(supervised("rss", lambda: poll_rss(self.cfg.news.rss, self.news_queue)))
        if self.cfg.news.x.enabled:
            if not self.secrets.x_bearer_token:
                log.error("X active dans config.json mais X_BEARER_TOKEN absent: source X desactivee")
            else:
                tasks.append(supervised("x", lambda: run_x(self.cfg.news.x, self.secrets.x_bearer_token,
                                                           self.news_queue)))
        await asyncio.gather(*tasks)


async def amain(config_path: str) -> None:
    cfg = load_config(config_path)
    secrets = load_secrets()
    if not secrets.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY absent de .env: le SDK tentera les autres sources d'identifiants")
    markets = await load_markets(cfg.markets)
    if not markets:
        log.error("Aucun marche valide dans la whitelist, arret.")
        sys.exit(1)
    bot = Bot(cfg, secrets, markets)

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, main_task.cancel)
    try:
        await bot.run()
    except asyncio.CancelledError:
        log.info("Arret demande, etat sauvegarde.")
        bot.state.save()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot Polymarket pilote par l'actualite")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    setup_logging()
    asyncio.run(amain(args.config))


if __name__ == "__main__":
    main()
