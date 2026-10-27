"""Examples of Bob's structure PDF (Oct 2026), rebuilt from the text (the PDF has pictures only): chapter 3 rules,
chapter 7 exceptions 1 and 2, exercise 3 corrigé. Exceptions are on by default; exceptions=False = rules to the letter."""
import numpy as np
import pytest

from app.engine.structure import build_structure, candle_colors
from structure_pdf_cases import CASES

NAMED = {k: v for k, v in CASES.items() if 'bars' in v}


def seq(bars, eq, tail, **kw):
    a = np.array(bars, dtype=float)
    p, _ = build_structure(a[:, 0], a[:, 1], a[:, 2], a[:, 3], eq_break=eq, **kw)
    return [(q.idx, q.kind) for q in p if q.idx >= tail]


@pytest.mark.parametrize('name', sorted(NAMED))
def test_pdf_case_right_reading(name):
    cs = NAMED[name]
    exp = cs.get('fixed_idx', cs.get('expect_idx'))
    assert seq(cs['bars'], cs.get('eq_break', False), cs['tail_from'])[:len(exp)] == exp


@pytest.mark.parametrize('name', sorted(n for n, c in NAMED.items() if 'classic_idx' in c))
def test_pdf_case_letter_reading_without_exceptions(name):
    cs = NAMED[name]
    exp = cs['classic_idx']
    assert seq(cs['bars'], cs.get('eq_break', False), cs['tail_from'], exceptions=False)[:len(exp)] == exp


def test_neutral_candle_takes_previous_colour():
    col = candle_colors(np.array([10, 11, 12, 12.0]), np.array([11, 10, 13, 12.0]))
    assert list(col) == [1, -1, 1, 1]


def test_equal_level_does_not_create_a_phantom_duplicate():
    # a repainted point must not 'break itself' with eq_break (256 phantom points on XAUUSD before the fix)

    bars = CASES['ch3_rule1_missing_low']['bars']
    a = np.array(bars, dtype=float)
    p, _ = build_structure(a[:, 0], a[:, 1], a[:, 2], a[:, 3], eq_break=True)
    assert all(not (p[i].kind == p[i - 1].kind and p[i].idx == p[i - 1].idx) for i in range(1, len(p)))
