# ADR 0003: Decimal for every financial value; floats refused

Date: 2026-11-11 · Status: accepted

## Decision
Prices, quantities, fees and balances are `Decimal` in Python and `NUMERIC` in the
database. `to_decimal()` raises on floats. Binance JSON is parsed with
`parse_float=Decimal`. Quantities round DOWN to the venue step; reaching a venue
minimum may round up only through `min_qty_for_notional()`, and the risk engine
must then re-check the trade's risk.

## Why
Binary floats cannot represent most decimal fractions; errors compound across
thousands of fills and break reconciliation against the exchange.

## Consequences
Floats are allowed only inside feature and model computations, never in the ledger.
