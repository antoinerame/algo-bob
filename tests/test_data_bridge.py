import importlib.util
import os
import sys
import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(ROOT, 'api'))
from app import config, ingest, store  # noqa: E402

spec = importlib.util.spec_from_file_location('mt5_bridge', os.path.join(ROOT, 'bridge', 'mt5_bridge.py'))
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def test_dst_rule():
    assert br.rule_offset_h(utc(2026, 1, 15, 12)) == 2
    assert br.rule_offset_h(utc(2026, 7, 15, 12)) == 3
    assert br.rule_offset_h(utc(2026, 3, 7, 12)) == 2 and br.rule_offset_h(utc(2026, 3, 9, 12)) == 3   # US DST starts 8 Mar 2026
    assert br.rule_offset_h(utc(2026, 10, 30, 12)) == 3 and br.rule_offset_h(utc(2026, 11, 3, 12)) == 2  # ends 1 Nov
    assert br.rule_offset_h(utc(2026, 3, 30, 12)) == 3


def test_to_utc_follows_dst_per_bar():
    c = br.ServerClock()
    assert c.to_utc(utc(2026, 1, 15, 12) + 2 * 3600) == utc(2026, 1, 15, 12)
    assert c.to_utc(utc(2026, 7, 15, 12) + 3 * 3600) == utc(2026, 7, 15, 12)
    c = br.ServerClock(fixed=4)
    assert c.to_utc(utc(2026, 7, 15, 12) + 4 * 3600) == utc(2026, 7, 15, 12)


def test_calibration_needs_agreeing_fresh_ticks():
    now = utc(2026, 1, 15, 12)
    c = br.ServerClock()
    assert c.calibrate([now + 7200 - 3, now + 7200 - 1, now + 7200], now) == 2 and c.delta == 0
    c = br.ServerClock()
    assert c.calibrate([now + 3 * 3600 - 2, now + 3 * 3600], now) == 3 and c.delta == 1  # broker differs from the rule: kept
    c = br.ServerClock()
    assert c.calibrate([now + 7200 - 1000, now + 5555], now) is None                    # stale / disagreeing ticks
    assert br.ServerClock(fixed=5).calibrate([], now) == 5


def test_fake_bridge_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(config, 'PRICE_SOURCE', 'mt5')
    monkeypatch.delenv('INGEST_TOKEN', raising=False)
    for d in (store._series, store._version, store._view, store._mt5_tick, store._duka_tick, store._prog):
        d.clear()
    store._loaded.clear()
    store._mt5_specs = None
    events = []
    monkeypatch.setattr(ingest.bus, 'publish', lambda t, d: events.append((t, d)))
    app = FastAPI()
    app.include_router(ingest.router)
    client = TestClient(app)

    class LocalApi(br.Api):
        def _req(self, method, path, body=None):
            r = client.request(method, path, json=body)
            r.raise_for_status()
            return r.json()

    symbols = {k: v for k, v in br.FALLBACK_SYMBOLS.items() if k in ('XAUUSD', 'EURUSD', 'US500')}
    fake = br.FakeMT5(symbols)
    b = br.Bridge(fake, LocalApi('http://x'), symbols, br.ServerClock(), since=time.time() - 7 * 86400)   # 7 days: >= 5 weekdays whatever today is
    b.calibrate()
    b.push_specs()
    assert b.backfill()
    assert b.push_ticks()
    # specs, ticks and bars arrived
    assert store.specs('XAUUSD')['source'] == 'mt5'
    t = store.last_tick('XAUUSD')
    assert t['source'] == 'mt5' and abs(t['t'] - time.time()) < 5          # tick time converted back to UTC
    bars = store.load_bars('XAUUSD')
    assert len(bars) > 40 and bars.index[-1].timestamp() + 3600 <= time.time() + 1
    assert (bars.index.minute == 0).all() and (bars.index.weekday < 5).all()
    # second backfill starts from the API's last bar: nothing duplicated, nothing lost
    n = len(bars)
    b.last_pushed.clear()
    b.backfill()
    assert len(store.load_bars('XAUUSD')) == n
    assert any(e[0] == 'bar' for e in events) and any(e[0] == 'tick' for e in events)
    assert client.get('/api/ingest/status').json()['XAUUSD'] == int(bars.index[-1].timestamp())


def test_load_symbols_reads_config():
    m = br.load_symbols()
    assert m['US500'] == 'US500.cash' and m == {x['id']: x['mt5'] for x in config.INSTRUMENTS}
    assert br.load_symbols('/nonexistent/config.py') == br.FALLBACK_SYMBOLS
