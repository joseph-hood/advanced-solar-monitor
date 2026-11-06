# eg4mon: notes for Claude / contributors

- Read `docs/HANDOFF.md` §4 and §6 before touching the model or "fixing" a constant.
  `src/eg4mon/model/lfp_soc_model.py` is pure and stdlib-only: no I/O, no clocks.
  Wrap it; don't add dependencies to it.
- Sign conventions: battery current/power + = charging; grid + = import.
- Process boundaries: the collector is the only DB writer; the API is read-only (plus the
  stateless `/replay`); the dashboard talks only to the API.
- Do not remove the EKF gates (|I| < 40 A, ≥ 300 s between corrections) or the gap logic.
- Read-only in v1: never add calls to EG4 write endpoints (`remoteSet`, `functionControl`, ...).
- Secrets live in `.env` only. Never commit `.env`, `discovery/`, `topics_sample.txt` or databases.
- Run: `pip install -e ".[dashboard,mqtt,dev]"`, then `pytest -q` and `ruff check src tests app scripts`.
- Demo without hardware: `eg4mon simulate` + `EG4MON_SOURCE=replay` (see README).
