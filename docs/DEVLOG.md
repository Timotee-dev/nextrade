# NEXTRADE development log

Newest first.

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
