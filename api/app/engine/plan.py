"""Current plan: the orders to place now, built from the latest structure state (last closed bar) and the live tick.

EPA = limit order at the (param-adjusted) zone entry, valid until U breaks (or the opposite point Lb breaks on the
way to the zone). FT = market order right after the confirmation bar. Orders already running are 'en position'.
"""
from __future__ import annotations

import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo   # needs the 'tzdata' pip package on python-slim images

from .. import config
from . import backtest as B

PARIS = ZoneInfo('Europe/Paris')


def fp(ts, fmt='%d/%m %H:%M'):
    """Time shown in Paris time, like the chart axis and the tickets of the site."""
    return datetime.fromtimestamp(ts, PARIS).strftime(fmt)


RECENT = 48 * 3600      # a finished / cancelled setup stays visible this long
DEFAULT_ACCOUNT = dict(balance=100000.0, currency='USD', risk_pct=0.5)
ORDER_RANK = {'à placer': 0, 'en position': 1, 'annulé': 2, 'exécuté': 3}


def fnum(x, dec) -> str:
    """French number: 4 145,64"""
    return f'{x:,.{dec}f}'.replace(',', ' ').replace('.', ',')


def normalize_account(raw: dict | None) -> dict:
    a = dict(DEFAULT_ACCOUNT)
    a.update({k: v for k, v in (raw or {}).items() if k in a and v is not None})
    try:
        a['balance'], a['risk_pct'] = float(a['balance']), float(a['risk_pct'])
    except (TypeError, ValueError):
        raise B.ParamError("Compte invalide : balance et risk_pct doivent être des nombres.")
    a['currency'] = str(a['currency']).upper()
    if a['balance'] <= 0 or not (0 < a['risk_pct'] <= 100):
        raise B.ParamError("Compte invalide : balance > 0 et 0 < risk_pct <= 100.")
    return a


def quote_to_account(quote: str, account_ccy: str, rates: dict) -> float:
    """Value of 1 unit of the quote currency in the account currency, from latest prices (EURUSD, USDJPY, GBPUSD)."""
    def usd_per(ccy):
        if ccy == 'USD':
            return 1.0
        if ccy == 'EUR':
            return rates['EURUSD']
        if ccy == 'GBP':
            return rates['GBPUSD']
        if ccy == 'JPY':
            return 1.0 / rates['USDJPY']
        raise KeyError(ccy)
    return usd_per(quote) / usd_per(account_ccy)


def needed_rates(quote: str, account_ccy: str) -> list[str]:
    pair = {'EUR': 'EURUSD', 'GBP': 'GBPUSD', 'JPY': 'USDJPY'}
    return sorted({pair[c] for c in (quote, account_ccy) if c in pair})


def lot_size(entry, sl, specs, account, rates):
    """(lots, actual risk in account currency, warning|None). lots rounded DOWN to volume_step, 0 below volume_min."""
    try:
        qta = quote_to_account(specs['quote'], account['currency'], rates)
    except (KeyError, ZeroDivisionError, TypeError):
        return 0.0, 0.0, f"Taux de change indisponible pour convertir {specs['quote']} en {account['currency']} : lots non calculés."
    budget = account['balance'] * account['risk_pct'] / 100
    per_lot = abs(entry - sl) * specs['contract'] * qta
    if per_lot <= 0:
        return 0.0, 0.0, 'Distance au stop nulle : lots non calculés.'
    step = specs.get('volume_step') or 0.01
    lots = math.floor(budget / per_lot / step + 1e-9) * step
    lots = round(lots, 8)
    if lots < (specs.get('volume_min') or 0):
        return 0.0, 0.0, (f"Risque trop faible pour le lot minimum ({specs['volume_min']}) : il faudrait risquer "
                          f"{fnum(specs['volume_min'] * per_lot, 2)} {account['currency']} au lieu de {fnum(budget, 2)}.")
    return lots, round(lots * per_lot, 2), None


# ------------------------------------------------------------------ plan

def _trend(st, dec):
    """Current trend from the walk events, completed by the structure breaks that came after the last event."""
    ev = st.events
    if not ev:
        return dict(dir=0, leg=None, n=0, prot=None, last_break_t=None), None
    last = ev[-1]
    brk = next((e for e in reversed(ev) if e['ev'] == 'BREAK'), None)
    d = last['d']
    prot = last['SL'] if last['ev'] == 'FT' else last['prot']
    n_break, leg = (brk['n'], brk['leg']) if brk and brk['d'] == d else (0, None)
    later = []
    for pt in reversed(st.pts):
        if pt.known <= last['known']:
            break
        if pt.is_break:
            later.append(pt.kind)
    if later:
        run = 0
        for k in later:      # newest first: trailing run of breaks of the same kind
            if k == later[0]:
                run += 1
            else:
                break
        if run >= 2:
            d, n_break, leg, prot = (1 if later[0] == 'H' else -1), run, 'debut', None
        else:
            d = 0
    px = float(st.c[-1])
    if d and prot is not None and (px - prot) * d < 0:
        d = 0
    t_brk = int(st.t[(brk or last)['known']] + 3600)
    return dict(dir=d, leg=leg, n=n_break, prot=None if prot is None else round(float(prot), dec + 1),
                last_break_t=t_brk), brk


def _order(setup, d, typ, status, entry, sl, tp, rr, created_t, cancel_if, note, specs, account, rates, warnings, dec,
           iid):
    lots, risk, w = lot_size(entry, sl, specs, account, rates) if status in ('à placer', 'en position') \
        else (0.0, 0.0, None)
    if w:
        warnings.append(f'{setup} {iid} : {w}')
    return dict(id=f'{iid}-{setup}-{created_t}', setup=setup, side='BUY' if d == 1 else 'SELL', type=typ, status=status,
                entry=round(entry, dec + 1), sl=round(sl, dec + 1), tp=round(tp, dec + 1), rr=round(rr, 2), lots=lots,
                risk_money=risk, risk_pct=round(risk / account['balance'] * 100, 3), created_t=int(created_t),
                cancel_if=cancel_if, note=note)


def make_plan(iid, st, p, account, specs, rates, tick, now, trades, hist):
    """Pure plan builder (no store access). trades = simulation trades for the params (for open_trades)."""
    dec = config.BY_ID[iid]['dec'] if iid in config.BY_ID else 5
    n, hold = len(st.t), p['max_hold_hours']
    asof = int(st.t[-1] + 3600)
    warnings, orders = [], []
    last = dict(tick) if tick else dict(t=asof, bid=float(st.c[-1]), ask=None, source='bar')
    px = float(last['bid'])
    if not tick:
        warnings.append('Aucun tick récent : le dernier prix est la clôture de la dernière barre H1.')
    delay = max(0, int(now - asof))
    wd = time.gmtime(now)
    weekend = wd.tm_wday == 5 or (wd.tm_wday == 4 and wd.tm_hour >= 22) or (wd.tm_wday == 6 and wd.tm_hour < 22)
    if delay > 3 * 3600 and not weekend:
        warnings.append(f'Données en retard de {delay // 3600} h : la dernière barre H1 clôture à '
                        f'{fp(asof)} (heure de Paris).')
    trend, brk = _trend(st, dec)
    d_tr = trend['dir']
    levels = None
    if d_tr and brk and brk['d'] == d_tr:
        levels = dict(U=brk['U'], Z=brk['Z'], zlo=brk['zlo'], Lb=brk['Lb'], prot=trend['prot'])
        levels = {k: None if v is None else round(float(v), dec + 1) for k, v in levels.items()}
    allowed = lambda d: p['direction'] == 'both' or (p['direction'] == 'long') == (d == 1)

    def mk(*a):
        return _order(*a, specs, account, rates, warnings, dec, iid)

    # ---- EPA: limit order of the latest break event
    if brk and d_tr == brk['d'] and p['setups']['epa'] and allowed(brk['d']):
        e, d = brk, brk['d']
        created = int(st.t[e['known']] + 3600)
        sc = B.epa_scan(st, e, p)
        entry, U, Lb = sc['entry'], e['U'], e['Lb']
        lv = B.levels(d, entry, e['zlo'], U, p)
        side = 'BUY' if d == 1 else 'SELL'
        if lv and asof - created <= hold * 3600 + 3600:
            sl, tp, risk = lv
            rr = abs(tp - entry) / risk
            path_lb = (Lb - entry) * d > 0     # Lb must break before the zone is reached (IPA leg)
            lb_first = p['epa_cancel_on_lb'] and sc['lb'] >= 0 and path_lb and all(x < 0 or sc['lb'] <= x for x in (sc['fill'], sc['u']))
            if rr < p['min_rr']:
                warnings.append(f'EPA ignoré : RR {fnum(rr, 2)} inférieur au minimum {fnum(p["min_rr"], 2)}.')
            elif sc['state'] == 'skip':
                warnings.append('EPA ignoré : à la confirmation de la cassure le prix était déjà dans la zone '
                                '(pas d\'ordre limite possible).')
            else:
                status, cancel_if, note = 'à placer', [], ''
                lbl = f'{side} LIMIT'
                if lb_first:
                    status = 'annulé'
                    note = (f'{lbl} annulé : le point {fnum(Lb, dec)} a cassé avant que le prix atteigne la zone '
                            f'(le setup passe en logique FT).')
                elif sc['state'] == 'cancel_u':
                    status = 'annulé'
                    note = f'{lbl} annulé : le prix a cassé {fnum(U, dec)} avant d\'atteindre la zone.'
                elif sc['state'] == 'filled':
                    tr = B._trade(st, p, p['cost_bp'], 'EPA', True, d, sc['fill'], entry, sl, tp, skip_tp0=True)
                    if tr['s'] == 'en cours':
                        status = 'en position'
                        note = (f'{lbl} exécuté le {fp(tr["ti"])} (heure de Paris) : '
                                f'position ouverte, SL {fnum(sl, dec)}, TP {fnum(tp, dec)}.')
                    else:
                        status = 'exécuté'
                        note = (f'Setup terminé ({tr["s"]}) le {fp(tr["to"])} (heure de Paris).')
                        if asof - tr['to'] > RECENT:
                            status = None
                elif sc['expired']:
                    status = 'annulé'
                    note = f'{lbl} annulé : zone non atteinte en {hold} h.'
                else:
                    cancel_if = [f'Annuler si le prix casse {fnum(U, dec)} avant d\'atteindre la zone']
                    if path_lb and p['epa_cancel_on_lb']:
                        cancel_if.append(f'Annuler si le prix casse {fnum(Lb, dec)} avant la zone (le setup passe en FT)')
                    elif path_lb:
                        cancel_if.append(f'Info : si {fnum(Lb, dec)} casse d\'abord, le setup passe en logique FT '
                                         f'(ordre conservé, option « annuler si Lb casse » désactivée)')
                        if sc['lb'] >= 0:
                            warnings.append(f'EPA : {fnum(Lb, dec)} a déjà cassé avant la zone ; l\'ordre est conservé '
                                            f'(option « annuler si Lb casse » désactivée).')
                    cancel_if.append(f'Annuler si la zone n\'est pas touchée dans les {hold} h suivant la cassure')
                    note = (f'{lbl} à {fnum(entry, dec)} (zone {fnum(min(e["zlo"], e["Z"]), dec)} à '
                            f'{fnum(max(e["zlo"], e["Z"]), dec)}), SL {fnum(sl, dec)}, TP {fnum(tp, dec)}, RR {fnum(rr, 2)}.')
                    lo, hi = p['entry_hours']
                    if (lo, hi) != (0, 24):
                        note += f' Fenêtre d\'entrée {lo} h-{hi} h UTC (13 h-14 h Paris en hiver, 14 h-15 h en été -> mettre 12-14 pour couvrir les deux) : un touché hors fenêtre n\'est pas pris.'
                    # live tick checks (the current bar is not closed yet)
                    if (px - U) * d >= 0:
                        status, cancel_if = 'annulé', []
                        note = f'{lbl} annulé : le prix actuel ({fnum(px, dec)}) a déjà cassé {fnum(U, dec)}.'
                    elif (px - sl) * d <= 0:
                        status, cancel_if = 'annulé', []
                        note = f'{lbl} annulé : le prix actuel ({fnum(px, dec)}) a déjà dépassé le SL {fnum(sl, dec)}.'
                    elif (px - entry) * d <= 0:
                        warnings.append(
                            f'{lbl} à {fnum(entry, dec)} : le prix actuel ({fnum(px, dec)}) est déjà '
                            f'{"sous" if d == 1 else "au-dessus de"} ce niveau, l\'ordre serait exécuté tout de suite '
                            f'ou refusé. Zone probablement touchée dans la barre en cours.')
                if status:
                    orders.append(mk('EPA', d, 'LIMIT', status, entry, sl, tp, rr, created, cancel_if, note))

    # ---- FT: market order right after the confirmation bar, or position already running
    for e in reversed(st.events):
        if e['ev'] != 'FT':
            continue
        created = int(st.t[e['known']] + 3600)
        if asof - created > hold * 3600 + 3600:
            break
        d, s = e['d'], e['known'] + 1
        if not allowed(d) or not (p['setups']['ft_valid'] if e['valid'] else p['setups']['ft_ipa']):
            continue
        lbl = 'FT valide' if e['valid'] else 'FT en IPA'
        side = 'BUY' if d == 1 else 'SELL'
        if s < n:
            P0 = float(st.o[s])
            lv = B.levels(d, P0, e['SL'], e['TP'], p)
            if lv is None or abs(lv[1] - P0) / lv[2] < p['min_rr'] or not B._hour_ok(st.t[s], p['entry_hours']):
                continue
            sl, tp, risk = lv
            tr = B._trade(st, p, p['cost_bp'], 'FT', e['valid'], d, s, P0, sl, tp)
            if tr['s'] == 'en cours':
                note = (f'{lbl} {side} ouvert le {fp(tr["ti"])} (heure de Paris) à '
                        f'{fnum(P0, dec)} : SL {fnum(sl, dec)}, TP {fnum(tp, dec)}.')
                orders.append(mk('FT', d, 'MARKET', 'en position', P0, sl, tp, tr['rr'], created, [], note))
            elif tr['to'] is not None and asof - tr['to'] <= RECENT and e is next(
                    x for x in reversed(st.events) if x['ev'] == 'FT'):
                note = f'Setup terminé ({tr["s"]}) le {fp(tr["to"])} (heure de Paris).'
                orders.append(mk('FT', d, 'MARKET', 'exécuté', P0, sl, tp, tr['rr'], created, [], note))
        else:
            # confirmed on the last closed bar: enter at the current price
            entry = float(last['ask'] if d == 1 and last.get('ask') else last['bid'])
            lv = B.levels(d, entry, e['SL'], e['TP'], p)
            if lv is None:
                warnings.append(f'{lbl} ignoré : le prix actuel ({fnum(entry, dec)}) a déjà dépassé le SL ou le TP.')
                continue
            sl, tp, risk = lv
            rr = abs(tp - entry) / risk
            if rr < p['min_rr']:
                warnings.append(f'{lbl} ignoré : RR {fnum(rr, 2)} inférieur au minimum {fnum(p["min_rr"], 2)}.')
                continue
            if not B._hour_ok(asof, p['entry_hours']):
                warnings.append(f'{lbl} ignoré : hors de la fenêtre d\'entrée {p["entry_hours"][0]} h-{p["entry_hours"][1]} h UTC.')
                continue
            note = (f'{side} au marché maintenant (~{fnum(entry, dec)}), SL {fnum(sl, dec)}, TP {fnum(tp, dec)}, '
                    f'RR {fnum(rr, 2)}. {lbl} confirmé à la clôture de la barre de '
                    f'{fp(asof - 3600, '%H:%M')} (heure de Paris).')
            orders.append(mk('FT', d, 'MARKET', 'à placer', entry, sl, tp, rr, created,
                             [f'Ne pas entrer si le prix a déjà atteint {fnum(tp, dec)} ou {fnum(sl, dec)}'], note))

    orders.sort(key=lambda o: (ORDER_RANK[o['status']], -o['created_t']))
    open_trades = [B.fmt_trade(x, dec) for x in trades if x['s'] == 'en cours' and B.enabled(x, p['setups'])]
    return dict(asof=asof, last=last, data_delay_s=delay, trend=trend, levels=levels, orders=orders,
                open_trades=open_trades, hist=hist, warnings=warnings, account=account, params=p)


def _last_price(iid):
    from .. import store
    tk = store.last_tick(iid)
    if tk and tk.get('bid'):
        return float(tk['bid'])
    bars = store.load_bars(iid)
    return float(bars['close'].iloc[-1]) if len(bars) else None


def build_plan(iid: str, params=None, account=None) -> dict:
    """Payload of POST /api/plan/{id}."""
    from .. import store
    p = B.normalize_params(params, iid)
    acc = normalize_account(account)
    st, tr, _ = B.get_sim(iid, p)
    specs = store.specs(iid)
    rates = {}
    for sym in needed_rates(specs['quote'], acc['currency']):
        v = _last_price(sym)
        if v:
            rates[sym] = v
    return make_plan(iid, st, p, acc, specs, rates, store.last_tick(iid), time.time(), tr, B.hist(iid))
