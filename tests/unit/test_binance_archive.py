import hashlib
import io
import zipfile
from datetime import date

import httpx
import pytest

from adapters.base import RetryableError
from adapters.binance.archive import (ArchiveIntegrityError, BinanceArchive, daily_path, monthly_path,
                                      parse_kline_zip)
from nextrade_core.data.candles import Timeframe
from tests.helpers import minutes, to_row


def make_zip(rows, header=False, name="X.csv"):
    lines = []
    if header:
        lines.append("open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tq,ignore")
    lines += [",".join(r) for r in rows]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, "\n".join(lines) + "\n")
    return buf.getvalue()


def test_paths():
    assert monthly_path("BTCUSDT", Timeframe.M1, 2024, 3) == \
        "/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-03.zip"
    assert daily_path("BTCUSDT", Timeframe.M1, date(2026, 11, 2)).endswith("BTCUSDT-1m-2026-11-02.zip")


@pytest.mark.parametrize("micros,header", [(False, False), (True, False), (True, True)])
def test_parse_handles_ms_microseconds_and_headers(micros, header):
    bars = minutes(3)
    parsed = parse_kline_zip(make_zip([to_row(c, micros) for c in bars], header), "BTCUSDT", Timeframe.M1)
    assert parsed == bars


class FakeArchive:
    def __init__(self, files):
        self.files = files
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        body = self.files.get(request.url.path)
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)


def served(path, data, checksum=None):
    digest = checksum or hashlib.sha256(data).hexdigest()
    return {path: data, path + ".CHECKSUM": f"{digest}  {path.rsplit('/', 1)[1]}".encode()}


def test_download_verify_cache(tmp_path):
    bars = minutes(5)
    path = monthly_path("BTCUSDT", Timeframe.M1, 2026, 1)
    fake = FakeArchive(served(path, make_zip([to_row(c) for c in bars])))
    archive = BinanceArchive(tmp_path, base_url="https://x.test", transport=httpx.MockTransport(fake))
    assert archive.monthly("BTCUSDT", Timeframe.M1, 2026, 1) == bars
    calls = fake.calls
    assert archive.monthly("BTCUSDT", Timeframe.M1, 2026, 1) == bars
    assert fake.calls == calls  # second read from cache
    assert archive.bytes_downloaded > 0


def test_not_published_returns_none(tmp_path):
    archive = BinanceArchive(tmp_path, base_url="https://x.test", transport=httpx.MockTransport(FakeArchive({})))
    assert archive.daily("BTCUSDT", Timeframe.M1, date(2026, 1, 1)) is None


def test_checksum_mismatch_rejected_and_not_cached(tmp_path):
    path = monthly_path("BTCUSDT", Timeframe.M1, 2026, 1)
    fake = FakeArchive(served(path, make_zip([to_row(minutes(1)[0])]), checksum="0" * 64))
    archive = BinanceArchive(tmp_path, base_url="https://x.test", transport=httpx.MockTransport(fake))
    with pytest.raises(ArchiveIntegrityError):
        archive.monthly("BTCUSDT", Timeframe.M1, 2026, 1)
    assert not list(tmp_path.rglob("*.zip"))


def test_server_errors_are_retryable(tmp_path):
    archive = BinanceArchive(tmp_path, base_url="https://x.test",
                             transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(RetryableError):
        archive.monthly("BTCUSDT", Timeframe.M1, 2026, 1)
