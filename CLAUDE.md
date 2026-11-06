# eg4mon: notes for Claude / contributors

Always-on monitor for an EG4 FlexBOSS21 (+ optional GridBOSS) with a fitted LiFePO4 SoC model
for a DIY 16S 805 Ah bank. The inverter runs in lead-acid (voltage) mode, so its SOC is not
usable; this project's model is the real estimate. Full background: `docs/HANDOFF.md`.
User docs: `README.md`.

## Run

```bash
pip install -e ".[dashboard,mqtt,dev]"
```
```bash
pytest -q && ruff check src tests app scripts      # both must pass before any push (CI runs 3.11 + 3.13)
```
```bash
eg4mon discover      # EG4 cloud: list devices, dump raw JSON to discovery/
eg4mon collect       # source -> model -> SQLite (only DB writer)
eg4mon api           # read-only FastAPI on :8000 (/docs)
eg4mon dashboard     # Streamlit on :8501
eg4mon simulate      # synthetic CSV; use with EG4MON_SOURCE=replay for a hardware-free demo
eg4mon replay x.csv  # offline: CSV through the full pipeline, prints summary
```

## Layout

| Path | What |
|---|---|
| `src/eg4mon/model/lfp_soc_model.py` | `SocEstimator`, `ModelParams`, `ocv`/`docv`. Pure, stdlib-only. |
| `src/eg4mon/pipeline/estimator_service.py` | wraps the estimator: validation, gaps, degraded flag, events, daily rollups, full-to-full Ah closure, state persistence |
| `src/eg4mon/sources/` | `base.Sample` (normalized) + `eg4_cloud`, `solar_assistant` (MQTT), `replay` (CSV) |
| `src/eg4mon/store/` | SQLite WAL, `schema.sql` (readings, events, daily, cycles, kv) |
| `src/eg4mon/api/main.py` | FastAPI; `run_replay()` is shared with the CLI |
| `src/eg4mon/collector.py`, `cli.py`, `settings.py` | process wiring; settings from `.env` via pydantic-settings |
| `src/eg4mon/sim.py` | synthetic telemetry (its "true" OCV has an extra top shoulder so CV tapers) |
| `app/` | Streamlit: `streamlit_app.py` + `pages/` (live, history, diagnostics, cells, replay); `common.py` = API client, colors |
| `config/` | `model_params.yaml`, `pipeline.yaml`, `eg4_cloud.yaml` (JSON field map), `topics.yaml` (SA, placeholders) |
| `deploy/systemd/`, `Dockerfile`, `docker-compose.yml` | deployment (Docker path not yet tested) |

## Rules

- Read `docs/HANDOFF.md` §4 and §6 before touching the model or "fixing" a constant.
  The model is pure and stdlib-only: no I/O, no clocks. Wrap it; don't add dependencies to it.
- Sign conventions: battery current/power + = charging; grid + = import.
- Process boundaries: the collector is the only DB writer. The API is read-only, plus the
  stateless `/replay`. The dashboard talks only to the API, never to the DB or a source.
- Do not remove the EKF gates (|I| < 40 A, ≥ 300 s between corrections) or the gap logic
  (dt > 900 s: integrate I = 0, inflate Pcov, mark degraded).
- Read-only in v1: never add calls to EG4 write endpoints (`remoteSet`, `functionControl`, ...).
  Future writes need an explicit allow-list, confirmation, rate limit and audit log (HANDOFF §8).
- Secrets live in `.env` only. Never commit `.env`, `discovery/`, `topics_sample.txt` or databases.
  Discovery dumps contain serial numbers.
- Any change to model params changes `ModelParams.hash()`. Readings store that hash, and a
  change is logged as a `params_changed` event. That's intended; don't "fix" it.
- `tests/test_model.py::test_yaml_params_match_defaults` keeps `config/model_params.yaml` and the
  dataclass defaults in sync. Update both together.

## EG4 cloud API notes (undocumented)

- Endpoints follow the portal and the pylxpweb project; see the docstring in `sources/eg4_cloud.py`.
- Requests **must** send `Accept: application/json` + `X-Requested-With: XMLHttpRequest`, or the
  portal returns its HTML login page with HTTP 200. Login is verified to work against the real portal.
- Session expiry shows up as HTML or a 30x/401/403. The client logs in again once and retries.
- `vBat` is in 0.1 V units. Battery power = `pCharge − pDisCharge`. The sign and scale are
  config (`config/eg4_cloud.yaml`), not code.
- Samples are only emitted when `deviceTime` changes, so the real update rate is the dongle's
  upload rate, not the poll rate.

## Status / next steps

Done: M1 (model from spec), M3 API, M4 Live + History, M5 Diagnostics + Replay, M2 code
(gap/restart tests). EG4 cloud login works on real hardware.

Open:
- Verify against a real `discover` dump that the `config/eg4_cloud.yaml` mappings are right
  (GridBOSS `midboxData.*` names especially), and that the battery sign matches the EG4 app.
- M2 acceptance: 24 h unattended run; `systemctl restart` keeps the estimate.
- M1 acceptance: the handoff bundle (reference `lfp_soc_model.py`, 5-day fixture, `golden_soc.csv`,
  `test_model_golden.py`) isn't in the repo yet. Add the fixture as
  `tests/fixtures/eg4_batt_*.csv`; `test_real_fixture_voltage_rms` (≤ 0.115 V) then runs
  automatically. Diff the reference module against ours. Known deliberate difference: we add
  process noise on every step, not only on correction steps.
- SolarAssistant (M0/M6): capture topics, fix `config/topics.yaml`, verify the sign. The Daly BMS
  then fills the BMS gauge and the Cells page. On Windows, the MQTT source would need a
  selector event loop (not done).
- Pending owner measurements that are config-only changes: η vs I_p (overnight clamp meter),
  Q (capacity test). See HANDOFF §6.
