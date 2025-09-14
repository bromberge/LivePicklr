# main.py
# main.py (very top)
try:
    from dotenv import load_dotenv, find_dotenv
    _DOTENV_PATH = find_dotenv(usecwd=True)
    if _DOTENV_PATH:
        load_dotenv(_DOTENV_PATH, override=False)
        print(f"[main] .env loaded early: {_DOTENV_PATH}")
except Exception as e:
    print(f"[main] dotenv load skipped: {e}")

import asyncio
import contextlib
import math
import os
import random
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import ccxt
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine, select

# ---- Local modules ----
import settings as S
from models import Candle, MetricsDaily, Order, Position, Trade, Wallet
from signal_engine import compute_signals
from sim import (
    can_open_new_position,
    ensure_wallet,
    get_last_price,
    mark_to_market_and_manage,
)
from universe import (
    UniversePair,
    ensure_pairs_for,
    get_active_universe,
    refresh_universe,
    universe_debug_snapshot,
    universe_stale,
)
from data import backfill_hourly, update_candles_for
from brokers import make_broker
from risk import size_position as risk_size_position

# ----------------------------------------
# App + env
# ----------------------------------------
load_dotenv(override=False)
app = FastAPI(title="autoPicklr Trading Simulator")

# --- Verbose TP logs toggle (match sim.py) ---
def _env_true(name: str, default: str = "0") -> bool:
    val = os.environ.get(name, default)
    if val is None:
        return False
    val = str(val).strip().lower()
    return val in ("1", "true", "yes", "on")

VERBOSE_TP_LOGS = _env_true("VERBOSE_TP_LOGS", "0")

def _vlog(msg: str) -> None:
    if VERBOSE_TP_LOGS:
        try:
            print(msg)
        except Exception:
            pass

            
# Discover external (Kraken-held) assets and import as DB positions if enabled
IMPORT_EXTERNAL_POSITIONS = _env_true("IMPORT_EXTERNAL_POSITIONS", "0")

# ----------------------------------------
# Helpers for settings with safe defaults
# ----------------------------------------
def cfg(name: str, default):
    return getattr(S, name, default)

# Frequently used knobs with defaults (used if missing in settings.py)
UNIVERSE_REFRESH_MINUTES = cfg("UNIVERSE_REFRESH_MINUTES", 10)
OPEN_POS_UPDATE_SECONDS = cfg("OPEN_POS_UPDATE_SECONDS", 60)
FULL_CANDLES_UPDATE_SECONDS = cfg("FULL_CANDLES_UPDATE_SECONDS", 300)
POLL_SECONDS = cfg("POLL_SECONDS", 15)

MAX_OPEN_POSITIONS = cfg("MAX_OPEN_POSITIONS", 8)
MAX_NEW_POSITIONS_PER_CYCLE = cfg("MAX_NEW_POSITIONS_PER_CYCLE", 2)

FEE_PCT = cfg("FEE_PCT", 0.001)          # 0.10%
SLIPPAGE_PCT = cfg("SLIPPAGE_PCT", 0.001)  # 0.10%

REQUIRE_BREAKOUT = cfg("REQUIRE_BREAKOUT", False)
MIN_BREAKOUT_PCT = cfg("MIN_BREAKOUT_PCT", 0.02)
DET_EMA_SHORT = cfg("DET_EMA_SHORT", 12)
DET_EMA_LONG = cfg("DET_EMA_LONG", 26)
BREAKOUT_LOOKBACK = cfg("BREAKOUT_LOOKBACK", 50)
EMA_SLOPE_LOOKBACK = cfg("EMA_SLOPE_LOOKBACK", 8)
EMA_SLOPE_MIN = cfg("EMA_SLOPE_MIN", 0.0)
MIN_EMA_SPREAD = cfg("MIN_EMA_SPREAD", 0.0)
MAX_EXTENSION_PCT = cfg("MAX_EXTENSION_PCT", 0.20)
MIN_RR = cfg("MIN_RR", 1.2)

USE_MODEL = cfg("USE_MODEL", False)
SCORE_THRESHOLD = cfg("SCORE_THRESHOLD", 0.0)
ENABLE_DEBUG_SIGNALS = cfg("ENABLE_DEBUG_SIGNALS", False)

USE_ATR_STOPS = cfg("USE_ATR_STOPS", True)
ATR_LEN = cfg("ATR_LEN", 14)
ATR_STOP_MULT = cfg("ATR_STOP_MULT", 1.5)
ATR_TARGET_MULT = cfg("ATR_TARGET_MULT", 2.5)

SIGNAL_MIN_NOTIONAL_USD = cfg("SIGNAL_MIN_NOTIONAL_USD", 20.0)
COOLDOWN_MINUTES = cfg("COOLDOWN_MINUTES", 30)
MAX_HOLD_MINUTES = cfg("MAX_HOLD_MINUTES", 0)  # 0/None means "no explicit cap"

DEFAULT_UNIVERSE = cfg(
    "DEFAULT_UNIVERSE",
    ["BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD", "LINK/USD"],
)

# ----------------------------------------
# DB
# ----------------------------------------
engine = create_engine(
    "sqlite:///picklr.db",
    echo=False,
    connect_args={"check_same_thread": False},
)

# --- Orders (simple JSON feed) ---
from typing import Optional
from fastapi import Query

@app.get("/api/orders")
def api_orders(
    symbol: Optional[str] = None,
    from_utc: Optional[str] = Query(None, description="ISO8601 e.g. 2025-09-01T00:00:00"),
    to_utc: Optional[str]   = Query(None, description="ISO8601 e.g. 2025-09-07T23:59:59"),
    limit: int = Query(500, ge=1, le=5000),
):
    def _parse(dt: Optional[str]):
        from datetime import datetime
        if not dt: return None
        try:
            return datetime.fromisoformat(dt.replace("Z",""))
        except Exception:
            return None

    dt_from = _parse(from_utc)
    dt_to   = _parse(to_utc)

    with Session(engine) as s:
        q = select(Order).order_by(Order.id.desc())
        if symbol:
            q = q.where(Order.symbol == symbol.upper())
        if dt_from:
            q = q.where(Order.ts >= dt_from)
        if dt_to:
            q = q.where(Order.ts <= dt_to)

        rows = s.exec(q).all()[:limit]
        return [{
            "id": r.id,
            "ts": r.ts.isoformat() if r.ts else None,
            "symbol": r.symbol,
            "side": r.side,
            "qty": float(r.qty or 0.0),
            "price_req": float(r.price_req or 0.0),
            "price_fill": (None if r.price_fill is None else float(r.price_fill)),
            "status": r.status,
            "reason": r.reason,
        } for r in rows]

def get_session():
    with Session(engine) as s:
        yield s


# ----------------------------------------
# Globals
# ----------------------------------------
BROKER_HANDLE = None
LAST_UNIVERSE_REFRESH = None
LAST_OPEN_POS_UPDATE = None
LAST_FULL_CANDLES_UPDATE = None

ACCOUNT_BASELINE = float(os.environ.get("LIVE_BASELINE_EQUITY", "0") or 0.0)

# ----------------------------------------
# Kraken (ccxt) helpers for /api/account_v2
# ----------------------------------------
EXCHANGE = None

def _get_exchange():
    global EXCHANGE
    if EXCHANGE is not None:
        return EXCHANGE
    ex = ccxt.kraken(
        {
            "apiKey": os.environ.get("KRAKEN_API_KEY", ""),
            "secret": os.environ.get("KRAKEN_API_SECRET", ""),
            "enableRateLimit": True,
        }
    )
    ex.load_markets()
    EXCHANGE = ex
    return EXCHANGE

def _fnum(x):
    try:
        return float(x)
    except Exception:
        return 0.0

def _norm_asset(a: str) -> str:
    return {"XBT": "BTC", "ZUSD": "USD"}.get(a, a)

def _usd_sym(asset: str) -> str:
    return f"{_norm_asset(asset)}/USD"

def _mid_price(t) -> float:
    bid = _fnum(t.get("bid"))
    ask = _fnum(t.get("ask"))
    last = _fnum(t.get("last"))
    close = _fnum(t.get("close"))
    if bid and ask:
        return (bid + ask) / 2.0
    return last or close or 0.0

# ---------- Trade Log CSV export ----------
from fastapi.responses import PlainTextResponse
import io, csv
from dateutil import parser as dateparser

# helpers
def _ema(seq, span):
    k = 2.0 / (span + 1.0)
    s = None
    out = []
    for x in seq:
        s = x if s is None else (x - s) * k + s
        out.append(s)
    return out

def _parse_dt(s):
    if not s:
        return None
    try:
        return dateparser.parse(s)
    except Exception:
        return None

def _load_nearby_candles(session, symbol, start_dt, end_dt):
    q = (
        select(Candle)
        .where(Candle.symbol == symbol, Candle.ts >= start_dt, Candle.ts <= end_dt)
        .order_by(Candle.ts.asc())
    )
    return session.exec(q).all()

def _kraken_trades_for_symbols(ex, syms, since_ms, until_ms):
    # Pull recent trades per symbol with CCXT and bucket them
    out = {s: [] for s in syms}
    for s in syms:
        try:
            # Kraken accepts since; paginate if needed (we keep it simple and wide)
            rows = ex.fetch_my_trades(s, since=since_ms, limit=500) or []
            # filter to until_ms range
            rows = [t for t in rows if (t.get("timestamp") or 0) <= until_ms]
            out[s] = rows
        except Exception as e:
            print(f"[trade_log] fetch_my_trades failed for {s}: {e}")
            out[s] = []
    return out

def _closest_fills(trades, side, placed_dt, want_qty):
    """
    From a list of Kraken trades (already symbol-filtered), pick fills that:
    - match side ('buy'/'sell')
    - are within [-2m, +6h] from placed time
    - accumulate to ~want_qty (±2%)
    If nothing reasonable, return [].
    """
    if not trades:
        return []
    placed_ms = int(placed_dt.timestamp()*1000) if placed_dt else 0
    lo = placed_ms - 120*1000
    hi = placed_ms + 6*60*60*1000

    cand = [t for t in trades if (t.get("side")==side) and lo <= (t.get("timestamp") or 0) <= hi]
    if not cand:
        return []

    # sort by |time - placed|
    cand.sort(key=lambda t: abs((t.get("timestamp") or 0) - placed_ms))

    # accumulate fills until we reach target qty (±2%)
    acc = []
    amt = 0.0
    target = float(want_qty or 0.0)
    tol = max(0.02 * target, 1e-12)
    for t in cand:
        a = float(t.get("amount") or 0.0)
        if a <= 0:
            continue
        acc.append(t)
        amt += a
        if target > 0 and abs(amt - target) <= tol:
            break
        if target > 0 and amt > target and (amt - target) <= (0.05 * target):  # small overfill tolerance
            break
    return acc if acc else []

def _sum_fee_ccxt(tr):
    # fee can be either 'fee' dict or 'fees' list
    fee = 0.0
    if not tr:
        return 0.0
    for t in tr:
        if isinstance(t.get("fee"), dict):
            fee += float(t["fee"].get("cost") or 0.0)
        if isinstance(t.get("fees"), list):
            for f in t["fees"]:
                fee += float(f.get("cost") or 0.0)
    return fee

def _sum_cost_ccxt(tr):
    cost = 0.0
    for t in tr or []:
        cost += float(t.get("cost") or 0.0) or (float(t.get("amount") or 0.0) * float(t.get("price") or 0.0))
    return cost

def _wavg_price(tr):
    # volume-weighted price across fills
    num, den = 0.0, 0.0
    for t in tr or []:
        px = float(t.get("price") or 0.0)
        qty = float(t.get("amount") or 0.0)
        if px > 0 and qty > 0:
            num += px * qty
            den += qty
    return (num/den) if den > 0 else None

def _first_ts(tr):
    if not tr:
        return None
    ms = min(t.get("timestamp") or 0 for t in tr)
    return datetime.utcfromtimestamp(ms/1000.0) if ms else None

def _last_ts(tr):
    if not tr:
        return None
    ms = max(t.get("timestamp") or 0 for t in tr)
    return datetime.utcfromtimestamp(ms/1000.0) if ms else None

def _compute_signal_metrics(session, symbol, ref_dt, ref_price):
    """
    Compute EMA slope/spread and breakout % at (or just before) ref_dt using stored candles.
    Returns (ema_slope, breakout_pct, ema_spread) or (None,None,None) if not enough data.
    """
    need = max(S.DET_EMA_LONG + S.EMA_SLOPE_LOOKBACK + 2, S.BREAKOUT_LOOKBACK + 2, 60)
    # take a window ending at ref_dt
    start = (ref_dt - timedelta(hours=48)) if ref_dt else (datetime.utcnow() - timedelta(hours=48))
    cs = _load_nearby_candles(session, symbol, start, ref_dt or datetime.utcnow())
    if not cs or len(cs) < need:
        return (None, None, None)

    closes = [float(c.close) for c in cs]
    ema_s = _ema(closes, S.DET_EMA_SHORT)
    ema_l = _ema(closes, S.DET_EMA_LONG)

    price = float(ref_price or closes[-1])
    ema_long = ema_l[-1]
    back = S.EMA_SLOPE_LOOKBACK
    ema_then = ema_l[-(back+1)]
    ema_slope = float(ema_long - ema_then)

    # breakout vs prior high
    prior_slice = closes[-(S.BREAKOUT_LOOKBACK+1):-1]
    if prior_slice:
        prior_high = max(prior_slice)
        breakout_pct = (price - prior_high) / prior_high if prior_high > 0 else None
    else:
        breakout_pct = None

    ema_spread = ((ema_s[-1] - ema_l[-1]) / price) if price > 0 else None
    return (ema_slope, breakout_pct, ema_spread)

def _hi_lo_pl_percent(session, symbol, entry_dt, exit_dt, entry_px):
    if not (entry_dt and exit_dt and entry_px and entry_px > 0):
        return (None, None)
    cs = _load_nearby_candles(session, symbol, entry_dt, exit_dt)
    if not cs:
        return (None, None)
    max_high = max(float(c.high if c.high is not None else c.close) for c in cs)
    min_low  = min(float(c.low  if c.low  is not None else c.close) for c in cs)
    hi_pct = (max_high/entry_px - 1.0) * 100.0
    lo_pct = (min_low/entry_px  - 1.0) * 100.0
    return (hi_pct, lo_pct)

# ---------- Trade Log CSV export (Kraken-driven) ----------
from fastapi.responses import PlainTextResponse
import io, csv, math
from datetime import datetime, timedelta

def _parse_iso_loose(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        # handle 'Z' TZ
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        # try date-only
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None


def _load_candles_range(session, symbol, start_dt, end_dt):
    return session.exec(
        select(Candle)
        .where(Candle.symbol == symbol, Candle.ts >= start_dt, Candle.ts <= end_dt)
        .order_by(Candle.ts.asc())
    ).all()

def _signal_metrics_at(session, symbol, ref_dt, ref_px):
    need = max(S.DET_EMA_LONG + S.EMA_SLOPE_LOOKBACK + 2, S.BREAKOUT_LOOKBACK + 2, 60)
    start = (ref_dt - timedelta(hours=48)) if ref_dt else (datetime.utcnow() - timedelta(hours=48))
    cs = _load_candles_range(session, symbol, start, ref_dt or datetime.utcnow())
    if not cs or len(cs) < need:
        return (None, None, None)
    closes = [float(c.close) for c in cs]
    ema_s = _ema(closes, S.DET_EMA_SHORT)
    ema_l = _ema(closes, S.DET_EMA_LONG)
    price = float(ref_px or closes[-1])
    ema_long = ema_l[-1]
    back = S.EMA_SLOPE_LOOKBACK
    ema_then = ema_l[-(back+1)]
    ema_slope = float(ema_long - ema_then)
    prior_slice = closes[-(S.BREAKOUT_LOOKBACK+1):-1]
    breakout_pct = ((price - max(prior_slice)) / max(prior_slice)) if prior_slice and max(prior_slice) > 0 else None
    ema_spread = ((ema_s[-1] - ema_l[-1]) / price) if price > 0 else None
    return (ema_slope, breakout_pct, ema_spread)

def _hi_lo_pl_pct(session, symbol, entry_dt, exit_dt, entry_px):
    if not (entry_dt and exit_dt and entry_px and entry_px > 0):
        return (None, None)
    cs = _load_candles_range(session, symbol, entry_dt, exit_dt)
    if not cs:
        return (None, None)
    max_high = max(float(c.high if c.high is not None else c.close) for c in cs)
    min_low  = min(float(c.low  if c.low  is not None else c.close) for c in cs)
    hi_pct = (max_high/entry_px - 1.0) * 100.0
    lo_pct = (min_low/entry_px  - 1.0) * 100.0
    return (hi_pct, lo_pct)

def _sum_fee_ccxt(trades):
    total = 0.0
    for t in trades or []:
        # ccxt fee formats
        if isinstance(t.get("fee"), dict):
            total += float(t["fee"].get("cost") or 0.0)
        for f in t.get("fees") or []:
            total += float(f.get("cost") or 0.0)
    return total

def _sum_cost_ccxt(trades):
    total = 0.0
    for t in trades or []:
        c = t.get("cost")
        if c is None:
            amt = float(t.get("amount") or 0.0)
            px  = float(t.get("price") or 0.0)
            c = amt * px
        total += float(c or 0.0)
    return total

def _wavg_px(trades):
    num, den = 0.0, 0.0
    for t in trades or []:
        px = float(t.get("price") or 0.0)
        q  = float(t.get("amount") or 0.0)
        if px > 0 and q > 0:
            num += px*q
            den += q
    return (num/den) if den > 0 else None

def _first_ts(trades):
    if not trades: return None
    ms = min(int(t.get("timestamp") or 0) for t in trades)
    return datetime.utcfromtimestamp(ms/1000.0) if ms else None

def _last_ts(trades):
    if not trades: return None
    ms = max(int(t.get("timestamp") or 0) for t in trades)
    return datetime.utcfromtimestamp(ms/1000.0) if ms else None

def _find_nearest_order(session, symbol, side, around_dt):
    """
    Find the nearest Autopicklr Order (same symbol+side) placed within ±6h of Kraken exec time.
    Prefer orders placed BEFORE execution; fallback to nearest.
    """
    if not around_dt: 
        return None
    lo = around_dt - timedelta(hours=6)
    hi = around_dt + timedelta(hours=6)
    rows = session.exec(
        select(Order)
        .where(Order.symbol == symbol, Order.side == side.upper(), Order.ts >= lo, Order.ts <= hi)
        .order_by(Order.ts.asc())
    ).all()
    if not rows:
        return None
    before = [r for r in rows if r.ts <= around_dt]
    if before:
        # nearest before
        before.sort(key=lambda r: abs((around_dt - r.ts).total_seconds()))
        return before[0]
    # otherwise absolute nearest
    rows.sort(key=lambda r: abs((r.ts - around_dt).total_seconds()))
    return rows[0]

def _atr_stop_from_position(session, symbol, entry_dt):
    """
    Try to pull the stop that Autopicklr used at entry from the Position row
    opened near the entry time.
    """
    if not entry_dt:
        return None
    lo = entry_dt - timedelta(hours=2)
    hi = entry_dt + timedelta(hours=6)
    pos = session.exec(
        select(Position)
        .where(Position.symbol == symbol, Position.opened_ts.is_not(None), Position.opened_ts >= lo, Position.opened_ts <= hi)
        .order_by(Position.opened_ts.asc())
    ).first()
    return float(getattr(pos, "stop", 0.0) or 0.0) if pos else None, (float(getattr(pos, "target", 0.0) or 0.0) if pos else None)

# ========= Improved positions CSV (Kraken-first, full coverage, score/percent/reason fixes) =========
from fastapi.responses import PlainTextResponse
import io, csv, math
from datetime import datetime, timedelta
from collections import defaultdict

def _iso_or_blank(dt): return (dt.isoformat() if isinstance(dt, datetime) else "")

def _base_from_ccxt_symbol(sym: str) -> str:
    # "ENA/USD" -> "ENA"
    if not sym: return ""
    return sym.split("/")[0].upper().strip()

def _norm_variants(base: str) -> list[str]:
    # search variants in your DB
    return [base, f"{base}/USD", f"{base}/USDT"]


def _candles_between(session, symbol, start_dt, end_dt):
    return session.exec(
        select(Candle).where(Candle.symbol == symbol, Candle.ts >= start_dt, Candle.ts <= end_dt).order_by(Candle.ts.asc())
    ).all()

def _entry_metrics(session, sym_variants, entry_dt, entry_px):
    need = max(S.DET_EMA_LONG + S.EMA_SLOPE_LOOKBACK + 2, S.BREAKOUT_LOOKBACK + 2, 60)
    for sym in sym_variants:
        cs = _candles_between(session, sym, entry_dt - timedelta(hours=48), entry_dt)
        if not cs or len(cs) < need: 
            continue
        closes = [float(c.close) for c in cs]
        e_s = _ema(closes, S.DET_EMA_SHORT)
        e_l = _ema(closes, S.DET_EMA_LONG)
        price = float(entry_px or closes[-1])
        ema_slope = float(e_l[-1] - e_l[-(S.EMA_SLOPE_LOOKBACK+1)])
        prior = closes[-(S.BREAKOUT_LOOKBACK+1):-1]
        breakout = ((price - max(prior))/max(prior)) if (prior and max(prior)>0) else None
        spread = ((e_s[-1]-e_l[-1])/price) if price>0 else None
        return ema_slope, breakout, spread, sym
    return None, None, None, None

def _hi_lo_pl_pct(session, sym, entry_dt, exit_dt, entry_px):
    if not (sym and entry_dt and exit_dt and entry_px>0): return None, None
    cs = _candles_between(session, sym, entry_dt, exit_dt)
    if not cs: return None, None
    hi = max(float(c.high if c.high is not None else c.close) for c in cs)
    lo = min(float(c.low  if c.low  is not None else c.close) for c in cs)
    return (hi/entry_px-1.0)*100.0, (lo/entry_px-1.0)*100.0

def _wavg_px(trs):
    num=0.0; den=0.0
    for t in trs:
        px=float(t.get("price") or 0.0); q=float(t.get("amount") or 0.0)
        if px>0 and q>0: num += px*q; den += q
    return (num/den) if den>0 else None

def _sum_cost(trs):
    tot=0.0
    for t in trs:
        c=t.get("cost")
        if c is None:
            amt=float(t.get("amount") or 0.0); px=float(t.get("price") or 0.0)
            c = amt*px
        tot += float(c or 0.0)
    return tot

def _sum_fees(trs):
    tot=0.0
    for t in trs:
        f=t.get("fee"); 
        if isinstance(f, dict): tot += float(f.get("cost") or 0.0)
        for ff in t.get("fees") or []: tot += float(ff.get("cost") or 0.0)
    return tot

def _first_ts(trs):
    if not trs: return None
    ms=min(int(t.get("timestamp") or 0) for t in trs)
    return datetime.utcfromtimestamp(ms/1000) if ms else None

def _last_ts(trs):
    if not trs: return None
    ms=max(int(t.get("timestamp") or 0) for t in trs)
    return datetime.utcfromtimestamp(ms/1000) if ms else None

def _candidate_symbols_for_window(ex, session, dt_start, dt_end):
    # union of: USD/USDT markets + symbols in your local Orders during window
    usd_markets = [m for m in getattr(ex, "markets", {}) if m.endswith("/USD") or m.endswith("/USDT")]
    with Session(engine) as s:
        sym_rows = s.exec(select(Order.symbol).where(Order.ts >= dt_start, Order.ts <= dt_end)).all()
    local_syms = [ (r[0] or "").upper() for r in sym_rows if r and r[0] ]
    # expand bare bases with /USD if market exists
    for b in list(local_syms):
        if "/" not in b:
            if f"{b}/USD" in ex.markets: local_syms.append(f"{b}/USD")
            if f"{b}/USDT" in ex.markets: local_syms.append(f"{b}/USDT")
    out = sorted({s for s in usd_markets + local_syms})
    return out

def _fetch_all_trades_between(ex, symbol, since_ms, until_ms):
    """Paginate Kraken trades for a symbol from since_ms..until_ms."""
    out=[]; cursor=since_ms; safety=0
    while cursor <= until_ms and safety < 200:
        safety += 1
        batch = ex.fetch_my_trades(symbol, since=cursor, limit=1000) or []
        if not batch:
            break
        out.extend([t for t in batch if (t.get("timestamp") or 0) <= until_ms])
        # bump cursor; guard if exchange repeats last ts
        max_ts = max(int(t.get("timestamp") or 0) for t in batch)
        next_cursor = max_ts + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
    return out

def _nearest_order(session, sym_variants, side, around_dt, ref_px=None):
    if not around_dt: return None
    lo = around_dt - timedelta(hours=24)
    hi = around_dt + timedelta(hours=24)
    rows = session.exec(select(Order).where(Order.side==side.upper(), Order.ts>=lo, Order.ts<=hi).order_by(Order.ts.asc())).all()
    if not rows: return None
    # symbol match (loose)
    sv = {v.upper() for v in sym_variants}
    cand = [r for r in rows if (r.symbol and r.symbol.upper() in sv)]
    if not cand: cand = rows  # as a fallback, consider any within time window

    def _score(r):
        # prefer orders before the execution, then closest time, then closest price if provided
        td = abs((r.ts - around_dt).total_seconds())
        before_bias = 0 if r.ts <= around_dt else 1e6
        px = float(r.price_req or 0.0)
        px_pen = abs((px - (ref_px or px))/max(ref_px or 1.0, 1.0))*1e5 if (px and ref_px) else 0.0
        return before_bias + td + px_pen

    cand.sort(key=_score)
    return cand[0] if cand else None

def _position_near_entry(session, sym_variants, entry_dt):
    if not entry_dt: return None
    lo = entry_dt - timedelta(hours=6)
    hi = entry_dt + timedelta(hours=24)
    rows = session.exec(
        select(Position).where(Position.opened_ts.is_not(None), Position.opened_ts >= lo, Position.opened_ts <= hi)
        .order_by(Position.opened_ts.asc())
    ).all()
    if not rows: return None
    sv = {v.upper() for v in sym_variants}
    rows = [p for p in rows if p.symbol and p.symbol.upper() in sv] or rows
    return rows[0] if rows else None

def _reason_from_order(order: Order | None) -> str:
    if not order or not order.reason:
        return ""
    r = (order.reason or "").lower()
    def has(x): return (x in r)
    if has("tp2"): return "WIN: TP2"
    if has("tp1"): return "WIN: TP1"
    if has("tsl"): return "WIN: TSL"
    if has("be"):  return "WIN: BE"
    if has("timeout"): return "TIMEOUT"
    if has("atr"): return "LOSS: ATR @ STOP"
    if has("stop"): return "LOSS: STOP REACHED"
    return order.reason  # fallback to raw text

@app.get("/export/positions.csv", response_class=PlainTextResponse)
def export_positions_csv(
    start: str = Query(..., description="ISO start (e.g. 2025-08-23 or 2025-08-23T00:00:00Z)"),
    end:   str = Query(..., description="ISO end")
):
    dt_start = _parse_iso_loose(start); dt_end = _parse_iso_loose(end)
    if not dt_start or not dt_end or dt_end <= dt_start:
        raise HTTPException(status_code=400, detail="Invalid start/end")

    header = [
        "Status","Symbol","BUY SCORE","BUY TIME PLACED (AUTOPICKLR)","BUY TIME EXECUTED (KRAKEN)",
        "BUY PRICE PLACED (AUTOPICKLR)","BUY PRICE EXECUTED (KRAKEN)","BUY QUANTITY (KRAKEN)",
        "BUY COST (KRAKEN)","BUY FEE (KRAKEN)","ATR STOP","EMA SLOPE","BREAKOUT PERCENTAGE","RR","EMA SPREAD",
        "SELL TIME PLACED (AUTOPICKLR)","SELL TIME EXECUTED (KRAKEN)","SELL PRICE PLACED (AUTOPICKLR)","SELL PRICE EXECUTED (KRAKEN)",
        "SELL QUANTITY (% OF POSITION)","SELL QUANTITY","SELL COST","SELL FEE",
        "REASON FOR SELL (WIN: TP1, TP2, TSL @ %, BE @ %, TIMEOUT. OR LOSS: STOP REACHED, ATR @ %, TIMEOUT)",
        "P/L (%)","P/L ($)","TIME POSITION WAS OPEN (HOURS)","POSITION HIGHEST P/L %","POSITION LOWEST P/L %"
    ]

    buf = io.StringIO(); w = csv.writer(buf); w.writerow(header)

    ex = _get_exchange()
    since_ms = int(dt_start.timestamp()*1000); until_ms = int(dt_end.timestamp()*1000)

    # 1) Kraken-first: gather all executions across USD/USDT symbols you touched
    symbols = _candidate_symbols_for_window(ex, Session, dt_start, dt_end)
    all_trades = []
    for sym in symbols:
        try:
            all_trades += _fetch_all_trades_between(ex, sym, since_ms, until_ms)
        except Exception as e:
            print(f"[export] fetch_my_trades({sym}) failed: {e}")

    # filter window just in case
    all_trades = [t for t in all_trades if since_ms <= (t.get("timestamp") or 0) <= until_ms]
    if not all_trades:
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")

    # 2) Group by base + build episodes (FIFO; long-only)
    grouped = defaultdict(list)
    for t in all_trades:
        base = _base_from_ccxt_symbol(t.get("symbol") or "")
        if base:
            grouped[base].append(t)

    episodes = []  # {base, buys:[...], sells:[...]}
    for base, ts in grouped.items():
        ts.sort(key=lambda x: x.get("timestamp") or 0)
        pos_qty = 0.0
        ep = {"base": base, "buys": [], "sells": []}
        for tr in ts:
            side = (tr.get("side") or "").lower()
            amt  = float(tr.get("amount") or 0.0)
            if side == "buy":
                if pos_qty <= 1e-12 and (ep["buys"] or ep["sells"]):
                    episodes.append(ep); ep = {"base": base, "buys": [], "sells": []}
                ep["buys"].append(tr); pos_qty += amt
            elif side == "sell":
                ep["sells"].append(tr); pos_qty -= amt
                if pos_qty <= 1e-12:
                    episodes.append(ep); ep = {"base": base, "buys": [], "sells": []}
        if ep["buys"] or ep["sells"]:
            episodes.append(ep)

    # 3) Emit rows
    with Session(engine) as s:
        for ep in episodes:
            base = ep["base"]; sym_variants = _norm_variants(base)
            buys = ep["buys"]; sells = ep["sells"]
            if not buys: continue

            buy_dt_first = _first_ts(buys); buy_dt_last = _last_ts(buys)
            buy_px = _wavg_px(buys); buy_qty = sum(float(x.get("amount") or 0.0) for x in buys)
            buy_cost = _sum_cost(buys); buy_fee = _sum_fees(buys)

            # Attach Autopicklr BUY & Position (for score/ATR stop/target)
            buy_ord = _nearest_order(s, sym_variants, "BUY", buy_dt_first or buy_dt_last, buy_px)
            pos_at_entry = _position_near_entry(s, sym_variants, buy_dt_first or buy_dt_last)

            buy_score = None
            if pos_at_entry and getattr(pos_at_entry, "score", None) is not None:
                buy_score = float(pos_at_entry.score)
            elif buy_ord and getattr(buy_ord, "score", None) is not None:
                buy_score = float(buy_ord.score)

            atr_stop_px = float(getattr(pos_at_entry, "stop", 0.0) or 0.0) if pos_at_entry else None
            target_px   = float(getattr(pos_at_entry, "target", 0.0) or 0.0) if pos_at_entry else None
            rr = None
            if buy_px and atr_stop_px and target_px and (target_px>buy_px) and (buy_px>atr_stop_px):
                rr = (target_px - buy_px) / (buy_px - atr_stop_px)

            ema_slope, breakout, spread, sym_used = _entry_metrics(s, sym_variants, buy_dt_first or buy_dt_last, buy_px)

            # SELL side
            closed_qty = sum(float(x.get("amount") or 0.0) for x in sells)
            status = "Closed" if closed_qty >= (buy_qty - 1e-12) and closed_qty>0 else "Open"

            if sells:
                sell_dt_first = _first_ts(sells); sell_dt_last = _last_ts(sells)
                sell_px = _wavg_px(sells); sell_cost = _sum_cost(sells); sell_fee = _sum_fees(sells)

                sell_ord = _nearest_order(s, sym_variants, "SELL", sell_dt_last or sell_dt_first, sell_px)
                reason = _reason_from_order(sell_ord)

                pct_of_pos = (closed_qty / buy_qty * 100.0) if buy_qty>0 else None

                # P/L on closed qty
                pl_d = pl_pct = None
                if buy_px and sell_px and closed_qty>0:
                    pl_d = (sell_px - buy_px) * min(closed_qty, buy_qty)
                    pl_pct = (sell_px/buy_px - 1.0) * 100.0

                # duration
                t_open_h = None
                t0 = buy_dt_first or buy_dt_last; t1 = sell_dt_last or sell_dt_first
                if t0 and t1: t_open_h = (t1 - t0).total_seconds()/3600.0

                hi_pl, lo_pl = _hi_lo_pl_pct(s, sym_used, buy_dt_first, sell_dt_last, buy_px) if (sym_used and buy_dt_first and sell_dt_last and buy_px) else (None, None)

                row = [
                    status,
                    f"{base}/USD",
                    (f"{buy_score:.6f}" if buy_score is not None else ""),
                    _iso_or_blank(buy_ord.ts if buy_ord else None),
                    _iso_or_blank(buy_dt_first or buy_dt_last),
                    (f"{float(buy_ord.price_req):.10f}" if (buy_ord and buy_ord.price_req is not None) else ""),
                    (f"{buy_px:.10f}" if buy_px is not None else ""),
                    (f"{buy_qty:.10f}" if buy_qty else ""),
                    (f"{buy_cost:.10f}" if buy_cost else ""),
                    (f"{buy_fee:.10f}" if buy_fee else ""),
                    (f"{atr_stop_px:.10f}" if atr_stop_px else ""),
                    (f"{ema_slope:.10f}" if ema_slope is not None else ""),
                    (f"{(breakout*100.0):.6f}" if breakout is not None else ""),
                    (f"{rr:.6f}" if rr is not None else ""),
                    (f"{spread:.10f}" if spread is not None else ""),
                    _iso_or_blank(sell_ord.ts if sell_ord else None),
                    _iso_or_blank(sell_dt_last or sell_dt_first),
                    (f"{float(sell_ord.price_req):.10f}" if (sell_ord and sell_ord.price_req is not None) else ""),
                    (f"{sell_px:.10f}" if sell_px is not None else ""),
                    (f"{pct_of_pos:.6f}" if pct_of_pos is not None else ""),
                    (f"{closed_qty:.10f}" if closed_qty else ""),
                    (f"{sell_cost:.10f}" if sell_cost else ""),
                    (f"{sell_fee:.10f}" if sell_fee else ""),
                    reason,
                    (f"{pl_pct:.6f}" if pl_pct is not None else ""),
                    (f"{pl_d:.10f}" if pl_d is not None else ""),
                    (f"{t_open_h:.6f}" if t_open_h is not None else ""),
                    (f"{hi_pl:.6f}" if hi_pl is not None else ""),
                    (f"{lo_pl:.6f}" if lo_pl is not None else ""),
                ]
                w.writerow(row)

            else:
                # OPEN: mark to dt_end to fill row
                mark_px = None
                try:
                    # try latest candle up to dt_end
                    for v in sym_variants:
                        c = s.exec(select(Candle).where(Candle.symbol==v, Candle.ts<=dt_end).order_by(Candle.ts.desc())).first()
                        if c: mark_px=float(c.close); break
                except Exception: pass
                if mark_px is None:
                    try:
                        tkr = ex.fetch_ticker(f"{base}/USD" if f"{base}/USD" in ex.markets else f"{base}/USDT")
                        mark_px = float(tkr.get("last") or tkr.get("close") or 0.0)
                    except Exception: pass

                sell_dt = dt_end
                pct_of_pos = 0.0  # no realized sell yet

                pl_d = pl_pct = None
                if buy_px and mark_px and buy_qty>0:
                    pl_d = (mark_px - buy_px) * buy_qty
                    pl_pct = (mark_px/buy_px - 1.0) * 100.0

                t_open_h = ( (sell_dt - (buy_dt_first or buy_dt_last)).total_seconds()/3600.0 ) if (buy_dt_first or buy_dt_last) else None
                hi_pl, lo_pl = _hi_lo_pl_pct(s, sym_used, buy_dt_first, sell_dt, buy_px) if (sym_used and buy_dt_first and sell_dt and buy_px) else (None, None)

                row = [
                    "Open",
                    f"{base}/USD",
                    (f"{buy_score:.6f}" if buy_score is not None else ""),
                    _iso_or_blank(buy_ord.ts if buy_ord else None),
                    _iso_or_blank(buy_dt_first or buy_dt_last),
                    (f"{float(buy_ord.price_req):.10f}" if (buy_ord and buy_ord.price_req is not None) else ""),
                    (f"{buy_px:.10f}" if buy_px is not None else ""),
                    (f"{buy_qty:.10f}" if buy_qty else ""),
                    (f"{buy_cost:.10f}" if buy_cost else ""),
                    (f"{buy_fee:.10f}" if buy_fee else ""),
                    (f"{atr_stop_px:.10f}" if atr_stop_px else ""),
                    (f"{ema_slope:.10f}" if ema_slope is not None else ""),
                    (f"{(breakout*100.0):.6f}" if breakout is not None else ""),
                    (f"{rr:.6f}" if rr is not None else ""),
                    (f"{spread:.10f}" if spread is not None else ""),
                    "",  # no local SELL order placed yet
                    _iso_or_blank(sell_dt),
                    "",  # no placed price
                    (f"{mark_px:.10f}" if mark_px is not None else ""),
                    (f"{pct_of_pos:.6f}"),
                    (f"{buy_qty:.10f}" if buy_qty else ""),
                    (f"{(buy_qty*(mark_px or 0.0)):.10f}" if mark_px is not None else ""),
                    "",  # no Kraken fee until real sell
                    "",  # no reason yet
                    (f"{pl_pct:.6f}" if pl_pct is not None else ""),
                    (f"{pl_d:.10f}" if pl_d is not None else ""),
                    (f"{t_open_h:.6f}" if t_open_h is not None else ""),
                    (f"{hi_pl:.6f}" if hi_pl is not None else ""),
                    (f"{lo_pl:.6f}" if lo_pl is not None else ""),
                ]
                w.writerow(row)

    return PlainTextResponse(buf.getvalue(), media_type="text/csv")



# ----------------------------------------
# Migrations (idempotent)
# ----------------------------------------
def _column_missing(conn, table: str, col: str) -> bool:
    info = conn.execute(text(f"PRAGMA table_info('{table}')")).fetchall()
    names = {row[1] for row in info}
    return col not in names

def migrate_db(engine):
    with engine.connect() as conn:
        pos_cols = [
            ("current_px", "REAL"),
            ("pl_usd", "REAL"),
            ("pl_pct", "REAL"),
            ("score", "REAL"),
            ("be_price", "REAL"),
            ("tp1_price", "REAL"),
            ("tsl_price", "REAL"),
            ("tp1_done", "INTEGER DEFAULT 0"),
            ("tp2_done", "INTEGER DEFAULT 0"),
            ("be_moved", "INTEGER DEFAULT 0"),
            ("tsl_active", "INTEGER DEFAULT 0"),
            ("tsl_high", "REAL"),
            ("time_in_trade_min", "REAL"),
            ("custom_tsl_pct", "REAL"),
        ]
        for col, typ in pos_cols:
            if _column_missing(conn, "position", col):
                conn.execute(text(f"ALTER TABLE position ADD COLUMN {col} {typ}"))

        if _column_missing(conn, "trade", "duration_min"):
            conn.execute(text("ALTER TABLE trade ADD COLUMN duration_min REAL"))
        conn.commit()

# ----------------------------------------
# Reconcile live balances → DB (spot)
# ----------------------------------------
def _extract_base(symbol: str) -> str:
    return (symbol or "").split("/")[0].strip()

def _mid_from_ticker(ex, symbol: str) -> float:
    try:
        t = ex.fetch_ticker(symbol)
        b = float(t.get("bid") or 0)
        a = float(t.get("ask") or 0)
        last = float(t.get("last") or 0)
        return (b + a) / 2 if b and a else (last or float(t.get("close") or 0) or 0.0)
    except Exception:
        return 0.0

def reconcile_spot_positions(session: Session):
    try:
        ex = _get_exchange()
    except Exception as e:
        print(f"[reconcile] cannot init exchange: {e}")
        return

    # env and balances
    dust_usd = float(os.environ.get("POSITION_DUST_USD", "1.00") or 1.00)
    try:
        bal = BROKER_HANDLE.get_balance()
        total = bal.get("total", {}) or {}
    except Exception as e:
        print(f"[reconcile] fetch_balance failed: {e}")
        return

    # Assets held at Kraken (normalize, drop USD)
    assets = { _norm_asset(a): _fnum(q) for a, q in total.items() if _fnum(q) > 0.0 }
    assets.pop("USD", None)

    # Optionally import live holdings that have no DB position yet (e.g., SUI)
    if _env_true("IMPORT_EXTERNAL_POSITIONS", "1"):
        for asset, qty in list(assets.items()):
            try:
                sym = _usd_sym(asset)                           # e.g., "SUI/USD"
                px_now = _mid_from_ticker(ex, sym)
                if px_now <= 0:
                    continue
                val_usd = px_now * float(qty or 0.0)
                if val_usd < dust_usd:
                    continue

                # Skip if OPEN position already exists
                existing = session.exec(
                    select(Position).where(Position.status == "OPEN", Position.symbol == sym)
                ).first()
                if existing:
                    continue

                # Create an OPEN position mirroring the live holding
                from datetime import datetime
                p = Position(
                    symbol=sym,
                    qty=float(qty),
                    avg_price=float(px_now),                # synthetic entry at current mid
                    opened_ts=datetime.utcnow(),
                    stop=float(px_now * (1.0 - S.STOP_PCT)),
                    target=float(px_now * (1.0 + S.TARGET_PCT)),
                    status="OPEN",
                    tp1_done=False,
                    tp2_done=False,
                    be_moved=False,
                    tsl_active=False,
                    tsl_high=None,
                )
                # Populate UI fields
                p.current_px = float(px_now)
                p.pl_usd = 0.0
                p.pl_pct = 0.0
                try:
                    p.tp1_price = float(px_now) * (1.0 + (S.PTP_LEVELS[0] if getattr(S, "PTP_LEVELS", None) else 0.037))
                except Exception:
                    p.tp1_price = None

                session.add(p)
                session.commit()
                print(f"[reconcile] external import: created OPEN position for {sym} qty={qty:.8f} @~{px_now:.8f} (≈${val_usd:.2f})")
            except Exception as ee:
                print(f"[reconcile] external import failed for {asset}: {ee}")

    # helper to value a given symbol/qty at current mid
    def _usd_value(sym: str, qty: float) -> tuple[float, float]:
        px = _mid_from_ticker(ex, sym)
        return (px * float(qty or 0.0), px)

    # Reconcile existing DB OPEN positions vs live balances
    open_rows = session.exec(select(Position).where(Position.status == "OPEN")).all()
    if not open_rows:
        return

    for p in open_rows:
        base = _extract_base(p.symbol)
        base_alias = {"XBT": "BTC", "ZUSD": "USD"}.get(base, base)

        # Avoid churn on very fresh entries
        try:
            age_sec = (datetime.utcnow() - (p.opened_ts or datetime.utcnow())).total_seconds()
            if age_sec < 300:
                continue
        except Exception:
            pass

        held_qty = float(total.get(base_alias) or total.get(base) or 0.0)
        db_qty = float(getattr(p, "qty", 0.0) or 0.0)
        if db_qty <= 0:
            continue

        val_db, px_now = _usd_value(p.symbol, db_qty)

        # A) Exchange shows zero, so DB should be closed if tiny or price known
        if held_qty <= 0:
            if val_db <= dust_usd or px_now <= 0:
                entry_px = float(p.avg_price or 0.0)
                exit_px = px_now or entry_px
                qty = db_qty
                pnl_usd = (exit_px - entry_px) * qty
                tr = Trade(
                    symbol=p.symbol,
                    entry_ts=p.opened_ts,
                    exit_ts=datetime.utcnow(),
                    entry_px=entry_px,
                    exit_px=exit_px,
                    qty=qty,
                    pnl_usd=pnl_usd,
                    result=("WIN" if pnl_usd > 0 else "LOSS" if pnl_usd < 0 else "EVEN"),
                )
                session.add(tr)
                p.status = "CLOSED"
                p.current_px = px_now
                p.pl_usd = pnl_usd
                p.pl_pct = ((exit_px / entry_px - 1.0) * 100.0) if entry_px else 0.0
                session.commit()
                print(f"[reconcile] Closed locally {p.symbol} qty={qty} (sold on exchange)")
                continue

        # B) Exchange shows partial reduction
        if 0 < held_qty < db_qty:
            closed_qty = db_qty - held_qty
            exit_px = px_now or float(p.avg_price or 0.0)
            entry_px = float(p.avg_price or 0.0)
            pnl_usd = (exit_px - entry_px) * closed_qty
            tr = Trade(
                symbol=p.symbol,
                entry_ts=p.opened_ts,
                exit_ts=datetime.utcnow(),
                entry_px=entry_px,
                exit_px=exit_px,
                qty=closed_qty,
                pnl_usd=pnl_usd,
                result=("WIN" if pnl_usd > 0 else "LOSS" if pnl_usd < 0 else "EVEN"),
            )
            session.add(tr)
            p.qty = held_qty
            p.current_px = px_now
            p.pl_usd = (px_now - entry_px) * held_qty
            p.pl_pct = ((px_now / entry_px - 1.0) * 100.0) if entry_px else 0.0
            session.commit()
            print(f"[reconcile] Partial reduce {p.symbol}: -{closed_qty}, keep {held_qty}")
            continue


# ----------------------------------------
# Daily metrics
# ----------------------------------------
def _day_floor_utc(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day)

# ----------------------------------------
# Score monitor payload
# ----------------------------------------
def score_monitor_payload(session: Session, symbols: List[str]) -> dict:
    scores = []
    passing = []
    for sym in symbols:
        sigs = compute_signals(session, sym)
        if not sigs:
            continue
        best = max(sigs, key=lambda s: float(getattr(s, "score", 0.0) or 0.0))
        sc = float(getattr(best, "score", 0.0) or 0.0)
        scores.append(sc)
        if sc >= SCORE_THRESHOLD:
            passing.append({"symbol": sym, "score": sc})

    bins = [0] * 10
    for sc in scores:
        idx = min(9, max(0, int(sc * 10)))
        bins[idx] += 1

    passing.sort(key=lambda r: r["score"], reverse=True)
    return {
        "scored": len(scores),
        "threshold": SCORE_THRESHOLD,
        "above_threshold": len(passing),
        "above_threshold_list": passing[:50],
        "histogram_bins": bins,
        "scores_sample": sorted(scores, reverse=True)[:50],
    }

# ----------------------------------------
# Lifespan
# ----------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global BROKER_HANDLE
    print("Starting autoPicklr Trading Simulator...")

    SQLModel.metadata.create_all(engine)
    print("[startup] Running simple migrations (add missing columns)...")
    migrate_db(engine)

    BROKER_HANDLE = make_broker(engine)
    print(f"[startup] Broker initialized: {getattr(BROKER_HANDLE, 'name', 'unknown')} (paper={getattr(BROKER_HANDLE, 'paper', None)})")

    try:
        with Session(engine) as s:
            bal = BROKER_HANDLE.get_balance(session=s)
            print(f"[startup] Balance: cash=${bal.cash_usd:,.2f}, equity=${bal.equity_usd:,.2f}")
            w = s.get(Wallet, 1)
            if w:
                w.balance_usd = bal.cash_usd
                w.equity_usd  = bal.equity_usd
                s.commit()
            print(f"[startup] Wallet synced from Kraken: cash=${bal.cash_usd:,.2f}, equity=${bal.equity_usd:,.2f}")
    except Exception as e:
        print(f"[startup] Could not fetch broker balance: {e}")

    with Session(engine) as s:
        print("[startup] Ensuring wallet exists...")
        ensure_wallet(s)


        # --- Force-include only real spot holdings (USD-filtered; no .F) ---
        try:
            HOLDINGS_DUST_USD = float(os.environ.get("HOLDINGS_DUST_USD", "1.00") or 1.00)
        except Exception:
            HOLDINGS_DUST_USD = 1.00
        try:
            if getattr(BROKER_HANDLE, "live", False) and hasattr(BROKER_HANDLE, "list_spot_holdings_bases_filtered"):
                live_bases = BROKER_HANDLE.list_spot_holdings_bases_filtered(s, min_usd=HOLDINGS_DUST_USD)
                if live_bases:
                    ensured = await ensure_pairs_for(s, live_bases)
                    if ensured:
                        print(f"[startup] Ensured Kraken mapping for LIVE holdings: {ensured}")
        except Exception as e:
            print(f"[startup] live holdings ensure failed: {e}")

        print("[startup] Refreshing universe once...")
        await refresh_universe(s)


    # Also ensure universe includes LIVE holdings from Kraken (e.g., SUI)
    try:
        if getattr(BROKER_HANDLE, "live", False) and hasattr(BROKER_HANDLE, "list_live_holdings"):
            bases = BROKER_HANDLE.list_live_holdings()
            if bases:
                from universe import ensure_pairs_for
                added = await ensure_pairs_for(s, bases)
                if added:
                    print(f"[startup] Ensured Kraken mapping for LIVE holdings: {added}")
    except Exception as e:
        print(f"[startup] ensure LIVE holdings failed: {e}")


    print("[startup] Starting trading loop...")
    loop_task = asyncio.create_task(trading_loop())
    print("[startup] Startup complete!")
    try:
        yield
    finally:
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

app.router.lifespan_context = lifespan

# ----------------------------------------
# Static & templates
# ----------------------------------------
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ----------------------------------------
# Pages & small APIs
# ----------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/ping")
def ping():
    return {"message": "pong"}

@app.get("/api/broker")
def api_broker():
    h = BROKER_HANDLE
    bal = h.get_balance()
    return {
        "broker": getattr(h, "name", "unknown"),
        "paper": getattr(h, "paper", True),
        "cash_usd": getattr(bal, "cash_usd", 0.0),
        "equity_usd": getattr(bal, "equity_usd", 0.0),
    }

@app.get("/api/monitor/model")
def api_monitor_model(limit: int = Query(50, ge=1, le=500)):
    with Session(engine) as s:
        active_syms = get_active_universe(s) or list(DEFAULT_UNIVERSE)
        mon = score_monitor_payload(s, active_syms)
        mon["above_threshold_list"] = mon["above_threshold_list"][:limit]
        return mon

# ----------------------------------------
# Stops study (MAE/MFE percentile based)
# ----------------------------------------
@app.get("/api/stops/study")
def api_stops_study(
    symbol: Optional[str] = Query(None),
    wins_only: int = Query(1),
    max_days: int = Query(365),
    pct_for: float = Query(95.0),
):
    with Session(engine) as s:
        q = select(Trade).where(Trade.exit_ts.is_not(None))
        if symbol:
            q = q.where(Trade.symbol == symbol.upper())

        since = datetime.utcnow() - timedelta(days=max_days)
        q = q.where(Trade.entry_ts >= since)

        rows = s.exec(q.order_by(Trade.exit_ts.desc())).all()
        if not rows:
            return {"ok": True, "trades": 0, "note": "No trades in range."}

        out = []
        for t in rows:
            epx = float(t.entry_px or 0.0)
            if epx <= 0 or not t.entry_ts or not t.exit_ts:
                continue
            cs = s.exec(
                select(Candle)
                .where(Candle.symbol == t.symbol, Candle.ts >= t.entry_ts, Candle.ts <= t.exit_ts)
                .order_by(Candle.ts.asc())
            ).all()
            if not cs:
                continue
            lows = [float(c.low) if c.low is not None else float(c.close) for c in cs]
            highs = [float(c.high) if c.high is not None else float(c.close) for c in cs]
            if not lows or not highs:
                continue
            min_low = min(lows)
            max_high = max(highs)
            mae_pct = (min_low / epx - 1.0) * 100.0
            mfe_pct = (max_high / epx - 1.0) * 100.0
            out.append(
                {
                    "symbol": t.symbol,
                    "entry_ts": t.entry_ts.isoformat(),
                    "exit_ts": t.exit_ts.isoformat(),
                    "result": t.result,
                    "entry_px": round(epx, 8),
                    "exit_px": (None if t.exit_px is None else round(float(t.exit_px), 8)),
                    "mae_pct": mae_pct,
                    "mfe_pct": mfe_pct,
                }
            )

        if wins_only:
            out = [r for r in out if r["result"] == "WIN"]

        n = len(out)
        if n == 0:
            return {"ok": True, "trades": 0, "note": "No trades after filters."}

        maes = sorted(r["mae_pct"] for r in out)
        mfes = sorted(r["mfe_pct"] for r in out)

        def _percentile(arr, p):
            if not arr:
                return None
            k = max(0, min(len(arr) - 1, int(round((p / 100.0) * (len(arr) - 1)))))
            return arr[k]

        p_mae = _percentile(maes, pct_for)
        p_mfe = _percentile(mfes, pct_for)

        suggested_stop_pct = None
        if wins_only and p_mae is not None:
            suggested_stop_pct = round(float(-p_mae) * 1.05, 3)

        return {
            "ok": True,
            "trades_analyzed": n,
            "wins_only": bool(wins_only),
            "percentile": pct_for,
            "mae_pctiles_sample": {
                "p25": _percentile(maes, 25),
                "p50": _percentile(maes, 50),
                "p75": _percentile(maes, 75),
                "p90": _percentile(maes, 90),
                "p95": _percentile(maes, 95),
                "p99": _percentile(maes, 99),
            },
            "mfe_pctiles_sample": {
                "p50": _percentile(mfes, 50),
                "p75": _percentile(mfes, 75),
                "p90": _percentile(mfes, 90),
            },
            "suggested_stop_pct": suggested_stop_pct,
            "note": "MAE/MFE are minute-candle approximations; true tick MAE can be slightly worse.",
            "examples": out[:25],
        }

# ----------------------------------------
# Diagnostics router: stop uplift + ATR grid
# ----------------------------------------
diag = APIRouter(prefix="/diagnostics", tags=["analytics"])

def _load_candles(session: Session, symbol: str, start: datetime, end: datetime):
    q = (
        select(Candle)
        .where(Candle.symbol == symbol, Candle.ts >= start, Candle.ts <= end)
        .order_by(Candle.ts.asc())
    )
    return session.exec(q).all()

def _would_be_win_for_stop(
    session: Session,
    t: Trade,
    stop_pct: float,
    tp_pct: float,
    use_time_exit: bool = True,
):
    entry = float(t.entry_px or 0.0)
    if entry <= 0:
        return False, "bad_entry", None

    be_edge = (FEE_PCT * 2.0) + (SLIPPAGE_PCT * 2.0)
    be_level = entry * (1.0 + be_edge)
    tp_level = entry * (1.0 + tp_pct + be_edge)
    stop_level = entry * (1.0 - stop_pct)

    # Robust horizon: honor MAX_HOLD_MINUTES if requested; otherwise 12h fallback
    FALLBACK_MIN = 12 * 60
    if use_time_exit:
        max_hold = int(MAX_HOLD_MINUTES or 0)
        horizon = t.entry_ts + timedelta(minutes=max_hold if max_hold > 0 else FALLBACK_MIN)
    else:
        horizon = t.entry_ts + timedelta(minutes=FALLBACK_MIN)

    candles = _load_candles(session, t.symbol, t.entry_ts, horizon)
    if not candles:
        return False, "no_candles", None

    stop_hit = False
    target_hit = False
    hit_ts = None

    for c in candles:
        px_low = getattr(c, "low", None) or c.close
        px_high = getattr(c, "high", None) or c.close

        if px_low <= stop_level:
            stop_hit = True
            hit_ts = c.ts
            break

        if px_high >= tp_level:
            target_hit = True
            hit_ts = c.ts
            break

    if target_hit and not stop_hit:
        return True, "hit_target", hit_ts

    if (not stop_hit) and (not target_hit):
        last_close = candles[-1].close
        if last_close >= be_level:
            return True, "ended_profitable", candles[-1].ts

    return False, "stopped_or_never_profitable", hit_ts

@diag.get("/stop_uplift")
def stop_uplift(
    session: Session = Depends(get_session),
    min_stop: float = Query(0.005, description="Min stop pct (0.005=0.5%)"),
    max_stop: float = Query(0.050, description="Max stop pct (0.050=5.0%)"),
    step: float = Query(0.0025, description="Grid step (0.0025=0.25%)"),
    tp_pct: float = Query(0.037, description="Target to define 'win' (0.037=+3.7% TP1)"),
    use_time_exit: bool = Query(True, description="Honor MAX_HOLD_MINUTES horizon"),
    include_examples: bool = Query(True, description="Attach examples"),
):
    losers = session.exec(
        select(Trade).where(Trade.result == "LOSS").order_by(Trade.entry_ts.asc())
    ).all()

    stops = []
    s = float(min_stop)
    while s <= float(max_stop) + 1e-12:
        stops.append(round(s, 6))
        s += float(step)

    out = {
        "losers_analyzed": len(losers),
        "grid": {"min_stop": min_stop, "max_stop": max_stop, "step": step},
        "target_for_win": tp_pct,
        "notes": [
            "Counterfactual on minute bars; true tick path may differ.",
            "Breakeven and target checks include fees+slippage buffer.",
        ],
        "stops": {},
    }

    for sp in stops:
        turned = 0
        examples = []
        for t in losers:
            ok, mode, when = _would_be_win_for_stop(session, t, sp, tp_pct, use_time_exit)
            if ok:
                turned += 1
                if include_examples and len(examples) < 10:
                    examples.append(
                        {
                            "symbol": t.symbol,
                            "entry_ts": t.entry_ts,
                            "exit_ts": t.exit_ts,
                            "entry_px": float(t.entry_px or 0),
                            "exit_px": float(t.exit_px or 0),
                            "mode": mode,
                            "when": when,
                        }
                    )
        rate = (turned / len(losers)) if losers else 0.0
        out["stops"][f"{sp:.4f}"] = {
            "turned_winners": turned,
            "turn_rate": round(rate, 4),
            "examples": examples,
        }

    return out

# --- ATR at-entry helper (simple SMA of True Range) ---
def _atr_at_entry(session: Session, symbol: str, entry_ts: datetime, atr_len: int) -> Optional[float]:
    rows_desc = (
        session.exec(
            select(Candle)
            .where(Candle.symbol == symbol, Candle.ts <= entry_ts)
            .order_by(Candle.ts.desc())
            .limit(atr_len + 1)
        ).all()
    )
    if len(rows_desc) < atr_len + 1:
        return None
    rows = list(reversed(rows_desc))
    trs = []
    for i in range(1, len(rows)):
        h = float(rows[i].high if rows[i].high is not None else rows[i].close)
        l = float(rows[i].low if rows[i].low is not None else rows[i].close)
        pc = float(rows[i - 1].close)
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < atr_len:
        return None
    return sum(trs[-atr_len:]) / atr_len

@diag.get("/atr_grid")
def atr_grid(
    session: Session = Depends(get_session),
    min_floor_from: float = Query(0.005),
    min_floor_to: float = Query(0.030),
    min_floor_step: float = Query(0.0025),
    atr_mult_from: float = Query(1.0),
    atr_mult_to: float = Query(3.0),
    atr_mult_step: float = Query(0.25),
    tp_pct: float = Query(0.037),
    atr_len: Optional[int] = Query(None),
    use_time_exit: bool = Query(True),
    include_examples: bool = Query(True),
):
    losers = session.exec(
        select(Trade).where(Trade.result == "LOSS").order_by(Trade.entry_ts.asc())
    ).all()
    if not losers:
        return {"ok": True, "losers_analyzed": 0, "note": "No losing trades found."}

    floors, mults = [], []
    f = float(min_floor_from)
    while f <= float(min_floor_to) + 1e-12:
        floors.append(round(f, 6))
        f += float(min_floor_step)
    m = float(atr_mult_from)
    while m <= float(atr_mult_to) + 1e-12:
        mults.append(round(m, 6))
        m += float(atr_mult_step)

    use_atr_len = int(atr_len or ATR_LEN)

    atr_cache: Dict[int, Optional[float]] = {}
    for t in losers:
        atr_cache[t.id] = _atr_at_entry(session, t.symbol, t.entry_ts, use_atr_len)

    from statistics import median

    out = {
        "losers_analyzed": len(losers),
        "axes": {
            "min_stop_pct": {
                "from": min_floor_from,
                "to": min_floor_to,
                "step": min_floor_step,
                "values": floors,
            },
            "atr_stop_mult": {
                "from": atr_mult_from,
                "to": atr_mult_to,
                "step": atr_mult_step,
                "values": mults,
            },
            "atr_len": use_atr_len,
            "tp_pct": tp_pct,
        },
        "cells": {},
        "best": [],
    }

    for floor in floors:
        for mult in mults:
            turned = 0
            eligible = 0
            eff_stops: List[float] = []
            examples = []
            for t in losers:
                entry = float(t.entry_px or 0.0)
                atr = atr_cache.get(t.id)
                if entry <= 0 or not atr:
                    continue
                eligible += 1

                atr_stop_pct = (mult * atr) / entry
                effective_stop_pct = max(floor, atr_stop_pct)
                eff_stops.append(effective_stop_pct)

                ok, mode, when = _would_be_win_for_stop(session, t, effective_stop_pct, tp_pct, use_time_exit)
                if ok:
                    turned += 1
                    if include_examples and len(examples) < 5:
                        examples.append(
                            {
                                "symbol": t.symbol,
                                "entry_ts": t.entry_ts,
                                "exit_ts": t.exit_ts,
                                "entry_px": float(t.entry_px),
                                "effective_stop_pct": round(effective_stop_pct, 6),
                                "mode": mode,
                                "when": when,
                            }
                        )

            rate = (turned / eligible) if eligible else 0.0
            key = f"{floor:.4f}|{mult:.2f}"
            out["cells"][key] = {
                "turned_winners": turned,
                "turn_rate": round(rate, 4),
                "eligible": eligible,
                "avg_effective_stop_pct": (round(sum(eff_stops) / len(eff_stops), 6) if eff_stops else None),
                "med_effective_stop_pct": (round(median(eff_stops), 6) if eff_stops else None),
                "examples": examples,
            }

    ranked = sorted(
        out["cells"].items(),
        key=lambda kv: (kv[1]["turn_rate"], kv[1]["turned_winners"]),
        reverse=True,
    )[:10]
    out["best"] = [{"cell": k, **v} for k, v in ranked]
    return out

app.include_router(diag)

# ----------------------------------------
# Admin
# ----------------------------------------
@app.post("/admin/backfill_hourly")
async def admin_backfill_hourly(days: int = 365, symbols: Optional[str] = None):
    try:
        with Session(engine) as s:
            syms = [x.strip().upper() for x in symbols.split(",")] if symbols else None
            res = await backfill_hourly(s, symbols=syms, days=days)
            return {"ok": True, **res}
    except Exception as e:
        print("[admin/backfill_hourly] error:", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin/universe")
def admin_universe():
    with Session(engine) as s:
        rows = s.exec(select(UniversePair).order_by(UniversePair.usd_vol_24h.desc())).all()
        return {"count": len(rows), "symbols": [r.symbol for r in rows]}

@app.post("/admin/universe_refresh")
async def admin_universe_refresh():
    with Session(engine) as s:
        rows = await refresh_universe(s)
        return {"ok": True, "count": len(rows)}

# ----------------------------------------
# Sim status & summaries
# ----------------------------------------
@app.get("/api/sim")
def sim_status():
    with Session(engine) as s:
        w = s.get(Wallet, 1)
        wallet_equity = float(w.equity_usd) if w else 0.0
        wallet_balance = float(w.balance_usd) if w else 0.0

        pos = s.exec(select(Position).where(Position.status == "OPEN")).all()
        open_positions = []
        for p in pos:
            last_px = get_last_price(s, p.symbol)
            cur = float(last_px) if last_px is not None else float(p.avg_price)
            pl_usd = (cur - float(p.avg_price)) * float(p.qty)
            pl_pct = (cur / float(p.avg_price) - 1.0) * 100.0 if p.avg_price else 0.0
            age_min = None
            if p.opened_ts:
                age_min = (datetime.utcnow() - p.opened_ts).total_seconds() / 60.0
            open_positions.append(
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "avg": float(p.avg_price),
                    "price": cur,
                    "pl_usd": pl_usd,
                    "pl_pct": pl_pct,
                    "confidence": (None if p.score is None else float(p.score)),
                    "tp1": (None if p.tp1_price is None else float(p.tp1_price)),
                    "be": (float(p.be_price) if p.be_price is not None else (float(p.avg_price) if p.be_moved else None)),
                    "tsl": (None if p.tsl_price is None else float(p.tsl_price)),
                    "stop": float(p.stop),
                    "target": float(p.target),
                    "age_min": age_min,
                }
            )

        closed = s.exec(select(Trade).where(Trade.exit_ts.is_not(None))).all()
        total_pnl = float(sum((t.pnl_usd or 0.0) for t in closed))
        wins = sum(1 for t in closed if (t.pnl_usd or 0.0) > 0.0)
        win_rate = (wins / len(closed) * 100.0) if closed else 0.0

        recent_trades = (
            s.exec(select(Trade).where(Trade.exit_ts.is_not(None)).order_by(Trade.exit_ts.desc()).limit(10)).all()
        )
        recent_payload = [
            {
                "symbol": t.symbol,
                "entry": float(t.entry_px),
                "exit": (None if t.exit_px is None else float(t.exit_px)),
                "qty": float(t.qty),
                "pnl": (None if t.pnl_usd is None else float(t.pnl_usd)),
                "result": t.result,
                "duration_min": t.duration_min,
            }
            for t in recent_trades
        ]

        return {
            "wallet_equity": wallet_equity,
            "wallet_balance": wallet_balance,
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "total_trades": len(closed),
            "open_positions_count": len(open_positions),
            "open_positions": open_positions,
            "recent_trades": recent_payload,
        }

# ----------------------------------------
# Account snapshots
# ----------------------------------------
@app.get("/api/account")
def api_account():
    global ACCOUNT_BASELINE
    with Session(engine) as s:
        pos = s.exec(select(Position).where(Position.status == "OPEN")).all()
        mv = 0.0
        for p in pos:
            c = s.exec(select(Candle).where(Candle.symbol == p.symbol).order_by(Candle.ts.desc())).first()
            last = float(c.close) if c else float(p.avg_price or 0.0)
            mv += last * float(p.qty or 0.0)

    bal = BROKER_HANDLE.get_balance() if BROKER_HANDLE else None
    cash = float(getattr(bal, "cash_usd", 0.0))
    equity = float(getattr(bal, "equity_usd", 0.0)) or (cash + mv)

    if ACCOUNT_BASELINE <= 0 and equity > 0:
        ACCOUNT_BASELINE = equity

    exposure_pct = (mv / equity * 100.0) if equity > 0 else 0.0
    lifetime_pl_pct = ((equity - ACCOUNT_BASELINE) / ACCOUNT_BASELINE * 100.0) if ACCOUNT_BASELINE > 0 else 0.0

    return {
        "cash": cash,
        "equity": equity,
        "positions": len(pos),
        "exposure_pct": exposure_pct,
        "lifetime_pl_pct": lifetime_pl_pct,
        "mode": "LIVE (Kraken)",
    }


from datetime import datetime
from sqlmodel import Session, select
from models import Order, Position

def _has_filled_sell_since(session: Session, symbol: str, opened_ts: datetime) -> bool:
    row = session.exec(
        select(Order)
        .where(Order.symbol == symbol)
        .where(Order.side == "SELL")
        .where(Order.status == "FILLED")   # only real fills
        .where(Order.qty > 0)              # non-zero
        .where(Order.ts >= opened_ts)      # after entry
        .limit(1)
    ).first()
    return row is not None


@app.get("/debug/tp1")
def debug_tp1(symbol: str):
    from sqlmodel import select
    from sim import get_last_price
    import settings as S

    with Session(engine) as s:
        p = s.exec(select(Position).where(Position.status=="OPEN", Position.symbol==symbol.upper())).first()
        if not p:
            return {"ok": False, "error": "open position not found"}

        entry = float(p.avg_price or 0.0)
        last = get_last_price(s, p.symbol) or entry
        gross = (last/entry - 1.0) if entry else 0.0

        tp1_pct = (S.PTP_LEVELS[0] if S.PTP_LEVELS else 0.037)
        sell_frac = (S.PTP_SIZES[0] if S.PTP_SIZES else 0.30)

        return {
            "ok": True,
            "symbol": p.symbol,
            "entry": entry,
            "last": last,
            "gross_move_pct": round(gross*100, 4),
            "tp1_threshold_pct": round(tp1_pct*100, 4),
            "tp1_done": bool(getattr(p, "tp1_done", False)),
            "would_fire_now": bool((gross + 1e-12) >= tp1_pct and not getattr(p, "tp1_done", False)),
            "sell_frac": sell_frac,
            "min_sell_usd": float(os.environ.get("LIVE_MIN_ORDER_USD", "15.00") or 15.00),
        }

        
@app.get("/debug/tp1_audit")
def debug_tp1_audit(repair: bool = Query(False, description="If true, reset tp1_done when no SELL order exists since entry")):
    with Session(engine) as s:
        rows = s.exec(select(Position).where(Position.status == "OPEN")).all()
        report = []
        repaired = 0
        for p in rows:
            sym = p.symbol
            entry_ts = p.opened_ts or (datetime.utcnow() - timedelta(days=7))
            sell = _has_filled_sell_since(s, sym, entry_ts)
            drift = bool(getattr(p, "tp1_done", False)) and (not sell)
            if drift and repair:
                p.tp1_done = False
                p.be_moved = 0
                p.be_price = None
                s.commit()
                repaired += 1
            report.append({
                "symbol": sym,
                "tp1_done": bool(getattr(p, "tp1_done", False)),
                "opened_ts": (p.opened_ts.isoformat() if p.opened_ts else None),
                "sell_order_found_since_entry": bool(sell),
                "drift_detected": drift,
            })
        return {"ok": True, "repaired": repaired if repair else 0, "rows": report}

@app.middleware("http")
async def log_head_api(request, call_next):
    if request.method == "HEAD" and request.url.path == "/api":
        ua = request.headers.get("user-agent", "")
        print(f"[probe] HEAD /api from {request.client.host} UA={ua}")
    return await call_next(request)

@app.get("/api/positions")
def positions():
    with Session(engine) as s:
        xs = s.exec(select(Position).where(Position.status == "OPEN")).all()
        out = []
        for x in xs:
            out.append({
                "id": x.id,
                "symbol": x.symbol,
                "qty": x.qty,
                "avg_price": x.avg_price,
                "opened_ts": x.opened_ts.isoformat() if x.opened_ts else None,
                "stop": x.stop,
                "target": x.target,
                "status": x.status,
                # new fields used by the dashboard JS
                "current_price": getattr(x, "current_px", None),
                "pl_usd": getattr(x, "pl_usd", None),
                "pl_pct": getattr(x, "pl_pct", None),
                "confidence": getattr(x, "score", None),
                "tp1_price": getattr(x, "tp1_price", None),
                "be_price": getattr(x, "be_price", None),
                "tsl_price": getattr(x, "tsl_price", None),
                "time_in_trade_min": getattr(x, "time_in_trade_min", None),
            })
        return out


@app.get("/api/pp_summary")
def profit_protection_summary():
    with Session(engine) as s:
        xs = s.exec(select(Position).where(Position.status == "OPEN")).all()
        return {
            "open_positions": len(xs),
            "tp1_done": sum(1 for p in xs if getattr(p, "tp1_done", False)),
            "tp2_done": sum(1 for p in xs if getattr(p, "tp2_done", False)),
            "be_moved": sum(1 for p in xs if getattr(p, "be_moved", False)),
            "tsl_active": sum(1 for p in xs if getattr(p, "tsl_active", False)),
        }


@app.get("/api/trades")
def trades():
    with Session(engine) as s:
        # last 200 closed trades
        xs = s.exec(select(Trade).where(Trade.exit_ts.is_not(None))
                    .order_by(Trade.exit_ts.desc())).all()[:200]

        # pull recent SELL orders to infer reasons (TP1/TP2/STOP/TSL/TIME, etc.)
        sell_orders = s.exec(
            select(Order)
            .where(Order.side == "SELL", Order.status == "FILLED")
            .order_by(Order.ts.desc())
        ).all()

        def find_reason(sym, exit_ts):
            if not exit_ts:
                return None
            # best-effort: first SELL for same symbol within ±10 minutes of exit
            for o in sell_orders:
                if o.symbol != sym:
                    continue
                dt = abs((exit_ts - o.ts).total_seconds())
                if dt <= 600:   # 10 minutes
                    return o.reason
            return None

        out = []
        for x in xs:
            reason = find_reason(x.symbol, x.exit_ts)
            out.append({
                "id": x.id,
                "symbol": x.symbol,
                "entry_ts": x.entry_ts.isoformat() if x.entry_ts else None,
                "exit_ts": x.exit_ts.isoformat() if x.exit_ts else None,
                "entry_px": x.entry_px,
                "exit_px": x.exit_px,
                "qty": x.qty,
                "pnl_usd": x.pnl_usd,
                "result": x.result,
                "reason": reason,
            })
        return out
        
        @app.get("/api/account_v2")
        def api_account_v2(lookback_days: int = 7, symbols: Optional[str] = None):
            try:
                print("[/api/account_v2] hit", lookback_days, symbols)
                with Session(engine) as s:
                    # use broker abstraction; robust to invalid keys
                    bal = BROKER_HANDLE.get_balance(session=s)
                    cash_usd = float(getattr(bal, "cash_usd", 0.0))
                    equity_usd = float(getattr(bal, "equity_usd", cash_usd))

                    # live/DB holdings (bases)
                    HOLDINGS_DUST_USD = float(os.environ.get("HOLDINGS_DUST_USD", "1.00") or 1.00)
                    bases = []
                    try:
                        bases = BROKER_HANDLE.list_spot_holdings_bases(s, min_usd=HOLDINGS_DUST_USD)
                    except Exception as e:
                        print(f"[/api/account_v2] list_spot_holdings_bases failed: {e}")
                        bases = []

                    # prices via PUBLIC tickers
                    ex = getattr(BROKER_HANDLE, "exch", None) or _get_exchange()
                    prices: Dict[str, float] = {}
                    positions = []
                    pos_value_sum = 0.0
                    for base in bases:
                        # try USD then USDT
                        px = 0.0
                        for q in ("USD", "USDT"):
                            sym = f"{base}/{q}"
                            try:
                                t = ex.fetch_ticker(sym)
                                px = _mid_price(t) if "kraken" in ex.id else _mid_from_ticker(t)
                                if px > 0:
                                    break
                            except Exception:
                                continue
                        if px <= 0:
                            continue
                        # estimate qty from DB open positions first (if any)
                        qty = 0.0
                        p = s.exec(select(Position).where(Position.symbol == base, Position.status == "OPEN")).first()
                        if p:
                            qty = float(p.qty or 0.0)
                        val = qty * px
                        dust = float(os.environ.get("POSITION_DUST_USD", "1.00"))
                        if val >= dust:
                            pos_value_sum += val
                            prices[f"{base}/USD"] = px
                            positions.append({"asset": base, "symbol": f"{base}/USD", "qty": qty, "price_usd": px, "value_usd": val})

                    # realized pnl window — optional; safe empty if private API is down
                    trades = []
                    realized_pnl = 0.0

                    return {
                        "cash_usd": cash_usd,
                        "equity_usd": equity_usd,
                        "positions": positions,
                        "trades": trades,
                        "realized_pnl_window": realized_pnl,
                        "prices": prices,
                        "lookback_days": lookback_days,
                    }

            except Exception as e:
                print("[/api/account_v2] ERROR:", e)
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=str(e))



# ----------------------------------------
# Performance & metrics
# ----------------------------------------
@app.get("/api/performance")
def performance():
    start = float(os.environ.get("WALLET_START_USD", "1000") or 1000.0)
    with Session(engine) as s:
        trades = s.exec(select(Trade).where(Trade.exit_ts.is_not(None)).order_by(Trade.exit_ts.asc())).all()
        curve = []
        equity = start
        if trades:
            t0 = trades[0].exit_ts
            curve.append({"t": (t0 or datetime.utcnow()).isoformat(), "equity": equity})
        else:
            curve.append({"t": datetime.utcnow().isoformat(), "equity": equity})
        for t in trades:
            equity += float(t.pnl_usd or 0.0)
            curve.append({"t": (t.exit_ts or datetime.utcnow()).isoformat(), "equity": equity})
        pos = s.exec(select(Position).where(Position.status == "OPEN")).all()
        open_pnl = 0.0
        for p in pos:
            last_px = get_last_price(s, p.symbol)
            cur = float(last_px) if last_px is not None else float(p.avg_price)
            open_pnl += (cur - float(p.avg_price)) * float(p.qty)
        now_equity = equity + open_pnl
        curve.append({"t": datetime.utcnow().isoformat(), "equity": now_equity})
        return {"equity_curve": curve, "current_equity": now_equity, "start_equity": start}


# ----------------------------------------
# Trading loop
# ----------------------------------------
# main.py (only the trading_loop function body)
def _open_position_symbols(session: Session):
    rows = session.exec(select(Position).where(Position.status == "OPEN")).all()
    return sorted({p.symbol for p in rows if getattr(p, "symbol", None)})

async def trading_loop():
    global LAST_UNIVERSE_REFRESH, LAST_OPEN_POS_UPDATE, LAST_FULL_CANDLES_UPDATE, LAST_ROLLUP_DATE
    print("[loop] Starting trading loop...")
    await asyncio.sleep(2)
    print("[loop] Initial delay complete, entering main loop")
    while True:
        try:
            print("[loop] Processing trading cycle...")
            with Session(engine) as s:
                now = datetime.utcnow()

                # ---------- Universe refresh gating ----------
                should_check_time = (
                    LAST_UNIVERSE_REFRESH is None
                    or (now - LAST_UNIVERSE_REFRESH) >= timedelta(minutes=UNIVERSE_REFRESH_MINUTES)
                    or universe_stale(s)
                )
                # cash check using broker (falls back to wallet on auth failure)
                try:
                    bal = BROKER_HANDLE.get_balance(session=s)
                    wallet_cash = float(getattr(bal, "cash_usd", 0.0))
                except Exception:
                    w = s.get(Wallet, 1)
                    wallet_cash = float(getattr(w, "balance_usd", 0.0) or 0.0)

                min_trade = float(os.environ.get("LIVE_MIN_ORDER_USD", "15") or 15)
                eligible_for_entries = wallet_cash >= min_trade

                if should_check_time and eligible_for_entries:
                    print("[universe] Refreshing from Kraken…")
                    rows = await refresh_universe(s)
                    print(f"[universe] Refreshed {len(rows)} USD/USDT pairs.")
                    LAST_UNIVERSE_REFRESH = now
                else:
                    if should_check_time and not eligible_for_entries:
                        print("[universe] Skipped refresh (not eligible: cap/cash).")

                # ---------- Wallet sync (DB) ----------
                try:
                    bal = BROKER_HANDLE.get_balance(session=s)
                    w = s.get(Wallet, 1)
                    if w:
                        w.balance_usd = float(getattr(bal, "cash_usd", w.balance_usd))
                        # rebuild equity = cash + DB MV
                        from sim import _portfolio_market_value
                        mv = float(_portfolio_market_value(s))
                        w.equity_usd = float(w.balance_usd + mv)
                        s.commit()
                except Exception as e:
                    print(f"[wallet-sync] failed: {e}")

                # ---------- Open positions + LIVE holdings candles (every N sec) ----------
                if (LAST_OPEN_POS_UPDATE is None) or (
                    (now - LAST_OPEN_POS_UPDATE).total_seconds() >= OPEN_POS_UPDATE_SECONDS
                ):
                    dust_usd = float(os.environ.get("POSITION_DUST_USD", "1.00") or 1.00)

                    # DB open bases (skip dust)
                    db_pos = s.exec(select(Position).where(Position.status == "OPEN")).all()
                    db_bases = []
                    for p in db_pos:
                        last_px = get_last_price(s, p.symbol) or p.avg_price or 0.0
                        if (last_px * float(p.qty or 0.0)) >= dust_usd:
                            db_bases.append(p.symbol.upper())

                    # LIVE spot holdings (may fall back to DB if auth down)
                    HOLDINGS_DUST_USD = float(os.environ.get("HOLDINGS_DUST_USD", "1.00") or 1.00)
                    try:
                        live_bases = BROKER_HANDLE.list_spot_holdings_bases(s, min_usd=HOLDINGS_DUST_USD)
                    except Exception as e:
                        print(f"[loop] live holdings read failed: {e}")
                        live_bases = []

                    want_bases = sorted(set(db_bases) | set(live_bases))
                    if want_bases:
                        # ensure Kraken mapping rows exist
                        rows = s.exec(select(UniversePair).where(UniversePair.symbol.in_(want_bases))).all()
                        present = {r.symbol for r in rows}
                        missing = [sym for sym in want_bases if sym not in present]
                        if missing:
                            try:
                                ensured = await ensure_pairs_for(s, missing)
                                if ensured:
                                    print(f"[loop] Ensured Kraken mapping for open/LIVE positions: {ensured}")
                            except Exception as e:
                                print(f"[loop] ensure_pairs_for failed: {e}")

                        print(f"[loop] Updating candles for DB+LIVE bases: {want_bases}")
                        await update_candles_for(s, want_bases)

                    LAST_OPEN_POS_UPDATE = now

                # ---------- Active universe (optionally union live holdings) ----------
                active_syms = get_active_universe(s)
                try:
                    HOLDINGS_DUST_USD = float(os.environ.get("HOLDINGS_DUST_USD", "1.00") or 1.00)
                    live_bases = BROKER_HANDLE.list_spot_holdings_bases(s, min_usd=HOLDINGS_DUST_USD)
                    if live_bases:
                        active_syms = sorted(set(active_syms) | set(live_bases))
                except Exception as e:
                    print(f"[universe] merge holdings into active failed: {e}")

                print(f"[loop] Active symbols: {active_syms}")

                # ---------- Full-universe minute candles (5m cadence) ----------
                if (LAST_FULL_CANDLES_UPDATE is None) or (
                    (now - LAST_FULL_CANDLES_UPDATE).total_seconds() >= FULL_CANDLES_UPDATE_SECONDS
                ):
                    print("[loop] Updating candles (full universe)…")
                    await update_candles_for(s, active_syms)
                    LAST_FULL_CANDLES_UPDATE = now
                else:
                    secs_left = FULL_CANDLES_UPDATE_SECONDS - int((now - LAST_FULL_CANDLES_UPDATE).total_seconds())
                    if secs_left < 0:
                        secs_left = 0
                    print(
                        f"[loop] Skipping full-universe candles (next in ~{secs_left}s); "
                        f"open positions update every {OPEN_POS_UPDATE_SECONDS}s"
                    )

                # ---------- Reconcile & manage ----------
                reconcile_spot_positions(s)
                print("[loop] Managing positions…")
                mark_to_market_and_manage(s, broker=BROKER_HANDLE)

        except Exception as e:
            print(f"[loop] error: {e}")
            traceback.print_exc()

        wake = datetime.utcnow() + timedelta(seconds=POLL_SECONDS)
        print(f"[loop] Sleeping {POLL_SECONDS}s — next cycle at {wake:%Y-%m-%d %H:%M:%S}Z")
        await asyncio.sleep(POLL_SECONDS)
