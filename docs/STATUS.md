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
| Forex | FX1-FX7 all FAIL (ADR 0007-0009); search closed 2026-11-12 | No forex strategy approved |
| Crypto fast | trend/meanrev/breakout at Gate.io costs FAIL (ADR 0010) | Ruled out |
| Microstructure | Quidax book recorder + gap / market-making analysis (ADR 0011) | Recording 2-4 weeks |
| Capacity | 100 trades/day per market | Not yet load-tested |

Tests: 440 passing, 94% line coverage. Phases 4-13 were first built in an earlier
session whose log entries were lost; see the DEVLOG recovery note.

## Open items for the operator
- Keep `.venv\Scripts\python manage.py run_paper_quidax` running (no `--new-run`: it resumes
  run 1). T1 trades about 6 times a year; review after 2-3 months. Stop the laptop sleeping.
- Weekly: dashboard at http://127.0.0.1:8004/dashboard/ (`runserver 8004` in a second window).
- No real money until T1's paper results match its backtest (then Phase 14: NGN 10,000 cap).
- Forex: no approved strategy. The Exness demo can stay; nothing runs on it.
- Keep `.venv\\Scripts\\python manage.py record_books` running 2-4 weeks, then run
  `analyze_quidax_edge --days 14` and share the output.
