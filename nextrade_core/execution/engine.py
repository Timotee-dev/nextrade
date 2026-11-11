"""Idempotent async execution (spec 5.4, 14).

Rules this engine enforces:
* Every order has a deterministic client order id. The same id is never sent twice
  unless the venue has confirmed it does not have that order.
* A timeout or 5xx on submit makes the order UNKNOWN. The engine then asks the venue
  by client order id (reconciliation) before doing anything else.
* RetryableError (nothing reached the venue) is retried with backoff.
* RejectedError is final.
* Orders on the same symbol are serialised; different symbols run concurrently.
* Every state change is reported to `on_event` for persistence and alerts.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from adapters.base import RejectedError, RetryableError, UnknownStateError

from .broker import Broker
from .orders import Order, OrderRequest, OrderStatus
from .ratelimit import TokenBucket

log = logging.getLogger("nextrade.execution")
EventHook = Callable[[Order, str], Awaitable[None] | None]


class DuplicateOrderError(Exception):
    pass


class ExecutionEngine:
    def __init__(self, broker: Broker, *, limiter: TokenBucket | None = None, max_retries: int = 3,
                 retry_delay_s: float = 0.5, reconcile_attempts: int = 5, reconcile_delay_s: float = 1.0,
                 on_event: EventHook | None = None) -> None:
        self.broker = broker
        self.limiter = limiter or TokenBucket(rate_per_s=5, burst=10)
        self.max_retries, self.retry_delay_s = max_retries, retry_delay_s
        self.reconcile_attempts, self.reconcile_delay_s = reconcile_attempts, reconcile_delay_s
        self.on_event = on_event
        self.orders: dict[str, Order] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.stats = defaultdict(int)

    async def _emit(self, order: Order, what: str) -> None:
        if self.on_event:
            result = self.on_event(order, what)
            if asyncio.iscoroutine(result):
                await result

    async def submit(self, request: OrderRequest) -> Order:
        if request.client_order_id in self.orders:
            raise DuplicateOrderError(f"{request.client_order_id} already submitted")
        order = Order(request)
        self.orders[request.client_order_id] = order
        async with self._locks[request.symbol]:
            order.transition(OrderStatus.SUBMITTING)
            await self._emit(order, "submitting")
            await self._send(order)
        return order

    async def _send(self, order: Order) -> None:
        req = order.request
        for attempt in range(self.max_retries + 1):
            await self.limiter.acquire()
            try:
                state = await self.broker.submit(req)
                self.stats["submitted"] += 1
                order.apply(state, "venue acknowledged")
                await self._emit(order, "acknowledged")
                return
            except RejectedError as exc:
                order.transition(OrderStatus.REJECTED, str(exc))
                self.stats["rejected"] += 1
                await self._emit(order, "rejected")
                return
            except (UnknownStateError, TimeoutError) as exc:
                order.transition(OrderStatus.UNKNOWN, str(exc))
                self.stats["unknown"] += 1
                await self._emit(order, "unknown")
                resend = await self.reconcile(order)
                if not resend:
                    return
                order.transition(OrderStatus.SUBMITTING, "venue confirmed absent; resending the same id")
            except RetryableError as exc:
                self.stats["retried"] += 1
                if attempt == self.max_retries:
                    order.transition(OrderStatus.REJECTED, f"gave up after retries: {exc}")
                    await self._emit(order, "rejected")
                    return
                await asyncio.sleep(self.retry_delay_s * (2 ** attempt))
        order.transition(OrderStatus.REJECTED, "submission attempts exhausted")
        await self._emit(order, "rejected")

    async def reconcile(self, order: Order) -> bool:
        """Ask the venue about an UNKNOWN order. Returns True only if the venue confirmed it
        does NOT have the order (so the same id may be sent again)."""
        for attempt in range(self.reconcile_attempts):
            try:
                state = await self.broker.get(order.request.symbol, order.client_order_id)
            except (RetryableError, UnknownStateError, TimeoutError):
                await asyncio.sleep(self.reconcile_delay_s * (attempt + 1))
                continue
            self.stats["reconciled"] += 1
            if state is None:
                return True
            order.apply(state, "reconciled with venue")
            await self._emit(order, "reconciled")
            return False
        log.error("order %s still UNKNOWN after reconciliation attempts; trading must halt", order.client_order_id)
        await self._emit(order, "reconcile_failed")
        return False

    async def refresh(self, order: Order) -> Order:
        if order.is_final:
            return order
        state = await self.broker.get(order.request.symbol, order.client_order_id)
        if state is not None:
            order.apply(state, "status poll")
            await self._emit(order, "updated")
        return order

    async def cancel(self, order: Order) -> Order:
        if order.is_final:
            return order
        async with self._locks[order.request.symbol]:
            await self.limiter.acquire()
            if order.status is not OrderStatus.UNKNOWN:
                order.transition(OrderStatus.CANCELING)
            try:
                state = await self.broker.cancel(order.request.symbol, order.client_order_id)
                order.apply(state, "cancel acknowledged")
            except RejectedError:
                await self.refresh(order)   # it may have filled meanwhile
            await self._emit(order, "cancel")
        return order

    async def cancel_remainder_after(self, order: Order, ttl_s: float) -> Order:
        """Partial-fill handling: give the order ttl_s, then cancel whatever is left."""
        await asyncio.sleep(ttl_s)
        await self.refresh(order)
        if not order.is_final:
            await self.cancel(order)
        return order

    @property
    def unresolved(self) -> list[Order]:
        return [o for o in self.orders.values() if o.status is OrderStatus.UNKNOWN]
