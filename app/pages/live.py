import time

import plotly.graph_objects as go
import streamlit as st
from common import COLORS, STATUS, ApiError, S, api_get, cached_get, fmt_age, plot_layout, show_api_error, to_df

from eg4mon import runtime
from eg4mon.model.lfp_soc_model import ModelParams
from eg4mon.runtime import RuntimeConfig

st.title("Live")
refresh = st.sidebar.slider("Refresh every (s)", 5, 120, int(S.dashboard_refresh_seconds), 5)


DASH = S.dashboard()
ICON = DASH.get("battery_icon") or {}
RT = DASH.get("critical_runtime") or {}
RT_CFG = RuntimeConfig.from_dict(RT)


def battery_icon(soc_pct: float, sub: str = "") -> str:
    """Battery outline with a continuous fill, colored by the configured breakpoints."""
    pct = min(max(soc_pct, 0.0), 100.0)
    if pct < float(ICON.get("red_below", 25)):
        color, state = STATUS["critical"], "low"
    elif pct < float(ICON.get("yellow_below", 50)):
        color, state = STATUS["warning"], "medium"
    else:
        color, state = STATUS["good"], "good"
    # Plain divs, not SVG: st.html sanitizes with DOMPurify's html-only profile, which drops <svg>.
    outline = "rgba(128,128,128,0.75)"
    return f"""
<div style="display:flex;align-items:center;gap:18px;padding:8px 0">
  <div role="img" aria-label="Battery {pct:.0f}%, {state}" style="display:flex;align-items:center">
    <div style="width:170px;height:80px;box-sizing:border-box;border:5px solid {outline};
                border-radius:14px;padding:5px">
      <div style="width:{pct:.1f}%;height:100%;background:{color};border-radius:7px"></div>
    </div>
    <div style="width:9px;height:32px;background:{outline};border-radius:0 4px 4px 0"></div>
  </div>
  <div>
    <div style="font-size:3.2rem;font-weight:600;line-height:1">{pct:.0f}%</div>
    <div style="opacity:0.7;font-size:0.9rem;margin-top:4px">Model SOC{" · " + sub if sub else ""}</div>
  </div>
</div>"""


def runtime_section(col, soc: float):
    now = time.time()
    t1 = int(now // 300) * 300  # stable cache key for 5 min
    try:
        params = ModelParams.from_dict(cached_get("/model/params")["params"])
        rows = cached_get("/range", **{"from": t1 - int(RT.get("window_days", 7)) * 86400, "to": t1,
                                       "bucket": int(RT.get("bucket_s", 300))})["rows"]
    except ApiError as e:
        col.caption(f"Run time unavailable: {e}")
        return
    est = runtime.estimate(soc, rows, params, RT_CFG)
    reserve = f"{RT_CFG.reserve_soc * 100:.0f}%"
    col.metric(f"Time remaining for critical loads (to {reserve})", runtime.fmt_hhmm(est.hours),
               help="Planning number: no sun, no grid, typical (median) house load. "
                    "Large intermittent loads are ignored. Model in src/eg4mon/runtime.py, "
                    "settings in config/dashboard.yaml.")
    if est.hours is None:
        col.caption(f"Collecting load history: {est.samples}/{RT_CFG.min_samples} "
                    f"{int(RT.get('bucket_s', 300)) // 60}-min samples. "
                    f"{est.usable_kwh:.1f} kWh usable to {reserve}.")
        return
    basis = ("fixed (config)" if RT_CFG.critical_load_w is not None
             else f"p{RT_CFG.percentile:.0f} of {int(RT.get('window_days', 7))} d")
    losses = "measured" if est.ratio_measured else f"assumed η {RT_CFG.inverter_efficiency:.0%}"
    col.caption(f"{est.critical_load_w:.0f} W load ({basis}) → {est.battery_w:.0f} W from battery "
                f"(losses {losses}) · {est.usable_kwh:.1f} kWh usable")


def tile(col, label, value, unit="", fmt="{:.0f}", help=None):
    col.metric(label, "-" if value is None else (fmt.format(value) + (f" {unit}" if unit else "")), help=help)


@st.fragment(run_every=refresh)
def live():
    try:
        j = api_get("/latest")
    except ApiError as e:
        show_api_error(e)
        return
    r = j.get("reading")
    if not r:
        st.info("No readings yet. Is the collector running?")
        return

    age = j.get("age_s")
    col = j.get("collector") or {}
    stale = age is not None and age > max(3 * float(col.get("poll_seconds") or 30), 120)
    hdr = st.columns([3, 2])
    hdr[0].caption(
        f"Last sample {fmt_age(age)} ago · source **{col.get('source', '?')}** · "
        f"params `{r.get('params_hash')}` · refreshed {time.strftime('%H:%M:%S')}")
    if col.get("last_error"):
        hdr[1].warning(f"Source error: {col['last_error']}", icon=":material/error:")
    elif stale:
        hdr[1].warning("Data is stale", icon=":material/schedule:")

    sigma = j.get("sigma")
    degraded = j.get("degraded")
    bcol, rcol = st.columns(2, vertical_alignment="center")
    sub = f"±{sigma * 100:.1f}%" + (" · DEGRADED" if degraded else "") if sigma is not None else ""
    bcol.html(battery_icon(r["soc_model"] * 100, sub))
    runtime_section(rcol, r["soc_model"])

    tiles(r, j, sigma, degraded)

    now = time.time()
    try:
        rng = api_get("/range", **{"from": now - 6 * 3600, "to": now, "bucket": 60})
    except ApiError as e:
        show_api_error(e)
        return
    df = to_df(rng["rows"])
    if not df.empty:
        st.plotly_chart(power_soc_figure(df), width="stretch", key="pw6h")


def tiles(r: dict, j: dict, sigma: float | None, degraded: bool | None):
    c = st.columns(4)
    tile(c[0], "Pack voltage", r["v"], "V", "{:.2f}")
    tile(c[1], "Battery current", r["i"], "A", "{:+.1f}", help="+ = charging")
    tile(c[2], "Battery power", r["p"], "W", "{:+.0f}", help="+ = charging")
    c[3].metric("Status", r.get("status") or "-")
    c = st.columns(4)
    tile(c[0], "PV MPPT1", r.get("pv1_w"), "W")
    tile(c[1], "PV MPPT2", r.get("pv2_w"), "W")
    tile(c[2], "Load", r.get("load_w"), "W")
    tile(c[3], "Grid", r.get("grid_w"), "W", "{:+.0f}", help="+ = import")
    c = st.columns(4)
    c[0].metric("Since full reset", fmt_age(j.get("since_reset_s")))
    tile(c[1], "Estimate σ", sigma * 100 if sigma is not None else None, "%", "{:.2f}",
         help="sqrt(Pcov). Grows with time since the last full charge or correction.")
    c[2].metric("Quality", "Degraded" if degraded else "OK",
                help="Degraded after a data gap, until a full charge or σ recovers.")
    tile(c[3], "V residual", r.get("v_resid"), "V", "{:+.3f}", help="measured − model-predicted")


def power_soc_figure(df) -> go.Figure:
    """Power flows (kW, left axis) with model SOC (%, right axis).

    Display signs: battery + = discharging (the stored `p` is + = charging, so it is negated
    here only); grid + = import.
    """
    fig = go.Figure()
    for y, name, color in (
        (df["pv_w"] / 1000, "Solar", COLORS["pv"]),
        (df["load_w"] / 1000, "Consumption", COLORS["load"]),
        (-df["p"] / 1000, "Battery (+ discharge)", COLORS["battery"]),
        (df["grid_w"] / 1000, "Grid (+ import)", COLORS["grid"]),
    ):
        if y.notna().any():
            fig.add_scatter(x=df["time"], y=y, name=name, line=dict(color=color, width=2),
                            hovertemplate="%{y:.2f} kW")
    fig.add_scatter(x=df["time"], y=df["soc_model"] * 100, name="SOC", yaxis="y2",
                    line=dict(color=COLORS["soc"], width=2), hovertemplate="%{y:.1f}%")
    plot_layout(fig, 420, "Power (kW)")
    fig.update_layout(yaxis=dict(zeroline=True, zerolinecolor="#9a9893"),
                      yaxis2=dict(title="SOC (%)", overlaying="y", side="right", range=[0, 100],
                                  showgrid=False, ticksuffix="%"))
    return fig


live()
