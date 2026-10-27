import lzma
import os
import struct
import sys
import urllib.error
from datetime import datetime

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from app import config  # noqa: E402
from app.feeds import dukascopy as dk  # noqa: E402

XAU = config.BY_ID['XAUUSD']


def candles_bi5(rows):  # rows: (sec, o, h, l, c, vol) -> file order is O, C, L, H
    return lzma.compress(b''.join(struct.pack('>5if', s, round(o * 1e3), round(c * 1e3), round(l * 1e3), round(h * 1e3), v)
                                  for s, o, h, l, c, v in rows))


def ticks_bi5(rows):  # rows: (ms, ask, bid, av, bv)
    return lzma.compress(b''.join(struct.pack('>3i2f', ms, round(a * 1e3), round(b * 1e3), av, bv) for ms, a, b, av, bv in rows))


def test_decode_candles_order_and_point():
    raw = candles_bi5([(0, 4450.0, 4460.5, 4449.0, 4455.25, 12.5), (3600, 4455.25, 4456.0, 4440.0, 4441.0, 3.0)])
    df = dk.decode_candles(raw, datetime(2026, 8, 1), XAU['point'])
    assert list(df.index) == [pd.Timestamp('2026-08-01 00:00'), pd.Timestamp('2026-08-01 01:00')]
    r = df.iloc[0]
    assert (r.open, r.high, r.low, r.close) == (4450.0, 4460.5, 4449.0, 4455.25)
    assert r.volume == pytest.approx(12.5)
    assert df.index.tz is None


def test_weekend_filler_dropped():
    raw = candles_bi5([(0, 100, 100, 100, 100, 0.0), (3600, 100, 101, 99, 100.5, 2.0), (7200, 100.5, 100.5, 100.5, 100.5, 0.0)])
    df = dk.drop_filler(dk.decode_candles(raw, datetime(2026, 8, 2), 1e3))
    assert list(df.index) == [pd.Timestamp('2026-08-02 01:00')]


def test_m1_to_h1_resample():
    rows = [(0, 10, 12, 9, 11, 1.0), (60, 11, 15, 10, 14, 2.0), (120, 14, 14, 14, 14, 0.0),  # flat zero-volume minute ignored
            (3600, 20, 21, 19, 20.5, 1.0)]
    h = dk.resample_h1(dk.decode_candles(candles_bi5(rows), datetime(2026, 8, 3), 1e3))
    assert len(h) == 2
    a = h.iloc[0]
    assert (a.open, a.high, a.low, a.close, a.volume) == (10, 15, 9, 14, pytest.approx(3.0))
    assert h.index[1] == pd.Timestamp('2026-08-03 01:00')


def test_ticks_to_h1_bid():
    raw = ticks_bi5([(0, 5.001, 5.0, 1.0, 1.0), (1000, 5.011, 5.01, 1.0, 2.0), (2000, 4.991, 4.99, 1.0, 3.0), (3599000, 5.006, 5.005, 1.0, 4.0)])
    t = dk.decode_ticks(raw, datetime(2026, 8, 3, 14), 1e3)
    assert t.index[1] == pd.Timestamp('2026-08-03 14:00:01')
    h = dk.ticks_to_h1(t)
    assert len(h) == 1 and h.index[0] == pd.Timestamp('2026-08-03 14:00')
    r = h.iloc[0]
    assert (r.open, r.high, r.low, r.close) == (5.0, 5.01, 4.99, 5.005)
    assert r.volume == pytest.approx(10.0)


def test_empty_files():
    assert dk.decode_candles(b'', datetime(2026, 1, 1), 1e3).empty
    assert dk.decode_ticks(b'', datetime(2026, 1, 1), 1e3).empty


def test_urls_month_is_zero_based():
    assert dk.month_url('XAUUSD', 2026, 8).endswith('/XAUUSD/2026/07/BID_candles_hour_1.bi5')
    assert dk.day_url('XAUUSD', datetime(2026, 1, 5)).endswith('/XAUUSD/2026/00/05/BID_candles_min_1.bi5')
    assert dk.hour_url('XAUUSD', datetime(2026, 12, 31, 7)).endswith('/XAUUSD/2026/11/31/07h_ticks.bi5')


class _Resp:
    def __init__(self, b):
        self.b = b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return self.b


def test_fetch_retry_and_404(monkeypatch):
    monkeypatch.setattr(dk.time, 'sleep', lambda s: None)
    calls = []
    seq = [urllib.error.HTTPError('u', 301, 'moved', {}, None), urllib.error.HTTPError('u', 503, 'x', {}, None), b'data']

    def fake_open(req, timeout=0):
        calls.append(req.full_url)
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return _Resp(x)
    monkeypatch.setattr(dk._opener, 'open', fake_open)
    assert dk.fetch('http://x/a') == b'data' and len(calls) == 3

    def nf(req, timeout=0):
        raise urllib.error.HTTPError('u', 404, 'nf', {}, None)
    monkeypatch.setattr(dk._opener, 'open', nf)
    assert dk.fetch('http://x/b') is None

    def bad(req, timeout=0):
        raise urllib.error.HTTPError('u', 429, 'slow', {}, None)
    monkeypatch.setattr(dk._opener, 'open', bad)
    with pytest.raises(dk.FeedError):
        dk.fetch('http://x/c', retries=3)


@pytest.mark.skipif(not os.environ.get('NETWORK'), reason='set NETWORK=1 to hit datafeed.dukascopy.com')
def test_network_month():
    df = dk.fetch_month_h1(XAU, 2026, 3)
    assert df is not None and 400 < len(df) < 600 and (df['volume'] > 0).all()
    assert 3000 < df['close'].median() < 8000


def test_fetch_circuit_breaker(monkeypatch):
    import time as _t
    import urllib.error
    from app.feeds import dukascopy as dkm
    calls = []

    class Op:
        def open(self, req, timeout):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 503, 'down', {}, None)
    monkeypatch.setattr(dkm, '_opener', Op())
    monkeypatch.setattr(dkm.time, 'sleep', lambda s: None)
    monkeypatch.setattr(dkm, '_down_until', [0.0])
    with pytest.raises(dkm.FeedError):
        dkm.fetch('http://x/a')
    assert len(calls) == 3                       # 3 attempts, then the breaker opens
    with pytest.raises(dkm.FeedError, match='indisponible'):
        dkm.fetch('http://x/b')
    assert len(calls) == 3                       # no network while the breaker is open
    dkm._down_until[0] = _t.time() - 1           # breaker closes after BREAKER_S
    with pytest.raises(dkm.FeedError):
        dkm.fetch('http://x/c')
    assert len(calls) == 6
