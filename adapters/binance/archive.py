"""Binance public data archive (data.binance.vision): free historical klines.

Monthly files cover complete months; daily files cover the current month until
the monthly file is published. Every zip is verified against its published
SHA-256 checksum before use, and cached on disk so reruns download nothing twice.
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path

import httpx

from adapters.base import RetryableError, VenueError
from nextrade_core.data.candles import Candle, Timeframe

from .klines import row_to_candle

ARCHIVE_URL = "https://data.binance.vision"


class ArchiveIntegrityError(VenueError):
    pass


def monthly_path(symbol: str, tf: Timeframe, year: int, month: int) -> str:
    name = f"{symbol}-{tf.value}-{year}-{month:02d}.zip"
    return f"/data/spot/monthly/klines/{symbol}/{tf.value}/{name}"


def daily_path(symbol: str, tf: Timeframe, day: date) -> str:
    name = f"{symbol}-{tf.value}-{day:%Y-%m-%d}.zip"
    return f"/data/spot/daily/klines/{symbol}/{tf.value}/{name}"


def parse_kline_zip(data: bytes, symbol: str, tf: Timeframe) -> list[Candle]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ArchiveIntegrityError(f"expected one CSV in archive, found {names}")
        text = zf.read(names[0]).decode()
    candles = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip().isdigit():  # skip a header row if present
            continue
        candles.append(row_to_candle(row, symbol, tf))
    return candles


class BinanceArchive:
    def __init__(self, cache_dir: Path, base_url: str = ARCHIVE_URL,
                 transport: httpx.BaseTransport | None = None, timeout_s: float = 60.0) -> None:
        self.cache_dir = Path(cache_dir)
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport,
                                  follow_redirects=True)
        self.bytes_downloaded = 0

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str) -> bytes | None:
        try:
            response = self._http.get(path)
        except httpx.TransportError as exc:
            raise RetryableError(f"archive {path}: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RetryableError(f"archive {path}: HTTP {response.status_code}")
        self.bytes_downloaded += len(response.content)
        return response.content

    def fetch(self, path: str) -> bytes | None:
        """Verified zip bytes, from cache or download. None if Binance has not published it."""
        local = self.cache_dir / path.lstrip("/")
        if local.exists():
            return local.read_bytes()
        data = self._get(path)
        if data is None:
            return None
        checksum = self._get(path + ".CHECKSUM")
        if checksum is None:
            raise ArchiveIntegrityError(f"no checksum published for {path}")
        expected = checksum.decode().split()[0].strip().lower()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise ArchiveIntegrityError(f"checksum mismatch for {path}")
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        return data

    def monthly(self, symbol: str, tf: Timeframe, year: int, month: int) -> list[Candle] | None:
        data = self.fetch(monthly_path(symbol, tf, year, month))
        return None if data is None else parse_kline_zip(data, symbol, tf)

    def daily(self, symbol: str, tf: Timeframe, day: date) -> list[Candle] | None:
        data = self.fetch(daily_path(symbol, tf, day))
        return None if data is None else parse_kline_zip(data, symbol, tf)
