"""Shared configuration: instruments, data sources, refresh cadence.

Contract specs (contract, quote) are FTMO MT5 defaults to be CHECKED in MT5 (Symbol > Specification).
When the MT5 bridge is running it pushes the real specs (POST /api/ingest/specs) and those win.
"""
import os

DATA_DIR = os.environ.get('DATA_DIR', '/data')
HISTORY_START = os.environ.get('HISTORY_START', '2010-01-01')
REFRESH_SECONDS = int(os.environ.get('REFRESH_SECONDS', '60'))
# 'dukascopy' (default, free, no account, H1 bars with ~1 h delay) or 'mt5' (bars pushed by bridge/mt5_bridge.py)
PRICE_SOURCE = os.environ.get('PRICE_SOURCE', 'dukascopy')
DUKASCOPY_URL = os.environ.get('DUKASCOPY_URL', 'http://datafeed.dukascopy.com/datafeed')  # https is blocked here, http works

INSTRUMENTS = [
    # id, label, Dukascopy symbol, Dukascopy price factor, display decimals, FTMO cost bp round trip,
    # MT5 symbol on FTMO, contract size (units per 1.00 lot), profit currency
    dict(id='XAUUSD', label='Or', duka='XAUUSD', point=1e3, dec=2, cost_bp=1.6,  # FTMO: 0.30 $ spread round trip + 0.35 $ fees ≈ 0.65 $ at ~4 000 $
         mt5='XAUUSD', contract=100, quote='USD'),
    dict(id='XAGUSD', label='Argent', duka='XAGUSD', point=1e3, dec=3, cost_bp=4.0, mt5='XAGUSD', contract=5000, quote='USD'),
    dict(id='EURUSD', label='Euro / Dollar', duka='EURUSD', point=1e5, dec=5, cost_bp=0.8, mt5='EURUSD', contract=100000, quote='USD'),
    dict(id='GBPUSD', label='Livre / Dollar', duka='GBPUSD', point=1e5, dec=5, cost_bp=1.0, mt5='GBPUSD', contract=100000, quote='USD'),
    dict(id='USDJPY', label='Dollar / Yen', duka='USDJPY', point=1e3, dec=3, cost_bp=0.9, mt5='USDJPY', contract=100000, quote='JPY'),
    dict(id='US500', label='S&P 500', duka='USA500IDXUSD', point=1e3, dec=1, cost_bp=1.5, mt5='US500.cash', contract=1, quote='USD'),
    dict(id='US100', label='Nasdaq 100', duka='USATECHIDXUSD', point=1e3, dec=1, cost_bp=1.5, mt5='US100.cash', contract=1, quote='USD'),
    dict(id='GER40', label='DAX', duka='DEUIDXEUR', point=1e3, dec=1, cost_bp=1.5, mt5='GER40.cash', contract=1, quote='EUR'),
    dict(id='USOIL', label='Pétrole WTI', duka='LIGHTCMDUSD', point=1e3, dec=2, cost_bp=3.0, mt5='USOIL.cash', contract=100, quote='USD'),
    dict(id='UKOIL', label='Pétrole Brent', duka='BRENTCMDUSD', point=1e3, dec=2, cost_bp=3.0, mt5='UKOIL.cash', contract=100, quote='USD'),
]
BY_ID = {x['id']: x for x in INSTRUMENTS}
