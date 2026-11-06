"""eg4mon dashboard entry point: `eg4mon dashboard` or `streamlit run app/streamlit_app.py`."""

import streamlit as st

st.set_page_config(page_title="EG4 Monitor", page_icon="🔋", layout="wide")

pages = [
    st.Page("pages/live.py", title="Live", icon=":material/bolt:", default=True),
    st.Page("pages/history.py", title="History", icon=":material/timeline:"),
    st.Page("pages/diagnostics.py", title="Model diagnostics", icon=":material/troubleshoot:"),
    st.Page("pages/cells.py", title="Cells", icon=":material/battery_full:"),
    st.Page("pages/replay.py", title="Replay", icon=":material/replay:"),
]
st.navigation(pages).run()
