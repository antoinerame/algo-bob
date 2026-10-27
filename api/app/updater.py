"""Background updater: bootstrap every instrument from HISTORY_START (monthly H1 files), then refresh every
REFRESH_SECONDS with what has become available: closed months, then daily M1 files (resampled to H1), then
hourly tick files (BID, aggregated to H1) for today. Only closed hours are stored. Blocking IO runs in threads.

Progress pointer `done` (per instrument, in memory): everything before it has been handled. After a restart it is
rebuilt from the stored bars (last bar + 1 h), so only what is missing is fetched.
"""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd

from . import config, store
from .events import bus
from .feeds import dukascopy as dk

log = logging.getLogger('updater')

POOL = 4
CHUNK = 8          # months fetched per batch (persisted after each batch)
TICK_LAG_H = 2     # a missing tick file older than this many hours = no trading, not "not yet published" (files appear ~1 min after the hour)
BACKFILL_BUDGET_S = 45  # per pass, after the live refresh, spent extending history back to HISTORY_START
DAY_FALLBACK_H = 36     # a missing daily file this recent = "not published yet": use hourly ticks
MONTH_FALLBACK_D = 40   # a missing monthly file this recent = "not published yet": use daily files
_done = {}         # iid -> datetime (naive UTC): all hours before it are processed
_last_status = [0.0]


def order():
    xs = sorted(config.INSTRUMENTS, key=lambda x: x['id'] != 'XAUUSD')  # gold first, config order otherwise
    return xs


def _hist_start():
    return datetime.fromisoformat(config.HISTORY_START)


def _cursor(inst):
    iid = inst['id']
    df = store.load_bars(iid)
    c = _hist_start()
    if len(df):
        c = max(c, df.index[-1].to_pydatetime() + timedelta(hours=1))
    return max(c, _done.get(iid, c))


def _pmap(fn, items):
    ex = ThreadPoolExecutor(POOL)
    try:
        return list(ex.map(fn, items))
    finally:
        ex.shutdown(wait=True, cancel_futures=True)  # first failure: don't keep fetching the rest of the batch


def _pub_status(force=False):
    if force or time.time() - _last_status[0] > 1.0:
        _last_status[0] = time.time()
        bus.publish('status', store.status())


def _months(a, b):
    out = []
    m = dk.month_start(a)
    while m < b:
        out.append(m)
        m = dk.next_month(m)
    return out


def sync_months(inst, cur_month, now, progress=None):
    """Closed months from `_done` up to the current month, from monthly H1 files (daily files for a month not published yet)."""
    iid = inst['id']
    changed = False
    months = _months(_cursor(inst), cur_month)
    total = len(months)
    for i in range(0, total, CHUNK):
        chunk = months[i:i + CHUNK]
        res = _pmap(lambda m: dk.fetch_month_h1(inst, m.year, m.month), chunk)
        for m, df in zip(chunk, res):
            if df is None and dk.next_month(m) > now - timedelta(days=MONTH_FALLBACK_D):
                changed |= sync_days(inst, max(m, _cursor(inst)), dk.next_month(m), now)
            elif df is not None:
                changed |= store.add_bars(iid, df, 'dukascopy')
            _done[iid] = max(_done.get(iid, m), dk.next_month(m))
        if progress:
            progress(min(1.0, (i + len(chunk)) / total))
    return changed


def sync_days(inst, a, b, now):
    """Complete UTC days in [a, b) from daily M1 files; a missing recent day falls back to hourly ticks."""
    iid = inst['id']
    changed = False
    d = datetime(a.year, a.month, a.day)
    days = []
    while d < b:
        days.append(d)
        d += timedelta(days=1)
    res = _pmap(lambda d: dk.fetch_day_h1(inst, d), days)
    for d, df in zip(days, res):
        if df is not None:
            changed |= store.add_bars(iid, df, 'dukascopy')
        elif now - (d + timedelta(days=1)) < timedelta(hours=DAY_FALLBACK_H):
            ok, ch = sync_hours(inst, max(d, _cursor(inst)), d + timedelta(days=1), now)
            changed |= ch
            if not ok:
                return changed  # tick files not out yet: retry next pass
        _done[iid] = max(_done.get(iid, d), d + timedelta(days=1))
    return changed


def _weekend(h):
    """All 10 markets are closed from Friday 22:00 to Sunday 21:00 UTC (empty tick files): don't fetch them."""
    wd = h.weekday()
    return wd == 5 or (wd == 4 and h.hour >= 22) or (wd == 6 and h.hour < 21)


def sync_hours(inst, a, b, now):
    """Closed hours in [a, b) from hourly tick files. Returns (all_resolved, changed)."""
    iid = inst['id']
    hours = []
    h = a.replace(minute=0, second=0, microsecond=0)
    while h < b:
        hours.append(h)
        h += timedelta(hours=1)
    res = _pmap(lambda h: None if _weekend(h) else dk.fetch_hour_ticks(inst, h), hours)
    changed = False
    for h, tk in zip(hours, res):
        if _weekend(h):
            _done[iid] = max(_done.get(iid, h), h + timedelta(hours=1))
            continue
        if tk is None and h > now - timedelta(hours=TICK_LAG_H):
            return False, changed  # probably not published yet
        if tk is not None and len(tk):
            changed |= store.add_bars(iid, dk.ticks_to_h1(tk), 'dukascopy')
            last = tk.iloc[-1]
            store.set_duka_tick(iid, tk.index[-1].timestamp(), last['bid'], last['ask'])
        _done[iid] = max(_done.get(iid, h), h + timedelta(hours=1))
    return True, changed


def backfill_back(inst, deadline=None):
    """HISTORY_START moved earlier than what the store covers: fetch the earlier months, newest first.
    state.covered_from[iid] = start of the verified contiguous history; it moves back after every batch, so an
    interrupted backfill resumes where it stopped and never leaves a hole. Stops at `deadline` (time.time())."""
    iid = inst['id']
    hs = _hist_start()
    df = store.load_bars(iid)
    if not len(df):
        return False
    cov = store.get_state('covered_from', {}) or {}
    start = datetime.fromisoformat(cov[iid]) if cov.get(iid) else dk.month_start(df.index[0].to_pydatetime())
    if start <= hs:
        return False
    months = _months(hs, dk.month_start(start))[::-1]
    changed = False
    for i in range(0, len(months), CHUNK):
        if deadline and time.time() > deadline:
            return changed
        chunk = months[i:i + CHUNK]
        for df in _pmap(lambda m: dk.fetch_month_h1(inst, m.year, m.month), chunk):
            if df is not None:
                changed |= store.add_bars(iid, df, 'dukascopy')
        cov = dict(store.get_state('covered_from', {}) or {})
        cov[iid] = max(chunk[-1], hs).date().isoformat()
        store.set_state('covered_from', cov)
    return changed


def sync_instrument(inst, now=None):
    """One catch-up pass for one instrument (bootstrap or refresh). Returns True if new bars were stored."""
    iid = inst['id']
    now = now or dk.utcnow()
    if config.PRICE_SOURCE == 'mt5' and store.has_mt5(iid):
        store.set_progress(iid, bootstrapping=False, ready=True, last_error=None)
        return False
    cur_month = dk.month_start(now)
    cur_day = datetime(now.year, now.month, now.day)
    cur_hour = now.replace(minute=0, second=0, microsecond=0)
    boot = iid not in (store.get_state('boot_done', []) or [])
    store.set_progress(iid, bootstrapping=boot, ready=not boot and store.data_version(iid) > 0, last_error=None,
                       **({'progress': 0.0} if boot else {}))
    if not len(store.load_bars(iid)) and iid not in (store.get_state('covered_from', {}) or {}):
        cov = dict(store.get_state('covered_from', {}) or {})
        cov[iid] = config.HISTORY_START
        store.set_state('covered_from', cov)
    before = store.data_version(iid)
    prog = (lambda p: (store.set_progress(iid, progress=p), _pub_status())) if boot else None
    if _cursor(inst) < cur_month:
        sync_months(inst, cur_month, now, prog)
    c = _cursor(inst)
    if c < cur_day:
        sync_days(inst, c, cur_day, now)
    c = _cursor(inst)
    if c < cur_hour:
        sync_hours(inst, max(c, cur_day), cur_hour, now)
    if boot:
        done = list(store.get_state('boot_done', []) or [])
        store.set_state('boot_done', done + [iid])
    new = store.data_version(iid) != before
    df = store.load_bars(iid)
    store.set_progress(iid, bootstrapping=False, ready=bool(len(df)), progress=1.0 if len(df) else 0.0, last_error=None)
    if new and len(df):
        bus.publish('bar', {'id': iid, 't': int(df.index[-1].timestamp())})
    return new


def run_pass(now=None):
    """Sync every instrument once; one failing instrument never stops the others. Returns list of ids with new bars."""
    updated, errors = [], []
    t0 = time.time()
    log.info('pass start')
    for inst in order():
        try:
            if sync_instrument(inst, now):
                updated.append(inst['id'])
        except Exception as e:
            log.exception('sync %s failed', inst['id'])
            errors.append(f"{inst['id']}: {type(e).__name__}: {e}")
            store.set_progress(inst['id'], bootstrapping=False, last_error=f'{type(e).__name__}: {e}',
                               ready=len(store.load_bars(inst['id'])) > 0)
        _pub_status(force=True)
    store.set_feed('dukascopy', ok=not errors, last_run=time.time(), last_error='; '.join(errors) or None)
    log.info('pass refreshed %d instruments in %.0f s, new bars: %s, errors: %d', len(config.INSTRUMENTS), time.time() - t0, updated, len(errors))
    deadline = time.time() + BACKFILL_BUDGET_S
    for inst in order():
        if time.time() > deadline:
            break
        try:
            if backfill_back(inst, deadline):
                df = store.load_bars(inst['id'])
                bus.publish('bar', {'id': inst['id'], 't': int(df.index[-1].timestamp())})
        except Exception:
            log.exception('backfill %s failed', inst['id'])
    _pub_status(force=True)
    return updated


async def run_updater():
    bus.attach_loop()
    for x in order():  # data already on disk is usable immediately
        if x['id'] in (store.get_state('boot_done', []) or []):
            store.set_progress(x['id'], ready=len(store.load_bars(x['id'])) > 0)
        else:
            store.set_progress(x['id'], ready=False, bootstrapping=True, progress=0.0)
    while True:
        try:
            await asyncio.to_thread(run_pass)
        except Exception as e:
            log.exception('updater pass failed')
            store.set_feed('dukascopy', ok=False, last_run=time.time(), last_error=f'{type(e).__name__}: {e}')
        await asyncio.sleep(config.REFRESH_SECONDS)
