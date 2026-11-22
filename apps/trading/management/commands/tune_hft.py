"""Honest tuning of the HFT scalper on recorded real tape: train on the earlier part, confirm on the later part it never saw.

    python manage.py tune_hft                   # all recorded tape, built-in grid
    python manage.py tune_hft --split 0.6 --min-trades 15

A setting is only called a CANDIDATE when it earns a net profit (after fees, spread and slippage) on BOTH parts and has enough trades in each.
No candidate means no evidence of an edge: the right answer is then to keep paper trading or stop. Everything is replayed with the same
costs and safeguards as the live engine. This is research on past tape: a candidate still has to prove itself live on new data.
"""

from __future__ import annotations

import itertools
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from nextrade_core.hft.engine import HftConfig
from nextrade_core.hft.market import SymbolState
from services.hft import runtime

STRATS = ["flow_burst", "book_pressure", "dip_bounce", "range_break"]


def grid(quick: bool) -> list[dict]:
    if quick:
        return [{}, {"target_mult": 2.0}, {"target_mult": 2.0, "hold_mult": 2.0, "min_vol_bps": 3.0}]
    out = []
    for tm, hm, vol, strat in itertools.product((1.0, 1.5, 2.0, 3.0), (1.0, 2.0), (0.0, 3.0, 6.0), (None, *STRATS)):
        out.append({"target_mult": tm, "hold_mult": hm, "min_vol_bps": vol, **({"enabled": (strat,)} if strat else {})})
    return out


def replay(files: list[str], params: dict, split: float, cash: float, fee: float) -> dict:
    """Run one setting over the tape once; the account is reset at the split so the two halves are independent."""
    paths = [Path(f) for f in files]
    first = next(iter(runtime.read_ticks(paths[0]))).ts
    last = first
    for ev in runtime.read_ticks(paths[-1]):
        last = ev.ts
    cut = first + (last - first) * split
    cfg = HftConfig(**params)
    out = {}
    for part in ("train", "test"):
        engine = runtime.build_engine(cfg, [], {}, cash, lambda r: None, fee, fee)
        prev_q = 0
        for p in paths:
            for ev in runtime.read_ticks(p):
                if (ev.ts < cut) != (part == "train"):
                    continue
                if ev.symbol not in engine.market.state:
                    engine.market.state[ev.symbol] = SymbolState(ev.symbol)
                engine.on_event(ev)
                q = int(ev.ts * 4)
                if q != prev_q:
                    prev_q = q
                    engine.on_timer(ev.ts)
        s = engine.summary()
        out[part] = {k: s[k] for k in ("closed", "net", "gross", "fees", "win_rate", "profit_factor", "avg_net_bps", "hours")}
    return {"params": params, **out}


def _job(args):
    return replay(*args)


class Command(BaseCommand):
    help = "Train/test tuning of the HFT scalper on recorded tape."

    def add_arguments(self, parser):
        parser.add_argument("--folder", default="")
        parser.add_argument("--split", type=float, default=0.6)
        parser.add_argument("--min-trades", type=int, default=15)
        parser.add_argument("--cash", type=float, default=27.0)
        parser.add_argument("--fee", type=float, default=0.001)
        parser.add_argument("--quick", action="store_true", help="3 settings only (smoke test)")
        parser.add_argument("--workers", type=int, default=4)

    def handle(self, *args, **o):
        folder = Path(o["folder"]) if o["folder"] else Path(settings.BASE_DIR) / "data" / "ticks"
        files = sorted(str(f) for f in folder.glob("ticks-*.jsonl.gz"))
        if not files:
            raise CommandError(f"no tick files in {folder}: let the scalper run first, it records the tape")
        g = grid(o["quick"])
        self.stdout.write(f"Testing {len(g)} settings on {len(files)} hourly tape file(s), train {o['split']:.0%} / test {1 - o['split']:.0%} ...")
        jobs = [(files, p, o["split"], o["cash"], o["fee"]) for p in g]
        with ProcessPoolExecutor(max_workers=max(1, o["workers"])) as ex:
            res = list(ex.map(_job, jobs))
        mt = o["min_trades"]
        rows = []
        for r in res:
            tr, te = r["train"], r["test"]
            ok = tr["closed"] >= mt and te["closed"] >= mt and tr["net"] > 0 and te["net"] > 0
            rows.append((ok, te["net"] + tr["net"], r))
        rows.sort(key=lambda x: (not x[0], -x[1]))
        self.stdout.write("\nsetting | TRAIN trades net(USDT) bps/trade | TEST trades net bps/trade | verdict")
        for ok, _, r in rows[:15]:
            tr, te = r["train"], r["test"]
            f = lambda x: "-" if x is None else f"{x:+.1f}"  # noqa: E731
            self.stdout.write(f"{json.dumps(r['params'], default=list)} | {tr['closed']} {tr['net']:+.4f} {f(tr['avg_net_bps'])} | "
                              f"{te['closed']} {te['net']:+.4f} {f(te['avg_net_bps'])} | {'CANDIDATE' if ok else 'no edge'}")
        n = sum(1 for ok, *_ in rows if ok)
        self.stdout.write(f"\n{n} of {len(rows)} settings earned a net profit on BOTH the training and the unseen test part (min {mt} trades each).")
        if n == 0:
            self.stdout.write("No setting shows an edge after costs on this tape. That is a real result: do not go live; keep gathering tape or stop.")
        else:
            self.stdout.write("Candidates are NOT proof: the tape is short and many settings were tried. Confirm on fresh live paper data before trusting them.")
