import os
import sys

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from app import config, ingest, store  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(config, 'PRICE_SOURCE', 'dukascopy')
    monkeypatch.delenv('INGEST_TOKEN', raising=False)
    for d in (store._series, store._version, store._view, store._mt5_tick, store._duka_tick, store._prog):
        d.clear()
    store._loaded.clear()
    store._mt5_specs = None
    store._feeds['mt5']['last_seen'] = None
    events = []
    monkeypatch.setattr(ingest.bus, 'publish', lambda t, d: events.append((t, d)))
    app = FastAPI()
    app.include_router(ingest.router)
    c = TestClient(app)
    c.events = events
    return c


T0 = int(pd.Timestamp('2026-01-05 10:00').timestamp())


def rows(n, base=100.0):
    return [[T0 + 3600 * i, base + i, base + i + 1, base + i - 1, base + i + .5, 10 + i] for i in range(n)]


def test_bars_status_roundtrip(client):
    assert client.get('/api/ingest/status').json()['XAUUSD'] is None
    r = client.post('/api/ingest/bars', json={'id': 'XAUUSD', 'source': 'mt5', 'bars': rows(5)})
    assert r.status_code == 200 and r.json()['changed'] is True and r.json()['last_bar_t'] == T0 + 4 * 3600
    assert client.get('/api/ingest/status').json()['XAUUSD'] == T0 + 4 * 3600
    # idempotent re-push
    assert client.post('/api/ingest/bars', json={'id': 'XAUUSD', 'source': 'mt5', 'bars': rows(5)}).json()['changed'] is False
    # dukascopy mode: MT5 bars are stored but not served, no 'bar' event
    assert store.load_bars('XAUUSD').empty and not [e for e in client.events if e[0] == 'bar']
    assert store.status()['feeds']['mt5']['connected'] is True


def test_bars_served_in_mt5_mode(client, monkeypatch):
    monkeypatch.setattr(config, 'PRICE_SOURCE', 'mt5')
    client.post('/api/ingest/bars', json={'id': 'EURUSD', 'source': 'mt5', 'bars': rows(3, 1.1)})
    assert len(store.load_bars('EURUSD')) == 3
    assert ('bar', {'id': 'EURUSD', 't': T0 + 2 * 3600}) in client.events


def test_bad_inputs(client):
    assert client.post('/api/ingest/bars', json={'id': 'NOPE', 'bars': rows(1)}).status_code == 404
    assert client.post('/api/ingest/bars', json={'id': 'XAUUSD', 'source': 'x', 'bars': rows(1)}).status_code == 400
    r = client.post('/api/ingest/bars', json={'id': 'XAUUSD', 'bars': [[T0 + 5, 1, 2, 0, 1, 1], [T0, 1, 1, 1, 1, 0]]})
    assert r.status_code == 200 and store.mt5_last_bar('XAUUSD') is None  # off-grid time and zero volume rejected


def test_tick_and_specs(client):
    r = client.post('/api/ingest/tick', json={'ticks': [{'id': 'XAUUSD', 't': 1.5e9, 'bid': 4400.1, 'ask': 4400.4},
                                                         {'id': 'US500', 't': 1.5e9, 'bid': 6700.0}, {'id': 'NOPE', 't': 1, 'bid': 1}]})
    assert r.json() == {'accepted': 2}
    assert store.last_tick('XAUUSD') == {'t': 1.5e9, 'bid': 4400.1, 'ask': 4400.4, 'source': 'mt5'}
    assert store.last_tick('US500')['ask'] is None
    assert [e for e in client.events if e[0] == 'tick'][0][1]['source'] == 'mt5'
    r = client.post('/api/ingest/specs', json={'specs': [{'id': 'XAUUSD', 'contract_size': 100, 'tick_size': 0.01, 'tick_value': 1.0,
                                                            'currency_profit': 'USD', 'volume_min': 0.01, 'volume_step': 0.01, 'digits': 2}]})
    assert r.json() == {'accepted': 1} and store.specs('XAUUSD')['source'] == 'mt5'


def test_token(client, monkeypatch):
    monkeypatch.setenv('INGEST_TOKEN', 's3cret')
    assert client.get('/api/ingest/status').status_code == 401
    assert client.get('/api/ingest/status', headers={'X-Ingest-Token': 'bad'}).status_code == 401
    assert client.get('/api/ingest/status', headers={'X-Ingest-Token': 's3cret'}).status_code == 200


def test_ingest_refuses_public_ip_without_token(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import ingest
    monkeypatch.delenv('INGEST_TOKEN', raising=False)
    app = FastAPI(); app.include_router(ingest.router)
    c = TestClient(app)
    assert c.get('/api/ingest/status', headers={'X-Real-IP': '8.8.8.8'}).status_code == 403
    assert c.get('/api/ingest/status', headers={'X-Real-IP': '172.18.0.1'}).status_code == 200
    assert c.get('/api/ingest/status', headers={'X-Real-IP': '127.0.0.1'}).status_code == 200
