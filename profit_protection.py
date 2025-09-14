from datetime import datetime, timedelta
from sqlmodel import Session, select
import settings as S

from models import Position, Order
from sim import get_last_price

def _min_notional_qty(price: float) -> float:
    if price <= 0: return 0.0
    return S.LIVE_MIN_ORDER_USD / price

def _place_sell(broker, session: Session, symbol: str, qty: float, price: float, reason: str):
    if qty <= 0: return None
    res = broker.place_order(
        symbol=symbol,
        side="SELL",
        qty=qty,
        order_type="market",
        price=price,
        reason=reason,
        session=session,
    )
    session.commit()
    return res

def _find_recent_sell(session: Session, sym: str, since_ts: datetime):
    return session.exec(
        select(Order)
        .where(
            Order.symbol == sym,
            Order.side == "SELL",
            Order.status.in_(["FILLED","PENDING"]),
            Order.ts >= since_ts
        )
        .order_by(Order.id.desc())
    ).first()

def manage_position(session: Session, broker, p: Position):
    """
    Unified TP1/TP2/BE/TSL logic.
    - TP1/TP2 thresholds/sizes from S.PTP_LEVELS / S.PTP_SIZES
    - Move BE on TP1 if TP1_BE_ENABLE (offset optional)
    - Activate TSL when gain >= TSL_ACTIVATE_GAIN_PCT
    - Fixed trailing by % (TSL_MODE=fixed) or ATR-driven (TSL_MODE=atr) using MIN_STOP_PCT as base size proxy
    - Hard stop floor never tighter than MIN_STOP_PCT from entry
    """
    sym = p.symbol
    entry = float(p.avg_price or 0.0)
    qty   = float(p.qty or 0.0)
    if entry <= 0 or qty <= 0 or not sym:
        return

    last = get_last_price(session, sym) or entry
    gain = (last / entry) - 1.0

    # --- TP1 ---
    if len(S.PTP_LEVELS) >= 1 and len(S.PTP_SIZES) >= 1 and not getattr(p, "tp1_done", False):
        if gain >= S.PTP_LEVELS[0]:
            chunk = qty * S.PTP_SIZES[0]
            chunk = max(chunk, _min_notional_qty(last))
            chunk = min(chunk, qty)
            if chunk * last >= S.LIVE_MIN_ORDER_USD - 1e-9:
                reason = f"TP1 @{gain*100:.2f}%"
                _place_sell(broker, session, sym, chunk, last, reason)
                # confirm real order exists before flipping flags
                placed = _find_recent_sell(session, sym, p.opened_ts or (datetime.utcnow() - timedelta(days=3)))
                if placed:
                    p.tp1_done = True
                    p.tp1_price = last
                    if S.TP1_BE_ENABLE:
                        p.be_price = entry * (1.0 + S.TP1_BE_OFFSET_PCT)
                        p.be_moved = 1
                    # raise hard floor to at least MIN_STOP_PCT from entry
                    floor = entry * (1.0 - S.MIN_STOP_PCT)
                    if (p.stop or 0.0) < floor:
                        p.stop = floor
                    session.commit()

    # --- TP2 ---
    if len(S.PTP_LEVELS) >= 2 and len(S.PTP_SIZES) >= 2 and not getattr(p, "tp2_done", False):
        if gain >= S.PTP_LEVELS[1]:
            chunk = qty * S.PTP_SIZES[1]
            chunk = max(chunk, _min_notional_qty(last))
            chunk = min(chunk, qty)
            if chunk * last >= S.LIVE_MIN_ORDER_USD - 1e-9:
                reason = f"TP2 @{gain*100:.2f}%"
                _place_sell(broker, session, sym, chunk, last, reason)
                placed = _find_recent_sell(session, sym, p.opened_ts or (datetime.utcnow() - timedelta(days=3)))
                if placed:
                    p.tp2_done = True
                    # ensure stop not below BE and not below MIN_STOP_PCT floor
                    be = float(getattr(p, "be_price", 0.0) or entry)
                    floor = max(be, entry * (1.0 - S.MIN_STOP_PCT))
                    if (p.stop or 0.0) < floor:
                        p.stop = floor
                    session.commit()

    # --- Trailing (TSL) ---
    if gain >= S.TSL_ACTIVATE_GAIN_PCT:
        p.tsl_active = True
        p.tsl_high = max(float(getattr(p, "tsl_high", 0.0) or 0.0), last)

        if S.TSL_MODE == "fixed":
            # fixed % trail off current high
            new_trail = p.tsl_high * (1.0 - S.TSL_PCT)
        else:
            # ATR-mode: approximate using MIN_STOP_PCT as unit; multiply then tighten
            effective_pct = max(S.MIN_STOP_PCT * S.TSL_ATR_MULT * S.TSL_TIGHTEN_MULT, S.MIN_STOP_PCT)
            new_trail = p.tsl_high * (1.0 - effective_pct)

        # never lower the stop
        if (p.stop or 0.0) < new_trail:
            p.stop = new_trail

        session.commit()
