import csv
import io

import pytest
from fastapi.testclient import TestClient

from eg4mon.api.main import create_app
from eg4mon.collector import build_service
from eg4mon.settings import Settings
from eg4mon.sources.replay import read_csv
from eg4mon.store.db import Store


def _csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


@pytest.fixture
def client(tmp_path, sim_rows):
    settings = Settings(db_path=tmp_path / "db.sqlite", site_tz="America/Chicago", api_token="t0k")
    store = Store.open(settings.db_path)
    svc = build_service(settings, store)
    for s in read_csv(_csv(sim_rows[:2000]), settings.site_tz):
        svc.process(s)
    store.conn.close()
    return TestClient(create_app(settings), headers={"Authorization": "Bearer t0k"}), sim_rows


def test_auth(client):
    c, _ = client
    assert c.get("/health").status_code == 200
    assert c.get("/latest", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_latest(client):
    c, rows = client
    j = c.get("/latest").json()
    assert j["reading"]["ts"] == rows[1999]["ts"]
    assert 0 <= j["reading"]["soc_model"] <= 1 and j["sigma"] is not None
    assert "flag_names" in j["reading"]


def test_range_bucketing(client):
    c, rows = client
    t0, t1 = rows[0]["ts"], rows[1999]["ts"]
    raw = c.get("/range", params={"from": t0, "to": t1, "bucket": 1}).json()
    assert raw["count"] == 2000
    b = c.get("/range", params={"from": t0, "to": t1, "bucket": 300}).json()
    assert b["bucket"] == 300 and 300 < b["count"] < 450 / 1  # ~2000 min / 5 min
    assert {"soc_model", "v", "v_hat", "n"} <= set(b["rows"][0])
    auto = c.get("/range", params={"from": t0, "to": t0 + 30 * 86400}).json()
    assert auto["bucket"] >= 900


def test_daily_events_cycles_params(client):
    c, _ = client
    assert len(c.get("/daily").json()) >= 1
    kinds = {e["kind"] for e in c.get("/events").json()}
    assert "cold_start" in kinds
    assert isinstance(c.get("/cycles").json(), list)
    p = c.get("/model/params").json()
    assert p["params"]["Q"] == 805.0 and len(p["hash"]) == 12
    o = c.get("/model/ocv", params={"points": 11}).json()
    assert len(o["soc"]) == 11


def test_replay_endpoint(client):
    c, rows = client
    r = c.post("/replay", files={"file": ("x.csv", _csv(rows[:600]), "text/csv")},
               params={"initial_soc": 0.7})
    assert r.status_code == 200
    j = r.json()
    assert len(j["rows"]) == 600 and j["rows"][0]["soc_model"] == pytest.approx(0.7, abs=0.01)
    bad = c.post("/replay", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert bad.status_code == 400


def test_missing_db(tmp_path):
    c = TestClient(create_app(Settings(db_path=tmp_path / "none.sqlite")))
    assert c.get("/latest").status_code == 503
