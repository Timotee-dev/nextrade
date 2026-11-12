# ADR 0011: Quidax microstructure research (market making, price gaps)

Date: 2026-11-12. Status: accepted. Research only: no orders are placed.

## Context
Thirteen price-pattern strategies failed (ADR 0006-0010); T1 alone passed. Every one of them
paid the spread. Quidax NGN books are thin (spreads 13-58 bps measured): that cost is income to
whoever provides liquidity, and a small local market may drift from world prices. Neither edge
needs a price forecast.

## Plan
1. `record_books`: every 10 s, top 5 levels of 7 Quidax NGN markets + USDT/NGN, and the Gate.io
   best bid/ask of each USDT pair. Run 2-4 weeks (about 130,000 rows a day).
2. `analyze_quidax_edge` (exploratory): gap distribution vs round-trip cost, a buy-the-dip gap
   sweep (k = 25-200 bps), and a market-making simulation one tick inside the spread with a
   conservative trade-through fill rule, spot inventory (sell only what is held), capped
   inventory, and a split of P&L into spread earned vs inventory loss.
3. Only if something is clearly positive: fixed rules written as a new ADR, confirmed on a
   NEW recording period, then paper trading with real Quidax limit orders on a tiny size.

## Known limits
10 s snapshots miss fast moves; queue position is ignored; the Quidax maker fee is not
confirmed (assumed equal to the 0.15% taker fee until checked); Gate.io and Quidax clocks are
aligned to the poll time; withdrawals/transfers between venues are out of scope (gap trading
here is Quidax-only, buy-when-cheap).
