"""Tiny SQLite persistence (stdlib sqlite3, WAL mode).

Persists what must survive a restart: IP bans and the aggregate solve
counters. Deliberately NOT written on every request — bans are rare and
counters are snapshotted periodically — so the request hot path stays
in-memory. Events stay in RAM (they're a live tail, not history).

ponytail: single global connection guarded by check_same_thread=False;
aiohttp runs one event loop so writes are effectively serialised. Swap for
aiosqlite only if a real thread pool ever contends on it.
"""

import os
import sqlite3
import time

_DB_PATH = os.environ.get("DB_PATH", "/data/solver.db")
_conn: sqlite3.Connection | None = None


def init() -> None:
    global _conn
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.execute("CREATE TABLE IF NOT EXISTS bans (ip TEXT PRIMARY KEY, until REAL)")
    _conn.execute("CREATE TABLE IF NOT EXISTS stats (k TEXT PRIMARY KEY, v INTEGER)")
    _conn.commit()


# ---- bans ----

def load_bans() -> dict[str, float]:
    """Return {ip: until_ts}, dropping any already expired."""
    if _conn is None:
        return {}
    now = time.time()
    rows = _conn.execute("SELECT ip, until FROM bans WHERE until > ?", (now,)).fetchall()
    # opportunistic cleanup of expired rows
    _conn.execute("DELETE FROM bans WHERE until <= ?", (now,))
    _conn.commit()
    return {ip: until for ip, until in rows}


def save_ban(ip: str, until: float) -> None:
    if _conn is None:
        return
    _conn.execute("INSERT OR REPLACE INTO bans (ip, until) VALUES (?, ?)", (ip, until))
    _conn.commit()


def clear_ban(ip: str) -> None:
    if _conn is None:
        return
    _conn.execute("DELETE FROM bans WHERE ip = ?", (ip,))
    _conn.commit()


# ---- stats ----

def load_stats() -> dict[str, int]:
    if _conn is None:
        return {}
    return {k: v for k, v in _conn.execute("SELECT k, v FROM stats").fetchall()}


def save_stats(stats: dict[str, int]) -> None:
    if _conn is None:
        return
    _conn.executemany("INSERT OR REPLACE INTO stats (k, v) VALUES (?, ?)",
                      [(k, int(v)) for k, v in stats.items()])
    _conn.commit()


def close() -> None:
    if _conn is not None:
        _conn.close()
