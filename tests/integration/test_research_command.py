from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.market.models import Instrument
from apps.market.store import CandleStore
from apps.research.models import ExperimentRun
from nextrade_core.data.candles import Candle, Timeframe
from tests.helpers import random_walk_frame

pytestmark = pytest.mark.django_db


def seed_minutes(symbol: str, days: int, seed: int) -> None:
    inst = Instrument.objects.create(venue="binance", symbol=symbol, base=symbol[:-4], quote="USDT",
                                     asset_class="crypto", tick_size=Decimal("0.0001"), step_size=Decimal("0.001"),
                                     min_qty=Decimal("0.001"), min_notional=Decimal("5"))
    df = random_walk_frame(days * 1440, seed=seed)
    q = lambda x: Decimal(f"{x:.4f}")  # noqa: E731
    candles = [Candle(symbol, Timeframe.M1, t.to_pydatetime(), q(r.open), q(r.high), q(r.low), q(r.close),
                      q(r.volume), q(r.quote_volume), int(r.trades)) for t, r in df.iterrows()]
    CandleStore({symbol: inst}).save_candles(candles, "archive")


def test_run_backtest_end_to_end(settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    seed_minutes("AAAUSDT", 75, seed=3)
    call_command("run_backtest", timeframe="1h", strategies="trend,breakout", model="hitrate",
                 train_days=20, test_days=10, min_trades=20)
    out = capsys.readouterr().out
    assert "decision gate" in out and "trend-v1-" in out and "breakout-v1-" in out
    runs = ExperimentRun.objects.all()
    assert runs.count() == 2 and not any(r.gate_pass for r in runs)   # random walk: no edge
    assert all(r.dataset_hash for r in runs)
    assert len(list((tmp_path / "research" / "experiments").glob("*.json"))) == 2


def test_run_backtest_without_data_fails_clearly():
    from django.core.management.base import CommandError
    with pytest.raises(CommandError, match="No stored candles"):
        call_command("run_backtest")
