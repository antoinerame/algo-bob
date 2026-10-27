"""Targeted synthetic tests of Bob's trend rules in continuation.walk (formation modules 02-03, Oct 2026):
première zone EPA, déséquilibre (IPA), regain d'EPA, FT en déséquilibre invalide, FT qui comble le trou, point protégé
cassé (égalité comprise), FT sur FT, retour du protégé à l'origine, et filtre des jambes IPA dans le backtest.
The points are built by hand (kind, price, is_break); the walk only reads the flags and the prices."""
import numpy as np
import pytest

from app.engine.continuation import walk
from app.engine.structure import Point


def mk(seq):
    """seq = [(kind, price, is_break), ...] in chronological order -> list of Point (idx = known = position)."""
    return [Point(k, float(p), i, i, bool(b)) for i, (k, p, b) in enumerate(seq)]


# bull trend founded by two H breaks: zone of the 2nd break = [112 (low before the break), 120 (high broken)], U = 130
FOUND = [('L', 100, 0), ('H', 110, 0), ('L', 105, 0), ('H', 120, 1), ('L', 112, 0), ('H', 130, 1)]


def evs(seq, kind=None):
    e = walk(mk(seq))
    return [x for x in e if kind is None or x['ev'] == kind]


def test_first_epa_zone_is_emitted_at_the_founding_break():
    b = evs(FOUND, 'BREAK')
    assert len(b) == 1 and b[0]['leg'] == 'debut'
    assert (b[0]['zlo'], b[0]['Z'], b[0]['U']) == (112.0, 120.0, 130.0)
    assert b[0]['prot'] == 105.0          # low that started the trend (lowest L since the 2nd last L)


def test_one_break_alone_founds_nothing():
    assert evs(FOUND[:4]) == []


def test_desequilibre_leg_is_ipa_and_zone_stays_the_old_one():
    seq = FOUND + [('L', 125, 0), ('H', 140, 1)]          # next low (125) stays ABOVE the broken high (120)
    b = evs(seq, 'BREAK')
    assert b[-1]['leg'] == 'IPA'
    assert (b[-1]['zlo'], b[-1]['Z']) == (112.0, 120.0)    # frozen: the old zone, not [125, 130]


def test_regain_of_epa_after_a_real_return_into_the_old_zone():
    seq = FOUND + [('L', 125, 0), ('H', 140, 1), ('L', 115, 0), ('H', 150, 1)]   # 115 goes back below 120
    b = evs(seq, 'BREAK')
    assert b[-1]['leg'] == 'EPA'
    assert (b[-1]['zlo'], b[-1]['Z']) == (115.0, 140.0)    # the zone moves again


def test_three_breaks_without_return_stay_ipa():
    seq = FOUND + [('L', 125, 0), ('H', 140, 1), ('L', 135, 0), ('H', 150, 1), ('L', 145, 0), ('H', 160, 1)]
    legs = [x['leg'] for x in evs(seq, 'BREAK')]
    assert legs[1:] == ['IPA', 'IPA', 'IPA']


def test_ft_inside_the_desequilibre_is_invalid():
    seq = FOUND + [('L', 125, 0), ('H', 140, 1), ('L', 131, 1)]   # counter break whose low (131) does not reach the old zone (120)
    f = evs(seq, 'FT')
    assert len(f) == 1 and f[0]['valid'] is False and f[0]['profond'] == 'IPA'


def test_ft_that_fills_the_gap_and_returns_to_the_zone_regains_epa():
    seq = FOUND + [('L', 125, 0), ('H', 140, 1), ('L', 118, 1), ('H', 128, 0), ('L', 126, 0), ('H', 150, 1)]
    f = evs(seq, 'FT')
    assert f and f[0]['valid'] is True
    b = evs(seq, 'BREAK')
    assert b[-1]['leg'] == 'apres_FT'                      # EPA is back: this leg is tradeable (not 'IPA')


def test_protected_point_broken_means_no_trend_and_two_new_breaks_are_needed():
    seq = FOUND + [('L', 104, 1)]                          # below the protected low (105)
    assert evs(seq, 'FT') == []
    seq2 = seq + [('H', 120, 0), ('H', 131, 1)]            # 1 break alone: nothing
    assert len(evs(seq2, 'BREAK')) == 1
    seq3 = [('L', 100, 0), ('H', 110, 0), ('L', 105, 0), ('H', 120, 1), ('L', 112, 0), ('H', 130, 1), ('L', 104, 1),
            ('H', 125, 0), ('L', 103, 0), ('H', 131, 1), ('L', 120, 0), ('H', 135, 1)]
    b = evs(seq3, 'BREAK')
    assert [x['leg'] for x in b].count('debut') == 2       # the old trend, then the NEW one after 2 fresh breaks


def test_protected_point_equality_counts_as_a_break():
    seq = FOUND + [('L', 105, 1)]                          # equal to the protected low 105
    assert evs(seq, 'FT') == []


def test_no_ft_on_ft_before_two_new_breaks():
    seq = FOUND + [('L', 125, 0), ('H', 140, 1), ('L', 118, 1), ('H', 135, 0), ('H', 142, 1), ('L', 119, 1)]
    f = evs(seq, 'FT')
    assert len(f) == 1                                     # the second counter break right after the FT gives no FT


def test_protected_point_returns_to_the_origin_after_two_breaks():
    # FT at 118 (protected = 118), then 2 new H breaks: protected back to 105 -> a deeper counter break (110) is a FT again
    seq = FOUND + [('L', 125, 0), ('H', 140, 1), ('L', 118, 1), ('H', 130, 0), ('L', 126, 0), ('H', 150, 1),
                   ('L', 140, 0), ('H', 160, 1), ('L', 110, 1)]
    f = evs(seq, 'FT')
    assert len(f) == 2 and f[1]['SL'] == 110.0


def test_backtest_skips_ipa_legs_when_asked_and_keeps_them_in_v1(tmp_path):
    from app.engine import backtest as B
    n = 600
    rng = np.random.default_rng(3)
    c = 4000 + np.cumsum(rng.normal(0, 3, n)); o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0, 2, n); l = np.minimum(o, c) - rng.uniform(0, 2, n)
    import pandas as pd
    idx = pd.date_range('2026-01-05', periods=n, freq='h')
    st = B.compute_structure(pd.DataFrame(dict(open=o, high=h, low=l, close=c), index=idx), True)
    from collections import defaultdict
    leg_of = defaultdict(set)
    for e in st.events:
        if e['ev'] == 'BREAK':
            leg_of[int(st.t[e['known']] + 3600)].add(e['leg'])
    # conftest forces DEFAULT_PARAMS = V1_PARAMS for the old tests: pass the Bob options explicitly
    tr, _ = B.simulate(st, B.normalize_params(dict(B.V1_PARAMS, epa_ipa_legs=False), 'XAUUSD'))
    assert all(leg_of[x['ts']] - {'IPA'} for x in tr if x['k'] == 'EPA')
    tr1, _ = B.simulate(st, B.normalize_params(B.V1_PARAMS, 'XAUUSD'))
    assert any(leg_of[x['ts']] == {'IPA'} for x in tr1 if x['k'] == 'EPA') or not any(e['leg'] == 'IPA' for e in st.events)


# ---------------------------------------------------------------- Bob's session (14:00-17:00 Paris)
def _flat_struct(o_h_l_c, start='2026-10-12 00:00', events=()):
    import pandas as pd
    from app.engine import backtest as B
    a = np.array(o_h_l_c, dtype=float)
    t = (pd.date_range(start, periods=len(a), freq='h').astype('int64') // 10**9).to_numpy()
    return B.Struct(t=t, o=a[:, 0], h=a[:, 1], l=a[:, 2], c=a[:, 3], pts=[], events=list(events))


def _ft_event(known=3, d=-1, TP=90.0, SL=105.0):
    return dict(ev='FT', d=d, known=known, TP=TP, SL=SL, valid=True, prev_leg=None, prot_old=None, age=0, profond='dans zone')


def test_ft_waits_for_the_14h_paris_session_and_enters_at_its_open():
    from app.engine import backtest as B
    bars = [(100, 101, 99, 100)] * 30          # flat: the FT (short, SL 105, TP 90) stays alive
    st = _flat_struct(bars, events=[_ft_event()])
    off = B.normalize_params(dict(B.V1_PARAMS), 'XAUUSD')
    on = B.normalize_params(dict(B.V1_PARAMS, session_paris=[14, 17]), 'XAUUSD')
    t_off, _ = B.simulate(st, off); t_on, _ = B.simulate(st, on)
    assert t_off[0]['ti'] == int(st.t[4])                      # right after the confirmation bar (04:00 UTC)
    assert t_on[0]['ti'] == int(st.t[12])                      # 12:00 UTC = 14:00 Paris (summer time)


def test_ft_already_dead_before_the_session_gives_no_trade():
    from app.engine import backtest as B
    bars = [(100, 101, 99, 100)] * 6 + [(100, 101, 88, 90)] + [(90, 91, 89, 90)] * 23     # TP 90 reached at 06:00 UTC
    st = _flat_struct(bars, events=[_ft_event()])
    on = B.normalize_params(dict(B.V1_PARAMS, session_paris=[14, 17]), 'XAUUSD')
    assert B.simulate(st, on)[0] == []


@pytest.mark.bob_defaults
def test_session_param_validation():
    from app.engine import backtest as B
    with pytest.raises(B.ParamError):
        B.normalize_params(dict(B.V1_PARAMS, session_paris=[17, 14]), 'XAUUSD')
    assert B.normalize_params(dict(B.V1_PARAMS, session_paris=None), 'XAUUSD')['session_paris'] is None
    assert B.DEFAULT_PARAMS['session_paris'] == [14, 17]
