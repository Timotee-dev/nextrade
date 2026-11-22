# ADR 0012: scalping engine (pre-registration, 2026-11-22)

## Context
The operator asked for an autonomous multi-asset scalping engine: many small net profits, several
simultaneous positions, automatic re-entry, N40,000 capital. Earlier results (ADR 0007-0010)
found that fast crypto strategies and all six forex strategies lose after costs. This ADR records
what was built and what is fixed BEFORE any scalp backtest is run, so results cannot be tuned.

## What already existed and is reused (not rebuilt)
Multi-position paper daemon (`services/trader/daemon.py`: scan, NAIA, NCTPI, NARA, reconcile on
restart, idempotent client order IDs), central risk engine, kill switch, paper broker, Telegram bot
and alert outbox, dashboard, regime classifier, cost model, walk-forward research and decision gate.

## What is new
- `strategies/scalp.py`: six strategies A-F (momentum, trend pullback, mean reversion, breakout,
  volatility expansion, short-selling), each from several evidence families plus 1h trend context.
- `scalping/mtf.py`: 1h context for 1m/3m/5m/15m bars using only CLOSED higher bars (tested for no look-ahead).
- `scalping/profit.py`: net-profit-aware exits (fixed, percent, ATR, trailing, momentum, time).
- `scalping/capital.py`: per-instrument feasibility (minimums, cost share of target, leverage cap, shorting).
- `scalping/reentry.py`: cooldowns, no reopening on an old signal, side-flip wait, loss-streak pause.
- `scalping/approvals.py` + `approve_strategy`: only a passed gate and an operator command allow paper.
- `scalping/scanner.py` + `scan_scalp`: research mode, accepted and rejected candidates with reasons.
- `scalping/analytics.py` + `scalp_report`: by strategy, symbol, session, regime; net vs gross vs fees.
- `run_scalper --mode research|paper|live`: live refused (no live broker; needs NEXTRADE_LIVE_ENABLED=yes).

## Pre-registered parameters (fixed now; not to be tuned)
| Code | Timeframe | Entry (both sides unless noted) | Stop / target / max hold / cooldown |
| --- | --- | --- | --- |
| scalp_momentum | 5m | close vs slow EMA >= 1 ATR, rel. volume >= 1.3, ADX >= 22, 1h trend agrees | 1.2 ATR / 1.5R / 24 bars / 6 |
| scalp_trend | 5m | 1h trend, EMA spread beyond 0.5 ATR, DI diff agrees, VWAP pullback, RSI 40-55 (short 45-60) | 1.2 ATR / 1.5R / 24 / 6 |
| scalp_meanrev | 5m | ADX < 20 and 1h ADX < 25, %B < 0.02 and RSI < 25 (short: > 0.98, > 75) | 1.5 ATR / 1.0R / 12 / 6 |
| scalp_breakout | 5m | bands in lowest 20% last bar, channel break, rel. volume >= 1.5, close in top/bottom 25% of bar, 1h not against | 1.0 ATR / 2.0R / 24 / 6 |
| scalp_vol | 15m | ATR percentile from <= 0.25 to >= 0.50 within 6 bars, 3-bar move > 1 ATR, 1h not against | 1.5 ATR / 2.0R / 16 / 6 |
| scalp_short | 5m | short only: 1h downtrend, structure break down, rel. volume >= 1.2, ADX >= 22 | 1.2 ATR / 1.5R / 24 / 6 |

Judged on raw trades after costs, against the random-entry baseline with identical exits, positive in both
halves of the out-of-sample period (the same gate as ADR 0007). Test on Gate.io costs (0.2%/side).
If a strategy fails, it is recorded and left failed. No parameter search after seeing results.

## Decisions
- Quidax is excluded for scalping by arithmetic: about 0.8% round trip against 5m targets of 0.3-0.6%.
- N40,000 is about $27: forex standard lots are infeasible (capital.py marks them unavailable).
- No martingale, no averaging down, no automatic risk or leverage increases (unchanged NARA rules).
- 100 trades/day is a capacity, never a target. Waiting is a valid outcome.

## Consequences
Nothing trades until a strategy passes the gate and the operator approves it. Expect most or all to fail.
That result is recorded, not hidden.
