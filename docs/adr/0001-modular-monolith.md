# ADR 0001: Modular monolith with a framework-free core

Date: 2026-11-11 · Status: accepted

## Decision
One repository, one PostgreSQL, one Redis, several process types (trader, watchdog,
web, worker). All trading logic lives in `nextrade_core`, which never imports Django.
Django provides persistence, admin, API and dashboard around it.

## Why
- The same strategy, risk and execution code must run in backtest, paper and live.
  Keeping it free of Django lets the backtester run it in memory, fast.
- Microservices add deployment and failure modes a single operator cannot justify yet.
- Clear package boundaries keep a later split into services possible.

## Consequences
- Django models convert to and from domain dataclasses (`to_domain()`).
- Business rules are tested without a database.
