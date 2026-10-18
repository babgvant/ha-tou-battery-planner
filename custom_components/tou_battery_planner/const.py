"""Constants for TOU Battery Planner."""

from __future__ import annotations

DOMAIN = "tou_battery_planner"
NAME = "TOU Battery Planner"

PLATFORMS = ["sensor", "binary_sensor", "switch", "select", "number", "time", "button"]

# Inverter step
CONF_SLOT_COUNT = "slot_count"
CONF_SOC_ENTITIES = "soc_entities"  # list[str], one per slot
CONF_TIME_ENTITIES = "time_entities"  # list[str], "" when not configured
CONF_CHARGE_FLAGS = "charge_flags"  # list[bool], static fallback
CONF_CHARGE_SWITCHES = "charge_switches"  # list[str], live grid-charge switch per slot or ""
CONF_TOU_SWITCH = "tou_switch"
CONF_CAPACITY = "capacity_kwh"
CONF_SLOT_ROLES = "slot_roles"  # list[str]
CONF_SLOT_STARTS = "slot_starts"  # list["HH:MM"]

# Sensors step
CONF_SOC_SENSOR = "soc_sensor"
CONF_PV_SENSOR = "pv_sensor"
CONF_LOAD_SENSOR = "load_sensor"
CONF_EV_SENSORS = "ev_sensors"
CONF_PRICE_SENSOR = "price_sensor"
CONF_PRICE_UNIT = "price_unit"  # auto | cents | dollars

# Forecast step
CONF_FORECAST_SOURCE = "forecast_source"  # open_meteo | sensor
CONF_FORECAST_ENTITY = "forecast_entity"
CONF_FORECAST_ATTR = "forecast_attribute"
CONF_FORECAST_TODAY_ENTITY = "forecast_today_entity"
CONF_FORECAST_TODAY_ATTR = "forecast_today_attribute"
CONF_LAT = "latitude"
CONF_LON = "longitude"

# Tunables (options + number/time entities)
CONF_FLOOR_SOC = "floor_soc"
CONF_BUFFER_KWH = "reserve_buffer_kwh"
CONF_CLOUDY_THRESHOLD = "cloudy_threshold_kwh"
CONF_GATE_PRICE = "gate_price"
CONF_SPIKE_PRICE = "spike_price"
CONF_DEADLINE = "charge_deadline"
CONF_PLAN_EVENING = "plan_time_evening"
CONF_PLAN_MORNING = "plan_time_morning"
CONF_AFTERNOON_SHARE = "afternoon_share"
CONF_LEARNING_DAYS = "learning_days"
CONF_WRITE_BUDGET = "write_budget"
CONF_FLAT_DAILY_KWH = "flat_daily_kwh"
CONF_PAUSE_MINUTES = "manual_pause_minutes"

TUNABLE_DEFAULTS: dict[str, float | int | str] = {
    CONF_FLOOR_SOC: 20,
    CONF_BUFFER_KWH: 5.0,
    CONF_CLOUDY_THRESHOLD: 50.0,
    CONF_GATE_PRICE: 4.5,
    CONF_SPIKE_PRICE: 15.0,
    CONF_DEADLINE: "04:30",
    CONF_PLAN_EVENING: "20:00",
    CONF_PLAN_MORNING: "04:00",
    CONF_AFTERNOON_SHARE: 0.5,
    CONF_LEARNING_DAYS: 28,
    CONF_WRITE_BUDGET: 12,
    CONF_FLAT_DAILY_KWH: 55.0,
    CONF_PAUSE_MINUTES: 60,
}

# Debounce delays in minutes: (on, off)
DEBOUNCE_SPIKE = (5, 15)
DEBOUNCE_GATE = (10, 10)
DEBOUNCE_EV = (2, 5)
EV_POWER_THRESHOLD_W = 1000

STARTUP_GRACE_SECONDS = 120
RECONCILE_INTERVAL_MINUTES = 10
TICK_SECONDS = 30
WRITE_SPACING_SECONDS = 5
REVERT_CHECK_MINUTES = 3
UNAVAILABLE_ISSUE_MINUTES = 30

PROFILE_MODES = ["auto", "sunny", "cloudy"]
FORECAST_OPEN_METEO = "open_meteo"
FORECAST_SENSOR = "sensor"

STORAGE_VERSION = 1
ATTR_ENTRY_ID = "entry_id"
SIGNAL_UPDATE = f"{DOMAIN}_update_{{}}"

ISSUE_SLOT_UNAVAILABLE = "slot_unavailable"
ISSUE_WRITE_REVERTED = "write_reverted"
ISSUE_BUDGET = "write_budget_exceeded"
ISSUE_NO_CHARGE = "overnight_charge_disabled"
