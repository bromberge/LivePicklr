# sim.py — unified with profit_protection.manage_position
from datetime import timedelta, datetime, timezone
from typing import Optional, List
import os

from sqlmodel import Session, select

import models as M
from models import Wallet

# --------------------------
# Settings
# --------------------------
import settings as S

# Some callers relied on WALLET_START_USD being in settings; keep a safe fallback here.
try:
    WALLET_START_USD = float(os.getenv("WALLET_START_USD", "1000") or 1000.0)
except Exception:
    WALLET_START_USD = 1000.0

EPSILON = 1e-9


def _utcnow() -> datetime:
    # store naive-UTC in DB (consistent with your DB)
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _vlog(msg: str) -> None:
    if S.VERBOSE_TP_LOGS:
        try:
            print(msg)
        except Exception:
            pass


# --------------------------
# Wallet helpers
# --------------------------
def ensure_wallet(session: Session) -> Wallet:
    """
    Get the first wallet row; create it if missing.
    Always returns a Wallet instance.
    """
    w = session.exec(select(Wallet).order_by(Wallet.id.asc())).first()
    if not w:
        w = Wallet(
            balance_usd=WALLET_START_USD,
            equity_usd=WALLET_START_USD,
            updated_at=_utcnow(),
        )
        session.add(w)
        session.commit()
        session.refresh(w)
        print(f"[startup] Created wallet with ${WALLET_START_USD:,.2f}")
    return w


# Backward-compatible alias used elsewhere in the app
def get_or_create_wallet(session: Session) -> Wallet:
    return ensure_wallet(session)


def _sanitize_wallet(w: M.Wallet) -> None:
    if abs(w.balance_usd) < EPSILON:
        w.balance_usd = 0.0
    if abs(w.equity_usd) < EPSILON:
        w.equity_usd = 0.0
    w.balance_usd = float(round(w.balance_usd, 8))
    w.equity_usd = float(round(w.equity_usd, 8))


def _portfolio_market_value(session: Session) -> float:
    total = 0.0
    opens: List[M.Position] = session.exec(
        select(M.Position).where(M.Position.status == "OPEN")
    ).all()
    for p in opens:
        px = get_last_price(session, p.symbol)
        if px is not None:
            total += px * float(p.qty or 0.0)
    return total


def _recalc_equity(session: Session, w: Optional[M.Wallet]) -> None:
    # Be defensive—if caller passed None, make sure we have a wallet row
    if w is None:
        w = ensure_wallet(session)

    mv = _portfolio_market_value(session)
    # defensive in case balance_usd was None at some point
    w.equity_usd = (w.balance_usd or 0.0) + mv
    w.updated_at = _utcnow()
    _sanitize_wallet(w)


# --------------------------
# Price helpers
# --------------------------
def get_last_price(session: Session, symbol: str) -> Optional[float]:
    c = session.exec(
        select(M.Candle)
        .where(M.Candle.symbol == symbol)
        .order_by(M.Candle.ts.desc())
    ).first()
    return (float(c.close) if c and (c.close is not None) else None)


def _prefer_kraken_mid(session: Session, symbol: str, broker) -> tuple[Optional[float], str]:
    """
    Returns (price, source_tag). If a live kraken broker is present, use mid=(bid+ask)/2.
    Fallback to latest candle close.
    """
    try:
        if broker and getattr(broker, "live", False) and hasattr(broker, "exch"):
            # Try broker's symbol mapper if available
            ccxt_sym = None
            _map = getattr(broker, "_ccxt_symbol_for", None)
            if callable(_map):
                ccxt_sym = _map(session, symbol)
            else:
                # naive guess
                ccxt_sym = symbol if "/" in symbol else f"{symbol}/USD"

            t = broker.exch.fetch_ticker(ccxt_sym)
            bid = float(t.get("bid") or 0) or None
            ask = float(t.get("ask") or 0) or None
            last = float(t.get("last") or 0) or None
            if bid and ask:
                return ((bid + ask) / 2.0, "kraken_mid")
            if last:
                return (last, "kraken_last")
    except Exception:
        pass

    # Fallback: candle close
    return (get_last_price(session, symbol), "candle")


# --------------------------
# Position sizing (risk-based)
# --------------------------
def risk_size_position(equity_usd: float, entry: float, stop: float,
                       risk_pct: float = S.RISK_PCT_PER_TRADE) -> float:
    """
    Risk a fixed % of equity per trade. Position size = risk_usd / per-unit-loss.
    """
    if entry <= 0 or stop <= 0 or entry <= stop:
        return 0.0
    risk_usd = max(0.0, equity_usd) * max(0.0, risk_pct)
    per_unit_loss = entry - stop
    if per_unit_loss <= 0:
        return 0.0
    qty = risk_usd / per_unit_loss
    return max(0.0, qty)


# --------------------------
# Order / Trade bookkeeping (paper mode)
# --------------------------
def _book_order(session: Session, symbol: str, side: str, qty: float,
                price_req: float, price_fill: float, status: str, note: str):
    session.add(M.Order(
        ts=_utcnow(), symbol=symbol, side=side, qty=qty,
        price_req=price_req, price_fill=price_fill,
        status=status, reason=note
    ))


def _add_trade(session: Session, symbol: str, entry_ts: datetime, entry_px: float,
               exit_px: float, qty: float):
    pnl = (exit_px - entry_px) * qty
    result = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "EVEN"
    session.add(M.Trade(
        symbol=symbol, entry_ts=entry_ts, exit_ts=_utcnow(),
        entry_px=entry_px, exit_px=exit_px, qty=qty,
        pnl_usd=float(pnl), result=result
    ))


def _partial_close(session: Session, p: M.Position, qty: float, exit_px: float, reason: str):
    if qty <= 0 or qty > p.qty:
        return

    w = ensure_wallet(session)

    fill_px = exit_px * (1 - S.SLIPPAGE_PCT)
    fee = fill_px * qty * S.FEE_PCT
    proceeds = fill_px * qty - fee

    w.balance_usd += proceeds
    _add_trade(session, p.symbol, p.opened_ts, p.avg_price, fill_px, qty)
    _book_order(session, p.symbol, "SELL", qty, p.avg_price, fill_px, "FILLED", reason)

    p.qty -= qty
    if p.qty <= EPSILON:
        p.qty = 0.0
        p.status = "CLOSED"

    _recalc_equity(session, w)
    session.commit()


def _close_position(session: Session, p: M.Position, exit_px: float, reason: str):
    if p.qty <= 0:
        return
    _partial_close(session, p, p.qty, exit_px, reason)


# --------------------------
# Entry
# --------------------------
def place_buy(
    session: Session,
    symbol: str,
    qty: float,
    entry: float,
    reason: str,
    stop: Optional[float] = None,
    target: Optional[float] = None,
    score: Optional[float] = None,
):
    """
    Simulated BUY:
      - executes at (entry * (1+slippage)) in paper mode
      - applies default stop/target if not provided
      - enforces LIVE_MIN_ORDER_USD notional minimum for “realistic” partials later
      - initializes Position with unified TP/BE/TSL state handled elsewhere
    """
    if qty <= 0 or entry <= 0:
        return

    w = ensure_wallet(session)
    _recalc_equity(session, w)

    last_px = get_last_price(session, symbol) or entry
    fill_px = last_px * (1 + S.SLIPPAGE_PCT)

    # Default stop/target if not provided:
    # Hard floor: not tighter than MIN_STOP_PCT from entry
    if stop is None:
        # If you later want ATR-based entry stop, wire it here;
        # for now: respect MIN_STOP_PCT as floor
        stop = entry * (1.0 - max(S.MIN_STOP_PCT, 1e-6))
    if target is None:
        # Use PTP first level as a minimum “expectation” if present; fallback to +2× stop gap
        gap = (entry - stop)
        tgt_from_gap = entry + (2.0 * gap)
        if S.PTP_LEVELS:
            target = max(tgt_from_gap, entry * (1.0 + S.PTP_LEVELS[0]))
        else:
            target = tgt_from_gap

    # Notional floor (paper guard so partials can be realistic)
    notional = qty * fill_px
    min_notional = float(S.LIVE_MIN_ORDER_USD)
    if notional + 1e-12 < min_notional:
        _book_order(session, symbol, "BUY", 0.0, entry, fill_px, "REJECTED",
                    f"Notional too small: {notional:.2f} < min {min_notional:.2f}")
        session.commit()
        return

    # ---- Execute buy (paper) ----
    fee = fill_px * qty * S.FEE_PCT
    cost = fill_px * qty + fee
    w.balance_usd -= cost

    _book_order(session, symbol, "BUY", qty, entry, fill_px, "FILLED",
                f"{reason} | cost={cost:.6f}")

    # Open or add position
    p = session.exec(
        select(M.Position).where(M.Position.symbol == symbol, M.Position.status == "OPEN")
    ).first()

    if p:
        # average in
        total_qty = p.qty + qty
        if total_qty > 0:
            p.avg_price = (p.avg_price * p.qty + fill_px * qty) / total_qty
            p.qty = total_qty
        # keep existing stop/target/flags, but never loosen existing stop
        p.stop = max(float(p.stop or 0.0), float(stop or 0.0))
        if target is not None:
            p.target = float(target)
    else:
        p = M.Position(
            symbol=symbol,
            qty=qty,
            avg_price=fill_px,
            opened_ts=_utcnow(),
            stop=float(stop),
            target=float(target),
            status="OPEN",
            tp1_done=False,
            tp2_done=False,
            be_moved=False,
            tsl_active=False,
            tsl_high=None,
        )
        # TP1 absolute level (for dashboard)
        if S.PTP_LEVELS:
            p.tp1_price = fill_px * (1 + S.PTP_LEVELS[0])

        # Store model score as confidence if that column exists; fall back to .score
        if isinstance(score, (int, float)):
            if hasattr(p, "confidence"):
                p.confidence = float(score)
            else:
                try:
                    p.score = float(score)
                except Exception:
                    pass

        session.add(p)

    # keep stats fresh
    refresh_position_stats(session, p)

    _recalc_equity(session, w)
    session.commit()


# --------------------------
# Live stats / UI helpers
# --------------------------
def refresh_position_stats(session: Session, p: M.Position):
    last_px = get_last_price(session, p.symbol)
    if last_px is None:
        return

    p.current_px = last_px
    try:
        p.pl_usd = (last_px - p.avg_price) * p.qty
        p.pl_pct = ((last_px / p.avg_price) - 1.0) * 100.0
    except Exception:
        p.pl_usd = 0.0
        p.pl_pct = 0.0

    # tp1 level (store once)
    if getattr(p, "tp1_price", None) is None and S.PTP_LEVELS:
        p.tp1_price = p.avg_price * (1 + S.PTP_LEVELS[0])

    # break-even price (only after BE is moved; managed in profit_protection)
    p.be_price = p.avg_price if p.be_moved else None

    # trailing stop “visible” price if active (mapped to current stop by manager)
    p.tsl_price = p.stop if p.tsl_active else None

    # time in trade (minutes)
    if p.opened_ts:
        delta = _utcnow() - p.opened_ts
        p.time_in_trade_min = int(delta.total_seconds() // 60)


# --------------------------
# MTM + full position management
# --------------------------
def mark_to_market_and_manage(session: Session, broker=None):
    """
    Every cycle:
      1) Refresh price & live stats
      2) Delegate TP1/TP2/BE/TSL to profit_protection.manage_position
      3) Enforce hard-stop exit (if price <= stop)
      4) Enforce time-based exit (if MAX_HOLD_MINUTES > 0)
    """
    from profit_protection import manage_position as _pp_manage

    w = ensure_wallet(session)

    opens: List[M.Position] = session.exec(
        select(M.Position).where(M.Position.status == "OPEN")
    ).all()

    for p in opens:
        # Prefer Kraken mid for decisioning; fallback to last candle
        last_px, src = _prefer_kraken_mid(session, p.symbol, broker)
        if last_px is None:
            last_px = get_last_price(session, p.symbol)
            src = src or "candle"

        if last_px is None:
            # no price this cycle
            continue

        # Dust guard
        try:
            dust = float(os.getenv("POSITION_DUST_USD", "1.00") or 1.00)
        except Exception:
            dust = 1.00
        if last_px * float(p.qty or 0.0) < dust:
            continue

        # Update live stats for dashboard
        p.current_px = last_px
        p.pl_usd = (last_px - p.avg_price) * p.qty
        p.pl_pct = ((last_px / p.avg_price) - 1.0) * 100.0 if p.avg_price > 0 else None

        # Delegate TP/BE/TSL to unified manager
        _pp_manage(session, broker, p)

        # Hard stop/TSL hit -> market out
        if p.status == "OPEN" and (p.qty or 0.0) > 0 and (p.stop or 0.0) > 0 and last_px <= float(p.stop):
            if broker and getattr(broker, "live", False):
                try:
                    broker.place_order(
                        symbol=p.symbol, side="SELL", qty=p.qty,
                        order_type="market", price=float(p.stop),
                        reason="STOP/TSL", session=session,
                    )
                except Exception as e:
                    _vlog(f"[stop] broker sell failed for {p.symbol}: {e}")
            else:
                _close_position(session, p, float(p.stop), "STOP/TSL")
            # continue to next position
            continue

        # Time-based exit (optional)
        if S.MAX_HOLD_MINUTES and (S.MAX_HOLD_MINUTES > 0) and p.status == "OPEN" and p.opened_ts:
            age = (_utcnow() - p.opened_ts)
            if age >= timedelta(minutes=int(S.MAX_HOLD_MINUTES)):
                if broker and getattr(broker, "live", False):
                    try:
                        broker.place_order(
                            symbol=p.symbol, side="SELL", qty=p.qty,
                            order_type="market", price=last_px,
                            reason="TIME", session=session,
                        )
                    except Exception as e:
                        _vlog(f"[time] broker sell failed for {p.symbol}: {e}")
                else:
                    _close_position(session, p, last_px, "TIME")
                continue

    # Refresh stats and equity once per cycle
    opens2: List[M.Position] = session.exec(
        select(M.Position).where(M.Position.status == "OPEN")
    ).all()
    for p2 in opens2:
        refresh_position_stats(session, p2)

    _recalc_equity(session, w)
    session.commit()


# --------------------------
# Account guard used by chooser / loop
# --------------------------
def can_open_new_position(session: Session) -> bool:
    dust_usd = float(os.getenv("POSITION_DUST_USD", "1.00") or 1.00)

    opens: List[M.Position] = session.exec(
        select(M.Position).where(M.Position.status == "OPEN")
    ).all()

    count_effective = 0
    for p in opens:
        last = get_last_price(session, p.symbol) or p.avg_price or 0.0
        if last * float(p.qty or 0.0) >= dust_usd:
            count_effective += 1

    return count_effective < int(S.MAX_OPEN_POSITIONS)
