import json
from io import StringIO

from django.core.management import call_command

from nextrade_core.hft.engine import HftConfig
from nextrade_core.hft.events import Quote, Trade
from services.hft import runtime
from apps.trading.management.commands.tune_hft import grid, replay


def make_tape(folder, n=4000):
    rec = runtime.TickRecorder(folder, min_gap_s=0)
    t, p = 1_700_000_000.0, 100.0
    for i in range(n):
        t += 0.05
        p *= 1.00004 if (i // 400) % 2 else 1.0
        rec.write(Quote("BTCUSDT", t, p * 0.9999, p * 1.0001, 400, 200))
        rec.write(Trade("BTCUSDT", t, p, 1, True))
    rec.close()


def test_replay_splits_train_and_test_and_applies_parameters(tmp_path):
    make_tape(tmp_path)
    files = [str(f) for f in sorted(tmp_path.glob("*.gz"))]
    r = replay(files, {"target_mult": 2.0}, 0.6, 27.0, 0.001)
    assert set(r) == {"params", "train", "test"}
    assert r["train"]["hours"] > 0 and r["test"]["hours"] > 0, "both parts must contain tape"
    assert 1.2 < r["train"]["hours"] / r["test"]["hours"] < 1.8      # 60/40 split


def test_daily_cap_and_enabled_filters(tmp_path):
    make_tape(tmp_path)
    files = [str(f) for f in sorted(tmp_path.glob("*.gz"))]
    capped = replay(files, {"max_entries_per_day": 1}, 0.6, 27.0, 0.001)
    assert capped["train"]["closed"] + capped["test"]["closed"] <= 3
    none = replay(files, {"enabled": ("no_such_strategy",)}, 0.6, 27.0, 0.001)
    assert none["train"]["closed"] == 0 and none["test"]["closed"] == 0


def test_tune_command_reports_honestly(tmp_path, settings):
    settings.BASE_DIR = tmp_path
    make_tape(tmp_path / "data" / "ticks")
    out = StringIO()
    call_command("tune_hft", "--quick", "--workers", "1", stdout=out)
    text = out.getvalue()
    assert "settings earned a net profit on BOTH" in text and ("CANDIDATE" in text or "no edge" in text)
    assert len(grid(False)) > 50


def test_reading_a_tape_file_that_is_still_being_written(tmp_path):
    rec = runtime.TickRecorder(tmp_path, min_gap_s=0)
    t = 1_700_000_000.0
    for i in range(3000):
        t += 0.05
        rec.write(Quote("BTCUSDT", t, 100.0, 100.02, 5, 5))
    rec._fh.flush()
    f = next(tmp_path.glob("*.gz"))
    data = f.read_bytes()
    cut = tmp_path / "ticks-cut.jsonl.gz"
    cut.write_bytes(data[: int(len(data) * 0.7)])          # simulate the scalper still writing: truncated mid-stream
    got = list(runtime.read_ticks(cut))
    assert 0 < len(got) < 3000
    rec.close()
    assert len(list(runtime.read_ticks(f))) == 3000
