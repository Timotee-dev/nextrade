# ADR 0007: Forex strategies FX1-FX3, pre-registered before testing

Date: 2026-11-12. Status: accepted, written BEFORE any forex backtest was run.

## Context
Crypto on Quidax costs about 80 bps per round trip; only one slow strategy (T1, ~6 trades a
year) survived. The operator wants many more trades. Forex through MT5 (Exness demo,
Standard account, 1:200) costs 0.65-2.9 bps of spread per round trip as measured on
2026-11-12, so faster strategies have a chance there.

## Data
Exness MT5 history for EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD, EURJPY:
1h from 2021-11, 15m from 2022-10/11 (the terminal's 100,000-bar limit). Bar times corrected
to UTC (Exness server time is UTC+0).

## Strategies (parameters fixed here; no tuning after results)
* FX1 `fx_trend`, 1h: long when close > previous 20-bar high and EMA50 > SMA200 and close >
  SMA200; short is the mirror. Stop 2 ATR, target 3 R, max 120 bars, cooldown 10 bars.
* FX2 `fx_meanrev`, 1h: when ADX < 20, long if RSI(14) < 25, short if RSI > 75. Stop 1.5 ATR,
  target 1.5 R, max 24 bars, cooldown 6 bars.
* FX3 `fx_london`, 15m: Asian range = high/low of bars opening 00:00-06:45 UTC. Between 07:00
  and 10:00 UTC, the first 15m close outside the range enters in that direction; the stop is
  the far side of the range; target 1.5 R; max 32 bars. Skipped if the range is under 0.15%
  or over 0.80% of price. At most one trade per pair per day. (Fixed UTC hours: London opens
  at 07:00 UTC in summer and 08:00 UTC in winter; not adjusted.)

All exit only by stop, target or time; all trade both directions.

## Costs and risk
No commission. Spread = measured median bar spread x 2 (MT5 records the lowest spread in a
bar; real spreads widen at news and rollover) + 0.5 bps slippage per side. Swap (overnight
financing) is NOT modelled yet: it matters for FX1 (holds up to 5 days), little for FX2/FX3.
Risk 1% per trade on a 10,000 USD research account at 1:200 (results are in R, so the edge
does not depend on account size). NARA limits apply as for live trading, including a
forex stress test of a 1% adverse gap through every stop at once.

## Pass rule (each strategy separately)
Walk-forward, 365-day train / 90-day test windows, rolling. A strategy passes only if ALL hold:
1. at least 100 out-of-sample trades;
2. the 95% bootstrap interval of mean net R lies above zero;
3. mean net R beats a random-entry baseline with random direction and identical exits;
4. mean net R is positive in BOTH halves of the out-of-sample period (by exit time).
Three strategies are tested, so roughly a 7% chance that one passes by luck under rule 2
alone; rule 4 is there to cut that.

## What follows
A strategy that passes goes to paper trading on the MT5 demo (batch F4), never straight to
real money. One that fails is recorded as failed and is not re-tuned on the same data.
