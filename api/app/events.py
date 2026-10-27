"""In-process event bus feeding the SSE stream. publish() is thread-safe; listen() is an async generator.

Types: 'tick' (throttled to <= 1/s per instrument, the newest tick of a burst is always delivered), 'bar', 'status'.
"""
import asyncio
import time


class Bus:
    def __init__(self, queue_size=256, tick_interval=1.0):
        self.queue_size = queue_size
        self.tick_interval = tick_interval
        self._loop = None
        self._subs = set()
        self._last_tick = {}  # id -> monotonic time of last delivered tick
        self._pending = {}    # id -> tick event waiting for the throttle window
        self._timers = {}

    def attach_loop(self, loop=None):
        """Remember the event loop (called by run_updater / first listen). Needed for publish() from threads."""
        self._loop = loop or asyncio.get_running_loop()

    def publish(self, type, data):
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._dispatch, {'type': type, 'data': data})
        except RuntimeError:  # loop closed meanwhile
            pass

    # everything below runs in the loop thread
    def _dispatch(self, ev):
        if ev['type'] == 'tick' and self.tick_interval > 0:
            iid = ev['data'].get('id')
            now = time.monotonic()
            wait = self.tick_interval - (now - self._last_tick.get(iid, -1e9))
            if wait > 0:
                self._pending[iid] = ev
                if iid not in self._timers:
                    self._timers[iid] = self._loop.call_later(wait, self._flush, iid)
                return
            self._last_tick[iid] = now
        self._send(ev)

    def _flush(self, iid):
        self._timers.pop(iid, None)
        ev = self._pending.pop(iid, None)
        if ev:
            self._last_tick[iid] = time.monotonic()
            self._send(ev)

    def _send(self, ev):
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()  # drop oldest
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(ev)

    async def listen(self):
        self._loop = asyncio.get_running_loop()
        q = asyncio.Queue(maxsize=self.queue_size)
        self._subs.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs.discard(q)


bus = Bus()
