from eg4mon.pipeline.flags import Flag
from eg4mon.store.db import Store

from .conftest import T0, sample


def _run(svc, start, n, p=-1000.0):
    out = None
    for k in range(n):
        out = svc.process(sample(start + 30 * k, 53.0, p))
    return out


def test_state_survives_restart(tmp_path, make_service):
    db = tmp_path / "x.sqlite"
    ref = make_service(Store.open(tmp_path / "ref.sqlite"))
    _run(ref, T0, 200)

    a = make_service(Store.open(db))
    _run(a, T0, 100)
    a.store.conn.close()
    b = make_service(Store.open(db))  # "systemctl restart"
    assert b._restored
    last = _run(b, T0 + 30 * 100, 100)
    assert abs(last["soc_model"] - ref.est.soc) < 1e-12
    assert b.store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='cold_start'").fetchone()[0] == 1


def test_long_downtime_is_a_gap(tmp_path, make_service):
    db = tmp_path / "x.sqlite"
    a = make_service(Store.open(db))
    _run(a, T0, 10)
    a.store.conn.close()
    b = make_service(Store.open(db))
    r = b.process(sample(T0 + 3600 * 5, 53.0, -1000.0))
    assert r["flags"] & Flag.GAP and r["flags"] & Flag.DEGRADED


def test_params_change_is_logged(tmp_path, params):
    from eg4mon.model.lfp_soc_model import ModelParams
    from eg4mon.pipeline.estimator_service import EstimatorService

    db = tmp_path / "x.sqlite"
    a = EstimatorService(Store.open(db), params)
    _run(a, T0, 3)
    b = EstimatorService(Store.open(db), ModelParams(Q=700))
    ev = b.store.conn.execute("SELECT * FROM events WHERE kind='params_changed'").fetchall()
    assert len(ev) == 1
    r = _run(b, T0 + 90, 1)
    assert r["params_hash"] == ModelParams(Q=700).hash()
