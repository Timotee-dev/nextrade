"""Rapid-scalping strategies (spot, long only). Pre-registered: parameters are fixed here, not tuned on results.

Each strategy looks at one symbol's live state (`SymbolState`) and returns a `HSignal` or None. They never size,
place orders or look at the account. `cost_bps` is the engine's current estimate of the round trip (both fees, half spread,
slippage): every target is set ABOVE it, and the engine refuses a signal whose planned net profit is below its minimum.

Families (ADR 0015):
  flow_burst     buyers are lifting the ask in size and price has just started to rise
  book_pressure  the best bid is much bigger than the best ask for seconds, price has not moved yet
  dip_bounce     a sharp, unusually large drop where selling stops and buyers return
  range_break    price breaks the last minute's high on a volume surge with buyers in control
"""

from __future__ import annotations

from dataclasses import dataclass

from .market import SymbolState


@dataclass(frozen=True)
class HSignal:
    strategy: str
    symbol: str
    ts: float
    mid: float
    target_bps: float          # wanted gross move from the signal mid
    stop_bps: float
    max_hold_s: float
    take_net_bps: float        # net profit (bps of notional) that arms the profit trail
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.strategy}:{self.symbol}:{int(self.ts * 10)}"


class HStrategy:
    code = "base"
    version = "v1"

    def evaluate(self, st: SymbolState, now: float, cost_bps: float) -> HSignal | None:
        raise NotImplementedError

    @property
    def tag(self) -> str:
        return f"{self.code}-{self.version}"


class FlowBurst(HStrategy):
    code = "flow_burst"
    max_spread_bps, buy_share, min_trades, ret_lo, ret_hi, min_imb = 6.0, 0.72, 6, 3.0, 25.0, 0.2
    take_net, stop_ratio, hold = 6.0, 0.8, 90.0

    def evaluate(self, st, now, cost_bps):
        if st.spread_bps > self.max_spread_bps:
            return None
        share, n, _vol = st.flow(now, 5.0)
        r5, r30 = st.ret_bps(now, 5.0), st.ret_bps(now, 30.0)
        if r5 is None or r30 is None or n < self.min_trades or share < self.buy_share:
            return None
        if not (self.ret_lo <= r5 <= self.ret_hi) or r30 < 0 or st.imbalance < self.min_imb:
            return None
        tgt = cost_bps + self.take_net
        return HSignal(self.code, st.symbol, now, st.mid, tgt, max(10.0, tgt * self.stop_ratio), self.hold, self.take_net,
                       f"buy share {share:.2f} on {n} trades, +{r5:.1f} bps in 5 s")


class BookPressure(HStrategy):
    code = "book_pressure"
    max_spread_bps, min_ema, min_imb, max_move, min_share = 4.0, 0.6, 0.5, 3.0, 0.55
    take_net, stop_ratio, hold = 5.0, 0.8, 45.0

    def evaluate(self, st, now, cost_bps):
        if st.spread_bps > self.max_spread_bps or st.imb_ema < self.min_ema or st.imbalance < self.min_imb:
            return None
        r3 = st.ret_bps(now, 3.0)
        share, n, _ = st.flow(now, 3.0)
        if r3 is None or abs(r3) > self.max_move or n < 2 or share < self.min_share:
            return None
        tgt = cost_bps + self.take_net
        return HSignal(self.code, st.symbol, now, st.mid, tgt, max(10.0, tgt * self.stop_ratio), self.hold, self.take_net,
                       f"bid/ask size imbalance {st.imb_ema:.2f} for seconds, price still flat")


class DipBounce(HStrategy):
    code = "dip_bounce"
    max_spread_bps, min_drop_bps, sigma_mult, min_share, min_trades, min_imb = 8.0, 8.0, 3.0, 0.6, 3, 0.1
    take_net, hold = 6.0, 120.0

    def evaluate(self, st, now, cost_bps):
        if st.spread_bps > self.max_spread_bps:
            return None
        r20 = st.ret_bps(now, 20.0)
        sig = st.vol_bps(now)
        if r20 is None or sig <= 0:
            return None
        need = max(self.min_drop_bps, self.sigma_mult * sig * (20.0 ** 0.5))
        if r20 > -need:
            return None
        share, n, _ = st.flow(now, 3.0)
        if n < self.min_trades or share < self.min_share or st.imbalance < self.min_imb:
            return None
        tgt = max(cost_bps + self.take_net, 0.5 * abs(r20))
        return HSignal(self.code, st.symbol, now, st.mid, tgt, max(10.0, 0.8 * tgt), self.hold, self.take_net,
                       f"dropped {r20:.1f} bps in 20 s (sigma {sig:.2f}/s), buyers returning")


class RangeBreak(HStrategy):
    code = "range_break"
    max_spread_bps, break_bps, vol_mult, min_share = 6.0, 1.0, 2.0, 0.6
    take_net, stop_ratio, hold = 8.0, 0.8, 90.0

    def evaluate(self, st, now, cost_bps):
        if st.spread_bps > self.max_spread_bps:
            return None
        prior = [m for ts, m in st.mids if now - 65.0 <= ts <= now - 5.0]
        if len(prior) < 40:
            return None
        if st.mid < max(prior) * (1 + self.break_bps / 10_000):
            return None
        share10, _n10, vol10 = st.flow(now, 10.0)
        _s60, _n60, vol60 = st.flow(now, 70.0, until=now - 10.0)       # volume in the 60 s before the last 10 s
        if share10 < self.min_share or vol60 <= 0 or vol10 < self.vol_mult * (vol60 / 6.0):
            return None
        tgt = cost_bps + self.take_net
        return HSignal(self.code, st.symbol, now, st.mid, tgt, max(10.0, tgt * self.stop_ratio), self.hold, self.take_net,
                       "broke the 60 s high on a volume surge")


ALL = (FlowBurst, BookPressure, DipBounce, RangeBreak)


def make_all() -> dict[str, HStrategy]:
    return {c.code: c() for c in ALL}
