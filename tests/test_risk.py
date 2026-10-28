import pytest

from core.config import EdgeConfig, ExitConfig, RiskConfig
from core.state import DailyStats, Portfolio, Position
from core.store import Store
from execution.risk_manager import RiskContext, RiskManager
from ingestion.market_state import BookState
from tests.helpers import T0, assessment, market


def book(asset, bid, ask, size=10_000):
    b = BookState(asset)
    b.apply_snapshot([{"price": bid, "size": size}], [{"price": ask, "size": size}], T0)
    return b


@pytest.fixture
def rm(tmp_path):
    p = Portfolio(Store(str(tmp_path / "r.db")))
    return RiskManager(RiskConfig(), EdgeConfig(), ExitConfig(), p), p


def ctx(a=None, m=None, yes=(0.49, 0.50), no=(0.50, 0.51), usdc=10_000.0, equity=10_000.0, daily=None, peak=None,
        halts=None, tradable=(True, ""), pending=False, fee=0.0):
    m = m or market()
    return RiskContext(m, a or assessment(), "ev", book("Y", *yes), book("N", *no), {"Y": fee, "N": fee}, tradable,
                       usdc, equity, daily or DailyStats("2027-01-15", equity), peak if peak is not None else equity,
                       halts or {}, pending, T0)


def test_trade_respects_caps_and_reports_binding_constraint(rm):
    r, _ = rm
    v = r.evaluate(ctx())
    d = v.decision
    assert d is not None and d.outcome == "YES"
    # Kelly 25% donnerait des milliers d'USDC: le plafond par position (100 USDC) s'applique.
    assert d.expected_cost_usdc <= 100 + 1e-6 and d.binding_constraint == "max_position"
    assert d.net_edge_per_share >= EdgeConfig().min_net_edge
    assert set(v.details["caps"]) >= {"kelly", "max_par_marche", "max_par_evenement", "max_total", "max_theme:economy"}


def test_fees_can_remove_the_edge(rm):
    r, _ = rm
    a = assessment(p=0.60, lo=0.58, hi=0.62)  # p prudente 0.57 vs ask 0.50
    assert r.evaluate(ctx(a, fee=0.0)).decision is not None
    v = r.evaluate(ctx(assessment(p=0.56, lo=0.555, hi=0.565), yes=(0.49, 0.50), fee=0.10))
    assert v.decision is None and "avantage net insuffisant" in v.reason


@pytest.mark.parametrize("kw,expected", [
    ({"halts": {"manuel": "x"}}, "coupe-circuit"),
    ({"tradable": (False, "carnet non synchronise")}, "non negociable"),
    ({"pending": True}, "incertain"),
    ({"usdc": None}, "inconnus"),
    ({"a": assessment(abstain=True, abstain_reason="ambigu")}, "abstention"),
    ({"a": assessment(confidence="low")}, "qualite"),
    ({"a": assessment(already_priced_in="fully")}, "integree"),
    ({"a": assessment(uncertainty="high")}, "incertitude"),
    ({"usdc": 104.0}, "minimum"),
])
def test_refusals(rm, kw, expected):
    r, _ = rm
    v = r.evaluate(ctx(**kw))
    assert v.decision is None and expected in v.reason, v.reason


def test_uncertainty_reduces_size(rm):
    r, _ = rm
    cfg_big = RiskConfig(max_position_usdc=1e9)
    r.cfg = cfg_big
    low = r.evaluate(ctx(assessment(p=0.70, lo=0.68, hi=0.72, uncertainty="low"))).decision
    med = r.evaluate(ctx(assessment(p=0.70, lo=0.68, hi=0.72, uncertainty="medium"))).decision
    assert low and med and med.expected_cost_usdc < low.expected_cost_usdc


def test_event_and_theme_exposure_caps(rm):
    r, p = rm
    other = market("other", yes="Y2", no="N2", event_id="e1", tags=["economy"])
    p.add_fill(market_slug=other.slug, token_id="Y2", outcome="YES", shares=1600, cost_usdc=800, ts=T0,
               event_id="e1", tags=["economy"])
    v = r.evaluate(ctx())  # meme evenement e1: 8% de 10k = 800 deja pris
    assert v.decision is None and "max_par_evenement" in v.reason
    p.add_fill(market_slug="x", token_id="Y3", outcome="YES", shares=2400, cost_usdc=1200, ts=T0,
               event_id="e9", tags=["economy"])
    v = r.evaluate(ctx(m=market(event_id="e2")))  # theme economy: 20% = 2000, deja 2000
    assert v.decision is None and "max_theme:economy" in v.reason


def test_daily_loss_and_drawdown_halts(rm):
    r, p = rm
    p.roll_day(10_000, T0)
    v = r.evaluate(ctx(equity=9_400, usdc=9_400, daily=DailyStats("2027-01-15", 10_000)))
    assert v.decision is None and "journaliere" in v.reason
    v = r.evaluate(ctx(equity=8_000, usdc=8_000, daily=DailyStats("2027-01-15", 8_000), peak=10_000))
    assert v.decision is None and "drawdown" in v.reason
    assert "drawdown" in p.halts()  # persistant: seule une action manuelle le leve


def test_exit_rules(rm):
    r, _ = rm
    pos = Position("fed", "Y", "YES", 100, 0.50, max_loss_usdc=15)
    assert r.exit_reason(pos, book("Y", 0.40, 0.42)) is None
    assert r.exit_reason(pos, book("Y", 0.34, 0.36)) == "stop_price"
    assert r.exit_reason(Position("fed", "Y", "YES", 100, 0.50, max_loss_usdc=5), book("Y", 0.44, 0.46)) == "max_loss"
    assert r.exit_reason(pos, book("Y", 0.98, 0.99)) == "take_profit"
    # these invalidee: meme la borne haute de la nouvelle estimation est sous le bid
    assert r.thesis_invalidated(pos, assessment(p=0.30, lo=0.25, hi=0.35), book("Y", 0.45, 0.47))
    assert not r.thesis_invalidated(pos, assessment(p=0.45, lo=0.40, hi=0.50), book("Y", 0.45, 0.47))
