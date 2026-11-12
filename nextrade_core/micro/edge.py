"""Microstructure edges on Quidax (research only; framework-free).

Two questions, both answered from recorded order books, neither needs a price forecast:

1. PRICE GAP: does Quidax's NGN price drift from the world price (Gate.io USDT price x the
   Quidax USDT/NGN rate) by more than it costs to trade, and does the gap close?
   Rule simulated (spot, so buy-only): when Quidax is at least `k` bps cheaper than the world,
   buy at Quidax's ask; sell at Quidax's bid when the gap has closed (>= 0) or after
   `max_hold` minutes. Taker fee on both sides.

2. MARKET MAKING: post a bid one tick above the best bid and an ask one tick below the best
   ask (when the spread is at least 3 ticks), re-quoted at every snapshot. Fill model,
   deliberately conservative: our bid fills only if at the NEXT snapshot the best ask has
   come down to or below our bid (someone sold through our price); our ask fills only if the
   next best bid is at or above our ask. Queue position and partial fills are ignored.
   Spot: we can only sell what we hold; inventory is capped. P&L is split into spread earned
   and inventory gain/loss (adverse selection), marked at the final mid.

Snapshots are 10 s apart by default, so fast moves between them are missed: results are an
indication to decide whether to build further, not a backtest to trade on.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def top_of_book(rows: list[dict]) -> pd.DataFrame:
    """rows: dicts with ts, bids, asks (best first, [price, qty] strings) -> ts-indexed bid/ask/mid."""
    if not rows:
        return pd.DataFrame(columns=["bid", "ask", "mid"])
    df = pd.DataFrame({"ts": [r["ts"] for r in rows],
                       "bid": [float(r["bids"][0][0]) for r in rows],
                       "ask": [float(r["asks"][0][0]) for r in rows]})
    df = df[(df["bid"] > 0) & (df["ask"] > df["bid"])].drop_duplicates("ts").set_index("ts").sort_index()
    df["mid"] = (df["bid"] + df["ask"]) / 2
    return df


def align_world(qx: pd.DataFrame, world_usdt: pd.DataFrame, usdtngn: pd.DataFrame,
                tolerance: str = "30s") -> pd.DataFrame:
    """Quidax NGN book joined with the world price in NGN (Gate USDT mid x Quidax USDT/NGN mid)."""
    tol = pd.Timedelta(tolerance)
    out = qx.copy()
    out = pd.merge_asof(out.sort_index(), world_usdt[["mid"]].rename(columns={"mid": "world_usdt"}),
                        left_index=True, right_index=True, tolerance=tol, direction="backward")
    out = pd.merge_asof(out, usdtngn[["mid"]].rename(columns={"mid": "usdtngn"}),
                        left_index=True, right_index=True, tolerance=tol, direction="backward")
    out["world_ngn"] = out["world_usdt"] * out["usdtngn"]
    out["gap_bps"] = (out["mid"] / out["world_ngn"] - 1) * 1e4
    out["spread_bps"] = (out["ask"] - out["bid"]) / out["mid"] * 1e4
    return out.dropna(subset=["gap_bps"])


def gap_summary(df: pd.DataFrame, taker_fee: float) -> dict:
    cost = 2 * taker_fee * 1e4 + df["spread_bps"]           # buy at ask, sell at bid, two fees
    g = df["gap_bps"]
    return {"snapshots": int(len(df)), "hours": round((df.index[-1] - df.index[0]).total_seconds() / 3600, 1)
            if len(df) > 1 else 0.0,
            "gap_median_bps": round(float(g.median()), 1), "gap_p05_bps": round(float(g.quantile(0.05)), 1),
            "gap_p95_bps": round(float(g.quantile(0.95)), 1),
            "spread_median_bps": round(float(df["spread_bps"].median()), 1),
            "round_trip_cost_median_bps": round(float(cost.median()), 1),
            "share_gap_beyond_cost": round(float((g.abs() > cost).mean()), 4)}


@dataclass(frozen=True)
class GapTrade:
    entry: pd.Timestamp
    exit: pd.Timestamp
    entry_gap_bps: float
    net_bps: float
    reason: str


def gap_trades(df: pd.DataFrame, k_bps: float, taker_fee: float, max_hold_min: float = 60) -> list[GapTrade]:
    """Buy when Quidax is >= k bps below the world price; exit when the gap closes or on time."""
    trades, i, n = [], 0, len(df)
    ts, gap, bid, ask = df.index, df["gap_bps"].to_numpy(), df["bid"].to_numpy(), df["ask"].to_numpy()
    hold = pd.Timedelta(minutes=max_hold_min)
    while i < n - 1:
        if gap[i] <= -k_bps:
            buy = ask[i] * (1 + taker_fee)
            j = i + 1
            while j < n - 1 and gap[j] < 0 and ts[j] - ts[i] < hold:
                j += 1
            sell = bid[j] * (1 - taker_fee)
            trades.append(GapTrade(ts[i], ts[j], float(gap[i]), float((sell / buy - 1) * 1e4),
                                   "closed" if gap[j] >= 0 else "time"))
            i = j + 1
        else:
            i += 1
    return trades


def gap_sweep(df: pd.DataFrame, ks: tuple[float, ...], taker_fee: float, max_hold_min: float = 60) -> pd.DataFrame:
    rows = []
    for k in ks:
        t = gap_trades(df, k, taker_fee, max_hold_min)
        net = np.array([x.net_bps for x in t])
        rows.append({"k_bps": k, "trades": len(t), "mean_net_bps": round(float(net.mean()), 1) if len(t) else None,
                     "win_rate": round(float((net > 0).mean()), 3) if len(t) else None,
                     "total_net_bps": round(float(net.sum()), 1) if len(t) else 0.0})
    return pd.DataFrame(rows)


@dataclass(frozen=True)
class MMResult:
    bid_fills: int
    ask_fills: int
    round_trips: int
    spread_earned_ngn: float       # realised: matched buys/sells, fees included
    inventory_pnl_ngn: float       # unmatched inventory marked at the last mid
    net_ngn: float
    max_inventory_ngn: float
    days: float

    @property
    def per_day_ngn(self) -> float:
        return self.net_ngn / self.days if self.days else 0.0


def simulate_market_making(book: pd.DataFrame, *, tick: float, order_ngn: float = 1250.0,
                           max_inventory_ngn: float = 6250.0, maker_fee: float = 0.0015,
                           min_spread_ticks: int = 3) -> MMResult:
    bid, ask, mid = (book[c].to_numpy() for c in ("bid", "ask", "mid"))
    ts = book.index
    lots: list[tuple[float, float]] = []      # FIFO inventory: (qty, cost per unit incl. fee)
    bid_fills = ask_fills = 0
    realised = 0.0
    max_inv = 0.0
    for i in range(len(book) - 1):
        if (ask[i] - bid[i]) / tick < min_spread_ticks:
            continue
        my_bid, my_ask = bid[i] + tick, ask[i] - tick
        inv_qty = sum(q for q, _ in lots)
        inv_ngn = inv_qty * mid[i]
        # our bid: only while inventory is under the cap
        if inv_ngn + order_ngn <= max_inventory_ngn and ask[i + 1] <= my_bid:
            q = order_ngn / my_bid
            cost = my_bid * (1 + maker_fee)
            lots.append((q, cost))
            bid_fills += 1
        # our ask: only what we hold (spot), one order's worth
        if lots and bid[i + 1] >= my_ask:
            q_sell = min(order_ngn / my_ask, sum(q for q, _ in lots))
            proceeds_unit = my_ask * (1 - maker_fee)
            left = q_sell
            while left > 1e-15 and lots:
                q, c = lots[0]
                take = min(q, left)
                realised += take * (proceeds_unit - c)
                left -= take
                lots[0] = (q - take, c)
                if lots[0][0] <= 1e-15:
                    lots.pop(0)
            ask_fills += 1
        max_inv = max(max_inv, sum(q for q, _ in lots) * mid[i])
    last_mid = mid[-1] if len(mid) else 0.0
    inventory_pnl = sum(q * (last_mid - c) for q, c in lots)
    days = (ts[-1] - ts[0]).total_seconds() / 86400 if len(ts) > 1 else 0.0
    return MMResult(bid_fills, ask_fills, min(bid_fills, ask_fills), round(realised, 2), round(inventory_pnl, 2),
                    round(realised + inventory_pnl, 2), round(max_inv, 2), round(days, 3))
