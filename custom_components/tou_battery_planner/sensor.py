"""Sensors exposing the plan, state and reasoning."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .controller import TouController
from .entity import TouEntity
from .planner import current_slot, next_boundary


@dataclass(frozen=True)
class _Desc:
    key: str
    name: str
    value: Callable[[TouController], Any]
    attrs: Callable[[TouController], dict[str, Any]] | None = None
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    icon: str | None = None


def _plan(c: TouController, field: str) -> Any:
    return getattr(c.plan_result, field) if c.plan_result else None


def _round(v: float | None, n: int = 1) -> float | None:
    return None if v is None else round(v, n)


def _desired_attrs(c: TouController) -> dict[str, Any]:
    if not c.desired:
        return {"status": c.status}
    out: dict[str, Any] = {"status": c.status, "current_slot": c.desired.current_slot}
    for i, (cap, why) in enumerate(zip(c.desired.caps, c.desired.reasons, strict=True), start=1):
        out[f"slot_{i}_cap"] = cap
        out[f"slot_{i}_reason"] = why
    out["overrides"] = c.desired.overrides
    out["hold_soc"] = c.hold_soc
    return out


def _slot_attrs(c: TouController) -> dict[str, Any]:
    slots = c.slots()
    now = dt_util.now().time()
    cur = current_slot(slots, now)
    return {
        "role": cur.role.value,
        "start": cur.start.strftime("%H:%M"),
        "next_boundary": next_boundary(slots, now).strftime("%H:%M"),
    }


def _load_attrs(c: TouController) -> dict[str, Any]:
    out: dict[str, Any] = {f"h{h:02d}": round(v, 3) for h, v in enumerate(c.load_profile.hourly)}
    out["provisional"] = c.load_profile.provisional
    out["days_of_data"] = c.load_profile.days_of_data
    out["weekend_split"] = c.load_profile.weekend is not None
    return out


DESCRIPTIONS: tuple[_Desc, ...] = (
    _Desc(
        "profile",
        "Profile",
        lambda c: c.effective_profile,
        lambda c: {
            "mode": c.st.profile_mode,
            "planned_profile": _plan(c, "profile"),
            "plan_for": c.plan_for.isoformat() if c.plan_for else None,
            "note": c.plan_note,
        },
        icon="mdi:weather-partly-cloudy",
    ),
    _Desc(
        "target_soc",
        "Target SOC",
        lambda c: _plan(c, "target_soc"),
        lambda c: c.plan_result.as_dict() if c.plan_result else {},
        unit=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _Desc(
        "forecast_pv",
        "Forecast PV",
        lambda c: _round(c.pv_tomorrow),
        lambda c: {
            "tomorrow_kwh": _round(c.pv_tomorrow),
            "today_kwh": _round(c.pv_today),
            "k_kwh_per_mj": round(c.st.k, 3),
        },
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
    ),
    _Desc(
        "evening_need",
        "Evening need",
        lambda c: _round(_plan(c, "evening_need_kwh")),
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
    ),
    _Desc(
        "reserve",
        "Reserve",
        lambda c: _round(_plan(c, "reserve_kwh")),
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
    ),
    _Desc(
        "pv_factor_k",
        "PV factor k",
        lambda c: round(c.st.k, 3),
        lambda c: {"history": c.st.k_history[-14:]},
        unit="kWh/(MJ/m²)",
        icon="mdi:solar-power",
    ),
    _Desc(
        "load_profile",
        "Load profile",
        lambda c: round(c.load_profile.daily_kwh, 1),
        _load_attrs,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
    ),
    _Desc(
        "desired_caps",
        "Desired caps",
        lambda c: ",".join(map(str, c.desired.caps)) if c.desired else None,
        _desired_attrs,
        icon="mdi:battery-clock",
    ),
    _Desc(
        "current_slot",
        "Current slot",
        lambda c: c.desired.current_slot if c.desired else None,
        _slot_attrs,
    ),
    _Desc(
        "last_action",
        "Last action",
        lambda c: c.last_action[0] if c.last_action else None,
        lambda c: {"summary": c.last_action[1] if c.last_action else None, "status": c.status},
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    _Desc(
        "writes_last_hour",
        "Writes in last hour",
        lambda c: c.writes_last_hour,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _Desc(
        "forecast_mae",
        "Forecast MAE",
        lambda c: _round(c.forecast_mae, 2),
        lambda c: {"days": min(len(c.st.pairs), 14)},
        unit=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    _Desc(
        "pending_actions",
        "Pending actions",
        lambda c: len(c.pending_actions),
        lambda c: {"actions": c.pending_actions, "status": c.status},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback
) -> None:
    controller: TouController = entry.runtime_data
    async_add_entities(TouSensor(controller, d) for d in DESCRIPTIONS)


class TouSensor(TouEntity, SensorEntity):
    def __init__(self, controller: TouController, desc: _Desc) -> None:
        super().__init__(controller, desc.key, desc.name)
        self._desc = desc
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_device_class = desc.device_class
        self._attr_state_class = desc.state_class
        self._attr_icon = desc.icon

    @property
    def native_value(self) -> Any:
        return self._desc.value(self.controller)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self._desc.attrs(self.controller) if self._desc.attrs else None
