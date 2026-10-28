"""Fabriques communes aux tests."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from bot import Bot, ReplayScheduler
from core.clock import ManualClock
from core.config import BotConfig, Secrets
from core.models import Assessment, Market, NewsItem
from core.state import Portfolio
from core.store import Store
from execution.paper_broker import PaperBroker

T0 = 1_800_000_000.0  # 2027-01-15, horodatage arbitraire fixe

SECRETS = Secrets(anthropic_api_key="sk-ant-test-0000000000", x_bearer_token=None, polygon_private_key=None,
                  signature_type=1, funder_address=None)


def market(slug="fed", yes="Y", no="N", keywords=("rate cut",), event_id="e1", tags=("economy",), **kw) -> Market:
    return Market(slug, f"{slug}?", "Regles: YES si ...", "2027-12-31", "c", yes, no, 0.01, False, 50_000,
                  list(keywords), event_id=event_id, tags=list(tags), **kw)


def book_msg(asset: str, bids: dict, asks: dict) -> str:
    return json.dumps([{"event_type": "book", "asset_id": asset,
                        "bids": [{"price": str(p), "size": str(s)} for p, s in bids.items()],
                        "asks": [{"price": str(p), "size": str(s)} for p, s in asks.items()]}])


def change_msg(asset: str, price: float, size: float, side: str, best_bid, best_ask) -> str:
    return json.dumps({"event_type": "price_change", "market": "c", "price_changes": [
        {"asset_id": asset, "price": str(price), "size": str(size), "side": side,
         "best_bid": str(best_bid), "best_ask": str(best_ask), "hash": "h"}]})


def news(text="Fed announces rate cut", nid="n1", t=T0, published_delay=10.0, source="rss:feeds.bbci.co.uk",
         url="https://bbc.example/a", reliability=0.8) -> NewsItem:
    return NewsItem(nid, source, text, "", url, datetime.fromtimestamp(t - published_delay, tz=timezone.utc),
                    datetime.fromtimestamp(t, tz=timezone.utc), reliability)


def assessment(p=0.85, lo=None, hi=None, uncertainty="low", confidence="high", **kw) -> Assessment:
    base = dict(market_id="fed", event_id="ev", p_yes=p, p_low=lo if lo is not None else p - 0.02,
                p_high=hi if hi is not None else min(1.0, p + 0.02), uncertainty=uncertainty, confidence=confidence,
                key_facts=["f"], supporting_evidence=[], contradicting_evidence=[], source_urls=[],
                published_at=[], market_resolution_understanding="ok", already_priced_in="no",
                reasoning_summary="r", abstain=False, abstain_reason="", analysis_id="an-test")
    base.update(kw)
    return Assessment(**base)


class FakeAnalyst:
    def __init__(self, result: Assessment | None = None, latency=5.0):
        self.result = result or assessment()
        self.latency = latency
        self.calls = []

    async def assess(self, trig, yes, no, corrob):
        self.calls.append(trig)
        a = Assessment(**{**self.result.__dict__})
        a.event_id, a.latency_s = trig.event_id, self.latency
        return a


def make_bot(tmp_path, cfg: dict | None = None, analyst=None, mode="replay", db="bot.db"):
    """Bot en mode deterministe (horloge manuelle, ordonnanceur de replay, courtier papier, pas de reseau)."""
    config = BotConfig.model_validate({"paper_bankroll_usdc": 10_000, **(cfg or {})})
    clock = ManualClock(T0)
    store = Store(str(tmp_path / db))
    broker = PaperBroker(Portfolio(store), config.paper_bankroll_usdc)
    sched = ReplayScheduler()
    bot = Bot(config, SECRETS, mode, clock, store, broker, analyst or FakeAnalyst(), None, sched)
    return bot, clock, sched


async def connect_and_book(bot, m: Market, yes_bids, yes_asks, no_bids, no_asks, t=T0):
    bot.state.on_connected([m.yes_token_id, m.no_token_id])
    for asset, b, a in ((m.yes_token_id, yes_bids, yes_asks), (m.no_token_id, no_bids, no_asks)):
        for ev in json.loads(book_msg(asset, b, a)):
            bot.state.apply_event(ev, t)


async def run_scheduled(sched: ReplayScheduler, clock: ManualClock, until: float):
    while sched.next_time() is not None and sched.next_time() <= until:
        at, fn, _ = sched.pop()
        clock.set(max(clock.now(), at))
        await fn()
