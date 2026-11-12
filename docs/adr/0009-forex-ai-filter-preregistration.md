# ADR 0009: FX7, an AI-filtered London breakout, pre-registered before testing

Date: 2026-11-12. Status: accepted, written BEFORE any test of FX7.

## Context
FX1-FX6 failed (ADR 0007, 0008). FX3 (London breakout) was the only strategy clearly better
than its random baseline (-0.083 R against -0.225 R), but it lost to costs. The operator asked
for an AI that learns continuously. The defensible form of that: NAIA's model, retrained on
every walk-forward window from earlier data only, decides which breakouts to skip.
This is presented as the LAST forex attempt in this series: after seven tests, a later pass
would be increasingly likely to be luck.

## Strategy FX7 `fx_london_ai` (15m)
* Entries, stops and target exactly as FX3 (ADR 0007), except the time limit: 96 bars (24 h)
  instead of 32, so most trades end at the stop or the target, which is what the model
  predicts.
* Model: gradient-boosted trees (`--model lgbm`), calibrated, retrained per window; falls back
  to the plain hit rate when a window has too little data.
* Inputs: the standard feature set plus six London inputs known at the signal close: night
  range in % and in ATR, distance of the close beyond the range in ATR, weekday, time, direction.
* NAIA takes a trade only if expected net R (probability x 1.5 R minus losses and costs, less
  one unit of model uncertainty) is at least 0.10 R, and uncertainty is at most 0.15 (defaults).

## Pass rule
Main test on data to 2025-11-12 (walk-forward 365/90 days, 1% risk, same costs and limits):
1. at least 100 filtered out-of-sample trades;
2. 95% interval of mean net R above zero;
3. beats the random-entry baseline;
4. positive in both halves of the test period;
5. NEW: the filtered trades beat the same rules WITHOUT the filter (else the AI adds nothing).
Then, only if all pass: the hold-out year (`--holdout-from 2025-11-12`) with at least 30 trades
and positive mean net R. Note: FX3's unfiltered result over the hold-out year was already seen
(ADR 0007 used all data); the filter itself has never seen it.
