# ADR 0004: 1-minute candles as the stored source of truth

Date: 2026-11-11 · Status: accepted

## Decision
- Only 1m candles are stored. Every higher timeframe is resampled from them, and only
  from COMPLETE buckets (a 1h bar needs all 60 minutes). The quality report checks
  our 1h bars against Binance's own 1h bars, field by field.
- History comes from Binance's public archive (data.binance.vision), verified against
  its published SHA-256 checksum and cached in `data/raw`. Today's bars come from REST.
- Live bars come from the websocket. The socket opens BEFORE the REST backfill runs,
  so no bar can fall between the two; the unique key (instrument, timeframe, open_time)
  drops duplicates.
- Best bid/ask is sampled every 10 s, not stored tick by tick (bookTicker sends many
  updates a second). That is enough to measure typical spreads per pair and hour.
- PostgreSQL is the target database. SQLite is allowed as a fallback, limited to
  90 days of history by default.

## Not done yet (deliberately)
- Monthly table partitioning. At about 2 million rows per symbol for 4 years, the
  unique index is enough for now. Revisit if queries slow down past 50 million rows.
- Bulk loading with PostgreSQL COPY. Django's bulk_create loads about 12,000 rows/s,
  which is acceptable for a one-off load of a few tens of millions of rows.

## Consequences
- A bar is usable only after its close time; partial bars never reach signals.
- Archive timestamps switched to microseconds in 2025; the parser detects both units.
