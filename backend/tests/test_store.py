"""Tests for the SQLite history store."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd.protocol import Status
from heatermeterd.store import Store


def _status(pit, ts_sp=225.0):
    return Status(set_point=ts_sp, pit=pit, food1=None, food2=None, ambient=72.0,
                  output_pct=30.0, fan_pct=30.0, servo_pct=0.0, lid_countdown=0)


def test_insert_and_count():
    s = Store(":memory:")
    for i in range(5):
        s.insert(_status(100 + i), ts=1000 + i)
    assert s.count() == 5


def test_history_columns_order():
    s = Store(":memory:")
    for i in range(5):
        s.insert(_status(100 + i), ts=1000 + i)
    h = s.history_columns()
    assert h["t"] == [1000, 1001, 1002, 1003, 1004]
    assert h["pit"] == [100, 101, 102, 103, 104]
    assert h["food1"] == [None] * 5


def test_history_since_filter():
    s = Store(":memory:")
    for i in range(10):
        s.insert(_status(i), ts=i)
    h = s.history_columns(since=5)
    assert h["t"] == [5, 6, 7, 8, 9]


def test_history_downsamples_to_limit():
    s = Store(":memory:")
    for i in range(100):
        s.insert(_status(i), ts=i)
    h = s.history_columns(limit=10)
    assert 0 < len(h["t"]) <= 10


def test_prune():
    s = Store(":memory:")
    for i in range(10):
        s.insert(_status(i), ts=i)
    removed = s.prune(older_than_ts=5)
    assert removed == 5
    assert s.count() == 5


def test_store_prune_downsample_and_stats():
    from heatermeterd.store import Store
    from heatermeterd.protocol import Status
    st = Store(":memory:")
    for t in range(100):
        st.insert(Status(pit=200.0), float(t), session_id=1)   # ts 0..99
    assert st.db_stats()["samples"] == 100
    assert st.prune_samples_before(50.0) == 50                 # drop ts < 50
    assert st.db_stats()["samples"] == 50
    # thin ts 50..99 to ts % 10 == 0 -> keep 50,60,70,80,90 (5)
    assert st.downsample_before(1000.0, keep_every=10) == 45
    assert st.db_stats()["samples"] == 5
    st.vacuum()
