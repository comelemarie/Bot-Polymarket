from datetime import datetime, timezone

from core.clock import ManualClock
from core.config import NewsConfig, TriggerConfig
from core.store import Store
from ingestion.market_state import MarketState
from ingestion.news_filter import TriggerEngine
from ingestion.news_normalizer import NewsDeduplicator, fingerprint, normalize_title
from ingestion.rss_feed import entry_to_item
from tests.helpers import T0, book_msg, change_msg, market, news
from core.config import MarketDataConfig
import json


def test_title_normalization_removes_publisher_suffix():
    assert normalize_title("Fed cuts rates by 25bp - Reuters") == normalize_title("Fed cuts rates by 25bp")
    assert fingerprint("Fed cuts rates &amp; more | BBC News") == fingerprint("Fed cuts rates & more")


def test_rss_item_without_date_is_not_given_one():
    received = datetime.fromtimestamp(T0, tz=timezone.utc)
    item = entry_to_item({"id": "g1", "title": "<b>Title</b>", "summary": "<p>Body</p>", "link": "u"},
                         "example.com", NewsConfig(), received)
    assert item.published_at is None and item.received_at == received
    assert (item.title, item.content) == ("Title", "Body")
    assert entry_to_item({"title": "no id"}, "example.com", NewsConfig(), received) is None


def test_cross_source_dedup_and_restart(tmp_path):
    clock = ManualClock(T0)
    store = Store(str(tmp_path / "n.db"))
    d = NewsDeduplicator(store, NewsConfig(), clock)
    first = news("Fed cuts interest rates by half point in surprise move", "rss:bbc1", url="https://a/1")
    assert d.process(first) is not None
    # Meme titre republie par Google News avec le suffixe de l'editeur
    assert d.process(news("Fed cuts interest rates by half point in surprise move - BBC", "rss:g1",
                          source="rss:news.google.com", url="https://g/1")) is None
    # Reformulation legere
    assert d.process(news("Fed cuts interest rates by half point in a surprise move", "rss:g2", url="https://g/2")) is None
    assert d.corroborations("rss:bbc1") == 2
    # Redemarrage: l'etat est relu depuis la base
    d2 = NewsDeduplicator(store, NewsConfig(), clock)
    assert d2.process(news("Fed cuts interest rates by half point in surprise move", "x:9", url="https://x/9")) is None
    assert d2.process(first) is None  # meme identifiant
    assert d2.process(news("Completely different headline about oil exports", "rss:z", url="https://z/1")) is not None
    # Meme URL (parametres de suivi en plus) = meme article
    assert d2.process(news("Other wording entirely", "rss:u", url="https://z/1?utm_source=feed")) is None


def setup_filter(tmp_path, **cfg):
    clock = ManualClock(T0)
    store = Store(str(tmp_path / "f.db"))
    st = MarketState(MarketDataConfig(), 300, 0.08)
    m = market()
    st.set_markets({m.slug: m})
    st.on_connected(["Y", "N"])
    for raw in (book_msg("Y", {0.49: 1000}, {0.51: 1000}), book_msg("N", {0.49: 1000}, {0.51: 1000})):
        for ev in json.loads(raw):
            st.apply_event(ev, T0)
    eng = TriggerEngine(TriggerConfig(**cfg), st, store, clock)
    eng.set_markets(st.markets)
    return eng, st, store, clock


def move_yes(st, t, bid, ask):
    st.apply_event(json.loads(change_msg("Y", ask, 1000, "SELL", 0.49, ask))["price_changes"] and
                   {"event_type": "book", "asset_id": "Y", "bids": [{"price": str(bid), "size": "1000"}],
                    "asks": [{"price": str(ask), "size": "1000"}]}, t)


def test_path1_news_without_move_triggers_quickly(tmp_path):
    eng, st, store, clock = setup_filter(tmp_path)
    trig = eng.on_news(news(t=T0))
    assert [t.kind for t in trig] == ["news_only"]
    assert store.one("SELECT kind FROM events")["kind"] == "news_only"


def test_path2_news_with_move(tmp_path):
    eng, st, store, clock = setup_filter(tmp_path)
    move_yes(st, T0 + 10, 0.55, 0.57)
    clock.set(T0 + 20)
    trig = eng.on_news(news(t=T0 + 20))
    assert [t.kind for t in trig] == ["news_with_move"] and trig[0].price_move > 0.05


def test_path2_deferred_when_news_only_disabled(tmp_path):
    eng, st, store, clock = setup_filter(tmp_path, news_only_enabled=False)
    assert eng.on_news(news(t=T0)) == []
    move_yes(st, T0 + 30, 0.55, 0.57)
    clock.set(T0 + 30)
    assert [t.kind for t in eng.on_price({"fed"})] == ["news_with_move"]


def test_path3_anomaly_never_triggers_and_sets_watch(tmp_path):
    eng, st, store, clock = setup_filter(tmp_path)
    move_yes(st, T0 + 10, 0.60, 0.62)
    clock.set(T0 + 10)
    assert eng.on_price({"fed"}) == []
    assert store.one("SELECT kind FROM events")["kind"] == "anomaly"
    # Une actualite arrivant pendant la surveillance passe en voie 2
    clock.set(T0 + 400)  # le mouvement est sorti de la fenetre
    trig = eng.on_news(news(t=T0 + 400))
    assert trig[0].kind == "news_with_move"


def test_filters_cooldown_persisted_across_restart(tmp_path):
    eng, st, store, clock = setup_filter(tmp_path)
    assert len(eng.on_news(news(t=T0))) == 1
    eng2 = TriggerEngine(TriggerConfig(), st, store, clock)  # redemarrage
    eng2.set_markets(st.markets)
    assert eng2.on_news(news("another rate cut story", "n2", t=T0 + 60)) == []
    assert store.one("SELECT details FROM events WHERE kind='filtered'")["details"] == '{"reason": "cooldown"}'


def test_filters_reliability_age_and_gates(tmp_path):
    eng, st, store, clock = setup_filter(tmp_path)
    assert eng.on_news(news(reliability=0.1)) == []
    assert eng.on_news(news(nid="old", published_delay=7200)) == []
    st.on_disconnected(["Y"])
    assert eng.on_news(news(nid="n3")) == []
    assert "connexion" in store.one("SELECT details FROM events WHERE kind='filtered'")["details"]
