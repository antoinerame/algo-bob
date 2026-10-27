import asyncio
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from app import config, store, updater  # noqa: E402
from app.feeds import dukascopy as dk  # noqa: E402


def px(h):
    return 100.0 + (h - datetime(2026, 1, 1)).total_seconds() / 3600 * 0.01


def trading(h):
    return h.weekday() < 5


def bar(h):
    p = px(h)
    return (h, p, p + 1, p - 1, p + 0.5, 4.0)


def frame(hours):
    rows = [bar(h) for h in hours if trading(h)]
    if not rows:
        return dk._empty()
    return pd.DataFrame([r[1:] for r in rows], columns=dk.COLS, index=pd.DatetimeIndex([r[0] for r in rows], name='t'))


class FakeFeed:
    """Synthetic Dukascopy: monthly files exist for finished months, daily files for finished days, tick files 3 min after the hour."""

    def __init__(self, now):
        self.now = now
        self.calls = []
        self.fail = set()

    def month(self, inst, y, m):
        self.calls.append(('month', inst['id'], y, m))
        if inst['id'] in self.fail:
            raise dk.FeedError('boom')
        a = datetime(y, m, 1)
        b = dk.next_month(a)
        if b > datetime(self.now.year, self.now.month, 1):
            return None
        return frame([a + timedelta(hours=i) for i in range(int((b - a).total_seconds() // 3600))])

    def day(self, inst, d):
        self.calls.append(('day', inst['id'], d))
        if inst['id'] in self.fail:
            raise dk.FeedError('boom')
        if d >= self.now.replace(hour=0, minute=0, second=0, microsecond=0):
            return None
        return frame([d + timedelta(hours=i) for i in range(24)]) if trading(d) else None

    def ticks(self, inst, h):
        self.calls.append(('ticks', inst['id'], h))
        if inst['id'] in self.fail:
            raise dk.FeedError('boom')
        if h + timedelta(hours=1, minutes=3) > self.now or not trading(h):
            return None
        p = px(h)
        idx = pd.DatetimeIndex([h, h + timedelta(seconds=1), h + timedelta(seconds=2), h + timedelta(seconds=3)])
        return pd.DataFrame({'ask': [p + .01, p + 1.01, p - .99, p + .51], 'bid': [p, p + 1, p - 1, p + .5],
                             'ask_vol': 1.0, 'bid_vol': 1.0}, index=idx)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(config, 'PRICE_SOURCE', 'dukascopy')
    monkeypatch.setattr(config, 'HISTORY_START', '2026-01-01')
    monkeypatch.setattr(config, 'INSTRUMENTS', config.INSTRUMENTS[:3])
    for d in (store._series, store._version, store._view, store._mt5_tick, store._duka_tick, store._prog, updater._done):
        d.clear()
    store._loaded.clear()
    store._state = None
    store._mt5_specs = None
    feed = FakeFeed(datetime(2026, 3, 10, 14, 30))
    monkeypatch.setattr(dk, 'fetch_month_h1', feed.month)
    monkeypatch.setattr(dk, 'fetch_day_h1', feed.day)
    monkeypatch.setattr(dk, 'fetch_hour_ticks', feed.ticks)
    events = []
    monkeypatch.setattr(updater.bus, 'publish', lambda t, d: events.append((t, d)))
    return feed, events


def test_bootstrap_then_refresh(env):
    feed, events = env
    inst = config.INSTRUMENTS[0]
    assert updater.sync_instrument(inst, feed.now) is True
    df = store.load_bars('XAUUSD')
    assert df.index[0] == pd.Timestamp('2026-01-01 00:00')
    assert df.index[-1] == pd.Timestamp('2026-03-10 13:00')     # closed hours only: no forming 14:00 bar
    assert df.index.is_unique and df.index.is_monotonic_increasing
    assert not (df.index.weekday >= 5).any()                     # no weekend filler
    assert ('bar', {'id': 'XAUUSD', 't': int(pd.Timestamp('2026-03-10 13:00').timestamp())}) in events
    assert store.status()['instruments']['XAUUSD']['ready'] is True
    assert store.last_tick('XAUUSD')['source'] == 'dukascopy' and store.last_tick('XAUUSD')['bid'] == pytest.approx(px(datetime(2026, 3, 10, 13)) + .5)
    v = store.data_version('XAUUSD')

    # 14:00 file not published yet at 15:01 -> nothing new, no error
    feed.now = datetime(2026, 3, 10, 15, 1)
    events.clear()
    assert updater.sync_instrument(inst, feed.now) is False
    assert store.data_version('XAUUSD') == v and not events
    # published at 15:03 -> 14:00 bar appears, version bumps, bar event emitted
    feed.now = datetime(2026, 3, 10, 15, 4)
    assert updater.sync_instrument(inst, feed.now) is True
    assert store.load_bars('XAUUSD').index[-1] == pd.Timestamp('2026-03-10 14:00')
    assert store.data_version('XAUUSD') == v + 1
    assert events == [('bar', {'id': 'XAUUSD', 't': int(pd.Timestamp('2026-03-10 14:00').timestamp())})]


def test_restart_fetches_only_missing(env):
    feed, _ = env
    inst = config.INSTRUMENTS[0]
    updater.sync_instrument(inst, feed.now)
    updater._done.clear()  # simulated restart: in-memory pointer lost
    for d in (store._series, store._version, store._view):
        d.clear()
    store._loaded.clear()
    store._state = None
    feed.calls.clear()
    feed.now = datetime(2026, 3, 10, 16, 5)
    assert updater.sync_instrument(inst, feed.now) is True
    kinds = {c[0] for c in feed.calls}
    assert 'month' not in kinds                                  # nothing before the last bar is re-downloaded
    assert store.load_bars('XAUUSD').index[-1] == pd.Timestamp('2026-03-10 15:00')


def test_month_rollover_and_weekend(env):
    feed, _ = env
    inst = config.INSTRUMENTS[0]
    feed.now = datetime(2026, 3, 29, 10, 0)  # Sunday
    updater.sync_instrument(inst, feed.now)
    last = store.load_bars('XAUUSD').index[-1]
    assert last == pd.Timestamp('2026-03-27 23:00')               # Friday, nothing on the weekend
    feed.now = datetime(2026, 4, 1, 2, 30)
    updater.sync_instrument(inst, feed.now)
    df = store.load_bars('XAUUSD')
    assert df.index[-1] == pd.Timestamp('2026-04-01 01:00') and df.index.is_unique


def test_failing_instrument_does_not_stop_others(env):
    feed, events = env
    feed.fail.add('XAUUSD')
    out = updater.run_pass(feed.now)
    assert 'XAUUSD' not in out and set(out) == {'XAGUSD', 'EURUSD'}
    s = store.status()
    assert s['instruments']['XAUUSD']['last_error'] and s['instruments']['XAUUSD']['ready'] is False
    assert s['instruments']['XAGUSD']['ready'] is True and s['instruments']['XAGUSD']['last_error'] is None
    assert s['feeds']['dukascopy']['ok'] is False and 'XAUUSD' in s['feeds']['dukascopy']['last_error']
    assert any(t == 'status' for t, _ in events)
    feed.fail.clear()                                             # network back: next pass completes it
    out = updater.run_pass(feed.now)
    assert out == ['XAUUSD'] and store.status()['feeds']['dukascopy']['ok'] is True


def test_gold_first():
    assert updater.order()[0]['id'] == 'XAUUSD'


def test_mt5_mode_skips_dukascopy_when_mt5_has_bars(env, monkeypatch):
    feed, _ = env
    inst = config.INSTRUMENTS[0]
    monkeypatch.setattr(config, 'PRICE_SOURCE', 'mt5')
    updater.sync_instrument(inst, feed.now)                       # no MT5 bars yet: bootstraps from Dukascopy
    assert len(store.load_bars('XAUUSD')) > 1000
    store.add_bars('XAUUSD', frame([datetime(2026, 3, 9, 10)]), 'mt5')
    feed.calls.clear()
    feed.now = datetime(2026, 3, 10, 18, 0)
    assert updater.sync_instrument(inst, feed.now) is False
    assert feed.calls == []


def test_run_updater_loop_survives(env, monkeypatch):
    feed, _ = env
    monkeypatch.setattr(config, 'REFRESH_SECONDS', 0.05)
    monkeypatch.setattr(dk, 'utcnow', lambda: feed.now)
    n = {'passes': 0}
    real = updater.run_pass

    def flaky(now=None):
        n['passes'] += 1
        if n['passes'] == 1:
            raise RuntimeError('unexpected')
        return real(now)
    monkeypatch.setattr(updater, 'run_pass', flaky)

    async def main():
        t = asyncio.create_task(updater.run_updater())
        for _ in range(100):
            await asyncio.sleep(0.1)
            if store.status()['instruments']['XAUUSD']['ready']:
                break
        t.cancel()
    asyncio.run(main())
    assert n['passes'] >= 2 and store.status()['instruments']['XAUUSD']['ready']


def test_backfill_back_is_newest_first_and_resumable(env, monkeypatch):
    feed, events = env
    inst = config.INSTRUMENTS[0]
    updater.sync_instrument(inst, feed.now)               # history from 2026-01
    monkeypatch.setattr(config, 'HISTORY_START', '2025-01-01')
    real = feed.month
    calls = []

    def month(i, y, m):                                   # interrupt after the first batch
        calls.append((y, m))
        if len(calls) > updater.CHUNK:
            raise dk.FeedError('coupure')
        return real(i, y, m)
    monkeypatch.setattr(dk, 'fetch_month_h1', month)
    with pytest.raises(dk.FeedError):
        updater.backfill_back(inst)
    assert calls[0] == (2025, 12)                         # newest month first
    first = store.load_bars(inst['id']).index[0]
    assert first == pd.Timestamp('2025-05-01') or first.month == 5   # contiguous: Dec..May stored
    monkeypatch.setattr(dk, 'fetch_month_h1', real)
    updater.backfill_back(inst)                           # resume
    df = store.load_bars(inst['id'])
    assert df.index[0] < pd.Timestamp('2025-01-03')
    assert df.index.to_series().diff().max() <= pd.Timedelta(days=4)   # no hole (weekends only)
    assert store.get_state('covered_from')[inst['id']] == '2025-01-01'


def test_backfill_respects_deadline(env, monkeypatch):
    feed, events = env
    inst = config.INSTRUMENTS[0]
    updater.sync_instrument(inst, feed.now)
    monkeypatch.setattr(config, 'HISTORY_START', '2024-01-01')
    updater.backfill_back(inst, deadline=0.0001)          # already past: nothing fetched, not marked covered
    assert store.load_bars(inst['id']).index[0] >= pd.Timestamp('2026-01-01')
    assert store.get_state('covered_from')[inst['id']] != '2024-01-01'


def test_weekend_hours_not_fetched():
    assert updater._weekend(datetime(2026, 10, 24, 12))          # Saturday
    assert updater._weekend(datetime(2026, 10, 23, 22))          # Friday 22:00
    assert not updater._weekend(datetime(2026, 10, 23, 21))      # Friday 21:00 (last hour)
    assert updater._weekend(datetime(2026, 10, 25, 20))          # Sunday 20:00
    assert not updater._weekend(datetime(2026, 10, 25, 21))      # Sunday 21:00 (reopening, summer)
