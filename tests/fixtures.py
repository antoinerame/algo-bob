"""Test helpers: H1 bars from the v1 site fixture (XAUUSD, 2026) and a fake `store`."""
import json
import os
import sys
import types

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api'))


def v1_json(iid='XAUUSD'):
    with open(os.path.join(ROOT, 'site', 'data', f'{iid}.json'), encoding='utf-8') as f:
        return json.load(f)


def fixture_bars(iid='XAUUSD') -> pd.DataFrame:
    c = v1_json(iid)['candles']
    a = np.array(c, dtype=float)
    idx = pd.to_datetime(a[:, 0].astype('int64'), unit='s')
    return pd.DataFrame({'open': a[:, 1], 'high': a[:, 2], 'low': a[:, 3], 'close': a[:, 4], 'volume': 1.0}, index=idx)


def synth_bars(n=2000, seed=1, start='2020-01-01', price=100.0, vol=0.002) -> pd.DataFrame:
    """Random-walk H1 bars (weekdays only)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n * 2, freq='h')
    idx = idx[idx.dayofweek < 5][:n]
    r = rng.normal(0, vol, n)
    c = price * np.exp(np.cumsum(r))
    o = np.r_[price, c[:-1]]
    wick = np.abs(rng.normal(0, vol / 2, (2, n))) * c
    return pd.DataFrame({'open': o, 'high': np.maximum(o, c) + wick[0], 'low': np.minimum(o, c) - wick[1],
                         'close': c, 'volume': 1.0}, index=idx)


class FakeStore(types.ModuleType):
    """In-memory replacement of app.store (same function names as ARCHITECTURE.md)."""

    def __init__(self):
        super().__init__('app.store')
        self.bars, self.ver, self.ticks, self._specs = {}, {}, {}, {}

    def put(self, iid, df, ver=1):
        self.bars[iid], self.ver[iid] = df, ver

    def load_bars(self, iid):
        return self.bars.get(iid, pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume']))

    def data_version(self, iid):
        return self.ver.get(iid, 0)

    def last_tick(self, iid):
        return self.ticks.get(iid)

    def specs(self, iid):
        from app import config
        base = dict(contract=config.BY_ID[iid]['contract'], quote=config.BY_ID[iid]['quote'], volume_min=0.01,
                    volume_step=0.01, tick_size=None, tick_value=None, source='defaut')
        base.update(self._specs.get(iid, {}))
        return base

    def status(self):
        return {}
