import logging
import sys

import pytest

from core.config import LIVE_CONFIRMATION, BotConfig, Secrets
from core.logging_setup import RedactingFilter
from core.modes import LiveGateError, assert_live_allowed, effective_mode, live_gate_failures

FULL = Secrets(anthropic_api_key="sk-ant-x", x_bearer_token=None, polygon_private_key="0x" + "a" * 64,
               signature_type=1, funder_address="0x" + "b" * 40, live_unlock=LIVE_CONFIRMATION)


def live_cfg(**over):
    return BotConfig.model_validate({"mode": "live", "live": {"confirmation": LIVE_CONFIRMATION}, **over})


def test_default_mode_is_paper():
    assert BotConfig().mode == "paper"
    assert effective_mode(BotConfig(), None) == "paper"


def test_legacy_dry_run_is_rejected():
    with pytest.raises(ValueError, match="dry_run"):
        BotConfig.model_validate({"dry_run": False})


def test_unknown_config_key_is_rejected():
    with pytest.raises(ValueError):
        BotConfig.model_validate({"risk": {"max_exposure_per_market": 0.5}})  # faute de frappe


def test_live_requires_all_four_keys():
    assert live_gate_failures(live_cfg(), FULL, cli_confirm=True) == []
    assert any("--confirm-live" in f for f in live_gate_failures(live_cfg(), FULL, cli_confirm=False))
    assert any("POLYBOT_LIVE_UNLOCK" in f for f in
               live_gate_failures(live_cfg(), FULL.model_copy(update={"live_unlock": None}), True))
    no_confirm = BotConfig.model_validate({"mode": "live"})
    assert any("live.confirmation" in f for f in live_gate_failures(no_confirm, FULL, True))
    with pytest.raises(LiveGateError):
        assert_live_allowed(BotConfig(), FULL, cli_confirm=True)  # mode paper dans la config


def test_live_requires_credentials_and_sane_limits():
    no_key = FULL.model_copy(update={"polygon_private_key": None})
    assert any("POLYGON_PRIVATE_KEY" in f for f in live_gate_failures(live_cfg(), no_key, True))
    aggressive = live_cfg(risk={"kelly_fraction": 1.0})
    assert any("kelly_fraction" in f for f in live_gate_failures(aggressive, FULL, True))


def test_cli_can_only_lower_risk():
    assert effective_mode(live_cfg(), "paper") == "paper"


def test_paper_never_loads_real_order_client(tmp_path):
    from main import build_broker
    from core.state import Portfolio
    from core.store import Store

    sys.modules.pop("py_clob_client.client", None)
    broker = build_broker("paper", FULL, Portfolio(Store(str(tmp_path / "b.db"))), BotConfig())
    assert broker.live is False
    assert "py_clob_client.client" not in sys.modules  # meme avec une cle privee presente


def test_secrets_are_redacted_in_logs():
    f = RedactingFilter(["sk-ant-supersecret123"])
    rec = logging.LogRecord("x", logging.INFO, "", 0, "key=%s pk=%s", ("sk-ant-supersecret123", "0x" + "c" * 64), None)
    f.filter(rec)
    assert "supersecret" not in rec.msg and "c" * 64 not in rec.msg
    rec2 = logging.LogRecord("x", logging.INFO, "", 0, "adresse 0x%s", ("d" * 40,), None)
    f.filter(rec2)
    assert "d" * 40 in rec2.msg  # une adresse publique n'est pas un secret
