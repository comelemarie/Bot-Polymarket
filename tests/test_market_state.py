import json

import pytest

from core.config import MarketDataConfig
from ingestion.market_state import MarketState
from ingestion.polymarket_ws import PolymarketFeed
from tests.helpers import T0, book_msg, change_msg, market


def make_state():
    st = MarketState(MarketDataConfig(), price_window_s=300, max_spread_for_move=0.05)
    m = market()
    st.set_markets({m.slug: m})
    return st, m


def apply(st, raw, t=T0):
    msgs = json.loads(raw)
    out_t, out_d = set(), set()
    for ev in msgs if isinstance(msgs, list) else [msgs]:
        t_, d_ = st.apply_event(ev, t)
        out_t |= t_
        out_d |= d_
    return out_t, out_d


def synced_state():
    st, m = make_state()
    st.on_connected(["Y", "N"])
    apply(st, book_msg("Y", {0.49: 100}, {0.51: 100}))
    apply(st, book_msg("N", {0.49: 100}, {0.51: 100}))
    return st, m


def test_snapshot_then_consistent_change():
    st, m = synced_state()
    assert st.tradable(m, T0) == (True, "")
    _, desynced = apply(st, change_msg("Y", 0.50, 30, "BUY", best_bid=0.50, best_ask=0.51))
    assert not desynced and st.books["Y"].best_bid == 0.50


def test_lost_message_detected_by_server_best_prices():
    st, m = synced_state()
    # Le serveur annonce un best_ask 0.52: un message (retrait du niveau 0.51) a ete perdu.
    _, desynced = apply(st, change_msg("Y", 0.50, 30, "BUY", best_bid=0.50, best_ask=0.52))
    assert desynced == {"Y"}
    ok, why = st.tradable(m, T0)
    assert not ok and "non synchronise" in why


def test_duplicate_message_is_idempotent():
    st, m = synced_state()
    msg = change_msg("Y", 0.50, 30, "BUY", best_bid=0.50, best_ask=0.51)
    apply(st, msg)
    _, desynced = apply(st, msg)  # tailles absolues: rejouer le meme message ne change rien
    assert not desynced and st.books["Y"].bids == {0.49: 100, 0.50: 30}


def test_crossed_and_out_of_bounds_books_are_rejected():
    st, m = make_state()
    st.on_connected(["Y", "N"])
    apply(st, book_msg("Y", {0.55: 10}, {0.50: 10}))
    assert not st.books["Y"].synced and "croise" in st.books["Y"].desync_reason
    apply(st, book_msg("N", {1.5: 10}, {0.6: 10}))
    assert not st.books["N"].synced


def test_disconnect_and_reconnect_require_new_snapshot():
    st, m = synced_state()
    st.on_disconnected(["Y", "N"])
    assert not st.tradable(m, T0)[0]
    st.on_connected(["Y", "N"])
    assert not st.tradable(m, T0)[0]  # connexion revenue mais pas encore d'instantane
    apply(st, change_msg("Y", 0.50, 30, "BUY", 0.50, 0.51))
    assert not st.books["Y"].synced  # une mise a jour sans instantane ne resynchronise pas
    apply(st, book_msg("Y", {0.49: 100}, {0.51: 100}))
    apply(st, book_msg("N", {0.49: 100}, {0.51: 100}))
    assert st.tradable(m, T0)[0]


def test_quiet_market_stays_tradable_but_stale_snapshot_does_not():
    st, m = synced_state()
    assert st.tradable(m, T0 + 600)[0]  # 10 min sans mise a jour: toujours coherent
    assert not st.tradable(m, T0 + 2 * MarketDataConfig().resync_interval_s + 1)[0]


def test_closed_market_and_tick_change():
    st, m = synced_state()
    apply(st, json.dumps({"event_type": "tick_size_change", "asset_id": "Y", "new_tick_size": "0.001"}))
    assert m.tick_size == 0.001
    apply(st, json.dumps({"event_type": "market_resolved", "asset_id": "Y"}))
    ok, why = st.tradable(m, T0)
    assert not ok and "ferme" in why


def test_price_history_ignores_wide_spread_points():
    st, m = synced_state()
    apply(st, change_msg("Y", 0.51, 0, "SELL", best_bid=0.49, best_ask=None))  # ask retire
    apply(st, book_msg("Y", {0.30: 10}, {0.90: 10}), t=T0 + 1)  # spread 0.6: mid non negociable
    assert st.price_move("fed", T0 + 1) == 0.0


@pytest.mark.asyncio
async def test_feed_records_and_reports_desync():
    st, m = synced_state()
    recorded = []
    feed = PolymarketFeed(st, MarketDataConfig(), clock=None, recorder=lambda k, p, t: recorded.append(k))
    assert await feed.handle_raw("PONG", T0) == set()
    desynced = await feed.handle_raw(change_msg("Y", 0.50, 1, "BUY", 0.50, 0.99), T0)
    assert desynced == {"Y"} and recorded == ["ws"]
    assert await feed.handle_raw("pas du json", T0) == set()  # message invalide ignore sans planter
