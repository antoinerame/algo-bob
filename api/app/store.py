"""Parquet store of closed H1 bars (+ last ticks, MT5 specs, feed status). Thread-safe, atomic writes.

Two bar series per instrument: 'dukascopy' (DATA_DIR/bars/ID.parquet) and 'mt5' (ID.mt5.parquet, pushed by the bridge).
load_bars() serves the effective series: dukascopy, unless config.PRICE_SOURCE == 'mt5' and MT5 bars exist, in which
case Dukascopy history before the first MT5 bar is followed by the MT5 bars.
"""
import json
import logging
import os
import threading
import time

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger('store')

COLS = ['open', 'high', 'low', 'close', 'volume']
MT5_FRESH_S = 120
_lock = threading.RLock()
_series = {}    # (iid, source) -> DataFrame
_loaded = set()
_version = {}   # iid -> int
_view = {}      # iid -> (version, source_key, DataFrame)
_mt5_tick = {}  # iid -> {'t','bid','ask','recv'}
_duka_tick = {}
_mt5_specs = None
_state = None
_prog = {}      # iid -> {'bootstrapping','progress','last_error','ready'}
_feeds = {'dukascopy': {'ok': False, 'last_run': None, 'last_error': None}, 'mt5': {'last_seen': None}}


def _dir():
    return os.path.join(config.DATA_DIR, 'bars')


def _path(iid, source):
    return os.path.join(_dir(), f'{iid}.parquet' if source == 'dukascopy' else f'{iid}.mt5.parquet')


def _atomic(path, writer):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    writer(tmp)
    os.replace(tmp, path)


def _write_df(path, df):
    _atomic(path, lambda tmp: df.reset_index().to_parquet(tmp, index=False))


def _write_json(name, obj):
    def w(tmp):
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f)
    _atomic(os.path.join(config.DATA_DIR, name), w)


def _read_json(name, default):
    try:
        with open(os.path.join(config.DATA_DIR, name), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def empty():
    return pd.DataFrame(columns=COLS, dtype=float, index=pd.DatetimeIndex([], name='t'))


def clean(df):
    """Normalise: float OHLCV, naive UTC DatetimeIndex 't', sorted, unique, no NaN, no zero-volume filler."""
    if df is None or len(df) == 0:
        return empty()
    df = df[COLS].astype(float)
    df.index = pd.DatetimeIndex(df.index, name='t')
    if df.index.tz is not None:
        df.index = df.index.tz_convert('UTC').tz_localize(None)
    df = df[~df.index.duplicated(keep='last')].sort_index()
    df = df.dropna()
    return df[df['volume'] > 0]


def _ensure(iid, source):
    k = (iid, source)
    if k in _loaded:
        return _series[k]
    df = empty()
    try:
        p = _path(iid, source)
        if os.path.exists(p):
            raw = pd.read_parquet(p)
            raw['t'] = pd.to_datetime(raw['t'])
            df = clean(raw.set_index('t'))
    except Exception as e:
        log.error('cannot read %s: %s', k, e)
    _series[k] = df
    _loaded.add(k)
    if source == 'dukascopy' or iid not in _version:
        _version[iid] = max(_version.get(iid, 0), 1 if len(df) else 0)
    return df


def _effective_key(iid):
    mt5 = _ensure(iid, 'mt5')
    return 'mt5' if (config.PRICE_SOURCE == 'mt5' and len(mt5)) else 'dukascopy'


def _changed(old, new):
    """True if `new` adds rows to `old` or modifies existing ones."""
    if old.empty:
        return len(new) > 0
    miss = ~new.index.isin(old.index)
    if miss.any():
        return True
    o = old.reindex(new.index)
    return not np.allclose(o.values, new.values, rtol=0, atol=1e-9)


def add_bars(iid, df, source='dukascopy', replace=True):
    """Merge CLOSED H1 bars into a series. Returns True if anything changed (data_version bumped when it affects load_bars)."""
    new = clean(df)
    if new.empty:
        return False
    with _lock:
        old = _ensure(iid, source)
        before = _effective_key(iid)
        if replace:
            if not _changed(old, new):
                return False
            merged = new if old.empty else pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
        else:
            add = new[~new.index.isin(old.index)]
            if add.empty:
                return False
            merged = add if old.empty else pd.concat([old, add]).sort_index()
        _series[(iid, source)] = merged
        _write_df(_path(iid, source), merged)
        after = _effective_key(iid)
        if source == after or after != before or (after == 'mt5' and new.index[0] < _series[(iid, 'mt5')].index[0]):
            _version[iid] = _version.get(iid, 0) + 1
        return True


def load_bars(iid):
    """CLOSED H1 bars (open/high/low/close/volume floats), tz-naive UTC index = bar open time. Cached per data_version.
    The returned frame shares memory with the cache: do not modify values in place."""
    with _lock:
        _ensure(iid, 'dukascopy')
        key = _effective_key(iid)
        v = _version.get(iid, 0)
        c = _view.get(iid)
        if c and c[0] == v and c[1] == key:
            return c[2].copy(deep=False)
        d = _series[(iid, 'dukascopy')]
        if key == 'mt5':
            m = _series[(iid, 'mt5')]
            df = pd.concat([d[d.index < m.index[0]], m])
        else:
            df = d
        _view[iid] = (v, key, df)
        return df.copy(deep=False)


def data_version(iid):
    with _lock:
        _ensure(iid, 'dukascopy')
        _effective_key(iid)
        return _version.get(iid, 0)


def mt5_last_bar(iid):
    """Epoch seconds of the newest MT5 bar pushed (None if none) - the bridge uses it to know what to backfill."""
    with _lock:
        m = _ensure(iid, 'mt5')
        return int(m.index[-1].timestamp()) if len(m) else None


def has_mt5(iid):
    with _lock:
        return len(_ensure(iid, 'mt5')) > 0


def _ts(df, i):
    return int(df.index[i].timestamp())


# --- ticks -----------------------------------------------------------------

def set_mt5_tick(iid, t, bid, ask=None):
    with _lock:
        _mt5_tick[iid] = {'t': float(t), 'bid': float(bid), 'ask': None if ask is None else float(ask), 'recv': time.time()}
        _feeds['mt5']['last_seen'] = time.time()


def set_duka_tick(iid, t, bid, ask=None):
    with _lock:
        cur = _duka_tick.get(iid)
        if cur is None or t >= cur['t']:
            _duka_tick[iid] = {'t': float(t), 'bid': float(bid), 'ask': None if ask is None else float(ask)}


def last_tick(iid):
    """MT5 tick if pushed within the last 120 s, else last Dukascopy tick seen, else last closed bar close."""
    with _lock:
        m = _mt5_tick.get(iid)
        if m and time.time() - m['recv'] <= MT5_FRESH_S:
            return {'t': m['t'], 'bid': m['bid'], 'ask': m['ask'], 'source': 'mt5'}
        d = _duka_tick.get(iid)
        if d:
            return {'t': d['t'], 'bid': d['bid'], 'ask': d['ask'], 'source': 'dukascopy'}
    df = load_bars(iid)
    if len(df):
        return {'t': _ts(df, -1) + 3600, 'bid': float(df['close'].iloc[-1]), 'ask': None, 'source': 'dukascopy'}
    return None


# --- specs -----------------------------------------------------------------

def _specs_pushed():
    global _mt5_specs
    if _mt5_specs is None:
        _mt5_specs = _read_json('specs.json', {})
    return _mt5_specs


def set_mt5_specs(specs):
    """specs: list of {id, contract_size, tick_size, tick_value, currency_profit, volume_min, volume_step, digits}."""
    with _lock:
        cur = _specs_pushed()
        for s in specs:
            iid = s.get('id')
            if iid in config.BY_ID:
                cur[iid] = {k: v for k, v in s.items() if k != 'id' and v is not None}
        _write_json('specs.json', cur)
        _feeds['mt5']['last_seen'] = time.time()


def specs(iid):
    x = config.BY_ID[iid]
    out = {'contract': float(x['contract']), 'quote': x['quote'], 'volume_min': 0.01, 'volume_step': 0.01,
           'tick_size': None, 'tick_value': None, 'source': 'defaut'}
    with _lock:
        p = _specs_pushed().get(iid)
    if p:
        for src, dst in (('contract_size', 'contract'), ('currency_profit', 'quote'), ('volume_min', 'volume_min'),
                         ('volume_step', 'volume_step'), ('tick_size', 'tick_size'), ('tick_value', 'tick_value'),
                         ('digits', 'digits')):
            if p.get(src) not in (None, '', 0):
                out[dst] = p[src]
        out['source'] = 'mt5'
    return out


# --- state / status ---------------------------------------------------------

def get_state(key, default=None):
    with _lock:
        global _state
        if _state is None:
            _state = _read_json('state.json', {})
        return _state.get(key, default)


def set_state(key, value):
    with _lock:
        global _state
        if _state is None:
            _state = _read_json('state.json', {})
        _state[key] = value
        _write_json('state.json', _state)


def set_progress(iid, **kw):
    """bootstrapping: bool, progress: 0..1, last_error: str|None, ready: bool."""
    with _lock:
        _prog.setdefault(iid, {}).update(kw)


def set_feed(name, **kw):
    with _lock:
        _feeds[name].update(kw)


def status():
    with _lock:
        inst = {}
        for x in config.INSTRUMENTS:
            iid = x['id']
            df = load_bars(iid)
            p = _prog.get(iid, {})
            boot = bool(p.get('bootstrapping', False))
            n = len(df)
            inst[iid] = {'ready': bool(n and p.get('ready', True)), 'bars': n,
                         'first_bar': _ts(df, 0) if n else None, 'last_bar': _ts(df, -1) if n else None,
                         'bootstrapping': boot, 'progress': float(p.get('progress', 1.0 if n else 0.0)),
                         'last_error': p.get('last_error')}
        seen = _feeds['mt5']['last_seen']
        return {'instruments': inst,
                'feeds': {'dukascopy': {'ok': bool(_feeds['dukascopy'].get('ok')), 'last_run': _feeds['dukascopy'].get('last_run'),
                                        'last_error': _feeds['dukascopy'].get('last_error')},
                          'mt5': {'connected': bool(seen and time.time() - seen <= MT5_FRESH_S), 'last_seen': seen}}}
