import math

import pytest

from eg4mon.model.lfp_soc_model import ModelParams, SocEstimator, docv, ocv


def test_ocv_monotonic_and_derivative(params):
    prev = -math.inf
    for k in range(1, 1000):
        s = k / 1000
        assert ocv(s, params) > prev
        prev = ocv(s, params)
        h = 1e-6
        num = (ocv(s + h, params) - ocv(s - h, params)) / (2 * h)
        assert docv(s, params) == pytest.approx(num, rel=1e-4, abs=1e-6)


def test_ocv_landmarks(params):
    # full ~53.6 V rested, plateau step around s0
    assert 53.5 < ocv(1.0, params) < 53.7
    assert ocv(params.s0 + 0.05, params) - ocv(params.s0 - 0.05, params) > 0.4


def test_coulomb_count_exact(params):
    p = ModelParams(**{**params.to_dict(), "I_p": 0.0})
    est = SocEstimator(p, soc=0.5, Pcov=0.0)
    est.step(3600, 53.0, 80.5)  # 80.5 Ah into 805 Ah, EKF blocked by |I| > 40
    assert est.soc == pytest.approx(0.6, abs=1e-12)
    est.step(3600, 53.0, -80.5)
    assert est.soc == pytest.approx(0.5, abs=1e-12)


def test_parasitic_drain_at_rest(params):
    est = SocEstimator(params, soc=0.5, Pcov=0.0)
    res = est.step(3600, 52.7, 0.0, allow_correction=False)
    assert res.soc == pytest.approx(0.5 - params.I_p / params.Q)


def test_eta_applies_to_charge_only(params):
    p = ModelParams(**{**params.to_dict(), "I_p": 0.0, "eta": 0.9})
    est = SocEstimator(p, soc=0.5, Pcov=0.0)
    est.step(3600, 53.0, 80.5)
    assert est.soc == pytest.approx(0.59)


def test_full_reset(params):
    est = SocEstimator(params, soc=0.9, Pcov=0.01)
    res = est.step(60, 54.8, 5.0)
    assert res.reset and est.soc == 1.0 and est.Pcov == 0.0
    # no reset at high current or while discharging
    est = SocEstimator(params, soc=0.9)
    assert not est.step(60, 54.8, 50.0).reset
    assert not est.step(60, 54.8, -1.0).reset
    assert not est.step(60, 54.6, 5.0).reset


def test_ekf_rate_limit_and_current_gate(params):
    est = SocEstimator(params, soc=0.8, Pcov=0.04)
    corrected = [est.step(60, 53.3, 2.0).corrected for _ in range(20)]
    # one correction every 300 s with 60 s steps
    assert corrected == ([False] * 4 + [True]) * 4
    est = SocEstimator(params, soc=0.8, Pcov=0.04)
    assert not any(est.step(60, 53.3, 45.0).corrected for _ in range(20))


def test_ekf_converges_from_wrong_start(params):
    # Synthetic rest at a known SOC on the steep part of the curve.
    true = 0.72
    v = ocv(true, params)
    est = SocEstimator(params, soc=0.95, Pcov=0.04)
    for _ in range(300):
        est.step(300, v, 0.0)
        v -= params.I_p * 300 / 3600 / params.Q * docv(true, params)  # track the drain
    assert abs(est.soc - true) < 0.05


def test_state_roundtrip(params):
    a = SocEstimator(params, soc=0.7)
    for k in range(50):
        a.step(30, 53.1, -20 + k)
    b = SocEstimator(params)
    b.load_dict(a.to_dict())
    for _ in range(50):
        ra, rb = a.step(30, 53.2, 5.0), b.step(30, 53.2, 5.0)
        assert ra == rb


def test_params_hash_and_validation(params):
    assert params.hash() == ModelParams.from_dict(params.to_dict()).hash()
    assert params.hash() != ModelParams(Q=600).hash()
    with pytest.raises(ValueError):
        ModelParams.from_dict({"bogus": 1})


def test_yaml_params_match_defaults():
    from eg4mon.settings import Settings

    assert ModelParams.from_dict(Settings().model_params()) == ModelParams()
