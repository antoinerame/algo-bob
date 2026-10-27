import os
import sys
import time

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from app import config, store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(config, 'PRICE_SOURCE', 'dukascopy')
    for d in (store._series, store._version, store._view, store._mt5_tick, store._duka_tick, store._prog):
        d.clear()
    store._loaded.clear()
    store._mt5_specs = None
    store._state = None
    store._feeds['mt5']['last_seen'] = None
    store._feeds['dukascopy'].update(ok=False, last_run=None, last_error=None)
    yield


def bars(start, n, base=100.0, vol=1.0):
    idx = pd.date_range(start, periods=n, freq='h', name='t')
    c = base + np.arange(n, dtype=float)
    return pd.DataFrame({'open': c - 0.5, 'high': c + 1, 'low': c - 1, 'close': c, 'volume': vol}, index=idx)


def test_empty():
    assert store.load_bars('XAUUSD').empty
    assert store.data_version('XAUUSD') == 0
    assert store.last_tick('XAUUSD') is None


def test_add_bumps_version_and_cache():
    assert store.add_bars('XAUUSD', bars('2026-01-05', 10)) is True
    v1 = store.data_version('XAUUSD')
    assert v1 >= 1
    a = store.load_bars('XAUUSD')
    assert len(a) == 10 and a.index.tz is None and a.index.is_monotonic_increasing and a.index.is_unique
    assert list(a.columns) == ['open', 'high', 'low', 'close', 'volume']
    # identical data again: no change, no bump
    assert store.add_bars('XAUUSD', bars('2026-01-05', 10)) is False
    assert store.data_version('XAUUSD') == v1
    # new bars: bump
    assert store.add_bars('XAUUSD', bars('2026-01-05 10:00', 3, base=110)) is True
    assert store.data_version('XAUUSD') == v1 + 1
    assert len(store.load_bars('XAUUSD')) == 13
    # value correction of an existing bar: replaced and bumped
    assert store.add_bars('XAUUSD', bars('2026-01-05 12:00', 1, base=999)) is True
    assert store.load_bars('XAUUSD')['close'].iloc[-1] == 999


def test_filler_and_dups_removed():
    df = pd.concat([bars('2026-01-05', 3), bars('2026-01-05 03:00', 2, vol=0.0), bars('2026-01-05 02:00', 1, base=500)])
    store.add_bars('EURUSD', df)
    out = store.load_bars('EURUSD')
    assert len(out) == 3 and out['close'].iloc[-1] == 500
    assert (out['volume'] > 0).all()


def test_cache_is_fast_and_isolated():
    store.add_bars('XAUUSD', bars('2020-01-01', 50000))
    store.load_bars('XAUUSD')
    t = time.perf_counter()
    for _ in range(200):
        df = store.load_bars('XAUUSD')
    assert (time.perf_counter() - t) / 200 < 0.005
    df['junk'] = 1  # adding a column must not leak into the cache
    assert 'junk' not in store.load_bars('XAUUSD').columns


def test_persistence_restart():
    store.add_bars('XAUUSD', bars('2026-01-05', 20))
    for d in (store._series, store._version, store._view):
        d.clear()
    store._loaded.clear()
    assert len(store.load_bars('XAUUSD')) == 20
    assert store.data_version('XAUUSD') >= 1
    assert not [f for f in os.listdir(os.path.join(config.DATA_DIR, 'bars')) if f.endswith('.tmp')]


def test_mt5_series_only_effective_in_mt5_mode(monkeypatch):
    store.add_bars('XAUUSD', bars('2026-01-05', 100, base=100))
    v = store.data_version('XAUUSD')
    store.add_bars('XAUUSD', bars('2026-01-07', 10, base=500), 'mt5')
    assert store.data_version('XAUUSD') == v and len(store.load_bars('XAUUSD')) == 100  # dukascopy mode: ignored
    assert store.mt5_last_bar('XAUUSD') == int(pd.Timestamp('2026-01-07 09:00').timestamp())
    monkeypatch.setattr(config, 'PRICE_SOURCE', 'mt5')
    df = store.load_bars('XAUUSD')
    first_mt5 = pd.Timestamp('2026-01-07')
    assert df.index.is_unique and df.index.is_monotonic_increasing
    assert (df.loc[df.index < first_mt5, 'close'] < 300).all() and (df.loc[df.index >= first_mt5, 'close'] > 400).all()
    v2 = store.data_version('XAUUSD')
    store.add_bars('XAUUSD', bars('2026-01-07 10:00', 1, base=600), 'mt5')
    assert store.data_version('XAUUSD') == v2 + 1 and store.load_bars('XAUUSD')['close'].iloc[-1] == 600


def test_last_tick_priority():
    store.add_bars('XAUUSD', bars('2026-01-05', 2, base=100))
    t = store.last_tick('XAUUSD')
    assert t['source'] == 'dukascopy' and t['bid'] == 101 and t['ask'] is None and t['t'] == pd.Timestamp('2026-01-05 02:00').timestamp()
    store.set_duka_tick('XAUUSD', 1000.0, 5.0, 5.1)
    assert store.last_tick('XAUUSD') == {'t': 1000.0, 'bid': 5.0, 'ask': 5.1, 'source': 'dukascopy'}
    store.set_mt5_tick('XAUUSD', 2000.0, 7.0, 7.2)
    assert store.last_tick('XAUUSD') == {'t': 2000.0, 'bid': 7.0, 'ask': 7.2, 'source': 'mt5'}
    store._mt5_tick['XAUUSD']['recv'] -= 121  # stale push -> falls back
    assert store.last_tick('XAUUSD')['source'] == 'dukascopy'


def test_specs_defaults_and_mt5_override():
    s = store.specs('USDJPY')
    assert s == {'contract': 100000.0, 'quote': 'JPY', 'volume_min': 0.01, 'volume_step': 0.01, 'tick_size': None,
                 'tick_value': None, 'source': 'defaut'}
    store.set_mt5_specs([{'id': 'USDJPY', 'contract_size': 100000, 'tick_size': 0.001, 'tick_value': 0.63,
                          'currency_profit': 'JPY', 'volume_min': 0.1, 'volume_step': 0.1, 'digits': 3}, {'id': 'NOPE'}])
    s = store.specs('USDJPY')
    assert s['source'] == 'mt5' and s['volume_min'] == 0.1 and s['tick_value'] == 0.63
    store._mt5_specs = None  # persisted
    assert store.specs('USDJPY')['tick_size'] == 0.001


def test_status_shape():
    store.add_bars('XAUUSD', bars('2026-01-05', 5))
    s = store.status()
    assert set(s) == {'instruments', 'feeds'}
    assert set(s['instruments']) == {x['id'] for x in config.INSTRUMENTS}
    g = s['instruments']['XAUUSD']
    assert set(g) == {'ready', 'progress', 'bootstrapping', 'bars', 'first_bar', 'last_bar', 'last_error'}
    assert g['ready'] is True and g['bars'] == 5 and g['progress'] == 1.0
    e = s['instruments']['EURUSD']
    assert e['ready'] is False and e['first_bar'] is None and e['last_bar'] is None and e['progress'] == 0.0
    assert set(s['feeds']['dukascopy']) == {'ok', 'last_run', 'last_error'} and s['feeds']['dukascopy']['ok'] is False
    assert s['feeds']['mt5'] == {'connected': False, 'last_seen': None}
    store.set_mt5_tick('XAUUSD', 1.0, 1.0)
    assert store.status()['feeds']['mt5']['connected'] is True
