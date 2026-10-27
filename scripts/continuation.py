"""Structure continuation statistics on gold (H1 structure, M1 resolution), with Bob's zone logic:
- trend after 2 breaks in the same direction; protected point as in strategy.py;
- reference zone for the next pullback = [last L, broken H] (bull), and it is FROZEN after an IPA leg
  (a pullback that did not come back into the reference zone does not move it);
- FT = one counter-trend break that does not take the protected point. Valid only if it reaches the reference zone
  (bull: FT low <= reference zone top). An FT that stays above the zone (after IPA legs) is "FT en IPA" = invalid.
  FT on FT (a 2nd counter break before 2 new breaks in the trend) = no trend.

Every outcome is a race between two price levels, so the no-edge (martingale) probability is known exactly:
P(reach upper level U before lower level D | price P) = (P - D) / (U - D).

Races recorded (bull case; bear is symmetric):
 BREAK events (a new break in the trend, known at the confirmation bar), price P0 just after:
   zone_first : does price come back to the zone top Z before breaking the new high U?     p0 = (U - P0) / (U - Z)
   cont       : does it break U before the protected low?                                  p0 = (P0 - prot) / (U - prot)
   epa        : once Z is touched (before U), does it break U before the zone low?         p0 = (Z - zlo) / (U - zlo)
 FT events (counter break known at its confirmation):
   ft         : back to the trend high (TP) before the FT extreme (SL)?                    p0 = (P0 - SL) / (TP - SL)
Ties inside the same minute count against the trade (SL first).
"""
import numpy as np
import pandas as pd
from structure import build_structure

MAXM = 60 * 24 * 20     # 20 days max per race
COST_BP = 1.2           # FTMO gold round trip


def first(mask):
    if mask.size == 0:
        return -1
    i = int(np.argmax(mask))
    return i if mask[i] else -1


def race(H, L, up, dn, d):
    """d=+1: does price reach `up` (high >= up) before `dn` (low <= dn)? d=-1 mirrors (up/dn are the target/stop).
    Returns (1 win / 0 loss / None unresolved, minutes)."""
    if d == 1:
        a, b = first(H >= up), first(L <= dn)
    else:
        a, b = first(L <= up), first(H >= dn)
    if a < 0 and b < 0:
        return None, None
    if a >= 0 and (b < 0 or a < b):
        return 1, a
    return 0, b


def walk(pts):
    """Chronological state machine over structure points (in the order they become known). Returns event dicts."""
    order = sorted(range(len(pts)), key=lambda k: (pts[k].known, k))
    ev = []
    trend = None
    cu = cd = 0
    prot = None
    ref = None          # (lo, hi) bull: [L, broken H]; bear: [broken L, H]
    ft_ok = True
    ft_active = False   # last counter-break was an FT and the trend has not re-broken twice since
    last_leg = None
    age = 0
    for k in order:
        p = pts[k]
        if not p.is_break:
            continue
        if p.kind == 'H':
            prevH = next((pts[j] for j in range(k - 1, -1, -1) if pts[j].kind == 'H'), None)
            Lb = next((pts[j] for j in range(k - 1, -1, -1) if pts[j].kind == 'L'), None)
            if prevH is None or Lb is None:
                continue
            cu += 1
            cd = 0
            if trend == 'bull':
                if ft_active:
                    leg = 'apres_FT'
                elif ref is None:
                    leg = 'premiere'
                elif Lb.price <= ref[1]:
                    leg = 'EPA'
                else:
                    leg = 'IPA'
                if leg != 'IPA':
                    ref = (Lb.price, prevH.price)
                if cu >= 2:
                    ft_ok, ft_active = True, False
                age += 1
                ev.append(dict(ev='BREAK', d=1, known=p.known, U=p.price, Lb=Lb.price, Z=ref[1], zlo=ref[0],
                               prot=prot, n=cu, leg=leg, prev_leg=last_leg, age=age))
                last_leg = leg
            elif trend == 'bear':
                # counter break against a bear trend
                if prot is not None and p.price > prot:
                    trend, ref, last_leg = None, None, None
                elif cu == 1:
                    if not ft_ok:
                        trend, ref, last_leg = None, None, None      # FT on FT = IPA, no trend
                    else:
                        valid = p.price >= ref[0] if ref else True   # bear zone = [broken L, H]; FT high must reach it
                        ev.append(dict(ev='FT', d=-1, known=p.known, TP=Lb.price, SL=p.price, valid=bool(valid),
                                       prev_leg=last_leg, prot_old=prot, age=age,
                                       profond=('au-dela zone' if ref and p.price > ref[1] else 'dans zone') if valid else 'IPA'))
                        prot = p.price
                        ft_ok, ft_active = False, True
            if trend is None and cu >= 2:
                trend = 'bull'
                lows = [x.price for x in pts[:k] if x.kind == 'L' and x.idx >= _last_idx(pts, k, 'L', 2)]
                prot = min(lows) if lows else Lb.price
                ref = (Lb.price, prevH.price)
                ft_ok, ft_active, last_leg, age = True, False, 'debut', 0
            elif trend == 'bear' and cu >= 2:
                trend = 'bull'
                lows = [x.price for x in pts[:k] if x.kind == 'L' and x.idx >= _last_idx(pts, k, 'L', 2)]
                prot = min(lows) if lows else Lb.price
                ref = (Lb.price, prevH.price)
                ft_ok, ft_active, last_leg, age = True, False, 'debut', 0
        else:
            prevL = next((pts[j] for j in range(k - 1, -1, -1) if pts[j].kind == 'L'), None)
            Hb = next((pts[j] for j in range(k - 1, -1, -1) if pts[j].kind == 'H'), None)
            if prevL is None or Hb is None:
                continue
            cd += 1
            cu = 0
            if trend == 'bear':
                if ft_active:
                    leg = 'apres_FT'
                elif ref is None:
                    leg = 'premiere'
                elif Hb.price >= ref[0]:
                    leg = 'EPA'
                else:
                    leg = 'IPA'
                if leg != 'IPA':
                    ref = (prevL.price, Hb.price)
                if cd >= 2:
                    ft_ok, ft_active = True, False
                age += 1
                ev.append(dict(ev='BREAK', d=-1, known=p.known, U=p.price, Lb=Hb.price, Z=ref[0], zlo=ref[1],
                               prot=prot, n=cd, leg=leg, prev_leg=last_leg, age=age))
                last_leg = leg
            elif trend == 'bull':
                if prot is not None and p.price < prot:
                    trend, ref, last_leg = None, None, None
                elif cd == 1:
                    if not ft_ok:
                        trend, ref, last_leg = None, None, None
                    else:
                        valid = p.price <= ref[1] if ref else True
                        ev.append(dict(ev='FT', d=1, known=p.known, TP=Hb.price, SL=p.price, valid=bool(valid),
                                       prev_leg=last_leg, prot_old=prot, age=age,
                                       profond=('au-dela zone' if ref and p.price < ref[0] else 'dans zone') if valid else 'IPA'))
                        prot = p.price
                        ft_ok, ft_active = False, True
            if trend is None and cd >= 2:
                trend = 'bear'
                highs = [x.price for x in pts[:k] if x.kind == 'H' and x.idx >= _last_idx(pts, k, 'H', 2)]
                prot = max(highs) if highs else Hb.price
                ref = (prevL.price, Hb.price)
                ft_ok, ft_active, last_leg, age = True, False, 'debut', 0
            elif trend == 'bull' and cd >= 2:
                trend = 'bear'
                highs = [x.price for x in pts[:k] if x.kind == 'H' and x.idx >= _last_idx(pts, k, 'H', 2)]
                prot = max(highs) if highs else Hb.price
                ref = (prevL.price, Hb.price)
                ft_ok, ft_active, last_leg, age = True, False, 'debut', 0
    return ev


def _last_idx(pts, k, kind, back):
    cnt = 0
    for j in range(k - 1, -1, -1):
        if pts[j].kind == kind:
            cnt += 1
            if cnt == back:
                return pts[j].idx
    return 0


def resolve(ev, h1, m1):
    mt = m1.index.values
    mh, ml, mo = m1.high.values, m1.low.values, m1.open.values
    close_t = (h1.index + pd.Timedelta(hours=1)).values
    out = []
    for e in ev:
        s = np.searchsorted(mt, close_t[e['known']])
        if s >= len(mt) - 10:
            continue
        H, L = mh[s:s + MAXM], ml[s:s + MAXM]
        P0 = mo[s]
        d = e['d']
        r = dict(e, t=pd.Timestamp(mt[s]), P0=P0)
        if e['ev'] == 'BREAK':
            U, Z, zlo, prot = e['U'], e['Z'], e['zlo'], e['prot']
            # already beyond the levels at the start: skip the affected race
            if (U - P0) * d > 0 and (P0 - Z) * d > 0:
                w, mins = race(L if d == 1 else H, H if d == 1 else L, Z, U, 1) if False else (None, None)
                # zone first? the 'win' here is reaching Z before U
                if d == 1:
                    a, b = first(L <= Z), first(H >= U)
                else:
                    a, b = first(H >= Z), first(L <= U)
                if a >= 0 or b >= 0:
                    r['zone_first'] = int(a >= 0 and (b < 0 or a <= b))
                    r['p0_zone'] = abs(U - P0) / abs(U - Z)
                    if r['zone_first']:
                        # EPA race from the zone top
                        HH, LL = H[a:], L[a:]
                        w, _ = race(HH, LL, U, zlo, d)
                        if w is not None and (Z - zlo) * d > 0:
                            r['epa'] = w
                            r['p0_epa'] = abs(Z - zlo) / abs(U - zlo)
                            risk = abs(Z - zlo)
                            r['R_epa'] = (abs(U - Z) / risk if w else -1.0) - Z * COST_BP * 1e-4 / risk
            if prot is not None and (P0 - prot) * d > 0 and (U - P0) * d > 0:
                w, _ = race(H, L, U, prot, d)
                if w is not None:
                    r['cont'] = w
                    r['p0_cont'] = abs(P0 - prot) / abs(U - prot)
        else:
            TP, SL = e['TP'], e['SL']
            if (TP - P0) * d > 0 and (P0 - SL) * d > 0:
                w, _ = race(H, L, TP, SL, d)
                if w is not None:
                    risk = abs(P0 - SL)
                    r['ft'] = w
                    r['p0_ft'] = risk / abs(TP - SL)
                    r['R_ft'] = (abs(TP - P0) / risk if w else -1.0) - P0 * COST_BP * 1e-4 / risk
                    r['risk'] = risk
        out.append(r)
    return pd.DataFrame(out)


def build(eq_break=True):
    h1 = pd.read_parquet('data/xau_h1.parquet')
    m1 = pd.read_parquet('data/xau_m1.parquet')
    o, h, l, c = (h1[x].values for x in ('open', 'high', 'low', 'close'))
    pts, _ = build_structure(o, h, l, c, eq_break=eq_break)
    ev = walk(pts)
    df = resolve(ev, h1, m1)
    df.to_pickle('research/continuation/events.pkl')
    return df


if __name__ == '__main__':
    import os
    os.makedirs('research/continuation', exist_ok=True)
    df = build()
    print(df.ev.value_counts().to_string())
    print(df.groupby('ev').t.agg(['min', 'max']))
