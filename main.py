"""Point d'entree.

  python main.py                       # PAPER (defaut): flux reels, aucun ordre reel
  python main.py --confirm-live        # LIVE, si et seulement si les 4 conditions de core/modes.py sont reunies
  python main.py --status              # etat: mode, positions, coupe-circuits, ordres incertains
  python main.py --halt [--flatten]    # arret d'urgence (plus aucune entree; --flatten: sortie des positions)
  python main.py --resume              # leve les coupe-circuits manuels/drawdown/reconciliation
Le replay est dans backtest/replay.py, le rapport de performance dans analytics/performance_tracker.py.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from bot import Bot, LiveScheduler
from brain.claude_analyst import ClaudeAnalyst
from core.clock import Clock
from core.config import BotConfig, load_config, load_secrets
from core.logging_setup import setup_logging
from core.modes import LiveGateError, assert_live_allowed, effective_mode
from core.state import Portfolio
from core.store import Store
from execution.paper_broker import PaperBroker
from ingestion.clob_rest import ClobRest
from ingestion.polymarket_ws import PolymarketFeed
from ingestion.recorder import Recorder
from ingestion.rss_feed import poll_rss
from ingestion.twitter_stream import run_x

log = logging.getLogger("polybot")
HEARTBEAT_FILE = Path("state/heartbeat")


async def supervised(name: str, factory: Callable[[], Awaitable[None]], store: Store, clock: Clock) -> None:
    """Relance une tache qui plante, sans faire tomber les autres."""
    backoff = 5
    while True:
        try:
            await factory()
            log.warning("Tache %s terminee, relance", name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Tache %s plantee, relance dans %ss", name, backoff)
            store.log_error(clock.now(), name, repr(e))
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300)


async def every(seconds: float, fn: Callable[[], Awaitable[object]]) -> None:
    while True:
        await asyncio.sleep(seconds)
        await fn()


def build_broker(mode: str, secrets, portfolio: Portfolio, cfg: BotConfig):
    if mode == "live":
        from execution.polygon_clob import LiveClobBroker  # importe uniquement en LIVE

        return LiveClobBroker(secrets)
    return PaperBroker(portfolio, cfg.paper_bankroll_usdc)


async def run(cfg: BotConfig, mode: str) -> None:
    secrets = load_secrets()
    clock = Clock()
    store = Store(cfg.storage.db_path)
    store.log_error(clock.now(), "startup", f"demarrage en mode {mode.upper()}")
    portfolio = Portfolio(store)
    broker = build_broker(mode, secrets, portfolio, cfg)
    await broker.start()
    rest = ClobRest(cfg.fees, clock)
    recorder = Recorder(cfg.storage.record_dir) if cfg.storage.record_market_data else None
    analyst = ClaudeAnalyst(cfg.claude, secrets.anthropic_api_key, store, clock)
    keyword_gen = None
    if cfg.discovery.enabled:
        from ingestion.market_discovery import KeywordGenerator

        keyword_gen = KeywordGenerator(secrets.anthropic_api_key, cfg.discovery.keyword_model)
    scheduler = LiveScheduler(clock)
    bot = Bot(cfg, secrets, mode, clock, store, broker, analyst, rest, scheduler, recorder, keyword_gen)
    feed = PolymarketFeed(bot.state, cfg.market_data, clock, bot.on_book_update, recorder)
    bot.on_markets_changed = feed.request_resubscribe

    try:
        await bot.refresh_markets()  # avant la reprise: la reconciliation a besoin des marches
    except httpx.HTTPError as e:
        # Reseau pas encore disponible au demarrage, API indisponible...: launchd relancera le processus.
        log.error("API Polymarket (Gamma) injoignable au demarrage: %s. Arret, nouvel essai au prochain lancement.", e)
        await rest.close()
        sys.exit(1)
    await bot.execution.recover_on_startup()
    if not bot.markets:
        log.error("Aucun marche a surveiller, arret.")
        sys.exit(1)
    log.warning("MODE %s | %d marches | solde %s USDC | coupe-circuits: %s", mode.upper(), len(bot.markets),
                await broker.balance(), list(portfolio.halts()) or "aucun")

    news_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)

    async def consume_news():
        while True:
            await bot.on_news(await news_queue.get())

    async def heartbeat():
        await bot.heartbeat()
        HEARTBEAT_FILE.parent.mkdir(exist_ok=True)
        HEARTBEAT_FILE.write_text(str(int(clock.now())))
        if recorder:
            recorder.flush()

    async def safe(fn):
        try:
            await fn()
        except Exception:
            log.exception("Erreur periodique dans %s", getattr(fn, "__name__", fn))

    tasks = [
        supervised("polymarket_ws", feed.run, store, clock),
        supervised("news_consumer", consume_news, store, clock),
        supervised("positions", lambda: every(cfg.exits.check_interval_s, lambda: safe(bot.check_positions)), store, clock),
        supervised("emergency", lambda: every(5, lambda: safe(bot.check_emergency)), store, clock),
        supervised("heartbeat", lambda: every(300, lambda: safe(heartbeat)), store, clock),
        supervised("resolutions", lambda: every(3600, lambda: safe(bot.check_resolutions)), store, clock),
    ]
    if mode == "live":
        tasks.append(supervised("reconcile", lambda: every(300, bot.execution.reconcile), store, clock))
    if cfg.news.rss.enabled and cfg.news.rss.feeds:
        tasks.append(supervised("rss", lambda: poll_rss(cfg.news, news_queue, clock), store, clock))
    if cfg.news.x.enabled:
        if secrets.x_bearer_token:
            tasks.append(supervised("x", lambda: run_x(cfg.news, secrets.x_bearer_token, news_queue, clock), store, clock))
        else:
            log.error("X active mais X_BEARER_TOKEN absent: source X desactivee")
    if cfg.discovery.enabled:
        tasks.append(supervised("discovery", lambda: every(cfg.discovery.refresh_interval_s,
                                                           lambda: safe(bot.refresh_markets)), store, clock))
    try:
        await asyncio.gather(*tasks)
    finally:
        # L'arret du processus n'annule rien sur le marche: voir docs/OPERATIONS.md.
        store.log_error(clock.now(), "shutdown", "arret du processus")
        if recorder:
            recorder.close()
        await rest.close()


def cmd_status(cfg: BotConfig) -> None:
    store = Store(cfg.storage.db_path)
    p = Portfolio(store)
    print(f"Mode configure: {cfg.mode.upper()}")
    print(f"Coupe-circuits: {p.halts() or 'aucun'} | jour: {store.kv_get('daily')}")
    print(f"Liquidites papier: {p.paper_usdc}")
    for pos in p.positions().values():
        print(f"  {pos.market_slug} {pos.outcome}: {pos.shares:.2f} parts @ {pos.avg_price:.4f} ({pos.status})")
    for r in store.query("SELECT client_order_id, status, error FROM orders WHERE status IN ('intent','unknown')"):
        print(f"  ORDRE INCERTAIN {r['client_order_id']}: {r['status']} {r['error']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot Polymarket pilote par l'actualite")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--mode", choices=["paper"], help="force PAPER quelle que soit la configuration")
    parser.add_argument("--confirm-live", action="store_true", help="une des 4 conditions du mode LIVE")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--halt", action="store_true", help="arret d'urgence: plus aucune nouvelle entree")
    parser.add_argument("--flatten", action="store_true", help="avec --halt: tenter de sortir de toutes les positions")
    parser.add_argument("--resume", action="store_true", help="lever les coupe-circuits (apres analyse!)")
    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.status:
        return cmd_status(cfg)
    if args.halt or args.resume:
        store, now = Store(cfg.storage.db_path), time.time()
        p = Portfolio(store)
        if args.halt:
            p.halt("manuel", "arret d'urgence demande en ligne de commande", now)
            store.kv_set("emergency_request", {"flatten": args.flatten, "ts": now}, now)
            print("Arret d'urgence enregistre: plus aucune entree."
                  + (" Sortie des positions demandee au bot en cours d'execution." if args.flatten else ""))
        else:
            p.clear_halts(now)
            print("Coupe-circuits leves.")
        return

    secrets = load_secrets()
    setup_logging(secrets.values())
    mode = effective_mode(cfg, args.mode)
    if mode == "replay":
        sys.exit("Le mode REPLAY se lance avec: python -m backtest.replay --help")
    if mode == "live":
        try:
            assert_live_allowed(cfg, secrets, args.confirm_live)
        except LiveGateError as e:
            log.error("%s", e)
            sys.exit(2)
    elif args.confirm_live:
        log.warning("--confirm-live ignore: le mode configure est %s", mode.upper())

    async def runner():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await run(cfg, mode)
        except asyncio.CancelledError:
            log.info("Arret demande.")

    asyncio.run(runner())


if __name__ == "__main__":
    main()
