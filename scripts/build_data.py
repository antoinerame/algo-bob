"""Build one JSON per instrument for the website: H1 candles (last months), structure points, theoretical trades
(EPA from the zone top, FT at confirmation), statistics against the no-edge rate, and the current plan.
Rules = continuation.py (zone frozen after IPA, FT valid only if it reaches the zone, FT on FT = no trend)."""
import json, sys, os
import numpy as np
import pandas as pd
sys.path.insert(0, '/home/claude/trading')
from structure import build_structure
from continuation import walk

D = '/home/claude/trading/data'
OUT = '/home/claude/trading/site/data'
INSTR = [  # id, label, file, cost bp round trip (approx. FTMO), decimals
    ('XAUUSD', 'Or', 'xau_m1.parquet', 1.2, 2),
    ('XAGUSD', 'Argent', 'xagusd_m1.parquet', 4.0, 3),
    ('EURUSD', 'Euro / Dollar', 'eurusd_m1.parquet', 0.8, 5),
    ('GBPUSD', 'Livre / Dollar', 'gbpusd_m1.parquet', 1.0, 5),
    ('USDJPY', 'Dollar / Yen', 'usdjpy_m1.parquet', 0.9, 3),
    ('US500', 'S&P 500', 'spxusd_m1.parquet', 1.5, 1),
    ('US100', 'Nasdaq 100', 'nsxusd_m1.parquet', 1.5, 1),
    ('GER40', 'DAX', 'grxeur_m1.parquet', 1.5, 1),
    ('USOIL', 'Pétrole WTI', 'wtiusd_m1.parquet', 3.0, 2),
    ('UKOIL', 'Pétrole Brent', 'bcousd_m1.parquet', 3.0, 2),
]
CHART_START = '2026-01-01'
MAXM = 60 * 24 * 20


def first(mask):
    if mask.size == 0:
        return -1
    i = int(np.argmax(mask))
    return i if mask[i] else -1


def race(H, L, up, dn, d):
    if d == 1:
        a, b = first(H >= up), first(L <= dn)
    else:
        a, b = first(L <= up), first(H >= dn)
    if a < 0 and b < 0:
        return None, None
    if a >= 0 and (b < 0 or a < b):
        return 1, a
    return 0, b


def clustered(x, wk):
    x = pd.Series(x).reset_index(drop=True)
    wk = pd.Series(wk).reset_index(drop=True)
    if len(x) < 10:
        return None
    s = x.groupby(wk).sum()
    se = np.sqrt(((s - x.mean() * x.groupby(wk).size()) ** 2).sum()) / len(x)
    return float(x.mean() / se) if se > 0 else None


def build(iid, label, f, cost_bp, dec):
    m1 = pd.read_parquet(f'{D}/{f}')
    if iid == 'XAUUSD':
        h1 = pd.read_parquet(f'{D}/xau_h1.parquet')
    else:
        h1 = m1.resample('1h', label='left', closed='left').agg(
            {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    o, h, l, c = (h1[x].values for x in ('open', 'high', 'low', 'close'))
    pts, _ = build_structure(o, h, l, c, eq_break=True)
    ev = walk(pts)
    mt = m1.index.values
    mh, ml, mo = m1.high.values, m1.low.values, m1.open.values
    close_t = (h1.index + pd.Timedelta(hours=1)).values
    last_t = pd.Timestamp(mt[-1])
    trades, breaks = [], []
    for e in ev:
        s = np.searchsorted(mt, close_t[e['known']])
        if s >= len(mt) - 2:
            continue
        H, L = mh[s:s + MAXM], ml[s:s + MAXM]
        P0, d = float(mo[s]), e['d']
        t0 = pd.Timestamp(mt[s])
        if e['ev'] == 'FT':
            TP, SL = e['TP'], e['SL']
            if not ((TP - P0) * d > 0 and (P0 - SL) * d > 0):
                continue
            w, k = race(H, L, TP, SL, d)
            risk = abs(P0 - SL)
            cost_r = P0 * cost_bp * 1e-4 / risk
            tr = dict(kind='FT', valid=bool(e['valid']), dir=d, t_in=t0, entry=P0, sl=SL, tp=TP,
                      rr=abs(TP - P0) / risk, p0=risk / abs(TP - SL), prev_leg=e.get('prev_leg'))
            if w is None:
                tr.update(status='en cours', R=None)
            else:
                tr.update(status='TP' if w else 'SL', t_out=pd.Timestamp(mt[s + k]),
                          R=(tr['rr'] if w else -1.0) - cost_r, win=w)
            trades.append(tr)
        else:
            U, Z, zlo, prot = e['U'], e['Z'], e['zlo'], e['prot']
            b = dict(t=t0, dir=d, leg=e['leg'], n=e['n'], P0=P0, U=U, Z=Z, zlo=zlo, prot=prot, Lb=e['Lb'])
            # next break in trend direction before the last opposite point
            if (U - P0) * d > 0 and (P0 - e['Lb']) * d > 0:
                w, _ = race(H, L, U, e['Lb'], d)
                if w is not None:
                    b['cont'] = w
                    b['p0_cont'] = abs(P0 - e['Lb']) / abs(U - e['Lb'])
            # zone return before the next break, then EPA trade from the zone top
            if (U - P0) * d > 0 and (P0 - Z) * d > 0 and (Z - zlo) * d > 0:
                a = first(L <= Z) if d == 1 else first(H >= Z)
                bb = first(H >= U) if d == 1 else first(L <= U)
                if a >= 0 or bb >= 0:
                    zf = int(a >= 0 and (bb < 0 or a <= bb))
                    b['zone'] = zf
                    b['p0_zone'] = abs(U - P0) / abs(U - Z)
                    if zf:
                        w, k = race(H[a:], L[a:], U, zlo, d)
                        risk = abs(Z - zlo)
                        tr = dict(kind='EPA', valid=True, dir=d, t_in=pd.Timestamp(mt[s + a]), entry=Z, sl=zlo, tp=U,
                                  rr=abs(U - Z) / risk, p0=risk / abs(U - zlo), prev_leg=e['leg'], t_setup=t0)
                        if w is None:
                            tr.update(status='en cours', R=None)
                        else:
                            tr.update(status='TP' if w else 'SL', t_out=pd.Timestamp(mt[s + a + k]),
                                      R=(tr['rr'] if w else -1.0) - Z * cost_bp * 1e-4 / risk, win=w)
                        trades.append(tr)
                elif (U - P0) * d > 0:
                    b['zone_pending'] = True
            breaks.append(b)
    T = pd.DataFrame(trades)
    B = pd.DataFrame(breaks)
    T['wk'] = T.t_in.dt.to_period('W').astype(str)
    B['wk'] = B.t.dt.to_period('W').astype(str)

    def race_stat(df, w, p0):
        g = df.dropna(subset=[w, p0])
        if len(g) < 10:
            return None
        x = g[w] - g[p0]
        return dict(n=int(len(g)), obs=round(float(g[w].mean()), 4), p0=round(float(g[p0].mean()), 4),
                    t=_r(clustered(x, g.wk)))

    def trade_stat(df):
        g = df[df.status.isin(['TP', 'SL'])]
        if len(g) < 10:
            return None
        return dict(n=int(len(g)), win=round(float(g.win.mean()), 4), p0=round(float(g.p0.mean()), 4),
                    rr=round(float(g.rr.median()), 2), R=round(float(g.R.mean()), 4), Rtot=round(float(g.R.sum()), 1),
                    t=_r(clustered(g.R, g.wk)))

    periods = {'tout': (None, None), '2010-2019': (None, '2020-01-01'), '2020-2026': ('2020-01-01', None)}
    stats = {}
    for pname, (a, z) in periods.items():
        tt = T.copy()
        bb = B.copy()
        if a:
            tt, bb = tt[tt.t_in >= a], bb[bb.t >= a]
        if z:
            tt, bb = tt[tt.t_in < z], bb[bb.t < z]
        stats[pname] = dict(
            continuation=race_stat(bb, 'cont', 'p0_cont'),
            retour_zone=race_stat(bb, 'zone', 'p0_zone'),
            retour_zone_apres_epa=race_stat(bb[bb.leg == 'EPA'], 'zone', 'p0_zone'),
            epa=trade_stat(tt[tt.kind == 'EPA']),
            ft_valide=trade_stat(tt[(tt.kind == 'FT') & tt.valid]),
            ft_ipa=trade_stat(tt[(tt.kind == 'FT') & ~tt.valid]),
        )
    # per year R for EPA and valid FT
    done = T[T.status.isin(['TP', 'SL'])].copy()
    done['an'] = done.t_in.dt.year
    years = []
    for an, g in done.groupby('an'):
        e_, f_ = g[g.kind == 'EPA'], g[(g.kind == 'FT') & g.valid]
        years.append(dict(an=int(an), epa_R=round(float(e_.R.sum()), 1), epa_n=int(len(e_)),
                          ft_R=round(float(f_.R.sum()), 1), ft_n=int(len(f_))))
    # chart window
    cw = h1[h1.index >= CHART_START]
    i0 = h1.index.get_indexer([cw.index[0]])[0]
    candles = [[int(t.timestamp()), round(r.open, dec), round(r.high, dec), round(r.low, dec), round(r.close, dec)]
               for t, r in cw.iterrows()]
    ptsj = [[int(h1.index[p.idx].timestamp()), p.kind, round(float(p.price), dec), int(p.is_break)]
            for p in pts if p.idx >= i0]
    tw = T[T.t_in >= CHART_START].sort_values('t_in')
    trj = []
    for _, r in tw.iterrows():
        trj.append(dict(k=r.kind, v=int(bool(r.valid)), d=int(r.dir), ti=int(r.t_in.timestamp()),
                        to=int(r.t_out.timestamp()) if isinstance(r.get('t_out'), pd.Timestamp) and not pd.isna(r.get('t_out')) else None,
                        e=round(float(r.entry), dec), sl=round(float(r.sl), dec), tp=round(float(r.tp), dec),
                        rr=round(float(r.rr), 2), p0=round(float(r.p0), 3), s=r.status,
                        R=None if r.R is None or pd.isna(r.R) else round(float(r.R), 2)))
    # current plan: last break event and whether its zone is still waiting; open trades
    plan = None
    if len(B):
        lb = B.iloc[-1]
        last_px = float(mo[-1])
        alive = (lb.U - last_px) * lb.dir > 0 and (last_px - lb.Lb) * lb.dir > 0
        plan = dict(t=int(lb.t.timestamp()), dir=int(lb.dir), leg=lb.leg, n=int(lb.n),
                    U=round(float(lb.U), dec), Z=round(float(lb.Z), dec), zlo=round(float(lb.zlo), dec),
                    prot=None if lb.prot is None or pd.isna(lb.prot) else round(float(lb.prot), dec),
                    Lb=round(float(lb.Lb), dec), last=round(last_px, dec), alive=bool(alive),
                    zone_pending=bool(lb.get('zone_pending', False) is True),
                    rr_zone=round(float(abs(lb.U - lb.Z) / abs(lb.Z - lb.zlo)), 2) if lb.Z != lb.zlo else None)
    open_tr = [x for x in trj if x['s'] == 'en cours']
    out = dict(id=iid, label=label, dec=dec, cost_bp=cost_bp, data_end=int(last_t.timestamp()),
               data_start=int(pd.Timestamp(mt[0]).timestamp()), candles=candles, points=ptsj, trades=trj,
               stats=stats, years=years, plan=plan, open=open_tr,
               counts=dict(ft=int((T.kind == 'FT').sum()), ft_valid=int(((T.kind == 'FT') & T.valid).sum()),
                           epa=int((T.kind == 'EPA').sum()), breaks=int(len(B))))
    with open(f'{OUT}/{iid}.json', 'w') as fh:
        json.dump(out, fh, separators=(',', ':'))
    return out


def _r(x):
    return None if x is None or not np.isfinite(x) else round(float(x), 2)


if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    only = sys.argv[1:]
    for row in INSTR:
        if only and row[0] not in only:
            continue
        o = build(*row)
        s = o['stats']['tout']
        print(row[0], len(o['candles']), 'bougies', o['counts'], 'FT valide', s['ft_valide'], 'EPA', s['epa'], flush=True)
