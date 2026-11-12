# NEXTRADE status

Last updated: 2026-11-11 (T1 passed; Quidax paper trading)

| Stage | Phase | Status |
| --- | --- | --- |
| 1. Foundation and data | 0. Venue access | Binance blocked on your network; Quidax reachable (ADR 0005) |
| | 1. Foundation | Done |
| | 2. Market data | Quidax keeps ~300 bars: recorder must run; Gate.io history for research |
| | 3. Features | Done |
| 2. Research | 4. Strategies A, C, D (+ filters B, E, F, G, random baseline) | Done, re-verified |
| | 5. NAIA (regimes, labels, models, scoring) | Built; tests pass |
| | 6. NARA (sizing, limits, stress, kill switch) | Built; tests pass |
| | 7. Backtesting + decision gate (`run_backtest`) | T1 trendfilter PASSED (63 trades, +0.98R); T2 rotation and A, C, D, breakout failed |
| 3. Trading machinery | 8. NCTPI (allocation, execution engine) | Built; tests pass |
| | 9. NPEI (health, exits) | Built; tests pass |
| | 10. Paper trading (`run_trader`, watchdog, paper broker) | `run_paper_quidax` built (T1, Quidax prices, software stops); needs months of running |
| 4. Platform and live | 11. Dashboard | Built; tests pass |
| | 12. Telegram | Built; tests pass |
| | 13. Adaptive learning (governance) | Built; tests pass |
| | 14. Controlled live trading | Not started (live Binance broker, `go_live_check`, `run_live`) |
| Forex | FX1-FX6 all FAIL (ADR 0007, 0008); FX6 break-even, to test on 2006-2021 | No forex strategy approved |
| Capacity | 100 trades/day per market | Not yet load-tested |

Tests: 428 passing, 94% line coverage. Phases 4-13 were first built in an earlier
session whose log entries were lost; see the DEVLOG recovery note.

## Open items for the operator
- Leave `python manage.py run_paper_quidax` running (start once with `--new-run`). T1 trades
  about 6 times a year, so judging it takes months, not days. Check the dashboard weekly.
- Run `python scripts\check_binance_access.py` and share the output.
- Run `python manage.py load_binance_history`, then `python manage.py data_quality_report`.
- Start `python manage.py record_live` and leave it running for 7 days; then run
  `python manage.py data_quality_report --days 7` and share the result.
- Optional: PostgreSQL for full history (Docker Desktop `docker compose up -d`, or a native
  install), then set DATABASE_URL and `pip install -r requirements-postgres.txt`.
