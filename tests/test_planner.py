"""Tests for the pure planner."""

from datetime import time

import pytest

from custom_components.tou_battery_planner.planner import (
    Role,
    Slot,
    current_slot,
    default_slots,
    desired_caps,
    in_charge_window,
    plan,
    spend_need_kwh,
)

SLOTS = default_slots(6)
DEADLINE = time(4, 30)


def caps(now, profile="cloudy", spike=False, gate=False, ev=False, soc=30, hold=30, target=45):
    res = desired_caps(SLOTS, now, soc, profile, target, 20, hold, spike, gate, ev, DEADLINE)
    assert res is not None
    return res.caps


@pytest.mark.parametrize(
    ("now", "kwargs", "expected"),
    [
        (time(22, 0), {}, [45, 45, 20, 20, 45, 45]),
        (time(22, 0), {"gate": True}, [45, 45, 20, 20, 30, 45]),
        (time(4, 45), {"gate": True}, [45, 45, 20, 20, 45, 45]),
        (time(9, 0), {}, [45, 45, 20, 20, 45, 45]),
        (time(9, 0), {"spike": True}, [45, 20, 20, 20, 45, 45]),
        (time(15, 0), {}, [45, 45, 20, 20, 45, 45]),
        (time(15, 0), {"ev": True}, [45, 45, 30, 20, 45, 45]),
        (time(23, 30), {"profile": "sunny"}, [20, 20, 20, 20, 20, 20]),
        (time(1, 0), {"profile": "sunny", "ev": True, "hold": 55}, [55, 20, 20, 20, 20, 20]),
        (time(2, 0), {"gate": True, "ev": True}, [30, 45, 20, 20, 45, 45]),
    ],
)
def test_desired_caps_table(now, kwargs, expected):
    assert caps(now, **kwargs) == expected


def test_unknown_inputs_give_no_state():
    assert (
        desired_caps(SLOTS, time(1), None, "cloudy", 45, 20, None, False, False, False, DEADLINE)
        is None
    )
    assert (
        desired_caps(SLOTS, time(1), 30, None, 45, 20, None, False, False, False, DEADLINE) is None
    )
    assert (
        desired_caps(SLOTS, time(1), 30, "cloudy", None, 20, None, False, False, False, DEADLINE)
        is None
    )


def test_gate_requires_cap_above_soc():
    # SOC already above the cap: nothing to defer
    assert caps(time(22, 0), gate=True, soc=50, hold=50) == [45, 45, 20, 20, 45, 45]


def test_spike_beats_gate_and_ev():
    assert caps(time(22, 0), spike=True, gate=True, ev=True)[4] == 20


def test_gate_window_spans_midnight():
    def win(h, m):
        now = time(h, m)
        return in_charge_window(SLOTS, current_slot(SLOTS, now), now, DEADLINE)

    assert win(21, 0)
    assert win(23, 30)
    assert win(0, 15)
    assert not win(4, 30)
    assert not win(12, 0)  # a spend slot, never gated


def test_reasons_present():
    res = desired_caps(SLOTS, time(15, 0), 30, "cloudy", 45, 20, 30, False, False, True, DEADLINE)
    assert res.current_slot == 3
    assert res.reasons[2] == "EV hold at 30%"
    assert len(res.reasons) == 6


def test_spend_need_default_window():
    profile = [1.0] * 24
    assert spend_need_kwh(SLOTS, profile) == pytest.approx(8.0)  # 13:00-21:00
    assert SLOTS[2].role is Role.SPEND


@pytest.mark.parametrize(
    ("pv", "reserve", "target", "profile"),
    [
        (12, 24.0, 45, "cloudy"),
        (16, 22.0, 43, "cloudy"),
        (40, 10.0, 31, "cloudy"),
        (86, 5.0, 26, "sunny"),
        (None, 30.0, 52, "cloudy"),
    ],
)
def test_plan_math(pv, reserve, target, profile):
    # 8 hours of 25/8 kWh each -> spend need 25
    load = [25 / 8] * 24
    res = plan(pv, SLOTS, load, 96.5, 20, 5.0, 50.0, 0.5)
    assert res.spend_need_kwh == pytest.approx(25.0)
    assert res.reserve_kwh == pytest.approx(reserve)
    assert res.target_soc == target
    assert res.profile == profile


def test_daytime_charge_layout():
    roles = ["hold", "hold", "hold", "charge", "spend", "hold"]
    starts = ["00:00", "08:00", "11:00", "13:00", "15:30", "21:00"]
    slots = [
        Slot(i + 1, time(int(s[:2]), int(s[3:])), Role(r))
        for i, (s, r) in enumerate(zip(starts, roles, strict=True))
    ]

    def run(now, **kw):
        args = dict(spike=False, gate_closed=False, ev_charging=False, deadline=DEADLINE)
        args.update(kw)
        return desired_caps(
            slots,
            now,
            30,
            "cloudy",
            60,
            40,
            40,
            args["spike"],
            args["gate_closed"],
            args["ev_charging"],
            args["deadline"],
        ).caps

    assert run(time(14, 0)) == [60, 60, 60, 60, 40, 60]
    assert run(time(14, 0), spike=True) == [60, 60, 60, 40, 40, 60]
    # the gate window opens at the charge slot start and runs to the deadline
    assert run(time(14, 0), gate_closed=True) == [60, 60, 60, 40, 40, 60]
    # deadline 13:30: past it, charging is no longer deferred
    assert run(time(14, 0), gate_closed=True, deadline=time(13, 30)) == [60, 60, 60, 60, 40, 60]
    # hold slots are never gated
    assert run(time(9, 0), gate_closed=True) == [60, 60, 60, 60, 40, 60]
