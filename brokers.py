# brokers.py
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from time import time
from typing import List, Optional, Dict

import ccxt
from ccxt.base.errors import AuthenticationError, DDoSProtection, RateLimitExceeded
from sqlmodel import Session, select

from models import Wallet, Position, Trade, Order, Candle
from universe import UniversePair
from settings import (
    BROKER,
    KRAKEN_API_KEY, KRAKEN_API_SECRET,
    LIVE_MAX_ORDER_USD, LIVE_MIN_ORDER_USD,
    FEE_PCT, SLIPPAGE_PCT,
)

# ---------------- shared types ----------------
@dataclass
class Balance:
    cash_usd: float
    equity_usd: float


# ---------------- helpers ----------------
def _mid_from_ticker(t: dict) -> float:
    bid = float(t.get("bid") or 0.0)
    ask = float(t.get("ask") or 0.0)
    last = float(t.get("last") or 0.0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return last


# ---------------- SIM broker (kept minimal) ----------------
class SimBroker:
    name = "sim"
    paper = True
    live = False

    def __init__(self, engine):
        self.engine = engine

    def get_balance(self, session: Session = None) -> Balance:
        # Use wallet table as source of truth
        cash = 0.0
        mv = 0.0
        if session is not None:
            w = session.get(Wallet, 1)
            if w:
                cash = float(w.balance_usd or 0.0)
            # portfolio MV from DB
            try:
                from sim import _portfolio_market_value
                mv = float(_portfolio_market_value(session))
            except Exception:
                mv = 0.0
        return Balance(cash_usd=cash, equity_usd=cash + mv)


# ---------------- LIVE Kraken broker ----------------
class KrakenLiveBroker:
    name = "kraken-live"
    paper = False
    live = True

    def __init__(self, engine):
        self.engine = engine
        self.exch = ccxt.kraken({
            "apiKey": KRAKEN_API_KEY,
            "secret": KRAKEN_API_SECRET,
            "enableRateLimit": True,
        })
        try:
            self.exch.load_markets()
        except Exception as e:
            print(f"[broker] load_markets warning: {e}")

        # private-call backoff on auth errors
        self._auth_ok = True
        self._last_auth_fail = 0.0

        # balance cache for a few seconds when auth is OK
        self._bal_cache: Optional[dict] = None
        self._bal_cache_t = 0.0
        self._bal_ttl = 15  # seconds

    # -------- private-call guard --------
    def _can_call_private(self) -> bool:
        if self._auth_ok:
            return True
        # wait 5 minutes after an auth failure
        return (time() - self._last_auth_fail) > 300

    def _call_private(self, fn, *args, **kwargs):
        if not self._can_call_private():
            raise AuthenticationError("auth backoff active")
        try:
            return fn(*args, **kwargs)
        except AuthenticationError as e:
            # flip auth flag and start backoff
            self._auth_ok = False
            self._last_auth_fail = time()
            print(f"[broker] fetch_balance auth error: {e}")
            raise
        except (RateLimitExceeded, DDoSProtection) as e:
            print(f"[broker] private call rate-limited: {e}")
            raise

    # -------- balance --------
    def _fetch_balance_cached(self) -> dict:
        now = time()
        if self._bal_cache and (now - self._bal_cache_t) < self._bal_ttl:
            return self._bal_cache
        res = self._call_private(self.exch.fetch_balance)
        self._bal_cache = res or {}
        self._bal_cache_t = now
        return self._bal_cache

    def get_balance(self, session: Session = None) -> Balance:
        """
        Always returns Balance.
        - If private auth works: cash = Kraken USD free; equity = cash + DB MTM
        - If auth fails/backoff: cash = Wallet.balance_usd; equity = cash + DB MTM
        """
        mv = 0.0
        if session is not None:
            try:
                from sim import _portfolio_market_value
                mv = float(_portfolio_market_value(session))
            except Exception:
                mv = 0.0

        # try Kraken private balance if allowed
        if self._can_call_private():
            try:
                bal = self._fetch_balance_cached() or {}
                free = (bal.get("free") or {}) if isinstance(bal, dict) else {}
                cash = float(free.get("USD") or free.get("ZUSD") or 0.0)
                # if you want to count USDT as cash too, uncomment:
                # cash += float(free.get("USDT") or 0.0)
                return Balance(cash_usd=cash, equity_usd=cash + mv)
            except Exception:
                pass  # fall through

        # fallback to DB wallet cash
        cash = 0.0
        if session is not None:
            try:
                w = session.get(Wallet, 1)
                if w:
                    cash = float(w.balance_usd or 0.0)
            except Exception:
                pass
        return Balance(cash_usd=cash, equity_usd=cash + mv)

    # -------- live holdings (bases) --------
    def list_spot_holdings_bases(self, session: Session, min_usd: float = 1.0) -> List[str]:
        """
        Returns base symbols you meaningfully hold (USD valuation >= min_usd).
        Uses Kraken private balance when auth is available; else falls back to DB open positions.
        """
        bases = set()

        def _norm(a: str) -> str:
            a = (a or "").upper()
            if a in ("ZUSD", "USD"):
                return "USD"
            if a in ("XXBT", "XBT"):
                return "BTC"
            # strip leading X/Z Kraken prefixes (rough norm)
            return a.replace("X", "").replace("Z", "")

        markets = getattr(self.exch, "markets", {}) or {}

        if self._can_call_private():
            try:
                bal = self._fetch_balance_cached() or {}
                total = (bal.get("total") or {}) if isinstance(bal, dict) else {}
                for asset, qty in total.items():
                    q = float(qty or 0.0)
                    base = _norm(asset)
                    if base in ("USD", "USDT") or q <= 0.0:
                        continue

                    # try USD then USDT for valuation using PUBLIC ticker
                    px = 0.0
                    for qte in ("USD", "USDT"):
                        sym = f"{base}/{qte}"
                        if sym in markets:
                            try:
                                t = self.exch.fetch_ticker(sym)
                                px = _mid_from_ticker(t)
                                if px > 0:
                                    break
                            except Exception:
                                continue
                    val = q * px
                    if val >= min_usd:
                        bases.add(base)
            except Exception as e:
                print(f"[broker] list_spot_holdings failed: {e}")

        if not bases and session is not None:
            # fallback to DB open positions
            rows = session.exec(select(Position).where(Position.status == "OPEN")).all()
            for p in rows:
                try:
                    val = float(p.qty or 0.0) * float(p.avg_price or 0.0)
                    if val >= float(min_usd):
                        bases.add((p.symbol or "").upper())
                except Exception:
                    pass

        return sorted(bases)

    # -------- ccxt market helpers --------
    def pair_min_notional_usd(self, symbol: str) -> float:
        try:
            ksym = symbol if "/" in symbol else f"{symbol}/USD"
            markets = getattr(self.exch, "markets", None) or self.exch.load_markets()
            m = markets.get(ksym)
            if m:
                lim = (m.get("limits") or {}).get("cost") or {}
                mn = lim.get("min")
                if mn is not None:
                    return float(mn)
        except Exception:
            pass
        env = (
            os.environ.get("MIN_TRADE_NOTIONAL_USD")
            or os.environ.get("MIN_SELL_NOTIONAL_USD")
            or getattr(self, "min_notional_usd", None)
        )
        return float(env or 5.0)

    def _ccxt_symbol_for(self, session: Session, base: str) -> str:
        base2 = "BTC" if base.upper() in ("XBT", "BTC") else base.upper()
        # choose USD if available else USDT
        markets = getattr(self.exch, "markets", {}) or {}
        for q in ("USD", "USDT"):
            if f"{base2}/{q}" in markets:
                return f"{base2}/{q}"
        # fallback to UniversePair quote
        try:
            row = session.exec(select(UniversePair).where(UniversePair.symbol == base2)).first()
            if row and row.quote and f"{base2}/{row.quote}" in markets:
                return f"{base2}/{row.quote}"
        except Exception:
            pass
        return f"{base2}/USDT"

    # -------- (optional) order path retained, unchanged behavior --------
    def fetch_open_orders(self):
        try:
            ods = self.exch.fetch_open_orders()
            out = []
            for o in ods:
                ts = o.get("timestamp")
                iso = datetime.utcfromtimestamp(ts / 1000).isoformat() if ts else None
                out.append({
                    "time": iso,
                    "symbol": o.get("symbol"),
                    "side": (o.get("side") or "").upper(),
                    "price": float(o.get("price") or 0.0),
                    "status": (o.get("status") or "open").upper(),
                })
            return out
        except Exception as e:
            print(f"[broker] fetch_open_orders failed: {e}")
            return []

    def place_order(
        self,
        *,
        symbol: str,             # base symbol, e.g. "BTC"
        side: str,               # "BUY" / "SELL"
        qty: float,
        order_type: str,         # "market"
        price: float,
        reason: str,
        session: Session,
        score: Optional[float] = None,
    ):
        ccxt_symbol = self._ccxt_symbol_for(session, symbol)

        # sanity price
        last = price
        if not last or last <= 0:
            c = session.exec(select(Candle).where(Candle.symbol == symbol).order_by(Candle.ts.desc())).first()
            last = float(c.close) if c else None
        if not last or last <= 0:
            session.add(Order(
                ts=datetime.utcnow(), symbol=symbol, side=side, qty=0.0,
                price_req=price or 0.0, price_fill=0.0,
                status="REJECTED", reason="LIVE: no price"
            ))
            session.commit()
            return None

        # notional floor
        try:
            pair_min = float(self.pair_min_notional_usd(symbol))
        except Exception:
            pair_min = float(LIVE_MIN_ORDER_USD)
        if qty * last + 1e-12 < pair_min:
            need = pair_min / max(1e-12, last)
            qty = max(qty, need)

        # hard cap
        if qty * last > LIVE_MAX_ORDER_USD:
            qty = LIVE_MAX_ORDER_USD / last

        qty2 = float(self.exch.amount_to_precision(ccxt_symbol, qty))
        try:
            if order_type.lower() != "market":
                raise ValueError("Only market orders supported.")
            if side.upper() == "BUY":
                od = self.exch.create_market_buy_order(ccxt_symbol, qty2)
            else:
                od = self.exch.create_market_sell_order(ccxt_symbol, qty2)
        except Exception as e:
            print(f"[broker] create_order failed: {e}")
            session.add(Order(
                ts=datetime.utcnow(), symbol=symbol, side=side, qty=0.0,
                price_req=price or last, price_fill=0.0,
                status="REJECTED", reason=f"LIVE: {e}"
            ))
            session.commit()
            return None

        status  = (od.get("status") or "").lower() or "closed"
        filled  = float(od.get("filled") or qty2)
        average = float(od.get("average") or od.get("price") or last)

        session.add(Order(
            ts=datetime.utcnow(), symbol=symbol, side=side, qty=filled,
            price_req=price or last, price_fill=average,
            status=("FILLED" if status == "closed" else "PENDING"),
            reason=f"LIVE kraken {reason}"
        ))

        # crude wallet/position update to mirror fills
        if side.upper() == "BUY":
            p = session.exec(select(Position).where(Position.symbol == symbol, Position.status == "OPEN")).first()
            if p:
                total = float(p.qty or 0.0) + filled
                p.avg_price = (float(p.avg_price or 0.0) * float(p.qty or 0.0) + average * filled) / max(1e-12, total)
                p.qty = total
            else:
                # minimal safe stop/target
                fee_buf = FEE_PCT * 2.0
                slip_buf = SLIPPAGE_PCT * 2.0
                min_gap = max(0.006, 3.0 * (fee_buf + slip_buf))
                p = Position(
                    symbol=symbol,
                    qty=filled,
                    avg_price=average,
                    opened_ts=datetime.utcnow(),
                    stop=average * (1.0 - min_gap),
                    target=average * 1.05,
                    status="OPEN",
                    score=float(score) if score is not None else None,
                )
                session.add(p)
        else:
            p = session.exec(select(Position).where(Position.symbol == symbol, Position.status == "OPEN")).first()
            if p and filled > 0:
                close_qty = min(float(p.qty or 0.0), filled)
                pnl = (average - float(p.avg_price or 0.0)) * close_qty
                result = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
                session.add(Trade(
                    symbol=symbol,
                    entry_ts=p.opened_ts,
                    exit_ts=datetime.utcnow(),
                    entry_px=float(p.avg_price or average),
                    exit_px=average,
                    qty=close_qty,
                    pnl_usd=float(pnl),
                    result=result,
                ))
                p.qty = max(0.0, float(p.qty or 0.0) - close_qty)
                if p.qty <= 1e-12:
                    p.qty = 0.0
                    p.status = "CLOSED"

            # credit wallet with proceeds net of fee (approx)
            w = session.get(Wallet, 1)
            if w:
                proceeds = average * filled * (1 - FEE_PCT)
                w.balance_usd = float((w.balance_usd or 0.0) + proceeds)

        session.commit()
        return od


# -------- factory --------
def make_broker(engine):
    b = (BROKER or "sim").lower()
    if b in ("kraken-live", "kraken"):
        # fail early if keys missing
        if not KRAKEN_API_KEY or not KRAKEN_API_SECRET:
            raise RuntimeError("KRAKEN_API_KEY/KRAKEN_API_SECRET are required for kraken-live.")
        return KrakenLiveBroker(engine)
    elif b in ("sim",):
        return SimBroker(engine)
    # default to sim
    return SimBroker(engine)
