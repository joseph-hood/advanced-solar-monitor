import time

import plotly.graph_objects as go
import streamlit as st
from common import COLORS, ApiError, S, api_get, fmt_age, plot_layout, show_api_error, to_df

st.title("Live")
refresh = st.sidebar.slider("Refresh every (s)", 5, 120, int(S.dashboard_refresh_seconds), 5)


def gauge(title: str, value: float, color: str, sub: str = "") -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=value,
        number={"suffix": "%", "valueformat": ".1f", "font": {"size": 38}},
        title={"text": f"{title}<br><span style='font-size:0.7em'>{sub}</span>", "font": {"size": 17}},
        gauge={"axis": {"range": [0, 100], "tickvals": [0, 25, 50, 75, 100]},
               "bar": {"color": color, "thickness": 0.35}, "borderwidth": 0},
    ))
    fig.update_layout(height=240, margin=dict(l=40, r=40, t=70, b=10))
    return fig


def soc_gauge(col, key: str, title: str, value: float | None, color: str, sub: str, missing: str):
    if value is None:
        with col.container(border=True, height=240):
            st.markdown(f"**{title}**")
            st.caption(missing)
        return
    col.plotly_chart(gauge(title, value, color, sub), width="stretch", key=key)


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
    g = st.columns(3)
    sub = f"±{sigma * 100:.1f}%" + (" · DEGRADED" if degraded else "") if sigma is not None else ""
    soc_gauge(g[0], "g_model", "Model SOC", r["soc_model"] * 100, COLORS["model"], sub, "")
    soc_gauge(g[1], "g_bms", "Daly BMS SOC", r.get("soc_bms"), COLORS["bms"], "independent check",
              "Not available yet. Arrives when the Daly BMS is readable (e.g. via SolarAssistant).")
    soc_gauge(g[2], "g_inv", "Inverter SOC", r.get("soc_inverter"), COLORS["inverter"],
              "voltage mode, comparison only", "Not reported by the source.")

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

    now = time.time()
    try:
        rng = api_get("/range", **{"from": now - 6 * 3600, "to": now, "bucket": 60})
    except ApiError as e:
        show_api_error(e)
        return
    df = to_df(rng["rows"])
    if df.empty:
        return
    left, right = st.columns(2)
    fig = go.Figure()
    fig.add_scatter(x=df["time"], y=df["soc_model"] * 100, name="Model", line=dict(color=COLORS["model"], width=2))
    if df["soc_bms"].notna().any():
        fig.add_scatter(x=df["time"], y=df["soc_bms"], name="Daly", line=dict(color=COLORS["bms"], width=2))
    fig.add_scatter(x=df["time"], y=df["soc_inverter"], name="Inverter",
                    line=dict(color=COLORS["inverter"], width=2, dash="dot"))
    left.plotly_chart(plot_layout(fig, 280, "SOC %"), width="stretch", key="soc6h")
    fig = go.Figure()
    fig.add_scatter(x=df["time"], y=df["pv_w"], name="PV", line=dict(color=COLORS["pv"], width=2))
    fig.add_scatter(x=df["time"], y=df["p"], name="Battery (+chg)", line=dict(color=COLORS["model"], width=2))
    fig.add_scatter(x=df["time"], y=df["load_w"], name="Load", line=dict(color=COLORS["load"], width=2))
    right.plotly_chart(plot_layout(fig, 280, "W"), width="stretch", key="pw6h")


live()
