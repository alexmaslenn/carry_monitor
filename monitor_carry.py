"""Multi-instrument carry monitor: Hyperliquid perp short vs Binance spot long.

Deliberately NOT shaped like aegis_monitor/monitor_exchange_okx_jlp.py. That file
supports three instruments by hardcoding them (FUTURES_BTC / FUTURES_ETH /
FUTURES_SOL) and flattening results into prefixed keys - btc_eq_usd, eth_eq_usd,
sol_eq_usd - inside one JSON blob per scrape. Adding a fourth instrument there
costs new constants, six duplicated extraction blocks, nine new JSON keys, and an
edit to every Grafana panel naming one of those keys.

Here the instrument is a row dimension (see sql/001_carry.sql) and the instrument
list comes from config, so adding one is a single env var and Grafana repeats its
panels off a template variable.

This process is READ-ONLY. It never places an order. The Binance key it uses
should have withdrawals and trading disabled - reading is all it needs.

Env (see .env.example):
  CARRY_SETUP          book name, default 'hlbn'
  CARRY_INSTRUMENTS    JSON list, e.g. ["DOGE","ETH"]
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

SETUP = os.getenv("CARRY_SETUP", "hlbn")
INSTRUMENTS: list[str] = json.loads(os.getenv("CARRY_INSTRUMENTS", '["DOGE"]'))
TARGETS: dict[str, float] = json.loads(os.getenv("CARRY_TARGETS", "{}"))

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
        log(f"HL info request failed ({body.get('type')}): {error}")
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
        log(f"Binance request failed ({path}): {error}")
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
        log(f"Binance spot price failed ({instrument}): {error}")
        return 0.0


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
    log(f"Carry monitor '{SETUP}' for {INSTRUMENTS}. Pulling data.")

    hl_positions, hl_account = hl_state()
    hl_ctx = hl_marks_and_funding()
    bn_holdings, bn_account = binance_state()

    leg_rows: list[tuple] = []
    delta_rows: list[tuple] = []

    for instrument in INSTRUMENTS:
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

    log(f"Finished. {len(leg_rows)} leg rows, {len(delta_rows)} delta rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
