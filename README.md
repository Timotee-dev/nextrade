# NEXTRADE AI

A research-driven crypto and forex trading platform. It starts as a crypto-only
research system on Binance spot. No live money goes in until a strategy shows a
positive out-of-sample net expectancy after costs.

The full design is in the architecture spec (Docs). Progress lives in
`docs/STATUS.md`; decisions in `docs/adr/`; the build diary in `docs/DEVLOG.md`.

## Quick start (Windows CMD)

```cmd
python -m pip install -r requirements-dev.txt
copy .env.example .env
python manage.py migrate
python -m pytest -q
python scripts\check_binance_access.py
```

## Milestone 0: Binance access check

`scripts/check_binance_access.py` has three parts:

1. **Public** (no keys needed): can this machine reach Binance, latency, clock
   offset, and the minimum order size for 20 pairs in USDT and naira.
2. **Mainnet key** (optional): put a key in `.env` with **Reading** and
   **Spot Trading** only. The script fails if the key can withdraw.
3. **Testnet order** (optional, `--testnet-order`): create a free key at
   https://testnet.binance.vision, put it in `.env`, and the script places and
   cancels a fake limit order, finding it again by its client order ID.

Each run saves a JSON report in `reports/`.

## Quidax (default crypto venue)

Binance is blocked on many Nigerian networks; NEXTRADE uses Quidax (ADR 0005).

```cmd
.venv\Scripts\python manage.py load_quidax_history
.venv\Scripts\python manage.py record_quidax
.venv\Scripts\python manage.py run_backtest --timeframe 1h
```

- `load_quidax_history` downloads native 1h and 4h candles for ETHNGN, USDTNGN, BTCNGN,
  SOLNGN, BTCUSDT and ETHUSDT (`--symbols`, `--timeframes`, `--start`). Rerun to update.
- `record_quidax` polls every minute for new 1m candles and spreads. Let it run: the
  backtester uses the measured spreads as costs.
- Quidax only keeps its newest ~300 bars, so keep `record_quidax` running to build real
  Quidax history.
- For research history use Gate.io (read-only, reachable from Nigeria):
  `load_gate_history`, then `run_backtest --venue gate --timeframe 4h`. That tests the
  strategies on Gate.io prices while charging Quidax fees and spreads.

## Phase 2: market data (Binance)

```cmd
python manage.py load_binance_history
python manage.py data_quality_report
python manage.py record_live
```

- `load_binance_history` downloads 1-minute candles from Binance's free archive and
  fills today over REST. On SQLite it loads the last 90 days; on PostgreSQL it loads
  from 2022-01-01 (about 1 GB of downloads for 10 pairs, and roughly 3 GB of database).
  It is resumable: rerun it any time and it fetches only what is missing.
- `record_live` streams new candles and bid/ask quotes until you press Ctrl+C. It
  backfills gaps by itself after any disconnect. Leave it running for the 7-day test.
- `data_quality_report --days 7` is the Phase 2 acceptance check: at least 99.9% of
  minutes present, no bad bars, and our hourly bars identical to Binance's.

### Using PostgreSQL (recommended for full history)

```cmd
docker compose up -d
python -m pip install -r requirements-postgres.txt
```

Then set `DATABASE_URL=postgres://nextrade:nextrade@localhost:5432/nextrade` in `.env`
and run `python manage.py migrate`.

## Layout

| Folder | What lives there |
| --- | --- |
| `nextrade_core/` | Framework-free domain and algorithms. Never imports Django |
| `adapters/` | Venue clients (Binance now, MT5 later) behind normalised errors |
| `apps/` | Django apps: persistence, admin, later API and dashboard |
| `services/` | Long-running processes (the live market recorder) |
| `scripts/` | One-off tools such as the access check |
| `tests/` | `unit/` (pure logic), `integration/` (Django + DB) |
| `docs/` | STATUS, DEVLOG, architecture decision records |

## Commands

- `python manage.py sync_binance_instruments` refreshes order rules (tick, step,
  minimum notional) for the configured symbols.
- `python manage.py createsuperuser`, then `python manage.py runserver` and open
  http://127.0.0.1:8000/admin/ to see instruments and the append-only state log.
