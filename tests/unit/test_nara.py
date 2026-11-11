import json
from decimal import Decimal as D
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from adapters.binance.filters import instrument_from_symbol
from nextrade_core.nara.engine import Decision, Halt, RiskEngine, TradeProposal
from nextrade_core.nara.killswitch import KillLevel, plan_actions
from nextrade_core.nara.limits import RiskLimits
from nextrade_core.nara.portfolio import (OpenPosition, PortfolioSnapshot, StressScenario, correlation_clusters,
                                          open_risk, stress_loss)
from nextrade_core.nara.sizing import size_position

INFO = json.loads((Path(__file__).parent.parent / "fixtures" / "binance_exchange_info.json").read_text())
BTC = instrument_from_symbol(INFO["symbols"][0])          # step 0.00001, min notional 5 USDT
COST = D("0.0026")


def snap(equity="1000", cash=None, positions=(), **kw):
    e = D(equity)
    return PortfolioSnapshot(equity=e, free_cash=D(cash) if cash else e, peak_equity=D(kw.pop("peak", e)),
                             day_start_equity=D(kw.pop("day", e)), week_start_equity=D(kw.pop("week", e)),
                             positions=tuple(positions), **kw)


# ------------------------------------------------------------------ sizing
def test_size_risks_the_budget_including_costs_and_rounds_down():
    r = size_position(equity=D(1000), risk_fraction=D("0.0025"), entry=D(60000), stop=D(58800),
                      instrument=BTC, cost_fraction=COST, free_cash=D(1000))
    assert r.ok
    per_unit = D(1200) + D(60000) * COST
    assert r.qty == BTC.normalize_qty(D("2.5") / per_unit)
    assert r.risk_amount <= D("2.5") and r.risk_amount == r.qty * per_unit
    assert BTC.order_violations(r.qty, D(60000)) == []


def test_naira_40k_account_hits_the_minimum_order_wall():
    """About 13 USDT of crypto capital: 0.25% risk cannot fund a 5 USDT minimum order."""
    r = size_position(equity=D("13.3"), risk_fraction=D("0.0025"), entry=D(60000), stop=D(58800),
                      instrument=BTC, cost_fraction=COST, free_cash=D("13.3"))
    assert not r.ok and "minimum order" in r.reject_reason


def test_free_cash_caps_spot_size():
    r = size_position(equity=D(100000), risk_fraction=D("0.01"), entry=D(60000), stop=D(59900),
                      instrument=BTC, cost_fraction=COST, free_cash=D(50))
    assert r.ok and r.notional <= D(50)


def test_stop_above_entry_rejected():
    r = size_position(equity=D(1000), risk_fraction=D("0.01"), entry=D(100), stop=D(101),
                      instrument=BTC, cost_fraction=COST, free_cash=D(1000))
    assert not r.ok


# ------------------------------------------------------------------ risk engine
ENGINE = RiskEngine()
GOOD = TradeProposal("BTCUSDT", D(60000), D(58800), COST)


def check(snapshot, proposal=GOOD, engine=ENGINE, **kw):
    args = dict(entries_allowed=True, data_fresh=True)
    args.update(kw)
    return engine.check(proposal, snapshot, BTC, **args)


def test_approves_clean_trade_and_records_every_rule():
    d = check(snap())
    assert d.decision is Decision.APPROVE and d.qty > 0 and d.limits_version == "limits-1"
    assert {r.rule for r in d.rules} >= {"size_valid", "open_risk_within_limit", "stress_loss_within_limit"}


@pytest.mark.parametrize("kwargs,rule", [
    (dict(entries_allowed=False), "system_allows_entries"),
    (dict(data_fresh=False), "data_fresh"),
    (dict(market_open=False), "instrument_tradable"),
])
def test_state_rules(kwargs, rule):
    d = check(snap(), **kwargs)
    assert not d.approved and rule in d.failed and d.qty == 0


def test_loss_and_drawdown_rules():
    assert "daily_loss_ok" in check(snap(equity="975", day="1000")).failed
    assert "weekly_loss_ok" in check(snap(equity="940", week="1000", day="940")).failed
    assert "drawdown_ok" in check(snap(equity="890", peak="1000", day="890", week="890")).failed
    assert "consecutive_losses_ok" in check(snap(consecutive_losses=6)).failed


def test_open_and_cluster_risk_limits():
    held = [OpenPosition(f"S{i}", D("0.0004"), D(60000), D(57000), D(60000)) for i in range(2)]
    d = check(snap(positions=held))  # 2 x 1.2 risk x 1.25 gap = 3.0 + new > 2% of 1000? no: 20
    assert d.approved
    heavy = [OpenPosition("ETHUSDT", D("0.004"), D(3000), D(2000), D(3000))]   # 4.0 risk -> 5.0 with gap
    snapshot = snap(positions=heavy, clusters={"ETHUSDT": "crypto", "BTCUSDT": "crypto"})
    d = check(snapshot, engine=RiskEngine(RiskLimits(max_cluster_risk=D("0.006"))))
    assert "cluster_risk_within_limit" in d.failed
    d2 = check(snapshot, engine=RiskEngine(RiskLimits(max_open_risk=D("0.005"))))
    assert "open_risk_within_limit" in d2.failed


def test_spread_position_count_stress_and_capital_cap():
    wide = TradeProposal("BTCUSDT", D(60000), D(58800), COST, spread_bps=D(40))
    assert "spread_within_limit" in check(snap(), wide).failed
    many = [OpenPosition(f"S{i}", D("0.00001"), D(100), D(99), D(100)) for i in range(10)]
    assert "position_count_ok" in check(snap(positions=many)).failed
    tight = RiskEngine(RiskLimits(max_stress_loss=D("0.0001")))
    assert "stress_loss_within_limit" in check(snap(), engine=tight).failed
    capped = RiskEngine(RiskLimits(max_capital_at_risk=D(5)))
    assert "capital_cap" in check(snap(), engine=capped).failed


def test_halts_in_priority_order():
    assert ENGINE.halts(snap())[0] is Halt.NONE
    assert ENGINE.halts(snap(equity="975", day="1000"))[0] is Halt.PAUSE_DAY
    assert ENGINE.halts(snap(consecutive_losses=6))[0] is Halt.PAUSE_LOSSES
    assert ENGINE.halts(snap(equity="940", week="1000", day="1000"))[0] is Halt.RISK_SHUTDOWN
    assert ENGINE.halts(snap(equity="890", peak="1000", day="890", week="890"))[0] is Halt.RISK_SHUTDOWN


def test_with_limits_does_not_mutate():
    other = ENGINE.with_limits(risk_per_trade=D("0.001"))
    assert other.limits.risk_per_trade == D("0.001") and ENGINE.limits.risk_per_trade == D("0.0025")


# ------------------------------------------------------------------ aggregate risk
def test_open_risk_and_stress():
    p = OpenPosition("BTCUSDT", D(1), D(100), D(95), D(100))
    assert open_risk([p], D("0.25")) == D("6.25")
    # 10% shock is worse than the 5% stop: exits at 90 * 0.995
    assert stress_loss([p]) == D(100) - D(90) * D("0.995")
    near = OpenPosition("BTCUSDT", D(1), D(100), D(98), D(100))
    assert stress_loss([near], StressScenario(market_shock=D("0.01"))) == D(100) - D(98) * D("0.995")


def test_correlation_clusters_with_floor():
    rng = np.random.default_rng(0)
    m = rng.normal(size=500)
    rets = pd.DataFrame({"BTC": m + rng.normal(0, .2, 500), "ETH": m + rng.normal(0, .2, 500),
                         "EURUSD": rng.normal(size=500), "GBPUSD": rng.normal(size=500)})
    c = correlation_clusters(rets, threshold=0.7, floor=None)
    assert c["BTC"] == c["ETH"] and c["EURUSD"] != c["GBPUSD"] and c["BTC"] != c["EURUSD"]
    c2 = correlation_clusters(rets, threshold=0.7, floor=0.8,
                              same_class={"BTC": "crypto", "ETH": "crypto", "EURUSD": "fx", "GBPUSD": "fx"})
    assert c2["EURUSD"] == c2["GBPUSD"] != c2["BTC"]


# ------------------------------------------------------------------ kill switch
def test_kill_levels_escalate():
    base = dict(venue_state_verified=True, working_entry_orders=["o1"], positions_missing_stop=["BTC"],
                positions=["BTC", "ETH"])
    a1 = [a.action for a in plan_actions(KillLevel.BLOCK_ENTRIES, **base)]
    assert a1 == ["block_new_entries"]
    a3 = [a.action for a in plan_actions(KillLevel.ENSURE_STOPS, **base)]
    assert a3 == ["block_new_entries", "cancel_order", "place_protective_stop"]


def test_never_blind_close_when_venue_unverified():
    actions = plan_actions(KillLevel.CLOSE_POSITIONS, venue_state_verified=False, working_entry_orders=[],
                           positions_missing_stop=[], positions=["BTC"], closing_safer_than_holding=True)
    names = [a.action for a in actions]
    assert "close_position" not in names and "reconcile_with_venue" in names
    ok = plan_actions(KillLevel.CLOSE_POSITIONS, venue_state_verified=True, working_entry_orders=[],
                      positions_missing_stop=[], positions=["BTC"], closing_safer_than_holding=True)
    assert [a.target for a in ok if a.action == "close_position"] == ["BTC"]


def test_limits_serialise():
    d = RiskLimits().as_dict()
    assert d["risk_per_trade"] == "0.0025" and d["max_positions"] == 10


def test_scaled_limits_scale_cross_trade_limits_only():
    from decimal import Decimal as D

    from nextrade_core.nara.limits import RiskLimits, scaled_limits
    base = RiskLimits()
    one = scaled_limits(D("0.01"), "quidax")
    assert one.risk_per_trade == D("0.01") and one.max_open_risk == D("0.08")
    assert one.max_cluster_risk == D("0.06") and one.max_stress_loss == D("0.16")
    assert (one.daily_loss, one.weekly_loss, one.max_drawdown) == (base.daily_loss, base.weekly_loss, base.max_drawdown)
    assert one.max_spread_bps == D("60") and scaled_limits(D("0.0025")).max_spread_bps == D("15")
    capped = scaled_limits(D("0.02"))
    assert capped.max_open_risk == D("0.10") and capped.max_cluster_risk == D("0.08")
    assert "r0.01" in one.version
    for bad in (D("0"), D("0.03")):
        with pytest.raises(ValueError):
            scaled_limits(bad)
