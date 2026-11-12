"""Direct MetaTrader 5 access through the official `MetaTrader5` Python package (Windows only).

The package talks to a running MT5 terminal on the same PC. No bridge server is needed.

Broker quirks handled here:
* Symbol suffixes: Exness cent accounts use "EURUSDc", others "EURUSDm" or "EURUSD.r".
  NEXTRADE stores the plain name (EURUSD); `suffix` maps it to the broker's name.
* Bar times: MT5 returns bar times in the broker's SERVER time written as if it were UTC
  (often UTC+2 in winter, UTC+3 in summer). `server_offset_hours` corrects that. It is
  detected from the latest tick when the market is open; otherwise it must be given.
* Spread: each bar carries the broker's spread in points; bps = spread * point / close.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from adapters.base import RejectedError, RetryableError
from adapters.binance.stream import Quote
from nextrade_core.data.candles import Candle, Timeframe
from nextrade_core.domain.instrument import AssetClass, Instrument

MT5_TIMEFRAMES = {
    Timeframe.M1: "TIMEFRAME_M1", Timeframe.M5: "TIMEFRAME_M5", Timeframe.M15: "TIMEFRAME_M15",
    Timeframe.M30: "TIMEFRAME_M30", Timeframe.H1: "TIMEFRAME_H1", Timeframe.H4: "TIMEFRAME_H4",
    Timeframe.D1: "TIMEFRAME_D1",
}
FOREX_MAJORS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURJPY")


def import_mt5():
    try:
        import MetaTrader5 as mt5  # noqa: N813
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise RejectedError("The MetaTrader5 package is not installed. On Windows run: "
                            ".venv\\Scripts\\pip install -r requirements-mt5.txt") from exc
    return mt5


def detect_server_offset(tick_time_s: int, now: datetime) -> int | None:
    """Whole-hour offset of the broker's server clock, or None if the tick is stale (market closed)."""
    shifted = datetime.fromtimestamp(tick_time_s, UTC)
    diff_h = (shifted - now).total_seconds() / 3600
    offset = round(diff_h)
    if abs(diff_h - offset) > 0.1 or not -12 <= offset <= 14:
        return None                      # tick is minutes or more old: the market is not trading now
    return offset


class MT5Local:
    def __init__(self, mt5=None, *, suffix: str = "", server_offset_hours: int | None = None,
                 login: int | None = None, password: str | None = None, server: str | None = None,
                 path: str | None = None) -> None:
        self.mt5 = mt5 or import_mt5()
        self.suffix = suffix
        kwargs = {k: v for k, v in {"login": login, "password": password, "server": server,
                                    "path": path}.items() if v}
        if not self.mt5.initialize(**kwargs):
            raise RetryableError(f"MT5 terminal did not start or log in: {self.mt5.last_error()}. "
                                 "Open MetaTrader 5, log in to your demo account, and retry.")
        self.offset = server_offset_hours

    def close(self) -> None:
        self.mt5.shutdown()

    def broker_symbol(self, symbol: str) -> str:
        return symbol + self.suffix

    def account(self) -> dict:
        info = self.mt5.account_info()
        if info is None:
            raise RetryableError(f"no account: {self.mt5.last_error()}")
        a = info._asdict()
        return {k: a.get(k) for k in ("login", "server", "currency", "balance", "equity", "leverage",
                                      "trade_mode", "company")}

    def symbol_info(self, symbol: str) -> dict:
        name = self.broker_symbol(symbol)
        if not self.mt5.symbol_select(name, True):
            raise RejectedError(f"broker has no symbol {name}: check the suffix (--suffix)")
        info = self.mt5.symbol_info(name)
        if info is None:
            raise RejectedError(f"broker has no symbol {name}")
        return info._asdict()

    def instrument(self, symbol: str) -> Instrument:
        i = self.symbol_info(symbol)
        return Instrument(
            venue="mt5", symbol=symbol, base=i.get("currency_base") or symbol[:3],
            quote=i.get("currency_profit") or symbol[3:6], asset_class=AssetClass.FOREX,
            tick_size=Decimal(str(i["point"])), step_size=Decimal(str(i["volume_step"])),
            min_qty=Decimal(str(i["volume_min"])), min_notional=Decimal(0),
            max_qty=Decimal(str(i["volume_max"])), contract_size=Decimal(str(i["trade_contract_size"])),
            tradable=i.get("trade_mode", 4) != 0)

    def server_offset(self, symbol: str, now: datetime) -> int:
        if self.offset is not None:
            return self.offset
        tick = self.mt5.symbol_info_tick(self.broker_symbol(symbol))
        found = detect_server_offset(int(tick.time), now) if tick is not None else None
        if found is None:
            raise RejectedError("The market is closed, so the broker's clock offset cannot be measured. "
                                "Run again while forex is open (Monday-Friday), or pass --utc-offset "
                                "(Exness and most brokers: 2 in winter, 3 in summer).")
        self.offset = found
        return found

    def rates(self, symbol: str, tf: Timeframe, start: datetime, end: datetime, now: datetime):
        """Closed bars with open_time in [start, end), times corrected to real UTC.

        Returns (candles, spreads_points) where spreads_points[i] belongs to candles[i]."""
        off = timedelta(hours=self.server_offset(symbol, now))
        const = getattr(self.mt5, MT5_TIMEFRAMES[tf])
        info = self.symbol_info(symbol)
        digits = int(info.get("digits", 5))
        candles, spreads = [], []
        chunk = timedelta(days=90 if tf.minutes <= 15 else 365)
        lo = start
        while lo < end:
            hi = min(lo + chunk, end)
            # MT5 wants naive datetimes in server time
            raw = self.mt5.copy_rates_range(self.broker_symbol(symbol), const,
                                            (lo + off).replace(tzinfo=None), (hi + off).replace(tzinfo=None))
            if raw is None:
                raise RetryableError(f"{symbol} {tf.value}: {self.mt5.last_error()}")
            for r in raw:
                t = datetime.fromtimestamp(int(r["time"]), UTC) - off
                if not lo <= t < hi or t + tf.delta > now:
                    continue
                q = lambda x: Decimal(f"{float(x):.{digits}f}")  # noqa: E731
                candles.append(Candle(symbol, tf, t, q(r["open"]), q(r["high"]), q(r["low"]), q(r["close"]),
                                      Decimal(int(r["tick_volume"])), Decimal(0), 0))
                spreads.append(int(r["spread"]))
            lo = hi
        return candles, spreads

    def quotes_from_bars(self, symbol: str, candles, spreads_points, point: Decimal) -> list[Quote]:
        """One quote per bar from the broker's recorded spread: bid = close, ask = close + spread."""
        return [Quote(symbol, c.close_time, c.close, c.close + point * s, Decimal(0), Decimal(0))
                for c, s in zip(candles, spreads_points, strict=True) if s > 0]
