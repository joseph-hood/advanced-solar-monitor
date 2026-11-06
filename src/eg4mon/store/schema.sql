-- eg4mon SQLite schema. Timestamps are unix seconds (UTC, REAL).
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS readings (
    ts           REAL PRIMARY KEY,
    dt           REAL,
    v            REAL,     -- pack voltage, measured
    i            REAL,     -- A, + = charge (P/V or measured)
    p            REAL,     -- W, + = charge
    soc_model    REAL,     -- 0..1
    soc_inverter REAL,     -- %, voltage-mode estimate (comparison only)
    soc_bms      REAL,     -- %, Daly if available
    v_hat        REAL,
    v_resid      REAL,
    v1           REAL,
    h            REAL,
    pcov         REAL,
    flags        INTEGER,  -- see eg4mon.pipeline.flags
    params_hash  TEXT,
    status       TEXT,
    pv1_w REAL, pv2_w REAL, pv3_w REAL, pv_w REAL,
    grid_w REAL, load_w REAL,
    bms_i REAL, cell_v_min REAL, cell_v_max REAL, temp_min REAL, temp_max REAL,
    extra        TEXT      -- JSON of any other mapped signals
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (
    id    INTEGER PRIMARY KEY,
    ts    REAL NOT NULL,
    kind  TEXT NOT NULL,
    message TEXT,
    data  TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);

-- Daily rollups in SITE_TZ, maintained incrementally by the collector.
CREATE TABLE IF NOT EXISTS daily (
    day      TEXT PRIMARY KEY,   -- YYYY-MM-DD local
    ah_in    REAL DEFAULT 0, ah_out  REAL DEFAULT 0,
    kwh_in   REAL DEFAULT 0, kwh_out REAL DEFAULT 0,
    pv_kwh   REAL DEFAULT 0, load_kwh REAL DEFAULT 0,
    grid_import_kwh REAL DEFAULT 0, grid_export_kwh REAL DEFAULT 0,
    soc_min  REAL, soc_max REAL,
    resets   INTEGER DEFAULT 0,
    gap_s    REAL DEFAULT 0,
    samples  INTEGER DEFAULT 0
);

-- Full-charge-to-full-charge intervals: the Ah closure diagnostic.
CREATE TABLE IF NOT EXISTS cycles (
    id          INTEGER PRIMARY KEY,
    start_ts    REAL NOT NULL,
    end_ts      REAL NOT NULL,
    hours       REAL,
    ah_in       REAL,
    ah_out      REAL,
    closure_ah  REAL,     -- eta*Ah_in - Ah_out - I_p*T, should be ~0
    soc_min     REAL,
    has_gap     INTEGER,
    params_hash TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated REAL NOT NULL
);
