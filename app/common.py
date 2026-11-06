"""Shared helpers for the dashboard. The dashboard only ever talks to the API."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import httpx
import pandas as pd
import streamlit as st

from eg4mon.settings import get_settings

S = get_settings()

# Fixed entity -> color (categorical slots in fixed order; never re-assigned by rank).
COLORS = {
    "model": "#2a78d6",      # slot 1 blue   - model SOC / battery
    "inverter": "#eb6834",   # slot 2 orange - inverter SOC (voltage mode)
    "bms": "#1baf7a",        # slot 3 aqua   - Daly BMS
    "pv": "#eda100",         # slot 4 yellow
    "load": "#e87ba4",       # slot 5 magenta
    "pv2": "#008300",        # slot 6 green
    "grid": "#6250d6",       # slot 7 violet
    "measured": "#52514e",   # neutral ink for measurements vs model
}
STATUS = {"good": "#0ca30c", "warning": "#fab219", "critical": "#d12f2f"}


class ApiError(RuntimeError):
    pass


def _headers() -> dict:
    tok = S.api_token.get_secret_value()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def api_get(path: str, **params):
    try:
        r = httpx.get(S.dashboard_api_url.rstrip("/") + path, params=params, headers=_headers(), timeout=20)
    except httpx.HTTPError as e:
        raise ApiError(f"API unreachable at {S.dashboard_api_url}: {e}") from e
    if r.status_code != 200:
        raise ApiError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def api_post_file(path: str, name: str, data: bytes, **params):
    r = httpx.post(S.dashboard_api_url.rstrip("/") + path, params=params, headers=_headers(),
                   files={"file": (name, data, "text/csv")}, timeout=300)
    if r.status_code != 200:
        raise ApiError(f"{path}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


@st.cache_data(ttl=30, show_spinner=False)
def cached_get(path: str, **params):
    return api_get(path, **params)


def to_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if not df.empty and "ts" in df:
        df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(S.site_tz)
    return df


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, seconds)
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def range_picker(key: str, default: str = "24 h") -> tuple[float, float]:
    options = {"6 h": 6, "24 h": 24, "3 d": 72, "7 d": 168, "30 d": 720, "Custom": None}
    c1, c2 = st.columns([3, 2])
    choice = c1.segmented_control("Range", list(options), default=default, key=key + "_r")
    now = time.time()
    if choice == "Custom":
        today = datetime.now().date()
        d = c2.date_input("Dates", (today - timedelta(days=7), today), key=key + "_d")
        if isinstance(d, tuple) and len(d) == 2:
            tz = pd.Timestamp.now(tz=S.site_tz).tz
            start = pd.Timestamp(d[0], tz=tz).timestamp()
            end = (pd.Timestamp(d[1], tz=tz) + pd.Timedelta(days=1)).timestamp()
            return start, min(end, now)
    hours = options.get(choice or default) or 24
    return now - hours * 3600, now


def plot_layout(fig, height: int = 320, y_title: str | None = None):
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        yaxis_title=y_title,
    )
    fig.update_xaxes(showgrid=False)
    return fig


def show_api_error(e: Exception) -> None:
    st.error(str(e))
    st.caption("Start the API with `eg4mon api` and check DASHBOARD_API_URL in .env.")
