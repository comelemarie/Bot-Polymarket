import pytest

from analytics.performance_tracker import max_drawdown, report, wilson
from tests.helpers import T0, FakeAnalyst, assessment, connect_and_book, make_bot, market, news, run_scheduled


def test_statistics_helpers():
    assert max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(0.25)
    lo, hi = wilson(5, 10)
    assert lo < 0.5 < hi and hi - lo > 0.5  # 10 observations: intervalle tres large


@pytest.mark.asyncio
async def test_report_brier_pnl_and_warnings(tmp_path):
    bot, clock, sched = make_bot(tmp_path, analyst=FakeAnalyst(assessment(p=0.85)))
    m = market()
    bot.set_markets({m.slug: m})
    await connect_and_book(bot, m, {0.49: 5000}, {0.50: 5000}, {0.49: 5000}, {0.51: 5000})
    await bot.on_news(news(t=T0))
    await run_scheduled(sched, clock, T0 + 60)
    s = bot.store
    # Analyse enregistree comme le ferait ClaudeAnalyst, puis resolution YES et reglement de la position
    s.insert("analyses", {"id": "an-test", "event_id": s.one("SELECT id FROM events")["id"], "market_slug": "fed",
                          "created_at": T0, "request_hash": "h", "valid": 1, "abstain": 0, "p_yes": 0.85})
    s.insert("outcomes", {"market_slug": "fed", "resolved_at": T0 + 86400, "winner": "YES"})
    pos = bot.portfolio.position("Y")
    bot.portfolio.reduce(token_id="Y", shares=pos.shares, proceeds_usdc=pos.shares, fee_usdc=0, ts=T0 + 86400,
                         reason="resolution")
    rep = report(s)
    assert rep["brier"]["modele"] == pytest.approx((0.85 - 1) ** 2) and rep["brier"]["n"] == 1
    assert rep["brier"]["prix_du_marche_au_declenchement"] == pytest.approx((0.495 - 1) ** 2)
    assert rep["pnl"]["realise_net_usdc"] > 0
    assert rep["execution"]["ordres_entree"] == {"filled": 1}
    assert any("aucune conclusion de rentabilite" in w for w in rep["avertissements"])
    assert rep["rendement_par_trade_solde"]["voie"]["news_only"]["n"] == 1
