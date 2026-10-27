"""Backtest rules on hand-built bars (H1 conservative resolution, params) and on the XAUUSD fixture."""
import numpy as np
import pytest

import fixtures as F
from app import config
from app.engine import backtest as B

T0 = 1767571200  # 2026-01-05 00:00 UTC (Monday)


def mk(bars, events):
    """Struct from [(o, h, l, c)] hourly bars from T0 and hand-written walk events."""
    a = np.array(bars, dtype=float)
    t = T0 + 3600 * np.arange(len(a), dtype=np.int64)
    return B.Struct(t=t, o=a[:, 0], h=a[:, 1], l=a[:, 2], c=a[:, 3], pts=[], events=events)


def ft(known, d, tp, sl, valid=True):
    return dict(ev='FT', d=d, known=known, TP=tp, SL=sl, valid=valid, prev_leg='EPA', prot_old=None, age=1, profond='x')


def brk(known, d, U, Z, zlo, Lb, leg='EPA', n=2, prot=None):
    return dict(ev='BREAK', d=d, known=known, U=U, Lb=Lb, Z=Z, zlo=zlo, prot=prot, n=n, leg=leg, prev_leg=None, age=1)


def P(**kw):
    return B.normalize_params(kw, None)


def sim(st, **kw):
    p = P(**kw)
    return B.simulate(st, p, p['cost_bp'])


# ---- FT

def test_ft_entry_at_next_open_and_tp():
    # FT confirmed at bar 0, entry = open of bar 1 (100), SL 95, TP 110
    st = mk([(100, 101, 99, 100), (100, 103, 99, 102), (102, 111, 101, 110), (110, 110, 110, 110)], [ft(0, 1, 110, 95)])
    tr, _ = sim(st, cost_bp=0.0)
    assert len(tr) == 1
    x = tr[0]
    assert (x['k'], x['d'], x['ti'], x['e'], x['sl'], x['tp']) == ('FT', 1, T0 + 3600, 100, 95, 110)
    assert x['s'] == 'TP' and x['to'] == T0 + 2 * 3600 and x['R'] == pytest.approx(2.0)
    assert x['rr'] == pytest.approx(2.0) and x['p0'] == pytest.approx(5 / 15)


def test_ft_tie_in_bar_is_sl():
    st = mk([(100, 101, 99, 100), (100, 111, 94, 100), (100, 100, 100, 100)], [ft(0, 1, 110, 95)])
    x = sim(st, cost_bp=0.0)[0][0]
    assert x['s'] == 'SL' and x['to'] == T0 + 3600 and x['R'] == pytest.approx(-1.0)


def test_cost_in_r():
    st = mk([(100, 101, 99, 100), (100, 111, 94, 100), (100, 100, 100, 100)], [ft(0, 1, 110, 95)])
    x = sim(st, cost_bp=50.0)[0][0]       # 100 * 50e-4 / 5 = 0.1 R
    assert x['R'] == pytest.approx(-1.1)


def test_ft_short_mirrored():
    st = mk([(100, 101, 99, 100), (100, 101, 94, 96), (96, 97, 89, 90)], [ft(0, -1, 90, 105)])
    x = sim(st, cost_bp=0.0)[0][0]
    assert x['d'] == -1 and x['s'] == 'TP' and x['R'] == pytest.approx(2.0)


def test_ft_invalid_geometry_skipped():
    # next open already beyond the TP
    st = mk([(100, 101, 99, 100), (111, 112, 110, 111)], [ft(0, 1, 110, 95)])
    assert sim(st)[0] == []


# ---- EPA

EPA_BARS = [(105, 106, 104, 105),            # 0: break confirmation
            (105, 106, 104, 105),            # 1: s, P0 = 105
            (105, 111, 99.5, 108),           # 2: touches the zone top (100) AND the TP (110) in the same bar
            (108, 109, 107, 108),            # 3
            (108, 112, 107, 111)]            # 4: TP


def epa_ev(**kw):
    return [brk(0, 1, U=110, Z=100, zlo=95, Lb=95, **kw)]


def test_epa_fill_bar_only_sl_can_trigger():
    st = mk(EPA_BARS, epa_ev())
    x = sim(st, cost_bp=0.0)[0][0]
    assert x['k'] == 'EPA' and x['e'] == 100 and x['ti'] == T0 + 2 * 3600
    # TP touched on the fill bar is ignored, hit later on bar 4
    assert x['s'] == 'TP' and x['to'] == T0 + 4 * 3600 and x['R'] == pytest.approx(2.0)


def test_epa_fill_bar_sl():
    bars = list(EPA_BARS)
    bars[2] = (105, 106, 94, 96)             # fill and SL in the same bar -> SL
    x = sim(mk(bars, epa_ev()), cost_bp=0.0)[0][0]
    assert x['s'] == 'SL' and x['to'] == T0 + 2 * 3600


def test_epa_cancelled_when_u_breaks_first():
    bars = list(EPA_BARS)
    bars[1] = (105, 111, 104.5, 110)         # U (110) broken before the zone is touched
    tr, races = sim(mk(bars, epa_ev()))
    assert tr == []
    assert races[0]['zone'] == 0


def test_epa_entry_fraction():
    st = mk(EPA_BARS, epa_ev())
    # entry at 97.5 (half zone): the 99.5 low does not fill it
    assert sim(st, epa_entry=0.5)[0] == []
    bars = [(105, 106, 104, 105), (105, 106, 104, 105), (105, 106, 99.5, 100), (100, 101, 97, 98), (98, 112, 97, 111)]
    x = sim(mk(bars, epa_ev()), epa_entry=0.5, cost_bp=0.0)[0][0]
    assert x['e'] == pytest.approx(97.5) and x['ti'] == T0 + 3 * 3600 and x['sl'] == 95 and x['s'] == 'TP'
    assert x['rr'] == pytest.approx(12.5 / 2.5)


def test_epa_entry_mirrored_short():
    # bear: Z = broken low (100), zlo = high (105) of the zone; half zone entry = 102.5
    bars = [(95, 96, 94, 95), (95, 96, 94, 95), (95, 103, 94, 102), (102, 104, 100, 101), (101, 102, 89, 90)]
    ev = [brk(0, -1, U=90, Z=100, zlo=105, Lb=105)]
    x = sim(mk(bars, ev), epa_entry=0.5, cost_bp=0.0)[0][0]
    assert x['d'] == -1 and x['e'] == pytest.approx(102.5) and x['sl'] == 105 and x['tp'] == 90 and x['s'] == 'TP'


# ---- params

def two_trades():
    """One long FT (SL) and one short FT (TP)."""
    bars = [(100, 101, 99, 100), (100, 111, 94, 100), (100, 100, 100, 100), (100, 101, 99, 100),
            (100, 101, 94, 96), (96, 97, 89, 90)]
    return mk(bars, [ft(0, 1, 110, 95), ft(3, -1, 90, 105)])


def test_direction_filter():
    st = two_trades()
    assert [x['d'] for x in sim(st)[0]] == [1, -1]
    assert [x['d'] for x in sim(st, direction='long')[0]] == [1]
    assert [x['d'] for x in sim(st, direction='short')[0]] == [-1]


def test_setups_select_trades_in_summary():
    st = two_trades()
    tr, ra = sim(st)
    off = P(setups=dict(ft_valid=False))
    r = B.summarize(tr, ra, off, 2, st.t[0], st.t[-1])
    assert r['trades'] == [] and r['equity'] == []
    r = B.summarize(tr, ra, P(), 2, st.t[0], st.t[-1])
    assert len(r['trades']) == 2 and len(r['equity']) == 2
    # FT en IPA only when enabled
    st2 = mk([(100, 101, 99, 100), (100, 111, 94, 100)], [ft(0, 1, 110, 95, valid=False)])
    tr2, ra2 = sim(st2)
    assert B.summarize(tr2, ra2, P(), 2, 0, 0)['trades'] == []
    assert len(B.summarize(tr2, ra2, P(setups=dict(ft_ipa=True)), 2, 0, 0)['trades']) == 1


def test_min_rr_filter():
    st = two_trades()     # both RR 2
    assert len(sim(st, min_rr=2.0)[0]) == 2
    assert sim(st, min_rr=2.1)[0] == []


def test_tp_mode_rr_and_sl_buffer():
    st = mk([(100, 101, 99, 100), (100, 103, 99, 102), (102, 121, 101, 120), (120, 120, 120, 120)], [ft(0, 1, 110, 95)])
    x = sim(st, tp_mode='rr', tp_rr=3.0, cost_bp=0.0)[0][0]
    assert x['tp'] == pytest.approx(115) and x['rr'] == pytest.approx(3.0) and x['s'] == 'TP' and x['R'] == pytest.approx(3.0)
    # sl_buffer 0.2: SL 5 * 1.2 = 6 away -> 94, risk 6
    x = sim(st, sl_buffer=0.2, cost_bp=0.0)[0][0]
    assert x['sl'] == pytest.approx(94) and x['rr'] == pytest.approx(10 / 6)
    # a stop widened enough to survive a bar that would have hit the original SL
    st2 = mk([(100, 101, 99, 100), (100, 103, 94.5, 102), (102, 111, 101, 110), (110, 110, 110, 110)], [ft(0, 1, 110, 95)])
    assert sim(st2, cost_bp=0.0)[0][0]['s'] == 'SL'
    assert sim(st2, sl_buffer=0.2, cost_bp=0.0)[0][0]['s'] == 'TP'


def test_entry_hours_window():
    st = two_trades()     # entries at 01:00 and 04:00 UTC
    assert len(sim(st, entry_hours=[0, 24])[0]) == 2
    assert [x['ti'] for x in sim(st, entry_hours=[2, 6])[0]] == [T0 + 4 * 3600]
    assert [x['ti'] for x in sim(st, entry_hours=[0, 4])[0]] == [T0 + 3600]
    assert len(sim(st, entry_hours=[23, 2])[0]) == 1          # wrap-around window


def test_max_hold_expiry_and_running():
    st = mk([(100, 101, 99, 100)] * 31, [ft(0, 1, 110, 95)])
    x = sim(st, max_hold_hours=5, cost_bp=0.0)[0][0]
    assert x['s'] == 'expiré' and x['R'] == pytest.approx(0.0) and x['to'] == T0 + 5 * 3600
    # window not covered by the data: still running
    x = sim(st, max_hold_hours=480, cost_bp=0.0)[0][0]
    assert x['s'] == 'en cours' and x['R'] is None and x['to'] is None


def test_normalize_params_validation():
    p = B.normalize_params({'direction': 'long', 'setups': {'ft_ipa': True}}, 'XAUUSD')
    assert p['setups'] == dict(epa=True, ft_valid=True, ft_ipa=True) and p['cost_bp'] == config.BY_ID['XAUUSD']['cost_bp']
    for bad in ({'direction': 'x'}, {'epa_entry': 1.0}, {'tp_mode': 'z'}, {'entry_hours': [5, 5]}, {'min_rr': -1}):
        with pytest.raises(B.ParamError):
            B.normalize_params(bad, 'XAUUSD')


# ---- statistics, period

def many_trades(n=40):
    """n alternating winning / losing long FTs, one every 3 hours."""
    bars, ev = [], []
    for i in range(n):
        base = len(bars)
        bars += [(100, 101, 99, 100),
                 (100, 111, 99, 110) if i % 2 == 0 else (100, 101, 94, 95),
                 (100, 100, 100, 100)]
        ev.append(ft(base, 1, 110, 95))
    return mk(bars, ev)


def test_trade_stat_and_none_below_ten():
    st = many_trades(40)
    tr, ra = sim(st, cost_bp=0.0)
    r = B.summarize(tr, ra, P(), 2, st.t[0], st.t[-1])
    s = r['stats']['total']
    assert s['n'] == 40 and s['win'] == 0.5 and s['R'] == pytest.approx(0.5) and s['Rtot'] == pytest.approx(20.0)
    assert s['maxdd'] == pytest.approx(1.0) and s['pf'] == pytest.approx(2.0) and s['rr'] == 2.0
    assert r['stats']['epa'] is None and r['stats']['ft_ipa'] is None       # < 10 trades
    assert B.summarize(tr[:9], ra, P(), 2, 0, 0)['stats']['total'] is None


def test_period_filters_on_entry_time_end_inclusive():
    st = many_trades(40)       # 120 bars = 5 days from Mon 2026-01-05, entries at hour 3k+1
    tr, ra = sim(st, cost_bp=0.0)
    full = B.summarize(tr, ra, P(), 2, st.t[0], st.t[-1])
    d1 = B.summarize(tr, ra, P(), 2, st.t[0], st.t[-1], start='2026-01-06', end='2026-01-06')
    assert 0 < len(d1['trades']) < len(full['trades'])
    assert all(1767657600 <= x['ti'] < 1767744000 for x in d1['trades'])     # the whole of 2026-01-06
    assert d1['period'] == {'start': '2026-01-06', 'end': '2026-01-06'}
    none = B.summarize(tr, ra, P(), 2, st.t[0], st.t[-1], start='2027-01-01')
    assert none['trades'] == [] and none['stats']['total'] is None
    with pytest.raises(B.ParamError):
        B.summarize(tr, ra, P(), 2, 0, 0, start='nope')


def test_equity_and_month_year():
    st = many_trades(20)
    tr, ra = sim(st, cost_bp=0.0)
    r = B.summarize(tr, ra, P(), 2, st.t[0], st.t[-1])
    assert [e[0] for e in r['equity']] == sorted(e[0] for e in r['equity'])
    assert r['equity'][-1][1] == pytest.approx(sum(x['R'] for x in tr), abs=1e-2)
    assert r['by_month'] == [dict(m='2026-01', R=pytest.approx(10.0), n=20)]
    assert r['by_year'][0]['an'] == 2026 and r['by_year'][0]['ft_n'] == 20 and r['by_year'][0]['epa_n'] == 0


def test_trades_cap_flag(monkeypatch):
    monkeypatch.setattr(B, 'MAX_TRADES', 5)
    st = many_trades(12)
    tr, ra = sim(st)
    r = B.summarize(tr, ra, P(), 2, 0, 0)
    assert len(r['trades']) == 5 and r['truncated'] is True and r['n_trades'] == 12
    assert r['trades'][-1]['ti'] == max(x['ti'] for x in tr)


def test_race_stats():
    # 12 long breaks: price returns to the zone top then breaks U (zone=1) / or breaks U directly (zone=0)
    bars, ev = [], []
    for i in range(12):
        base = len(bars)
        bars += [(105, 106, 104, 105), (105, 106, 104, 105),
                 (105, 106, 99, 100) if i % 2 == 0 else (105, 111, 104, 110), (100, 112, 99, 111)]
        ev.append(brk(base, 1, U=110, Z=100, zlo=95, Lb=95))
    _, ra = B.simulate(mk(bars, ev), P(), 0.0)
    s = B.summarize([], ra, P(), 2, 0, 0)['stats']
    assert s['retour_zone']['n'] == 12 and s['retour_zone']['obs'] == pytest.approx(0.5)
    assert s['retour_zone']['p0'] == pytest.approx(5 / 10)           # (U - P0) / (U - Z)
    assert s['continuation']['n'] == 12 and s['continuation']['p0'] == pytest.approx(10 / 15, abs=1e-3)
    assert s['retour_zone_apres_epa']['n'] == 12


# ---- real structure on the XAUUSD fixture: must reproduce v1's trades

def test_fixture_matches_v1_trades():
    bars = F.fixture_bars()
    st = B.compute_structure(bars, True)
    p = P(cost_bp=1.2)
    tr, _ = B.simulate(st, p, p['cost_bp'])
    v1 = F.v1_json()['trades']
    key = lambda k, v, d, ti: (k, v, d, ti // 3600)
    mine = {key(x['k'], x['v'], x['d'], x['ti']): x for x in tr}
    hit = [(y, mine[key(y['k'], y['v'], y['d'], y['ti'])]) for y in v1 if key(y['k'], y['v'], y['d'], y['ti']) in mine]
    # Since 2026-10-27 the engine applies Bob's structure exceptions 1-2, drops eq_break phantom points and emits
    # the first EPA zone of each trend: v1's trades are still found (same levels), plus new ones.
    assert len(hit) / len(v1) > 0.9
    same = [abs(x['e'] - y['e']) < 0.02 and abs(x['sl'] - y['sl']) < 0.02 and abs(x['tp'] - y['tp']) < 0.02 for y, x in hit]
    assert sum(same) / len(hit) > 0.9
    assert sum(x['s'] == y['s'] for y, x in hit) / len(hit) > 0.9
    assert len(tr) >= len(v1) - 10


def test_run_with_store_and_cache(fake_store):
    fake_store.put('XAUUSD', F.fixture_bars())
    r = B.run('XAUUSD', None, None, {})
    assert r['resolution'] == 'H1' and r['params']['cost_bp'] == config.BY_ID['XAUUSD']['cost_bp'] and r['stats']['total']['n'] > 100
    assert B.run('XAUUSD', None, None, {}) is r                      # cached
    assert B.run('XAUUSD', '2026-06-01', None, {})['stats']['total']['n'] < r['stats']['total']['n']
    fake_store.put('XAUUSD', F.fixture_bars().iloc[:-48])            # new data version -> recomputed
    assert B.run('XAUUSD', None, None, {}) is not r
    fake_store.put('GBPUSD', F.fixture_bars().iloc[:5])
    with pytest.raises(B.NoData):
        B.run('GBPUSD', None, None, {})


@pytest.mark.bob_defaults
def test_defaults_are_bob_rules():
    p = B.normalize_params({})
    assert p['epa_inzone'] == 'market' and p['epa_entry_rr'] == 3.0 and p['epa_ipa_legs'] is False
    assert p['max_per_day'] == 1 and p['entry_hours_tz'] == 'paris' and p['min_rr'] == 1.0
    v1 = B.normalize_params(B.V1_PARAMS)
    assert v1['epa_inzone'] == 'skip' and v1['max_per_day'] == 0
