# ADR 0015: event-driven high-frequency paper scalper

Status: accepted (paper only). Date: 2026-11-22.

## Context
The 5-minute paper scalper (ADR 0014) scans on closed 5m bars, exits on a 5 s poll of REST quotes and re-enters after a 15 minute
cooldown. That design cannot approach ~100 completed trades per hour. The operator asked for a faster, event-driven system.

## Decision
A second, separate engine. The 5-minute scalper is untouched and is refused at start-up while it runs (one Telegram bot per token).
* `adapters/bybit/ws.py`: public spot WebSocket (`orderbook.1`, `publicTrade`), reconnect with backoff, silence watchdog, ping.
  Only `subscribe`/`unsubscribe`/`ping` can ever be sent (`check_op`); order operations raise.
* `nextrade_core/hft/`: `market` (per-symbol rolling state), `strategies` (four fast, pre-registered), `exchange` (paper fills against the
  real best bid/ask: 150 ms latency, slippage, size cap, stale-book reject, trade-through rule for resting limits, cancel latency),
  `engine` (position manager, risk guards).
* `services/hft/runtime.py`: asyncio loop, hourly gzip tick recorder (real tape for later validation and `replay_hft`), JSONL event log,
  state snapshot (restart recovery), status file, periodic Telegram summary through the existing `Alert` outbox.
* Every quote/trade event of a symbol completes due orders and manages that symbol's position (stop, net-profit trail, time limit,
  resting profit target). Positions on different symbols are independent; opening one never waits on another.

## Safeguards kept (loss protection, not throttles)
Max concurrent positions (12) and exposure (90%), one position per market, risk-based sizing, duplicate-signal block, 10 s / 30 s
per-market cooldowns, per-strategy pause after 6 straight net losses, daily-loss entry pause (3%, resumes next UTC day), drawdown halt
(10%) that flattens and stays latched. These are operator-level HFT settings; NARA's crash-stress sizing is NOT applied per tick.

## Costs (assumptions, Bybit spot taker/maker 0.10% per side)
Round trip fees 0.20% plus half-spread plus slippage: ~21-25 bps before any edge. At ~2 USDT per position a trade costs ~0.005 USDT in fees.

## Measured so far (synthetic, NOT evidence of profit)
Random-walk tape, 40 markets, 1 simulated hour (benchmark in this ADR's commit): 144 closed trades, 12 simultaneous positions at most,
net -0.79 USDT on a 27 USDT account (fees 0.61), win rate 10%. The engine can reach and exceed 100 trades/hour mechanically; with no edge
every trade loses its costs. Handling averaged 44 us per event (about 20,000 events/s capacity against ~500/s needed).
No real-tape result exists yet. The four strategies are unvalidated.

## Known gaps
Maker queue position is not modelled beyond the conservative trade-through rule. Fee tier is the default retail tier. The existing
Telegram `/status` commands read the 5-minute scalper's DB records, not the HFT engine; HFT stats arrive as periodic summaries and in
`hft_status`. Optimisation needs recorded real tape first.
