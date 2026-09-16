"""Unit tests for the stats API's bucketing math (pure logic, no DB)."""

from datetime import date, datetime, timedelta, timezone

from routes.stats import _bucket_ranges, _month_shift, _roll, _window_start


def test_month_shift_wraps_year():
    assert _month_shift(date(2026, 1, 17), -1) == date(2025, 12, 1)
    assert _month_shift(date(2026, 12, 1), 3) == date(2027, 3, 1)
    assert _month_shift(date(2026, 5, 9), -5) == date(2025, 12, 1)


def test_month_boundaries_handle_leap_february():
    assert _month_shift(date(2024, 2, 10), 1) == date(2024, 3, 1)
    assert _month_shift(date(2024, 2, 1), 1) - timedelta(days=1) == date(2024, 2, 29)


def test_window_start_matches_first_bucket():
    for period in ("day", "week", "month"):
        ranges = _bucket_ranges(period, 5)
        assert len(ranges) == 5
        assert ranges[0][0] == _window_start(period, 5)
        for (s1, e1), (s2, e2) in zip(ranges, ranges[1:]):
            assert e1 < s2


def test_roll_day_zero_fill_and_order():
    today = datetime.now(timezone.utc).date()
    daily = {(today - timedelta(days=1)).isoformat():
             {"tokens_in": 10, "tokens_out": 5, "turns": 2}}
    buckets = _roll("day", 3, daily, ("tokens_in", "tokens_out", "turns"))
    assert [b["bucket_start"] for b in buckets] == [
        (today - timedelta(days=2)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
        today.isoformat(),
    ]
    assert buckets[0]["tokens_in"] == 0 and buckets[0]["turns"] == 0
    assert buckets[1]["tokens_in"] == 10 and buckets[1]["turns"] == 2
    assert buckets[2]["tokens_in"] == 0


def test_roll_week_aggregates_days():
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    daily = {(monday + timedelta(days=i)).isoformat():
             {"tokens_in": 1, "tokens_out": 1, "turns": 1} for i in range(7)}
    buckets = _roll("week", 2, daily, ("tokens_in", "tokens_out", "turns"))
    assert len(buckets) == 2
    assert buckets[1]["tokens_in"] == 7 and buckets[1]["turns"] == 7
    assert buckets[0]["tokens_in"] == 0


def test_roll_month_spans_month_boundary():
    today = datetime.now(timezone.utc).date()
    first_of_prev = _month_shift(today, -1)
    last_of_prev = _month_shift(today, 0) - timedelta(days=1)
    daily = {
        first_of_prev.isoformat(): {"turns": 1, "tokens_in": 2, "tokens_out": 3},
        last_of_prev.isoformat(): {"turns": 1, "tokens_in": 2, "tokens_out": 3},
    }
    buckets = _roll("month", 2, daily, ("turns", "tokens_in", "tokens_out"))
    assert len(buckets) == 2
    assert buckets[0]["turns"] == 2
    assert buckets[0]["tokens_in"] == 4
    assert buckets[0]["tokens_out"] == 6
    assert buckets[1]["turns"] == 0


def test_roll_ignores_data_outside_window():
    out_of_window = _window_start("month", 2) - timedelta(days=1)
    daily = {out_of_window.isoformat():
             {"turns": 5, "tokens_in": 9, "tokens_out": 9}}
    buckets = _roll("month", 2, daily, ("turns", "tokens_in", "tokens_out"))
    assert len(buckets) == 2
    assert all(b["turns"] == 0 and b["tokens_in"] == 0 for b in buckets)