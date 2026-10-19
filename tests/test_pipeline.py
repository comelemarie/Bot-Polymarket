"""Test de bout en bout en mode papier: WebSocket simule -> actu -> Claude simule -> ordre fictif."""
import json
from datetime import datetime, timezone

import pytest

from core.config import BotConfig, Secrets
from core.models import Assessment, Market, NewsItem
from main import Bot


def market() -> Market:
    return Market("fed", "Fed cut?", "rules", "", "c", "Y", "N", 0.01, False, 50_000, ["rate cut"])


def book_event(asset: str, bid: float, ask: float) -> str:
    return json.dumps([{
        "event_type": "book", "asset_id": asset,
        "bids": [{"price": str(bid), "size": "5000"}],
        "asks": [{"price": str(ask), "size": "5000"}],
    }])


@pytest.mark.asyncio
async def test_paper_trade_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = BotConfig.model_validate({"markets": [{"slug": "fed", "keywords": ["rate cut"]}],
                                    "paper_bankroll_usdc": 10_000})
    bot = Bot(cfg, Secrets(anthropic_api_key="test", x_bearer_token=None, polygon_private_key=None,
                           signature_type=1, funder_address=None), {"fed": market()})
    await bot.executor.start()

    captured = {}

    async def fake_assess(m, news, yes_book, no_book):
        captured["news"] = news
        return Assessment(True, 0.85, 0.9, False, "test", [])

    monkeypatch.setattr(bot.analyst, "assess", fake_assess)

    await bot.feed._handle(book_event("Y", 0.49, 0.50))
    await bot.feed._handle(book_event("N", 0.50, 0.51))
    await bot.news_queue.put(NewsItem("x:@t", "Fed announces rate cut", "", datetime.now(timezone.utc), "n1"))
    trig = bot.triggers.on_news(await bot.news_queue.get())
    assert trig == []  # pas encore de variation de prix

    # Nouveau format price_change: le YES monte de 5 points
    await bot.feed._handle(json.dumps({"event_type": "price_change", "market": "c", "price_changes": [
        {"asset_id": "Y", "price": "0.50", "size": "0", "side": "SELL"},
        {"asset_id": "Y", "price": "0.55", "size": "5000", "side": "SELL"},
        {"asset_id": "Y", "price": "0.54", "size": "5000", "side": "BUY"},
    ]}))
    for t in list(bot._background):
        await t

    assert captured["news"][0].id == "n1"
    pos = bot.state.positions.get("Y")
    assert pos is not None and pos.outcome == "YES"
    assert pos.cost_usdc <= 0.05 * 10_000 + 1e-6
    assert bot.state.paper_usdc == pytest.approx(10_000 - pos.cost_usdc)
    assert (tmp_path / "logs" / "decisions.jsonl").exists()
