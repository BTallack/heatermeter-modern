"""SQLite storage: status samples, cook sessions, and timeline notes.

* ``samples`` - one row per ``$HMSU`` update, tagged with the session it belongs
  to. Reads return columnar data ready for the chart.
* ``sessions`` - a "cook": auto-started when data begins flowing, auto-closed
  after an idle gap. Named, described, searchable. (FireBoard's core idea.)
* ``notes`` - timestamped annotations ("wrapped the brisket") rendered as
  markers on the chart.

A single connection guarded by a lock is used; writes happen ~once a second and
are tiny. History reads should be wrapped in ``asyncio.to_thread`` by callers on
the event loop.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Optional

from .protocol import Status

# Columns persisted from each Status, in order.
COLS = [
    "set_point", "pit", "food1", "food2", "ambient",
    "output_pct", "fan_pct", "servo_pct", "lid_countdown",
]


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        # Create the parent dir for a file-backed DB if missing, so deployment
        # does not depend on a separate mkdir step (sqlite cannot create a
        # database inside a non-existent directory).
        self.path = path
        # Where note photos are stored (alongside the DB). None for in-memory.
        self.photos_dir = (None if path == ":memory:"
                           else os.path.join(os.path.dirname(os.path.abspath(path)),
                                             "photos"))
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        cols_ddl = ", ".join(f"{c} REAL" for c in COLS)
        with self.lock:
            self.conn.execute(
                f"CREATE TABLE IF NOT EXISTS samples ("
                f"ts REAL NOT NULL, session_id INTEGER, {cols_ddl})"
            )
            # Migrate an older samples table created before session tagging
            # existed. CREATE TABLE IF NOT EXISTS won't add columns to a table
            # that already exists, so add any missing columns explicitly. This
            # makes upgrades on a Pi with an existing hm.sqlite self-healing.
            existing = {row[1] for row in
                        self.conn.execute("PRAGMA table_info(samples)").fetchall()}
            wanted = {"session_id": "INTEGER"}
            for c in COLS:
                wanted[c] = "REAL"
            for col, coltype in wanted.items():
                if col not in existing:
                    self.conn.execute(
                        f"ALTER TABLE samples ADD COLUMN {col} {coltype}")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_samples_session ON samples(session_id)"
            )
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT, description TEXT, "
                "started_ts REAL NOT NULL, ended_ts REAL, "
                "share_token TEXT, "
                "completed_ts REAL, completed_reason TEXT)"
            )
            # Migrate older sessions tables that predate later columns.
            scols = {row[1] for row in
                     self.conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "share_token" not in scols:
                self.conn.execute("ALTER TABLE sessions ADD COLUMN share_token TEXT")
            if "completed_ts" not in scols:
                self.conn.execute("ALTER TABLE sessions ADD COLUMN completed_ts REAL")
            if "completed_reason" not in scols:
                self.conn.execute(
                    "ALTER TABLE sessions ADD COLUMN completed_reason TEXT")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS notes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id INTEGER, ts REAL NOT NULL, "
                "channel TEXT, text TEXT NOT NULL)"
            )
            # Migrate older notes tables that predate photo attachments.
            ncols = {row[1] for row in
                     self.conn.execute("PRAGMA table_info(notes)").fetchall()}
            if "photo" not in ncols:
                self.conn.execute("ALTER TABLE notes ADD COLUMN photo TEXT")
            # Saved cook-program templates (stages stored as a JSON blob).
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS programs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT NOT NULL, stages_json TEXT NOT NULL, "
                "created_ts REAL NOT NULL)"
            )
            # Auto timeline events (lid open, stall, target reached, setpoint
            # change, stage change, ...) rendered as markers on the graph.
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id INTEGER, ts REAL NOT NULL, "
                "kind TEXT NOT NULL, channel TEXT, label TEXT, value REAL)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)"
            )
            self.conn.commit()

    # -- samples -----------------------------------------------------------

    def insert(self, status: Status, ts: float,
               session_id: Optional[int] = None) -> None:
        cols = ["ts", "session_id"] + COLS
        vals = [ts, session_id] + [getattr(status, c) for c in COLS]
        placeholders = ",".join("?" * len(cols))
        with self.lock:
            self.conn.execute(
                f"INSERT INTO samples ({','.join(cols)}) VALUES ({placeholders})",
                vals,
            )
            self.conn.commit()

    def count(self, session_id: Optional[int] = None) -> int:
        with self.lock:
            if session_id is None:
                return self.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            return self.conn.execute(
                "SELECT COUNT(*) FROM samples WHERE session_id=?", (session_id,)
            ).fetchone()[0]

    def db_stats(self) -> dict:
        """Sizing info for the storage UI: sample/session/note counts, oldest
        sample timestamp, and the on-disk file size."""
        with self.lock:
            samples = self.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            sessions = self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            try:
                notes = self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            except Exception:
                notes = 0
            oldest = self.conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
        size = 0
        try:
            if self.path and self.path != ":memory:":
                size = os.path.getsize(self.path)
        except OSError:
            pass
        return {"samples": samples, "sessions": sessions, "notes": notes,
                "oldest_ts": oldest, "size_bytes": size}

    def prune_samples_before(self, cutoff_ts: float) -> int:
        """Delete samples older than *cutoff_ts*; return the number removed."""
        with self.lock:
            cur = self.conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff_ts,))
            self.conn.commit()
            return cur.rowcount or 0

    def downsample_before(self, cutoff_ts: float, keep_every: int = 60) -> int:
        """Thin samples older than *cutoff_ts* to roughly one per *keep_every*
        seconds (keeps long history at low resolution). Returns rows removed."""
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM samples WHERE ts < ? AND "
                "CAST(ts AS INTEGER) % ? != 0", (cutoff_ts, max(2, int(keep_every))))
            self.conn.commit()
            return cur.rowcount or 0

    def vacuum(self) -> None:
        """Reclaim disk space after deletes (can take a moment on a big DB)."""
        with self.lock:
            self.conn.execute("VACUUM")
            self.conn.commit()

    def history_columns(self, since: Optional[float] = None, limit: int = 5000,
                        session_id: Optional[int] = None) -> dict:
        """Return {"t": [...], "<col>": [...], ...}. Filter by *since* (epoch) or
        *session_id*. If more than *limit* rows match, stride down to ~*limit*."""
        query = f"SELECT ts,{','.join(COLS)} FROM samples"
        clauses, params = [], []
        if since is not None:
            clauses.append("ts>=?")
            params.append(since)
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(session_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY ts"
        with self.lock:
            rows = self.conn.execute(query, params).fetchall()

        if limit and len(rows) > limit:
            step = len(rows) // limit + 1
            rows = rows[::step]

        out: dict = {"t": []}
        for c in COLS:
            out[c] = []
        for r in rows:
            out["t"].append(r[0])
            for c in COLS:
                out[c].append(r[c])
        return out

    def recent_series(self, column: str, seconds: float, now: float):
        """Return (timestamps, values) for one column over the trailing window.
        Used by the predictor. *column* must be in COLS."""
        if column not in COLS:
            raise ValueError(f"unknown column {column!r}")
        with self.lock:
            rows = self.conn.execute(
                f"SELECT ts,{column} FROM samples WHERE ts>=? ORDER BY ts",
                (now - seconds,),
            ).fetchall()
        return [r["ts"] for r in rows], [r[column] for r in rows]

    def prune(self, older_than_ts: float) -> int:
        """Delete samples older than *older_than_ts*. Returns rows removed."""
        with self.lock:
            cur = self.conn.execute("DELETE FROM samples WHERE ts<?", (older_than_ts,))
            self.conn.commit()
            return cur.rowcount

    # -- sessions ----------------------------------------------------------

    def start_session(self, ts: float, name: Optional[str] = None) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO sessions (name, started_ts) VALUES (?, ?)",
                (name, ts),
            )
            self.conn.commit()
            return cur.lastrowid

    def close_session(self, session_id: int, ts: float) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET ended_ts=? WHERE id=? AND ended_ts IS NULL",
                (ts, session_id),
            )
            self.conn.commit()

    def mark_completed(self, session_id: int, ts: float,
                       reason: Optional[str] = None) -> None:
        """Stamp a cook as complete (Meater-style: food pulled). Set once; the
        record stays open (ended_ts) until the real close via the idle gap, so
        the controller can keep streaming without spawning a new session."""
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET completed_ts=?, completed_reason=? "
                "WHERE id=? AND completed_ts IS NULL",
                (ts, reason, session_id),
            )
            self.conn.commit()

    def last_sample(self, session_id: int):
        """Return the most recent sample row for a session, or None."""
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM samples WHERE session_id=? ORDER BY ts DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def open_session(self):
        """Return the currently-open session row (ended_ts IS NULL) or None."""
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE ended_ts IS NULL ORDER BY started_ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, search: Optional[str] = None, limit: int = 200) -> list:
        query = ("SELECT s.*, "
                 "(SELECT COUNT(*) FROM samples WHERE session_id=s.id) AS sample_count "
                 "FROM sessions s")
        params: list = []
        if search:
            query += " WHERE s.name LIKE ? OR s.description LIKE ?"
            params += [f"%{search}%", f"%{search}%"]
        query += " ORDER BY s.started_ts DESC LIMIT ?"
        params.append(limit)
        with self.lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: int) -> Optional[dict]:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_session(self, session_id: int, name=None, description=None) -> None:
        sets, params = [], []
        if name is not None:
            sets.append("name=?"); params.append(name)
        if description is not None:
            sets.append("description=?"); params.append(description)
        if not sets:
            return
        params.append(session_id)
        with self.lock:
            self.conn.execute(
                f"UPDATE sessions SET {','.join(sets)} WHERE id=?", params
            )
            self.conn.commit()

    def delete_session(self, session_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM samples WHERE session_id=?", (session_id,))
            self.conn.execute("DELETE FROM notes WHERE session_id=?", (session_id,))
            self.conn.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            self.conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self.conn.commit()

    # -- notes -------------------------------------------------------------

    def add_note(self, ts: float, text: str, session_id: Optional[int] = None,
                 channel: Optional[str] = None, photo: Optional[str] = None) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO notes (session_id, ts, channel, text, photo) "
                "VALUES (?,?,?,?,?)",
                (session_id, ts, channel, text, photo),
            )
            self.conn.commit()
            return cur.lastrowid

    def save_photo(self, data: bytes, ext: str = "jpg") -> Optional[str]:
        """Write a note photo to the photos dir, returning its bare filename
        (None if storage isn't available, e.g. in-memory DB)."""
        if not self.photos_dir:
            return None
        import uuid
        ext = "".join(c for c in (ext or "jpg").lower() if c.isalnum())[:5] or "jpg"
        name = f"{uuid.uuid4().hex}.{ext}"
        os.makedirs(self.photos_dir, exist_ok=True)
        with open(os.path.join(self.photos_dir, name), "wb") as f:
            f.write(data)
        return name

    def photo_fullpath(self, name: str) -> Optional[str]:
        """Resolve a stored photo's path, guarding against path traversal."""
        if not self.photos_dir or not name:
            return None
        # Only allow a bare filename living directly in photos_dir.
        if "/" in name or "\\" in name or name in (".", ".."):
            return None
        full = os.path.abspath(os.path.join(self.photos_dir, name))
        if os.path.dirname(full) != os.path.abspath(self.photos_dir):
            return None
        return full if os.path.exists(full) else None

    def list_notes(self, session_id: Optional[int] = None,
                   since: Optional[float] = None) -> list:
        query = "SELECT * FROM notes"
        clauses, params = [], []
        if session_id is not None:
            clauses.append("session_id=?"); params.append(session_id)
        if since is not None:
            clauses.append("ts>=?"); params.append(since)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY ts"
        with self.lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # -- auto timeline events -----------------------------------------------

    def add_event(self, ts: float, kind: str, session_id: Optional[int] = None,
                  channel: Optional[str] = None, label: Optional[str] = None,
                  value: Optional[float] = None) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO events (session_id, ts, kind, channel, label, value) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, ts, kind, channel, label, value),
            )
            self.conn.commit()
            return cur.lastrowid

    def list_events(self, session_id: Optional[int] = None,
                    since: Optional[float] = None, limit: int = 1000) -> list:
        query = "SELECT * FROM events"
        clauses, params = [], []
        if session_id is not None:
            clauses.append("session_id=?"); params.append(session_id)
        if since is not None:
            clauses.append("ts>=?"); params.append(since)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY ts LIMIT ?"
        params.append(max(1, int(limit)))
        with self.lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def session_setpoint(self, session_id: int) -> Optional[float]:
        """The dominant (most common) non-null setpoint of a session - what the
        cook was 'run at', robust to startup ramps and keep-warm tails."""
        with self.lock:
            row = self.conn.execute(
                "SELECT set_point, COUNT(*) n FROM samples "
                "WHERE session_id=? AND set_point IS NOT NULL "
                "GROUP BY set_point ORDER BY n DESC LIMIT 1",
                (session_id,)).fetchone()
        return row["set_point"] if row else None

    def delete_note(self, note_id: int) -> None:
        with self.lock:
            row = self.conn.execute(
                "SELECT photo FROM notes WHERE id=?", (note_id,)).fetchone()
            self.conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
            self.conn.commit()
        # Remove the orphaned photo file, if any (outside the DB lock).
        if row and row["photo"]:
            p = self.photo_fullpath(row["photo"])
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass

    # -- cook programs (saved templates) -----------------------------------

    def save_program(self, name: str, stages, created_ts: float) -> int:
        import json
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO programs (name, stages_json, created_ts) VALUES (?,?,?)",
                (name, json.dumps(stages), created_ts))
            self.conn.commit()
            return cur.lastrowid

    def list_programs(self) -> list:
        import json
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM programs ORDER BY name").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["stages"] = json.loads(d.pop("stages_json"))
            except (ValueError, TypeError):
                d["stages"] = []
            out.append(d)
        return out

    def delete_program(self, program_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM programs WHERE id=?", (program_id,))
            self.conn.commit()

    # -- session sharing ---------------------------------------------------

    def set_session_share(self, session_id: int, token: Optional[str]) -> None:
        with self.lock:
            self.conn.execute("UPDATE sessions SET share_token=? WHERE id=?",
                              (token, session_id))
            self.conn.commit()

    def session_by_share_token(self, token: str) -> Optional[dict]:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE share_token=?", (token,)).fetchone()
        return dict(row) if row else None

    # -- export ------------------------------------------------------------

    def export_csv(self, session_id: Optional[int] = None,
                   since: Optional[float] = None) -> str:
        """Return full-resolution CSV of samples (no downsampling)."""
        import csv
        import io
        cols = self.history_columns(since=since, limit=10_000_000,
                                    session_id=session_id)
        buf = io.StringIO()
        w = csv.writer(buf)
        header = ["timestamp_iso", "epoch"] + COLS
        w.writerow(header)
        from datetime import datetime, timezone
        for i, t in enumerate(cols["t"]):
            iso = datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
            w.writerow([iso, t] + [cols[c][i] for c in COLS])
        return buf.getvalue()

    def close(self) -> None:
        with self.lock:
            self.conn.close()
