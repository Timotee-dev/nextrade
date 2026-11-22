import time

from services.hft import notifier as tg

OPEN = {"symbol": "BTCUSDT", "strategy": "flow_burst", "qty": 0.0215, "fill": 100.18, "fee": 0.0022, "target": 100.4,
        "stop": 99.84, "note": "buyers in control", "open_positions": 3}
CLOSE = {"symbol": "BTCUSDT", "strategy": "flow_burst", "reason": "target", "qty": 0.0215, "entry": 100.18, "exit": 100.4,
         "gross": 0.0047, "fees": 0.0043, "net": 0.0004, "net_bps": 1.9, "hold_s": 0.9}


def test_open_message_says_what_was_bought_and_the_plan():
    t = tg.open_text(OPEN)
    for w in ("OPENED BTCUSDT", "Bought 0.0215 @ 100.18", "Target sell", "stop", "Open positions now: 3", "fee"):
        assert w in t


def test_close_message_says_what_it_sold_with_and_net():
    t = tg.close_text(CLOSE, {"closed": 5, "net": 0.01, "trades_per_hour": 90.0})
    for w in ("CLOSED BTCUSDT", "WIN", "sold @ 100.4 via target", "NET +0.0004", "Held 0.9 s", "5 closed"):
        assert w in t
    assert "LOSS" in tg.close_text({**CLOSE, "net": -0.01})


def test_notifier_batches_and_retries_and_never_blocks():
    got, fails = [], {"n": 1}

    def send(cid, text):
        if fails["n"]:
            fails["n"] -= 1
            return False
        got.append((cid, text))
        return True

    n = tg.TelegramNotifier(send, {42}, batch_s=0.2, sleep=lambda s: None)
    t0 = time.monotonic()
    for i in range(5):
        n.push(f"m{i}")
    assert time.monotonic() - t0 < 0.1
    n.close()
    assert n.sent == 1 and n.failed == 0 and got and all(f"m{i}" in got[0][1] for i in range(5)) and got[0][0] == 42


def test_digest_line_has_open_closed_pnl():
    s = {"open": 2, "closed": 4, "wins": 3, "losses": 1, "win_rate": 0.75, "net": 0.01, "gross": 0.02, "fees": 0.01,
         "equity": 27.04, "trades_per_hour": 99.0}
    t = tg.digest_text(s, [{"symbol": "A", "net_bps": 3.0}])
    assert "open 2 (A +3bp)" in t and "closed 4 (3W/1L" in t and "99/h" in t
