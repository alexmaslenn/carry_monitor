-- Multi-instrument carry monitoring schema.
--
-- Additive: creates its own tables and touches nothing in aegis_monitor. Runs
-- against the SAME TimescaleDB instance so Grafana can join across both if
-- wanted, but the existing tables and dashboards are unaffected.
--
-- WHY NEW TABLES RATHER THAN REUSING balance_time / market_time:
-- Those are keyed (time, exchange) with a single JSON blob, so instruments have
-- to live inside the blob as prefixed keys - btc_eq_usd, eth_eq_usd, sol_eq_usd.
-- That is why monitor_exchange_okx_jlp.py hardcodes exactly three instruments,
-- and why every Grafana panel names one of those keys directly. Adding a fourth
-- instrument there costs new constants, duplicated extraction blocks, new JSON
-- keys, AND an edit to every panel that displays it.
--
-- Here the instrument is a COLUMN. One row per leg per instrument per scrape, so
-- adding an instrument is a config change, and Grafana repeats panels off a
-- template variable instead of needing new ones written by hand.

-- One row per leg, per instrument, per scrape.
CREATE TABLE IF NOT EXISTS carry_leg_time (
  time        TIMESTAMPTZ NOT NULL,
  setup       TEXT NOT NULL,   -- which book, e.g. 'hlbn' (HL perp / Binance spot)
  instrument  TEXT NOT NULL,   -- 'DOGE' - the asset, venue-independent
  exchange    TEXT NOT NULL,   -- 'hl' | 'binance'
  leg         TEXT NOT NULL,   -- 'perp' | 'spot'
  data        JSONB NOT NULL   -- qty, usd, mark, funding_rate_ann
);
SELECT create_hypertable('carry_leg_time', by_range('time'), if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS carry_leg_time_setup_instr_time_idx
  ON carry_leg_time (setup, instrument, time DESC);
CREATE INDEX IF NOT EXISTS carry_leg_time_setup_time_idx
  ON carry_leg_time (setup, time DESC);


-- Net exposure per instrument across both legs. This is the number that decides
-- whether the book is actually hedged, and it is what should drive alerting -
-- not either leg on its own.
CREATE TABLE IF NOT EXISTS carry_delta_time (
  time        TIMESTAMPTZ NOT NULL,
  setup       TEXT NOT NULL,
  instrument  TEXT NOT NULL,
  data        JSONB NOT NULL   -- delta_usd, target_usd, perp_usd, spot_usd,
                               -- carry_ann_usd, basis_bps
);
SELECT create_hypertable('carry_delta_time', by_range('time'), if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS carry_delta_time_setup_instr_time_idx
  ON carry_delta_time (setup, instrument, time DESC);


-- Venue-level collateral, kept separate from the legs because a cross-venue
-- carry does NOT net margin: Hyperliquid collateral cannot support the Binance
-- leg or vice versa. Each venue's headroom has to be watched on its own, since
-- a sharp move can margin-call one side while the offsetting profit is stranded
-- on the other.
CREATE TABLE IF NOT EXISTS carry_venue_time (
  time      TIMESTAMPTZ NOT NULL,
  setup     TEXT NOT NULL,
  exchange  TEXT NOT NULL,
  data      JSONB NOT NULL     -- equity, free, margin_used, uni_mmr / spot_usdc
);
SELECT create_hypertable('carry_venue_time', by_range('time'), if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS carry_venue_time_setup_exch_time_idx
  ON carry_venue_time (setup, exchange, time DESC);
