"""Bob's "Algo Market Structure" on one timeframe, as described in his videos.

Rules (videos 'Voici comment placer la Structure de Marché' and 'APPRENDS la STRUCTURE'):
- Points alternate L / H. The "fourchette" is the range [last L, last H].
- When price breaks the last H (a wick is enough), wait for the first bearish candle;
  the new H is the highest point reached since the break (up to and including that bearish candle).
- If the two last points are then H, H, insert the missing L = lowest point strictly between them
  (intrabar order from candle colour: bullish candle = low then high, bearish = high then low).
- Symmetric for breaks of the last L (wait for a bullish candle, insert the missing H).
- Neutral candles (open == close) take the colour of the previous candle (as in the Pine code).

Output: list of structure points with the bar index where they become KNOWN (confirm index),
so that any later logic only uses information available at that time (no look-ahead).
"""
from dataclasses import dataclass
import numpy as np


@dataclass
class Point:
    kind: str        # 'H' or 'L'
    price: float
    idx: int         # bar index where the extreme occurred
    known: int       # bar index (close) at which the point is known
    is_break: bool   # True = created by a structure break (HH or LL), False = inserted point
    brk: int = -1    # bar index where the previous point was first broken (break points only)


def candle_colors(o, c):
    col = np.where(c > o, 1, np.where(c < o, -1, 0))
    for i in range(len(col)):
        if col[i] == 0:
            col[i] = col[i - 1] if i > 0 and col[i - 1] != 0 else 1
    return col


def build_structure(o, h, l, c, eq_break=False):
    n = len(o)
    col = candle_colors(o, c)
    pts = []
    # init with first bar: order of extremes by colour
    if col[0] == 1:
        pts.append(Point('L', l[0], 0, 0, False)); pts.append(Point('H', h[0], 0, 0, False))
    else:
        pts.append(Point('H', h[0], 0, 0, False)); pts.append(Point('L', l[0], 0, 0, False))

    def last(kind):
        for p in reversed(pts):
            if p.kind == kind:
                return p

    pend_up = None    # [max_high, idx]
    pend_dn = None    # [min_low, idx]

    def lowest_between(i0, i1):
        """lowest low strictly after extreme at bar i0 (a high) and before extreme at bar i1 (a high)."""
        best, bi = np.inf, None
        for j in range(i0, i1 + 1):
            if j == i0 and i0 == i1:
                # both highs on same bar: impossible to have a low between
                continue
            if j == i0 and col[j] == 1:   # bullish: low happened before the high -> not between
                continue
            if j == i1 and col[j] == -1:  # bearish: low after the high -> not between
                continue
            if l[j] < best:
                best, bi = l[j], j
        return best, bi

    def highest_between(i0, i1):
        best, bi = -np.inf, None
        for j in range(i0, i1 + 1):
            if j == i0 and i0 == i1:
                continue
            if j == i0 and col[j] == -1:  # bearish: high before low -> not between
                continue
            if j == i1 and col[j] == 1:   # bullish: high after low -> not between
                continue
            if h[j] > best:
                best, bi = h[j], j
        return best, bi

    upd = None  # 'H' if the last point is a fresh H still extendable by consecutive bearish candles, 'L' symmetric
    for i in range(1, n):
        # Repaint rule (cours avancé 40:21-43:31): consecutive opposite-colour candles right after a confirmation
        # extend the point instead of creating a new one.
        if upd == 'H' and col[i] == -1 and pts[-1].kind == 'H' and h[i] > pts[-1].price and pend_up is None:
            pts[-1] = Point('H', h[i], i, i, True, pts[-1].brk)
        elif upd == 'L' and col[i] == 1 and pts[-1].kind == 'L' and l[i] < pts[-1].price and pend_dn is None:
            pts[-1] = Point('L', l[i], i, i, True, pts[-1].brk)
        if not ((upd == 'H' and col[i] == -1) or (upd == 'L' and col[i] == 1)):
            upd = None
        H, L = last('H'), last('L')
        seq = ('l', 'h') if col[i] == 1 else ('h', 'l')
        for e in seq:
            if e == 'h':
                if pend_up is not None:
                    if h[i] > pend_up[0]:
                        pend_up = [h[i], i, pend_up[2]]
                elif h[i] > H.price or (eq_break and h[i] == H.price):
                    pend_up = [h[i], i, i]
            else:
                if pend_dn is not None:
                    if l[i] < pend_dn[0]:
                        pend_dn = [l[i], i, pend_dn[2]]
                elif l[i] < L.price or (eq_break and l[i] == L.price):
                    pend_dn = [l[i], i, i]
        # confirmations at the close of bar i
        if col[i] == -1 and pend_up is not None:
            newH = Point('H', pend_up[0], pend_up[1], i, True, pend_up[2])
            if pts[-1].kind == 'H':
                lv, li = lowest_between(pts[-1].idx, newH.idx)
                if li is not None:
                    pts.append(Point('L', lv, li, i, False))
            pts.append(newH)
            pend_up = None
            upd = 'H'
            # a pending down break opened against the OLD low is re-evaluated against the new range
            if pend_dn is not None and not (pend_dn[0] < last('L').price):
                pend_dn = None
            # bearish bar: its low comes after its high, may break the (new) last low
            if pend_dn is None and l[i] < last('L').price:
                pend_dn = [l[i], i, i]
        elif col[i] == 1 and pend_dn is not None:
            newL = Point('L', pend_dn[0], pend_dn[1], i, True, pend_dn[2])
            if pts[-1].kind == 'L':
                hv, hi = highest_between(pts[-1].idx, newL.idx)
                if hi is not None:
                    pts.append(Point('H', hv, hi, i, False))
            pts.append(newL)
            pend_dn = None
            upd = 'L'
            if pend_up is not None and not (pend_up[0] > last('H').price):
                pend_up = None
            if pend_up is None and h[i] > last('H').price:
                pend_up = [h[i], i, i]
    return pts, col
