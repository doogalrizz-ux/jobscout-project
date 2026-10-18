"""On-disk cache of per-job details (SQLite), shared by the scanner threads.

Workday and SmartRecruiters need one request per job for details. Caching them means each job is
fetched once, and because every entry is written as it arrives, an interrupted run keeps its work.
Entries not used for `keep_days` are pruned when the cache is closed.
"""

import json
import sqlite3
import threading
from datetime import date, timedelta


class DetailCache:
    def __init__(self, path, keep_days=30):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS details (key TEXT PRIMARY KEY, data TEXT, used TEXT)")
        self._lock = threading.Lock()
        self._today = date.today().isoformat()
        self._keep_days = keep_days
        self._writes = 0

    def _maybe_commit(self):
        self._writes += 1
        if self._writes % 200 == 0:
            self._db.commit()

    def get(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT data, used FROM details WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            if row[1] != self._today:
                self._db.execute("UPDATE details SET used=? WHERE key=?", (self._today, key))
                self._maybe_commit()
            return json.loads(row[0])

    def __contains__(self, key):
        with self._lock:
            return self._db.execute("SELECT 1 FROM details WHERE key=?", (key,)).fetchone() is not None

    def __setitem__(self, key, value):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO details VALUES (?, ?, ?)",
                             (key, json.dumps(value, ensure_ascii=False), self._today))
            self._maybe_commit()

    def __len__(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM details").fetchone()[0]

    def close(self):
        cutoff = (date.today() - timedelta(days=self._keep_days)).isoformat()
        with self._lock:
            self._db.execute("DELETE FROM details WHERE used < ?", (cutoff,))
            self._db.commit()
            self._db.execute("VACUUM")
            self._db.close()
