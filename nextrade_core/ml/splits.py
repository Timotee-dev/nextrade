"""Purged walk-forward splits (spec 12).

Each fold trains on a window of past samples and tests on the following period.
Training samples whose outcome window overlaps the test period (t_end after the
test start minus an embargo) are PURGED: their label used prices from the test
period, which would leak the answer into training.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Fold:
    number: int
    train_start: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_idx: np.ndarray
    test_idx: np.ndarray


def walk_forward(t: pd.Series, t_end: pd.Series, train: pd.Timedelta, test: pd.Timedelta,
                 step: pd.Timedelta | None = None, embargo: pd.Timedelta = pd.Timedelta(0),
                 expanding: bool = False, min_train: int = 30) -> Iterator[Fold]:
    step = step or test
    t = pd.Series(pd.to_datetime(t)).reset_index(drop=True)
    t_end = pd.Series(pd.to_datetime(t_end)).reset_index(drop=True)
    if t.empty:
        return
    first, last = t.min(), t.max()
    test_start = first + train
    n = 0
    while test_start <= last:
        test_end = test_start + test
        train_lo = first if expanding else test_start - train
        in_train = (t >= train_lo) & (t < test_start) & (t_end <= test_start - embargo)
        in_test = (t >= test_start) & (t < test_end)
        if in_train.sum() >= min_train and in_test.any():
            yield Fold(n, train_lo, test_start, test_end, np.flatnonzero(in_train), np.flatnonzero(in_test))
            n += 1
        test_start += step
