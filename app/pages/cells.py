import plotly.graph_objects as go
import streamlit as st
from common import COLORS, ApiError, cached_get, plot_layout, range_picker, show_api_error, to_df

st.title("Cells")
t0, t1 = range_picker("cells")
try:
    rng = cached_get("/range", **{"from": int(t0), "to": int(t1)})
except ApiError as e:
    show_api_error(e)
    st.stop()
df = to_df(rng["rows"])
if df.empty or df[["cell_v_min", "cell_v_max"]].isna().all().all():
    st.info("No BMS cell data yet. Cell voltages and temperatures arrive once the Daly BMS is "
            "readable (e.g. through SolarAssistant): set EG4MON_SOURCE=solar_assistant and map "
            "the battery topics in config/topics.yaml.")
    st.stop()

last = df.dropna(subset=["cell_v_min"]).iloc[-1]
c = st.columns(4)
c[0].metric("Cell min", f"{last['cell_v_min']:.3f} V")
c[1].metric("Cell max", f"{last['cell_v_max']:.3f} V")
c[2].metric("Delta", f"{(last['cell_v_max'] - last['cell_v_min']) * 1000:.0f} mV")
c[3].metric("Temp max", "-" if last.get("temp_max") != last.get("temp_max") else f"{last['temp_max']:.1f} °C")

fig = go.Figure()
fig.add_scatter(x=df["time"], y=df["cell_v_max"], name="Max", line=dict(color=COLORS["inverter"], width=2))
fig.add_scatter(x=df["time"], y=df["cell_v_min"], name="Min", line=dict(color=COLORS["model"], width=2))
st.plotly_chart(plot_layout(fig, 300, "V/cell"), width="stretch")
fig = go.Figure()
fig.add_scatter(x=df["time"], y=(df["cell_v_max"] - df["cell_v_min"]) * 1000, name="Delta",
                line=dict(color=COLORS["model"], width=2))
st.subheader("Cell imbalance")
st.plotly_chart(plot_layout(fig, 240, "mV"), width="stretch")
if df[["temp_min", "temp_max"]].notna().any().any():
    fig = go.Figure()
    for col, color in (("temp_max", COLORS["inverter"]), ("temp_min", COLORS["model"])):
        fig.add_scatter(x=df["time"], y=df[col], name=col, line=dict(color=color, width=2))
    st.subheader("Temperatures")
    st.plotly_chart(plot_layout(fig, 240, "°C"), width="stretch")
