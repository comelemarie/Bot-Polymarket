from datetime import datetime, timedelta, timezone

from core.config import TriggerConfig
from core.models import Market, NewsItem
from ingestion.news_filter import TriggerEngine
from ingestion.polymarket_ws import PriceHistory


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


def make_engine(move: dict, **cfg):
    market = Market("fed", "Fed cut?", "rules", "", "c", "Y", "N", 0.01, False, 0, [r"\bfed\b", "rate cut"])
    clock = Clock()
    engine = TriggerEngine({"fed": market}, TriggerConfig(**cfg), lambda slug: move[slug], clock)
    return engine, clock


def news(text: str, nid: str = "1", age_s: float = 0) -> NewsItem:
    return NewsItem("x:@test", text, "", datetime.now(timezone.utc) - timedelta(seconds=age_s), nid)


def test_requires_keyword_and_price_move():
    move = {"fed": 0.0}
    engine, clock = make_engine(move)
    assert engine.on_news(news("Unrelated football result")) == []
    assert engine.on_news(news("The Fed signals a RATE CUT", "2")) == []  # mot-cle mais pas de mouvement
    move["fed"] = 0.05
    trig = engine.on_price("fed")
    assert trig is not None and len(trig.news) == 1


def test_news_window_expires():
    move = {"fed": 0.0}
    engine, clock = make_engine(move, news_window_s=60)
    engine.on_news(news("fed decision"))
    clock.t += 120
    move["fed"] = 0.05
    assert engine.on_price("fed") is None


def test_cooldown_and_dedup():
    move = {"fed": 0.05}
    engine, clock = make_engine(move, market_cooldown_s=600, news_window_s=900)
    assert len(engine.on_news(news("fed hikes", "a"))) == 1
    assert engine.on_news(news("fed hikes", "a")) == []  # doublon
    assert engine.on_news(news("fed again", "b")) == []  # cooldown
    clock.t += 601
    trig = engine.on_price("fed")  # cooldown ecoule, l'actu "b" est encore dans la fenetre
    assert trig is not None and trig.news[0].id == "b"


def test_stale_news_ignored():
    engine, _ = make_engine({"fed": 0.05})
    assert engine.on_news(news("fed", age_s=7200)) == []


def test_price_history_move():
    h = PriceHistory(window_s=60)
    h.add(0.50, ts=0)
    h.add(0.55, ts=30)
    assert abs(h.move() - 0.05) < 1e-9
    h.add(0.56, ts=100)  # 0.50 et 0.55 sortent de la fenetre
    assert h.move() == 0.0
