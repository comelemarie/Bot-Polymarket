"""Bout en bout: actualite -> detection -> analyse -> avantage -> risque -> ordre papier, tracable en base."""
import json

import pytest

from tests.helpers import T0, FakeAnalyst, assessment, connect_and_book, make_bot, market, news, run_scheduled


@pytest.mark.asyncio
async def test_news_to_paper_trade_is_fully_traceable(tmp_path):
    bot, clock, sched = make_bot(tmp_path, analyst=FakeAnalyst(assessment(p=0.85), latency=5))
    m = market()
    bot.set_markets({m.slug: m})
    await connect_and_book(bot, m, {0.49: 5000}, {0.50: 5000}, {0.49: 5000}, {0.51: 5000})
    await bot.on_news(news(t=T0))
    await run_scheduled(sched, clock, T0 + 60)

    pos = bot.portfolio.position("Y")
    assert pos is not None and pos.outcome == "YES"
    s = bot.store
    ev = s.one("SELECT * FROM events WHERE kind='news_only'")
    dec = s.one("SELECT * FROM decisions WHERE action='trade'")
    order = s.one("SELECT * FROM orders")
    assert dec["event_id"] == ev["id"] and order["decision_id"] == dec["id"]
    assert json.loads(ev["news_ids"]) == ["n1"]
    assert json.loads(dec["details"])["execution"] == "filled"
    assert s.one("SELECT COUNT(*) n FROM market_snapshots WHERE ref_id=?", (f"decide:{ev['id']}",))["n"] == 2
    assert order["created_at"] >= T0 + 5  # decision prise apres la latence du modele


@pytest.mark.asyncio
async def test_decision_uses_book_at_decision_time(tmp_path):
    bot, clock, sched = make_bot(tmp_path, analyst=FakeAnalyst(assessment(p=0.60, lo=0.58, hi=0.62), latency=8))
    m = market()
    bot.set_markets({m.slug: m})
    await connect_and_book(bot, m, {0.49: 5000}, {0.50: 5000}, {0.49: 5000}, {0.51: 5000})
    await bot.on_news(news(t=T0))
    # Pendant l'analyse, le marche reagit: l'ask YES passe a 0.56, l'avantage residuel disparait.
    await connect_and_book(bot, m, {0.55: 5000}, {0.56: 5000}, {0.43: 5000}, {0.45: 5000}, t=T0 + 4)
    await run_scheduled(sched, clock, T0 + 60)
    assert bot.portfolio.position("Y") is None
    assert "avantage net insuffisant" in bot.store.one("SELECT reason FROM decisions")["reason"]


@pytest.mark.asyncio
async def test_abstention_never_trades(tmp_path):
    bot, clock, sched = make_bot(tmp_path, analyst=FakeAnalyst(assessment(abstain=True, abstain_reason="ambigu")))
    m = market()
    bot.set_markets({m.slug: m})
    await connect_and_book(bot, m, {0.49: 5000}, {0.50: 5000}, {0.49: 5000}, {0.51: 5000})
    await bot.on_news(news(t=T0))
    await run_scheduled(sched, clock, T0 + 60)
    assert bot.store.one("SELECT COUNT(*) n FROM orders")["n"] == 0
    assert bot.store.one("SELECT reason FROM decisions")["reason"].startswith("abstention")


@pytest.mark.asyncio
async def test_emergency_halt_blocks_entries_and_flattens(tmp_path):
    bot, clock, sched = make_bot(tmp_path, analyst=FakeAnalyst(assessment(p=0.85)))
    m = market()
    bot.set_markets({m.slug: m})
    await connect_and_book(bot, m, {0.49: 5000}, {0.50: 5000}, {0.49: 5000}, {0.51: 5000})
    await bot.on_news(news(t=T0))
    await run_scheduled(sched, clock, T0 + 60)
    assert bot.portfolio.position("Y") is not None
    bot.portfolio.halt("manuel", "test", clock.now())
    bot.store.kv_set("emergency_request", {"flatten": True})
    await bot.check_emergency()
    assert bot.portfolio.position("Y") is None
    clock.set(T0 + 2000)
    await bot.on_news(news("new rate cut headline", "n2", t=T0 + 2000, url="https://b/2"))
    await run_scheduled(sched, clock, T0 + 2100)
    assert "coupe-circuit" in bot.store.one("SELECT reason FROM decisions ORDER BY created_at DESC")["reason"]


@pytest.mark.asyncio
async def test_stop_loss_triggers_on_quiet_but_synced_market(tmp_path):
    bot, clock, sched = make_bot(tmp_path, analyst=FakeAnalyst(assessment(p=0.85)))
    m = market()
    bot.set_markets({m.slug: m})
    await connect_and_book(bot, m, {0.49: 5000}, {0.50: 5000}, {0.49: 5000}, {0.51: 5000})
    await bot.on_news(news(t=T0))
    await run_scheduled(sched, clock, T0 + 60)
    await connect_and_book(bot, m, {0.30: 5000}, {0.32: 5000}, {0.67: 5000}, {0.69: 5000}, t=T0 + 100)
    clock.set(T0 + 900)  # 13 minutes sans aucune mise a jour du carnet
    await bot.check_positions()
    assert bot.portfolio.position("Y") is None
    assert bot.store.one("SELECT reason FROM exits")["reason"] == "stop_price"
