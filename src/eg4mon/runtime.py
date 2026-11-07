"""Planning estimate: time remaining for critical loads (no sun, no grid).

Pure and stdlib-only, like the model. The caller supplies history and the current SOC.

- Critical load: a low percentile (default the median) of bucket-averaged AC load over a
  trailing window. Large intermittent loads (HVAC, oven, dryer) run a small share of the
  time, so they don't move the median. Fridge/freezer cycling is averaged within buckets.
- AC -> battery: the median of (battery discharge / load) over buckets with no PV and no
  grid, i.e. inverter losses and idle draw as measured. Falls back to 1/efficiency.
- Battery side: plus the model's unmetered drain I_p, the same drain the SOC model applies.
- Energy: Ah from SOC down to the reserve, at the mean model OCV over that span.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from eg4mon.model.lfp_soc_model import ModelParams, ocv


@dataclass(frozen=True)
class RuntimeConfig:
    reserve_soc: float = 0.10       # run time is to this SOC
    percentile: float = 50.0        # of bucket-averaged load
    min_samples: int = 72           # buckets needed before an estimate is shown
    inverter_efficiency: float = 0.92  # fallback when no off-grid, no-sun data yet
    min_ratio_samples: int = 12
    critical_load_w: float | None = None  # fixed override; skips the percentile

    @classmethod
    def from_dict(cls, d: dict | None) -> RuntimeConfig:
        d = d or {}
        names = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass(frozen=True)
class RuntimeEstimate:
    hours: float | None
    critical_load_w: float | None
    battery_w: float | None       # DC draw incl. losses and I_p
    usable_kwh: float
    ratio: float                  # battery W per load W
    ratio_measured: bool
    samples: int


def percentile(xs: list[float], pct: float) -> float:
    s = sorted(xs)
    k = (len(s) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def estimate(soc: float, rows: list[dict], p: ModelParams, cfg: RuntimeConfig) -> RuntimeEstimate:
    """`rows`: bucket averages with load_w, p (battery, + = charging), pv_w, grid_w."""
    loads = [r["load_w"] for r in rows if (r.get("load_w") or 0) > 0]

    pairs = [-r["p"] / r["load_w"] for r in rows
             if (r.get("load_w") or 0) > 50 and (r.get("p") or 0) < 0
             and abs(r.get("pv_w") or 0) < 20 and abs(r.get("grid_w") or 0) < 20]
    pairs = [x for x in pairs if 1.0 <= x <= 2.0]  # discard buckets that straddle a transition
    measured = len(pairs) >= cfg.min_ratio_samples
    ratio = statistics.median(pairs) if measured else 1.0 / cfg.inverter_efficiency

    lo = cfg.reserve_soc
    hi = max(soc, lo)
    n = 50
    v_avg = sum(ocv(lo + (hi - lo) * (i + 0.5) / n, p) for i in range(n)) / n
    ah = (hi - lo) * p.Q
    usable_kwh = ah * v_avg / 1000.0

    if cfg.critical_load_w is not None:
        load = float(cfg.critical_load_w)
    elif len(loads) >= cfg.min_samples:
        load = percentile(loads, cfg.percentile)
    else:
        return RuntimeEstimate(None, None, None, usable_kwh, ratio, measured, len(loads))

    battery_w = load * ratio + p.I_p * v_avg
    hours = ah / (battery_w / v_avg) if battery_w > 0 else None
    return RuntimeEstimate(hours, load, battery_w, usable_kwh, ratio, measured, len(loads))


def fmt_hhmm(hours: float | None) -> str:
    if hours is None:
        return "--:--"
    m = round(hours * 60)
    return f"{m // 60:02d}:{m % 60:02d}"
