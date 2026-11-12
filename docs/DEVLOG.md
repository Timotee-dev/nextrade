# NEXTRADE development log

Newest first.

## 2026-11-12 · FX1-FX3 failed; FX4-FX6 and a hold-out year pre-registered

**Results (operator's run, ADR 0007 rules)**: FX1 fx_trend 276 OOS trades, -0.240 R (CI -0.416
to -0.054), baseline -0.074: FAIL. FX2 fx_meanrev 170 trades, -0.191 R, baseline -0.066: FAIL.
FX3 fx_london 993 trades, -0.067 R (CI -0.118 to -0.017), baseline -0.224, halves -0.075 /
-0.058: FAIL (beats random but not costs). Not re-tuned.

**Built**: FX4 fx_sweep (liquidity sweep, 1h), FX5 fx_tsmom (time-series momentum, 1d), FX6
fx_xsmom (cross-sectional momentum, 1d), ADR 0008 written first; `--holdout-from` scores only
trades in the unseen final year. **Tests**: 426 passing.

## 2026-11-12 · Fix: timeframe guessed from a weekend gap

The operator's first forex run crashed: the timeframe was inferred from the first two bars,
and Exness 1h data starts with a Sunday 22:00 bar followed by a gap. Now the most common bar
spacing is used. Also: the test suite wrote experiment records into the real
research/experiments folder (13 test files reached the repository); every database test now
writes into a temporary folder, and those files are removed. **Tests**: 422 passing.

## 2026-11-12 · Forex F1 data loaded; F2 shorts, leverage, conversion; F3 pre-registered

**Data (operator's run)**: Exness demo 477738622 (Exness-MT5Trial9), 15 USD, 1:200, server
clock UTC+0. 8 majors: 1h/4h/1d from 2021-11, 15m from 2022-10 (100,000-bar terminal limit).
Median bar spreads 0.65-2.9 bps (Quidax: 13-58).

**F2 built**: shorts through costs (fills move against the trade on both sides), labels,
sizing (stop above entry), NARA (signed risk, contract size, conversion), NPEI trailing (never
loosened), stress (forex shock 1%, both directions) and the backtester (margin account: only
P&L and fees move cash, notional/leverage locked as margin; JPY/CHF/CAD profits converted at
the latest USD-pair close). Spot crypto accounting unchanged and refuses shorts.
MT5 costs: no commission, measured bar spread x2, 0.5 bps slippage per side; spread limit 8 bps.
**Found**: at 15 USD a 0.01 lot with a 15-pip stop risks 1.50 USD = 10% (1% needs about
150 USD, or a cent-size contract). With 15-pip stops at 1% risk, the stress limit allows
about 2-3 forex positions at once.

**F3**: ADR 0007 pre-registers FX1 fx_trend (1h), FX2 fx_meanrev (1h), FX3 fx_london (15m),
two-sided, exits by stop/target/time only, judged on raw trades, and a new gate rule: positive
in both halves of the test period. Random baseline draws direction too.
Not modelled yet: swap (overnight financing). **Tests**: 421 passing.

## 2026-11-12 · Forex batch F1: direct MT5 history download

Decision (operator): forex through MetaTrader 5. Gaps found before any forex test: the system
is long-only, has no leverage/margin, no conversion of JPY/CHF/CAD profits to the account
currency, and no swap costs. Plan: F1 data, F2 shorts + leverage + conversion, F3 pre-registered
forex strategies and tests, F4 paper trading on an MT5 demo.

**Built (F1)**: `adapters/mt5/local.py` (official MetaTrader5 package, no bridge server; symbol
suffixes such as Exness cent "c"; broker server time corrected to UTC, detected from the latest
tick while the market is open; spread per bar) and `load_mt5_history` (resumable; 15m/1h/4h/1d;
H1 spreads of the last 30 days kept as quote samples; warns on a REAL account).
**Fixed**: `save_instrument` did not store contract size (1 for crypto, 100,000 or 1,000 for forex).
**Tests**: 403 passing (fake MT5 terminal in tests/mt5_fake.py).

## 2026-11-12 · First paper run started; quieter output

**Run 1** started 2026-11-11 20:09 UTC: NGN 20,000 = 14.30 USDT at NGN 1,398.22/USDT; all 9
Quidax order books answered. Bar 2026-11-10: 0 signals (no coin crossed above its 200-day average).

**Changed**: HTTP request lines (8 a minute) hidden; one status line per hour instead
(rate, open positions, closed trades, net result). **Tests**: 398 passing.

## 2026-11-11 · T1 passed; Quidax paper trading built

**T1 `trendfilter`** (pre-registered, daily, Gate.io prices with Quidax costs): 63 out-of-sample
trades, mean net R +0.984 (95% CI +0.370 to +1.718), random baseline +0.439, profit factor 5.2,
win rate 38.1%. PASS. **T2 `rotation`**: +0.493 (CI -0.016 to +1.113), below its baseline +0.764. FAIL.
Caveats: about 6 trades a year; results lean on a few large winners; coin list chosen today
(survivorship); Gate.io prices, not Quidax prices.

**Built**: `run_paper_quidax`. Signals from Gate.io daily bars; fills simulated at live Quidax
NGN bid/ask converted to USDT at the live USDT/NGN rate; NEXTRADE holds the stops itself (Quidax
has no exchange stop orders). Only pre-registered, rule-based strategies are allowed.

**Bugs found by the tests and fixed**:
- NAIA had its own 15 bps spread limit separate from NARA's, so every Quidax signal
  (spreads 13-58 bps) would have been rejected. NAIA now uses the venue limit (Quidax 60 bps).
- Paper orders used Gate.io's minimum quantity (0.001 ETH, about 2 USDT) instead of Quidax's
  rules (7-decimal quantities, NGN 1,250 minimum). That made small trades look unsizeable.
- Backtests were not affected: they pass no live spread to NAIA, and T1 is judged on raw trades.

**Tests**: 397 passing.

## 2026-11-11 · Confirmation failed; strategies T1 and T2 pre-registered

**Confirmation test** (decided before running): daily breakout, unchanged, on 10 unseen coins.
Result: 408 unfiltered trades, mean net R -0.144 (random baseline -0.160). The BTC/ETH/SOL
result (100 trades, +0.222, 95% CI -0.054 to +0.503) does not generalise. Breakout dropped.

**Quidax universe** (by NGN volume, spread): USDT, BTC, USDC, ETH, SOL, LTC, XRP (13.6 bps),
DASH, TRX. Others have no volume.

**Built**: pre-registered T1 `trendfilter` and T2 `rotation` (ADR 0006, written before any test),
rule-based exits in research (exit on invalidation), strategies judged on raw trades,
cross-sectional preparation hook for strategies that rank coins.

**Tests**: 391 passing.

## 2026-11-11 · Daily walk-forward with real sizing: breakout is the only candidate

| Daily, risk/trade | Strategy | OOS trades | Mean net R (95% CI) | Random baseline |
| --- | --- | --- | --- | --- |
| 0.5% | breakout | 8 | +0.292 (-0.763 to +1.332) | 6 trades, -0.187 |
| 0.5% | trend | 3 | -0.118 | 18 trades, +0.491 |
| 1.0% | breakout | 10 | +0.602 (-0.263 to +1.464) | 12 trades, -0.136 |
| 1.0% | trend | 36 | +0.109 (-0.340 to +0.560) | 81 trades, +0.126 |

**Reading**
- Trend does not beat random entries with the same exits: its profit is crypto's long-run
  drift, not its entry rule. Dropped as a candidate.
- Breakout beats random entries clearly but on 10 trades; the interval includes losses.
- Only 3 breakout folds: rolling 730-day windows rarely held the 30 signals a fold needs.

**Built**: `--expanding` (each test window trains on all earlier data, still strictly out of
sample) and `quidax_markets` (Quidax markets by NGN volume and spread).

**Next, decided before looking**: breakout is now a hypothesis chosen after seeing data.
The confirmation test runs it UNCHANGED on coins it has not seen (Quidax-listed, on Gate.io).

**Tests**: 384 passing.

## 2026-11-11 · Daily bars: first positive signal, blocked by position sizing

**Run** (Gate.io prices, Quidax costs, NGN 20,000):

| Timeframe | Strategy | Signals | Hit rate after costs | Mean net R per signal | Walk-forward trades |
| --- | --- | --- | --- | --- | --- |
| 1d | breakout | 149 | 51.0% | +0.337 | 0 (sizing) |
| 1d | trend | 286 | 40.6% | +0.182 | 0 (sizing, some low EV) |
| 1d | meanrev | 4 | 25.0% | -0.384 | 0 |
| 4h | trend | 644 | 30.4% | -0.196 | 0; unfiltered 81 trades at -0.384 R |
| 4h | breakout | 444 | 35.8% | -0.333 | 0; unfiltered 200 trades at -0.430 R |
| 4h | trend sweep reward_risk 2-5 | - | - | - | unfiltered -0.38 to -0.27 R: no plateau |

**Reading**: 4h is dead at Quidax costs. Daily breakout and trend are positive per signal
over 2013-2026, with default parameters never tuned on this data. That is a hypothesis,
not an edge yet: it has not passed walk-forward with real position sizing.

**Why 0 daily trades**: a daily stop is ~8-10% away. At 0.25% risk of NGN 20,000 (NGN 50)
the position would be ~NGN 500, below Quidax's NGN 1,250 minimum; forcing the minimum
breaks the risk limit, so NARA refuses ("size_valid", "trade_risk_within_limit").

**Built**: `scaled_limits()` and `run_backtest --risk-per-trade`: the cross-trade limits
(open risk, cluster, stress) scale with risk per trade; loss limits do not. Venue spread
limits: the Binance-calibrated 15 bps would have blocked every Quidax trade; Quidax: 60.

**Tests**: 382 passing.

## 2026-11-11 · First real research result: no-go for A, C, D at Quidax costs

**Run** (operator's PC): Gate.io prices for ETH, BTC, SOL; Quidax costs (0.15% fee per
side, 40 bps spread); NGN 20,000 as 14.27 USDT; NGN 1,250 minimum as 0.89 USDT.

| Run | Strategy | Out-of-sample trades | Random-entry baseline |
| --- | --- | --- | --- |
| 4h, 365/90-day folds (14 folds) | trend | 0 (NAIA rejected all) | 94 trades, -0.140 R |
| | breakout | 0 (NAIA rejected all) | 175 trades, -0.424 R |
| | meanrev | no signals at all | - |
| 1h, 180/45-day folds | trend | 0 | 165 trades, -0.605 R |
| | breakout | 0 | 160 trades, -0.777 R |
| | meanrev | no signals | - |

**Reading**: in every fold, the training-window hit rate implied negative expected value
after costs, so NAIA refused to trade: the no-trade rule worked as designed. Gate: FAIL.

**Built to understand it better**
- The report now shows signal counts, the hit rate and mean R after costs of every signal,
  results with the NAIA filter switched off, and NAIA's rejection reasons.
- `run_backtest --sweep param=v1,v2,...` for one strategy (look for a plateau).
- Gate.io daily bars (`load_gate_history` now loads 1h, 4h and 1d).

**Tests**: 380 passing.

## 2026-11-11 · Research history from Gate.io; Quidax limits measured

**Found on the operator's network**
- Quidax serves only its newest ~300 bars per period; `timestamp` is ignored; 4h and 1d
  return nothing. Quidax alone cannot support a backtest.
- Reachable: Gate.io (public API) and CoinGecko. Blocked: Binance, Bybit, KuCoin, OKX,
  Kraken, Coinbase. CryptoCompare needs a key.
- Gate.io candles: `[time_s, quote_volume, CLOSE, HIGH, LOW, OPEN, base_volume, closed]`,
  at most 10,000 bars back (1h about 14 months, 4h about 4.5 years), 1,000 per request.

**Built**
- `adapters/gate/client.py` (read-only research data) and `load_gate_history`.
- `run_backtest --venue gate`: Gate prices with Quidax costs (0.15% fee, Quidax spreads
  measured on the matching NGN pair), NGN 20,000 and the NGN 1,250 minimum converted to
  USDT at the latest stored USDT/NGN rate (or `--ngn-per-usdt`).
- `load_quidax_history` now requests only what Quidax serves (1h, 1m); `record_quidax`
  also stores each closed 1h bar, so real Quidax history accumulates while it runs.
- 4h frames can be built from stored 1h bars (`resample_from`).
- `.gitattributes` for consistent line endings.

**Tests**: 378 passing. Fixtures are the operator's real Gate.io responses.

**Caveat**: Gate prices are a stand-in for Quidax prices. Quidax NGN books are thinner, so
real fills will differ; paper trading on live Quidax quotes is the check.

## 2026-11-11 · Quidax market data (crypto venue switch)

**Why**: Binance, its data archive and Bybit are blocked on the operator's network (DNS
fails; Google resolves). See ADR 0005.

**Built**
- `adapters/quidax/rest.py`: Quidax client. Public: markets, tickers, k-lines, depth.
  Authenticated: wallets, create/get/list/cancel orders. Bearer auth, request spacing under
  300/minute, HTTP 444 treated as rate limiting, unknown outcomes on order timeouts.
- `adapters/quidax/markets.py`: parsing of real response formats, including the
  open-close-high-low k-line order. Market rules become Instruments (min order NGN 1,250).
- `load_quidax_history` (native 1h/4h by default, paged, resumable) and `record_quidax`
  (1m candles + spread samples every minute).
- Venue cost presets (`cost_model_for`); `run_backtest --venue quidax` uses measured
  spreads and NGN capital (default NGN 20,000), and keeps one quote currency per run.
- `CandleStore.load_frame` uses native bars at a timeframe when stored.
- Settings: empty .env values now fall back to defaults (fixes the DATABASE_URL= crash).

**Tests**: 369 passing. Fixtures are the operator's real Quidax responses.

**Not verified yet**: how far back Quidax history goes, and any authenticated call.

## 2026-11-11 · Recovery note and Phase 4 strategies

**What happened.** Between 14:14 and 14:50 an earlier build session produced code for
Phases 4 to 13 (strategies, NAIA, NARA, backtester, NCTPI, NPEI, execution, paper broker,
trading daemon, watchdog, dashboard, Telegram, model governance, MT5 bridge) but did not
update this log. A later session, unaware of it, overwrote two files:
`nextrade_core/strategies/base.py` and `baseline.py`.

**Fix.** Both files were rebuilt from how the rest of the codebase uses them (the `Signal`
fields and order, `Strategy.generate(f, symbol, tf)`, cooldowns, `is_invalidated`,
`RandomEntry.matching`). The duplicate strategy modules written by the later session were
removed. All 318 existing tests pass again, unchanged.

**Phase 4 tests rewritten** (the original strategy tests could not be confirmed to exist):
29 tests covering every entry condition of strategies A, C and D, cooldowns, filters B, E,
F and G, the random-entry baseline (deterministic per bar, symbol and seed; copies a
strategy's exits exactly), and the Phase 4 gate: signals computed on data up to bar t are
identical to the full run's signals up to t, for every strategy.

**Tests**: 347 passing.

## 2026-11-11 · Phase 3: features

**Built**
- `nextrade_core/features/indicators.py`: SMA, EMA, MACD, ADX with +DI/-DI, RSI, stochastic,
  rate of change, CCI, ATR, Bollinger bands, historical volatility, rolling percentile, OBV,
  rolling and daily VWAP, relative volume. RSI, ATR and ADX use Wilder smoothing.
- `structure.py`: swing highs and lows stamped at the bar where they are CONFIRMED (k bars
  after the pivot), higher highs and lows, structure trend, breaks of structure, Donchian
  breakout levels, distances to support and resistance in ATR units.
- `feature_set.py`: feature set fs-1. 12 level columns for strategy rules and 33 scale-free
  model columns, each tagged with a family (trend, momentum, volatility, volume, structure).
  A version hash changes with any parameter. Correlation pruning (|r| > 0.9) is fitted on
  training data only.
- `frame.py`: candles to float frame, and multi-timeframe alignment that only attaches a
  higher-timeframe bar after it has closed.
- `CandleStore.load_frame()`: any timeframe, resampled from stored 1m bars.

**Checks**
- Every classic indicator matches the independent `ta` library after warm-up (ADX, RSI and
  ATR to within 0.1%, the rest to within 0.0001%). OBV differs only by its arbitrary
  starting level; its changes match exactly.
- Look-ahead test: for four cut points, computing all 45 columns on data up to bar t gives
  exactly the full computation's row t. No column uses future data.

**Tests**: 160 passing.

## 2026-11-11 · Phase 2: market data

**Built**
- `nextrade_core.data`: Candle and Timeframe types (UTC buckets like Binance), resampling
  from complete buckets only, and a chunked `QualityScanner` (gaps, duplicates, out-of-order,
  bad OHLC, non-positive prices, negative volume, spikes, staleness, completeness).
- Binance archive client: monthly and daily kline zips, SHA-256 checksum check, disk cache,
  millisecond and microsecond timestamps.
- REST kline pagination that drops the still-forming bar.
- Websocket parsing (klines, best bid/ask) and a quote sampler.
- `services/market_recorder.py`: live recorder. Opens the socket, backfills over REST,
  then streams. Repairs jumps in bar times, reconnects with backoff, and treats a stream
  that is silent for 60 s as dead.
- Django: Candle, QuoteSample and DataQualityEvent tables; CandleStore.
- Commands: `load_binance_history` (resumable), `record_live`, `data_quality_report`.

**Tests**: 132 passing. New code covered 84-100%; the gaps are mostly error branches
for network failures inside the commands.

**Measured here**: writing 129,600 one-minute bars (90 days, one symbol) to SQLite took
10 s; reading them back and scanning took 2 s.

**Not verified yet**: real Binance archive, REST and websocket traffic (blocked from the
build machine), and the PostgreSQL path (no server here). Both are exercised only through
mocks and SQLite. Your first run is the real test.

**Decision**: ADR 0004 (1m source of truth, sampling, no partitioning yet).

## 2026-11-11 · Phases 0 and 1

**Built**
- Core domain: `Money` and rounding helpers (Decimal only, floats refused),
  venue-independent `Instrument` with order validation, deterministic client order IDs,
  `SimClock`/`WallClock`.
- Controller state machine with the spec's 8 states. LIVE needs reconciliation,
  promoted strategy and operator confirmation. RISK_SHUTDOWN and EMERGENCY are
  latched until an operator acknowledges. Halts can only escalate.
- Binance REST client with normalised errors (ADR 0002) and exchange-rule parsing.
- Django project: `market` (Instrument, VenueAccount) and `ops` (append-only
  SystemStateTransition, AuditLog); `sync_binance_instruments` command.
- Milestone 0 script `scripts/check_binance_access.py`.

**Tests**: 91 passing. Coverage 100% on money and instrument, 98% on the state
machine, 96% on the Binance client.

**Not verified yet**: the access check against real Binance. The build machine's
network blocks Binance (HTTP 403), so the script's failure path was exercised but
the success path only through mocked responses. Your run on your own network is the
real Milestone 0 result.

**Decisions**: ADR 0001 (modular monolith), 0002 (native Binance client),
0003 (Decimal money).
