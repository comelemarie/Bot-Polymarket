"""Replay chronologique des flux enregistres (mode REPLAY).

  python -m backtest.replay --from 2026-11-01 --to 2026-11-08 [--analyses-db state/bot.db] [--out state/replay.db]

Principes (absence de fuite du futur):
- Les evenements sont rejoues dans l'ordre de leur horodatage de RECEPTION reel.
- Une decision est prise a: declenchement + latence du modele + latence de traitement + latence d'execution,
  avec le carnet tel qu'il est A CET INSTANT dans le replay (pas celui du declenchement).
- Les analyses viennent de la base de collecte (ce que Claude a repondu a l'epoque). Rappeler le modele
  aujourd'hui sur des evenements passes introduirait une fuite: il peut connaitre l'issue (date de coupure).
  Un declenchement sans analyse enregistree donne une abstention, comptee comme telle.
- Les marches sont ceux connus a l'epoque (enregistres), pas la liste actuelle: pas de biais de survivance.
- Les issues des marches ne servent qu'a l'evaluation (analytics), jamais aux decisions.
Limites: pas d'impact de nos ordres sur le carnet, pas de file d'attente, frais au taux enregistre ou de repli.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from bot import Bot, ReplayScheduler
from core.clock import ManualClock
from core.config import BotConfig, Secrets, load_config
from core.models import Assessment, NewsItem
from core.state import Portfolio
from core.store import Store
from execution.paper_broker import PaperBroker
from ingestion.market_state import BookState
from ingestion.news_filter import Trigger
from ingestion.polymarket_ws import parse_raw
from ingestion.recorder import market_from_record, news_from_record, read_recordings

log = logging.getLogger("replay")


class ReplayAnalyst:
    """Rejoue les analyses enregistrees en direct (aucun appel au modele)."""

    def __init__(self, source: Store | None, default_latency_s: float):
        self.source = source
        self.default_latency_s = default_latency_s

    async def assess(self, trig: Trigger, yes: BookState, no: BookState, corroborations: dict[str, int]) -> Assessment:
        row = None
        if self.source is not None:
            row = self.source.one(
                "SELECT a.* FROM analyses a JOIN events e ON a.event_id = e.id "
                "WHERE a.market_slug=? AND e.news_ids=? AND a.assessment IS NOT NULL ORDER BY a.created_at LIMIT 1",
                (trig.market.slug, json.dumps([n.id for n in trig.news])))
        if row is None:
            a = Assessment.abstention(trig.market.slug, trig.event_id, "replay: aucune analyse enregistree")
            a.latency_s = self.default_latency_s
            return a
        a = Assessment(**json.loads(row["assessment"]))
        a.event_id = trig.event_id
        a.latency_s = row["latency_s"] or self.default_latency_s
        a.cost_usd = 0.0
        return a


async def run_replay(cfg: BotConfig, recordings: str | Path, start: float, end: float, out_db: str,
                     analyses_db: str | None) -> dict:
    cfg = cfg.model_copy(update={"mode": "replay"})
    clock = ManualClock(start)
    store = Store(out_db)
    portfolio = Portfolio(store)
    broker = PaperBroker(portfolio, cfg.paper_bankroll_usdc)
    source = Store(analyses_db) if analyses_db and Path(analyses_db).exists() else None
    analyst = ReplayAnalyst(source, cfg.replay.default_model_latency_s)
    scheduler = ReplayScheduler()
    secrets = Secrets(anthropic_api_key=None, x_bearer_token=None, polygon_private_key=None, signature_type=0,
                      funder_address=None)
    bot = Bot(cfg, secrets, "replay", clock, store, broker, analyst, None, scheduler)

    async def periodic_checks():
        await bot.check_positions()
        scheduler.schedule(clock.now() + cfg.exits.check_interval_s, periodic_checks, "positions")

    scheduler.schedule(start + cfg.exits.check_interval_s, periodic_checks, "positions")

    async def drain_until(t: float) -> None:
        while scheduler.next_time() is not None and scheduler.next_time() <= t:
            at, fn, _ = scheduler.pop()
            clock.set(max(clock.now(), at))
            await fn()

    counts: dict[str, int] = {}
    for row in read_recordings(recordings, start, end):
        t, kind, payload = row["t"], row["k"], row["p"]
        await drain_until(t)
        clock.set(max(clock.now(), t))
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "markets":
            bot.set_markets({m.slug: m for m in (market_from_record(d) for d in payload)})
        elif kind == "ws_connect":
            bot.state.on_connected(payload)
        elif kind == "ws_disconnect":
            bot.state.on_disconnected(payload)
        elif kind == "fee":
            bot.recorded_fees[payload["token_id"]] = payload["rate"]
        elif kind == "ws":
            touched: set[str] = set()
            for ev in parse_raw(payload) or []:
                tch, _ = bot.state.apply_event(ev, t)
                touched |= tch
            if touched:
                await bot.on_book_update(touched, t)
        elif kind == "news":
            item: NewsItem = news_from_record(payload)
            await bot.on_news(item)
    await drain_until(end)
    clock.set(max(clock.now(), end))

    usdc = await broker.balance()
    report = {
        "window": [datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
                   datetime.fromtimestamp(end, tz=timezone.utc).isoformat()],
        "records": counts,
        "events": {r["kind"]: r["n"] for r in store.query("SELECT kind, COUNT(*) n FROM events GROUP BY kind")},
        "decisions": {r["action"]: r["n"] for r in store.query("SELECT action, COUNT(*) n FROM decisions GROUP BY action")},
        "orders": {r["status"]: r["n"] for r in store.query("SELECT status, COUNT(*) n FROM orders GROUP BY status")},
        "realized_pnl_usdc": store.one("SELECT COALESCE(SUM(pnl_usdc),0) v FROM exits")["v"],
        "open_positions_mark_usdc": bot.mark_value(),
        "open_positions_cost_usdc": portfolio.total_exposure(),
        "final_usdc": usdc,
        "equity_end_usdc": (usdc or 0) + bot.mark_value(),
        "analyses_source": analyses_db if source else None,
        "output_db": out_db,
    }
    store.close()
    if source:
        source.close()
    return report


def _ts(s: str) -> float:
    dt = datetime.fromisoformat(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def main() -> None:
    p = argparse.ArgumentParser(description="Replay chronologique des flux enregistres")
    p.add_argument("--config", default="config.json")
    p.add_argument("--from", dest="start", required=True, help="debut (ISO, UTC par defaut)")
    p.add_argument("--to", dest="end", required=True, help="fin (ISO, UTC par defaut)")
    p.add_argument("--recordings", default=None, help="dossier des enregistrements (defaut: storage.record_dir)")
    p.add_argument("--analyses-db", default=None, help="base de collecte contenant les analyses (defaut: storage.db_path)")
    p.add_argument("--out", default=None, help="base de sortie du replay (doit etre nouvelle)")
    args = p.parse_args()
    cfg = load_config(args.config)
    out = args.out or f"state/replay-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.db"
    if Path(out).exists():
        raise SystemExit(f"{out} existe deja: choisis une base de sortie neuve (--out)")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    report = asyncio.run(run_replay(cfg, args.recordings or cfg.storage.record_dir, _ts(args.start), _ts(args.end),
                                    out, args.analyses_db or cfg.storage.db_path))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nAnalyse detaillee: python -m analytics.performance_tracker --db {out}")


if __name__ == "__main__":
    main()
