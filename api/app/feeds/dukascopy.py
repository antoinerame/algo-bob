"""Dukascopy datafeed client (free, no account): monthly H1 candles, daily M1 candles, hourly ticks.

Files are LZMA (.bi5). Candle record = '>5if' (seconds from file start, open, close, low, high, volume);
tick record = '>3i2f' (ms from hour start, ask, bid, ask_vol, bid_vol). Prices are ints / point.
URL months are 0-based. 404 = no data (weekend, holiday, future) -> None.
"""
import logging
import lzma
import random
import struct
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .. import config

log = logging.getLogger('dukascopy')

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
MAX_CONCURRENT = 4
RETRIES = 9
TIMEOUT = 40
BREAKER_S = 120
_down_until, _down_why = [0.0], ['']
COLS = ['open', 'high', 'low', 'close', 'volume']
CANDLE = struct.Struct('>5if')
TICK = struct.Struct('>3i2f')

_sem = threading.BoundedSemaphore(MAX_CONCURRENT)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # http -> https redirect is a (transient) refusal here, never follow
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class FeedError(Exception):
    pass


def fetch(url, retries=RETRIES):
    """Raw bytes (possibly b'' for an empty file), None on 404. Retries with backoff, else raises FeedError.
    Circuit breaker: after 3 failures in a row the service is considered down for BREAKER_S and every fetch fails
    at once, so an outage (503 all weekend, 301 to the blocked https) ends a pass in seconds instead of hours."""
    last = None
    for k in range(retries):
        if time.time() < _down_until[0]:
            raise FeedError(f'Dukascopy indisponible ({_down_why[0]}) <- {url}')
        if k:
            time.sleep(min(20, 1.5 * 2 ** (k - 1)) * (0.7 + 0.6 * random.random()))
        try:
            with _sem:
                req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
                with _opener.open(req, timeout=TIMEOUT) as r:
                    return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = f'HTTP {e.code}'
        except Exception as e:  # timeouts, resets, DNS...
            last = f'{type(e).__name__}: {e}'
        log.debug('retry %s (%s) %s', k + 1, last, url)
        if k >= 2:
            _down_until[0], _down_why[0] = time.time() + BREAKER_S, last
    raise FeedError(f'{last} <- {url}')


def _lzma(raw):
    return lzma.decompress(raw) if raw else b''


def _empty():
    return pd.DataFrame(columns=COLS, dtype=float, index=pd.DatetimeIndex([], name='t'))


def decode_candles(raw, start, point):
    """Candle bytes (compressed) -> OHLCV frame indexed by open time (naive UTC). `start` = file start (datetime)."""
    d = _lzma(raw)
    n = len(d) // CANDLE.size
    if n == 0:
        return _empty()
    a = np.array(list(CANDLE.iter_unpack(d[:n * CANDLE.size])), dtype=np.float64)
    start = pd.Timestamp(start).tz_localize(None) if pd.Timestamp(start).tzinfo else pd.Timestamp(start)
    idx = pd.DatetimeIndex(start + pd.to_timedelta(a[:, 0], unit='s'), name='t')
    # record order is O, C, L, H
    return pd.DataFrame({'open': a[:, 1] / point, 'high': a[:, 4] / point, 'low': a[:, 3] / point,
                         'close': a[:, 2] / point, 'volume': a[:, 5]}, index=idx)


def decode_ticks(raw, hour_start, point):
    """Tick bytes (compressed) -> frame indexed by tick time with columns ask, bid, ask_vol, bid_vol."""
    d = _lzma(raw)
    n = len(d) // TICK.size
    if n == 0:
        return pd.DataFrame(columns=['ask', 'bid', 'ask_vol', 'bid_vol'], dtype=float, index=pd.DatetimeIndex([], name='t'))
    a = np.array(list(TICK.iter_unpack(d[:n * TICK.size])), dtype=np.float64)
    idx = pd.DatetimeIndex(pd.Timestamp(hour_start) + pd.to_timedelta(a[:, 0], unit='ms'), name='t')
    return pd.DataFrame({'ask': a[:, 1] / point, 'bid': a[:, 2] / point, 'ask_vol': a[:, 3], 'bid_vol': a[:, 4]}, index=idx)


def drop_filler(df):
    """Remove weekend/holiday filler bars: zero volume (these are also flat o=h=l=c)."""
    if df.empty:
        return df
    return df[df['volume'] > 0]


def resample_h1(df):
    """Finer candles (M1) -> H1, hour label = open time. Hours without any traded minute are dropped."""
    df = drop_filler(df)
    if df.empty:
        return _empty()
    g = df.groupby(df.index.floor('h'))
    out = pd.DataFrame({'open': g['open'].first(), 'high': g['high'].max(), 'low': g['low'].min(),
                        'close': g['close'].last(), 'volume': g['volume'].sum()})
    out.index.name = 't'
    return out


def ticks_to_h1(ticks):
    """Ticks (one or several hours) -> H1 bars from BID. Volume = sum of bid volumes."""
    if ticks.empty:
        return _empty()
    g = ticks.groupby(ticks.index.floor('h'))
    out = pd.DataFrame({'open': g['bid'].first(), 'high': g['bid'].max(), 'low': g['bid'].min(),
                        'close': g['bid'].last(), 'volume': g['bid_vol'].sum()})
    out.index.name = 't'
    return out


def _u(duka, y, m, rest):
    return f'{config.DUKASCOPY_URL}/{duka}/{y:04d}/{m - 1:02d}/{rest}'


def month_url(duka, y, m):
    return _u(duka, y, m, 'BID_candles_hour_1.bi5')


def day_url(duka, d):
    return _u(duka, d.year, d.month, f'{d.day:02d}/BID_candles_min_1.bi5')


def hour_url(duka, h):
    return _u(duka, h.year, h.month, f'{h.day:02d}/{h.hour:02d}h_ticks.bi5')


def fetch_month_h1(inst, y, m):
    """Monthly H1 file -> frame (filler dropped); None if the file does not exist (month not over / no data)."""
    raw = fetch(month_url(inst['duka'], y, m))
    if raw is None:
        return None
    return drop_filler(decode_candles(raw, datetime(y, m, 1), inst['point']))


def fetch_day_h1(inst, d):
    """Daily M1 file resampled to H1; None if the file does not exist."""
    raw = fetch(day_url(inst['duka'], d))
    if raw is None:
        return None
    return resample_h1(decode_candles(raw, datetime(d.year, d.month, d.day), inst['point']))


def fetch_hour_ticks(inst, h):
    """Tick frame of hour `h` (naive UTC datetime, on the hour); None if the file does not exist (yet)."""
    raw = fetch(hour_url(inst['duka'], h))
    if raw is None:
        return None
    return decode_ticks(raw, h, inst['point'])


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def month_start(t):
    return datetime(t.year, t.month, 1)


def next_month(t):
    return datetime(t.year + (t.month == 12), t.month % 12 + 1, 1)
