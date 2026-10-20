import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from core.config import BotConfig, DiscoveryConfig, Secrets
from core.models import Market
from ingestion.market_discovery import (
    KeywordGenerator,
    heuristic_keywords,
    phrase_to_regex,
    select_markets,
)
from ingestion.polymarket_ws import PolymarketFeed

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def gamma_market(slug, liquidity=50_000, volume=20_000, days=30, question="Will X happen?", **kw):
    return {
        "slug": slug, "question": question, "description": "rules", "active": True, "closed": False,
        "liquidityNum": liquidity, "volume24hr": volume,
        "endDate": (NOW + timedelta(days=days)).isoformat().replace("+00:00", "Z"),
        "outcomes": '["Yes", "No"]', "clobTokenIds": f'["{slug}-y", "{slug}-n"]',
        "conditionId": "c", "orderPriceMinTickSize": 0.01, **kw,
    }


def event(markets, tags=("politics",), title="Event"):
    return {"title": title, "tags": [{"slug": t} for t in tags], "markets": markets}


def test_select_markets_filters():
    events = [
        event([gamma_market("ok", volume=30_000), gamma_market("illiquid", liquidity=500)]),
        event([gamma_market("too-soon", days=0.5), gamma_market("too-far", days=400)]),
        event([gamma_market("sport")], tags=("sports", "nba")),
        event([gamma_market("btc", question="Bitcoin Up or Down - 3PM ET")], tags=("crypto",)),
        event([gamma_market("quiet", volume=100), gamma_market("closed", closed=True)]),
        event([gamma_market("ok2", volume=90_000)], tags=("economy",)),
    ]
    cfg = DiscoveryConfig(exclude_tags=["sports"], exclude_question_patterns=["up or down"])
    rows = select_markets(events, cfg, NOW)
    assert [r["slug"] for r in rows] == ["ok2", "ok"]  # tri par volume 24h decroissant
    assert rows[0]["_event_title"] == "Event"

    assert [r["slug"] for r in select_markets(events, DiscoveryConfig(include_tags=["economy"]), NOW)] == ["ok2"]
    assert len(select_markets(events, DiscoveryConfig(max_markets=1), NOW)) == 1


def test_phrase_regex_and_heuristic():
    pat = re.compile(phrase_to_regex("Fed rate cut"), re.IGNORECASE)
    assert pat.search("BREAKING: fed rate cut announced")
    assert not pat.search("Federal rate cuts")
    assert re.search(phrase_to_regex("$BTC"), "price of $BTC jumps")
    assert heuristic_keywords("Will Trump meet Putin before December 2026?") == ["Trump", "Putin"]


@pytest.mark.asyncio
async def test_keyword_generator_caches(tmp_path, monkeypatch):
    gen = KeywordGenerator("test", "claude-haiku-5-5", cache_path=tmp_path / "kw.json")
    calls = []

    async def fake_generate(batch):
        calls.append([r["slug"] for r in batch])
        return {r["slug"]: [f"kw {r['slug']}"] for r in batch if r["slug"] != "fails"}

    monkeypatch.setattr(gen, "_generate", fake_generate)
    rows = [gamma_market("a"), gamma_market("fails", question="Will Macron resign?")]
    out = await gen.keywords_for(rows)
    assert out == {"a": ["kw a"], "fails": ["Macron"]}  # secours heuristique
    assert json.loads((tmp_path / "kw.json").read_text()) == {"a": ["kw a"]}

    await gen.keywords_for(rows)
    assert calls == [["a", "fails"], ["fails"]]  # "a" vient du cache, "fails" est retente


def test_feed_set_markets_resubscribes():
    m1 = Market("a", "", "", "", "", "a-y", "a-n", 0.01, False, 0, [])
    m2 = Market("b", "", "", "", "", "b-y", "b-n", 0.01, False, 0, [])
    feed = PolymarketFeed({"a": m1}, 60)
    feed.books["a-y"].bids = {0.5: 10}
    feed._resubscribe.clear()
    feed.set_markets({"a": m1, "b": m2})
    assert feed._resubscribe.is_set()
    assert feed.books["a-y"].bids == {0.5: 10}  # carnet existant conserve
    assert set(feed.history) == {"a", "b"}
    feed._resubscribe.clear()
    feed.set_markets({"a": m1, "b": m2})
    assert not feed._resubscribe.is_set()  # rien n'a change


@pytest.mark.asyncio
async def test_refresh_keeps_markets_with_open_positions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from main import Bot

    cfg = BotConfig.model_validate({"discovery": {"enabled": True}})
    bot = Bot(cfg, Secrets(anthropic_api_key="t", x_bearer_token=None, polygon_private_key=None,
                           signature_type=1, funder_address=None), {})
    held = Market("held", "", "", "", "", "h-y", "h-n", 0.01, False, 0, [])
    new = Market("new", "", "", "", "", "n-y", "n-n", 0.01, False, 0, ["x"])
    bot.set_markets({"held": held})
    bot.state.add_fill("held", "h-y", "YES", 0.5, 10)

    async def fake_discover(cfg, gen):
        return {"new": new}

    monkeypatch.setattr("main.discover_markets", fake_discover)
    await bot.refresh_markets()
    assert set(bot.markets) == {"held", "new"}
    assert set(bot.triggers.markets) == {"held", "new"}
    assert "n-y" in bot.feed.books
