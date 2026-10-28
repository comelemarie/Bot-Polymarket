from dataclasses import asdict

import pytest

from backtest.replay import run_replay
from core.config import BotConfig
from core.store import Store
from ingestion.recorder import Recorder, read_recordings
from tests.helpers import T0, assessment, book_msg, market, news


def record_session(root, with_late_move=False):
    rec = Recorder(root)
    m = market()
    rec.markets({m.slug: m}, T0)
    rec("ws_connect", ["Y", "N"], T0 + 1)
    rec("ws", book_msg("Y", {0.49: 5000}, {0.50: 5000}), T0 + 2)
    rec("ws", book_msg("N", {0.49: 5000}, {0.51: 5000}), T0 + 2)
    rec.news(news(t=T0 + 10))
    if with_late_move:  # le marche reagit pendant que Claude reflechit
        rec("ws", book_msg("Y", {0.62: 5000}, {0.63: 5000}), T0 + 12)
        rec("ws", book_msg("N", {0.36: 5000}, {0.38: 5000}), T0 + 12)
    rec.close()


def source_db_with_analysis(path, latency=6.0):
    """Base de collecte: l'evenement et l'analyse tels que produits en direct."""
    s = Store(str(path))
    s.insert("events", {"id": "ev-live", "kind": "news_only", "market_slug": "fed", "created_at": T0 + 10,
                        "news_ids": ["n1"], "price_move": 0.0, "details": {}})
    a = assessment(p=0.85, event_id="ev-live", analysis_id="an-live")
    s.insert("analyses", {"id": "an-live", "event_id": "ev-live", "market_slug": "fed", "created_at": T0 + 10,
                          "request_hash": "h", "valid": 1, "abstain": 0, "assessment": asdict(a),
                          "latency_s": latency, "cost_usd": 0.02, "request_id": "req"})
    s.close()


def test_recordings_are_read_in_order_and_survive_truncation(tmp_path):
    record_session(tmp_path / "rec")
    import gzip

    path = next((tmp_path / "rec").glob("*/*.jsonl.gz"))
    with gzip.open(path, "at") as fh:
        fh.write('{"t": 1, "k": "ws", "p": tronque')  # arret brutal en pleine ecriture
    rows = list(read_recordings(tmp_path / "rec", T0 - 10, T0 + 100))
    assert [r["k"] for r in rows] == ["markets", "ws_connect", "ws", "ws", "news"]


@pytest.mark.asyncio
async def test_replay_uses_recorded_analysis_and_latency(tmp_path):
    record_session(tmp_path / "rec")
    source_db_with_analysis(tmp_path / "live.db")
    rep = await run_replay(BotConfig(paper_bankroll_usdc=10_000), tmp_path / "rec", T0 - 10, T0 + 600,
                           str(tmp_path / "out.db"), str(tmp_path / "live.db"))
    assert rep["events"] == {"news_only": 1} and rep["orders"] == {"filled": 1}
    out = Store(str(tmp_path / "out.db"))
    order = out.one("SELECT created_at FROM orders")
    assert order["created_at"] >= T0 + 10 + 6  # pas de decision avant la fin de l'analyse
    assert out.one("SELECT COUNT(*) n FROM analyses")["n"] == 0  # aucun appel au modele en replay


@pytest.mark.asyncio
async def test_replay_no_lookahead_market_moves_during_analysis(tmp_path):
    record_session(tmp_path / "rec", with_late_move=True)
    source_db_with_analysis(tmp_path / "live.db")
    rep = await run_replay(BotConfig(paper_bankroll_usdc=10_000), tmp_path / "rec", T0 - 10, T0 + 600,
                           str(tmp_path / "out.db"), str(tmp_path / "live.db"))
    # Decision a T0+10+6+1.5: le carnet utilise est celui d'APRES la reaction du marche (ask 0.63),
    # pas celui du declenchement (ask 0.50). L'avantage residuel (p prudente 0.82) suffit encore.
    assert rep["orders"] == {"filled": 1}
    order = Store(str(tmp_path / "out.db")).one("SELECT avg_price, observed_best_price FROM orders")
    assert order["observed_best_price"] == 0.63 and order["avg_price"] > 0.63  # frais de repli inclus


@pytest.mark.asyncio
async def test_replay_without_recorded_analysis_abstains(tmp_path):
    record_session(tmp_path / "rec")
    rep = await run_replay(BotConfig(), tmp_path / "rec", T0 - 10, T0 + 600, str(tmp_path / "out.db"), None)
    out = Store(str(tmp_path / "out.db"))
    assert rep["orders"] == {}
    assert "aucune analyse enregistree" in out.one("SELECT reason FROM decisions")["reason"]
