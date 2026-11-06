import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import COLORS, ApiError, S, cached_get, plot_layout, range_picker, show_api_error, to_df

st.title("Model diagnostics")
t0, t1 = range_picker("diag", default="3 d")
try:
    rng = cached_get("/range", **{"from": int(t0), "to": int(t1), "bucket": 300})
    events = cached_get("/events", **{"from": int(t0), "to": int(t1)})
    cycles = cached_get("/cycles")
    params = cached_get("/model/params")
    ocv = cached_get("/model/ocv")
except ApiError as e:
    show_api_error(e)
    st.stop()

df = to_df(rng["rows"])
if df.empty:
    st.info("No data in this range.")
    st.stop()

resid = df["v_resid"].dropna()
c = st.columns(4)
c[0].metric("Voltage RMS residual", f"{np.sqrt((resid ** 2).mean()):.3f} V", help="Fit on the fixture: 0.112 V")
c[1].metric("Mean residual", f"{resid.mean():+.3f} V")
c[2].metric("Current σ", f"{np.sqrt(df['pcov'].iloc[-1]) * 100:.2f} %")
c[3].metric("Params hash", params["hash"])

left, right = st.columns([2, 1])
fig = go.Figure()
fig.add_scatter(x=df["time"], y=df["v_resid"], name="V − V̂", line=dict(color=COLORS["model"], width=2))
fig.add_hline(y=0, line_width=1, line_color="gray")
left.subheader("Voltage residual")
left.plotly_chart(plot_layout(fig, 280, "V"), width="stretch")
fig = go.Figure(go.Histogram(x=resid, nbinsx=40, marker_color=COLORS["model"], marker_line_width=0))
fig.update_layout(bargap=0.05)
right.subheader("Distribution")
right.plotly_chart(plot_layout(fig, 280, "count"), width="stretch")

fig = go.Figure()
fig.add_scatter(x=df["time"], y=np.sqrt(df["pcov"]) * 100, name="σ", line=dict(color=COLORS["model"], width=2))
st.subheader("Estimate uncertainty √Pcov")
st.plotly_chart(plot_layout(fig, 240, "σ (SOC %)"), width="stretch")

st.subheader("Ah closure per full-to-full interval")
st.caption("η·Ah_in − Ah_out − I_p·T between consecutive full-charge resets. Should be ≈ 0; "
           "a trend means η, I_p or capacity are drifting (HANDOFF §6.1).")
cy = pd.DataFrame(cycles)
if cy.empty:
    st.info("No complete full-to-full interval yet (needs two full-charge resets).")
else:
    cy["end"] = pd.to_datetime(cy["end_ts"], unit="s", utc=True).dt.tz_convert(S.site_tz)
    fig = go.Figure(go.Bar(
        x=cy["end"], y=cy["closure_ah"], marker_color=COLORS["model"], marker_line_width=0,
        customdata=np.stack([cy["hours"], cy["ah_in"], cy["ah_out"], cy["has_gap"]], axis=-1),
        hovertemplate="closure %{y:+.1f} Ah<br>%{customdata[0]:.1f} h, in %{customdata[1]:.1f} Ah, "
                      "out %{customdata[2]:.1f} Ah<br>gap: %{customdata[3]}<extra></extra>"))
    fig.add_hline(y=0, line_width=1, line_color="gray")
    st.plotly_chart(plot_layout(fig, 260, "Ah"), width="stretch")
    st.dataframe(cy.drop(columns=["end"]), hide_index=True, width="stretch")

st.subheader("OCV curve with low-current operating points")
st.caption("Points are 5-minute averages with |I| < 10 A. They sit above the curve while charging "
           "(hysteresis and polarization), below while discharging.")
fig = go.Figure()
fig.add_scatter(x=np.array(ocv["soc"]) * 100, y=ocv["ocv"], name="Model OCV", line=dict(color=COLORS["measured"], width=2))
low = df[df["i"].abs() < 10]
if not low.empty:
    fig.add_scatter(x=low["soc_model"] * 100, y=low["v"], mode="markers", name="Measured (|I|<10 A)",
                    marker=dict(color=COLORS["model"], size=8, line=dict(width=2, color="white")))
fig.update_yaxes(range=[51.5, 55.0])
fig.update_layout(hovermode="closest")
st.plotly_chart(plot_layout(fig, 360, "V").update_layout(hovermode="closest", xaxis_title="SOC %"),
                width="stretch")

st.subheader("Events")
ev = to_df(events)
if ev.empty:
    st.info("No events in this range.")
else:
    kinds = sorted(ev["kind"].unique())
    pick = st.multiselect("Kinds", kinds, default=kinds)
    ev = ev[ev["kind"].isin(pick)]
    st.dataframe(ev[["time", "kind", "message"]], hide_index=True, width="stretch")
