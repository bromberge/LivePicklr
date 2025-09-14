# settings.py
# Unified config + comprehensive legacy aliases so older modules keep working.
# All knobs come from .env; change behavior by editing .env only.
try:
    from dotenv import load_dotenv, find_dotenv
    _DOTENV_PATH = find_dotenv(usecwd=True)
    if _DOTENV_PATH:
        load_dotenv(_DOTENV_PATH, override=False)
        print(f"[settings] .env loaded: {_DOTENV_PATH}")
    else:
        print("[settings] no .env found via find_dotenv()")
except Exception as _e:
    print(f"[settings] dotenv load skipped: {_e}")

import os
from typing import List, Optional

def _get(name: str) -> Optional[str]:
    # Prefer UPPER, but accept lower; strip whitespace; treat "" as None
    v = os.getenv(name)
    if v is None:
        v = os.getenv(name.lower())
    if v is None:
        return None
    v2 = str(v).strip()
    return v2 if v2 != "" else None

def _b(name: str, default: str="0") -> bool:
    v = _get(name)
    if v is None:
        v = default
    v = str(v).strip().lower()
    return v in ("1","true","yes","on")

def _f(name: str, default: float) -> float:
    v = _get(name)
    try:
        return float(v) if v is not None else float(default)
    except:
        return float(default)

def _i(name: str, default: int) -> int:
    v = _get(name)
    try:
        return int(float(v)) if v is not None else int(default)
    except:
        return int(default)

def _s(name: str, default: str) -> str:
    v = _get(name)
    return v if v is not None else default

def _flist(name: str, default: str) -> List[float]:
    raw = _get(name)
    if raw is None:
        raw = default
    if not raw:
        return []
    out: List[float] = []
    for x in str(raw).split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.append(float(x))
        except:
            pass
    return out

def _slist(name: str, default: str) -> List[str]:
    raw = _get(name)
    if raw is None:
        raw = default
    if not raw:
        return []
    return [x.strip() for x in str(raw).split(",") if x.strip()]


# =============================
# CORE KNOBS (edit via .env)
# =============================

# Cadence / universe
POLL_SECONDS                = _i("POLL_SECONDS", 60)      # main loop tick
OPEN_POS_UPDATE_SECONDS     = _i("OPEN_POS_UPDATE_SECONDS", 60)
FULL_CANDLES_UPDATE_SECONDS = _i("FULL_CANDLES_UPDATE_SECONDS", 300)

UNIVERSE_REFRESH_MINUTES    = _i("UNIVERSE_REFRESH_MINUTES", 10)
UNIVERSE_CACHE_MINUTES      = _i("UNIVERSE_CACHE_MINUTES", 60)
UNIVERSE_TOP_N              = _i("UNIVERSE_TOP_N", 150)
ALLOWED_QUOTES              = _slist("ALLOWED_QUOTES", "USD,USDT")
MIN_VOLUME_USD              = _f("MIN_VOLUME_USD", 10_000_000.0)
UNIVERSE_EXCLUDE            = _slist("UNIVERSE_EXCLUDE", "")
UNIVERSE_INCLUDE            = _slist("UNIVERSE_INCLUDE", "")
DEFAULT_UNIVERSE            = _slist("DEFAULT_UNIVERSE", "BTC/USD,ETH/USD,SOL/USD,ADA/USD,LINK/USD")

MAX_OPEN_POSITIONS          = _i("MAX_OPEN_POSITIONS", 3)
MAX_NEW_POSITIONS_PER_CYCLE = _i("MAX_NEW_POSITIONS_PER_CYCLE", 2)

# Backfill / history windows
BACKFILL_DAYS               = _i("BACKFILL_DAYS", 365)
HISTORY_DAYS                = _i("HISTORY_DAYS", BACKFILL_DAYS)
HISTORY_MINUTES             = _i("HISTORY_MINUTES", 600)
DATA_FETCH_INTERVAL_SECONDS = _i("DATA_FETCH_INTERVAL_SECONDS", POLL_SECONDS)

# >>> New: knobs used by data.py hourly backfill <<<
BACKFILL_CONCURRENCY        = _i("BACKFILL_CONCURRENCY", 8)     # number of concurrent pairs
BACKFILL_PAUSE_MS           = _i("BACKFILL_PAUSE_MS", 200)      # polite pacing between pages
BACKFILL_MAX_PAIRS          = _i("BACKFILL_MAX_PAIRS", 0)       # 0 = no cap

# Fees / wallet / sizing / limits
FEE_PCT                    = _f("FEE_PCT", 0.004)     # exchange fee (one side)
SLIPPAGE_PCT               = _f("SLIPPAGE_PCT", 0.0008)
RISK_PCT_PER_TRADE         = _f("RISK_PCT_PER_TRADE", 0.02)
RISK_MAX_MULTIPLIER = _f("RISK_MAX_MULTIPLIER", 2.0)
POSITION_DUST_USD          = _f("POSITION_DUST_USD", 1.00)

WALLET_START_USD           = _f("WALLET_START_USD", 1000.0)

MAX_TRADE_COST_PCT         = _f("MAX_TRADE_COST_PCT", 0.15)
MAX_GROSS_EXPOSURE_PCT     = _f("MAX_GROSS_EXPOSURE_PCT", 1.00)

MIN_TRADE_NOTIONAL         = _f("MIN_TRADE_NOTIONAL", 15.0)
MIN_TRADE_NOTIONAL_PCT     = _f("MIN_TRADE_NOTIONAL_PCT", 0.0)
MIN_BREAKEVEN_EDGE_PCT     = _f("MIN_BREAKEVEN_EDGE_PCT", 0.0)

MIN_TRADE_NOTIONAL_USD     = _f("MIN_TRADE_NOTIONAL_USD", MIN_TRADE_NOTIONAL)
MIN_SELL_NOTIONAL_USD      = _f("MIN_SELL_NOTIONAL_USD", MIN_TRADE_NOTIONAL_USD)



# Defaults if ATR-based entry stops are unavailable
TARGET_PCT                 = _f("TARGET_PCT", 0.045)
STOP_PCT                   = _f("STOP_PCT", 0.025)

# Signals / chooser / model
DET_EMA_SHORT        = _i("DET_EMA_SHORT", 12)
DET_EMA_LONG         = _i("DET_EMA_LONG", 26)
EMA_SLOPE_MIN        = _f("EMA_SLOPE_MIN", 0.0)
MIN_EMA_SPREAD       = _f("MIN_EMA_SPREAD", 0.0005)
REQUIRE_BREAKOUT     = _b("REQUIRE_BREAKOUT", "0")
MIN_BREAKOUT_PCT     = _f("MIN_BREAKOUT_PCT", 0.002)
BREAKOUT_LOOKBACK    = _i("BREAKOUT_LOOKBACK", 50)
EMA_SLOPE_LOOKBACK   = _i("EMA_SLOPE_LOOKBACK", 8)

CHOOSER_THRESHOLD    = _f("CHOOSER_THRESHOLD", 0.25)
SCORE_THRESHOLD      = _f("SCORE_THRESHOLD", 0.15)

USE_MODEL            = _b("USE_MODEL", "1")
MODEL_PATH           = _s("MODEL_PATH", "")

HORIZON_HOURS        = _i("HORIZON_HOURS", 96)
FEATURE_DAYS         = _i("FEATURE_DAYS", 90)
ENABLE_DEBUG_SIGNALS = _b("ENABLE_DEBUG_SIGNALS", "0")

# Risk / ATR / stops
USE_ATR_STOPS   = _b("USE_ATR_STOPS", "1")
ATR_LEN         = _i("ATR_LEN", 14)
ATR_STOP_MULT   = _f("ATR_STOP_MULT", 1.8)
ATR_TARGET_MULT = _f("ATR_TARGET_MULT", 4.5)
MIN_STOP_PCT    = _f("MIN_STOP_PCT", 0.025)
MIN_RR          = _f("MIN_RR", 1.8)

# Profit protection (PTP + BE)
PTP_LEVELS         = _flist("PTP_LEVELS", "0.037,0.08")
PTP_SIZES          = _flist("PTP_SIZES",  "0.30,0.30")

# BE controls (clarified in Q&A)
BE_TRIGGER_PCT     = _f("BE_TRIGGER_PCT", 0.0)           # optional % gain trigger
BE_AFTER_FIRST_TP  = _b("BE_AFTER_FIRST_TP", "1")        # set BE after TP1

# Convenience: TP1_BE_ENABLE maps to BE_AFTER_FIRST_TP
TP1_BE_ENABLE      = _b("TP1_BE_ENABLE", "1")
TP1_BE_OFFSET_PCT  = _f("TP1_BE_OFFSET_PCT", 0.0)
if TP1_BE_ENABLE:
    BE_AFTER_FIRST_TP = True

# Trailing stop logic (.env knobbed)
TSL_MODE               = _s("TSL_MODE", "")              # "", "fixed", or "atr"
if "TSL_USE_ATR" in os.environ:
    TSL_USE_ATR = _b("TSL_USE_ATR", os.environ.get("TSL_USE_ATR", "0"))
else:
    TSL_USE_ATR = (TSL_MODE.strip().lower() == "atr")
TSL_PCT               = _f("TSL_PCT", 0.04)
TSL_ATR_MULT          = _f("TSL_ATR_MULT", 3.0)
TSL_TIGHTEN_MULT      = _f("TSL_TIGHTEN_MULT", 0.7)
TSL_ACTIVATE_AFTER_TP = _b("TSL_ACTIVATE_AFTER_TP", "0")
TSL_ACTIVATE_PCT      = _f("TSL_ACTIVATE_PCT", 9.99)     # dormant unless used
TSL_ACTIVATE_GAIN_PCT = _f("TSL_ACTIVATE_GAIN_PCT", 0.10)

# Live exchange min/max order guidance (used for partial TP upsizing)
LIVE_MIN_ORDER_USD    = _f("LIVE_MIN_ORDER_USD", 15.0)
LIVE_MAX_ORDER_USD    = _f("LIVE_MAX_ORDER_USD", 40.0)

# Data ingestion / APIs (Coingecko / Kraken)
USE_COINGECKO                  = _b("USE_COINGECKO", "0")
COINGECKO_API_KEY              = _s("COINGECKO_API_KEY", "")
COINGECKO_BASE_URL             = _s("COINGECKO_BASE_URL", "https://pro-api.coingecko.com/api/v3")
COINGECKO_VS_CURRENCY          = _s("COINGECKO_VS_CURRENCY", "usd")
COINGECKO_SLEEP_SECONDS        = _f("COINGECKO_SLEEP_SECONDS", 1.2)
COINGECKO_MAX_IDS_PER_CALL     = _i("COINGECKO_MAX_IDS_PER_CALL", 100)
COINGECKO_TIMEOUT_SECONDS      = _f("COINGECKO_TIMEOUT_SECONDS", 30.0)
COINGECKO_RATE_LIMIT_PER_SEC   = _f("COINGECKO_RATE_LIMIT_PER_SEC", 5.0)

USE_KRAKEN                     = _b("USE_KRAKEN", "1")
KRAKEN_API_KEY                 = _s("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET              = _s("KRAKEN_API_SECRET", "")
KRAKEN_SANDBOX                 = _b("KRAKEN_SANDBOX", "0")
KRAKEN_TIMEFRAME               = _s("KRAKEN_TIMEFRAME", "1m")
KRAKEN_MAX_SYMBOLS_PER_CALL    = _i("KRAKEN_MAX_SYMBOLS_PER_CALL", 20)
KRAKEN_SLEEP_SECONDS           = _f("KRAKEN_SLEEP_SECONDS", 1.0)
KRAKEN_RATE_LIMIT_PER_SEC      = _f("KRAKEN_RATE_LIMIT_PER_SEC", 5.0)
USE_SPOT_ONLY                  = _b("USE_SPOT_ONLY", "1")
BROKER                = _s("BROKER", "kraken-live")

# Kraken private-endpoint hygiene
KRAKEN_PRIVATE_TTL_SECONDS   = _i("KRAKEN_PRIVATE_TTL_SECONDS", 20)  # serve cache for this long
KRAKEN_ERROR_BACKOFF_SECONDS = _i("KRAKEN_ERROR_BACKOFF_SECONDS", 5) # wait after an error


# Misc
COOLDOWN_MINUTES   = _i("COOLDOWN_MINUTES", 30)
MAX_HOLD_MINUTES   = _i("MAX_HOLD_MINUTES", 0)
VERBOSE_TP_LOGS    = _b("VERBOSE_TP_LOGS", "0")
VERBOSE_HTTP_LOGS  = _b("VERBOSE_HTTP_LOGS", "0")

# =============================
# LEGACY-COMPAT ALIASES
# =============================

# Universe aliases
UNIVERSE                 = DEFAULT_UNIVERSE
UNIVERSE_WHITELIST       = UNIVERSE_INCLUDE
UNIVERSE_BLACKLIST       = UNIVERSE_EXCLUDE

# Backfill / history aliases frequently used by data.py variants
BACKFILL_DAYS_DEFAULT    = BACKFILL_DAYS
BACKFILL_HOURS           = _i("BACKFILL_HOURS", BACKFILL_DAYS * 24)
BACKFILL_MINUTES         = _i("BACKFILL_MINUTES", BACKFILL_HOURS * 60)
HISTORY_MINUTES_DEFAULT  = HISTORY_MINUTES
HISTORY_WINDOW_MINUTES   = HISTORY_MINUTES
FETCH_LOOKBACK_MIN       = _i("FETCH_LOOKBACK_MIN", HISTORY_MINUTES)

# Coingecko aliases
USE_CG                   = USE_COINGECKO
CG_API_KEY               = COINGECKO_API_KEY
CG_BASE_URL              = COINGECKO_BASE_URL
CG_VS_CURRENCY           = COINGECKO_VS_CURRENCY
CG_RATE_LIMIT_PER_SEC    = COINGECKO_RATE_LIMIT_PER_SEC
CG_TIMEOUT_SECONDS       = COINGECKO_TIMEOUT_SECONDS

# Kraken aliases
KRAKEN_PUBLIC_KEY        = KRAKEN_API_KEY
KRAKEN_PRIVATE_KEY       = KRAKEN_API_SECRET
KRAKEN_PAPER             = KRAKEN_SANDBOX
PAPER_TRADING            = KRAKEN_SANDBOX
PAPER                    = KRAKEN_SANDBOX
EXCHANGE_TIMEFRAME       = KRAKEN_TIMEFRAME

# Cadence aliases
TICK_SECONDS             = POLL_SECONDS
CANDLES_UPDATE_SECONDS   = FULL_CANDLES_UPDATE_SECONDS

# Sizing / limits aliases
TRADE_FEE_PCT            = FEE_PCT
SLIP_PCT                 = SLIPPAGE_PCT
RISK_PER_TRADE_PCT       = RISK_PCT_PER_TRADE
MIN_NOTIONAL_USD         = MIN_TRADE_NOTIONAL

# Profit-protection / trailing aliases
TP1_LEVEL_PCT            = PTP_LEVELS[0] if len(PTP_LEVELS) > 0 else 0.037
TP2_LEVEL_PCT            = PTP_LEVELS[1] if len(PTP_LEVELS) > 1 else 0.08
TP1_SIZE_PCT             = PTP_SIZES[0] if len(PTP_SIZES) > 0 else 0.30
TP2_SIZE_PCT             = PTP_SIZES[1] if len(PTP_SIZES) > 1 else 0.30

TSL_FIXED_PCT            = TSL_PCT
TSL_ATR_MULTIPLIER       = TSL_ATR_MULT
TSL_TIGHTEN_FACTOR       = TSL_TIGHTEN_MULT
TSL_ENABLE_AFTER_TP      = TSL_ACTIVATE_AFTER_TP
TSL_ENABLE_AT_GAIN_PCT   = TSL_ACTIVATE_GAIN_PCT

# Default stop/target aliases
DEFAULT_TARGET_PCT       = TARGET_PCT
DEFAULT_STOP_PCT         = STOP_PCT

# Wallet alias
STARTING_BALANCE_USD     = WALLET_START_USD

# --- Kraken private-call guardrails ---
KRAKEN_PRIVATE_MIN_INTERVAL_MS = _i("KRAKEN_PRIVATE_MIN_INTERVAL_MS", 1200)  # >= 1100ms
KRAKEN_PRIVATE_BACKOFF_MS_BASE = _i("KRAKEN_PRIVATE_BACKOFF_MS_BASE", 500)   # first backoff
KRAKEN_PRIVATE_BACKOFF_MS_MAX  = _i("KRAKEN_PRIVATE_BACKOFF_MS_MAX", 4000)  # cap
KRAKEN_PRIVATE_CACHE_TTL_SEC   = _i("KRAKEN_PRIVATE_CACHE_TTL_SEC", 30)     # balance cache
