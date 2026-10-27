# Structure H1 v2 — architecture and contracts

v1 (backup in `../structure-h1-v1-statique/`) was a static nginx site reading pre-computed JSON.
v2 adds a Python API that downloads prices itself, refreshes them continuously, recomputes Bob's structure,
runs backtests on a chosen period with adjustable parameters, and outputs the orders to place.

```
browser ──> web (nginx :8080) ── /            -> site/ (static)
                               └─ /api/*      -> api (FastAPI :8000)   (SSE: no buffering)
api ──(http, every REFRESH_SECONDS)──> Dukascopy datafeed (free, no account)
bridge/mt5_bridge.py (Windows host, optional) ──POST /api/ingest/*──> api   (FTMO MT5 real-time ticks, bars, specs)
api volume: /data (parquet store, survives restarts)
```

## Folders and owners

| Path | Content |
|---|---|
| `api/app/config.py` | instruments + env settings (fixed, do not rename keys) |
| `api/app/engine/structure.py`, `continuation.py` | Bob's rules, copied unchanged from `scripts/` (only the import was made relative). Do not change the rules. |
| `api/app/feeds/dukascopy.py`, `api/app/store.py`, `api/app/updater.py`, `api/app/events.py`, `api/app/ingest.py`, `bridge/` | **data layer** |
| `api/app/engine/backtest.py`, `api/app/engine/plan.py`, `api/app/main.py`, `api/Dockerfile`, `api/requirements.txt`, `docker-compose.yml`, `Dockerfile`, `nginx.conf` | **engine + API** |
| `site/index.html` | **frontend** |

## Data layer interface (used by the engine)

```python
# app/store.py
def load_bars(iid: str) -> pd.DataFrame
    # CLOSED H1 bars only, columns open/high/low/close/volume (float), index = tz-naive UTC DatetimeIndex of the
    # bar OPEN time, sorted, unique, no zero-volume/flat weekend filler bars. Empty DataFrame if nothing yet.
def data_version(iid: str) -> int          # increments every time new closed bars are added (cache key)
def last_tick(iid: str) -> dict | None     # {'t': epoch s, 'bid': float, 'ask': float|None, 'source': 'mt5'|'dukascopy'}
def specs(iid: str) -> dict                # config defaults merged with MT5-pushed specs:
    # {'contract': float, 'quote': str, 'volume_min': float, 'volume_step': float, 'tick_size': float|None,
    #  'tick_value': float|None, 'source': 'mt5'|'defaut'}
def status() -> dict                       # per instrument: ready, bars, first_bar, last_bar (epoch s), bootstrapping
                                           # progress 0..1, last_error ; feeds: dukascopy {ok,last_run,last_error},
                                           # mt5 {connected, last_seen}
# app/events.py
bus.publish(type: str, data: dict)         # THREAD-SAFE (updater may run in a thread)
async for ev in bus.listen(): ...          # ev = {'type': ..., 'data': ...}; types: 'tick', 'bar', 'status'
# app/updater.py
async def run_updater(): ...               # started by main.py on startup (bootstrap then refresh loop)
# app/ingest.py
router = APIRouter(prefix='/api/ingest')   # included by main.py
```

Event payloads: `tick` = `{id, t, bid, ask, source}` (≤ 1 per second per instrument);
`bar` = `{id, t}` (t = open time of the newest closed bar, emitted once per refresh that added bars);
`status` = `store.status()`.

Prices are BID. All timestamps in the HTTP API are epoch seconds UTC; bars are keyed by their open time.

## HTTP API (engine + API implements, frontend consumes)

`GET /api/instruments` → `[{id, label, dec, cost_bp, ready, first_bar, last_bar, last_tick}]`

`GET /api/status` → `store.status()` + `{refresh_seconds, price_source}`

`GET /api/chart/{id}?from=<epoch>&to=<epoch>` (default from = last_bar − 180 days, to = now) →
`{id, dec, candles: [[t,o,h,l,c], ...], points: [[t, 'H'|'L', price, is_break 0|1], ...], data_end, last_tick}`

`GET /api/defaults` → `{params: {...default strategy params...}, account: {balance: 100000, currency: 'USD', risk_pct: 0.5}}`

Strategy params (all optional, defaults = Bob's v1 rules so that defaults reproduce v1):
```
eq_break: true            # equal high/low counts as a break
setups: {epa: true, ft_valid: true, ft_ipa: false}
direction: 'both'         # 'both' | 'long' | 'short'
epa_cancel_on_lb: false   # true = EPA cancelled if the opposite point Lb breaks before the zone is reached (backtest drops those fills; plan marks the order 'annulé'); false = v1 (kept, Lb shown as info)
epa_entry: 0.0            # where the EPA limit sits in the zone: 0 = zone top (v1), 0.5 = middle, 1 = zone bottom (excluded)
sl_buffer: 0.0            # extra SL distance as a fraction of the initial risk (0.1 = SL 10 % further)
tp_mode: 'structure'      # 'structure' (v1: the break level / trend point) | 'rr' (fixed multiple of risk)
tp_rr: 2.0                # used when tp_mode == 'rr'
min_rr: 0.0               # skip trades whose RR < min_rr
max_hold_hours: 480       # race limit (v1: 20 days); unresolved => 'en cours' if it reaches the end of data, else closed at last price
entry_hours: [0, 24]      # UTC hour window for the entry time (start inclusive, end exclusive)
cost_bp: null             # null = instrument default
# Bob's rules (defaults since 2026-10-27; V1_PARAMS in engine/backtest.py = the v1 site):
epa_entry_rr: 3.0         # EPA limit where RR (SL = zone low, TP = U) equals this value, capped at the zone top (0 = zone top)
epa_inzone: 'market'      # price already in the zone when the break is confirmed: 'market' (RR >= epa_entry_rr) | 'skip'
epa_ipa_legs: false       # false = no EPA trade after an IPA leg (déséquilibre)
entry_hours_tz: 'paris'   # 'paris' | 'utc': time zone of entry_hours
max_per_day: 1            # trades per Paris day (0 = no cap)
session_paris: [14, 17]   # an order exists only from the first bar inside this Paris-time window after the confirmation
                          # (Bob: analysis at 14h, live until ~17h); null = orders exist right after the confirmation bar
```

`POST /api/backtest/{id}` body `{start: 'YYYY-MM-DD'|null, end: 'YYYY-MM-DD'|null, params: {...}}` →
```
{period: {start, end}, params: <effective params>, resolution: 'H1',
 stats: {epa, ft_valide, ft_ipa, total: TradeStat|null,
         continuation, retour_zone, retour_zone_apres_epa: RaceStat|null},
 equity: [[t_out, cumR], ...],                    # closed trades of the enabled setups, in exit order
 by_month: [{m: 'YYYY-MM', R, n}], by_year: [{an, R, n, epa_R, ft_R}],
 trades: [{k:'EPA'|'FT', v:0|1, d:1|-1, ti, to|null, e, sl, tp, rr, p0, s:'TP'|'SL'|'en cours'|'expiré', R|null}]}
TradeStat = {n, win, p0, rr, R, Rtot, t, maxdd, pf}    # R = mean net R per trade, maxdd in R, pf = profit factor
RaceStat  = {n, obs, p0, t}
```
Structure (points, events) is always computed on the FULL history; the period only filters trades by entry time.

`POST /api/plan/{id}` body `{params: {...}, account: {balance, currency, risk_pct}}` →
```
{asof: <last closed bar close time>, last: {t, bid, ask, source}, data_delay_s,
 trend: {dir: 1|-1|0, leg, n, prot, last_break_t} ,
 levels: {U, Z, zlo, Lb, prot},                   # null when no trend
 orders: [{id, setup:'EPA'|'FT', side:'BUY'|'SELL', type:'LIMIT'|'MARKET',
           status:'à placer'|'en position'|'annulé'|'exécuté',
           entry, sl, tp, rr, lots, risk_money, risk_pct, created_t,
           cancel_if: [str, ...], note: str}],
 open_trades: [<trade like in backtest>],
 hist: {epa: TradeStat|null, ft_valide: TradeStat|null},  # default params, full history, for context
 warnings: [str, ...]}
```
Lot sizing: `lots = risk_money / (|entry - sl| * contract * quote_to_account)`, rounded DOWN to volume_step,
0 if below volume_min (with a warning). quote_to_account uses the latest prices in the store
(JPY: 1/USDJPY, EUR: EURUSD when account is USD; account EUR: divide USD amounts by EURUSD).

`GET /api/stream` — Server-Sent Events, `event: tick|bar|status`, `data: <json>`; heartbeat comment every 15 s.

## Backtest resolution (H1)

v1 resolved races on M1 data (HistData, not available here). v2 uses H1 bars:
- entries happen at the open of the bar after the confirmation bar (FT), or at the limit price on the first bar touching it (EPA);
- a bar that touches both SL and TP counts as SL (conservative);
- on the EPA fill bar only the SL can be hit (the TP may have been touched before the fill inside that bar);
- this makes v2 slightly more pessimistic than v1; the gap is measured and reported in README.
