-- Instrument registry and monitor heartbeat.
--
-- WHY: discovery was stateless - it recomputed the instrument list on every
-- scrape from live sources, so ANY source failure silently shrank the list. If
-- Prometheus was unreachable, declared_instruments() returned an empty set, which
-- is indistinguishable from "the robot trades nothing". The instrument would then
-- stop being collected, its dashboard panels would go flat, and nothing would say
-- why. That is monitoring that stops monitoring precisely when something is wrong.
--
-- The registry makes membership STICKY. Once an instrument has been seen it keeps
-- being collected until deliberately retired, so a transient outage anywhere in
-- the chain cannot drop it.

CREATE TABLE IF NOT EXISTS carry_instrument (
  setup        TEXT NOT NULL,
  instrument   TEXT NOT NULL,
  first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- Last scrape on which SOME source still reported this instrument. A stale
  -- last_seen next to active=true means the instrument is being collected out of
  -- registry memory alone, which is worth surfacing.
  last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- Last scrape on which it carried a real (non-dust) position.
  last_active  TIMESTAMPTZ,
  -- Retirement is deliberate, never automatic. An instrument that has genuinely
  -- gone away costs one row of collection per scrape; an instrument dropped by
  -- accident costs visibility on a live position.
  active       BOOLEAN NOT NULL DEFAULT TRUE,
  PRIMARY KEY (setup, instrument)
);

CREATE INDEX IF NOT EXISTS carry_instrument_setup_active_idx
  ON carry_instrument (setup, active);


-- Monitor heartbeat. Without this, a monitor that has stopped running looks
-- exactly like a book with nothing happening: no new rows either way. Alert on
-- max(time) falling behind, and on sources_failed being non-empty.
CREATE TABLE IF NOT EXISTS carry_heartbeat (
  time            TIMESTAMPTZ NOT NULL,
  setup           TEXT NOT NULL,
  instruments     INT NOT NULL,      -- how many were collected this scrape
  sources_failed  TEXT NOT NULL,     -- comma separated; empty when all healthy
  data            JSONB NOT NULL     -- declared/held/registry breakdown
);
SELECT create_hypertable('carry_heartbeat', by_range('time'), if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS carry_heartbeat_setup_time_idx
  ON carry_heartbeat (setup, time DESC);
