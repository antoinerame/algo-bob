"""GET /api/chart/{id}: optional `events` (walk events) and `breaks` (BOS lines) and `trend` (zigzag colour) next to the unchanged fields."""
import fixtures as F
from app import main


def _chart(fs, **kw):
    fs.put('XAUUSD', F.fixture_bars('XAUUSD'))
    return main._chart('XAUUSD', kw.get('frm', 0), kw.get('to'))


def test_chart_keeps_existing_fields(fake_store):
    d = _chart(fake_store)
    assert {'id', 'dec', 'candles', 'points', 'data_end', 'last_tick', 'events', 'breaks'} <= set(d)
    assert d['candles'] and d['points']


def test_chart_events_match_walk(fake_store):
    d = _chart(fake_store)
    from app.engine import backtest
    st, _ = backtest.get_struct('XAUUSD', True)
    assert len(d['events']) == len(st.events) > 0
    times = set(c[0] for c in d['candles'])
    for e, w in zip(d['events'], st.events):
        assert e['ev'] == w['ev'] and e['dir'] == w['d'] and e['t'] == int(st.t[w['known']]) and e['t'] in times
        assert e['tp'] is not None and e['tp'] <= e['t']
        if e['ev'] == 'BREAK':
            assert (e['U'], e['leg'], e['n']) == (round(w['U'], 3), w['leg'], w['n'])
            assert e['tb'] is not None and e['tk'] is not None and e['tk'] <= e['t']
        else:
            assert e['valid'] == w['valid'] and e['SL'] == round(w['SL'], 3) and e['TP'] == round(w['TP'], 3)


def test_chart_breaks_and_window(fake_store):
    d = _chart(fake_store)
    assert d['breaks']
    for tb, price, tk, direction in d['breaks']:
        assert tb > 0 and tk > 0 and direction in (1, -1) and isinstance(price, float)
    t_mid = d['candles'][len(d['candles']) // 2][0]
    d2 = _chart(fake_store, frm=t_mid)
    assert 0 < len(d2['events']) < len(d['events'])
    assert all(e['t'] >= t_mid for e in d2['events'])


def test_chart_trend_aligned_and_consistent_with_events(fake_store):
    d = _chart(fake_store)
    assert len(d['trend']) == len(d['points']) and set(d['trend']) <= {1, -1, 0} and {1, -1} <= set(d['trend'])
    # a point that triggered a BREAK / FT event is in a trend of the event's direction
    by_t = {}
    for (t, kind, price, brk), tr in zip(d['points'], d['trend']):
        by_t.setdefault(t, []).append(tr)
    for e in d['events']:
        assert e['tp'] in by_t and e['dir'] in by_t[e['tp']], e
