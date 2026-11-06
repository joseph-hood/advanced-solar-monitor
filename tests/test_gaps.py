import math

from eg4mon.pipeline.flags import Flag

from .conftest import T0, sample


def test_gap_does_not_integrate_stale_current(store, make_service, params):
    svc = make_service(store)
    svc.process(sample(T0, 53.0, -2650.0))  # ~ -50 A
    for k in range(1, 4):
        svc.process(sample(T0 + 30 * k, 53.0, -2650.0))
    soc_before = svc.est.soc
    # 2 hour outage, then the next sample still says -50 A
    r = svc.process(sample(T0 + 90 + 7200, 53.0, -2650.0))
    expected = soc_before - params.I_p * 7200 / 3600 / params.Q  # only the parasitic drain
    assert math.isclose(r["soc_model"], expected, abs_tol=1e-9)
    assert r["flags"] & Flag.GAP and r["flags"] & Flag.DEGRADED
    assert math.sqrt(r["pcov"]) >= 0.05
    assert store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='gap'").fetchone()[0] == 1
    day = store.conn.execute("SELECT gap_s FROM daily").fetchone()
    assert day["gap_s"] == 7200


def test_short_dt_is_not_a_gap(store, make_service):
    svc = make_service(store)
    svc.process(sample(T0))
    r = svc.process(sample(T0 + 899, 53.0, -500.0))
    assert not r["flags"] & Flag.GAP


def test_degraded_clears_on_full_reset(store, make_service):
    svc = make_service(store)
    svc.process(sample(T0))
    svc.process(sample(T0 + 5000))
    assert svc.state.degraded
    r = svc.process(sample(T0 + 5030, 54.8, 5 * 54.8))
    assert r["flags"] & Flag.RESET and not r["flags"] & Flag.DEGRADED
    assert not svc.state.degraded


def test_rejects_absurd_values(store, make_service):
    svc = make_service(store)
    assert svc.process(sample(T0, 70.0, 100.0)) is None
    assert svc.process(sample(T0 + 1, 53.0, 50000.0)) is None
    assert svc.process(sample(T0 + 2, 53.0, 100.0)) is not None
    # throttled: two rejections within 10 min -> one event
    assert store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='rejected'").fetchone()[0] == 1


def test_duplicate_and_out_of_order_samples_dropped(store, make_service):
    svc = make_service(store)
    svc.process(sample(T0))
    assert svc.process(sample(T0)) is None
    assert svc.process(sample(T0 - 10)) is None
