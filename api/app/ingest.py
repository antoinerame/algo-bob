"""Endpoints fed by bridge/mt5_bridge.py (MT5 on the Windows host). Optional shared secret: env INGEST_TOKEN."""
import hmac
import ipaddress
import logging
import os
import time

import pandas as pd
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from . import config, store
from .events import bus

log = logging.getLogger('ingest')


def _local(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or '')
    except ValueError:
        return True  # not an IP (in-process test client)
    return ip.is_loopback or ip.is_private


def _auth(request: Request, x_ingest_token: str | None = Header(default=None), x_real_ip: str | None = Header(default=None)):
    """With INGEST_TOKEN set, the token is required. Without it, only local/private clients may push prices
    (nginx passes the real client in X-Real-IP), so a server exposed on the internet can't be fed fake prices."""
    tok = os.environ.get('INGEST_TOKEN')
    if tok:
        if not hmac.compare_digest(tok, x_ingest_token or ''):
            raise HTTPException(401, 'invalid X-Ingest-Token')
    elif not _local(x_real_ip or (request.client.host if request.client else None)):
        raise HTTPException(403, 'INGEST_TOKEN requis pour envoyer des prix depuis internet')


router = APIRouter(prefix='/api/ingest', dependencies=[Depends(_auth)])


class BarsIn(BaseModel):
    id: str
    source: str = 'mt5'
    bars: list[list[float]]  # [t, o, h, l, c, v], closed H1 bars, t = epoch s UTC of the open time


class TickIn(BaseModel):
    id: str
    t: float
    bid: float
    ask: float | None = None


class TicksIn(BaseModel):
    ticks: list[TickIn]


class SpecIn(BaseModel):
    id: str
    contract_size: float | None = None
    tick_size: float | None = None
    tick_value: float | None = None
    currency_profit: str | None = None
    volume_min: float | None = None
    volume_step: float | None = None
    digits: int | None = None


class SpecsIn(BaseModel):
    specs: list[SpecIn]


def _check(iid):
    if iid not in config.BY_ID:
        raise HTTPException(404, f'unknown instrument {iid}')


@router.post('/bars')
def post_bars(body: BarsIn):
    _check(body.id)
    if body.source != 'mt5':
        raise HTTPException(400, "source must be 'mt5'")
    rows = [b[:6] for b in body.bars if len(b) >= 6]
    if not rows:
        return {'id': body.id, 'received': 0, 'changed': False, 'last_bar_t': store.mt5_last_bar(body.id)}
    df = pd.DataFrame(rows, columns=['t', 'open', 'high', 'low', 'close', 'volume'])
    df = df[df['t'] % 3600 == 0]
    df.index = pd.to_datetime(df['t'].astype('int64'), unit='s')
    v0 = store.data_version(body.id)
    changed = store.add_bars(body.id, df[store.COLS], 'mt5', replace=True)
    store.set_feed('mt5', last_seen=time.time())
    if store.data_version(body.id) != v0:  # the effective series changed (PRICE_SOURCE == 'mt5')
        bus.publish('bar', {'id': body.id, 't': int(store.load_bars(body.id).index[-1].timestamp())})
    if changed:
        bus.publish('status', store.status())
    return {'id': body.id, 'received': len(df), 'changed': changed, 'last_bar_t': store.mt5_last_bar(body.id)}


@router.post('/tick')
def post_tick(body: TicksIn):
    n = 0
    for k in body.ticks:
        if k.id not in config.BY_ID or not k.bid > 0:
            continue
        store.set_mt5_tick(k.id, k.t, k.bid, k.ask)
        bus.publish('tick', {'id': k.id, 't': k.t, 'bid': k.bid, 'ask': k.ask, 'source': 'mt5'})
        n += 1
    return {'accepted': n}


@router.post('/specs')
def post_specs(body: SpecsIn):
    items = [s.model_dump() for s in body.specs if s.id in config.BY_ID]
    store.set_mt5_specs(items)
    return {'accepted': len(items)}


@router.get('/status')
def get_status():
    """{id: last MT5 bar open time (epoch s) or null} - the bridge backfills from there."""
    return {x['id']: store.mt5_last_bar(x['id']) for x in config.INSTRUMENTS}
