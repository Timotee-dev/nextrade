# ADR 0010: The faster crypto strategies at Gate.io costs, pre-registered

Date: 2026-11-12. Status: accepted, written BEFORE the test.

Strategies trend, meanrev and breakout (unchanged, default parameters) failed on 1h and 4h
bars at Quidax costs (about 0.80% per round trip). Gate.io standard spot costs about 0.46%
(0.2% fee per side, 2 bps spread, 2 bps slippage per side). Same rules, same data (Gate.io
history: BTC/ETH/SOL USDT), only the cost of the venue changes: not a re-tune.
Tests: 4h and 1h, walk-forward 365/90 (1h: --expanding, history is about 14 months), 1% risk,
NAIA hit-rate model as before, the standard gate (>= 100 trades, CI above zero, beats random).
A pass would need its own paper test, and an operator decision on trading at Gate.io
(not licensed in Nigeria; funding via P2P/USDT). A fail closes the fast-crypto question.
