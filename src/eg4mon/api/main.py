"""Read-only HTTP API over the SQLite store (plus a stateless CSV replay endpoint)."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .. import __version__
from ..collector import STATUS_KEY
from ..model.lfp_soc_model import ModelParams, docv, ocv
from ..pipeline.estimator_service import STATE_KEY, EstimatorService, PipelineConfig
from ..pipeline.flags import names as flag_names
from ..settings import Settings, get_settings
from ..sources.replay import read_csv
from ..store.db import NUMERIC_READING_COLS, Store, connect

MAX_POINTS = 3000
_bearer = HTTPBearer(auto_error=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="eg4mon", version=__version__,
                  description="Read-only API for EG4 telemetry and the LiFePO4 SoC model.")
    token = settings.api_token.get_secret_value()

    def auth(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
        if token and (creds is None or creds.credentials != token):
            raise HTTPException(401, "invalid or missing bearer token")

    @contextmanager
    def db() -> Iterator[sqlite3.Connection]:
        try:
            conn = connect(settings.db_path, readonly=True)
        except sqlite3.OperationalError as e:
            raise HTTPException(
                503, f"database {settings.db_path} not available yet; is the collector running?") from e
        try:
            yield conn
        finally:
            conn.close()

    def kv(conn, key):
        try:
            row = conn.execute("SELECT value, updated FROM kv WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None, None
        return (json.loads(row["value"]), row["updated"]) if row else (None, None)

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__, "time": time.time()}

    @app.get("/latest", dependencies=[Depends(auth)])
    def latest():
        with db() as conn:
            row = conn.execute("SELECT * FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
            status, status_updated = kv(conn, STATUS_KEY)
            state, _ = kv(conn, STATE_KEY)
        reading = _reading(row) if row else None
        svc = (state or {}).get("service", {})
        now = time.time()
        return {
            "reading": reading,
            "age_s": now - row["ts"] if row else None,
            "sigma": math.sqrt(row["pcov"]) if row and row["pcov"] is not None else None,
            "degraded": svc.get("degraded"),
            "last_reset_ts": svc.get("last_reset_ts"),
            "since_reset_s": now - svc["last_reset_ts"] if svc.get("last_reset_ts") else None,
            "collector": status,
            "collector_age_s": now - status_updated if status_updated else None,
        }

    @app.get("/range", dependencies=[Depends(auth)])
    def range_(
        from_: float = Query(..., alias="from", description="unix seconds"),
        to: float = Query(default_factory=time.time),
        bucket: int | None = Query(None, ge=1, description="seconds; auto when omitted"),
    ):
        if to <= from_:
            raise HTTPException(400, "`to` must be after `from`")
        if bucket is None:
            bucket = _auto_bucket(to - from_)
        with db() as conn:
            if bucket <= 1:
                rows = conn.execute("SELECT * FROM readings WHERE ts >= ? AND ts <= ? ORDER BY ts",
                                    (from_, to)).fetchall()
                data = [_reading(r) for r in rows]
            else:
                aggs = ", ".join(f"AVG({c}) AS {c}" for c in NUMERIC_READING_COLS if c != "dt")
                rows = conn.execute(
                    f"""SELECT CAST(ts / :b AS INTEGER) * :b + :b / 2.0 AS ts, {aggs},
                               SUM(dt) AS dt, COUNT(*) AS n, MAX(flags & 4) > 0 AS gap,
                               MAX(flags & 8) > 0 AS degraded, MAX(flags & 1) > 0 AS reset,
                               MAX(status) AS status
                        FROM readings WHERE ts >= :f AND ts <= :t
                        GROUP BY CAST(ts / :b AS INTEGER) ORDER BY 1""",
                    {"b": bucket, "f": from_, "t": to}).fetchall()
                data = [dict(r) for r in rows]
        return {"from": from_, "to": to, "bucket": bucket, "count": len(data), "rows": data}

    @app.get("/daily", dependencies=[Depends(auth)])
    def daily(days: int = Query(30, ge=1, le=3650)):
        with db() as conn:
            rows = conn.execute("SELECT * FROM daily ORDER BY day DESC LIMIT ?", (days,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    @app.get("/events", dependencies=[Depends(auth)])
    def events(from_: float = Query(0, alias="from"), to: float = Query(default_factory=time.time),
               kind: str | None = None, limit: int = Query(500, le=5000)):
        q = "SELECT * FROM events WHERE ts >= ? AND ts <= ?"
        args: list = [from_, to]
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        with db() as conn:
            rows = conn.execute(q + " ORDER BY ts DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"]) if r["data"] else None} for r in rows]

    @app.get("/cycles", dependencies=[Depends(auth)])
    def cycles(limit: int = Query(100, le=2000)):
        with db() as conn:
            rows = conn.execute("SELECT * FROM cycles ORDER BY end_ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    @app.get("/model/params", dependencies=[Depends(auth)])
    def model_params():
        p = ModelParams.from_dict(settings.model_params())
        with db() as conn:
            state, _ = kv(conn, STATE_KEY)
        return {"params": p.to_dict(), "hash": p.hash(), "state": state}

    @app.get("/model/ocv", dependencies=[Depends(auth)])
    def model_ocv(points: int = Query(201, ge=11, le=2001)):
        p = ModelParams.from_dict(settings.model_params())
        soc = [i / (points - 1) for i in range(points)]
        return {"soc": soc, "ocv": [ocv(s, p) for s in soc], "docv": [docv(s, p) for s in soc]}

    @app.post("/replay", dependencies=[Depends(auth)])
    async def replay(file: UploadFile = File(...), initial_soc: float | None = None,
                     tz: str | None = None):
        """Run an uploaded CSV through the full pipeline in memory. Nothing is persisted."""
        raw = await file.read()
        try:
            samples = read_csv(raw, tz or settings.site_tz)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, f"could not parse CSV: {e}") from e
        return run_replay(samples, settings, initial_soc)

    return app


def run_replay(samples, settings: Settings, initial_soc: float | None = None) -> dict:
    store = Store.open(":memory:")
    cfg = PipelineConfig.from_dict(settings.pipeline())
    if initial_soc is not None:
        cfg.cold_soc = initial_soc
    svc = EstimatorService(store, ModelParams.from_dict(settings.model_params()), cfg, settings.site_tz)
    for s in samples:
        svc.process(s)
    c = store.conn
    rows = [_reading(r) for r in c.execute("SELECT * FROM readings ORDER BY ts")]
    resid = [r["v_resid"] for r in rows if r["v_resid"] is not None]
    rms = math.sqrt(sum(x * x for x in resid) / len(resid)) if resid else None
    return {
        "rows": rows,
        "events": [dict(r) for r in c.execute("SELECT * FROM events ORDER BY ts")],
        "cycles": [dict(r) for r in c.execute("SELECT * FROM cycles ORDER BY end_ts")],
        "daily": [dict(r) for r in c.execute("SELECT * FROM daily ORDER BY day")],
        "v_rms": rms,
        "params_hash": svc.params_hash,
    }


def _reading(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("extra"):
        d["extra"] = json.loads(d["extra"])
    d["flag_names"] = flag_names(d.get("flags") or 0)
    return d


def _auto_bucket(span_s: float) -> int:
    """Smallest 'nice' bucket that keeps the result under MAX_POINTS."""
    for b in (1, 5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 86400):
        if span_s / b <= MAX_POINTS:
            return b
    return 86400


def __getattr__(name):  # `uvicorn eg4mon.api.main:app` without building at import time
    if name == "app":
        return create_app()
    raise AttributeError(name)
