"""Synthetic telemetry for tests and hardware-free demos.

A crude off-grid day: PV bell curve, house load, battery = PV - load, with a
bulk / CV (54.8 V hold, tapering current) / float sequence near full. Voltages
come from the model itself plus 0.1 V quantization, so this is *not* a model
validation dataset; it exercises the pipeline end to end.
"""

from __future__ import annotations

import csv
import math
import random
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .model.lfp_soc_model import ModelParams, ocv

CV_V, FLOAT_V = 54.8, 54.0


def _ocv_true(s: float, p: ModelParams) -> float:
    # The "real" pack gets a steep shoulder at the very top that the fitted model
    # does not have; it is what makes the CV current taper.
    return ocv(s, p) + 1.2 * math.exp(40.0 * (s - 1.0))


def simulate(start_ts: float, days: float = 3.0, step_s: float = 60.0, soc0: float = 0.75,
             tz: str = "UTC", seed: int = 1, pv_peak_w: float = 10000.0,
             params: ModelParams | None = None) -> list[dict]:
    p = params or ModelParams()
    rng = random.Random(seed)
    zone = ZoneInfo(tz)
    soc, v1, h = soc0, 0.0, 0.0
    phase, cv_t = "bulk", 0.0
    cloud = 1.0
    rows: list[dict] = []
    n = int(days * 86400 / step_s)
    for k in range(n):
        ts = start_ts + k * step_s
        local = datetime.fromtimestamp(ts, zone)
        hr = local.hour + local.minute / 60.0
        if local.hour == 0 and local.minute == 0:
            phase = "bulk"
        cloud = min(1.0, max(0.2, cloud + rng.gauss(0, 0.03)))
        pv = max(0.0, pv_peak_w * math.sin(math.pi * (hr - 7.0) / 10.0)) * cloud if 7 <= hr <= 17 else 0.0
        load = 1000 + 700 * (17 <= hr <= 22) + rng.gauss(0, 120) + (2500 if rng.random() < 0.03 else 0)
        V_bat = 53.0
        I = (pv - load) / V_bat

        a = math.exp(-step_s / p.tau1)
        if phase == "bulk" and I > 0:
            v_pred = _ocv_true(soc, p) + p.R0 * I + a * v1 + p.R1 * (1 - a) * I + h
            if v_pred >= CV_V:
                phase, cv_t = "cv", 0.0
        if phase == "cv":
            cv_t += step_s
            # current that holds the terminal at CV_V
            i_cv = (CV_V - _ocv_true(soc, p) - a * v1 - h) / (p.R0 + p.R1 * (1 - a))
            I = min(I, max(i_cv, 0.0))
            if I < 4.0 and cv_t > 1800:
                phase = "float"
        if phase == "float":
            I = min(I, 113.0 / FLOAT_V)  # the reported ~113 W float "charge"
        if phase != "bulk" and pv < load:
            phase = "bulk" if soc < 0.9 else phase

        v1 = a * v1 + p.R1 * (1 - a) * I
        sgn = (I > 0) - (I < 0)
        b = math.exp(-p.gamma * abs(I) * step_s / (3600 * p.Q))
        h = b * h + (1 - b) * p.M * sgn
        dq = (p.eta * max(I, 0) - max(-I, 0) - p.I_p) * step_s / (3600 * p.Q)
        soc = min(1.0, max(0.0, soc + dq))

        V = _ocv_true(soc, p) + p.R0 * I + v1 + h
        if phase == "cv" and I > 0:
            V = CV_V
        elif phase == "float" and I > 0:
            V = min(V, FLOAT_V)
        V = round(V + rng.gauss(0, 0.03), 1)
        P = round(I * V)
        if pv > 0:
            status = "PV Charge + Grid off" if P > 0 else "PV&Battery Grid off"
        else:
            status = "Battery Grid off"
        rows.append({
            "ts": round(ts, 1), "battery_v": V, "battery_p": P,
            "soc_inverter": max(0, min(100, round(85 + 10 * (V - 53.0)))),
            "status": status, "pv1_w": round(pv * 0.55), "pv2_w": round(pv * 0.45),
            "load_w": round(max(load, 0)), "grid_w": 0, "true_soc": round(soc, 5),
        })
    return rows


def write_csv(rows: list[dict], path: str | Path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
