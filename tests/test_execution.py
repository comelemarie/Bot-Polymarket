import pytest

from core.clock import ManualClock
from core.config import ExitConfig, MarketDataConfig
from core.metrics import Metrics
from core.models import OrderRequest, SubmitResult, TradeDecision
from core.state import Portfolio
from core.store import Store
from execution.execution_engine import ExecutionEngine
from execution.paper_broker import PaperBroker
from execution.polygon_clob import classify_exception, parse_post_response
from execution.risk_manager import Verdict
from ingestion.market_state import BookState
from tests.helpers import T0, market


def book(asset="Y", bid=0.49, ask=0.50, size=1000):
    b = BookState(asset)
    b.apply_snapshot([{"price": bid, "size": size}], [{"price": ask, "size": size}], T0)
    return b


def decision(event="ev1", shares=100.0, limit=0.50, token="Y"):
    return TradeDecision(f"dec-{event}", event, "fed", token, "YES", limit, shares, shares * limit, 0.0, 0.1, 0.7,
                         0.2, "max_position", 20.0, "test")


async def ok_recheck(b):
    return Verdict(decision(), "ok")


class FakeLive:
    live = True

    def __init__(self, submit: SubmitResult, lookup: SubmitResult | None = None, positions=None):
        self.submit, self.lookup, self._positions = submit, lookup, positions
        self.submitted, self.cancelled = [], []

    async def balance(self):
        return 10_000.0

    async def submit_fok(self, req, book, fee_rate):
        self.submitted.append(req)
        return self.submit

    async def lookup_order(self, oid):
        return self.lookup

    async def cancel(self, oid):
        self.cancelled.append(oid)

    async def positions(self):
        return self._positions

    async def cancel_all(self):
        self.cancelled.append("*")


def engine(tmp_path, broker=None, mode="paper", db="e.db"):
    store = Store(str(tmp_path / db))
    p = Portfolio(store)
    broker = broker or PaperBroker(p, 1000.0)
    eng = ExecutionEngine(mode, store, p, broker, None, MarketDataConfig(verify_rest_before_order=False),
                          ExitConfig(), ManualClock(T0), Metrics())
    eng.market_lookup = {"fed": market()}.get
    return eng, store, p


@pytest.mark.asyncio
async def test_paper_fill_updates_position_and_cash_atomically(tmp_path):
    eng, store, p = engine(tmp_path)
    status = await eng.enter(decision(), market(), book(), 0.04, ok_recheck)
    assert status == "filled"
    pos = p.position("Y")
    fee_shares = 100 * 0.04 * 0.5
    assert pos.shares == pytest.approx(100 - fee_shares)  # frais preleves en parts
    assert pos.cost_usdc == pytest.approx(50.0) and p.paper_usdc == pytest.approx(950.0)
    assert store.one("SELECT status FROM orders")["status"] == "filled"


@pytest.mark.asyncio
async def test_same_event_never_produces_two_orders_even_after_restart(tmp_path):
    eng, store, p = engine(tmp_path)
    assert await eng.enter(decision(), market(), book(), 0.0, ok_recheck) == "filled"
    eng2, _, _ = engine(tmp_path)  # redemarrage sur la meme base
    assert await eng2.enter(decision(), market(), book(), 0.0, ok_recheck) == "duplicate"
    assert store.one("SELECT COUNT(*) n FROM orders")["n"] == 1


@pytest.mark.asyncio
async def test_fok_rejected_when_depth_insufficient(tmp_path):
    eng, store, p = engine(tmp_path)
    status = await eng.enter(decision(shares=5000), market(), book(size=100), 0.0,
                             lambda b: _verdict(decision(shares=5000)))
    assert status.startswith("rejected") and p.position("Y") is None and p.paper_usdc == 1000.0


async def _verdict(d):
    return Verdict(d, "ok")


@pytest.mark.asyncio
async def test_abort_when_price_moved_or_risk_recheck_fails(tmp_path):
    eng, store, p = engine(tmp_path)
    status = await eng.enter(decision(limit=0.50), market(), book(ask=0.53), 0.0, ok_recheck)
    assert status.startswith("aborted") and "bouge" in status

    async def refuse(b):
        return Verdict(None, "solde ou equity inconnus")

    status = await eng.enter(decision("ev2"), market(), book(), 0.0, refuse)
    assert status.startswith("aborted") and "inconnus" in status
    assert store.one("SELECT COUNT(*) n FROM orders")["n"] == 0  # rien n'a ete envoye


@pytest.mark.asyncio
async def test_ambiguous_response_blocks_then_reconciles(tmp_path):
    live = FakeLive(SubmitResult("unknown", exchange_order_id="0xabc", error="timeout"),
                    lookup=SubmitResult("filled", 100, 0.5, exchange_order_id="0xabc"),
                    positions={"Y": 100.0})
    eng, store, p = engine(tmp_path, live, mode="live")
    assert await eng.enter(decision(), market(), book(), 0.0, ok_recheck) == "unknown"
    assert p.position("Y") is None  # une reponse perdue n'est PAS une absence d'execution...
    assert eng.unresolved_on(["Y", "N"])  # ...et bloque le marche
    assert (await eng.enter(decision("ev2"), market(), book(), 0.0, ok_recheck)).startswith("blocked")
    assert len(live.submitted) == 1
    assert await eng.reconcile() is True
    assert p.position("Y").shares == 100 and not eng.unresolved_on(["Y", "N"])


@pytest.mark.asyncio
async def test_resting_order_found_during_reconciliation_is_cancelled(tmp_path):
    live = FakeLive(SubmitResult("unknown", exchange_order_id="0xdef"),
                    lookup=SubmitResult("unknown", 0, 0.5, exchange_order_id="0xdef", error="ordre au carnet"),
                    positions={})
    eng, store, p = engine(tmp_path, live, mode="live")
    await eng.enter(decision(), market(), book(), 0.0, ok_recheck)
    assert await eng.reconcile() is False and live.cancelled == ["0xdef"]


@pytest.mark.asyncio
async def test_position_mismatch_halts_and_adopts_account(tmp_path):
    live = FakeLive(SubmitResult("filled", 100, 0.5, exchange_order_id="1"), positions={"Y": 60.0})
    eng, store, p = engine(tmp_path, live, mode="live")
    await eng.enter(decision(), market(), book(), 0.0, ok_recheck)
    assert await eng.reconcile() is True and not p.halts()  # Data API possiblement en retard: delai de grace
    eng.clock.advance(301)
    assert await eng.reconcile() is False
    assert p.position("Y").shares == 60.0 and "reconciliation" in p.halts()
    live._positions = None
    await eng.reconcile()
    assert "reconciliation" in p.halts()  # positions inconnues: exposition inconnue => blocage


@pytest.mark.asyncio
async def test_interrupted_intent_after_crash(tmp_path):
    eng, store, p = engine(tmp_path)
    store.insert("orders", {"client_order_id": "entry:x:Y", "decision_id": "d", "created_at": T0, "updated_at": T0,
                            "market_slug": "fed", "token_id": "Y", "side": "BUY", "purpose": "entry",
                            "limit_price": 0.5, "shares": 10, "status": "intent"})
    await eng.recover_on_startup()
    assert store.one("SELECT status FROM orders")["status"] == "cancelled"  # simulation: aucun effet possible

    live = FakeLive(SubmitResult("filled"), lookup=None, positions={})
    eng2, store2, _ = engine(tmp_path, live, mode="live", db="live.db")
    store2.insert("orders", {"client_order_id": "entry:x:Y", "decision_id": "d", "created_at": T0, "updated_at": T0,
                             "market_slug": "fed", "token_id": "Y", "side": "BUY", "purpose": "entry",
                             "limit_price": 0.5, "shares": 10, "status": "intent"})
    await eng2.recover_on_startup()
    assert store2.one("SELECT status FROM orders")["status"] == "unknown"  # reel: a verifier, jamais suppose
    assert eng2.unresolved_on(["Y"])


@pytest.mark.asyncio
async def test_exit_sells_available_depth_and_books_pnl(tmp_path):
    eng, store, p = engine(tmp_path)
    await eng.enter(decision(), market(), book(), 0.0, ok_recheck)
    status = await eng.exit(p.position("Y"), market(), "stop_price", book(bid=0.30, ask=0.32), 0.0)
    assert status == "filled" and p.position("Y") is None
    ex = store.one("SELECT * FROM exits")
    assert ex["reason"] == "stop_price" and ex["pnl_usdc"] == pytest.approx(100 * 0.30 - 50)


def test_live_response_classification():
    from py_clob_client.exceptions import PolyApiException

    assert classify_exception(PolyApiException(error_msg="Request exception!")).status == "unknown"

    class R:
        def __init__(self, code):
            self.status_code, self.text = code, "x"

        def json(self):
            return {"error": "x"}

    assert classify_exception(PolyApiException(R(400))).status == "rejected"
    assert classify_exception(PolyApiException(R(503))).status == "unknown"
    assert classify_exception(PolyApiException(R(408))).status == "unknown"
    req = OrderRequest("c", "Y", "BUY", 0.5, 10)
    r = parse_post_response({"success": True, "status": "matched", "orderID": "1", "makingAmount": "5",
                             "takingAmount": "9.8"}, "BUY", req)
    assert r.status == "filled" and r.filled_shares == 9.8 and r.raw["cost_usdc"] == 5.0
    assert parse_post_response({"success": True, "status": "delayed"}, "BUY", req).status == "unknown"
    assert parse_post_response({"success": False, "errorMsg": "not enough balance"}, "BUY", req).status == "rejected"
    assert parse_post_response(None, "BUY", req).status == "unknown"
