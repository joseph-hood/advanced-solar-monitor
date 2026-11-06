"""The full pipeline (CSV -> Sample -> service -> SQLite) must give the same SOC as
stepping the bare estimator, and track a simulated pack."""

import csv
import io
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from eg4mon.api.main import run_replay
from eg4mon.model.lfp_soc_model import SocEstimator
from eg4mon.settings import Settings
from eg4mon.sources.replay import read_csv, rows_to_samples

FIXTURES = Path(__file__).parent / "fixtures"


def _csv_bytes(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


def test_pipeline_matches_bare_estimator(sim_rows, params):
    settings = Settings(site_tz="America/Chicago")
    samples = read_csv(_csv_bytes(sim_rows), "America/Chicago")
    res = run_replay(samples, settings, initial_soc=0.8)

    est = SocEstimator(params, soc=0.8, Pcov=0.04)
    est.step(0.0, samples[0].battery_v, samples[0].battery_p / samples[0].battery_v)
    socs = [est.soc]
    for prev, s in zip(samples, samples[1:]):
        est.step(s.ts - prev.ts, s.battery_v, s.battery_p / s.battery_v)
        socs.append(est.soc)
    assert len(res["rows"]) == len(socs)
    assert max(abs(r["soc_model"] - s) for r, s in zip(res["rows"], socs)) < 1e-12


def test_tracks_simulated_pack(sim_rows):
    res = run_replay(read_csv(_csv_bytes(sim_rows), "America/Chicago"),
                     Settings(site_tz="America/Chicago"), initial_soc=0.55)
    err = [abs(r["soc_model"] - t["true_soc"]) for r, t in zip(res["rows"], sim_rows)]
    assert max(err[1440:]) < 0.03  # after a day
    assert any(e["kind"] == "full_reset" for e in res["events"])
    assert res["daily"] and all(d["samples"] > 0 for d in res["daily"])


def test_eg4_raw_columns():
    rows = [
        {"time": "2026-10-30 00:00:00", "vBat": "527", "pCharge": "0", "pDisCharge": "1500",
         "soc": "84", "statusText": "Battery Grid off"},
        {"time": "2026-10-30 00:05:00", "vBat": "526", "pCharge": "0", "pDisCharge": "1400",
         "soc": "84", "statusText": "Battery Grid off"},
    ]
    s = rows_to_samples(rows, ZoneInfo("America/Chicago"))
    assert s[0].battery_v == pytest.approx(52.7) and s[0].battery_p == -1500
    assert s[0].status == "Battery Grid off" and s[1].ts - s[0].ts == 300


@pytest.mark.skipif(not list(FIXTURES.glob("eg4_batt_*.csv")), reason="real telemetry fixture not present")
def test_real_fixture_voltage_rms():
    """M1 acceptance: voltage RMS <= 0.115 V (EKF) on the 5-day fixture from the handoff."""
    path = sorted(FIXTURES.glob("eg4_batt_*.csv"))[0]
    res = run_replay(read_csv(path, Settings().site_tz), Settings())
    assert res["v_rms"] is not None and res["v_rms"] <= 0.115
    assert math.isfinite(res["rows"][-1]["soc_model"])
