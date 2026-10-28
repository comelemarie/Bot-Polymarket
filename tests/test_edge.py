import pytest

from core.config import EdgeConfig
from execution.edge_calculator import (
    conservative_interval,
    kelly_fraction,
    marginal_buy_edge,
    max_shares_with_edge,
    quote_buy,
    quote_sell,
    shares_for_budget,
    side_candidates,
    taker_fee_usdc,
)
from tests.helpers import assessment


def test_fee_formula():
    # parts * taux * p * (1 - p)
    assert taker_fee_usdc(100, 0.5, 0.04) == pytest.approx(1.0)
    assert taker_fee_usdc(100, 0.9, 0.04) == pytest.approx(0.36)
    assert taker_fee_usdc(100, 0.5, 0.0) == 0.0


def test_quote_buy_vwap_fees_in_shares_and_net_ev():
    q = quote_buy([(0.50, 10), (0.52, 10)], 15, p=0.70, fee_rate=0.04, slippage=0.005)
    assert q.vwap == pytest.approx((10 * 0.50 + 5 * 0.52) / 15)
    assert q.worst_price == 0.52
    fee_shares = 10 * 0.04 * 0.5 + 5 * 0.04 * 0.48
    assert q.received_shares == pytest.approx(15 - fee_shares)
    assert q.ev_net_usdc == pytest.approx(0.70 * q.received_shares - q.cost_usdc - 0.005 * 15)
    assert q.ev_gross_usdc > q.ev_net_usdc  # frais et slippage comptes une fois, en moins
    assert quote_buy([(0.5, 10)], 11, 0.7, 0.0, 0.0) is None  # profondeur insuffisante


def test_max_shares_stops_when_marginal_edge_too_low():
    levels = [(0.50, 100), (0.60, 100), (0.70, 100)]
    shares, worst = max_shares_with_edge(levels, p=0.70, fee_rate=0.0, slippage=0.0, min_edge=0.05)
    assert (shares, worst) == (200, 0.60)
    assert marginal_buy_edge(0.70, 0.60, 0.0, 0.0) == pytest.approx(0.10)


def test_no_side_uses_no_book_not_complement():
    a = assessment(p=0.20, lo=0.18, hi=0.22, uncertainty="low")
    # YES ask 0.25 -> NO "complement" serait 0.75, mais le vrai ask NO est 0.85 (spread large)
    cands = side_candidates(a, [(0.25, 100)], [(0.85, 100)], "Y", "N", {"Y": 0.0, "N": 0.0},
                            EdgeConfig(min_net_edge=0.04, slippage_buffer=0.0))
    no = next(c for c in cands if c.outcome == "NO")
    assert no.p_conservative == pytest.approx(1 - 0.23)  # borne haute elargie au minimum (0.03)
    assert no.max_shares == 0  # 0.77 - 0.85 < 0: aucun avantage au prix reel


def test_interval_never_narrower_than_configured_minimum():
    cfg = EdgeConfig()
    lo, hi = conservative_interval(assessment(p=0.6, lo=0.59, hi=0.61, uncertainty="medium"), cfg)
    assert (lo, hi) == (pytest.approx(0.53), pytest.approx(0.67))
    lo, hi = conservative_interval(assessment(p=0.6, lo=0.40, hi=0.80, uncertainty="low"), cfg)
    assert (lo, hi) == (0.40, 0.80)  # un intervalle plus large que le minimum est conserve


def test_kelly_and_budget():
    assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)
    assert kelly_fraction(0.4, 0.5) == 0.0
    assert shares_for_budget([(0.5, 10), (0.6, 10)], budget_usdc=8.0, max_shares=100) == 15.0
    assert shares_for_budget([(0.5, 10), (0.6, 10)], budget_usdc=100, max_shares=12.345) == 12.34


def test_quote_sell_net_of_fees():
    s = quote_sell([(0.60, 5), (0.58, 10)], 10, fee_rate=0.04)
    assert s.vwap == pytest.approx((5 * 0.60 + 5 * 0.58) / 10)
    assert s.proceeds_usdc == pytest.approx(5 * 0.60 + 5 * 0.58 - s.fee_usdc)
    assert quote_sell([(0.6, 5)], 10, 0.0) is None
