"""Normalized telemetry sample and the source interface every backend implements."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from pydantic import BaseModel, Field

# Numeric fields every source may fill. Anything else a source maps goes in `extra`.
NUMERIC_FIELDS = (
    "battery_v", "battery_p", "battery_i", "soc_inverter",
    "pv1_w", "pv2_w", "pv3_w", "grid_w", "load_w",
    "bms_soc", "bms_i", "cell_v_min", "cell_v_max", "temp_min", "temp_max",
)


class Sample(BaseModel):
    """One aligned snapshot. Sign conventions: battery + = charge, grid + = import."""

    ts: float  # unix seconds, UTC
    battery_v: float | None = None  # pack V
    battery_p: float | None = None  # W
    battery_i: float | None = None  # A, if the source publishes it
    soc_inverter: float | None = None  # %, voltage-mode estimate: comparison only
    status: str | None = None
    pv1_w: float | None = None
    pv2_w: float | None = None
    pv3_w: float | None = None
    grid_w: float | None = None
    load_w: float | None = None
    # BMS (Daly) when available
    bms_soc: float | None = None  # %
    bms_i: float | None = None
    cell_v_min: float | None = None
    cell_v_max: float | None = None
    temp_min: float | None = None
    temp_max: float | None = None
    extra: dict[str, float] = Field(default_factory=dict)
    source: str = ""

    @property
    def pv_w(self) -> float | None:
        vals = [v for v in (self.pv1_w, self.pv2_w, self.pv3_w) if v is not None]
        return sum(vals) if vals else None

    @classmethod
    def from_signals(cls, ts: float, values: dict[str, Any], source: str) -> Sample:
        known = set(cls.model_fields) - {"ts", "extra", "source"}
        kw = {k: v for k, v in values.items() if k in known}
        extra = {k: float(v) for k, v in values.items() if k not in known and isinstance(v, (int, float))}
        return cls(ts=ts, extra=extra, source=source, **kw)


class Source(Protocol):
    name: str

    def samples(self) -> AsyncIterator[Sample]:
        """Yield samples forever (live sources) or until exhausted (replay)."""
        ...

    def status(self) -> dict:
        """Health info for the dashboard: last success, last error, etc."""
        ...


# ---- shared helpers for declarative field maps --------------------------------

def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def to_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def eval_terms(obj: Any, terms: list[dict]) -> float | None:
    """Sum of scale*value over terms; None when no term is present."""
    total, found = 0.0, False
    for t in terms:
        v = to_float(get_path(obj, t["path"]))
        if v is None:
            continue
        total += v * float(t.get("scale", 1.0))
        found = True
    return total if found else None


def apply_field_map(obj: Any, section: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, terms in (section.get("numeric") or {}).items():
        v = eval_terms(obj, terms)
        if v is not None:
            out[name] = v
    for name, path in (section.get("text") or {}).items():
        v = get_path(obj, path)
        if v is not None:
            out[name] = str(v)
    return out
