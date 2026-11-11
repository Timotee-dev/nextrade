# NEXTRADE development log

Newest first.

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
