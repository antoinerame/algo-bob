"""Synthetic rebuilds of the examples of Bob's formation PDF (10/2026). Candles are (open, high, low, close).
The PDF only gives pictures: the candles below are RE-CONSTRUCTED from the text of each case (colours, which wick goes
where, which candle validates). PDF candle k is index k-1 when the case starts on its candle 1. Prices are fictive."""
CASES = {}

# ch.3 rule 1: two highs in a row -> the missing low is the lowest point between them (wick of candle 4)
CASES['ch3_rule1_missing_low'] = dict(bars=[
    (10, 10.0, 9.0, 9.2),        # 0 bearish start: init H0, L0
    (9.2, 14.2, 9.1, 14.0),      # 1 bull
    (14.0, 15.0, 11.8, 12.0),    # 2 bear: breaks H0, validates H=15
    (12.0, 12.0, 10.5, 11.0),    # 3 bear
    (11.0, 11.0, 9.8, 10.0),     # 4 bear: lowest wick
    (10.0, 13.5, 9.9, 13.0),     # 5 bull
    (13.0, 16.5, 12.9, 16.0),    # 6 bull: breaks the high 15
    (16.0, 16.2, 14.8, 15.0),    # 7 bear: validates the new high (bar 6)
], expect_idx=[(2, 'H'), (4, 'L'), (6, 'H')], tail_from=2)

# ch.3 rule 2 A/B/C: validation candle
_base = [(10, 10.5, 9.5, 10.4), (10.4, 11.0, 10.3, 10.9), (10.9, 12.0, 10.8, 11.9), (11.9, 13.0, 11.8, 12.9)]
CASES['ch3_rule2A_bear_does_not_exceed'] = dict(bars=_base + [(12.9, 12.95, 12.0, 12.2), (12.2, 12.4, 11.5, 11.6)],
    expect_idx=[(3, 'H')], tail_from=3)
CASES['ch3_rule2B_bear_wick_exceeds'] = dict(bars=_base + [(12.9, 13.5, 12.0, 12.2), (12.2, 12.4, 11.5, 11.6)],
    expect_idx=[(4, 'H')], tail_from=3)
CASES['ch3_rule2C_several_bears_point_moves'] = dict(bars=_base + [(12.9, 12.95, 12.0, 12.2), (12.2, 13.4, 11.5, 11.6), (11.6, 12.0, 11.0, 11.2)],
    expect_idx=[(5, 'H')], tail_from=3)

# ch.3 rule 3: a top inside the range is not a point; an EQUAL level is a break (bar 6 returns exactly on the high)
CASES['ch3_rule3_equal_high_is_break'] = dict(bars=[
    (10, 10.5, 9.0, 9.2),        # 0
    (9.2, 12.0, 9.1, 11.8),      # 1 bull
    (11.8, 14.0, 11.0, 11.5),    # 2 bear: validates H=14 (bar 2)
    (11.5, 11.9, 9.5, 10.0),     # 3 bear  (will be the missing low)
    (10.0, 13.0, 9.9, 12.5),     # 4 bull: top inside the range (13 < 14) -> NOT a point
    (12.5, 12.6, 11.0, 11.2),    # 5 bear
    (11.8, 14.0, 11.0, 11.2),    # 6 bear touching exactly 14 -> equal = break, validates at once
], expect_idx=[(2, 'H'), (3, 'L'), (6, 'H')], tail_from=2, eq_break=True)

# ch.7 exception 1 (XAUUSD H1 14/10/2026, simplified): low on a tiny bullish doji (bar 4), then two bearish candles go
# lower BEFORE the big bearish candle 7 makes its high first. Right reading: low shifted to the lowest wick (bar 6).
CASES['ch7_exception1_shifted_low'] = dict(bars=[
    (10, 11.2, 9.8, 11.0),       # 0 bull (init)
    (11.0, 11.1, 10.4, 10.5),    # 1 bear
    (10.5, 13.5, 10.4, 13.0),    # 2 bull: breaks the high
    (13.0, 13.2, 11.5, 12.0),    # 3 bear: validates H=13.5 (bar 2)
    (12.0, 12.3, 10.0, 12.05),   # 4 tiny bullish doji: breaks the low 10.4, validates L=10.0
    (12.05, 12.1, 8.5, 9.0),     # 5 bear goes lower
    (9.0, 9.1, 8.0, 8.4),        # 6 bear goes even lower (lowest wick)
    (8.4, 14.0, 7.5, 7.6),       # 7 big bearish: high first (breaks 13.5), then low
    (7.6, 9.6, 7.4, 9.5),        # 8 bull: validates the next low
], classic_idx=[(2, 'H'), (4, 'L'), (7, 'H')], fixed_idx=[(2, 'H'), (6, 'L'), (7, 'H')], tail_from=2)

# ch.7 exception 2 (XAUUSD H1 28/09/2026, simplified): L, then bull with a long upper wick (5), then a bull whose long
# lower wick breaks the low (6), then a big bearish (7). Classic: L H L H L ; right reading: the first low and the wick
# high (5) disappear, remaining: low+high on candle 6 (bullish: low then high) then the low of candle 7.
CASES['ch7_exception2_no_candle_to_validate'] = dict(bars=[
    (10, 10.8, 9.5, 10.5),       # 0 bull (init)
    (10.5, 10.6, 8.0, 9.0),      # 1 bear: breaks the low
    (9.0, 9.5, 8.2, 9.3),        # 2 bull: validates L=8.0 (bar 1)
    (9.3, 10.0, 9.1, 9.8),       # 3 bull
    (10.2, 10.4, 9.7, 9.9),      # 4 BEAR (breaks the run of bulls: otherwise the low simply moves, repaint rule)
    (10.2, 10.7, 10.0, 10.5),    # 5 bull, long upper wick
    (10.5, 11.0, 7.5, 10.9),     # 6 bull, long lower wick breaks the low
    (10.9, 11.0, 7.0, 7.6),      # 7 big bearish, breaks again
    (7.6, 8.6, 7.4, 8.5),        # 8 bull: validates L=7.0 (bar 7)
], classic_idx=[(1, 'L'), (5, 'H'), (6, 'L'), (6, 'H'), (7, 'L')], fixed_idx=[(6, 'L'), (6, 'H'), (7, 'L')], tail_from=1)

# ch.10 exercise 3 corrige (rebuilt, PDF candle k = index k-1): yellow doji 8 breaks the high and validates it at once;
# bulls 9 and 10 break it again; bull 11 has the long low wick; bear 12 validates. Letter reading: high on the doji.
# Right reading: the high is shifted on the wick of candle 10 (the doji stays its validation candle).
CASES['ex3_corrige_high_shifted_on_candle10'] = dict(bars=[
    (70, 72, 67.5, 68),          # 0  (PDF 1) start on the high
    (68, 68.2, 63.5, 64),        # 1
    (64, 64.2, 60, 62),          # 2
    (62, 64.5, 61.5, 64),        # 3  bull validates the low
    (65, 65.6, 63.8, 64.5),      # 4  bear
    (66, 68, 64.8, 65),          # 5  bear, wick = missing high between the two lows
    (65.5, 66.5, 56, 66),        # 6  bull, long low wick breaks the low, validates its own low
    (66.0, 72.5, 65.8, 65.95),   # 7  yellow doji: high first, breaks 68, validates at once
    (66, 73, 65.9, 70),          # 8  bull breaks again
    (70, 74.5, 69.8, 71),        # 9  bull, highest wick
    (71, 72.5, 62, 72),          # 10 bull, long low wick
    (72, 75, 69, 70),            # 11 bear validates
], classic_idx=[(5, 'H'), (6, 'L'), (7, 'H'), (10, 'L'), (11, 'H')], fixed_idx=[(5, 'H'), (6, 'L'), (9, 'H'), (10, 'L'), (11, 'H')], tail_from=5)

# doji trap (ch.7, 'piege du doji'): same tiny candle, blue = low then high, yellow = high then low.
# Blue doji breaking both sides keeps L H L H; with the colour of the previous candle for a perfectly neutral doji.
CASES['doji_colour_neutral_takes_previous'] = dict(neutral=True)
