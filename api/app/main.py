"""FastAPI app: prices store + updater (background), structure/backtest/plan endpoints, SSE stream."""
from __future__ import annotations

import asyncio
import json
import faulthandler
import logging
import signal
import time
from contextlib import asynccontextmanager

import numpy as np
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from . import config, events, store, updater
from .engine import backtest, plan
from .engine.continuation import _last_idx
from .ingest import router as ingest_router

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
log = logging.getLogger('structure-h1')
if hasattr(faulthandler, "register"):  # not on Windows
    faulthandler.register(signal.SIGUSR1, all_threads=True)  # `docker compose kill -s SIGUSR1 api` dumps every thread's stack to the logs
NO_DATA = 'données en cours de téléchargement'
MAX_CANDLES = 150_000


async def _run_updater():
    """Keep the updater alive: log and restart it if it ever crashes."""
    while True:
        try:
            await updater.run_updater()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('updater crashed, restarting in 30 s')
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_run_updater())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


class SafeGZip(GZipMiddleware):
    """GZip for the JSON payloads, but never for the SSE stream (it would be buffered)."""

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and scope['path'] == '/api/stream':
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)


app = FastAPI(title='Structure H1', lifespan=lifespan)
app.add_middleware(SafeGZip, minimum_size=1000)
app.include_router(ingest_router)


def _err(status, msg):
    return JSONResponse({'error': msg, 'detail': msg}, status_code=status)


@app.exception_handler(StarletteHTTPException)
async def _http_err(request, exc):
    return _err(exc.status_code, exc.detail if isinstance(exc.detail, str) else str(exc.detail))


@app.exception_handler(RequestValidationError)
async def _val_err(request, exc):
    e = exc.errors()[0] if exc.errors() else {}
    return _err(422, f"Requête invalide ({'.'.join(str(x) for x in e.get('loc', []))}) : {e.get('msg', 'erreur')}")


@app.exception_handler(backtest.ParamError)
async def _param_err(request, exc):
    return _err(422, str(exc))


@app.exception_handler(backtest.NoData)
async def _nodata_err(request, exc):
    iid = exc.args[0] if exc.args else None
    try:
        prog = float(_inst_status(iid).get('progress') or 0.0)
    except (TypeError, ValueError):
        prog = 0.0
    return JSONResponse({'error': NO_DATA, 'detail': NO_DATA, 'progress': prog}, status_code=503)


@app.exception_handler(Exception)
async def _any_err(request, exc):
    log.exception('unhandled error on %s', request.url.path)
    return _err(500, f'Erreur interne : {type(exc).__name__}: {exc}')


def _inst(iid: str) -> dict:
    ins = config.BY_ID.get(iid)
    if ins is None:
        raise HTTPException(404, f"Instrument inconnu : '{iid}'. Valeurs possibles : {', '.join(config.BY_ID)}.")
    return ins


def _inst_status(iid: str) -> dict:
    """Per-instrument entry of store.status(), tolerant to a flat or a nested {'instruments': {...}} layout."""
    try:
        s = store.status() or {}
    except Exception:
        return {}
    d = s.get('instruments', s)
    x = d.get(iid) if isinstance(d, dict) else None
    return x if isinstance(x, dict) else {}


# ------------------------------------------------------------------ reference data

@app.get('/api/instruments')
async def instruments():
    out = []
    for ins in config.INSTRUMENTS:
        s = _inst_status(ins['id'])
        out.append(dict(id=ins['id'], label=ins['label'], dec=ins['dec'], cost_bp=ins['cost_bp'],
                        ready=bool(s.get('ready', False)), first_bar=s.get('first_bar'), last_bar=s.get('last_bar'),
                        last_tick=store.last_tick(ins['id'])))
    return out


@app.get('/api/status')
async def status():
    return {**(store.status() or {}), 'refresh_seconds': config.REFRESH_SECONDS, 'price_source': config.PRICE_SOURCE}


@app.get('/api/defaults')
async def defaults():
    return dict(params=backtest.DEFAULT_PARAMS, account=plan.DEFAULT_ACCOUNT)


# ------------------------------------------------------------------ chart

def _chart(iid: str, frm, to, eq_break=True):
    dec = _inst(iid)['dec']
    st, _ = backtest.get_struct(iid, eq_break)
    t = st.t
    to = int(to) if to is not None else int(time.time())
    frm = int(frm) if frm is not None else int(t[-1]) - 180 * 86400
    i0, i1 = int(np.searchsorted(t, frm, 'left')), int(np.searchsorted(t, to, 'right'))
    if i1 - i0 > MAX_CANDLES:
        i0 = i1 - MAX_CANDLES
    sl = slice(i0, i1)
    cols = [np.round(a[sl], dec).tolist() for a in (st.o, st.h, st.l, st.c)]
    candles = [list(x) for x in zip(t[sl].tolist(), *cols)]
    pts = [[int(t[p.idx]), p.kind, round(float(p.price), dec), int(p.is_break)] for p in st.pts
           if i0 <= p.idx < i1]
    ex = _structure_extras(st, dec)
    lo, hi = i0, i1
    return dict(id=iid, dec=dec, candles=candles, points=pts, data_end=int(t[-1]) + 3600, last_tick=store.last_tick(iid),
                trend=[tr for p, tr in zip(st.pts, ex['trend']) if lo <= p.idx < hi],
                events=[e for k, e in ex['events'] if lo <= k < hi],
                breaks=[b for k, b in ex['breaks'] if lo <= k < hi])


def _trend_states(pts, ev_dir=None):
    """Trend in force (1 bull / -1 bear / 0 none) after each structure point; used to colour the zigzag.
    Approximates the state machine of engine.continuation.walk(), which only emits events (no trend timeline):
    ev_dir = {point index: dir} of the points that produced a walk event is authoritative (the trend is alive there),
    the tracker only decides where a trend ends (protected point taken, FT on FT) or starts between events."""
    ev_dir = ev_dir or {}
    n = len(pts)
    prev_same, prev_opp, last = [None] * n, [None] * n, {}
    for k, p in enumerate(pts):
        prev_same[k], prev_opp[k] = last.get(p.kind), last.get('L' if p.kind == 'H' else 'H')
        last[p.kind] = k
    out, trend, cu, cd, prot, ft_ok = [0] * n, 0, 0, 0, None, True
    for k in sorted(range(n), key=lambda j: (pts[j].known, j)):
        p = pts[k]
        if p.is_break and prev_same[k] is not None and prev_opp[k] is not None:
            d = 1 if p.kind == 'H' else -1
            if d == 1:
                cu, cd = cu + 1, 0
                c = cu
            else:
                cd, cu = cd + 1, 0
                c = cd
            if trend == -d:
                if prot is not None and (p.price - prot) * d >= 0:
                    trend = 0
                elif c == 1:
                    if not ft_ok:
                        trend = 0
                    else:
                        prot, ft_ok = p.price, False
            elif trend == d and c >= 2:
                ft_ok = True
            if trend != d and c >= 2:
                trend = d
                opp = 'L' if d == 1 else 'H'
                i2 = _last_idx(pts, k, opp, 2)
                ex = [x.price for x in pts[:k] if x.kind == opp and x.idx >= i2]
                prot = (min(ex) if d == 1 else max(ex)) if ex else pts[prev_opp[k]].price
                ft_ok = True
            if k in ev_dir:
                trend = ev_dir[k]
        out[k] = trend
    return out


def _structure_extras(st, dec):
    """Optional extras of the chart payload, computed once per structure (cached on it) on the FULL history.
    All times = bar OPEN time, epoch s, like the candles.
      trend  = [1|-1|0, ...] aligned with `points`: trend in force after each point (zigzag colour)
      events = walk() events:
        BREAK {ev, t, dir, leg, n, U, Z, zlo, Lb, prot, tb, tk, tp}
              t = confirmation bar, tb = point that was broken, tk = bar that broke it, tp = extreme of the new point
        FT    {ev, t, dir, SL, TP, valid, profond, tp}
      breaks = [[tb, broken_price, tk, dir], ...] one per structure break (BOS line), dir 1 = high broken, -1 = low broken
    events / breaks are stored as (bar index, payload) so that the caller can filter by window."""
    cached = st.__dict__.get('_chart_extras')
    if cached is not None:
        return cached
    t, pts = st.t, st.pts
    rd = lambda v: None if v is None else round(float(v), dec + 1)
    pos, broken, last_of = {}, {}, {}      # (known, kind, price) -> index in pts ; index -> previous point of the same kind
    for k, p in enumerate(pts):
        if p.is_break:
            pos[(p.known, p.kind, float(p.price))] = k
            broken[k] = last_of.get(p.kind)
        last_of[p.kind] = k
    breaks = []
    for k, q in broken.items():
        p = pts[k]
        if q is not None:
            tk = p.brk if p.brk >= 0 else p.idx
            breaks.append((p.idx, [int(t[pts[q].idx]), rd(pts[q].price), int(t[tk]), 1 if p.kind == 'H' else -1]))
    breaks.sort(key=lambda x: x[0])
    evs, ev_dir = [], {}
    for e in st.events:
        d = e['d']
        kind = ('H' if d == 1 else 'L') if e['ev'] == 'BREAK' else ('L' if d == 1 else 'H')
        k = pos.get((e['known'], kind, float(e['U'] if e['ev'] == 'BREAK' else e['SL'])))
        tp = int(t[pts[k].idx]) if k is not None else None
        if k is not None:
            ev_dir[k] = d
        if e['ev'] == 'BREAK':
            q = broken.get(k) if k is not None else None
            evs.append((e['known'], dict(
                ev='BREAK', t=int(t[e['known']]), dir=d, leg=e['leg'], n=e['n'], U=rd(e['U']), Z=rd(e['Z']), zlo=rd(e['zlo']),
                Lb=rd(e['Lb']), prot=rd(e['prot']), tb=None if q is None else int(t[pts[q].idx]),
                tk=int(t[pts[k].brk]) if k is not None and pts[k].brk >= 0 else None, tp=tp)))
        else:
            evs.append((e['known'], dict(ev='FT', t=int(t[e['known']]), dir=d, SL=rd(e['SL']), TP=rd(e['TP']),
                                         valid=bool(e['valid']), profond=e.get('profond'), tp=tp)))
    out = dict(trend=_trend_states(pts, ev_dir), events=evs, breaks=breaks)
    st.__dict__['_chart_extras'] = out
    return out


@app.get('/api/chart/{iid}')
async def chart(iid: str, frm: float | None = Query(None, alias='from'), to: float | None = None,
                eq_break: int = Query(1, ge=0, le=1)):
    return await asyncio.to_thread(_chart, iid, frm, to, bool(eq_break))


# ------------------------------------------------------------------ backtest / plan

@app.post('/api/backtest/{iid}')
async def backtest_ep(iid: str, body: dict = Body(default=None)):
    _inst(iid)
    body = body or {}
    return await asyncio.to_thread(backtest.run, iid, body.get('start'), body.get('end'), body.get('params'))


@app.post('/api/plan/{iid}')
async def plan_ep(iid: str, body: dict = Body(default=None)):
    _inst(iid)
    body = body or {}
    return await asyncio.to_thread(plan.build_plan, iid, body.get('params'), body.get('account'))


# ------------------------------------------------------------------ SSE

@app.get('/api/stream')
async def stream(request: Request):
    async def gen():
        agen = events.bus.listen().__aiter__()
        pending = None
        try:
            yield 'retry: 3000\n\n'
            while not await request.is_disconnected():
                if pending is None:
                    pending = asyncio.ensure_future(agen.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=15)
                if not done:
                    yield ': ping\n\n'
                    continue
                try:
                    ev = pending.result()
                except StopAsyncIteration:
                    break
                pending = None
                yield f"event: {ev['type']}\ndata: {json.dumps(ev['data'], separators=(',', ':'))}\n\n"
        finally:
            if pending is not None:
                pending.cancel()
            aclose = getattr(agen, 'aclose', None)
            if aclose:
                try:
                    await aclose()
                except Exception:
                    pass

    return StreamingResponse(gen(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'})
