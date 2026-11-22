# ADR 0014: Live-market paper scalping (exploratory trial)

Status: accepted, 2026-11-22. Operator authorisation: "move NEXTRADE into LIVE-MARKET PAPER TRADING ... this does NOT
mean real-money trading."

## What changed
- **One net-profit rule for backtest and paper trader** (`nextrade_core/scalping/net_exit.py`). A signal is refused at
  entry if its own target nets less than 15 bps after spread, slippage and both fees (`rejected_by_net_target` /
  `skip:net_target_below_costs`). An open position takes profit only when its NET figure (after the exit fee, half spread,
  slippage and the entry fee) cleared 8 bps of notional and then gave back 35% of its peak. Gross profit is never a success;
  win/loss is classified on net P&L. Stops and time limits remain risk exits and may close at a net loss.
  The trader checks this every poll against the live quote; the backtester at every bar close. `verify_scalping_behaviour`
  and `validate_scalping` now run it.
- **Spot short signals are skipped and counted** (`skipped_short_unsupported`), never bought and never counted as signals.
- **Time stop works for positions stored without a signal object** (max hold is kept in the stored thesis).
- **Duplicate-signal protection**: a signal key that already opened a position is never traded again, including after a restart.
- **Risk**: NARA gains optional `max_exposure` and `max_symbol_exposure`. Paper trial defaults: 0.10% risk per trade, 5
  positions, 80% total and 30% per-market exposure, 2% daily loss pause, 5% weekly and 10% drawdown shutdown, re-entry guard
  (15 min cooldown, 45 min after a loss, 1 h flip, 3-loss streak pause). NARA's stress-loss rule is unchanged and is the
  binding limit on position size; it is not bypassed.
- **Capital**: NGN 40,000 at an ASSUMED 1,480 NGN/USDT = 27.03 USDT. The rate is an input (`--ngn-per-usdt`), not a measurement.
- **Exploratory-trial authorisation** (`authorise_paper_trial`): paper only, `live=false`, `trial=true`, evidence text says it
  is not a decision-gate pass. Live remains impossible: needs `NEXTRADE_LIVE_ENABLED=yes`, a live broker (not built) and a
  gate-based live approval.
- **No real order can be sent** (`paper_safety.py`): three layers (config flags, read-only data clients, runtime write guard
  that makes any non-GET request to an exchange host raise). `run_trader` and `run_paper_scalper` refuse to start if a check fails.
- **Supervisor** (`run_paper_scalper`): recorder + trader (+ Telegram bot if configured); crashed processes restart with backoff;
  one instance at a time; stop file / Ctrl+C / `stop_paper_scalper` for a clean stop. Monitoring: `paper_status`.

## Known limits (stated, not hidden)
- The six strategies are NOT validated. This trial gathers real-time evidence; it proves nothing about profit.
- `scalp_vol` is designed for 15m bars; the single trader process runs 5m, so it is not running. `scalp_short` runs but, on spot,
  every signal is skipped and counted.
- With NGN 40,000 the account holds a few small positions. Fees (0.10% per side) plus spread and slippage cost ~0.31% per round
  trip; a scalp must move more than that before it earns anything.
- Paper fills use the recorded best bid/ask (sampled about every 20 s) plus modelled slippage; real fills can be worse.
- Everything in the test suite and the sandbox smoke run used simulated feeds; the real Bybit feed is only reachable from the operator's machine.
- 1-minute REST polling (20 s) is slower than a websocket; very fast exits are evaluated at that cadence.

## Evidence rule
Paper mode continues until there are enough real-time trades (hundreds, across several days and market conditions) whose NET
result after all costs is positive with a confidence interval above zero. Negative results are recorded, not hidden.
