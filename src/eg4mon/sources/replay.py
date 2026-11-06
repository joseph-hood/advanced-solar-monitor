"""CSV replay source: feed recorded telemetry through the same pipeline.

Accepted columns (case-insensitive, first match wins):

    time        ts | timestamp | time | datetime | date
                unix seconds, or ISO 8601 (naive times are read in SITE_TZ)
    voltage     battery_v | v | vbat           (vbat in 0.1 V units is auto-detected)
    power       battery_p | p | batpower       (+ = charge)
                or pcharge and pdischarge      (P = pCharge - pDisCharge)
    current     battery_i | i                  (optional)
    others      soc_inverter|soc, status|statustext, pv1_w|ppv1, pv2_w|ppv2, pv3_w|ppv3,
                grid_w, load_w|consumptionpower, bms_soc, cell_v_min, cell_v_max ...
"""

from __future__ import annotations

import asyncio
import csv
import io
import statistics
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .base import NUMERIC_FIELDS, Sample, to_float

TIME_COLS = ("ts", "timestamp", "time", "datetime", "date")
ALIASES: dict[str, tuple[str, ...]] = {
    "battery_v": ("battery_v", "v", "vbat"),
    "battery_p": ("battery_p", "p", "batpower"),
    "battery_i": ("battery_i", "i"),
    "soc_inverter": ("soc_inverter", "soc"),
    "pv1_w": ("pv1_w", "ppv1"),
    "pv2_w": ("pv2_w", "ppv2"),
    "pv3_w": ("pv3_w", "ppv3"),
    "load_w": ("load_w", "consumptionpower"),
}
TEXT_ALIASES = {"status": ("status", "statustext")}


def parse_time(raw: str, tz: ZoneInfo) -> float:
    raw = raw.strip()
    f = to_float(raw)
    if f is not None:
        return f / 1000.0 if f > 1e11 else f  # tolerate epoch milliseconds
    dt = datetime.fromisoformat(raw.replace("/", "-"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.timestamp()


def read_csv(src: str | Path | io.TextIOBase | bytes, tz: str = "UTC") -> list[Sample]:
    if isinstance(src, bytes):
        fh: io.TextIOBase = io.StringIO(src.decode("utf-8-sig"))
    elif isinstance(src, (str, Path)):
        fh = open(src, encoding="utf-8-sig", newline="")
    else:
        fh = src
    try:
        rows = list(csv.DictReader(fh))
    finally:
        if isinstance(src, (str, Path)):
            fh.close()
    return rows_to_samples(rows, ZoneInfo(tz))


def rows_to_samples(rows: Iterable[dict], tz: ZoneInfo) -> list[Sample]:
    rows = [{(k or "").strip().lower(): v for k, v in r.items()} for r in rows]
    if not rows:
        return []
    cols = set(rows[0])
    tcol = next((c for c in TIME_COLS if c in cols), None)
    if tcol is None:
        raise ValueError(f"no time column; expected one of {TIME_COLS}, got {sorted(cols)}")

    pick = {name: next((a for a in al if a in cols), None) for name, al in ALIASES.items()}
    for name in NUMERIC_FIELDS:
        if pick.get(name) is None and name in cols:
            pick[name] = name
    split_power = pick["battery_p"] is None and {"pcharge", "pdischarge"} <= cols
    if pick["battery_v"] is None or (pick["battery_p"] is None and not split_power
                                     and pick["battery_i"] is None):
        raise ValueError("CSV needs a voltage column and a power (or current) column")

    vscale = 1.0
    if pick["battery_v"] == "vbat":
        vals = [to_float(r["vbat"]) for r in rows]
        vals = [v for v in vals if v is not None]
        if vals and statistics.median(vals) > 100:  # 0.1 V units, as in the cloud API
            vscale = 0.1
    tpick = {n: next((a for a in al if a in cols), None) for n, al in TEXT_ALIASES.items()}

    out: list[Sample] = []
    for r in rows:
        if not (r.get(tcol) or "").strip():
            continue
        vals: dict = {}
        for name, col in pick.items():
            if col is not None:
                v = to_float(r.get(col))
                if v is not None:
                    vals[name] = v * (vscale if name == "battery_v" else 1.0)
        if split_power:
            pc, pd = to_float(r.get("pcharge")), to_float(r.get("pdischarge"))
            if pc is not None or pd is not None:
                vals["battery_p"] = (pc or 0.0) - (pd or 0.0)
        for name, col in tpick.items():
            if col is not None and r.get(col):
                vals[name] = r[col]
        out.append(Sample.from_signals(parse_time(r[tcol], tz), vals, "replay"))
    out.sort(key=lambda s: s.ts)
    return out


class ReplaySource:
    name = "replay"

    def __init__(self, path: Path, tz: str = "UTC", speed: float = 0.0, retime: bool = False):
        """speed 0: as fast as possible, N: N x real time.
        retime: shift all timestamps so the recording ends 'now' (dt is preserved),
        handy for loading a recording into the dashboard as recent history."""
        self.samples_list = read_csv(path, tz)
        self.speed = speed
        self.retime = retime
        self._status = {"source": self.name, "file": str(path), "count": len(self.samples_list)}

    def status(self) -> dict:
        return dict(self._status)

    async def samples(self) -> AsyncIterator[Sample]:
        import time

        if not self.samples_list:
            return
        t_first = self.samples_list[0].ts
        wall0 = time.time()
        offset = (wall0 - self.samples_list[-1].ts) if self.retime else 0.0
        for i, s in enumerate(self.samples_list):
            if self.speed > 0:
                target = wall0 + (s.ts - t_first) / self.speed
                await asyncio.sleep(max(0.0, target - time.time()))
            if offset:
                s = s.model_copy(update={"ts": s.ts + offset})
            self._status.update(position=i + 1, last_ok_ts=time.time())
            yield s
