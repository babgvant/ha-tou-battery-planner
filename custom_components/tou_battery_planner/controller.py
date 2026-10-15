"""Controller: state tracking, debouncers, planning, reconciler and safety guards."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import forecast as fc
from . import learning
from .const import (
    CONF_AFTERNOON_SHARE,
    CONF_BUFFER_KWH,
    CONF_CAPACITY,
    CONF_CHARGE_FLAGS,
    CONF_CLOUDY_THRESHOLD,
    CONF_DEADLINE,
    CONF_EV_SENSORS,
    CONF_FLAT_DAILY_KWH,
    CONF_FLOOR_SOC,
    CONF_FORECAST_ATTR,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_SOURCE,
    CONF_FORECAST_TODAY_ATTR,
    CONF_FORECAST_TODAY_ENTITY,
    CONF_GATE_PRICE,
    CONF_LAT,
    CONF_LEARNING_DAYS,
    CONF_LOAD_SENSOR,
    CONF_LON,
    CONF_PAUSE_MINUTES,
    CONF_PLAN_EVENING,
    CONF_PLAN_MORNING,
    CONF_PRICE_SENSOR,
    CONF_PRICE_UNIT,
    CONF_PV_SENSOR,
    CONF_SLOT_COUNT,
    CONF_SLOT_ROLES,
    CONF_SLOT_STARTS,
    CONF_SOC_ENTITIES,
    CONF_SOC_SENSOR,
    CONF_SPIKE_PRICE,
    CONF_TIME_ENTITIES,
    CONF_TOU_SWITCH,
    CONF_WRITE_BUDGET,
    DEBOUNCE_EV,
    DEBOUNCE_GATE,
    DEBOUNCE_SPIKE,
    DOMAIN,
    EV_POWER_THRESHOLD_W,
    FORECAST_SENSOR,
    ISSUE_BUDGET,
    ISSUE_NO_CHARGE,
    ISSUE_SLOT_UNAVAILABLE,
    ISSUE_WRITE_REVERTED,
    RECONCILE_INTERVAL_MINUTES,
    REVERT_CHECK_MINUTES,
    SIGNAL_UPDATE,
    STARTUP_GRACE_SECONDS,
    STORAGE_VERSION,
    TICK_SECONDS,
    TUNABLE_DEFAULTS,
    UNAVAILABLE_ISSUE_MINUTES,
    WRITE_SPACING_SECONDS,
)
from .planner import (
    DesiredCaps,
    PlanResult,
    Role,
    Slot,
    current_slot,
    default_slots,
    desired_caps,
    hold_value,
    parse_hhmm,
    plan,
)

_LOGGER = logging.getLogger(__name__)

BAD_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN, None, "")


class Debouncer:
    """Boolean debouncer with separate on/off delays (explicit clock for testing)."""

    def __init__(self, on_delay: timedelta, off_delay: timedelta, initial: bool = False) -> None:
        self.on_delay = on_delay
        self.off_delay = off_delay
        self.state = initial
        self._since: datetime | None = None

    def reset(self, value: bool) -> None:
        self.state = value
        self._since = None

    def update(self, raw: bool, now: datetime) -> bool:
        if raw == self.state:
            self._since = None
            return self.state
        if self._since is None:
            self._since = now
        delay = self.on_delay if raw else self.off_delay
        if now - self._since >= delay:
            self.state = raw
            self._since = None
        return self.state


@dataclass
class _Pending:
    value: int
    at: datetime
    retries: int = 0


@dataclass
class _Action:
    slot: int
    entity_id: str
    current: int | None
    target: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "entity_id": self.entity_id,
            "from": self.current,
            "to": self.target,
            "reason": self.reason,
        }


@dataclass
class ControllerState:
    """Mutable values persisted in the Store."""

    enabled: bool = False
    dry_run: bool = True
    profile_mode: str = "auto"
    k: float = learning.K_DEFAULT
    k_history: list[tuple[str, float]] = field(default_factory=list)
    forecasts: dict[str, float] = field(default_factory=dict)  # date -> forecast kWh
    pairs: list[dict[str, Any]] = field(default_factory=list)  # {date, forecast, actual}


class TouController:
    """Owns all planner runtime state for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.st = ControllerState()
        self.write_spacing: float = WRITE_SPACING_SECONDS
        self.startup_grace = timedelta(seconds=STARTUP_GRACE_SECONDS)

        self.started_at: datetime | None = None
        self.plan_result: PlanResult | None = None
        self.plan_for: date | None = None
        self.plan_note: str = ""
        self.pv_tomorrow: float | None = None
        self.pv_today: float | None = None
        self.shortwave: dict[date, float] = {}
        self.load_profile: learning.LoadProfile = learning.build_load_profile(
            [], self._opt(CONF_FLAT_DAILY_KWH)
        )
        self.hold_soc: int | None = None
        self.deb_spike = Debouncer(
            timedelta(minutes=DEBOUNCE_SPIKE[0]), timedelta(minutes=DEBOUNCE_SPIKE[1])
        )
        self.deb_gate = Debouncer(
            timedelta(minutes=DEBOUNCE_GATE[0]), timedelta(minutes=DEBOUNCE_GATE[1])
        )
        self.deb_ev = Debouncer(
            timedelta(minutes=DEBOUNCE_EV[0]), timedelta(minutes=DEBOUNCE_EV[1])
        )
        self._primed = False
        self._last_slot_index: int | None = None

        self.desired: DesiredCaps | None = None
        self.status: str = "starting"
        self.controls_available: bool = False
        self.pending_actions: list[dict[str, Any]] = []
        self.last_action: tuple[datetime, str] | None = None
        self.write_log: deque[dict[str, Any]] = deque(maxlen=50)
        self._write_times: list[datetime] = []
        self._pending: dict[int, _Pending] = {}
        self._last_written: dict[int, int] = {}
        self._paused_until: dict[int, datetime] = {}
        self._blocked: set[int] = set()
        self._unavailable_since: datetime | None = None
        self._lock = asyncio.Lock()
        self._unsubs: list[CALLBACK_TYPE] = []
        self._plan_unsubs: list[CALLBACK_TYPE] = []
        self._startup_unsub: CALLBACK_TYPE | None = None
        self.skip_next_reload = False

    # ------------------------------------------------------------------ config
    @property
    def cfg(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    def _opt(self, key: str) -> Any:
        val = self.cfg.get(key)
        return val if val is not None else TUNABLE_DEFAULTS.get(key)

    @property
    def slot_count(self) -> int:
        return int(self.cfg.get(CONF_SLOT_COUNT, 6))

    @property
    def soc_entities(self) -> list[str]:
        return list(self.cfg.get(CONF_SOC_ENTITIES, []))[: self.slot_count]

    @property
    def time_entities(self) -> list[str]:
        return list(self.cfg.get(CONF_TIME_ENTITIES, []))

    @property
    def floor(self) -> int:
        return int(self._opt(CONF_FLOOR_SOC))

    @property
    def deadline(self) -> time:
        return parse_hhmm(str(self._opt(CONF_DEADLINE)))

    def configured_slots(self) -> list[Slot]:
        """Slots from config (roles and start times)."""
        roles = self.cfg.get(CONF_SLOT_ROLES)
        starts = self.cfg.get(CONF_SLOT_STARTS)
        if not roles or not starts:
            return default_slots(self.slot_count)
        return [Slot(i + 1, parse_hhmm(starts[i]), Role(roles[i])) for i in range(self.slot_count)]

    def slots(self) -> list[Slot]:
        """Configured slots with live start times from the time entities when valid."""
        out = []
        times = self.time_entities
        for slot in self.configured_slots():
            start = slot.start
            if slot.index <= len(times) and times[slot.index - 1]:
                state = self.hass.states.get(times[slot.index - 1])
                if state is not None and state.state not in BAD_STATES:
                    try:
                        start = parse_hhmm(state.state)
                    except (ValueError, IndexError):
                        pass
            out.append(Slot(slot.index, start, slot.role))
        return out

    # ----------------------------------------------------------------- helpers
    def _state(self, entity_id: str | None) -> State | None:
        return self.hass.states.get(entity_id) if entity_id else None

    def _float(self, entity_id: str | None) -> float | None:
        st = self._state(entity_id)
        if st is None or st.state in BAD_STATES:
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None

    def _power_factor_kw(self, entity_id: str) -> float:
        """Multiply a power reading by this to get kW."""
        st = self._state(entity_id)
        unit = str(st.attributes.get("unit_of_measurement", "W")) if st else "W"
        return {"kW": 1.0, "MW": 1000.0}.get(unit, 0.001)

    @property
    def soc(self) -> float | None:
        return self._float(self.cfg.get(CONF_SOC_SENSOR))

    @property
    def price_cents(self) -> float | None:
        entity = self.cfg.get(CONF_PRICE_SENSOR)
        value = self._float(entity)
        if value is None:
            return None
        pref = self.cfg.get(CONF_PRICE_UNIT, "auto")
        if pref == "auto":
            st = self._state(entity)
            unit = str(st.attributes.get("unit_of_measurement", "")).lower() if st else ""
            dollars = ("$" in unit or "usd" in unit) and "¢" not in unit and "cent" not in unit
        else:
            dollars = pref == "dollars"
        return value * 100 if dollars else value

    @property
    def ev_power_w(self) -> float | None:
        """Total EV power in W, None if there are sensors but none is readable."""
        sensors = list(self.cfg.get(CONF_EV_SENSORS) or [])
        if not sensors:
            return 0.0
        vals = [(self._float(s), s) for s in sensors]
        known = [v * self._power_factor_kw(s) * 1000 for v, s in vals if v is not None]
        return sum(known) if known else None

    @property
    def effective_profile(self) -> str | None:
        if self.st.profile_mode in ("sunny", "cloudy"):
            return self.st.profile_mode
        return self.plan_result.profile if self.plan_result else None

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    # ------------------------------------------------------------- persistence
    async def _async_load(self) -> None:
        data = await self._store.async_load() or {}
        s = self.st
        s.enabled = bool(data.get("enabled", False))
        s.dry_run = bool(data.get("dry_run", True))
        s.profile_mode = data.get("profile_mode", "auto")
        s.k = float(data.get("k", learning.K_DEFAULT))
        s.k_history = [tuple(x) for x in data.get("k_history", [])]
        s.forecasts = dict(data.get("forecasts", {}))
        s.pairs = list(data.get("pairs", []))

    def _save(self) -> None:
        s = self.st
        self._store.async_delay_save(
            lambda: {
                "enabled": s.enabled,
                "dry_run": s.dry_run,
                "profile_mode": s.profile_mode,
                "k": s.k,
                "k_history": s.k_history[-60:],
                "forecasts": dict(list(s.forecasts.items())[-30:]),
                "pairs": s.pairs[-60:],
            },
            1,
        )

    # --------------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        await self._async_load()
        self.started_at = dt_util.utcnow()
        cfg = self.cfg
        watch = [
            cfg.get(CONF_SOC_SENSOR),
            cfg.get(CONF_PRICE_SENSOR),
            *(cfg.get(CONF_EV_SENSORS) or []),
            *self.soc_entities,
        ]
        watch = [w for w in watch if w]
        self._unsubs.append(
            async_track_state_change_event(self.hass, watch, self._handle_state_event)
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=TICK_SECONDS))
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_periodic_reconcile,
                timedelta(minutes=RECONCILE_INTERVAL_MINUTES),
            )
        )
        self._schedule_plans()
        self._check_charge_flags()
        self._startup_unsub = async_call_later(
            self.hass, self.startup_grace.total_seconds(), self._async_startup
        )

    async def async_stop(self) -> None:
        for unsub in (*self._unsubs, *self._plan_unsubs):
            unsub()
        self._unsubs.clear()
        self._plan_unsubs.clear()
        if self._startup_unsub:
            self._startup_unsub()
            self._startup_unsub = None
        await self._store.async_save(
            {
                "enabled": self.st.enabled,
                "dry_run": self.st.dry_run,
                "profile_mode": self.st.profile_mode,
                "k": self.st.k,
                "k_history": self.st.k_history[-60:],
                "forecasts": self.st.forecasts,
                "pairs": self.st.pairs[-60:],
            }
        )

    def _schedule_plans(self) -> None:
        for unsub in self._plan_unsubs:
            unsub()
        self._plan_unsubs.clear()
        for key, evening in ((CONF_PLAN_EVENING, True), (CONF_PLAN_MORNING, False)):
            t = parse_hhmm(str(self._opt(key)))

            async def _run(_now: datetime, evening: bool = evening) -> None:
                if evening:
                    await self.async_learn()
                await self.async_plan("scheduled", for_tomorrow=evening)
                await self.async_reconcile("plan")

            self._plan_unsubs.append(
                async_track_time_change(self.hass, _run, hour=t.hour, minute=t.minute, second=0)
            )

    async def _async_startup(self, _now: datetime | None = None) -> None:
        self._startup_unsub = None
        await self.async_learn()
        await self.async_plan("startup")
        await self.async_reconcile("startup")

    def apply_options(self) -> None:
        """Re-read options after an in-place change."""
        self._schedule_plans()
        self._notify()

    def set_option(self, key: str, value: Any) -> None:
        """Persist a tunable into the entry options without reloading."""
        self.skip_next_reload = True
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, key: value}
        )
        if key in (CONF_PLAN_EVENING, CONF_PLAN_MORNING):
            self._schedule_plans()
        self._notify()
        self.hass.async_create_task(self.async_reconcile("option"))

    def set_enabled(self, value: bool) -> None:
        self.st.enabled = value
        self._save()
        self._notify()
        if value:
            self.hass.async_create_task(self.async_reconcile("enabled"))

    def set_dry_run(self, value: bool) -> None:
        self.st.dry_run = value
        self._save()
        self._notify()
        if not value:
            self.hass.async_create_task(self.async_reconcile("dry_run_off"))

    def set_profile_mode(self, value: str) -> None:
        self.st.profile_mode = value
        self._save()
        self._notify()
        self.hass.async_create_task(self.async_reconcile("profile_mode"))

    def _check_charge_flags(self) -> None:
        roles = self.configured_slots()
        flags = list(self.cfg.get(CONF_CHARGE_FLAGS) or [])
        overnight = [s.index for s in roles if s.role is Role.OVERNIGHT]
        has_charge = any(i <= len(flags) and flags[i - 1] for i in overnight)
        if overnight and not has_charge:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_NO_CHARGE}_{self.entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_NO_CHARGE,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_NO_CHARGE}_{self.entry.entry_id}")

    # ------------------------------------------------------------ state events
    @callback
    def _handle_state_event(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        new: State | None = event.data.get("new_state")
        old: State | None = event.data.get("old_state")
        if entity_id in self.soc_entities and new is not None:
            self._handle_cap_change(self.soc_entities.index(entity_id) + 1, old, new)
        self.hass.async_create_task(self.async_reconcile("state"))

    def _handle_cap_change(self, slot: int, old: State | None, new: State) -> None:
        if old is None or old.state in BAD_STATES or new.state in BAD_STATES:
            return
        if old.state == new.state:
            return
        try:
            value = int(float(new.state))
        except ValueError:
            return
        if slot in self._pending:
            return  # confirmation / revert handled by the verifier
        if self._last_written.get(slot) == value:
            return
        if self.desired and self.desired.caps[slot - 1] == value:
            return
        minutes = int(self._opt(CONF_PAUSE_MINUTES))
        until = dt_util.utcnow() + timedelta(minutes=minutes)
        self._paused_until[slot] = until
        _LOGGER.info(
            "Slot %s changed to %s outside the planner; pausing writes for %s min",
            slot,
            value,
            minutes,
        )
        self._record(slot, old.state, value, "manual change detected, slot paused", "paused")

    # ------------------------------------------------------------ debouncing
    def _raw_inputs(self) -> tuple[bool, bool, bool]:
        price = self.price_cents
        spike = price is not None and price >= float(self._opt(CONF_SPIKE_PRICE))
        gate_closed = price is None or price > float(self._opt(CONF_GATE_PRICE))
        ev_w = self.ev_power_w
        ev = ev_w is not None and ev_w > EV_POWER_THRESHOLD_W
        return spike, gate_closed, ev

    def _update_debouncers(self, now: datetime) -> None:
        spike, gate_closed, ev = self._raw_inputs()
        if not self._primed:
            self.deb_spike.reset(spike)
            self.deb_gate.reset(gate_closed)
            self.deb_ev.reset(ev)
            self._primed = True
            self._record_hold()
            return
        prev_gate, prev_ev = self.deb_gate.state, self.deb_ev.state
        self.deb_spike.update(spike, now)
        self.deb_gate.update(gate_closed, now)
        self.deb_ev.update(ev, now)
        if (self.deb_ev.state and not prev_ev) or (self.deb_gate.state and not prev_gate):
            self._record_hold()
        slots = self.slots()
        if slots:
            idx = current_slot(slots, dt_util.as_local(now).time()).index
            if idx != self._last_slot_index:
                if self._last_slot_index is not None:
                    self._record_hold()
                self._last_slot_index = idx

    def _record_hold(self) -> None:
        self.hold_soc = hold_value(self.soc, self.floor)

    @property
    def spike_active(self) -> bool:
        return self.deb_spike.state

    @property
    def gate_closed(self) -> bool:
        return self.deb_gate.state

    @property
    def ev_charging(self) -> bool:
        return self.deb_ev.state

    # ----------------------------------------------------------- forecasting
    async def _async_refresh_shortwave(self) -> None:
        cfg = self.cfg
        lat = cfg.get(CONF_LAT, self.hass.config.latitude)
        lon = cfg.get(CONF_LON, self.hass.config.longitude)
        res = await fc.async_fetch_open_meteo(
            self.hass, lat, lon, str(self.hass.config.time_zone), learning.K_WINDOW_DAYS + 2
        )
        if res is not None and res.by_day:
            self.shortwave = res.by_day

    async def _async_refresh_forecast(self) -> None:
        cfg = self.cfg
        today = dt_util.now().date()
        if cfg.get(CONF_FORECAST_SOURCE) == FORECAST_SENSOR:
            self.pv_tomorrow = fc.read_sensor_kwh(
                self._state(cfg.get(CONF_FORECAST_ENTITY)), cfg.get(CONF_FORECAST_ATTR)
            )
            today_entity = cfg.get(CONF_FORECAST_TODAY_ENTITY)
            self.pv_today = (
                fc.read_sensor_kwh(self._state(today_entity), cfg.get(CONF_FORECAST_TODAY_ATTR))
                if today_entity
                else None
            )
            return
        await self._async_refresh_shortwave()
        k = self.st.k
        self.pv_today = fc.pv_kwh_from_shortwave(self.shortwave.get(today), k)
        self.pv_tomorrow = fc.pv_kwh_from_shortwave(
            self.shortwave.get(today + timedelta(days=1)), k
        )

    async def async_learn(self) -> None:
        """Recompute the load profile and PV calibration factor from recorder data."""
        cfg = self.cfg
        days = int(self._opt(CONF_LEARNING_DAYS))
        end = dt_util.utcnow()
        start = end - timedelta(days=max(days, learning.K_WINDOW_DAYS + 2))
        load = cfg.get(CONF_LOAD_SENSOR)
        pv = cfg.get(CONF_PV_SENSOR)
        evs = list(cfg.get(CONF_EV_SENSORS) or [])
        ids = [i for i in [load, pv, *evs] if i]
        try:
            stats = await learning.async_hourly_means(self.hass, ids, start, end)
        except Exception:  # noqa: BLE001 - recorder trouble must never break planning
            _LOGGER.exception("Could not read recorder statistics")
            return
        if load and stats.get(load):
            combined = learning.combine_load(
                stats[load],
                [stats.get(e, []) for e in evs],
                self._power_factor_kw(load),
                [self._power_factor_kw(e) for e in evs],
            )
            cutoff = dt_util.now() - timedelta(days=days)
            combined = [(t, v) for t, v in combined if t >= cutoff]
            self.load_profile = learning.build_load_profile(
                combined, float(self._opt(CONF_FLAT_DAILY_KWH))
            )
        if pv and stats.get(pv):
            factor = self._power_factor_kw(pv)
            pv_kwh = learning.daily_totals(
                [(t, v * factor) for t, v in stats[pv]],
                exclude_day=dt_util.now().date(),
            )
            if cfg.get(CONF_FORECAST_SOURCE) != FORECAST_SENSOR:
                await self._async_refresh_shortwave()
                k, used = learning.calibration_factor(pv_kwh, self.shortwave)
                self.st.k = k
                self.st.k_history = [(d.isoformat(), round(r, 3)) for d, r in used]
            self._update_pairs(pv_kwh)
        self._save()
        self._notify()

    def _update_pairs(self, actual_by_day: dict[date, float]) -> None:
        have = {p["date"] for p in self.st.pairs}
        for day_s, forecast in self.st.forecasts.items():
            if day_s in have:
                continue
            actual = actual_by_day.get(date.fromisoformat(day_s))
            if actual is not None:
                self.st.pairs.append({"date": day_s, "forecast": forecast, "actual": actual})
        self.st.pairs = self.st.pairs[-60:]

    @property
    def forecast_mae(self) -> float | None:
        recent = self.st.pairs[-learning.K_WINDOW_DAYS :]
        return learning.mean_absolute_error([(p["forecast"], p["actual"]) for p in recent])

    # -------------------------------------------------------------- planning
    async def async_plan(self, reason: str = "manual", for_tomorrow: bool | None = None) -> None:
        """Compute profile and target SOC for the day being planned."""
        now = dt_util.now()
        if for_tomorrow is None:
            evening = parse_hhmm(str(self._opt(CONF_PLAN_EVENING)))
            for_tomorrow = now.time() >= evening
        await self._async_refresh_forecast()
        day = now.date() + timedelta(days=1 if for_tomorrow else 0)
        pv = self.pv_tomorrow if for_tomorrow else self.pv_today
        external = self.cfg.get(CONF_FORECAST_SOURCE) == FORECAST_SENSOR
        if (
            external
            and not for_tomorrow
            and not self.cfg.get(CONF_FORECAST_TODAY_ENTITY)
            and self.plan_result is not None
        ):
            self.plan_note = "no 'today' forecast configured, kept the evening plan"
            self._notify()
            return
        slots = self.slots()
        self.plan_result = plan(
            pv,
            slots,
            self.load_profile.for_date(day),
            float(self.cfg.get(CONF_CAPACITY, 96.5)),
            self.floor,
            float(self._opt(CONF_BUFFER_KWH)),
            float(self._opt(CONF_CLOUDY_THRESHOLD)),
            float(self._opt(CONF_AFTERNOON_SHARE)),
        )
        self.plan_for = day
        self.plan_note = f"{reason}: planned for {day.isoformat()}"
        if pv is not None:
            self.st.forecasts[day.isoformat()] = round(pv, 2)
        self._blocked.clear()
        self.hass.async_create_task(self._async_clear_revert_issues())
        self._save()
        self._notify()

    async def _async_clear_revert_issues(self) -> None:
        for i in range(1, self.slot_count + 1):
            ir.async_delete_issue(
                self.hass, DOMAIN, f"{ISSUE_WRITE_REVERTED}_{self.entry.entry_id}_{i}"
            )

    # ----------------------------------------------------------- desired state
    def compute_desired(self, now: datetime | None = None) -> DesiredCaps | None:
        now = now or dt_util.utcnow()
        if self.plan_result is None:
            return None
        return desired_caps(
            self.slots(),
            dt_util.as_local(now).time(),
            self.soc,
            self.effective_profile,
            self.plan_result.target_soc,
            self.floor,
            self.hold_soc,
            self.spike_active,
            self.gate_closed,
            self.ev_charging,
            self.deadline,
        )

    @property
    def in_grace(self) -> bool:
        return self.started_at is None or dt_util.utcnow() - self.started_at < self.startup_grace

    # ------------------------------------------------------------ reconciling
    async def _async_periodic_reconcile(self, _now: datetime) -> None:
        await self.async_reconcile("timer")

    async def _async_tick(self, _now: datetime) -> None:
        """Advance debouncers and verify pending writes."""
        before = (self.spike_active, self.gate_closed, self.ev_charging)
        self._update_debouncers(dt_util.utcnow())
        await self._async_verify_pending()
        if before != (self.spike_active, self.gate_closed, self.ev_charging):
            await self.async_reconcile("debounce")
        else:
            self._notify()

    def _slot_value(self, slot: int) -> int | None:
        v = self._float(self.soc_entities[slot - 1])
        return None if v is None else int(round(v))

    def _write_budget_used(self, now: datetime) -> int:
        self._write_times = [t for t in self._write_times if now - t < timedelta(hours=1)]
        return len(self._write_times)

    @property
    def writes_last_hour(self) -> int:
        return self._write_budget_used(dt_util.utcnow())

    async def async_reconcile(self, reason: str = "manual") -> None:
        if self.started_at is None:
            return
        async with self._lock:
            await self._reconcile(reason)
        self._notify()

    async def _reconcile(self, reason: str) -> None:
        now = dt_util.utcnow()
        self._update_debouncers(now)
        self.desired = self.compute_desired(now)

        # availability of every slot entity (no partial writes)
        missing = [
            i + 1 for i, e in enumerate(self.soc_entities) if self._slot_value(i + 1) is None
        ]
        issue_id = f"{ISSUE_SLOT_UNAVAILABLE}_{self.entry.entry_id}"
        if missing:
            self.controls_available = False
            self._unavailable_since = self._unavailable_since or now
            if now - self._unavailable_since >= timedelta(minutes=UNAVAILABLE_ISSUE_MINUTES):
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key=ISSUE_SLOT_UNAVAILABLE,
                    translation_placeholders={"slots": ", ".join(map(str, missing))},
                )
            self.pending_actions = []
            self.status = f"inverter slot entities unavailable: {missing}"
            return
        self.controls_available = True
        self._unavailable_since = None
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)

        if self.desired is None:
            self.pending_actions = []
            self.status = "inputs unknown, nothing to write"
            return

        actions: list[_Action] = []
        notes: list[str] = []
        for i, entity_id in enumerate(self.soc_entities, start=1):
            target = self.desired.caps[i - 1]
            current = self._slot_value(i)
            if current == target:
                continue
            if i in self._blocked:
                notes.append(f"slot {i} blocked after repeated revert")
                continue
            until = self._paused_until.get(i)
            if until and now < until:
                notes.append(f"slot {i} paused until {dt_util.as_local(until):%H:%M}")
                continue
            if i in self._pending:
                continue
            actions.append(_Action(i, entity_id, current, target, self.desired.reasons[i - 1]))
        self.pending_actions = [a.as_dict() for a in actions]

        if not actions:
            self.status = "in sync" + (f" ({'; '.join(notes)})" if notes else "")
            return
        if self.in_grace:
            self.status = "startup grace period"
            return
        if not self.st.enabled:
            self.status = "planner disabled, no writes"
            return
        if self.st.dry_run:
            self.status = "dry run"
            _LOGGER.info(
                "Dry run: would write %s",
                ", ".join(f"slot {a.slot} {a.current}->{a.target}" for a in actions),
            )
            return

        used = self._write_budget_used(now)
        budget = int(self._opt(CONF_WRITE_BUDGET))
        if used + len(actions) > budget:
            await self._trip_budget(actions, used, budget)
            return
        await self._write_actions(actions, now)
        self.status = "wrote changes"

    async def _write_actions(self, actions: list[_Action], now: datetime) -> None:
        done: list[_Action] = []
        for n, act in enumerate(actions):
            if not self.st.enabled or self.st.dry_run:
                break
            if n:
                await asyncio.sleep(self.write_spacing)
            ok = await self._call_set_value(act.entity_id, act.target)
            self._write_times.append(dt_util.utcnow())
            if ok:
                self._pending[act.slot] = _Pending(act.target, dt_util.utcnow())
                self._last_written[act.slot] = act.target
                done.append(act)
            self._record(
                act.slot, act.current, act.target, act.reason, "written" if ok else "failed"
            )
        if done:
            summary = "; ".join(
                f"slot {a.slot}: {a.current}->{a.target}% ({a.reason})" for a in done
            )
            self.last_action = (dt_util.utcnow(), summary)
            await self._logbook(summary)

    async def _call_set_value(self, entity_id: str, value: int) -> bool:
        value = int(min(100, max(self.floor, value)))
        try:
            await self.hass.services.async_call(
                "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to write %s", entity_id)
            return False
        return True

    async def _logbook(self, message: str) -> None:
        if not self.hass.services.has_service("logbook", "log"):
            return
        try:
            await self.hass.services.async_call(
                "logbook",
                "log",
                {"name": "TOU Battery Planner", "message": message, "domain": DOMAIN},
                blocking=True,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("logbook write failed", exc_info=True)

    def _record(self, slot: int, old: Any, new: Any, reason: str, result: str) -> None:
        self.write_log.append(
            {
                "time": dt_util.utcnow().isoformat(),
                "slot": slot,
                "from": old,
                "to": new,
                "reason": reason,
                "result": result,
            }
        )

    async def _trip_budget(self, actions: list[_Action], used: int, budget: int) -> None:
        self.st.enabled = False
        self._save()
        desired = self.desired.caps if self.desired else None
        self.status = "write budget exceeded, planner disabled"
        msg = (
            f"Writes in the last hour ({used}) plus {len(actions)} pending would exceed the "
            f"budget of {budget}. The planner was turned off. Desired caps: {desired}"
        )
        _LOGGER.warning(msg)
        self.last_action = (dt_util.utcnow(), "write budget exceeded, planner disabled")
        persistent_notification.async_create(
            self.hass,
            msg,
            title="TOU Battery Planner turned off",
            notification_id=f"{DOMAIN}_{self.entry.entry_id}_budget",
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_BUDGET}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_BUDGET,
            translation_placeholders={"budget": str(budget)},
        )

    async def _async_verify_pending(self) -> None:
        now = dt_util.utcnow()
        for slot, pend in list(self._pending.items()):
            if self._slot_value(slot) == pend.value:
                self._pending.pop(slot)
                self._record(slot, None, pend.value, "confirmed by entity", "confirmed")
                continue
            if now - pend.at < timedelta(minutes=REVERT_CHECK_MINUTES):
                continue
            if pend.retries < 1 and self.st.enabled and not self.st.dry_run:
                entity = self.soc_entities[slot - 1]
                ok = await self._call_set_value(entity, pend.value)
                self._write_times.append(now)
                pend.retries += 1
                pend.at = now
                self._record(
                    slot,
                    self._slot_value(slot),
                    pend.value,
                    "retry after revert",
                    "retried" if ok else "failed",
                )
                continue
            self._pending.pop(slot)
            self._blocked.add(slot)
            self._last_written.pop(slot, None)
            self._record(slot, self._slot_value(slot), pend.value, "reverted twice", "blocked")
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_WRITE_REVERTED}_{self.entry.entry_id}_{slot}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_WRITE_REVERTED,
                translation_placeholders={"slot": str(slot)},
            )

    # ----------------------------------------------------------------- service
    async def async_write_slot_times(self) -> None:
        """Write the configured slot start times once and enable the TOU switch."""
        times = self.time_entities
        for slot in self.configured_slots():
            if slot.index <= len(times) and times[slot.index - 1]:
                await self.hass.services.async_call(
                    "time",
                    "set_value",
                    {"entity_id": times[slot.index - 1], "time": slot.start.strftime("%H:%M:%S")},
                    blocking=True,
                )
        switch = self.cfg.get(CONF_TOU_SWITCH)
        if switch:
            await self.hass.services.async_call(
                "switch", "turn_on", {"entity_id": switch}, blocking=True
            )
        self.last_action = (dt_util.utcnow(), "wrote slot start times and enabled TOU switch")

    # ------------------------------------------------------------ diagnostics
    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "enabled": self.st.enabled,
            "dry_run": self.st.dry_run,
            "profile_mode": self.st.profile_mode,
            "plan": self.plan_result.as_dict() if self.plan_result else None,
            "plan_for": self.plan_for.isoformat() if self.plan_for else None,
            "desired": (
                {"caps": self.desired.caps, "reasons": self.desired.reasons}
                if self.desired
                else None
            ),
            "inputs": {
                "soc": self.soc,
                "price_cents": self.price_cents,
                "spike": self.spike_active,
                "gate_closed": self.gate_closed,
                "ev": self.ev_charging,
                "hold_soc": self.hold_soc,
            },
            "load_profile": {
                "hourly": [round(v, 3) for v in self.load_profile.hourly],
                "provisional": self.load_profile.provisional,
                "days": self.load_profile.days_of_data,
            },
            "k": self.st.k,
            "k_history": self.st.k_history,
            "forecast_pairs": self.st.pairs[-14:],
            "writes": list(self.write_log),
        }
