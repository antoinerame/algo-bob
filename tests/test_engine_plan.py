"""Plan: order statuses, params, lot sizing (incl. JPY / EUR conversion) and the HTTP layer."""
import pytest

import fixtures as F
from app.engine import backtest as B
from app.engine import plan as PL
from test_engine_backtest import EPA_BARS as EPA_BARS_LB, P, T0, brk, ft, mk

SPECS = F.FakeStore().specs('XAUUSD')       # contract 100, USD, step 0.01
ACC = dict(balance=100000.0, currency='USD', risk_pct=0.5)


def plan_of(bars, events, params=None, tick='mid', account=None, specs=None, rates=None):
    st = mk(bars, events)
    p = B.normalize_params(params, 'XAUUSD')
    trades, _ = B.simulate(st, p, p['cost_bp'])
    asof = int(st.t[-1] + 3600)
    if tick == 'mid':
        tick = dict(t=asof + 60, bid=float(st.c[-1]), ask=float(st.c[-1]) + 0.2, source='dukascopy')
    return PL.make_plan('XAUUSD', st, p, PL.normalize_account(account or ACC), specs or SPECS, rates or {}, tick,
                        asof + 300, trades, {})


EV = [brk(0, 1, U=110, Z=100, zlo=95, Lb=95, leg='EPA', n=2, prot=90)]
WAIT = [(105, 106, 104, 105)] * 4                 # break at bar 0, price waits above the zone


def epa(plan):
    return [o for o in plan['orders'] if o['setup'] == 'EPA']


# ---- EPA order statuses

def test_epa_buy_limit_to_place():
    pl = plan_of(WAIT, EV)
    assert pl['trend']['dir'] == 1 and pl['trend']['n'] == 2 and pl['trend']['leg'] == 'EPA'
    assert pl['levels'] == dict(U=110, Z=100, zlo=95, Lb=95, prot=90)
    [o] = epa(pl)
    assert (o['side'], o['type'], o['status']) == ('BUY', 'LIMIT', 'à placer')
    assert (o['entry'], o['sl'], o['tp'], o['rr']) == (100, 95, 110, 2.0)
    assert o['lots'] == 1.0 and o['risk_money'] == 500.0 and o['risk_pct'] == 0.5       # 500 / (5 * 100)
    assert o['created_t'] == T0 + 3600
    assert any('Annuler si le prix casse 110,00 avant d\'atteindre la zone' in c for c in o['cancel_if'])
    assert pl['asof'] == T0 + 4 * 3600 and pl['last']['source'] == 'dukascopy' and pl['warnings'] == []


def test_epa_params_move_the_order():
    o = epa(plan_of(WAIT, EV, dict(epa_entry=0.5, sl_buffer=0.2)))[0]
    assert o['entry'] == 97.5 and o['sl'] == pytest.approx(97.5 - 2.5 * 1.2)
    assert o['lots'] == pytest.approx(500 / (3 * 100), abs=0.01)
    o = epa(plan_of(WAIT, EV, dict(tp_mode='rr', tp_rr=3)))[0]
    assert o['tp'] == 115 and o['rr'] == 3.0


def test_epa_en_position_when_zone_touched():
    bars = WAIT + [(105, 106, 99.5, 101), (101, 103, 100.5, 102)]
    pl = plan_of(bars, EV, tick=dict(t=0, bid=102, ask=102.2, source='mt5'))
    [o] = epa(pl)
    assert o['status'] == 'en position' and o['entry'] == 100 and o['lots'] == 1.0
    assert len(pl['open_trades']) == 1 and pl['open_trades'][0]['s'] == 'en cours'


def test_epa_annule_when_u_breaks_first():
    bars = WAIT + [(105, 111, 104, 110), (110, 111, 103, 104)]
    [o] = epa(plan_of(bars, EV))
    assert o['status'] == 'annulé' and '110,00' in o['note'] and o['lots'] == 0


def test_epa_lb_first_depends_on_epa_cancel_on_lb():
    ev = [brk(0, 1, U=110, Z=100, zlo=95, Lb=102, leg='IPA')]      # IPA leg: Lb sits above the zone
    on = dict(epa_cancel_on_lb=True)
    assert any('102,00' in c and c.startswith('Annuler') for c in epa(plan_of(WAIT, ev, on))[0]['cancel_if'])
    bars = WAIT + [(105, 105, 101.5, 103)]                          # Lb broke before the zone
    [o] = epa(plan_of(bars, ev, on))
    assert o['status'] == 'annulé' and '102,00' in o['note']
    pl = plan_of(bars, ev)                                          # default: order kept, informational only
    [o] = epa(pl)
    assert o['status'] == 'à placer' and any(c.startswith('Info') and '102,00' in c for c in o['cancel_if'])
    assert any('102,00' in w for w in pl['warnings'])


def test_backtest_drops_epa_filled_after_lb_when_asked():
    # IPA-like: Lb = 102 above the zone top 100; price breaks 102 then reaches 100 -> filled (v1) / dropped (option)
    bars = [(105, 106, 104, 105), (105, 106, 104, 105), (105, 105, 101.5, 102), (102, 102, 99.5, 100), (100, 112, 100, 111)]
    ev = [brk(0, 1, U=110, Z=100, zlo=95, Lb=102, leg='IPA')]
    assert len(B.simulate(mk(bars, ev), P(), 0.0)[0]) == 1
    tr, _ = B.simulate(mk(bars, ev), P(epa_cancel_on_lb=True), 0.0)
    assert tr == []
    # Lb not on the path (EPA leg, Lb = zone bottom): unaffected
    ev2 = [brk(0, 1, U=110, Z=100, zlo=95, Lb=95)]
    assert len(B.simulate(mk(EPA_BARS_LB, ev2), P(epa_cancel_on_lb=True), 0.0)[0]) == 1
    assert B.normalize_params({}, 'XAUUSD')['epa_cancel_on_lb'] is False


def test_epa_live_tick_checks():
    # price already below the entry: warning, order kept
    pl = plan_of(WAIT, EV, tick=dict(t=0, bid=99.0, ask=99.2, source='mt5'))
    assert epa(pl)[0]['status'] == 'à placer' and any('BUY LIMIT' in w and 'sous' in w for w in pl['warnings'])
    # price through the target: cancelled
    assert epa(plan_of(WAIT, EV, tick=dict(t=0, bid=110.5, ask=110.7, source='mt5')))[0]['status'] == 'annulé'
    # price under the stop: cancelled
    assert epa(plan_of(WAIT, EV, tick=dict(t=0, bid=94.0, ask=94.2, source='mt5')))[0]['status'] == 'annulé'


def test_sell_limit_for_bear_trend():
    ev = [brk(0, -1, U=90, Z=100, zlo=105, Lb=105, leg='EPA', n=2, prot=110)]
    bars = [(95, 96, 94, 95)] * 4
    [o] = epa(plan_of(bars, ev))
    assert (o['side'], o['type'], o['status'], o['entry'], o['sl'], o['tp']) == ('SELL', 'LIMIT', 'à placer', 100, 105, 90)
    assert o['lots'] == 1.0


def test_no_trend_no_order_when_trend_broken():
    # price closed above the protected point of a bear trend
    ev = [brk(0, -1, U=90, Z=100, zlo=105, Lb=105, leg='EPA', n=2, prot=96)]
    pl = plan_of([(95, 96, 94, 95), (95, 99, 94, 98)], ev)
    assert pl['trend']['dir'] == 0 and pl['levels'] is None and pl['orders'] == []
    assert PL.make_plan  # noqa


def test_params_filter_orders():
    assert epa(plan_of(WAIT, EV, dict(setups=dict(epa=False)))) == []
    assert epa(plan_of(WAIT, EV, dict(direction='short'))) == []
    pl = plan_of(WAIT, EV, dict(min_rr=3))
    assert epa(pl) == [] and any('RR' in w and 'minimum' in w for w in pl['warnings'])


# ---- FT orders

def test_ft_market_order_on_last_bar():
    bars = [(100, 101, 99, 100)] * 3                       # FT confirmed on the last closed bar (index 2)
    pl = plan_of(bars, [ft(2, -1, tp=90, sl=105)], tick=dict(t=0, bid=100.0, ask=100.2, source='mt5'))
    [o] = pl['orders']
    assert (o['setup'], o['side'], o['type'], o['status']) == ('FT', 'SELL', 'MARKET', 'à placer')
    assert (o['entry'], o['sl'], o['tp'], o['rr']) == (100.0, 105.0, 90.0, 2.0)      # SELL at the bid
    assert o['lots'] == 1.0
    # a BUY uses the ask
    [o] = plan_of(bars, [ft(2, 1, tp=110, sl=95)], tick=dict(t=0, bid=100.0, ask=100.2, source='mt5'))['orders']
    assert (o['side'], o['entry']) == ('BUY', 100.2)


def test_ft_in_position_resolved_and_ignored():
    bars = [(100, 101, 99, 100), (100, 103, 99, 102), (102, 103, 101, 102)]
    [o] = plan_of(bars, [ft(0, 1, tp=110, sl=95)])['orders']
    assert o['status'] == 'en position' and o['type'] == 'MARKET' and o['entry'] == 100
    # TP hit last bar -> shown as finished
    bars2 = bars[:2] + [(102, 111, 101, 110)]
    [o] = plan_of(bars2, [ft(0, 1, tp=110, sl=95)])['orders']
    assert o['status'] == 'exécuté' and 'TP' in o['note']
    # invalid FT (IPA) needs ft_ipa
    assert plan_of(bars, [ft(0, 1, 110, 95, valid=False)])['orders'] == []
    assert len(plan_of(bars, [ft(0, 1, 110, 95, valid=False)], dict(setups=dict(ft_ipa=True)))['orders']) == 1


def test_ft_price_already_beyond_levels_warns():
    bars = [(100, 101, 99, 100)] * 3
    pl = plan_of(bars, [ft(2, 1, tp=99, sl=95)], tick=dict(t=0, bid=100.0, ask=100.2, source='mt5'))
    assert pl['orders'] == [] and pl['warnings']


def test_orders_sorted_actionable_first_and_no_tick_warning():
    bars = WAIT[:1] + [(105, 106, 99.5, 101)] + [(101, 102, 100.5, 101)] * 3        # EPA filled
    pl = plan_of(bars, EV + [ft(3, 1, tp=110, sl=95)], tick=None)
    assert any('Aucun tick' in w for w in pl['warnings']) and pl['last']['source'] == 'bar'
    assert [o['status'] for o in pl['orders']] == sorted([o['status'] for o in pl['orders']], key=PL.ORDER_RANK.get)


# ---- lot sizing

def test_lots_usd_quote():
    lots, risk, w = PL.lot_size(100, 95, SPECS, ACC, {})
    assert (lots, risk, w) == (1.0, 500.0, None)


def test_lots_round_down_to_step_and_minimum():
    sp = dict(SPECS, volume_step=0.1, volume_min=0.1)
    lots, risk, _ = PL.lot_size(100, 97, sp, ACC, {})       # 500 / 300 = 1.666 -> 1.6
    assert lots == 1.6 and risk == pytest.approx(480.0)
    lots, risk, w = PL.lot_size(100, 50, sp, dict(ACC, balance=1000), {})     # 5 USD / 5000 -> 0.001 lot
    assert lots == 0 and risk == 0 and 'lot minimum' in w


def test_lots_jpy_quote_uses_usdjpy():
    sp = dict(SPECS, contract=100000, quote='JPY')
    lots, risk, _ = PL.lot_size(150.00, 149.50, sp, ACC, {'USDJPY': 150.0})
    assert lots == 1.5 and risk == pytest.approx(500.0)     # 0.5 * 100000 / 150 = 333.33 USD per lot


def test_lots_eur_quote_uses_eurusd():
    sp = dict(SPECS, contract=1, quote='EUR')
    lots, risk, _ = PL.lot_size(18000, 17900, sp, ACC, {'EURUSD': 1.10})     # 100 * 1 * 1.10 = 110 USD per lot
    assert lots == 4.54 and risk == pytest.approx(4.54 * 110, abs=0.01)


def test_lots_eur_account():
    # account in EUR: 500 EUR risk, USD quote -> 1 USD = 1 / 1.25 EUR
    lots, risk, _ = PL.lot_size(100, 95, SPECS, dict(ACC, currency='EUR'), {'EURUSD': 1.25})
    assert lots == 1.25 and risk == pytest.approx(500.0)
    # JPY quote with EUR account: per lot = 0.5 * 100000 / 150 USD = 333.33 USD = 266.67 EUR
    sp = dict(SPECS, contract=100000, quote='JPY')
    lots, _, _ = PL.lot_size(150.0, 149.5, sp, dict(ACC, currency='EUR'), {'USDJPY': 150.0, 'EURUSD': 1.25})
    assert lots == 1.87


def test_lots_missing_rate_warns():
    sp = dict(SPECS, quote='JPY', contract=100000)
    lots, risk, w = PL.lot_size(150, 149, sp, ACC, {})
    assert lots == 0 and 'Taux' in w


def test_account_validation():
    with pytest.raises(B.ParamError):
        PL.normalize_account(dict(balance=-5))
    with pytest.raises(B.ParamError):
        PL.normalize_account(dict(risk_pct=0))
    assert PL.normalize_account(None) == dict(balance=100000.0, currency='USD', risk_pct=0.5)


def test_french_number_format():
    assert PL.fnum(4145.64, 2) == '4 145,64' and PL.fnum(1.08123, 5) == '1,08123'


# ---- full path on the fixture + HTTP layer

def test_build_plan_and_api(fake_store):
    from fastapi.testclient import TestClient
    from app.main import app
    bars = F.fixture_bars()
    fake_store.put('XAUUSD', bars)
    last = float(bars.close.iloc[-1])
    fake_store.ticks['XAUUSD'] = dict(t=int(bars.index[-1].timestamp()) + 3700, bid=last, ask=last + 0.3, source='dukascopy')
    with TestClient(app) as c:
        r = c.post('/api/plan/XAUUSD', json=dict(params={}, account=dict(balance=50000, risk_pct=1)))
        assert r.status_code == 200, r.text
        j = r.json()
        assert j['hist']['epa']['n'] > 50 and j['trend']['dir'] in (-1, 0, 1) and 'orders' in j
        for o in j['orders']:
            assert o['status'] in ('à placer', 'en position', 'annulé', 'exécuté') and o['lots'] >= 0
        assert c.post('/api/plan/XAUUSD', json=dict(account=dict(balance=-1))).status_code == 422

        r = c.post('/api/backtest/XAUUSD', json=dict(start='2026-03-01', end='2026-06-30', params=dict(direction='long')))
        j = r.json()
        assert r.status_code == 200 and j['period'] == {'start': '2026-03-01', 'end': '2026-06-30'}
        assert all(t['d'] == 1 for t in j['trades']) and j['resolution'] == 'H1'
        assert c.post('/api/backtest/XAUUSD', json=dict(params=dict(direction='up'))).status_code == 422
        assert c.post('/api/backtest/NOPE', json={}).status_code == 404
        assert c.post('/api/backtest/EURUSD', json={}).status_code == 503
        assert 'téléchargement' in c.post('/api/backtest/EURUSD', json={}).json()['error']

        r = c.get('/api/chart/XAUUSD')
        j = r.json()
        assert r.status_code == 200 and j['candles'] and len(j['candles'][0]) == 5 and j['points']
        assert j['candles'][0][0] >= j['data_end'] - 181 * 86400 and j['last_tick']['bid'] == last
        r = c.get('/api/chart/XAUUSD', params={'from': j['data_end'] - 3 * 86400})
        assert 0 < len(r.json()['candles']) < 80
        r0 = c.get('/api/chart/EURUSD')
        assert r0.status_code == 503 and 'progress' in r0.json() and 'detail' in r0.json()
        r1 = c.get('/api/chart/XAUUSD', params={'eq_break': 0})
        assert r1.status_code == 200 and r1.json()['points']

        d = c.get('/api/defaults').json()
        assert d['params']['tp_mode'] == 'structure' and d['account']['balance'] == 100000
        assert len(c.get('/api/instruments').json()) == 10
