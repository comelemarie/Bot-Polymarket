import asyncio
from types import SimpleNamespace

import pytest

from brain.claude_analyst import ClaudeAnalyst, ClaudeAssessment
from core.clock import ManualClock
from core.config import ClaudeConfig
from core.store import Store
from ingestion.market_state import BookState
from ingestion.news_filter import Trigger
from tests.helpers import T0, market, news


def make_parsed(**over) -> ClaudeAssessment:
    base = dict(market_id="fed", event_id="ev1", p_yes=0.7, p_low=0.6, p_high=0.8, uncertainty="medium",
                confidence="high", key_facts=["a"], supporting_evidence=["s"], contradicting_evidence=["c"],
                source_urls=["https://bbc.example/a"], published_at=["2027-01-15"],
                market_resolution_understanding="YES si la Fed baisse ses taux avant le 31/12",
                already_priced_in="partially", reasoning_summary="r", abstain=False, abstain_reason="")
    base.update(over)
    return ClaudeAssessment(**base)


class FakeMessages:
    def __init__(self, parsed=None, stop_reason="end_turn", exc=None, delay=0.0):
        self.parsed, self.stop_reason, self.exc, self.delay = parsed, stop_reason, exc, delay
        self.calls = 0

    async def parse(self, **kw):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return SimpleNamespace(parsed_output=self.parsed, stop_reason=self.stop_reason, stop_details=None,
                               usage=SimpleNamespace(input_tokens=3000, output_tokens=1000), id="msg_1",
                               _request_id="req_1")


def make(tmp_path, msgs, **cfg):
    client = SimpleNamespace(beta=SimpleNamespace(messages=msgs))
    store = Store(str(tmp_path / "a.db"))
    clock = ManualClock(T0)
    an = ClaudeAnalyst(ClaudeConfig(**cfg), "k", store, clock, client=client)
    return an, store, clock


def trig(eid="ev1", news_items=None):
    return Trigger(eid, "news_only", market(), news_items or [news()], 0.0, T0)


def books():
    y, n = BookState("Y"), BookState("N")
    y.apply_snapshot([{"price": 0.49, "size": 100}], [{"price": 0.51, "size": 100}], T0)
    n.apply_snapshot([{"price": 0.49, "size": 100}], [{"price": 0.51, "size": 100}], T0)
    return y, n


@pytest.mark.asyncio
async def test_valid_answer_is_logged_with_cost(tmp_path):
    an, store, _ = make(tmp_path, FakeMessages(make_parsed()))
    a = await an.assess(trig(), *books(), {})
    assert not a.abstain and a.p_yes == 0.7
    row = store.one("SELECT * FROM analyses")
    assert row["valid"] == 1 and row["cost_usd"] == pytest.approx((3000 * 2 + 1000 * 10) / 1e6)
    assert "<incoming_news>" in row["prompt"] and row["request_id"] == "req_1"


@pytest.mark.asyncio
@pytest.mark.parametrize("parsed,stop,exc,reason", [
    (None, "end_turn", None, "non parsable"),
    (make_parsed(), "max_tokens", None, "tronquee"),
    (make_parsed(), "refusal", None, "refus"),
    (None, "end_turn", ValueError("invalid json"), "schema"),
    (make_parsed(source_urls=["https://invented.example/x"]), "end_turn", None, "non fournie"),
    (make_parsed(p_low=0.75), "end_turn", None, "incoherent"),
    (make_parsed(event_id="autre"), "end_turn", None, "identifiants"),
    (make_parsed(abstain=True, abstain_reason="regles ambigues"), "end_turn", None, "ambigues"),
])
async def test_invalid_answers_abstain(tmp_path, parsed, stop, exc, reason):
    an, store, _ = make(tmp_path, FakeMessages(parsed, stop, exc))
    a = await an.assess(trig(), *books(), {})
    assert a.abstain and reason in a.abstain_reason, a.abstain_reason


@pytest.mark.asyncio
async def test_timeout_abstains(tmp_path):
    an, _, _ = make(tmp_path, FakeMessages(make_parsed(), delay=5), timeout_s=0.01, max_retries=0)
    an.cfg.timeout_s = 0.01
    import brain.claude_analyst as mod

    orig = asyncio.wait_for

    async def fast_wait_for(aw, timeout):
        return await orig(aw, 0.05)

    mod.asyncio.wait_for = fast_wait_for
    try:
        a = await an.assess(trig(), *books(), {})
    finally:
        mod.asyncio.wait_for = orig
    assert a.abstain and "expiration" in a.abstain_reason


@pytest.mark.asyncio
async def test_call_budget_and_cost_cap_survive_restart(tmp_path):
    msgs = FakeMessages(make_parsed())
    an, store, clock = make(tmp_path, msgs, max_calls_per_hour=1)
    await an.assess(trig("ev1"), *books(), {})
    an2 = ClaudeAnalyst(ClaudeConfig(max_calls_per_hour=1), "k", store, clock,
                        client=SimpleNamespace(beta=SimpleNamespace(messages=msgs)))  # redemarrage
    y, n = books()
    y.apply_snapshot([{"price": 0.30, "size": 100}], [{"price": 0.32, "size": 100}], T0)  # autre requete
    a = await an2.assess(trig("ev1", [news("other", "n9", url="https://o/1")]), y, n, {})
    assert a.abstain and "budget" in a.abstain_reason and msgs.calls == 1

    an3, _, _ = make(tmp_path / "x", FakeMessages(make_parsed()), max_cost_usd_per_day=0.01)
    a = await an3.assess(trig(), *books(), {})
    assert a.abstain and "cout" in a.abstain_reason


@pytest.mark.asyncio
async def test_cache_and_inflight_dedup(tmp_path):
    msgs = FakeMessages(make_parsed(), delay=0.05)
    an, store, _ = make(tmp_path, msgs)
    a1, a2 = await asyncio.gather(an.assess(trig(), *books(), {}), an.assess(trig(), *books(), {}))
    assert msgs.calls == 1 and a1.p_yes == a2.p_yes  # requetes identiques simultanees: un seul appel
    a3 = await an.assess(trig("ev2"), *books(), {})
    assert msgs.calls == 1 and a3.cached and a3.event_id == "ev2"
