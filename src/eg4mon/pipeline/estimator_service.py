"""Wraps the pure SocEstimator with validation, gap handling, persistence and events.

One `process(sample)` call = at most one estimator step and one DB transaction.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..model.lfp_soc_model import ModelParams, SocEstimator, StepResult
from ..sources.base import Sample
from ..store.db import Store
from .flags import Flag

log = logging.getLogger(__name__)

STATE_KEY = "service_state"


@dataclass
class PipelineConfig:
    v_min: float = 40.0
    v_max: float = 60.0
    p_abs_max: float = 20000.0
    prefer_measured_current: bool = False
    gap_threshold_s: float = 900.0
    gap_sigma_per_hour: float = 0.05
    gap_sigma_min: float = 0.05
    gap_sigma_max: float = 0.30
    degraded_clear_sigma: float = 0.03
    cold_soc: float = 0.8
    cold_pcov: float = 0.04
    raw_retention_days: float = 0.0

    @classmethod
    def from_dict(cls, d: dict | None) -> PipelineConfig:
        d = d or {}
        lim, cur, gap = d.get("limits", {}), d.get("current", {}), d.get("gap", {})
        deg, cold, ret = d.get("degraded", {}), d.get("cold_start", {}), d.get("retention", {})
        c = cls()
        return cls(
            v_min=lim.get("v_min", c.v_min), v_max=lim.get("v_max", c.v_max),
            p_abs_max=lim.get("p_abs_max", c.p_abs_max),
            prefer_measured_current=cur.get("prefer_measured", c.prefer_measured_current),
            gap_threshold_s=gap.get("threshold_s", c.gap_threshold_s),
            gap_sigma_per_hour=gap.get("sigma_per_hour", c.gap_sigma_per_hour),
            gap_sigma_min=gap.get("sigma_min", c.gap_sigma_min),
            gap_sigma_max=gap.get("sigma_max", c.gap_sigma_max),
            degraded_clear_sigma=deg.get("clear_sigma", c.degraded_clear_sigma),
            cold_soc=cold.get("soc", c.cold_soc), cold_pcov=cold.get("pcov", c.cold_pcov),
            raw_retention_days=ret.get("raw_days", c.raw_retention_days),
        )


@dataclass
class _Cycle:
    start_ts: float | None = None
    ah_in: float = 0.0
    ah_out: float = 0.0
    soc_min: float | None = None
    has_gap: bool = False


@dataclass
class ServiceState:
    last_ts: float | None = None
    degraded: bool = False
    in_full: bool = False
    last_reset_ts: float | None = None
    params_hash: str | None = None
    cycle: _Cycle = field(default_factory=_Cycle)

    def to_dict(self) -> dict:
        return {
            "last_ts": self.last_ts, "degraded": self.degraded, "in_full": self.in_full,
            "last_reset_ts": self.last_reset_ts, "params_hash": self.params_hash,
            "cycle": self.cycle.__dict__.copy(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> ServiceState:
        return cls(
            last_ts=d.get("last_ts"), degraded=d.get("degraded", False),
            in_full=d.get("in_full", False), last_reset_ts=d.get("last_reset_ts"),
            params_hash=d.get("params_hash"), cycle=_Cycle(**(d.get("cycle") or {})),
        )


class EstimatorService:
    EVENT_THROTTLE_S = {"rejected": 600.0}

    def __init__(self, store: Store, params: ModelParams, cfg: PipelineConfig | None = None,
                 tz: str = "UTC"):
        self.store = store
        self.params = params
        self.params_hash = params.hash()
        self.cfg = cfg or PipelineConfig()
        self.tz = ZoneInfo(tz)
        self.est = SocEstimator(params)
        self.state = ServiceState()
        self._throttle: dict[str, float] = {}
        self._restored = self._restore()

    # ---- lifecycle ------------------------------------------------------------
    def _restore(self) -> bool:
        saved = self.store.get_kv(STATE_KEY)
        if not saved:
            return False
        self.est.load_dict(saved["estimator"])
        self.state = ServiceState.from_dict(saved["service"])
        if self.state.params_hash and self.state.params_hash != self.params_hash:
            self.store.add_event(self.state.last_ts or 0.0, "params_changed",
                                 f"model params {self.state.params_hash} -> {self.params_hash}",
                                 {"old": self.state.params_hash, "new": self.params_hash})
        self.state.params_hash = self.params_hash
        log.info("restored estimator state: soc=%.3f sigma=%.3f last_ts=%s",
                 self.est.soc, self.est.sigma, self.state.last_ts)
        return True

    @property
    def sigma(self) -> float:
        return self.est.sigma

    # ---- main entry -------------------------------------------------------------
    def process(self, s: Sample) -> dict | None:
        events: list[dict] = []
        V, P = s.battery_v, s.battery_p
        reason = self._reject_reason(s)
        if reason:
            self._event(events, s.ts, "rejected", reason, {"v": V, "p": P, "i": s.battery_i})
            if events:
                self.store.write_step(None, events, None, None, {})
            return None

        flags = Flag(0)
        if self.cfg.prefer_measured_current and s.battery_i is not None:
            I = float(s.battery_i)
            flags |= Flag.MEASURED_CURRENT
        else:
            I = float(P) / float(V)
        if P is None:
            P = I * V

        st = self.state
        if st.last_ts is None:
            # Cold start: no saved state at all.
            soc0 = s.bms_soc / 100.0 if s.bms_soc is not None else self.cfg.cold_soc
            self.est.load_dict({"soc": min(1.0, max(0.0, soc0)), "Pcov": self.cfg.cold_pcov})
            st.params_hash = self.params_hash
            flags |= Flag.COLD_START
            self._event(events, s.ts, "cold_start",
                        f"cold start at SOC {soc0:.1%} ({'BMS' if s.bms_soc is not None else 'default'})",
                        {"soc": soc0})
            dt = 0.0
        else:
            dt = s.ts - st.last_ts
            if dt <= 0:
                log.debug("dropping non-increasing sample ts=%s (last %s)", s.ts, st.last_ts)
                return None

        gap = dt > self.cfg.gap_threshold_s
        if gap:
            res = self._gap_step(dt, V, s.ts, events)
            flags |= Flag.GAP
            I_step, P_step = 0.0, 0.0
        else:
            res = self.est.step(dt, V, I)
            I_step, P_step = I, P

        if res.reset:
            flags |= Flag.RESET
        if res.corrected:
            flags |= Flag.CORRECTED
        if res.large_correction:
            flags |= Flag.LARGE_CORRECTION
            self._event(events, s.ts, "large_correction",
                        f"EKF moved SOC by {res.correction:+.1%}",
                        {"delta": res.correction, "soc": res.soc, "v_resid": res.v_resid})

        # degraded flag lifecycle
        if st.degraded and (res.reset or math.sqrt(res.Pcov) < self.cfg.degraded_clear_sigma):
            st.degraded = False
            self._event(events, s.ts, "degraded_cleared",
                        "full reset" if res.reset else f"sigma {math.sqrt(res.Pcov):.3f}")
        if st.degraded:
            flags |= Flag.DEGRADED

        cycle_row = self._cycle_update(s.ts, dt, I_step, res, gap, events)
        daily = self._daily_row(s, dt, I_step, P_step, res, gap)
        st.in_full = res.reset
        st.last_ts = s.ts

        reading = {
            "ts": s.ts, "dt": dt, "v": V, "i": I, "p": P,
            "soc_model": res.soc, "soc_inverter": s.soc_inverter, "soc_bms": s.bms_soc,
            "v_hat": res.v_hat, "v_resid": res.v_resid, "v1": res.v1, "h": res.h, "pcov": res.Pcov,
            "flags": int(flags), "params_hash": self.params_hash, "status": s.status,
            "pv1_w": s.pv1_w, "pv2_w": s.pv2_w, "pv3_w": s.pv3_w, "pv_w": s.pv_w,
            "grid_w": s.grid_w, "load_w": s.load_w, "bms_i": s.bms_i,
            "cell_v_min": s.cell_v_min, "cell_v_max": s.cell_v_max,
            "temp_min": s.temp_min, "temp_max": s.temp_max, "extra": s.extra,
        }
        self.store.write_step(reading, events, daily, cycle_row, {STATE_KEY: self.snapshot()})
        for e in events:
            log.info("event %s: %s", e["kind"], e.get("message", ""))
        return reading

    def snapshot(self) -> dict:
        return {"estimator": self.est.to_dict(), "service": self.state.to_dict()}

    # ---- pieces -----------------------------------------------------------------
    def _reject_reason(self, s: Sample) -> str | None:
        c = self.cfg
        if s.battery_v is None:
            return "missing battery voltage"
        if not (c.v_min <= s.battery_v <= c.v_max):
            return f"battery voltage {s.battery_v:.2f} V outside [{c.v_min}, {c.v_max}]"
        if s.battery_p is None and s.battery_i is None:
            return "missing battery power/current"
        if s.battery_p is not None and abs(s.battery_p) > c.p_abs_max:
            return f"battery power {s.battery_p:.0f} W exceeds {c.p_abs_max:.0f} W"
        return None

    def _gap_step(self, dt: float, V: float, ts: float, events: list[dict]) -> StepResult:
        """Don't integrate stale current across a gap: I = 0 (I_p still drains), no EKF,
        inflate the covariance for the unknown charge/discharge, mark degraded."""
        res = self.est.step(dt, V, 0.0, allow_correction=False)
        hours = dt / 3600.0
        add = min(self.cfg.gap_sigma_max, max(self.cfg.gap_sigma_min, self.cfg.gap_sigma_per_hour * hours))
        self.est.Pcov = min(0.25, self.est.Pcov + add * add)
        res.Pcov = self.est.Pcov
        self.state.degraded = True
        self.state.cycle.has_gap = True
        self._event(events, ts, "gap", f"{hours:.2f} h without data; estimate degraded",
                    {"dt": dt, "sigma": math.sqrt(res.Pcov)})
        return res

    def _cycle_update(self, ts: float, dt: float, I: float, res: StepResult, gap: bool,
                      events: list[dict]) -> dict | None:
        """Accumulate Ah between full resets. The interval runs from the last reset step of
        one full charge to the first reset step of the next."""
        st, cy = self.state, self.state.cycle
        row = None
        if cy.start_ts is not None and not st.in_full:
            if I > 0:
                cy.ah_in += I * dt / 3600.0
            elif I < 0:
                cy.ah_out += -I * dt / 3600.0
            cy.soc_min = res.soc_prior if cy.soc_min is None else min(cy.soc_min, res.soc_prior)
        if res.reset and not st.in_full:
            if cy.start_ts is not None:
                T = (ts - cy.start_ts) / 3600.0
                closure = self.params.eta * cy.ah_in - cy.ah_out - self.params.I_p * T
                row = {
                    "start_ts": cy.start_ts, "end_ts": ts, "hours": T, "ah_in": cy.ah_in,
                    "ah_out": cy.ah_out, "closure_ah": closure, "soc_min": cy.soc_min,
                    "has_gap": int(cy.has_gap), "params_hash": self.params_hash,
                }
            self._event(events, ts, "full_reset", "full-charge reset (SOC = 100%)",
                        {"soc_before": res.soc_prior, "closure_ah": row["closure_ah"] if row else None})
            st.last_reset_ts = ts
        if res.reset:
            self.state.cycle = _Cycle(start_ts=ts)
        return row

    def _daily_row(self, s: Sample, dt: float, I: float, P: float, res: StepResult,
                   gap: bool) -> dict:
        day = datetime.fromtimestamp(s.ts, self.tz).date().isoformat()
        h = dt / 3600.0
        d: dict[str, Any] = {"day": day, "samples": 1, "soc_min": res.soc, "soc_max": res.soc}
        if res.reset and not self.state.in_full:
            d["resets"] = 1
        if gap:
            d["gap_s"] = dt
            return d
        d["ah_in"] = max(I, 0.0) * h
        d["ah_out"] = max(-I, 0.0) * h
        d["kwh_in"] = max(P, 0.0) * h / 1000.0
        d["kwh_out"] = max(-P, 0.0) * h / 1000.0
        if s.pv_w is not None:
            d["pv_kwh"] = max(s.pv_w, 0.0) * h / 1000.0
        if s.load_w is not None:
            d["load_kwh"] = max(s.load_w, 0.0) * h / 1000.0
        if s.grid_w is not None:
            d["grid_import_kwh"] = max(s.grid_w, 0.0) * h / 1000.0
            d["grid_export_kwh"] = max(-s.grid_w, 0.0) * h / 1000.0
        return d

    def _event(self, events: list[dict], ts: float, kind: str, message: str = "",
               data: dict | None = None) -> None:
        thr = self.EVENT_THROTTLE_S.get(kind)
        if thr is not None:
            last = self._throttle.get(kind)
            if last is not None and ts - last < thr:
                return
            self._throttle[kind] = ts
        events.append({"ts": ts, "kind": kind, "message": message, "data": data})
