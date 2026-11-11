"""Milestone 0: can NEXTRADE reach Binance from this machine, and what do the rules allow?

Runs in three parts, each optional after the first:

1. Public mainnet (no keys): latency, clock offset, order rules for each symbol,
   and what the minimum order costs in naira.
2. Mainnet key check (BINANCE_API_KEY / BINANCE_API_SECRET in .env): confirms the
   key can trade spot and CANNOT withdraw. Read-only calls; places no orders.
3. Testnet order round trip (BINANCE_TESTNET_API_KEY / _SECRET, with --testnet-order):
   places a tiny limit buy far below market on the TESTNET, finds it by its
   client order id, cancels it. Fake money only.

Writes a JSON report to reports/ and exits non-zero if a required check fails.
Usage:  python scripts/check_binance_access.py [--ngn-per-usdt 1500] [--testnet-order]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from adapters.base import VenueError  # noqa: E402
from adapters.binance.filters import instrument_from_symbol, supports_oco  # noqa: E402
from adapters.binance.rest import MAINNET_URL, TESTNET_URL, BinanceRest  # noqa: E402
from nextrade_core.domain.ids import make_client_order_id  # noqa: E402

DEFAULT_SYMBOLS = ("BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,TRXUSDT,LINKUSDT,"
                   "AVAXUSDT,DOTUSDT,LTCUSDT,BCHUSDT,UNIUSDT,ATOMUSDT,NEARUSDT,APTUSDT,ARBUSDT,"
                   "OPUSDT,SUIUSDT")


def line(text: str = "") -> None:
    print(text, flush=True)


def check_public(client: BinanceRest, symbols: list[str], ngn_rate: Decimal, report: dict) -> bool:
    line("== 1. Public mainnet access ==")
    latencies = []
    for _ in range(5):
        t0 = time.perf_counter()
        client.ping()
        latencies.append((time.perf_counter() - t0) * 1000)
    offset = client.sync_time()
    med = statistics.median(latencies)
    line(f"  reachable: yes   median latency: {med:.0f} ms   clock offset: {offset} ms")
    if abs(offset) > 1000:
        line("  WARNING: your PC clock is off by more than 1 s; enable automatic time sync.")

    info = client.exchange_info(symbols)
    found = {s["symbol"]: s for s in info["symbols"]}
    missing = sorted(set(symbols) - set(found))
    rows = []
    line(f"\n  {'symbol':<10}{'status':<9}{'tick':>12}{'step':>12}{'min $':>8}{'min NGN':>10}  OCO")
    for sym in symbols:
        if sym not in found:
            continue
        raw = found[sym]
        inst = instrument_from_symbol(raw)
        min_ngn = (inst.min_notional * ngn_rate).quantize(Decimal(1))
        rows.append({"symbol": sym, "status": raw.get("status"), "tick_size": str(inst.tick_size),
                     "step_size": str(inst.step_size), "min_notional_usdt": str(inst.min_notional),
                     "min_notional_ngn": str(min_ngn), "oco_allowed": supports_oco(raw)})
        line(f"  {sym:<10}{raw.get('status', '?'):<9}{inst.tick_size:>12}{inst.step_size:>12}"
             f"{inst.min_notional:>8}{min_ngn:>10}  {'yes' if supports_oco(raw) else 'no'}")
    if missing:
        line(f"  not found on Binance: {', '.join(missing)}")
    line(f"\n  request weight used this minute: {client.used_weight_1m}")

    if rows:
        typical = statistics.median(Decimal(r["min_notional_usdt"]) for r in rows)
        line(f"  typical minimum order: {typical} USDT, about NGN {typical * ngn_rate:,.0f} "
             f"at NGN {ngn_rate}/USDT")
    report["public"] = {"latency_ms_median": round(med), "clock_offset_ms": offset,
                        "used_weight_1m": client.used_weight_1m, "symbols": rows,
                        "missing": missing}
    return bool(rows)


def check_mainnet_key(client: BinanceRest, report: dict) -> bool:
    line("\n== 2. Mainnet API key (read-only checks) ==")
    if not client.has_credentials:
        line("  skipped: BINANCE_API_KEY / BINANCE_API_SECRET not set in .env")
        report["mainnet_key"] = "skipped"
        return True
    acct = client.account()
    perms = acct.get("permissions", [])
    line(f"  canTrade: {acct.get('canTrade')}   canWithdraw (account): {acct.get('canWithdraw')}"
         f"   permissions: {perms}")
    ok = True
    try:
        r = client.api_restrictions()
        withdraw = r.get("enableWithdrawals")
        line(f"  key: spot trading {r.get('enableSpotAndMarginTrading')}, "
             f"withdrawals {withdraw}, IP restricted {r.get('ipRestrict')}")
        if withdraw:
            line("  FAIL: this key can withdraw. Create a new key with withdrawals disabled.")
            ok = False
        if not r.get("ipRestrict"):
            line("  note: no IP whitelist yet. Add one once NEXTRADE runs on a VPS with a fixed IP.")
        report["mainnet_key"] = {"can_trade": acct.get("canTrade"), "withdrawals": withdraw,
                                 "ip_restricted": r.get("ipRestrict"), "permissions": perms}
    except VenueError as exc:
        line(f"  could not read key restrictions: {exc}")
        report["mainnet_key"] = {"can_trade": acct.get("canTrade"), "restrictions_error": str(exc)}
    nonzero = [b for b in acct.get("balances", []) if Decimal(str(b["free"])) + Decimal(str(b["locked"])) > 0]
    line(f"  assets with a balance: {', '.join(b['asset'] for b in nonzero) or 'none'}")
    return ok


def check_testnet_order(client: BinanceRest, symbol: str, report: dict) -> bool:
    line(f"\n== 3. Testnet order round trip ({symbol}, fake money) ==")
    if not client.has_credentials:
        line("  skipped: BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET not set "
             "(create them at testnet.binance.vision)")
        report["testnet_order"] = "skipped"
        return True
    client.sync_time()
    inst = instrument_from_symbol(client.exchange_info([symbol])["symbols"][0])
    market = client.ticker_price(symbol)
    price = inst.normalize_price(market * Decimal("0.5"))  # far below market: will not fill
    qty = inst.min_qty_for_notional(price)
    problems = inst.order_violations(qty, price)
    if problems:
        line(f"  FAIL: could not build a valid order: {problems}")
        return False

    cid = make_client_order_id(f"m0-{int(time.time())}", "access-check", "entry")
    line(f"  placing LIMIT BUY {qty} @ {price} (market {market}), client id {cid}")
    placed = client.new_order(symbol=symbol, side="BUY", order_type="LIMIT", quantity=qty,
                              price=price, time_in_force="GTC", client_order_id=cid)
    found = client.get_order(symbol, cid)
    line(f"  placed: {placed.get('status')}   found by client id: {found is not None}")
    cancelled = client.cancel_order(symbol, cid)
    after = client.get_order(symbol, cid)
    final = after.get("status") if after else None
    line(f"  cancel response: {cancelled.get('status')}   final status: {final}")
    ok = found is not None and final == "CANCELED"
    report["testnet_order"] = {"client_order_id": cid, "qty": str(qty), "price": str(price),
                               "found_by_client_id": found is not None, "final_status": final}
    line("  PASS" if ok else "  FAIL")
    return ok


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", default=os.getenv("NEXTRADE_M0_SYMBOLS") or DEFAULT_SYMBOLS)
    parser.add_argument("--ngn-per-usdt", default=os.getenv("NGN_PER_USDT") or "1500",
                        help="your current naira rate, for display only")
    parser.add_argument("--base-url", default=os.getenv("BINANCE_BASE_URL") or MAINNET_URL)
    parser.add_argument("--testnet-order", action="store_true", help="run part 3")
    parser.add_argument("--testnet-symbol", default="BTCUSDT")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    report: dict = {"run_at": datetime.now(UTC).isoformat(), "base_url": args.base_url}
    results: dict[str, bool] = {}

    mainnet = BinanceRest(args.base_url, os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))
    try:
        try:
            results["public"] = check_public(mainnet, symbols, Decimal(args.ngn_per_usdt), report)
        except VenueError as exc:
            line(f"  FAIL: cannot reach Binance at {args.base_url}: {exc}")
            line("  Check your internet connection. If this network blocks Binance, note it in the")
            line("  review: the exchange choice may need to change. Do not try to bypass the block.")
            results["public"] = False
            report["public"] = {"error": str(exc)}
        if results["public"]:
            try:
                results["mainnet_key"] = check_mainnet_key(mainnet, report)
            except VenueError as exc:
                line(f"  FAIL: {exc}")
                results["mainnet_key"] = False
    finally:
        mainnet.close()

    if args.testnet_order:
        testnet = BinanceRest(TESTNET_URL, os.getenv("BINANCE_TESTNET_API_KEY"),
                              os.getenv("BINANCE_TESTNET_API_SECRET"))
        try:
            results["testnet_order"] = check_testnet_order(testnet, args.testnet_symbol, report)
        except VenueError as exc:
            line(f"  FAIL: {exc}")
            results["testnet_order"] = False
        finally:
            testnet.close()

    report["results"] = results
    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"m0_access_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    line(f"\nResult: {'PASS' if all(results.values()) else 'FAIL'}  {results}")
    line(f"Report saved to {out.relative_to(ROOT)}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
