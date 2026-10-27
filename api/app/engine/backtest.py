"""H1 backtest of Bob's structure setups with adjustable parameters.

Structure and events are computed once on the FULL history (cached per instrument / data version / eq_break);
a backtest only turns the events into trades (numpy races on H1 bars), then filters by period on entry time.

Conservative H1 resolution (see ARCHITECTURE.md):
- FT: entry = open of the bar after the confirmation bar;
- EPA: limit at `entry` (zone top by default), filled on the first bar touching it before U breaks;
- a bar touching both SL and TP counts as SL; on the EPA fill bar only the SL can be hit.
"""
from __future__ import annotations

import copy
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

from .. import config
from .continuation import walk
from .structure import build_structure

# Defaults = Bob's official rules (course modules 02-04, Oct 2026): entry in the zone (market if already in it,
# else limit at RR 3), no trade after a déséquilibre (IPA leg), one trade per Paris day, RR >= 1.
# V1_PARAMS reproduces the v1 site (limit at zone top, all legs, no daily cap).
DEFAULT_PARAMS = dict(
    eq_break=True,
    setups=dict(epa=True, ft_valid=True, ft_ipa=False),
    direction='both',
    epa_entry=0.0,
    epa_entry_rr=3.0,       # >0: EPA limit where RR equals this value (capped at the zone top)
    epa_inzone='market',    # 'skip' | 'market': price already in the zone when the break is confirmed
    epa_ipa_legs=False,     # False: no EPA trade after an IPA leg (déséquilibre)
    entry_hours_tz='paris', # 'utc' | 'paris'
    max_per_day=1,          # 1 = one trade per (Paris) day
    session_paris=[14, 17], # Bob analyses at 14:00 Paris and trades live until ~17:00: an order exists only from the first
                            # bar of this Paris-time window after the setup is confirmed (None = off: right after the bar)
    epa_cancel_on_lb=False,
    sl_buffer=0.0,
    tp_mode='structure',
    tp_rr=2.0,
    min_rr=1.0,
    max_hold_hours=480,
    entry_hours=[0, 24],
    cost_bp=None,
)
V1_PARAMS = dict(DEFAULT_PARAMS, epa_entry_rr=0.0, epa_inzone='skip', epa_ipa_legs=True, entry_hours_tz='utc',
                 max_per_day=0, min_rr=0.0, session_paris=None)
MAX_TRADES = 3000
PARIS = ZoneInfo('Europe/Paris')
MIN_N = 10


class ParamError(ValueError):
    pass


class NoData(RuntimeError):
    pass


# ------------------------------------------------------------------ params

def _num(p, key, lo=None, hi=None, hi_open=False):
    try:
        v = float(p[key])
    except (TypeError, ValueError):
        raise ParamError(f"Paramètre '{key}' invalide : un nombre est attendu.")
    if not np.isfinite(v) or (lo is not None and v < lo) or (hi is not None and (v >= hi if hi_open else v > hi)):
        raise ParamError(f"Paramètre '{key}' hors limites ({v}).")
    return v


def normalize_params(raw: dict | None, iid: str | None = None) -> dict:
    """Merge user params with the defaults and validate. cost_bp = instrument default when null."""
    p = copy.deepcopy(DEFAULT_PARAMS)
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ParamError("'params' doit être un objet.")
    for k, v in raw.items():
        if k == 'setups':
            if not isinstance(v, dict):
                raise ParamError("'setups' doit être un objet {epa, ft_valid, ft_ipa}.")
            for sk, sv in v.items():
                if sk in p['setups']:
                    p['setups'][sk] = bool(sv)
        elif k in p:
            p[k] = v
    p['eq_break'] = bool(p['eq_break'])
    p['epa_cancel_on_lb'] = bool(p['epa_cancel_on_lb'])
    if p['direction'] not in ('both', 'long', 'short'):
        raise ParamError("direction doit valoir 'both', 'long' ou 'short'.")
    if p['tp_mode'] not in ('structure', 'rr'):
        raise ParamError("tp_mode doit valoir 'structure' ou 'rr'.")
    p['epa_entry'] = _num(p, 'epa_entry', 0, 1, hi_open=True)
    p['epa_entry_rr'] = _num(p, 'epa_entry_rr', 0, 50)
    p['epa_ipa_legs'] = bool(p['epa_ipa_legs'])
    p['max_per_day'] = int(_num(p, 'max_per_day', 0, 10))
    sp = p.get('session_paris')
    if sp is not None:
        try:
            a, b = int(sp[0]), int(sp[1])
        except (TypeError, ValueError, IndexError, KeyError):
            raise ParamError("session_paris doit être [début, fin] en heures de Paris (ou null).")
        if not (0 <= a < b <= 24):
            raise ParamError("session_paris : 0 <= début < fin <= 24.")
        p['session_paris'] = [a, b]
    if p['epa_inzone'] not in ('skip', 'market'):
        raise ParamError("epa_inzone doit valoir 'skip' ou 'market'.")
    if p['entry_hours_tz'] not in ('utc', 'paris'):
        raise ParamError("entry_hours_tz doit valoir 'utc' ou 'paris'.")
    p['sl_buffer'] = _num(p, 'sl_buffer', 0, 10)
    p['tp_rr'] = _num(p, 'tp_rr', 0.05, 100)
    p['min_rr'] = _num(p, 'min_rr', 0, 100)
    p['max_hold_hours'] = int(_num(p, 'max_hold_hours', 1, 24 * 365 * 2))
    eh = p['entry_hours']
    try:
        a, b = int(eh[0]), int(eh[1])
    except (TypeError, ValueError, IndexError, KeyError):
        raise ParamError("entry_hours doit être [début, fin] en heures UTC.")
    if not (0 <= a <= 24 and 0 <= b <= 24) or a == b:
        raise ParamError("entry_hours : heures entre 0 et 24, début différent de fin.")
    p['entry_hours'] = [a, b]
    if p['cost_bp'] is None:
        p['cost_bp'] = config.BY_ID[iid]['cost_bp'] if iid in config.BY_ID else 0.0
    else:
        p['cost_bp'] = _num(p, 'cost_bp', 0, 1000)
    return p


def _sim_key(p: dict) -> str:
    """Cache key of the trade simulation: everything but `setups` (setups only select trades afterwards)."""
    q = {k: v for k, v in p.items() if k != 'setups'}
    return json.dumps(q, sort_keys=True)


# ------------------------------------------------------------------ structure

@dataclass
class Struct:
    t: np.ndarray            # bar open time, epoch seconds (int64)
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    pts: list = field(default_factory=list)
    events: list = field(default_factory=list)


def compute_structure(bars, eq_break: bool = True) -> Struct:
    """bars: DataFrame open/high/low/close with a tz-naive UTC DatetimeIndex (bar open time)."""
    o, h, l, c = (bars[x].to_numpy(dtype=float) for x in ('open', 'high', 'low', 'close'))
    t = bars.index.values.astype('datetime64[s]').astype(np.int64)
    pts, _ = build_structure(o, h, l, c, eq_break=eq_break)
    return Struct(t=t, o=o, h=h, l=l, c=c, pts=pts, events=walk(pts))


class _LRU:
    """Small thread-safe LRU cache; one computation per key even with concurrent callers."""

    def __init__(self, size):
        self.size, self.d, self.lock, self.klocks = size, OrderedDict(), threading.Lock(), {}

    def get(self, key, fn):
        with self.lock:
            if key in self.d:
                self.d.move_to_end(key)
                return self.d[key]
            kl = self.klocks.setdefault(key, threading.Lock())
        with kl:
            with self.lock:
                if key in self.d:
                    return self.d[key]
            val = fn()
            with self.lock:
                self.d[key] = val
                while len(self.d) > self.size:
                    self.d.popitem(last=False)
                self.klocks.pop(key, None)
            return val


_STRUCTS = _LRU(12)
_SIMS = _LRU(24)
_RUNS = _LRU(48)


def get_struct(iid: str, eq_break: bool = True) -> tuple[Struct, int]:
    from .. import store
    bars = store.load_bars(iid)
    if bars is None or len(bars) < 10:
        raise NoData(iid)
    ver = store.data_version(iid)
    return _STRUCTS.get((iid, ver, bool(eq_break)), lambda: compute_structure(bars, eq_break)), ver


# ------------------------------------------------------------------ races

def _first(mask) -> int:
    if mask.size == 0:
        return -1
    i = int(mask.argmax())
    return i if mask[i] else -1


def _race(H, L, d, tp, sl, skip_tp0=False):
    """Bars H/L from the entry bar. Returns (1 TP / 0 SL / None unresolved, bar offset).
    A bar touching both = SL. skip_tp0: the TP cannot be hit on bar 0 (EPA fill bar)."""
    if d == 1:
        mt, ms = H >= tp, L <= sl
    else:
        mt, ms = L <= tp, H >= sl
    if skip_tp0 and mt.size:
        mt[0] = False
    a, b = _first(mt), _first(ms)
    if a >= 0 and (b < 0 or a < b):
        return 1, a
    if b >= 0:
        return 0, b
    return None, None


def _win_end(t, i0, hours) -> int:
    """First bar index outside the hold window that starts at bar i0."""
    return int(np.searchsorted(t, t[i0] + hours * 3600, side='left'))


def _complete(t, i0, hours, e) -> bool:
    """True when the hold window is fully covered by the data (so an unresolved trade is closed, not running)."""
    return e < len(t) or t[-1] + 3600 >= t[i0] + hours * 3600


def _session_bars(t, window):
    """Indices of the bars opening inside the Paris-time window [a, b) (None when the session is off)."""
    if window is None:
        return None
    import pandas as pd
    hp = pd.to_datetime(np.asarray(t, dtype='int64'), unit='s', utc=True).tz_convert('Europe/Paris').hour.values
    return np.flatnonzero((hp >= window[0]) & (hp < window[1]))


def _hour_ok(ts, hours, tz='utc') -> bool:
    a, b = hours
    if tz == 'paris':
        h = datetime.fromtimestamp(int(ts), PARIS).hour
    else:
        h = int(ts % 86400 // 3600)
    return a <= h < b if a < b else (h >= a or h < b)


def levels(d, entry, sl0, tp0, p):
    """Apply sl_buffer and tp_mode to the structural SL/TP. None if the geometry is invalid."""
    if not ((tp0 - entry) * d > 0 and (entry - sl0) * d > 0):
        return None
    sl = sl0 - d * p['sl_buffer'] * abs(entry - sl0)
    risk = abs(entry - sl)
    tp = tp0 if p['tp_mode'] == 'structure' else entry + d * p['tp_rr'] * risk
    return sl, tp, risk


def _trade(st, p, cost_bp, kind, valid, d, i0, entry, sl, tp, skip_tp0=False, **extra):
    """Resolve one trade entered at bar i0 (entry time = open of bar i0)."""
    t = st.t
    risk = abs(entry - sl)
    e2 = _win_end(t, i0, p['max_hold_hours'])
    code, k = _race(st.h[i0:e2], st.l[i0:e2], d, tp, sl, skip_tp0)
    cost_r = entry * cost_bp * 1e-4 / risk
    rr = abs(tp - entry) / risk
    tr = dict(k=kind, v=int(valid), d=d, ti=int(t[i0]), to=None, e=float(entry), sl=float(sl), tp=float(tp),
              rr=float(rr), p0=float(risk / abs(tp - sl)), s='en cours', R=None, **extra)
    if code is not None:
        tr.update(s='TP' if code else 'SL', to=int(t[i0 + k]), R=float((rr if code else -1.0) - cost_r))
    elif _complete(t, i0, p['max_hold_hours'], e2):
        last = e2 - 1
        tr.update(s='expiré', to=int(t[last]), R=float((st.c[last] - entry) * d / risk - cost_r))
    return tr


def epa_scan(st: Struct, e: dict, p: dict, start: int | None = None) -> dict:
    """State of the EPA limit order of a BREAK event on the bars that followed its confirmation.
    state: 'wait' (not yet touched), 'filled', 'cancel_u' (U broke first), 'skip' (price already past the entry
    when the break was confirmed). Also reports where the opposite point Lb broke (not a cancel in the backtest,
    as in v1; the plan cancels the order when Lb sits between the price and the entry).
    start: first bar index at which the order exists (Bob's 14:00 session); the zone must still be alive then."""
    d, U, Z, zlo, Lb = e['d'], e['U'], e['Z'], e['zlo'], e['Lb']
    entry = Z - p['epa_entry'] * (Z - zlo)
    if p.get('epa_entry_rr', 0) > 0:
        R_ = p['epa_entry_rr']
        deep = (U + R_ * zlo) / (1 + R_)             # price giving RR = R_ between SL (zlo) and TP (U)
        entry = deep if (Z - deep) * d >= 0 else Z     # never above the zone top
    s, n = e['known'] + 1, len(st.t)
    out = dict(entry=entry, s=s, state='wait', fill=-1, u=-1, lb=-1, expired=False, P0=None)
    if start is not None and start > s:
        if start >= n:
            return out
        a_, b_ = st.h[s:start], st.l[s:start]
        if (d == 1 and (a_.max() >= U or b_.min() <= zlo)) or (d == -1 and (b_.min() <= U or a_.max() >= zlo)):
            out['state'] = 'skip'          # target reached or zone broken before the session: no order
            return out
        s = start
        out['s'] = s
    if s >= n:
        return out
    P0 = float(st.o[s])
    out['P0'] = P0
    e1 = _win_end(st.t, s, p['max_hold_hours'])
    if not ((U - P0) * d > 0 and (P0 - entry) * d > 0 and (entry - zlo) * d > 0):
        if p.get('epa_inzone') == 'market' and (U - P0) * d > 0 and (P0 - zlo) * d > 0 and (zlo - Z) * d <= 0 and (Z - P0) * d >= 0:
            out.update(state='filled', fill=s, entry=P0)      # price already in the zone: market order at the open
            return out
        out['state'] = 'skip'
        return out
    H, L = st.h[s:e1], st.l[s:e1]
    eq = p['eq_break']
    if d == 1:
        a, b, lb = _first(L <= entry), _first(H >= U), _first(L <= Lb if eq else L < Lb)
    else:
        a, b, lb = _first(H >= entry), _first(L <= U), _first(H >= Lb if eq else H > Lb)
    out['lb'] = s + lb if lb >= 0 else -1
    if a >= 0 and (b < 0 or a <= b):
        out.update(state='filled', fill=s + a)
    elif b >= 0:
        out.update(state='cancel_u', u=s + b)
    else:
        out['expired'] = _complete(st.t, s, p['max_hold_hours'], e1)
    return out


def simulate(st: Struct, p: dict, cost_bp: float | None = None):
    """All theoretical trades (every setup) and the race records of the break events, on the full history.
    Direction, min_rr, entry_hours apply here; `setups` is applied later (see summarize)."""
    cost_bp = p['cost_bp'] if cost_bp is None else cost_bp
    t, n, hold = st.t, len(st.t), p['max_hold_hours']
    dirs = {'both': (1, -1), 'long': (1,), 'short': (-1,)}[p['direction']]
    trades, races = [], []
    sess = _session_bars(t, p.get('session_paris'))

    def place(i):          # first bar >= i at which Bob can place an order (his 14:00 session)
        if sess is None:
            return i
        j = int(np.searchsorted(sess, i))
        return int(sess[j]) if j < len(sess) else n

    for e in st.events:
        s, d = e['known'] + 1, e['d']
        if s >= n or d not in dirs:
            continue
        if e['ev'] == 'FT':
            sp = place(s)
            if sp >= n:
                continue
            if sp > s:     # the FT must still be alive (between its point and its target) when Bob looks at the chart
                hh, ll = st.h[s:sp], st.l[s:sp]
                if (d == 1 and (hh.max() >= e['TP'] or ll.min() <= e['SL'])) or (d == -1 and (ll.min() <= e['TP'] or hh.max() >= e['SL'])):
                    continue
            s = sp
            P0 = float(st.o[s])
            lv = levels(d, P0, e['SL'], e['TP'], p)
            if lv is None:
                continue
            sl, tp, risk = lv
            if abs(tp - P0) / risk < p['min_rr'] or not _hour_ok(t[s], p['entry_hours'], p['entry_hours_tz']):
                continue
            trades.append(_trade(st, p, cost_bp, 'FT', e['valid'], d, s, P0, sl, tp))
            continue
        # BREAK: race records (independent of the trade parameters) and the EPA trade
        U, Z, zlo, Lb = e['U'], e['Z'], e['zlo'], e['Lb']
        P0 = float(st.o[s])
        e1 = _win_end(t, s, hold)
        H, L = st.h[s:e1], st.l[s:e1]
        r = dict(t=int(t[s]), d=d, leg=e['leg'])
        if (U - P0) * d > 0 and (P0 - Lb) * d > 0:
            code, _ = _race(H, L, d, U, Lb)
            if code is not None:
                r['cont'], r['p0_cont'] = code, abs(P0 - Lb) / abs(U - Lb)
        if (U - P0) * d > 0 and (P0 - Z) * d > 0 and (Z - zlo) * d > 0:
            if d == 1:
                a, b = _first(L <= Z), _first(H >= U)
            else:
                a, b = _first(H >= Z), _first(L <= U)
            if a >= 0 or b >= 0:
                r['zone'], r['p0_zone'] = int(a >= 0 and (b < 0 or a <= b)), abs(U - P0) / abs(U - Z)
        races.append(r)
        sc = epa_scan(st, e, p, start=place(e['known'] + 1) if sess is not None else None)
        if sc['state'] != 'filled' or (e['leg'] == 'IPA' and not p['epa_ipa_legs']):
            continue
        i0, entry = sc['fill'], sc['entry']
        lv = levels(d, entry, zlo, U, p)
        if lv is None:
            continue
        sl, tp, risk = lv
        if abs(tp - entry) / risk < p['min_rr'] or not _hour_ok(t[i0], p['entry_hours'], p['entry_hours_tz']):
            continue
        lbf = bool(sc['lb'] >= 0 and sc['lb'] <= i0 and (Lb - entry) * d > 0)
        if lbf and p['epa_cancel_on_lb']:
            continue          # Lb broke on the way to the zone: order cancelled
        trades.append(_trade(st, p, cost_bp, 'EPA', True, d, i0, entry, sl, tp, skip_tp0=True, ts=int(t[s]), lbf=lbf))
    if p['max_per_day']:
        # Bob: one trade per day (first entry of the Paris day, FT and EPA together). Approximation: the day of the entry time.
        seen, keep = {}, []
        for x in sorted(trades, key=lambda x: x['ti']):
            if x['k'] == 'FT' and not x['v']:
                keep.append(x); continue        # invalid FT stay hidden
            day = datetime.fromtimestamp(x['ti'], PARIS).date()
            if seen.get(day, 0) < p['max_per_day']:
                seen[day] = seen.get(day, 0) + 1; keep.append(x)
        trades = keep
    return trades, races


# ------------------------------------------------------------------ statistics

def _wk(ts):
    """Monday-aligned week index of epoch seconds (1970-01-05 is a Monday)."""
    return (np.asarray(ts, dtype=np.int64) - 4 * 86400) // 604800


def clustered(x, wk):
    """t-stat of the mean with errors clustered by week (as v1)."""
    x = np.asarray(x, dtype=float)
    if len(x) < MIN_N:
        return None
    _, inv = np.unique(wk, return_inverse=True)
    s, cnt = np.bincount(inv, weights=x), np.bincount(inv)
    se = np.sqrt(((s - x.mean() * cnt) ** 2).sum()) / len(x)
    return float(x.mean() / se) if se > 0 else None


def _r(x, nd=2):
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def trade_stat(rows):
    """TradeStat of the closed trades (TP / SL / expiré); None below 10 trades."""
    g = [r for r in rows if r['R'] is not None]
    if len(g) < MIN_N:
        return None
    R = np.array([r['R'] for r in g])
    win = np.array([r['s'] == 'TP' for r in g])
    cum = np.cumsum(R[np.argsort([r['to'] for r in g], kind='stable')])
    dd = np.maximum.accumulate(np.maximum(cum, 0)) - cum
    neg = -R[R < 0].sum()
    return dict(n=len(g), win=_r(win.mean(), 4), p0=_r(np.mean([r['p0'] for r in g]), 4),
                rr=_r(np.median([r['rr'] for r in g])), R=_r(R.mean(), 4), Rtot=_r(R.sum(), 1),
                t=_r(clustered(R, _wk([r['ti'] for r in g]))), maxdd=_r(dd.max(), 1),
                pf=_r(R[R > 0].sum() / neg) if neg > 0 else None)


def race_stat(rows, w, p0):
    g = [r for r in rows if w in r]
    if len(g) < MIN_N:
        return None
    x = np.array([r[w] - r[p0] for r in g], dtype=float)
    return dict(n=len(g), obs=_r(np.mean([r[w] for r in g]), 4), p0=_r(np.mean([r[p0] for r in g]), 4),
                t=_r(clustered(x, _wk([r['t'] for r in g]))))


def _day(s, end=False):
    if s is None or s == '':
        return None
    try:
        dt = datetime.strptime(str(s)[:10], '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except ValueError:
        raise ParamError(f"Date invalide '{s}' (format AAAA-MM-JJ).")
    return int(dt.timestamp()) + (86400 if end else 0)     # end date included


def enabled(tr, setups) -> bool:
    return setups['epa'] if tr['k'] == 'EPA' else (setups['ft_valid'] if tr['v'] else setups['ft_ipa'])


def fmt_trade(tr: dict, dec: int) -> dict:
    """Public shape of a trade (internal fields dropped, prices rounded)."""
    rd = dec + 1
    return dict(k=tr['k'], v=tr['v'], d=tr['d'], ti=tr['ti'], to=tr['to'], e=round(tr['e'], rd), sl=round(tr['sl'], rd),
                tp=round(tr['tp'], rd), rr=round(tr['rr'], 2), p0=round(tr['p0'], 3), s=tr['s'],
                R=None if tr['R'] is None else round(tr['R'], 3))


def summarize(trades, races, p, dec, first_t, last_t, start=None, end=None):
    """Statistics, equity and trade list of a simulation for a period (filter on entry time)."""
    t0, t1 = _day(start), _day(end, True)
    lo = t0 if t0 is not None else -1 << 62
    hi = t1 if t1 is not None else 1 << 62
    inp = [x for x in trades if lo <= x['ti'] < hi]
    rac = [x for x in races if lo <= x['t'] < hi]
    sel = [x for x in inp if enabled(x, p['setups'])]
    closed = [x for x in sel if x['R'] is not None]
    stats = dict(
        epa=trade_stat([x for x in inp if x['k'] == 'EPA']),
        ft_valide=trade_stat([x for x in inp if x['k'] == 'FT' and x['v']]),
        ft_ipa=trade_stat([x for x in inp if x['k'] == 'FT' and not x['v']]),
        total=trade_stat(sel),
        continuation=race_stat(rac, 'cont', 'p0_cont'),
        retour_zone=race_stat(rac, 'zone', 'p0_zone'),
        retour_zone_apres_epa=race_stat([x for x in rac if x['leg'] == 'EPA'], 'zone', 'p0_zone'),
    )
    byexit = sorted(closed, key=lambda x: (x['to'], x['ti']))
    cum, equity = 0.0, []
    for x in byexit:
        cum += x['R']
        equity.append([x['to'], round(cum, 3)])
    ym = {}
    for x in closed:
        dt = datetime.fromtimestamp(x['ti'], timezone.utc)
        ym.setdefault((dt.year, dt.month), []).append(x)
    by_month = [dict(m=f'{y}-{m:02d}', R=round(sum(z['R'] for z in g), 2), n=len(g)) for (y, m), g in sorted(ym.items())]
    yy = {}
    for (y, _), g in ym.items():
        yy.setdefault(y, []).extend(g)
    by_year = []
    for y, g in sorted(yy.items()):
        e_, f_ = [z for z in g if z['k'] == 'EPA'], [z for z in g if z['k'] == 'FT']
        by_year.append(dict(an=y, R=round(sum(z['R'] for z in g), 2), n=len(g), epa_R=round(sum(z['R'] for z in e_), 2),
                            ft_R=round(sum(z['R'] for z in f_), 2), epa_n=len(e_), ft_n=len(f_)))
    shown = sorted(sel, key=lambda x: x['ti'])
    n_all = len(shown)
    shown = shown[-MAX_TRADES:]
    day = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d')
    return dict(
        period=dict(start=start or day(first_t), end=end or day(last_t)), params=p, resolution='H1', stats=stats,
        equity=equity, by_month=by_month, by_year=by_year, trades=[fmt_trade(x, dec) for x in shown],
        n_trades=n_all, truncated=n_all > MAX_TRADES, data_end=int(last_t) + 3600)


# ------------------------------------------------------------------ entry points

def get_sim(iid: str, p: dict):
    """(Struct, trades, races) for normalized params, cached per (instrument, data version, params)."""
    st, ver = get_struct(iid, p['eq_break'])
    tr, ra = _SIMS.get((iid, ver, _sim_key(p)), lambda: simulate(st, p))
    return st, tr, ra


def run(iid: str, start=None, end=None, params=None) -> dict:
    """Backtest payload of POST /api/backtest/{id}."""
    p = normalize_params(params, iid)
    t0, t1 = _day(start), _day(end, True)
    if t0 is not None and t1 is not None and t1 <= t0:
        raise ParamError("La date de fin doit être après la date de début.")
    st, tr, ra = get_sim(iid, p)
    from .. import store
    ver = store.data_version(iid)
    dec = config.BY_ID[iid]['dec'] if iid in config.BY_ID else 5
    key = (iid, ver, _sim_key(p), json.dumps(p['setups'], sort_keys=True), start, end)
    return _RUNS.get(key, lambda: summarize(tr, ra, p, dec, st.t[0], st.t[-1], start, end))


def hist(iid: str) -> dict:
    """Default-params, full-history stats of the two main setups (context for the plan)."""
    r = run(iid)
    return dict(epa=r['stats']['epa'], ft_valide=r['stats']['ft_valide'])
