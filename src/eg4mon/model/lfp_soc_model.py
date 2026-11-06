"""LiFePO4 pack state-of-charge estimator.

Pure Python, stdlib only. No I/O, no clocks: the caller supplies dt, V and I.
Sign convention: I > 0 is charging, pack level (volts, amps).

Model (see HANDOFF.md section 4):

    SOC-  = clamp01(SOC + dt*(eta*I+ - I- - I_p) / (3600*Q))      coulomb count
    v1    = a*v1 + R1*(1-a)*I,            a = exp(-dt/tau1)       RC polarization
    h     = b*h + (1-b)*M*sgn(I),         b = exp(-gamma*|I|*dt/(3600*Q))
    V_hat = OCV(SOC-) + R0*I + v1 + h

    full reset:  V >= V_full and 0 < I < I_tail  ->  SOC = 1, P = 0
    EKF:         only when |I| < ekf_max_current and >= ekf_interval s since
                 the last correction (r was fitted at 5-minute samples)

Process noise is added on every step (P- = P + q*dt/3600) so that the time
between rate-limited corrections still grows the covariance.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, fields

__all__ = ["ModelParams", "SocEstimator", "StepResult", "docv", "ocv"]


@dataclass(frozen=True)
class ModelParams:
    # capacity and current bookkeeping
    Q: float = 805.0  # Ah, nameplate; not identifiable from the fit data
    eta: float = 1.0  # charge factor applied to telemetry
    I_p: float = 1.22  # A, constant unmetered drain / telemetry offset
    # OCV(s) = A + C*exp(D*(s-1)) + E*sigmoid((s-s0)/w) - K_b*exp(-s/alpha_b)
    A: float = 52.631
    C: float = 0.4376
    D: float = 3.787
    E: float = 0.5224
    s0: float = 0.7087
    w: float = 0.02398
    K_b: float = 4.0  # bottom knee: generic LFP prior, not fitted
    alpha_b: float = 0.04
    # impedance
    R0: float = 0.00599  # ohm
    R1: float = 0.00526  # ohm
    tau1: float = 3470.0  # s
    # hysteresis
    M: float = 0.2044  # V, half-width
    gamma: float = 400.0
    # EKF
    q: float = 0.002**2  # per hour
    r: float = 0.12**2  # V^2
    ekf_interval: float = 300.0  # s, do not lower without refitting r
    ekf_max_current: float = 40.0  # A
    # full-charge reset
    V_full: float = 54.7
    I_tail: float = 10.0
    # reporting
    large_correction: float = 0.02  # |dSOC| per EKF step that gets flagged

    @classmethod
    def from_dict(cls, d: dict | None) -> ModelParams:
        d = dict(d or {})
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown model params: {sorted(unknown)}")
        return cls(**{k: float(v) for k, v in d.items()})

    def to_dict(self) -> dict:
        return asdict(self)

    def hash(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def ocv(s: float, p: ModelParams) -> float:
    return (
        p.A
        + p.C * math.exp(p.D * (s - 1.0))
        + p.E * _sigmoid((s - p.s0) / p.w)
        - p.K_b * math.exp(-s / p.alpha_b)
    )


def docv(s: float, p: ModelParams) -> float:
    sg = _sigmoid((s - p.s0) / p.w)
    return (
        p.C * p.D * math.exp(p.D * (s - 1.0))
        + (p.E / p.w) * sg * (1.0 - sg)
        + (p.K_b / p.alpha_b) * math.exp(-s / p.alpha_b)
    )


@dataclass
class StepResult:
    soc: float
    soc_prior: float
    v_hat: float
    v_resid: float
    v1: float
    h: float
    Pcov: float
    reset: bool = False
    corrected: bool = False
    correction: float = 0.0  # SOC change applied by the EKF this step
    large_correction: bool = False


@dataclass
class SocEstimator:
    params: ModelParams = field(default_factory=ModelParams)
    soc: float = 0.8
    v1: float = 0.0
    h: float = 0.0
    Pcov: float = 0.04
    _since_corr: float = 0.0

    # ---- model functions -------------------------------------------------
    def ocv(self, s: float) -> float:
        return ocv(s, self.params)

    def docv(self, s: float) -> float:
        return docv(s, self.params)

    # ---- step ------------------------------------------------------------
    def step(self, dt: float, V: float, I: float, allow_correction: bool = True) -> StepResult:
        """Advance by dt seconds with measured pack voltage V and current I (+ = charge)."""
        p = self.params
        dt = max(0.0, float(dt))

        i_chg = I if I > 0 else 0.0
        i_dis = -I if I < 0 else 0.0
        soc_prior = _clamp01(self.soc + dt * (p.eta * i_chg - i_dis - p.I_p) / (3600.0 * p.Q))

        a = math.exp(-dt / p.tau1)
        self.v1 = a * self.v1 + p.R1 * (1.0 - a) * I

        sgn = (I > 0) - (I < 0)
        b = math.exp(-p.gamma * abs(I) * dt / (3600.0 * p.Q))
        self.h = b * self.h + (1.0 - b) * p.M * sgn

        v_hat = self.ocv(soc_prior) + p.R0 * I + self.v1 + self.h
        P_prior = self.Pcov + p.q * dt / 3600.0
        self._since_corr += dt

        res = StepResult(
            soc=soc_prior, soc_prior=soc_prior, v_hat=v_hat, v_resid=V - v_hat,
            v1=self.v1, h=self.h, Pcov=P_prior,
        )

        if V >= p.V_full and 0.0 < I < p.I_tail:
            self.soc, self.Pcov = 1.0, 0.0
            res.soc, res.Pcov, res.reset = 1.0, 0.0, True
            return res

        if allow_correction and abs(I) < p.ekf_max_current and self._since_corr >= p.ekf_interval:
            H = self.docv(soc_prior)
            K = P_prior * H / (H * H * P_prior + p.r)
            soc_post = _clamp01(soc_prior + K * (V - v_hat))
            P_post = (1.0 - K * H) * P_prior
            self._since_corr = 0.0
            res.soc, res.Pcov, res.corrected = soc_post, P_post, True
            res.correction = soc_post - soc_prior
            res.large_correction = abs(res.correction) > p.large_correction
            self.soc, self.Pcov = soc_post, P_post
            return res

        self.soc, self.Pcov = soc_prior, P_prior
        return res

    # ---- persistence -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "soc": self.soc, "v1": self.v1, "h": self.h,
            "Pcov": self.Pcov, "_since_corr": self._since_corr,
        }

    def load_dict(self, d: dict) -> None:
        self.soc = float(d["soc"])
        self.v1 = float(d.get("v1", 0.0))
        self.h = float(d.get("h", 0.0))
        self.Pcov = float(d.get("Pcov", 0.04))
        self._since_corr = float(d.get("_since_corr", 0.0))

    @property
    def sigma(self) -> float:
        return math.sqrt(max(self.Pcov, 0.0))
