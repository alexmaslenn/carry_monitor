# carry_monitor

Risk monitoring for delta-neutral funding carries across multiple instruments and
venues. Currently: Hyperliquid perp short against Binance Portfolio Margin spot
long.

Runs alongside `aegis_monitor` and writes to the **same** TimescaleDB, so one
Grafana datasource sees both. It creates its own tables and changes nothing that
already exists.

## Why this is a separate tool

`aegis_monitor` stores instruments *inside* a JSON blob keyed `(time, exchange)`.
That works for one instrument and is why `monitor_exchange_okx_jlp.py` hardcodes
exactly three — `FUTURES_BTC`, `FUTURES_ETH`, `FUTURES_SOL` — flattened into
prefixed keys like `btc_eq_usd`, `eth_funding_rate`, `sol_premium`.

Adding a fourth instrument there costs:

- a new `FUTURES_*` constant
- six duplicated extraction blocks
- nine new JSON keys
- **an edit to every Grafana panel**, because each panel names its key directly

Here the instrument is a **column**. Adding one is a single env var; Grafana
repeats its panels off a template variable. Nothing else changes.

## Schema

| table | grain | holds |
|---|---|---|
| `carry_leg_time` | leg × instrument × scrape | qty, usd, mark, funding_rate_ann |
| `carry_delta_time` | instrument × scrape | delta_usd, target_usd, carry_ann_usd, basis_bps |
| `carry_venue_time` | venue × scrape | equity, free, margin_used, uni_mmr |

`carry_venue_time` is deliberately separate from the legs. A cross-venue carry
does **not** net margin: Hyperliquid collateral cannot support the Binance leg or
vice versa, so a sharp move can margin-call one side while the offsetting profit
sits stranded on the other. Each venue's headroom has to be watched on its own.

## Install

```bash
psql -h <ts-host> -U <user> -d <db> -f sql/001_carry.sql

cp .env.example .env      # then fill it in
pip install -r requirements.txt
python3 monitor_carry.py  # one scrape, writes one row set
```

Then schedule it the way `aegis_monitor` does — a cron line per minute:

```
* * * * * python3 /app/monitor_carry.py
```

## Adding an instrument

```bash
CARRY_INSTRUMENTS=["DOGE","ETH"]
CARRY_TARGETS={"DOGE": -20, "ETH": -50}
```

That is the whole change. No code, no new panels.

## Access

This process is **read-only** and never places an order. The Binance key it uses
should have withdrawals *and* trading disabled — reading is all it needs.
Hyperliquid needs no key at all: the `info` endpoint is public, so only the
account address is configured.

## Notes that cost real money to learn

**Binance `crossMarginAsset` is not a position.** Binance defines it as
`crossMarginFree + crossMarginLocked` and does **not** subtract borrowing, so an
asset that was borrowed and sold still reads as a full long. This tool nets off
`crossMarginBorrowed` and `crossMarginInterest`.

**Hyperliquid perp `accountValue` reads `0.0` on a unified account.** Spot USDC
backs perps there, so collateral must be read from `spotClearinghouseState`. A
monitor that reads only the perp summary reports an account with $200 of backing
as having none.

**Funding periods differ.** Hyperliquid settles hourly, Binance every 8 hours.
Annualising with the wrong multiplier misjudges a carry by 8x, so the two
constants are kept explicitly separate.
