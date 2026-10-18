"""Pure planning logic: daily target SOC and per-slot desired caps.

No Home Assistant imports so the module can be unit tested in isolation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import time
from enum import StrEnum

MINUTES_PER_DAY = 24 * 60


class Role(StrEnum):
    """What a TOU slot is used for."""

    CHARGE = "charge"  # raise to the target; grid charges if the slot has Charge on
    HOLD = "hold"  # keep the battery at the target (idle) on cloudy days
    SPEND = "spend"  # let the battery serve the house down to the floor

    @classmethod
    def _missing_(cls, value: object) -> Role | None:
        """Accept the pre-release time-of-day role names."""
        legacy = {
            "overnight": cls.CHARGE,
            "morning": cls.HOLD,
            "peak": cls.SPEND,
            "evening": cls.SPEND,
        }
        return legacy.get(str(value))


SPEND_ROLES = (Role.SPEND,)
TARGET_ROLES = (Role.CHARGE, Role.HOLD)
GRID_CHARGE_ROLES = (Role.CHARGE,)


@dataclass(frozen=True)
class Slot:
    """One inverter TOU slot (index is 1-based)."""

    index: int
    start: time
    role: Role


DEFAULT_LAYOUT: tuple[tuple[str, Role], ...] = (
    ("00:00", Role.CHARGE),
    ("06:00", Role.HOLD),
    ("13:00", Role.SPEND),
    ("19:00", Role.SPEND),
    ("21:00", Role.CHARGE),
    ("23:00", Role.CHARGE),
)


def parse_hhmm(value: str) -> time:
    """Parse "HH:MM" or "HH:MM:SS"."""
    parts = [int(p) for p in value.split(":")]
    return time(parts[0], parts[1])


def default_slots(count: int = 6) -> list[Slot]:
    """Return the default layout, truncated to ``count`` slots."""
    return [
        Slot(i + 1, parse_hhmm(start), role)
        for i, (start, role) in enumerate(DEFAULT_LAYOUT[:count])
    ]


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def slot_intervals(slots: list[Slot]) -> list[tuple[Slot, int, int]]:
    """Return (slot, start_min, end_min) sorted by start; end may exceed 1440."""
    ordered = sorted(slots, key=lambda s: _minutes(s.start))
    out: list[tuple[Slot, int, int]] = []
    for i, slot in enumerate(ordered):
        start = _minutes(slot.start)
        if i + 1 < len(ordered):
            end = _minutes(ordered[i + 1].start)
        else:
            end = _minutes(ordered[0].start) + MINUTES_PER_DAY
        out.append((slot, start, end))
    return out


def current_slot(slots: list[Slot], now: time) -> Slot:
    """Return the slot that is active at ``now`` (wraps past midnight)."""
    minute = _minutes(now)
    for slot, start, end in slot_intervals(slots):
        if start <= minute < end or start <= minute + MINUTES_PER_DAY < end:
            return slot
    return slot_intervals(slots)[-1][0]


def next_boundary(slots: list[Slot], now: time) -> time:
    """Return the start time of the slot following the current one."""
    cur = current_slot(slots, now)
    for slot, _start, end in slot_intervals(slots):
        if slot.index == cur.index:
            return time((end % MINUTES_PER_DAY) // 60, end % 60)
    return cur.start


def spend_need_kwh(slots: list[Slot], load_profile: list[float]) -> float:
    """Sum the hourly load over the hours covered by spend slots."""
    total = 0.0
    for slot, start, end in slot_intervals(slots):
        if slot.role not in SPEND_ROLES:
            continue
        for h in range(48):
            lo = max(start, h * 60)
            hi = min(end, (h + 1) * 60)
            if hi > lo:
                total += load_profile[h % 24] * (hi - lo) / 60
    return total


@dataclass(frozen=True)
class PlanResult:
    """Outcome of the daily plan with every intermediate exposed."""

    pv_forecast_kwh: float | None
    spend_need_kwh: float
    pv_after_kwh: float
    reserve_kwh: float
    target_soc: int
    profile: str  # "sunny" | "cloudy"

    def as_dict(self) -> dict:
        return {
            "pv_forecast_kwh": self.pv_forecast_kwh,
            "spend_need_kwh": round(self.spend_need_kwh, 2),
            "pv_after_kwh": round(self.pv_after_kwh, 2),
            "reserve_kwh": round(self.reserve_kwh, 2),
            "target_soc": self.target_soc,
            "profile": self.profile,
        }


def plan(
    pv_forecast_kwh: float | None,
    slots: list[Slot],
    load_profile: list[float],
    capacity_kwh: float,
    floor_soc: int,
    buffer_kwh: float,
    cloudy_threshold_kwh: float,
    spend_pv_share: float,
) -> PlanResult:
    """Compute target SOC and profile for the day being planned."""
    need = spend_need_kwh(slots, load_profile)
    pv_after = spend_pv_share * pv_forecast_kwh if pv_forecast_kwh is not None else 0.0
    reserve = max(0.0, need - pv_after) + buffer_kwh
    pct = math.ceil(round(reserve / capacity_kwh * 100, 6))
    target = int(min(100, floor_soc + pct))
    cloudy = pv_forecast_kwh is None or pv_forecast_kwh < cloudy_threshold_kwh
    return PlanResult(
        pv_forecast_kwh=pv_forecast_kwh,
        spend_need_kwh=need,
        pv_after_kwh=pv_after,
        reserve_kwh=reserve,
        target_soc=target,
        profile="cloudy" if cloudy else "sunny",
    )


def hold_value(soc: float | None, floor_soc: int) -> int | None:
    """SOC to hold at: max(floor, floor_int(SOC))."""
    if soc is None:
        return None
    return max(floor_soc, int(math.floor(soc)))


def in_charge_window(slots: list[Slot], current: Slot, now: time, deadline: time) -> bool:
    """True while the price gate may defer charging in ``current`` (a charge slot).

    The window opens at the start of the run of consecutive charge slots that
    contains the current slot and closes at the deadline (cyclic, so it can span
    midnight). A deadline at the window start means the gate never defers.
    """
    if current.role is not Role.CHARGE:
        return False
    ordered = [s for s, _, _ in slot_intervals(slots)]
    pos = next(i for i, s in enumerate(ordered) if s.index == current.index)
    first = pos
    for step in range(1, len(ordered)):
        prev = (pos - step) % len(ordered)
        if ordered[prev].role is not Role.CHARGE:
            break
        first = prev
    start = _minutes(ordered[first].start)
    window = (_minutes(deadline) - start) % MINUTES_PER_DAY
    return (_minutes(now) - start) % MINUTES_PER_DAY < window


@dataclass(frozen=True)
class DesiredCaps:
    """Per-slot desired caps (slot index order) with reasons."""

    caps: list[int]
    reasons: list[str]
    current_slot: int
    overrides: list[str] = field(default_factory=list)


def _fmt(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def desired_caps(
    slots: list[Slot],
    now: time,
    soc: float | None,
    profile: str | None,
    target_soc: int | None,
    floor_soc: int,
    hold_soc: int | None,
    spike: bool,
    gate_closed: bool,
    ev_charging: bool,
    deadline: time,
) -> DesiredCaps | None:
    """Compute the desired cap of every slot, or None if inputs are unknown."""
    if soc is None or profile is None or target_soc is None or not slots:
        return None
    floor_soc = int(floor_soc)
    target = max(floor_soc, min(100, int(target_soc)))
    cur = current_slot(slots, now)
    intervals = {s.index: (st, en) for s, st, en in slot_intervals(slots)}
    base_by_index: dict[int, int] = {}
    reasons_by_index: dict[int, str] = {}
    for slot in slots:
        end = intervals[slot.index][1] % MINUTES_PER_DAY
        end_txt = f"{end // 60:02d}:{end % 60:02d}"
        if profile == "cloudy" and slot.role in TARGET_ROLES:
            base_by_index[slot.index] = target
            reasons_by_index[slot.index] = f"cloudy: hold {target}% until {end_txt}"
        elif profile == "cloudy":
            base_by_index[slot.index] = floor_soc
            reasons_by_index[slot.index] = f"cloudy: spend to {floor_soc}%"
        else:
            base_by_index[slot.index] = floor_soc
            reasons_by_index[slot.index] = f"sunny: floor {floor_soc}%"

    hold = hold_soc if hold_soc is not None else hold_value(soc, floor_soc)
    hold = max(floor_soc, hold)
    cap = base_by_index[cur.index]
    reason = reasons_by_index[cur.index]
    overrides: list[str] = []

    if spike:
        cap = floor_soc
        reason = "spike release"
        overrides.append("spike")
    else:
        if (
            profile == "cloudy"
            and cur.role is Role.CHARGE
            and cap > soc
            and gate_closed
            and in_charge_window(slots, cur, now, deadline)
        ):
            cap = min(cap, hold)
            reason = f"price gate: defer charge, hold {cap}% until {_fmt(deadline)}"
            overrides.append("price_gate")
        if ev_charging:
            new_cap = max(cap, hold)
            reason = f"EV hold at {hold}%" if new_cap != cap or not overrides else reason
            cap = new_cap
            overrides.append("ev")

    caps = [base_by_index[s.index] for s in slots]
    reasons = [reasons_by_index[s.index] for s in slots]
    pos = next(i for i, s in enumerate(slots) if s.index == cur.index)
    caps[pos] = int(min(100, max(floor_soc, cap)))
    reasons[pos] = reason
    return DesiredCaps(caps=caps, reasons=reasons, current_slot=cur.index, overrides=overrides)
