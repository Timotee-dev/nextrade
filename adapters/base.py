"""Normalised venue errors shared by every adapter (Binance, MT5, paper).

The execution layer only ever handles these four kinds:

* RetryableError  - nothing happened at the venue; safe to retry the same call.
* RateLimitedError - a RetryableError with a wait time.
* RejectedError   - the venue refused; final. Do not retry.
* UnknownStateError - the request may or may not have taken effect (timeout or
  5xx on an order call). Never retry blindly: reconcile by client order id first.
"""

from __future__ import annotations


class VenueError(Exception):
    pass


class RetryableError(VenueError):
    pass


class RateLimitedError(RetryableError):
    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class RejectedError(VenueError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class UnknownStateError(VenueError):
    pass
