import pytest

from eg4mon.model.lfp_soc_model import ModelParams
from eg4mon.runtime import RuntimeConfig, estimate, fmt_hhmm, percentile

P = ModelParams()


def _rows(loads, ratio=1.25, pv=0.0, grid=0.0):
    return [{"load_w": x, "p": -x * ratio, "pv_w": pv, "grid_w": grid} for x in loads]


def test_median_ignores_large_intermittent_loads():
    base = [400.0] * 80 + [4000.0] * 20  # HVAC/oven 20% of the time
    est = estimate(0.9, _rows(base), P, RuntimeConfig())
    assert est.critical_load_w == pytest.approx(400.0)


def test_runtime_uses_measured_losses_and_drain():
    est = estimate(0.9, _rows([400.0] * 100, ratio=1.25), P, RuntimeConfig())
    assert est.ratio_measured and est.ratio == pytest.approx(1.25)
    ah = 0.8 * P.Q
    v = est.usable_kwh * 1000 / ah
    expected_h = ah / ((400 * 1.25 + P.I_p * v) / v)
    assert est.hours == pytest.approx(expected_h)
    assert 50 < est.hours < 80  # ~33 kWh at ~560 W


def test_falls_back_to_efficiency_when_sun_or_grid_present():
    est = estimate(0.9, _rows([400.0] * 100, pv=2000.0), P, RuntimeConfig(inverter_efficiency=0.8))
    assert not est.ratio_measured and est.ratio == pytest.approx(1.25)


def test_not_enough_history_and_zero_loads_excluded():
    rows = _rows([0.0] * 500) + _rows([400.0] * 10)
    est = estimate(0.9, rows, P, RuntimeConfig())
    assert est.hours is None and est.samples == 10


def test_fixed_override_and_reserve():
    est = estimate(0.9, [], P, RuntimeConfig(critical_load_w=300))
    assert est.critical_load_w == 300 and est.hours > 0
    assert estimate(0.05, [], P, RuntimeConfig(critical_load_w=300)).hours == 0


def test_helpers():
    assert percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert fmt_hhmm(None) == "--:--"
    assert fmt_hhmm(61.5) == "61:30"
    assert fmt_hhmm(0.999) == "01:00"
