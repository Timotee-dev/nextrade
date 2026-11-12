# ADR 0008: Forex strategies FX4-FX6 and a hold-out year, pre-registered before testing

Date: 2026-11-12. Status: accepted, written BEFORE any test of FX4-FX6.

## Context
FX1-FX3 (ADR 0007) all failed: -0.24 R, -0.19 R and -0.07 R per trade out of sample. FX3
beat its random baseline (-0.22 R) but not costs. They are not re-tuned. The operator asked
for NEXTRADE to learn "how great forex traders trade". Three ideas are tested next: the two
with the most published evidence in currencies, and one widely taught by retail educators.

## Strategies (parameters fixed here)
* FX4 `fx_sweep`, 1h ("liquidity sweep" / stop hunt, Smart Money Concepts): a bar trades above
  the previous UTC day's high and closes back below it -> short; stop 0.1 ATR above that bar's
  high; target 2 R; max 24 bars; cooldown 6. Mirror for longs at the previous day's low.
* FX5 `fx_tsmom`, 1d (time-series momentum; Moskowitz, Ooi and Pedersen 2012): on the first
  daily bar of each month, long each pair whose close is above its close 126 bars earlier,
  short if below. Stop 3 ATR; target 10 R (out of reach); exit after 21 bars.
* FX6 `fx_xsmom`, 1d (cross-sectional currency momentum; Menkhoff, Sarno, Schmeling and
  Schrimpf 2012): on the first daily bar of each month rank the pairs by 63-bar return; long
  the 2 strongest, short the 2 weakest. Stop 3 ATR; target 10 R; exit after 21 bars.
Exness daily data includes short Sunday bars, so 126 bars is about 5-6 months, not exactly 6.

## Hold-out
The last 12 months (2025-11-12 to 2026-11-12) are NOT used in the main test.
1. Main test: data up to 2025-11-12, walk-forward 365/90 days, the ADR 0007 pass rule
   (>= 100 trades, 95% interval above zero, beats random baseline, positive in both halves).
2. Only a strategy that passes step 1 is run on the hold-out year (`--holdout-from 2025-11-12`).
   It must have a positive mean net R there, with at least 30 trades. The hold-out is used once.

## Unchanged
Costs (measured bar spread x2, 0.5 bps slippage, no swap), 1% risk, NARA limits. Note: all
forex pairs are one risk cluster, so at 1% risk about 4 positions fit (cluster limit 6% with a
25% gap allowance); monthly strategies with 8 pairs will have some entries refused.
