"""Test that an old-schema samples table is migrated in place.

Reproduces the Pi upgrade failure: an existing hm.sqlite created by the v0.1
store (samples without a session_id column) must gain the new columns rather
than crashing the daemon on startup.
"""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd.store import Store, COLS


def test_old_schema_is_migrated():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        # Build the OLD schema: samples with ts + data cols, but NO session_id.
        conn = sqlite3.connect(path)
        old_cols = ", ".join(f"{c} REAL" for c in COLS)
        conn.execute(f"CREATE TABLE samples (ts REAL NOT NULL, {old_cols})")
        conn.execute(
            f"INSERT INTO samples (ts,{','.join(COLS)}) "
            f"VALUES ({','.join(['?'] * (len(COLS) + 1))})",
            [1000.0] + [10.0] * len(COLS))
        conn.commit()
        conn.close()

        # Opening with the new Store must migrate, not crash.
        s = Store(path)
        info = {row[1] for row in
                s.conn.execute("PRAGMA table_info(samples)").fetchall()}
        assert "session_id" in info
        # Old row is preserved; new session_id is NULL for it.
        assert s.count() == 1
        # New inserts with a session work.
        from heatermeterd.protocol import Status
        sid = s.start_session(2000)
        s.insert(Status(pit=99.0), ts=2001, session_id=sid)
        assert s.count(session_id=sid) == 1
        s.close()
    finally:
        os.unlink(path)
