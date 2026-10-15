"""Tests for learning math (pure)."""

from datetime import date, datetime, timedelta

import pytest

from custom_components.tou_battery_planner.learning import (
    DEFAULT_SHAPE,
    K_DEFAULT,
    build_load_profile,
    calibration_factor,
    combine_load,
    daily_totals,
    flat_profile,
    mean_absolute_error,
)


def _samples(days: int, value=lambda h: 1.0 + h / 24, start=datetime(2026, 9, 1)):
    return [(start + timedelta(days=d, hours=h), value(h)) for d in range(days) for h in range(24)]


def test_shape_sums_to_one():
    assert sum(DEFAULT_SHAPE) == pytest.approx(1.0)
    assert sum(flat_profile(55)) == pytest.approx(55)


def test_provisional_below_seven_days():
    prof = build_load_profile(_samples(6), 55)
    assert prof.provisional
    assert prof.daily_kwh == pytest.approx(55)


def test_median_resists_outliers():
    samples = _samples(10)
    # one-off 20 kWh spike at 18:00 on one day
    samples = [(ts, 20.0 if (ts.day == 3 and ts.hour == 18) else v) for ts, v in samples]
    prof = build_load_profile(samples, 55)
    assert not prof.provisional
    assert prof.hourly[18] == pytest.approx(1.0 + 18 / 24)
    assert prof.weekend is None


def test_weekday_weekend_split_needs_four_weeks():
    samples = _samples(28, value=lambda h: 1.0)
    samples = [(ts, 3.0 if ts.weekday() >= 5 else v) for ts, v in samples]
    prof = build_load_profile(samples, 55)
    assert prof.weekend is not None
    assert prof.for_date(date(2026, 9, 5))[0] == 3.0  # Saturday
    assert prof.for_date(date(2026, 9, 7))[0] == 1.0  # Monday


def test_combine_subtracts_ev_and_clamps():
    t = datetime(2026, 9, 1, 12)
    out = combine_load([(t, 5000.0)], [[(t, 7000.0)]], 0.001, [0.001])
    assert out == [(t, 0.0)]
    out = combine_load([(t, 5000.0)], [[(t, 2000.0)]], 0.001, [0.001])
    assert out[0][1] == pytest.approx(3.0)


def test_daily_totals_drops_incomplete_days():
    full = _samples(1)
    partial = [s for s in _samples(1, start=datetime(2026, 9, 2)) if s[0].hour > 5]
    totals = daily_totals(full + partial)
    assert date(2026, 9, 1) in totals
    assert date(2026, 9, 2) not in totals


def test_calibration_default_then_median_and_clamp():
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(8)]
    pv = {d: 60.0 for d in days}
    sw = {d: 10.0 for d in days}
    k, _ = calibration_factor({d: pv[d] for d in days[:4]}, sw)
    assert k == K_DEFAULT
    k, used = calibration_factor(pv, sw)
    assert k == pytest.approx(6.0) and len(used) == 8
    k, _ = calibration_factor({d: 200.0 for d in days}, sw)
    assert k == 9.0
    sw[days[0]] = 0.5  # excluded: below 1 MJ/m2
    _, used = calibration_factor(pv, sw)
    assert len(used) == 7


def test_mae():
    assert mean_absolute_error([]) is None
    assert mean_absolute_error([(10, 12), (20, 17)]) == pytest.approx(2.5)
