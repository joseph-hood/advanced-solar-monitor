import plotly.graph_objects as go
import streamlit as st
from common import COLORS, ApiError, cached_get, plot_layout, range_picker, show_api_error, to_df

st.title("History")
t0, t1 = range_picker("hist")
try:
    rng = cached_get("/range", **{"from": int(t0), "to": int(t1)})
    daily = cached_get("/daily", days=60)
except ApiError as e:
    show_api_error(e)
    st.stop()

df = to_df(rng["rows"])
st.caption(f"{rng['count']} points, {rng['bucket']} s buckets (downsampled by the API)")
if df.empty:
    st.info("No data in this range.")
    st.stop()

fig = go.Figure()
fig.add_scatter(x=df["time"], y=df["soc_model"] * 100, name="Model", line=dict(color=COLORS["model"], width=2))
if df["soc_bms"].notna().any():
    fig.add_scatter(x=df["time"], y=df["soc_bms"], name="Daly BMS", line=dict(color=COLORS["bms"], width=2))
fig.add_scatter(x=df["time"], y=df["soc_inverter"], name="Inverter (voltage mode)",
                line=dict(color=COLORS["inverter"], width=2, dash="dot"))
st.subheader("State of charge")
st.plotly_chart(plot_layout(fig, 340, "SOC %"), width="stretch")

fig = go.Figure()
fig.add_scatter(x=df["time"], y=df["v"], name="Measured V", line=dict(color=COLORS["measured"], width=2))
fig.add_scatter(x=df["time"], y=df["v_hat"], name="Model V̂", line=dict(color=COLORS["model"], width=2))
st.subheader("Pack voltage: measured vs model")
st.plotly_chart(plot_layout(fig, 300, "V"), width="stretch")

st.subheader("Power")
fig = go.Figure()
for col, name, color in (("pv_w", "PV", COLORS["pv"]), ("p", "Battery (+ charge)", COLORS["battery"]),
                         ("load_w", "Load", COLORS["load"]), ("grid_w", "Grid (+ import)", COLORS["grid"])):
    if col in df and df[col].notna().any():
        fig.add_scatter(x=df["time"], y=df[col], name=name, line=dict(color=color, width=2))
st.plotly_chart(plot_layout(fig, 320, "W"), width="stretch")

st.subheader("Daily energy")
dd = to_df(daily)
if not dd.empty:
    fig = go.Figure()
    for col, name, color in (("pv_kwh", "PV", COLORS["pv"]), ("kwh_in", "Battery in", COLORS["model"]),
                             ("kwh_out", "Battery out", COLORS["inverter"]), ("load_kwh", "Load", COLORS["load"]),
                             ("grid_import_kwh", "Grid import", COLORS["grid"])):
        if dd[col].fillna(0).any():
            fig.add_bar(x=dd["day"], y=dd[col], name=name, marker_color=color)
    fig.update_layout(barmode="group", bargap=0.25, bargroupgap=0.08)
    fig.update_traces(marker_line_width=0)
    st.plotly_chart(plot_layout(fig, 320, "kWh"), width="stretch")
    with st.expander("Daily table"):
        st.dataframe(dd.drop(columns=["time"], errors="ignore"), hide_index=True, width="stretch")
