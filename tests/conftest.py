"""pytest setup: make `app` importable and fall back to stubs for the data-layer modules that may not exist yet."""
import asyncio
import os
import sys
import tempfile
import types

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='sh1test_')   # never touch the real data volume from tests

import pytest

import fixtures as F  # noqa: F401  (adds api/ to sys.path)


def _stub(name, **attrs):
    m = types.ModuleType(f'app.{name}')
    m.__dict__.update(attrs)
    sys.modules[f'app.{name}'] = m
    import app
    setattr(app, name, m)
    return m


try:
    import app.store as store_mod
except Exception:
    store_mod = F.FakeStore()
    sys.modules['app.store'] = store_mod
    import app
    app.store = store_mod

try:
    import app.events  # noqa: F401
except Exception:
    class _Bus:
        def __init__(self):
            self.q = []

        def publish(self, type, data):
            self.q.append({'type': type, 'data': data})

        async def listen(self):
            while True:
                while self.q:
                    yield self.q.pop(0)
                await asyncio.sleep(0.05)

    _stub('events', bus=_Bus())
try:
    import app.updater  # noqa: F401
except Exception:
    async def _run_updater():
        await asyncio.sleep(3600)

    _stub('updater', run_updater=_run_updater)
try:
    import app.ingest  # noqa: F401
except Exception:
    from fastapi import APIRouter
    _stub('ingest', router=APIRouter(prefix='/api/ingest'))

_COUNTER = [0]


@pytest.fixture
def fake_store(monkeypatch):
    """Fresh in-memory store patched into app.store, caches cleared. Use fs.put(iid, bars) (auto-increments version)."""
    from app.engine import backtest
    import app.updater as updater_mod

    async def no_updater():          # never start the real downloader from tests
        await asyncio.sleep(3600)
    monkeypatch.setattr(updater_mod, 'run_updater', no_updater)
    fs = F.FakeStore()
    for name in ('load_bars', 'data_version', 'last_tick', 'specs', 'status'):
        monkeypatch.setattr(store_mod, name, getattr(fs, name), raising=False)
    orig_put = fs.put

    def put(iid, df, ver=None):
        _COUNTER[0] += 1
        orig_put(iid, df, ver or _COUNTER[0])
    fs.put = put
    for c in (backtest._STRUCTS, backtest._SIMS, backtest._RUNS):
        c.d.clear()
    return fs


@pytest.fixture(autouse=True)
def _v1_engine_defaults(request, monkeypatch):
    """The engine tests were written against the v1 rules (limit at zone top, every leg, no daily cap).
    Bob's rules became the defaults on 2026-10-27; tests that check them opt out with @pytest.mark.bob_defaults."""
    if request.node.get_closest_marker('bob_defaults'):
        return
    from app.engine import backtest as bt
    monkeypatch.setattr(bt, 'DEFAULT_PARAMS', bt.V1_PARAMS)


def pytest_configure(config):
    config.addinivalue_line('markers', "bob_defaults: run with the real (Bob) default params")
