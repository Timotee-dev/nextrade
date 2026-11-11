# ADR 0002: Native Binance REST client instead of CCXT

Date: 2026-11-11 · Status: accepted (revisit for websockets in Phase 2)

## Decision
Binance REST calls go through a small client in `adapters/binance/rest.py`
(httpx + HMAC signing), not CCXT. This changes the spec's tech-stack line for this layer.

## Why
- Order safety depends on exact control of `newClientOrderId` (idempotency),
  on Binance error codes (-2013 "order does not exist", -1021 clock drift), and on
  the used-weight headers. The native client exposes all three directly.
- Every failure maps onto four normalised kinds (retryable, rate limited,
  rejected, unknown). Timeouts and 5xx on order calls become "unknown", which
  forces reconciliation instead of a blind retry.
- About 200 lines, fully unit-tested with mocked transports; no large dependency.

## Consequences
- MT5 and any second crypto venue get their own adapter behind the same errors.
- CCXT stays an option for quick research downloads, never for order placement.
