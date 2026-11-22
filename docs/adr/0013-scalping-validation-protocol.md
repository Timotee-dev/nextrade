# ADR 0013: Scalping validation protocol

Status: accepted, 2026-11-22.

## Question
Can any of the six pre-registered scalping strategies make frequent small profits AFTER costs, on
liquid crypto pairs, at 1m, 3m or 5m? No result is assumed. This protocol decides.

## Data
Binance is blocked on the operator's network and Gate keeps only ~7 days of 1m bars. Bybit public
REST gives deep 1m history without keys (`load_bybit_history`); `probe_venues` shows what the
operator's network can reach. 3m and 5m bars are built from stored 1m bars (complete buckets only).
Universe: top 30 USDT spot pairs by 24h turnover, no stablecoins or leveraged tokens, saved to
`research/universe_bybit.json`. Ranking by today's turnover is a selection effect (survivorship): reports say so.

## Method
- Parameters are the fixed, pre-registered ones (ADR 0012); the same parameters run on all three timeframes. No tuning.
- Costs: Bybit assumption 0.10% fee, 4 bps spread, 3 bps slippage per side (conservative) until `record_rest` has measured real spreads.
- Walk-forward (train 7 / test 3 days, 1 day embargo) on the DEVELOPMENT window; decision gate as for every strategy
  (min trades, bootstrap 95% CI of mean net R above zero, beats random-entry baseline, positive in both halves).
- The latest 10 days are a HOLD-OUT, opened only for strategies that passed development, once.
- "Sustains high frequency" requires: development gate pass, net > 0, >= 20 trades/day, hold-out gate pass, hold-out net > 0, >= 20 trades/day.
- Reported per strategy and timeframe: signals/day, executed trades/day, net, win rate, profit factor, max drawdown
  (from stitched realised pnl), peak/mean simultaneous positions, symbols traded, rejections.
- Spot cannot short, so `scalp_short` is skipped on spot venues.

## Limits we accept and state
18 strategy/timeframe tests means some passes can be luck: a pass must hold in continuous paper trading before it is trusted.
A short history is a weak sample. Executed trades/day are below signals/day because risk limits and minimum sizes reject signals.

## Paper trading
Only strategies approved by `approve_strategy --timeframe` (needs a recorded gate pass) run in `run_scalper --mode paper --venue bybit`,
fed by `record_rest`. Live trading stays disabled.
