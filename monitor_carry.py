"""Multi-instrument carry monitor: Hyperliquid perp short vs Binance spot long.

Deliberately NOT shaped like aegis_monitor/monitor_exchange_okx_jlp.py. That file
supports three instruments by hardcoding them (FUTURES_BTC / FUTURES_ETH /
FUTURES_SOL) and flattening results into prefixed keys - btc_eq_usd, eth_eq_usd,
sol_eq_usd - inside one JSON blob per scrape. Adding a fourth instrument there
costs new constants, six duplicated extraction blocks, nine new JSON keys, and an
edit to every Grafana panel naming one of those keys.

Here the instrument is a row dimension (see sql/001_carry.sql) and the instrument
list is DISCOVERED at runtime - from what the robot declares in its metrics and
from what is actually held on either venue. Trading a new pair needs no change
here, and the Grafana variables are queries over what has been written, so the
dashboards follow automatically.

This process is READ-ONLY. It never places an order. The Binance key it uses
should have withdrawals and trading disabled - reading is all it needs.

Env (see .env.example):
  CARRY_SETUP          book name, default 'carry'
  CARRY_INSTRUMENTS    OPTIONAL seed list. Instruments are discovered; this only
                       forces extra ones to be collected even when flat.
  CARRY_PROMETHEUS_URL Where to read the robot's declared instruments from
  CARRY_ROBOT_METRICS_URL  Fallback: scrape the robot directly
  CARRY_DISCOVERY_MIN_USD  Dust floor for held-position discovery, default 1
  CARRY_TARGETS        JSON map instrument -> signed USD perp target, e.g.
                       {"DOGE": -20}. Negative = short perp / long spot.
  HL_ADDRESS           Hyperliquid MAIN account address (public, read-only)
  BINANCE_API_KEY      Binance Portfolio Margin key, read-only
  BINANCE_API_SECRET
  TS_DB_*              TimescaleDB connection
"""

import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.parse import urlencode

import dotenv
import psycopg2
import requests

if dotenv.find_dotenv():
    dotenv.load_dotenv()

SETUP = os.getenv("CARRY_SETUP", "carry")


def json_env(name: str, default: str):
    """Parse a JSON-valued environment variable, reporting what was wrong.

    A bare json.loads traceback names the variable but not the value, which is
    the wrong half. These reach the process through docker and then a cron
    environment snapshot, so the realistic failure is the value being mangled in
    transit rather than mistyped in .env - and only printing what actually
    arrived tells those two apart.
    """
    raw = os.getenv(name, default)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise SystemExit(
            f"{name} is not valid JSON: {error}\n"
            f"  value seen by this process: {raw!r}\n"
            f"  expected something like: {default!r}\n"
            f"  If .env looks right, it was mangled on the way in - check the "
            f"env.sh snapshot written by docker-entrypoint.sh."
        ) from error


# A SEED, not the source of truth. Instruments are discovered at runtime (see
# discover_instruments) so that trading a new pair does not require editing this.
# Anything listed here is always collected even when flat, which is useful for a
# pair you are about to trade and want history for.
SEED_INSTRUMENTS: list[str] = json_env("CARRY_INSTRUMENTS", "[]")

TARGETS: dict[str, float] = json_env("CARRY_TARGETS", "{}")

# Where to learn what the robot INTENDS to trade. Either works; Prometheus is
# preferred because it survives a robot restart, whereas scraping the robot
# directly goes blind exactly when the robot is down.
PROM_URL = os.getenv("CARRY_PROMETHEUS_URL", "")          # e.g. http://carry_prometheus:9090
ROBOT_METRICS_URL = os.getenv("CARRY_ROBOT_METRICS_URL", "")  # e.g. http://robot:9702/metrics

# Below this notional an asset is dust, not a position. Without it the residue
# left by a closed leg - a fraction of a coin the venue minimum cannot trade
# away - would register as a live instrument forever.
DISCOVERY_MIN_USD = float(os.getenv("CARRY_DISCOVERY_MIN_USD", "1"))

# Collateral, not positions. These sit in the same balance response as real
# holdings and would otherwise be discovered as enormous instruments.
QUOTE_ASSETS = {
    "USDT", "USDC", "BUSD", "FDUSD", "DAI", "TUSD", "USDE", "USDH", "USDT0",
}

HL_ADDRESS = os.getenv("HL_ADDRESS", "")
HL_INFO = "https://api.hyperliquid.xyz/info"

BINANCE_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_PAPI = "https://papi.binance.com"
BINANCE_SPOT = "https://api.binance.com"

CARRY_DISABLED = bool(os.getenv("CARRY_DISABLED", ""))

CONNECTION = (
    f"postgres://{os.getenv('TS_DB_USER')}:{os.getenv('TS_DB_PASSWORD')}"
    f"@{os.getenv('TS_DB_HOST')}:{os.getenv('TS_DB_PORT', '5432')}"
    f"/{os.getenv('TS_DB_NAME')}"
)

# Hyperliquid settles funding HOURLY, Binance every 8 hours. Annualising with the
# wrong multiplier misjudges a carry by 8x, so the two are kept apart explicitly.
HL_FUNDING_PERIODS_PER_YEAR = 24 * 365
BINANCE_FUNDING_PERIODS_PER_YEAR = 3 * 365


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


# Which upstream sources failed this scrape.
#
# This exists because every fetch used to catch its exception and return an empty
# result, making "Prometheus is unreachable" indistinguishable from "the robot
# trades nothing". Discovery would then collect nothing, the instrument's panels
# would go flat, and nothing would say why - monitoring that stops monitoring
# exactly when something is wrong. A source that failed has told us NOTHING, and
# must never be allowed to shrink the instrument list.
SOURCES_FAILED: set[str] = set()


def source_failed(name: str, error: object) -> None:
    SOURCES_FAILED.add(name)
    log(f"SOURCE FAILED [{name}]: {error}")


def minute_time() -> str:
    return time.strftime("%Y-%m-%d %H:%M:00", time.gmtime())


# ---------------------------------------------------------------- Hyperliquid


def hl_info(body: dict[str, Any]) -> Any:
    """Hyperliquid's info endpoint is public - no key, no signing."""
    try:
        r = requests.post(HL_INFO, json=body, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as error:
        source_failed(f"hl:{body.get('type')}", error)
        return None


def hl_state() -> tuple[dict[str, float], dict[str, Any]]:
    """Returns (positions by coin in contracts, account summary)."""
    state = hl_info({"type": "clearinghouseState", "user": HL_ADDRESS})
    if not state:
        return {}, {}

    positions: dict[str, float] = {}
    for ap in state.get("assetPositions") or []:
        pos = ap.get("position")
        if pos:
            # szi is signed - negative is a short.
            positions[pos["coin"]] = float(pos["szi"])

    summary = state.get("marginSummary") or {}

    # On a UNIFIED HL account the spot USDC wallet backs perps, so perp
    # accountValue alone badly understates available collateral - it reads 0.0
    # on an account that is perfectly able to trade.
    spot = hl_info({"type": "spotClearinghouseState", "user": HL_ADDRESS}) or {}
    usdc = next(
        (
            float(b["total"])
            for b in spot.get("balances", [])
            if b.get("coin") == "USDC"
        ),
        0.0,
    )

    account = {
        "equity": float(summary.get("accountValue") or 0),
        "margin_used": float(summary.get("totalMarginUsed") or 0),
        "notional": float(summary.get("totalNtlPos") or 0),
        "spot_usdc": usdc,
        "withdrawable": float(state.get("withdrawable") or 0),
    }
    account["free"] = usdc + account["equity"] - account["margin_used"]
    return positions, account


def hl_marks_and_funding() -> dict[str, dict[str, float]]:
    """Mark price and annualised funding per coin."""
    data = hl_info({"type": "metaAndAssetCtxs"})
    if not data or len(data) < 2:
        return {}

    names = [u["name"] for u in data[0].get("universe", [])]
    out: dict[str, dict[str, float]] = {}
    for i, ctx in enumerate(data[1]):
        if i >= len(names):
            break
        try:
            out[names[i]] = {
                "mark": float(ctx["markPx"]),
                "funding_rate_ann": float(ctx["funding"]) * HL_FUNDING_PERIODS_PER_YEAR,
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


# -------------------------------------------------------------------- Binance


def binance_signed(path: str, params: dict[str, Any] | None = None) -> Any:
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    # Generous on purpose. recvWindow is spent on one-way network latency plus
    # local clock error - not on server processing - so a tight value fails on
    # any box that is not co-located with an accurate clock.
    params["recvWindow"] = 10000
    query = urlencode(params)
    sig = hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    try:
        r = requests.get(
            f"{BINANCE_PAPI}{path}?{query}&signature={sig}",
            headers={"X-MBX-APIKEY": BINANCE_KEY},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as error:
        source_failed(f"binance:{path}", error)
        return None


def binance_state() -> tuple[dict[str, float], dict[str, Any]]:
    """Returns (cross-margin asset holdings net of debt, account summary)."""
    balances = binance_signed("/papi/v1/balance") or []
    holdings: dict[str, float] = {}
    for b in balances:
        asset = b.get("asset")
        if not asset:
            continue
        # Binance defines crossMarginAsset as free + locked ONLY - it does NOT
        # subtract borrowing, so an asset that was borrowed and sold still reads
        # as a full long. Net it here or the hedge is misreported.
        net = (
            float(b.get("crossMarginFree") or 0)
            + float(b.get("crossMarginLocked") or 0)
            - float(b.get("crossMarginBorrowed") or 0)
            - float(b.get("crossMarginInterest") or 0)
        )
        if net:
            holdings[asset] = net

    acct = binance_signed("/papi/v1/account") or {}
    account = {
        "equity": float(acct.get("accountEquity") or 0),
        "free": float(acct.get("totalAvailableBalance") or 0),
        "margin_used": float(acct.get("accountMaintMargin") or 0),
        # Healthy above ~1.2; liquidation begins around 1.05.
        "uni_mmr": float(acct.get("uniMMR") or 0),
    }
    return holdings, account


def binance_spot_mid(instrument: str) -> float:
    try:
        r = requests.get(
            f"{BINANCE_SPOT}/api/v3/ticker/bookTicker",
            params={"symbol": f"{instrument}USDT"},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        return (float(d["bidPrice"]) + float(d["askPrice"])) / 2
    except Exception as error:
        source_failed(f"binance_spot:{instrument}", error)
        return 0.0


# ------------------------------------------------------------------ discovery


def declared_instruments() -> set[str]:
    """What the ROBOT says it is trading, from its own metric labels.

    CarryHedge tags carry_delta_usd with the asset for every configured market,
    so adding a market to the strategy makes it appear here - before it has
    filled anything. That early visibility is the point: a market that is
    configured but never fills is a failure worth seeing, and it is invisible if
    discovery only looks at held positions.
    """
    assets: set[str] = set()

    if PROM_URL:
        try:
            r = requests.get(
                f"{PROM_URL}/api/v1/query",
                params={"query": "carry_delta_usd"},
                timeout=10,
            )
            r.raise_for_status()
            for series in r.json().get("data", {}).get("result", []):
                asset = series.get("metric", {}).get("asset")
                if asset:
                    assets.add(asset)
            return assets
        except Exception as error:
            source_failed("prometheus", error)

    if ROBOT_METRICS_URL:
        try:
            r = requests.get(ROBOT_METRICS_URL, timeout=10)
            r.raise_for_status()
            for line in r.text.splitlines():
                if line.startswith("carry_delta_usd{"):
                    inner = line[line.index("{") + 1 : line.index("}")]
                    for part in inner.split(","):
                        k, _, v = part.partition("=")
                        if k.strip() == "asset":
                            assets.add(v.strip().strip('"'))
        except Exception as error:
            source_failed("robot_metrics", error)

    return assets


def held_instruments(
    hl_positions: dict[str, float],
    bn_holdings: dict[str, float],
    hl_ctx: dict[str, dict[str, float]],
) -> set[str]:
    """What is actually HELD on either venue, above the dust threshold.

    This is the more important half. Declared instruments tell you what the robot
    means to do; held instruments tell you what you are actually exposed to -
    including anything the robot does not know about. A position left by a
    reconfigured strategy, or opened by hand, is exactly the position most likely
    to go unwatched, and it would never appear in any config.
    """
    assets: set[str] = set()

    for coin, qty in hl_positions.items():
        if coin in QUOTE_ASSETS or not qty:
            continue
        mark = hl_ctx.get(coin, {}).get("mark", 0.0)
        # No mark means we cannot size it - include it rather than drop it.
        # Over-monitoring is cheap; an unmonitored position is not.
        if not mark or abs(qty * mark) >= DISCOVERY_MIN_USD:
            assets.add(coin)

    for asset, qty in bn_holdings.items():
        if asset in QUOTE_ASSETS or not qty:
            continue
        mark = hl_ctx.get(asset, {}).get("mark", 0.0)
        if not mark:
            mark = binance_spot_mid(asset)
        if not mark or abs(qty * mark) >= DISCOVERY_MIN_USD:
            assets.add(asset)

    return assets


def registry_active() -> set[str]:
    """Instruments this setup has seen before and not retired.

    Membership is STICKY on purpose. Discovery reads live sources, and live
    sources fail; without a persisted set, one unreachable endpoint silently
    drops an instrument from collection at exactly the moment something is wrong.
    """
    try:
        with psycopg2.connect(CONNECTION) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT instrument FROM carry_instrument WHERE setup = %s AND active;",
                (SETUP,),
            )
            return {r[0] for r in cur.fetchall()}
    except (Exception, psycopg2.Error) as error:
        source_failed("registry", error)
        return set()


def registry_upsert(seen: set[str], active_now: set[str]) -> None:
    """Record what was seen, and when each instrument last held a position."""
    if not seen:
        return
    try:
        with psycopg2.connect(CONNECTION) as conn:
            cur = conn.cursor()
            for instrument in sorted(seen):
                is_active = instrument in active_now
                cur.execute(
                    "INSERT INTO carry_instrument (setup, instrument, last_active) "
                    "VALUES (%s, %s, CASE WHEN %s THEN NOW() ELSE NULL END) "
                    "ON CONFLICT (setup, instrument) DO UPDATE SET "
                    "  last_seen = NOW(), "
                    "  active = TRUE, "
                    "  last_active = CASE WHEN %s THEN NOW() "
                    "                     ELSE carry_instrument.last_active END;",
                    (SETUP, instrument, is_active, is_active),
                )
            conn.commit()
    except (Exception, psycopg2.Error) as error:
        source_failed("registry_write", error)


def discover_instruments(
    hl_positions: dict[str, float],
    bn_holdings: dict[str, float],
    hl_ctx: dict[str, dict[str, float]],
) -> list[str]:
    """Union of seeded, declared, held and previously-registered.

    The registry term is what makes this survive a broken export. If Prometheus
    is down, declared comes back empty - not because the robot stopped trading
    but because we could not ask. Unioning with the registry keeps the instrument
    collected regardless, and SOURCES_FAILED records why the live answer was thin.
    """
    seeded = set(SEED_INSTRUMENTS)
    declared = declared_instruments()
    held = held_instruments(hl_positions, bn_holdings, hl_ctx)
    remembered = registry_active()

    combined_set = seeded | declared | held | remembered
    combined = sorted(combined_set)

    # Held but not declared: the book holds something the robot is not managing.
    # A risk finding, not a logging detail - a position left by a reconfigured
    # strategy or opened by hand is the one most likely to go unwatched.
    unmanaged = held - declared - seeded
    if unmanaged:
        log(
            f"HELD BUT NOT DECLARED BY THE ROBOT: {sorted(unmanaged)} - "
            "position exists that the strategy is not configured to manage."
        )

    # Collected only because the registry remembers it. Fine when a pair is
    # genuinely flat; a warning when it coincides with a failed source, because
    # then its current state is simply unknown.
    only_remembered = remembered - declared - held - seeded
    if only_remembered and SOURCES_FAILED:
        log(
            f"COLLECTING FROM REGISTRY MEMORY ONLY: {sorted(only_remembered)} while "
            f"sources failed {sorted(SOURCES_FAILED)} - state of these is unverified."
        )

    registry_upsert(combined_set, held)

    log(
        f"Instruments: {combined} (seeded={sorted(seeded)}, "
        f"declared={sorted(declared)}, held={sorted(held)}, "
        f"registry={sorted(remembered)})"
    )
    return combined


# -------------------------------------------------------------------- writing


def write_rows(table: str, columns: str, rows: list[tuple]) -> None:
    if not rows:
        return
    placeholders = ",".join(["%s"] * len(rows[0]))
    try:
        with psycopg2.connect(CONNECTION) as conn:
            cur = conn.cursor()
            cur.executemany(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders});", rows
            )
            conn.commit()
    except (Exception, psycopg2.Error) as error:
        log(f"Failed to write {table}: {error}")


def main() -> int:
    if CARRY_DISABLED:
        log("Carry monitoring disabled. Terminating.")
        return 0
    if not HL_ADDRESS or not BINANCE_KEY:
        log("HL_ADDRESS or BINANCE_API_KEY not set. Terminating.")
        return 1

    t = minute_time()
    log(f"Carry monitor '{SETUP}'. Pulling data.")

    hl_positions, hl_account = hl_state()
    hl_ctx = hl_marks_and_funding()
    bn_holdings, bn_account = binance_state()

    # Discovered every scrape, not read from config. Trading a new pair makes it
    # appear here on the next run, and the Grafana variables are already queries
    # over what has been written - so the dashboards pick it up with no edit
    # anywhere in the chain.
    instruments = discover_instruments(hl_positions, bn_holdings, hl_ctx)

    leg_rows: list[tuple] = []
    delta_rows: list[tuple] = []

    for instrument in instruments:
        ctx = hl_ctx.get(instrument, {})
        hl_mark = ctx.get("mark", 0.0)
        hl_funding = ctx.get("funding_rate_ann", 0.0)

        # PERP leg - Hyperliquid, short.
        perp_qty = hl_positions.get(instrument, 0.0)
        perp_usd = perp_qty * hl_mark
        leg_rows.append(
            (
                t, SETUP, instrument, "hl", "perp",
                json.dumps({
                    "qty": perp_qty,
                    "usd": perp_usd,
                    "mark": hl_mark,
                    "funding_rate_ann": hl_funding,
                }),
            )
        )

        # SPOT leg - Binance cross margin, long. Note the venue reports the
        # ASSET (DOGE), not the traded pair (DOGEUSDT).
        spot_qty = bn_holdings.get(instrument, 0.0)
        spot_px = binance_spot_mid(instrument)
        spot_usd = spot_qty * spot_px
        leg_rows.append(
            (
                t, SETUP, instrument, "binance", "spot",
                json.dumps({
                    "qty": spot_qty,
                    "usd": spot_usd,
                    "mark": spot_px,
                    "funding_rate_ann": 0.0,  # spot pays no funding
                }),
            )
        )

        delta_usd = perp_usd + spot_usd
        basis_bps = ((spot_px - hl_mark) / hl_mark * 10000) if hl_mark else 0.0
        delta_rows.append(
            (
                t, SETUP, instrument,
                json.dumps({
                    "delta_usd": delta_usd,
                    "target_usd": TARGETS.get(instrument, 0.0),
                    "perp_usd": perp_usd,
                    "spot_usd": spot_usd,
                    # Carry income accrues on the SHORT perp leg only - the spot
                    # leg pays nothing, which is the point of this structure.
                    "carry_ann_usd": abs(perp_usd) * hl_funding,
                    # HL mark vs Binance spot. A widening basis means the two
                    # legs are being marked against drifting references, so a
                    # "flat" delta is less flat than it looks.
                    "basis_bps": basis_bps,
                }),
            )
        )

    write_rows("carry_leg_time", "time, setup, instrument, exchange, leg, data", leg_rows)
    write_rows("carry_delta_time", "time, setup, instrument, data", delta_rows)
    write_rows(
        "carry_venue_time",
        "time, setup, exchange, data",
        [
            (t, SETUP, "hl", json.dumps(hl_account)),
            (t, SETUP, "binance", json.dumps(bn_account)),
        ],
    )

    # Heartbeat, written unconditionally - including when everything upstream
    # failed and there was nothing else to write. Without it a monitor that has
    # stopped running is indistinguishable from a book with nothing happening:
    # no new rows either way. Alert on max(time) falling behind, and on
    # sources_failed being non-empty.
    write_rows(
        "carry_heartbeat",
        "time, setup, instruments, sources_failed, data",
        [
            (
                t,
                SETUP,
                len(instruments),
                ",".join(sorted(SOURCES_FAILED)),
                json.dumps(
                    {
                        "instruments": instruments,
                        "leg_rows": len(leg_rows),
                        "delta_rows": len(delta_rows),
                        "sources_failed": sorted(SOURCES_FAILED),
                    }
                ),
            )
        ],
    )

    if SOURCES_FAILED:
        log(
            f"Finished DEGRADED. {len(leg_rows)} leg rows, {len(delta_rows)} delta "
            f"rows. Failed sources: {sorted(SOURCES_FAILED)}"
        )
        return 2

    log(f"Finished. {len(leg_rows)} leg rows, {len(delta_rows)} delta rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
