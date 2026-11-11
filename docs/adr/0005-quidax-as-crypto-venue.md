# ADR 0005: Quidax replaces Binance as the crypto venue

Date: 2026-11-11 · Status: accepted

## Context
From the operator's network, api.binance.com, data.binance.vision and api.bybit.com all
fail DNS resolution while google.com resolves: the exchanges are blocked. NEXTRADE does
not route around blocks (VPN or DNS changes): a bot whose venue access can vanish
mid-trade is unsafe, and the SEC has said only approved platforms may run crypto
trading in Nigeria. Quidax holds an SEC approval-in-principle (August 2024) and its
API answered from the operator's PC.

## Decision
- Research and trading use Quidax (`NEXTRADE_VENUE=quidax`). Binance code stays for
  operators who can reach it.
- Quidax k-lines are `[time_ms, open, CLOSE, HIGH, LOW, volume]`, newest first. This was
  checked against real responses (tests/fixtures/quidax) and is covered by a test.
- Research uses native 1h and 4h k-lines: Quidax returns up to 10,000 bars per call.
- Costs: 0.15% fee per side (0.1% advertised, 0.15% taker reported), spreads measured from
  recorded quotes, 40 bps default. Spreads on 2026-11-11: ETH/NGN 18 bps, USDT/NGN 21,
  DASH/NGN 34, BTC/NGN 49, SOL/NGN 50. Round trips cost 0.5% to 0.8% of price.

## Consequences
- Short timeframes are uneconomic: on 15m bars costs are about half of each trade's risk.
  Research focuses on 1h and 4h, where stops are wide enough for costs to be ~0.1-0.2 R.
  The 100 trades per day target is not achievable profitably on Quidax.
- Quidax has no exchange-side stop orders. Stops must be watched and executed by NEXTRADE
  itself (next step), which makes the watchdog and uptime critical while positions are open.
- Quidax documents no client order id: after an unknown outcome, orders are reconciled by
  listing recent orders. Live order handling must be verified with a tiny real order first.
- The minimum order size (1,250 on every market seen) is assumed to be in NGN. To verify.
