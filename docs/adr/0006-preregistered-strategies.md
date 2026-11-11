# ADR 0006: Pre-registered strategies T1 and T2

Date: 2026-11-11 · Status: registered BEFORE any test of these rules

## Why pre-register
Strategies A, C and D failed at Quidax costs; daily breakout looked good on BTC/ETH/SOL and
then failed on 10 unseen coins. Every extra idea tried raises the odds of a lucky backtest.
So the next two ideas, their parameters, data, test and pass mark are fixed here, before
any result exists, and are not changed after seeing results.

## T1 `trendfilter` (200-day trend filter)
Buy on the daily close that crosses above the 200-day simple average. Sell on the first daily
close below the (current) 200-day average. Catastrophic stop 3 ATR(14) below entry. Target 10 R
(never meant to be hit). Maximum hold 365 bars.

## T2 `rotation` (monthly momentum rotation)
On the first daily bar of each month, rank the universe by 30-day return. Buy the top 2, only
if their 30-day return is positive. Exit after 25 bars, then re-select next month. Stop 3 ATR,
target 10 R.

## Test
- Universe: Quidax-tradable coins with real NGN volume and Gate.io history: BTC, ETH, SOL,
  LTC, XRP, DASH, TRX (USDT pairs on Gate.io as the price source).
- Daily bars; expanding walk-forward, 730-day first training window, 180-day test windows.
- Quidax costs: 0.15% fee per side, 40 bps spread (or measured). Capital NGN 20,000 in USDT
  equivalent; NGN 1,250 minimum order; 1% risk per trade with scaled cross-trade limits.
- Judged on raw trades (the NAIA hit-rate model does not describe rule-based exits).

## Pass mark (all three)
1. At least 30 out-of-sample trades.
2. 95% bootstrap interval of mean net R entirely above zero.
3. Mean net R above the random-entry baseline with identical exits.

A pass leads to paper trading on live Quidax prices, not to live money.
