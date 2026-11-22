# NEXTRADE audit against the Master Engineering Prompt

Date: 2026-11-14. Method: read the repository (code, models, commands, tests, ADRs, STATUS).
Nothing was changed for this audit. Evidence level is stated per item: **tested** means a test
exists and passes (441 passing); **built** means code exists; **evidence** means validated on data.

## 1. What exists (maps to prompt sections)

| Prompt item | Where | State |
| --- | --- | --- |
| A. Market data | `adapters/{quidax,gate,binance,mt5}`, `apps/market`, `nextrade_core/data/quality.py`, `record_books`, `record_quidax`, `load_*_history` | Built, tested. Candle quality checker exists (gaps, spikes, stale). |
| B. Market analysis | `nextrade_core/features`, `regime/classifier.py` | Built (rule-based regimes + transitions). |
| C. Strategy research | `strategies/`, `backtest/research.py`, `run_backtest`, ADRs 0006-0010 | Built; walk-forward, random baseline, decision gate, hold-out. 1 strategy (T1) passed; 13+ failed and are recorded. |
| D. NAIA | `naia/engine.py`, `scoring.py`, `ml/` (logistic/LightGBM, walk-forward, registry) | Built, tested. Can reject all signals. |
| E. Risk (NARA) | `nara/` limits, sizing, portfolio, stress, kill switch | Built, tested. Independent of NAIA. |
| F. Execution/portfolio (NCTPI) | `nctpi/allocation.py`, `execution/`, paper broker | Built, tested. Dynamic limits via risk budget. |
| G. Learning/evaluation | `ml/registry.py`, `ModelVersion`, governance tests | Built; promotion is gated. |
| NPEI exits | `npei/exits.py` (trailing, health) | Built; rule-based, not predictive. |
| Telegram, dashboard | `apps/notify`, `apps/dashboard` | Built, tested. |
| Quidax vs Gate gap, market making | `micro/edge.py`, `analyze_quidax_edge` | Built, tested on synthetic data. **No real recorded data analysed yet.** |

## 2. Gaps against the prompt (highest priority first)

1. **Recorder data reliability (prompt s.4, Phase 2).** `record_books` stores every poll with
   no duplicate check, no stale/crossed-book rejection, no per-market failure counters in the DB,
   no backoff after repeated errors, and `DataQualityEvent` is not used for book data. Failures
   are only counted in a log line. Risk: a feed outage looks like "no gap" or a stuck price looks
   like a real quote. This is the first fix because all microstructure evidence depends on it.
2. **Opportunity ledger (s.5, s.9).** `OpportunityRecord` exists for trading signals, but
   cross-exchange gaps and market-making candidates are not stored with accept/reject reasons.
   The analysis reports aggregates only.
3. **Market-making realism (s.6).** Fills use trade-through, but there is no queue-position
   sensitivity, partial-fill, cancellation-rate or adverse-selection-after-fill report.
   Quidax trade prints are not recorded, so fills can only be inferred from book changes.
4. **Gap analysis (s.5).** Missing: duration of divergence, convergence speed, depth-limited
   size, transfer/withdrawal cost model, execution-delay sensitivity.
5. **Performance reporting (s.13).** Dashboard and `send_report` exist, but no single report
   that separates backtest / simulated / paper / live and shows Sharpe, Sortino, profit factor,
   exposure, per-regime and per-model results.
6. **Monitoring (s.16).** No feature-drift or prediction-drift monitor, no automatic
   per-strategy disable on deterioration (kill switch is portfolio-wide).
7. **Regime layer (s.7).** Rule-based only; no measurement of which strategy works in which
   regime, and the "uncertain" state needs a confidence rule check.
8. **Not in the stack, and deliberately not added:** Celery, Redis, XGBoost, PyTorch. Prompt
   s.17 says not to add infrastructure the project cannot operate; one laptop with SQLite and
   `manage.py` loops does the job. LightGBM and scikit-learn already cover the model list.
9. **Technical debt:** Phases 4-13 DEVLOG entries were lost in an earlier session (recovery note
   exists); SQLite is used locally, PostgreSQL path exists but is not exercised in CI here;
   `research/experiments` is empty in this copy because test records are kept out of the repo.

## 3. What the evidence supports today

- Only T1 (crypto trend filter, ~6 trades/year at Quidax costs) passed the decision gate.
  Expected profit at NGN 20,000 is small (about NGN 1,200 a year). It is in paper trading.
- All forex and fast-crypto strategies failed after costs. Those results stay on record.
- The Quidax microstructure question is **open**: the recorder was started but no analysis of
  real data has been shared. No claim of edge or no-edge is supported yet.
- No live trading is authorised, and none is built into the paper path.

## 4. Proposed plan (incremental, nothing destructive)

1. **Phase 2 now:** harden `record_books` (validation, dedupe, crossed/stale rejection, backoff,
   `DataQualityEvent` rows, per-market health in the status line). Backward compatible; same
   command and options. Migration adds only nullable/new tables.
2. **Phase 3:** `GapObservation` ledger (accepted and rejected with reason), duration and
   convergence metrics, depth-limited sizing, delay sensitivity; MM queue/adverse-selection
   sensitivity report. Run on real data once ≥1 week is recorded.
3. **Phase 4-5:** unified performance report (four separated modes); regime-by-strategy table.
4. **Phase 6-8:** NAIA reason logging for every decision, per-strategy disable, drift monitor.
5. **Phase 9-11:** paper evaluation, Telegram alerts mapping, readiness report.

Each step ships with tests and an ADR where a decision is pre-registered. No destructive
changes are proposed in step 1.
