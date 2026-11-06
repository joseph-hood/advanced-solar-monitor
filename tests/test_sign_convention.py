"""Battery power + = charging everywhere downstream of the source adapters."""

import pytest

from eg4mon.settings import REPO_ROOT
from eg4mon.sources.base import apply_field_map
from eg4mon.sources.eg4_cloud import load_field_map

from .conftest import T0, sample

FM = load_field_map(REPO_ROOT / "config" / "eg4_cloud.yaml")


def test_eg4_runtime_charge_is_positive():
    vals = apply_field_map({"vBat": 534, "pCharge": 2500, "pDisCharge": 0}, FM["inverter"])
    assert vals["battery_v"] == pytest.approx(53.4) and vals["battery_p"] == 2500
    vals = apply_field_map({"vBat": 524, "pCharge": 0, "pDisCharge": 1800}, FM["inverter"])
    assert vals["battery_p"] == -1800


def test_charging_raises_model_soc(store, make_service):
    svc = make_service(store)
    svc.process(sample(T0, 53.2, 0.0))
    soc0 = svc.est.soc
    for k in range(1, 11):
        svc.process(sample(T0 + 60 * k, 53.6, 3000.0))
    assert svc.est.soc > soc0
    r = store.conn.execute("SELECT i FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
    assert r["i"] > 0
