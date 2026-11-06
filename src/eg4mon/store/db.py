"""SQLite storage. The collector is the only writer; the API opens read-only connections."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from importlib import resources
from pathlib import Path
from typing import Any

READING_COLS = (
    "ts", "dt", "v", "i", "p", "soc_model", "soc_inverter", "soc_bms", "v_hat", "v_resid",
    "v1", "h", "pcov", "flags", "params_hash", "status", "pv1_w", "pv2_w", "pv3_w", "pv_w",
    "grid_w", "load_w", "bms_i", "cell_v_min", "cell_v_max", "temp_min", "temp_max", "extra",
)
NUMERIC_READING_COLS = tuple(
    c for c in READING_COLS if c not in ("ts", "flags", "params_hash", "status", "extra"))
DAILY_SUM_COLS = ("ah_in", "ah_out", "kwh_in", "kwh_out", "pv_kwh", "load_kwh",
                  "grid_import_kwh", "grid_export_kwh", "resets", "gap_s", "samples")


def _schema() -> str:
    return resources.files("eg4mon.store").joinpath("schema.sql").read_text()


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    if str(path) == ":memory:":
        conn = sqlite3.connect(":memory:", check_same_thread=False)
    elif readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=10)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    if not readonly:
        conn.executescript(_schema())
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


class Store:
    """Writer-side helpers. All writes for one step go through `write_step` in one transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, path: str | Path) -> Store:
        return cls(connect(path))

    # ---- kv ---------------------------------------------------------------
    def get_kv(self, key: str) -> Any | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def _put_kv(self, key: str, value: Any, now: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value, updated) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
            (key, json.dumps(value), now if now is not None else time.time()),
        )

    def put_kv(self, key: str, value: Any) -> None:
        with self.conn:
            self._put_kv(key, value)

    # ---- step writes --------------------------------------------------------
    def write_step(self, reading: dict | None, events: Iterable[dict], daily: dict | None,
                   cycle: dict | None, state: dict[str, Any]) -> None:
        with self.conn:
            if reading is not None:
                row = dict(reading)
                if isinstance(row.get("extra"), dict):
                    row["extra"] = json.dumps(row["extra"]) if row["extra"] else None
                cols = [c for c in READING_COLS if c in row]
                self.conn.execute(
                    f"INSERT OR REPLACE INTO readings({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [row[c] for c in cols],
                )
            for e in events:
                self._insert_event(e)
            if daily is not None:
                self._upsert_daily(daily)
            if cycle is not None:
                cols = list(cycle)
                self.conn.execute(
                    f"INSERT INTO cycles({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [cycle[c] for c in cols],
                )
            for k, v in state.items():
                self._put_kv(k, v)

    def add_event(self, ts: float, kind: str, message: str = "", data: dict | None = None) -> None:
        with self.conn:
            self._insert_event({"ts": ts, "kind": kind, "message": message, "data": data})

    def _insert_event(self, e: dict) -> None:
        self.conn.execute(
            "INSERT INTO events(ts, kind, message, data) VALUES (?, ?, ?, ?)",
            (e["ts"], e["kind"], e.get("message", ""), json.dumps(e["data"]) if e.get("data") else None),
        )

    def _upsert_daily(self, d: dict) -> None:
        sets = ", ".join(f"{c} = {c} + excluded.{c}" for c in DAILY_SUM_COLS)
        self.conn.execute(
            f"""INSERT INTO daily(day, {','.join(DAILY_SUM_COLS)}, soc_min, soc_max)
                VALUES (?, {','.join('?' * len(DAILY_SUM_COLS))}, ?, ?)
                ON CONFLICT(day) DO UPDATE SET {sets},
                  soc_min = MIN(COALESCE(soc_min, excluded.soc_min), COALESCE(excluded.soc_min, soc_min)),
                  soc_max = MAX(COALESCE(soc_max, excluded.soc_max), COALESCE(excluded.soc_max, soc_max))""",
            [d["day"], *[d.get(c, 0) for c in DAILY_SUM_COLS], d.get("soc_min"), d.get("soc_max")],
        )

    def prune(self, before_ts: float) -> int:
        with self.conn:
            n = self.conn.execute("DELETE FROM readings WHERE ts < ?", (before_ts,)).rowcount
        return n
