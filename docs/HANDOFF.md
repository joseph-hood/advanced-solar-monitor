# EG4 Monitor + Battery SoC Model: Handoff

> **Implementation notes (added when the repo was built):**
> - SolarAssistant is not available yet. v1 therefore polls the **EG4 cloud portal**
>   (`EG4MON_SOURCE=eg4_cloud`). The SolarAssistant MQTT path described below is implemented as
>   the `solar_assistant` source and needs only the topic mapping (§3.2) to switch over.
> - The bundle files listed below (reference `lfp_soc_model.py`, the 5-day fixture,
>   `golden_soc.csv`, `test_model_golden.py`, the validation plot) were not committed with this
>   doc. The model in `src/eg4mon/model/lfp_soc_model.py` is implemented from §4. When the bundle
>   is added, diff the two modules and run the golden test.
> - Process noise is added on every step (`P⁻ = P + q·Δt/3600`), not only on correction steps,
>   so the time between rate-limited corrections still grows the covariance.

Goal: a small always-on server that ingests EG4 FlexBOSS21/GridBOSS telemetry (via SolarAssistant), runs the fitted LiFePO4 SoC model in real time, stores history, and serves a Streamlit dashboard. **Read-only in v1**: no writes to the inverter.

This bundle contains:

```
HANDOFF.md                                  this doc
model/lfp_soc_model.py                      reference SoC estimator (stdlib only), the source of truth for the math
tests/fixtures/eg4_batt_2026-10-30_to_11-04.csv   5 days of real telemetry (5-min samples) used for the fit
tests/test_model_golden.py                  runnable regression test (passes against the bundled module)
tests/fixtures/golden_soc.csv               estimator output on that fixture (regression target)
docs/soc_model_validation.png               fit/convergence plot
```

---

## 1. System context

| Item | Detail |
|---|---|
| Inverter | EG4 FlexBOSS21 + GridBOSS. FlexBOSS is the only charger (PV on MPPT1/MPPT2, plus AC charge from the grid). No grid export. |
| Battery | DIY 16S LiFePO4, 48 V nominal, **805 Ah nameplate**, single Daly Smart BMS (16S, 200 A charge / 400 A discharge, CAN/485 + UART), cell balancing |
| Battery mode | FlexBOSS runs in **Lead-Acid (voltage) mode**: no closed-loop BMS comms. Charge 55.2 V setpoint; observed CV hold at **54.8 V**; float 54.0 V. |
| Data path | SolarAssistant on a Raspberry Pi, connected to the FlexBOSS (and ideally the Daly). SolarAssistant publishes over MQTT. |
| Planned architecture | SolarAssistant → MQTT broker → own backend → dashboard. Limited writes (mode toggles, SOC thresholds) come later. |

**The inverter-reported SOC is not usable.** It's the FlexBOSS voltage-mode estimate: it sits at 82–86% overnight while the pack actually swings about 30% (~230 Ah), and it jumps 85→94% during CV. Show it on the dashboard for comparison only; never use it as ground truth.

---

## 2. Architecture

```
FlexBOSS ──RS485──┐
                  ├─ SolarAssistant (Pi) ── MQTT ──► collector service (Python, asyncio)
Daly BMS ──UART───┘                                  │  • subscribes to topics
                                                     │  • runs SocEstimator per battery sample
                                                     │  • persists state every update
                                                     ▼
                                              SQLite (WAL) / or Timescale later
                                                     │
                                                FastAPI (read API)
                                                     │
                                                Streamlit app (polls API)
```

Recommended stack (adjust freely):
- Python 3.11+, `aiomqtt` (or `paho-mqtt`), `pydantic` for message models, `FastAPI` + `uvicorn`, `SQLite` in WAL mode (plenty for one site at a 5 s rate, ~20k rows/day), `streamlit` + `plotly`.
- Run the collector and API as systemd services (or docker compose) on the Pi or a home server. Streamlit can be a third service.
- Keep the model module **pure and dependency-free**. The collector wraps it.

Process boundaries:
- **collector**: owns MQTT, the estimator and DB writes. It's the only writer.
- **api**: read-only queries (latest, range, daily rollups, model diagnostics).
- **streamlit**: talks only to the API, never to MQTT or the DB directly. Use `st.fragment(run_every=...)` for live tiles.

---

## 3. Data contract

### 3.1 Signals needed

| Signal | Units | Used for | Notes |
|---|---|---|---|
| battery voltage | V (pack) | model input + EKF measurement | fixture has 0.1 V resolution |
| battery power | W | model input, I = P / V | **sign: + = charging** in the model. Verify SolarAssistant's sign; the fixture used `pCharge − pDisCharge`. |
| battery current (if published) | A | prefer over P/V when available | check sign; compare against P/V for a day before switching |
| PV power per MPPT | W | dashboard | |
| inverter SOC | % | dashboard comparison only | voltage-mode estimate, unreliable |
| inverter status/mode | text | dashboard, event annotation | fixture values: `Battery Grid off`, `PV Charge + Grid off`, `PV&Battery Grid off`, `PV&AC Charge`, `AC Charge` |
| grid / load power | W | dashboard | |
| **Daly SOC, current, cell min/max V, temps** (if SA reads the BMS) | %, A, V, °C | validation and future model inputs | the best independent check of the model. Cell delta V is a useful balance metric. |

### 3.2 Topic discovery (do this first)

SolarAssistant's topic names vary by version and device. **Don't hard-code them from memory.** Enable MQTT in SolarAssistant, then capture what's actually published:

```bash
mosquitto_sub -h <pi-host> -t 'solar_assistant/#' -v | tee topics_sample.txt
```

They typically look like `solar_assistant/<device>/<metric>/state` (e.g. under `inverter_1/`, `battery_1/`, `total/`). Put the final mapping in one config file (`config/topics.yaml`) so a renamed topic is a one-line fix.

### 3.3 Sampling, alignment and gaps

- SolarAssistant updates every few seconds, and the metrics arrive as separate messages. Keep a "latest value" cache and **step the estimator when a new battery-power (or current) value arrives**, using the latest voltage. dt = time since the previous step.
- The model was fitted on 5-minute data, but the math is dt-aware and valid at any rate. The voltage correction is **rate-limited to once per 300 s** (`ekf_interval`), because its noise variance `r` was fitted at 5-minute samples. Don't remove that gate.
- **Gaps**: if dt > 900 s (MQTT or Pi outage), don't integrate the stale current across the gap. Step with I = 0 for the gap (the parasitic `I_p` still drains), let `Pcov` grow, and mark the estimate as `degraded` until the next full-charge reset or until `Pcov` falls back below a threshold.
- Clamp absurd values (|P| > 20 kW, V outside 40–60 V) and log them; don't feed them to the estimator.

---

## 4. SoC model spec

`model/lfp_soc_model.py` is authoritative. The math, for reference (pack level, I > 0 = charge):

```
Iₖ    = Pₖ / Vₖ

SOCₖ⁻ = clamp₀¹( SOCₖ₋₁ + Δtₖ·(η·Iₖ⁺ − Iₖ⁻ − I_p) / (3600·Q) )          coulomb count

v1ₖ   = aₖ·v1ₖ₋₁ + R1·(1−aₖ)·Iₖ,               aₖ = e^(−Δtₖ/τ1)           RC polarization
hₖ    = bₖ·hₖ₋₁ + (1−bₖ)·M·sgn(Iₖ),             bₖ = e^(−γ·|Iₖ|·Δtₖ/(3600·Q))   hysteresis
V̂ₖ    = OCV(SOCₖ⁻) + R0·Iₖ + v1ₖ + hₖ                                        predicted voltage

Full reset:   if Vₖ ≥ 54.7 and 0 < Iₖ < 10 A  →  SOCₖ = 1, Pₖ = 0

EKF (only if |Iₖ| < 40 A and ≥ 300 s since the last correction):
  Pₖ⁻ = Pₖ₋₁ + q·Δtₖ/3600
  H   = OCV′(SOCₖ⁻)
  K   = Pₖ⁻·H / (H²·Pₖ⁻ + r)
  SOCₖ = clamp₀¹( SOCₖ⁻ + K·(Vₖ − V̂ₖ) ),   Pₖ = (1 − K·H)·Pₖ⁻

OCV(s)  = A + C·e^(D(s−1)) + E·σ((s−s₀)/w) − K_b·e^(−s/α_b)
OCV′(s) = C·D·e^(D(s−1)) + (E/w)·σ(1−σ) + (K_b/α_b)·e^(−s/α_b)
```

| Const | Value | Meaning |
|---|---|---|
| Q | 805 Ah | nameplate; **not identifiable from data** (see §6) |
| η | 1.0 | charge factor applied to telemetry |
| I_p | 1.22 A | constant unmetered drain / telemetry offset (~65 W) |
| A, C, D | 52.631 V, 0.4376 V, 3.787 | OCV baseline + top knee |
| E, s₀, w | 0.5224 V, 0.7087, 0.02398 | LFP plateau step (~3.33 → ~3.30 V/cell), ~235 Ah below full |
| K_b, α_b | 4.0 V, 0.04 | bottom knee: generic LFP prior, **not fitted** (no data below ~60%) |
| R0 | 5.99 mΩ | ohmic |
| R1, τ1 | 5.26 mΩ, 3470 s | polarization RC |
| M, γ | 0.2044 V, 400 | hysteresis half-width (12.8 mV/cell), switching rate |
| q, r | (0.002)²/h, 0.12² V² | EKF process / measurement noise |
| V_full, I_tail | 54.7 V, 10 A | full-charge reset condition |

Fit quality on the fixture: voltage RMS 0.112 V with EKF (0.122 V open-loop when anchored to a known full charge), against 0.1 V telemetry resolution. From a 55/75/95% wrong start, the EKF gets within 3% of the anchored reference in about 1.5 days without a full charge.

### 4.1 Estimator lifecycle requirements
- **Persist state** (`to_dict()`: soc, v1, h, Pcov, _since_corr) plus its timestamp after every step, or at least every 30 s. On restart, `load_dict()`, then treat the downtime as a gap (§3.3).
- **Cold start with no saved state**: initialize `soc` from Daly SOC if available, otherwise 0.8 with `Pcov = 0.04`. The EKF or the next full charge will fix it.
- Make params loadable from `config/model_params.yaml`. Store a params hash with each output row so history is attributable after a refit.
- Log every event: full-charge reset, gap, degraded flag, large EKF correction (|ΔSOC| > 2% in one step).

### 4.2 Outputs to store per step
`ts, V, I, P, soc_model, soc_inverter, soc_daly?, v_hat, v_resid, v1, h, Pcov, flags, params_hash`

Daily rollups (for the closure diagnostic): Ah in, Ah out, kWh in/out, min/max SOC, reset count. **Ah closure between consecutive full resets** (Ah_in − Ah_out − I_p·T) should be ≈ 0. Trending it is how you'll detect drift in η, I_p or capacity.

---

## 5. Streamlit app

Pages:
1. **Live**: SOC gauges (model, Daly, inverter side by side), pack V, I, P, PV per MPPT, load, status, time since last full reset, estimate quality (σ = √Pcov, degraded flag).
2. **History**: selectable range. SOC (all three), V measured vs V̂, power stack (PV / battery / load / grid), daily energy bars.
3. **Model diagnostics**: voltage residual over time + histogram, √Pcov, reset/gap/correction events, Ah closure per full-to-full interval, OCV curve with live (SOC, V) points overlaid at low current (|I| < 10 A).
4. **Cells** (if Daly is in SolarAssistant): min/max cell V, delta, temps.
5. **Replay**: upload a CSV in the fixture format, run the estimator, plot. Useful for refits and debugging.

Downsample long ranges in the API (e.g. 1-min or 5-min buckets), not in Streamlit.

---

## 6. Known data facts and caveats (read before "fixing" anything)

1. **Telemetry Ah doesn't close.** Between full charges on 11/03 14:42 and 11/04 14:47: 262.8 Ah in, 233.4 Ah out, so about 29 Ah/day is unaccounted for. Modeled as `I_p = 1.22 A` with `η = 1`. Equally plausible: the inverter over-reports charge current, giving `η = 0.888, I_p = 0`. **Pending a test**: clamp-meter the battery cable overnight and compare to the FlexBOSS-reported discharge current. Both are config values; no code change needed.
2. **Q is unidentifiable** from these 5 days. The voltage fit is equally good from 480 to 930 Ah. The plateau step at ~235 Ah below full hints usable capacity may be below 805 Ah. **Pending**: a capacity test or a discharge into the lower knee (rested pack below ~52.0 V).
3. During float after a full charge, the FlexBOSS reports a constant **~113 W "charge"** while the voltage relaxes from 54.8 to 53.8 V. That reading is probably not real charge current, but its effect is negligible (~5 Ah/day).
4. **Relaxation after CV** (54.8 → 53.8 V over about 2 h) is only approximately captured. Those afternoons hold the largest voltage residuals (up to −0.7 V). The current gate keeps the EKF from acting on them; don't loosen it.
5. **No temperature term.** The fixture has no temperature. Once Daly temps are logged, R0/R1 temperature dependence is the first refit candidate.
6. The bottom-knee OCV is a generic prior. SOC readings below about 60% are extrapolation until there's data down there.

---

## 7. Suggested repo layout

```
eg4-monitor/
  CLAUDE.md                 # short: conventions, how to run, pointer to this doc
  HANDOFF.md
  pyproject.toml
  config/ topics.yaml  model_params.yaml  app.yaml
  src/eg4mon/
    model/lfp_soc_model.py  # copied as-is; wrap, don't rewrite
    ingest/mqtt_client.py   # topic map → typed samples, latest-value cache
    pipeline/estimator_service.py  # alignment, gap logic, persistence, events
    store/db.py  schema.sql
    api/main.py             # FastAPI
  app/streamlit_app.py  app/pages/*.py
  tests/
    fixtures/...
    test_model_golden.py    # fixture → golden_soc.csv (tolerance 1e-6)
    test_pipeline_replay.py # fixture through the full pipeline (fake MQTT) → same SOC
    test_gaps.py  test_restart.py  test_sign_convention.py
  deploy/ systemd/*.service  (or docker-compose.yml)
```

---

## 8. Milestones and acceptance criteria

| # | Milestone | Done when |
|---|---|---|
| M0 | Topic discovery | `topics_sample.txt` captured; `topics.yaml` maps every signal in §3.1; battery power sign verified against the SolarAssistant UI |
| M1 | Model package + golden test | `test_model_golden.py` reproduces `golden_soc.csv` (all three columns); voltage RMS on the fixture ≤ 0.115 V (EKF) |
| M2 | Collector → SQLite | 24 h unattended run with no gaps > 30 s; estimator state survives `systemctl restart`; gap logic covered by a test |
| M3 | API | `/latest`, `/range?from&to&bucket`, `/daily`, `/events`, `/model/params` |
| M4 | Streamlit Live + History | pages 1–2 working on a phone browser over the LAN |
| M5 | Diagnostics + Replay | pages 3 and 5; Ah closure shown per full-to-full interval |
| M6 | Daly integration | Daly SOC/cells logged; model vs Daly SOC difference plotted |
| Later | Refit tooling, temperature term, controlled writes | writes behind an explicit allow-list and confirmation, rate-limited, with an audit log |

Replay harness: a fake publisher that plays a CSV onto a local mosquitto at N× speed. It lets M2–M5 be developed without the Pi.

---

## 9. Open questions for the owner

- Is the Daly connected to SolarAssistant (BMS topics available)?
- Where will the services run: on the SolarAssistant Pi or a separate box?
- Does SolarAssistant use its built-in broker or an external mosquitto?
- Retention: keep raw data forever, or raw for N days plus rollups?
- Overnight clamp-meter reading of battery current (settles η vs I_p).
- Any plan or date for a capacity test (settles Q).
