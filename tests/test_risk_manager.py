import pytest

from core.config import RiskConfig
from core.models import Assessment, Market, OrderBook
from core.state import DailyStats, Position
from execution.polygon_clob import simulate_fok
from execution.risk_manager import RiskManager, floor_to_tick, kelly_fraction, shrink_to_market


def make_market() -> Market:
    return Market("m", "Q?", "rules", "2026-12-31", "c", "YES_T", "NO_T", 0.01, False, 100_000, ["x"])


def make_book(asset: str, bid: float, ask: float, depth: float = 10_000) -> OrderBook:
    return OrderBook(asset, bids={bid: depth}, asks={ask: depth, round(ask + 0.01, 2): depth}, updated_at=1)


def make_assessment(p: float, conf: float = 1.0, **kw) -> Assessment:
    base = dict(relevant=True, probability_yes=p, confidence=conf, already_priced_in=False,
                reasoning="", key_facts=[])
    base.update(kw)
    return Assessment(**base)


def evaluate(rm, assessment, yes=(0.49, 0.50), no=(0.50, 0.51), usdc=10_000.0, equity=10_000.0,
             market_exp=0.0, total_exp=0.0, daily=None):
    return rm.evaluate(
        make_market(), assessment, make_book("YES_T", *yes), make_book("NO_T", *no),
        usdc_balance=usdc, equity=equity, market_exposure=market_exp, total_exposure=total_exp,
        daily=daily or DailyStats("2026-01-01", equity),
    )


def test_kelly_formula():
    assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)
    assert kelly_fraction(0.4, 0.5) == 0.0
    assert kelly_fraction(0.9, 1.0) == 0.0


def test_shrink_and_tick():
    assert shrink_to_market(0.8, 0.5, 0.5) == pytest.approx(0.65)
    assert floor_to_tick(0.5678, 0.01) == 0.56
    assert floor_to_tick(0.57, 0.01) == 0.57


def test_buys_yes_capped_at_5pct_per_market():
    rm = RiskManager(RiskConfig())
    v = evaluate(rm, make_assessment(0.80))
    d = v.decision
    assert d is not None and d.outcome == "YES" and d.token_id == "YES_T"
    # Kelly 25% donnerait ~1500 USDC; le plafond par marche (5% de 10k) doit s'appliquer.
    assert d.size_usdc <= 500.0 + 1e-6
    assert "max_par_marche" in d.reason
    assert d.limit_price >= 0.50


def test_buys_no_when_claude_bearish():
    rm = RiskManager(RiskConfig())
    d = evaluate(rm, make_assessment(0.20)).decision
    assert d is not None and d.outcome == "NO"


def test_rejections():
    rm = RiskManager(RiskConfig())
    assert evaluate(rm, make_assessment(0.53)).decision is None  # edge trop faible
    assert evaluate(rm, make_assessment(0.8, conf=0.3)).decision is None  # confiance trop basse
    assert evaluate(rm, make_assessment(0.8, relevant=False)).decision is None
    assert evaluate(rm, make_assessment(0.8, already_priced_in=True)).decision is None
    # Spread trop large des deux cotes
    assert evaluate(rm, make_assessment(0.8), yes=(0.30, 0.50), no=(0.40, 0.70)).decision is None


def test_usdc_reserve_and_existing_exposure():
    rm = RiskManager(RiskConfig())
    # 104 USDC dispo, reserve 100 -> 4 USDC < minimum d'ordre
    assert evaluate(rm, make_assessment(0.8), usdc=104).decision is None
    # Marche deja expose a 5%
    assert evaluate(rm, make_assessment(0.8), market_exp=500).decision is None


def test_daily_kill_switch():
    rm = RiskManager(RiskConfig())
    daily = DailyStats("2026-01-01", start_equity=10_000)
    v = evaluate(rm, make_assessment(0.8), equity=8_500, usdc=8_500, daily=daily)
    assert v.decision is None and daily.halted


def test_exit_signal():
    rm = RiskManager(RiskConfig(stop_loss_pct=0.3, take_profit_price=0.97))
    pos = Position("m", "YES_T", "YES", 100, 0.50)
    assert rm.exit_signal(pos, 0.36) is None
    assert "stop-loss" in rm.exit_signal(pos, 0.35)
    assert "take-profit" in rm.exit_signal(pos, 0.98)


def test_simulate_fok():
    asks = {0.50: 10, 0.51: 10}
    assert simulate_fok(asks, 15, 0.51, "BUY") == pytest.approx((10 * 0.50 + 5 * 0.51) / 15)
    assert simulate_fok(asks, 25, 0.51, "BUY") is None
    assert simulate_fok(asks, 15, 0.50, "BUY") is None
