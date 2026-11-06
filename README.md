# advanced-solar-monitor (`eg4mon`)

An always-on monitor for **EG4 FlexBOSS21 / GridBOSS** systems that does what the stock apps don't:

- **Auto-refreshing dashboard** (Streamlit, phone-friendly) with a configurable refresh rate.
- **A real state-of-charge estimate for a DIY LiFePO4 bank**: a fitted OCV + RC + hysteresis model
  with coulomb counting, rate-limited EKF voltage correction and full-charge resets. It runs
  live on every sample, so you don't have to rely on the inverter's voltage-mode SOC.
- **History in SQLite**, daily energy rollups, and model diagnostics. The diagnostics include
  voltage residuals, uncertainty, events, and the Ah closure between full charges, which shows
  drift in charge efficiency, parasitic drain or capacity.
- **Read-only.** v1 never writes to the inverter.

Data sources are pluggable:

| Source | Status | Notes |
|---|---|---|
| `eg4_cloud` | **default** | Polls the EG4 cloud portal (`monitor.eg4electronics.com`) with your normal login. |
| `solar_assistant` | ready, needs topic mapping | Subscribes to SolarAssistant over MQTT. Lower latency, and it can read the Daly BMS. |
| `replay` | for dev and demos | Plays a CSV through the same pipeline. |

```
EG4 cloud / SolarAssistant MQTT / CSV
        │  source adapter → normalized Sample (+ = charge)
        ▼
    collector ── SocEstimator (pure) + gaps, events, rollups
        │
        ▼
  SQLite (WAL) ──► FastAPI (read-only) ──► Streamlit dashboard
```

The collector is the only DB writer. The API is read-only. The dashboard talks only to the API.

## Quick start: demo without hardware

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dashboard,mqtt,dev]"
cp .env.example .env
```

In `.env`, set `EG4MON_SOURCE=replay`, `REPLAY_CSV=data/simulated.csv` and your `SITE_TZ`. Then:

```bash
eg4mon simulate --days 5 --seed 2 --soc0 0.85
```

```bash
eg4mon collect
```

```bash
eg4mon api &
```

```bash
eg4mon dashboard
```

Open http://localhost:8501. The simulated week ends "now", so the Live and History pages fill in right away.

## Using it with your EG4 system

1. Put your EG4 portal login in `.env` (`EG4_USERNAME`, `EG4_PASSWORD`). A separate **viewer**
   account is a good idea. Keep `EG4MON_SOURCE=eg4_cloud`.
2. Discover your devices and look at the raw data:
   ```bash
   eg4mon discover
   ```
   This lists plants and devices and writes the raw runtime JSON to `discovery/`, which is
   git-ignored because it contains serial numbers.
3. Set `EG4_INVERTER_SERIALS` (and `EG4_GRIDBOSS_SERIAL` if you have one) in `.env`.
4. **Check the field map** in `config/eg4_cloud.yaml` against the dumped JSON. Pay attention to
   the **battery power sign** (+ = charging, built as `pCharge − pDisCharge`) and the `vBat`
   scaling (0.1 V units). Compare one reading with the EG4 app.
5. Run the three services (see [Deployment](#deployment)).

### How fast does it refresh?

The collector polls every `EG4_POLL_SECONDS` (default 30, minimum 10). It only steps the model
when the cloud's `deviceTime` changes, so stale cloud data is never counted twice. How often the
dongle uploads limits the real update rate. For true seconds-level data, switch to SolarAssistant
(below). The dashboard re-renders its live tiles every `DASHBOARD_REFRESH_SECONDS`, and you can
also change this with a slider in the UI.

## Switching to SolarAssistant later

1. Enable MQTT in SolarAssistant and capture what it really publishes:
   ```bash
   mosquitto_sub -h <pi-host> -t 'solar_assistant/#' -v | tee topics_sample.txt
   ```
2. Fix `config/topics.yaml`. The names in it are placeholders. **Verify the battery power sign**
   against the SolarAssistant UI; `scale: -1` flips it.
3. Set `EG4MON_SOURCE=solar_assistant` and `MQTT_HOST`, then restart the collector. The estimator
   state carries over.
4. If SolarAssistant reads the Daly BMS, map the `bms_*`, `cell_v_*` and `temp_*` signals. The
   Daly SOC gauge and the Cells page start filling in, and a cold start seeds from the BMS SOC.

`scripts/replay_mqtt.py` acts as a fake SolarAssistant. It publishes a CSV to a local mosquitto
using the same topic map, so this path can be developed without the Pi.

## Configuration

| File | What |
|---|---|
| `.env` | credentials, source selection, paths, timezone, ports (template: `.env.example`) |
| `config/model_params.yaml` | SoC model constants. Each stored row carries a hash of this set. |
| `config/pipeline.yaml` | sanity limits, gap handling, degraded flag, cold start, retention |
| `config/eg4_cloud.yaml` | EG4 cloud JSON → signal map (dotted paths, scales, sums) |
| `config/topics.yaml` | SolarAssistant topic → signal map |

## The SoC model, in short

The model is in `src/eg4mon/model/lfp_soc_model.py`: pure Python, stdlib only, no I/O. The full
spec and the reasoning behind each constant are in [`docs/HANDOFF.md`](docs/HANDOFF.md) §4–6.

- Coulomb counting with charge factor η and a constant unmetered drain `I_p`.
- Predicted voltage: OCV(SOC) + R0·I + RC polarization + current-direction hysteresis.
- **Full-charge reset** when V ≥ 54.7 V and 0 < I < 10 A.
- **EKF correction** only at |I| < 40 A, and at most once per 300 s. The noise was fitted on
  5-minute data, so don't remove that gate.
- **Gaps** (dt > 900 s): stale current is not integrated, only `I_p` drains. Covariance is
  inflated and the estimate is flagged *degraded* until a full charge or until σ recovers.

Pipeline behaviour you can rely on:

- State is persisted every step. A restart resumes the estimate, and the downtime is treated as a gap.
- Implausible samples are logged and dropped: V outside 40–60 V, or |P| > 20 kW.
- Changing the params is logged as an event, and each reading records which params produced it.

The inverter SOC is shown for comparison only. In voltage (lead-acid) mode it is not a usable SOC.

## Dashboard

| Page | |
|---|---|
| **Live** | Model / Daly / inverter SOC gauges, pack V/I/P, PV per MPPT, load, grid, status, time since full charge, estimate σ and quality |
| **History** | SOC (all three), measured vs predicted voltage, power, daily energy |
| **Model diagnostics** | residual over time and histogram, √Pcov, Ah closure per full-to-full interval, OCV curve with low-current operating points, event log |
| **Cells** | Daly min/max cell voltage, imbalance, temperatures (when available) |
| **Replay** | upload a CSV, run the estimator with current params, plot it and download the results |

## API

`eg4mon api` serves interactive docs at `/docs`. If `API_TOKEN` is set, every endpoint except
`/health` needs `Authorization: Bearer <token>`.

| Endpoint | |
|---|---|
| `GET /latest` | latest reading + σ, degraded flag, time since full reset, collector status |
| `GET /range?from&to&bucket` | readings, averaged into `bucket`-second buckets (auto-picked to stay ≤ 3000 points) |
| `GET /daily?days` | daily rollups: Ah/kWh in/out, PV/load/grid kWh, SOC min/max, resets, gap time |
| `GET /events?from&to&kind` | full_reset, gap, degraded_cleared, large_correction, rejected, cold_start, params_changed |
| `GET /cycles` | full-to-full intervals with the Ah closure |
| `GET /model/params`, `GET /model/ocv` | active params + hash + estimator state; OCV curve |
| `POST /replay` | CSV upload → full pipeline in memory → rows/events/cycles |

## Replay CSV format

The CSV needs a time column (`ts` / `time` / `timestamp`, either unix seconds or ISO 8601;
naive times are read as `SITE_TZ`) and a voltage column (`battery_v` or `vBat`; 0.1 V units are
auto-detected). Battery power can be given as `battery_p` or as the pair `pCharge` + `pDisCharge`.
Optional columns: `soc`, `statusText`, `ppv1`, `ppv2`, `load_w`, `grid_w`, BMS fields.

```bash
eg4mon replay my_export.csv --out results.csv
```

## Deployment

**systemd** (on a Pi or a home server). Units are in `deploy/systemd/`. They assume
`/opt/eg4mon` with a venv in `.venv` and a user called `eg4mon`.

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
```

```bash
sudo systemctl enable --now eg4mon-collect eg4mon-api eg4mon-dashboard
```

**docker compose**: `docker compose up -d`. It uses `.env` and keeps data in a named volume.

SQLite in WAL mode is plenty for one site. At a 30 s poll that's about 3k rows/day; at a 5 s
MQTT rate, about 17k rows/day. Set `retention.raw_days` in `config/pipeline.yaml` to prune raw
rows. Daily and cycle rollups are always kept.

## Development

```bash
pytest -q
```

```bash
ruff check src tests app scripts
```

The tests cover the model math, the sign convention, gaps, restart and persistence, the full CSV
pipeline vs the bare estimator, tracking a simulated pack, the EG4 client (mocked portal:
login, session expiry, dedupe, backoff), the SolarAssistant assembler, and the API.

The 5-day real-telemetry fixture from the handoff is not in the repo yet. Drop it in as
`tests/fixtures/eg4_batt_*.csv` and the voltage-RMS acceptance test (≤ 0.115 V) runs
automatically.

## Status vs. milestones (docs/HANDOFF.md §8)

| | |
|---|---|
| M0 topic discovery | `eg4mon discover` for the EG4 cloud; `topics.yaml` + capture steps for SolarAssistant |
| M1 model + golden test | model implemented from the spec. **Pending:** reference module/fixture/golden CSV to diff against |
| M2 collector → SQLite | done (gap logic, restart, events tested); 24 h unattended run pending on real hardware |
| M3 API | done |
| M4 Live + History | done |
| M5 Diagnostics + Replay | done (Ah closure per full-to-full interval) |
| M6 Daly | plumbing done (signals, gauge, Cells page); needs SolarAssistant or another BMS path |

## Disclaimer

Not affiliated with EG4 Electronics, Luxpower or SolarAssistant. The EG4 cloud API is
undocumented and may change. The endpoints follow the web portal and the
[pylxpweb](https://pypi.org/project/pylxpweb/) project. MIT licensed.
