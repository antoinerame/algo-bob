#!/usr/bin/env python
"""Pont MetaTrader 5 -> API : pousse specs, barres H1 cloturees et ticks vers /api/ingest/*.

A lancer sur l'hote Windows, avec un terminal MT5 DEJA ouvert et connecte (aucun identifiant n'est manipule ici).
Seule dependance : le paquet MetaTrader5 (HTTP en stdlib). Voir README.md.

  python mt5_bridge.py --api http://127.0.0.1:8080
  python mt5_bridge.py --fake --api http://localhost:8000 --max-seconds 20      # test sans MT5
"""
import argparse
import importlib.util
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

# id -> symbole MT5 chez FTMO. Duplique api/app/config.py (champ `mt5`) ; config.py est relu s'il est trouve a cote.
FALLBACK_SYMBOLS = {'XAUUSD': 'XAUUSD', 'XAGUSD': 'XAGUSD', 'EURUSD': 'EURUSD', 'GBPUSD': 'GBPUSD', 'USDJPY': 'USDJPY',
                    'US500': 'US500.cash', 'US100': 'US100.cash', 'GER40': 'GER40.cash', 'USOIL': 'USOIL.cash',
                    'UKOIL': 'UKOIL.cash'}
FAKE_BASE = {'XAUUSD': 4400.0, 'XAGUSD': 55.0, 'EURUSD': 1.16, 'GBPUSD': 1.33, 'USDJPY': 158.0, 'US500': 6700.0,
             'US100': 24500.0, 'GER40': 23400.0, 'USOIL': 62.0, 'UKOIL': 66.0}


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def load_symbols(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'api', 'app', 'config.py')
    try:
        spec = importlib.util.spec_from_file_location('bridge_config', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return {x['id']: x['mt5'] for x in mod.INSTRUMENTS}
    except Exception:
        return dict(FALLBACK_SYMBOLS)


# --- heure du serveur ------------------------------------------------------
# MT5 donne les heures en temps SERVEUR (FTMO : UTC+2 en hiver, UTC+3 en ete, bascule calee sur les USA), encodees
# comme si c'etait de l'UTC. Conversion : utc = t_serveur - decalage. Le decalage suit la regle EET + DST americain,
# recale sur la mesure (tick frais vs horloge locale) ; --server-offset N force un decalage fixe.

def _nth_sunday(year, month, n):
    d = datetime(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))
    return d


def us_dst(utc_s):
    y = datetime.fromtimestamp(utc_s, timezone.utc).year
    start = _nth_sunday(y, 3, 2).replace(hour=7, tzinfo=timezone.utc).timestamp()   # 2h NY (EST) = 07h UTC
    end = _nth_sunday(y, 11, 1).replace(hour=6, tzinfo=timezone.utc).timestamp()     # 2h NY (EDT) = 06h UTC
    return start <= utc_s < end


def rule_offset_h(utc_s):
    return 3 if us_dst(utc_s) else 2


class ServerClock:
    def __init__(self, fixed=None):
        self.fixed = fixed
        self.delta = 0  # mesure - regle

    def at_utc(self, utc_s):
        return self.fixed if self.fixed is not None else rule_offset_h(utc_s) + self.delta

    def to_utc(self, srv_s):
        utc = srv_s - self.at_utc(srv_s - 3 * 3600) * 3600
        return srv_s - self.at_utc(utc) * 3600

    def calibrate(self, srv_times, now_s):
        """Decalage entier d'heures deduit de ticks frais (tick - horloge locale ~ k heures). Besoin de >= 2 symboles d'accord."""
        if self.fixed is not None:
            return self.fixed
        votes = Counter()
        for t in srv_times:
            d = t - now_s
            n = round(d / 3600)
            if abs(d - n * 3600) <= 120 and -12 <= n <= 14:
                votes[n] += 1
        if not votes:
            return None
        n, c = votes.most_common(1)[0]
        if c < min(2, len(srv_times)):
            return None
        self.delta = n - rule_offset_h(now_s)
        return n


# --- HTTP -----------------------------------------------------------------

class Api:
    def __init__(self, base, token=None, dry=False):
        self.base, self.token, self.dry = base.rstrip('/'), token, dry

    def _req(self, method, path, body=None):
        h = {'Content-Type': 'application/json'}
        if self.token:
            h['X-Ingest-Token'] = self.token
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        with urllib.request.urlopen(r, timeout=15) as f:
            return json.loads(f.read() or b'null')

    def post(self, path, body):
        if self.dry:
            log(f'[dry-run] POST {path} {json.dumps(body)[:160]}')
            return {}
        try:
            return self._req('POST', path, body)
        except urllib.error.HTTPError as e:
            log(f'API {path}: HTTP {e.code} {e.read()[:200]!r}')
        except Exception as e:
            log(f'API {path} injoignable : {e}')
        return None

    def last_bars(self):
        if self.dry:
            return {}
        try:
            return self._req('GET', '/api/ingest/status') or {}
        except Exception as e:
            log(f'API /api/ingest/status injoignable : {e}')
            return None


# --- MT5 factice pour tester sans terminal ---------------------------------

class FakeMT5:
    """Meme surface que le module MetaTrader5 (temps serveur = UTC + regle FTMO), prix en marche aleatoire."""
    TIMEFRAME_H1 = 16385

    def __init__(self, symbols, seed=1):
        self.rng = random.Random(seed)
        self.px = {s: FAKE_BASE.get(i, 100.0) for i, s in symbols.items()}
        self.names = set(symbols.values())

    @staticmethod
    def _srv(utc_s):
        return utc_s + rule_offset_h(utc_s) * 3600

    def initialize(self, *a, **k):
        return True

    def shutdown(self):
        pass

    def last_error(self):
        return (0, 'fake')

    def symbol_select(self, s, on=True):
        return s in self.names

    def terminal_info(self):
        return object()

    def symbol_info(self, s):
        if s not in self.names:
            return None
        big = self.px[s] > 1000
        o = type('Info', (), {})()
        o.trade_contract_size = 1.0 if big else 100000.0
        o.trade_tick_size = 0.01 if big else 0.00001
        o.trade_tick_value = 0.01 if big else 1.0
        o.currency_profit = 'USD'
        o.volume_min, o.volume_step, o.digits = 0.01, 0.01, 2 if big else 5
        return o

    def symbol_info_tick(self, s):
        if s not in self.names:
            return None
        p = self.px[s] = self.px[s] * (1 + self.rng.gauss(0, 2e-5))
        now = time.time()
        o = type('Tick', (), {})()
        o.time, o.time_msc = int(self._srv(now)), int(self._srv(now) * 1000)
        o.bid, o.ask = p, p * 1.00002
        return o

    def copy_rates_from_pos(self, s, tf, pos, count):
        if s not in self.names:
            return None
        h0 = int(time.time() // 3600) * 3600 - pos * 3600
        px = self.px[s]
        out, rng = [], random.Random(hash(s) & 0xffff)
        for k in range(count):
            t = h0 - k * 3600
            if datetime.fromtimestamp(t, timezone.utc).weekday() >= 5:
                continue
            c = px * (1 + 0.001 * math.sin(t / 7200.0))
            out.append((self._srv(t), c * 0.9995, c * 1.001, c * 0.998, c, 100 + rng.randint(0, 50), 3, 0))
        dt = [('time', '<i8'), ('open', '<f8'), ('high', '<f8'), ('low', '<f8'), ('close', '<f8'),
              ('tick_volume', '<u8'), ('spread', '<i4'), ('real_volume', '<u8')]
        import numpy as np
        return np.array(sorted(out), dtype=dt)


# --- pont -----------------------------------------------------------------

class Bridge:
    def __init__(self, mt5, api, symbols, clock, since, interval=1.0):
        self.mt5, self.api, self.symbols, self.clock = mt5, api, symbols, clock
        self.since, self.interval = since, interval
        self.last_pushed = {}  # id -> t (UTC epoch) du dernier H1 pousse
        self.empty_cycles = 0

    # specs
    def push_specs(self):
        specs = []
        for iid, sym in self.symbols.items():
            i = self.mt5.symbol_info(sym)
            if i is None:
                continue
            specs.append({'id': iid, 'contract_size': i.trade_contract_size, 'tick_size': i.trade_tick_size,
                          'tick_value': i.trade_tick_value, 'currency_profit': i.currency_profit,
                          'volume_min': i.volume_min, 'volume_step': i.volume_step, 'digits': i.digits})
        if specs:
            self.api.post('/api/ingest/specs', {'specs': specs})
            log(f'specs poussees : {len(specs)} symboles')

    # heure serveur
    def calibrate(self):
        ts = []
        for sym in self.symbols.values():
            k = self.mt5.symbol_info_tick(sym)
            if k is not None and k.time:
                ts.append(k.time)
        n = self.clock.calibrate(ts, time.time())
        if n is None:
            g = rule_offset_h(time.time()) + self.clock.delta
            log(f'decalage serveur non mesurable (marche ferme ?) -> regle FTMO UTC+{g}h (ou --server-offset)')
        else:
            note = '' if self.clock.fixed is not None or self.clock.delta == 0 else ' (different de la regle FTMO UTC+2/+3 : decalage mesure conserve)'
            log(f'decalage serveur = UTC+{n}h{note}')

    # barres
    def bars(self, sym, count, pos=0):
        rates = self.mt5.copy_rates_from_pos(sym, self.mt5.TIMEFRAME_H1, pos, count)
        if rates is None:
            return []
        now = time.time()
        out = []
        for r in rates:
            t = int(round(self.clock.to_utc(int(r['time'])) / 3600.0)) * 3600
            if t + 3600 <= now + 1 and float(r['tick_volume']) > 0:  # closed bars only
                out.append([t, float(r['open']), float(r['high']), float(r['low']), float(r['close']), float(r['tick_volume'])])
        return out

    def push_bars(self, iid, bars, chunk=5000):
        for k in range(0, len(bars), chunk):
            self.api.post('/api/ingest/bars', {'id': iid, 'source': 'mt5', 'bars': bars[k:k + chunk]})
        if bars:
            self.last_pushed[iid] = max(self.last_pushed.get(iid, 0), bars[-1][0])

    def backfill(self):
        api_last = self.api.last_bars()
        if api_last is None:
            return False
        now = time.time()
        for iid, sym in self.symbols.items():
            start = api_last.get(iid) or self.since
            count = min(95000, int((now - start) / 3600 * 0.75) + 300)
            bars = [b for b in self.bars(sym, count) if b[0] >= start]
            self.push_bars(iid, bars)
            log(f'{iid}: {len(bars)} barres H1 poussees (depuis {datetime.fromtimestamp(start, timezone.utc):%Y-%m-%d %H:%M})')
        return True

    def push_new_bars(self):
        for iid, sym in self.symbols.items():
            bars = [b for b in self.bars(sym, 4) if b[0] >= self.last_pushed.get(iid, 0) - 3600]  # last one re-sent: final values
            if bars:
                self.push_bars(iid, bars)

    # ticks
    def push_ticks(self):
        ticks = []
        for iid, sym in self.symbols.items():
            k = self.mt5.symbol_info_tick(sym)
            if k is None or not k.bid:
                continue
            ms = getattr(k, 'time_msc', 0) or k.time * 1000
            ticks.append({'id': iid, 't': self.clock.to_utc(ms / 1000.0), 'bid': float(k.bid), 'ask': float(k.ask) or None})
        self.empty_cycles = 0 if ticks else self.empty_cycles + 1
        if ticks:
            self.api.post('/api/ingest/tick', {'ticks': ticks})
        return bool(ticks)

    def run(self, max_seconds=None):
        t_start = time.time()
        self.calibrate()
        self.push_specs()
        backfilled = self.backfill()
        hour = int(time.time() // 3600)
        t_cal = time.time()
        log('boucle temps reel (Ctrl+C pour arreter)')
        while True:
            t0 = time.time()
            if max_seconds and t0 - t_start >= max_seconds:
                return
            if not self.push_ticks() and self.empty_cycles >= 30:
                log('plus aucun tick recu du terminal MT5 : verifier qu il est ouvert et connecte')
                self.empty_cycles = 0
            h = int(t0 // 3600)
            if h != hour and t0 - h * 3600 >= 2:
                hour = h
                if not backfilled:
                    backfilled = self.backfill()
                else:
                    self.push_new_bars()
            elif not backfilled and int(t0) % 30 == 0:
                backfilled = self.backfill()
            if t0 - t_cal > 6 * 3600:
                t_cal = t0
                self.calibrate()
                self.push_specs()
            time.sleep(max(0.0, self.interval - (time.time() - t0)))


def main():
    ap = argparse.ArgumentParser(description='Pont MT5 -> API Structure H1')
    ap.add_argument('--api', default='http://127.0.0.1:8080', help='URL de l API (nginx proxifie /api)')
    ap.add_argument('--interval', type=float, default=1.0, help='periode d envoi des ticks (s)')
    ap.add_argument('--since', default=None, help='AAAA-MM-JJ : depart du backfill si l API n a encore aucune barre MT5 (defaut : 2 ans)')
    ap.add_argument('--terminal', default=None, help='chemin de terminal64.exe (sinon terminal MT5 detecte automatiquement)')
    ap.add_argument('--server-offset', type=int, default=None, help='decalage fixe serveur-UTC en heures (defaut : auto)')
    ap.add_argument('--symbols', default=None, help='ids a suivre, separes par des virgules (defaut : tous)')
    ap.add_argument('--config', default=None, help='chemin de api/app/config.py')
    ap.add_argument('--token', default=os.environ.get('INGEST_TOKEN'), help='secret partage (env INGEST_TOKEN)')
    ap.add_argument('--fake', action='store_true', help='MT5 factice (ticks/barres synthetiques) pour tester la chaine API')
    ap.add_argument('--dry-run', action='store_true', help='n envoie rien, affiche seulement')
    ap.add_argument('--max-seconds', type=float, default=None, help='s arrete apres N secondes (tests)')
    a = ap.parse_args()

    symbols = load_symbols(a.config)
    if a.symbols:
        want = [s.strip() for s in a.symbols.split(',')]
        symbols = {k: v for k, v in symbols.items() if k in want}
    since = (datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc) if a.since
             else datetime.now(timezone.utc) - timedelta(days=730)).timestamp()
    api = Api(a.api, a.token, a.dry_run)

    if a.fake:
        mt5 = FakeMT5(symbols)
        log('MODE FACTICE : donnees synthetiques, MT5 non utilise')
    else:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            sys.exit('Le paquet MetaTrader5 est absent : pip install -r bridge/requirements.txt (Python 3.8 a 3.14, Windows 64 bits).')
        ok = mt5.initialize(path=a.terminal) if a.terminal else mt5.initialize()
        if not ok:
            sys.exit(f'Impossible de se connecter au terminal MT5 ({mt5.last_error()}).\n'
                     'Ouvrez le terminal FTMO MT5, connectez-vous a votre compte, puis relancez. '
                     'Si plusieurs terminaux sont installes : --terminal "C:/Program Files/.../terminal64.exe".')
        ti = mt5.terminal_info()
        log(f'terminal MT5 connecte : {getattr(ti, "name", "?")} / compte {getattr(mt5.account_info(), "server", "?")}')
    present = {}
    for iid, sym in symbols.items():
        if mt5.symbol_select(sym, True) and mt5.symbol_info(sym) is not None:
            present[iid] = sym
        else:
            log(f'symbole introuvable chez ce courtier : {sym} ({iid}) - ignore. Verifier le nom dans la Surveillance du marche.')
    if not present:
        sys.exit('Aucun symbole utilisable.')
    br = Bridge(mt5, api, present, ServerClock(a.server_offset), since, a.interval)
    try:
        br.run(a.max_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        mt5.shutdown()


if __name__ == '__main__':
    main()
