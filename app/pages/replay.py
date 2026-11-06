import plotly.graph_objects as go
import streamlit as st
from common import COLORS, ApiError, api_post_file, plot_layout, to_df

st.title("Replay")
st.caption("Upload a CSV (fixture format or normalized columns), run it through the full pipeline "
           "with the current model params, and plot. Nothing is stored.")
f = st.file_uploader("Telemetry CSV", type=["csv"])
c1, c2 = st.columns(2)
init = c1.number_input("Initial SOC (cold start)", 0.0, 1.0, 0.8, 0.05)
tz = c2.text_input("Timezone for naive timestamps", value="")
if not f:
    st.stop()
with st.spinner("Running estimator..."):
    try:
        params = {"initial_soc": init}
        if tz:
            params["tz"] = tz
        res = api_post_file("/replay", f.name, f.getvalue(), **params)
    except ApiError as e:
        st.error(str(e))
        st.stop()

df = to_df(res["rows"])
c = st.columns(4)
c[0].metric("Steps", len(df))
c[1].metric("Voltage RMS", f"{res['v_rms']:.4f} V" if res["v_rms"] is not None else "-")
c[2].metric("Full resets", sum(e["kind"] == "full_reset" for e in res["events"]))
c[3].metric("Params", res["params_hash"])

fig = go.Figure()
fig.add_scatter(x=df["time"], y=df["soc_model"] * 100, name="Model", line=dict(color=COLORS["model"], width=2))
if df["soc_inverter"].notna().any():
    fig.add_scatter(x=df["time"], y=df["soc_inverter"], name="Inverter",
                    line=dict(color=COLORS["inverter"], width=2, dash="dot"))
st.plotly_chart(plot_layout(fig, 320, "SOC %"), width="stretch")
fig = go.Figure()
fig.add_scatter(x=df["time"], y=df["v"], name="Measured", line=dict(color=COLORS["measured"], width=2))
fig.add_scatter(x=df["time"], y=df["v_hat"], name="Model V̂", line=dict(color=COLORS["model"], width=2))
st.plotly_chart(plot_layout(fig, 300, "V"), width="stretch")
if res["cycles"]:
    st.subheader("Full-to-full intervals")
    st.dataframe(res["cycles"], hide_index=True, width="stretch")
with st.expander("Events"):
    st.dataframe(res["events"], hide_index=True, width="stretch")
st.download_button("Download results CSV", df.drop(columns=["time"]).to_csv(index=False),
                   "replay_results.csv", "text/csv")
