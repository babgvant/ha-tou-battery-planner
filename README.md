# TOU Battery Planner

A Home Assistant custom integration (HACS) that plans and enforces a home battery's
time-of-use (TOU) schedule from a PV forecast, a learned load profile and a real-time
electricity price. It writes slot SOC targets to your inverter through existing
`number` entities, so it works with any integration that exposes Sol-Ark, Sunsynk or Deye
style TOU slots as generic `number` / `time` / `switch` entities.

> Safety first: after setup the planner is **disabled** and in **dry-run** mode. Nothing
> is written until you turn off dry run and turn on the master switch.

## Why

Morning discharge is nearly worthless and the evening (about 15:00-20:00) is where the
price risk is. A sunny-day profile that runs the battery down overnight expecting PV to
refill it fails on a cloudy day, right before the expensive evening hours. The planner
decides each day whether the battery needs a reserve (cloudy profile) and holds the
caps accordingly.

## Installation

1. HACS, Integrations, three-dot menu, Custom repositories.
2. Add this repository URL, category Integration.
3. Install "TOU Battery Planner", restart Home Assistant.
4. Settings, Devices and services, Add integration, "TOU Battery Planner".

## Configuration walkthrough

1. **Inverter**: number of slots, usable capacity (confirm it, there is no real default),
   floor SOC, optional TOU enable switch. Pick the slot 1 SOC entity
   (`number.<prefix>_tou_1_battery_soc`) to prefill slots 2..6 by pattern.
2. **Slots**: per slot the SOC cap `number`, the optional start `time` entity, and the grid
   Charge flag. Pick the inverter's per-slot grid charging `switch` (for example
   `switch.<prefix>_tou_<n>_grid_charging`, prefilled by pattern) and the planner reads it live;
   it never writes it. Without a switch, the fallback checkbox is used (default off).
3. **Sensors**: battery SOC, PV power, house load power, optional EV power sensors,
   real-time price (cents/kWh or $/kWh, auto-detected from the unit).
4. **Forecast**: built-in Open-Meteo (location from HA, overridable) or an external sensor
   that reports tomorrow's PV kWh (Solcast, Forecast.Solar, ...) with an optional attribute,
   and optionally a "today" entity used by the same-day replan.
5. **Slot roles**: a role says what the slot does to the battery, not when it runs and not
   whether it can grid-charge (that is the inverter's per-slot Charge setting).
   - `charge`: raise to the target SOC. If the slot has Charge on, the grid tops the battery up;
     otherwise the battery holds until PV charges it. The charge price gate and deadline apply.
   - `hold`: keep the battery at the target SOC (idle) on a cloudy day.
   - `spend`: let the battery serve the house down to the floor. The hours these slots cover
     set the "spend need" the plan must reserve energy for.
   On a sunny day every slot uses the floor. Default 6-slot layout: 00:00 charge, 06:00 hold,
   13:00 spend, 19:00 spend, 21:00 charge, 23:00 charge. Start times and suggested roles
   (charge where the slot has Charge on, hold otherwise) are prefilled from your live time
   entities and Charge switches; set your spend slots yourself.

Use the "Write slot start times" button (or the `write_slot_times` service) once to push
the role start times to your time entities and enable the TOU switch.

Tunables (floor, buffer, cloudy threshold, price gate, spike price, deadline, plan times,
PV share during spend slots, learning days, write budget) are in the options flow and also exposed as
number / time entities in the CONFIG category.

## Inverter TOU semantics (assumption)

Per Sol-Ark's TOU Application Guide WI3-019, each slot has a start time, a battery SOC
cap and a Charge flag. In "Limited power to home" mode the battery serves loads down to the
slot cap; below it the grid does. With Charge set, the grid charges the battery up to the
cap and does not charge if SOC is already at or above it. So, with Charge flags fixed per
slot, the cap alone controls behavior:

- cap above SOC in a Charge slot: grid charges the battery
- cap about equal to SOC: hold (house on grid, battery idle)
- cap at floor: battery serves the load

Firmware may differ. **Verify manually before enabling**: in a Charge slot that is
currently active, set the cap above the current SOC and watch for grid charging; then set
it below SOC and watch for battery discharge. Do the same in a non-Charge slot to confirm
that it never grid-charges.

## How it works

Planning (pure logic in `planner.py`):

```
spend_need   = sum(load_profile[h]) over hours covered by spend slots
pv_after     = spend_pv_share * forecast_pv_kwh
reserve_kwh  = max(0, spend_need - pv_after) + buffer
target_soc   = min(100, floor + ceil(reserve_kwh / capacity * 100))
profile      = cloudy if forecast unavailable or forecast < threshold else sunny
```

The next-day plan (default 20:00) plans tomorrow; the same-day plan (default 04:00) plans today.

Caps: sunny sets every slot to the floor. Cloudy sets charge and hold slots to the
target and spend slots to the floor. Overrides apply to the current slot only:

1. Price spike (>= spike price, on after 5 min, off after 15 min): floor.
2. Charge price gate (cloudy, charge slot, cap above SOC, from the start of the run of charge
   slots until the charge deadline, price above gate, 10 min debounce both ways):
   hold at the current SOC instead of charging. After the deadline it charges at any
   price below the spike price.
3. EV charging (> 1000 W, on after 2 min, off after 5 min): never go below the held SOC.

An unknown price counts as "above gate" and "not spike".

## Safety model

- Master switch off and dry run on at first setup.
- No writes unless SOC, every slot entity, the profile and the target are valid. If any
  slot entity is unavailable nothing is written (a repair issue appears after 30 min).
- Only differing slots are written, sequentially, 5 s apart, clamped to [floor, 100].
- After a write the entity must show the value; if it reverts within 3 minutes the write
  is retried once, then a repair issue is raised and that slot is left alone until the next plan.
- Write budget (default 12 per hour): exceeding it turns the planner off, posts a
  persistent notification and raises a repair issue.
- If a cap entity changes to a value the planner did not write, that slot is paused for
  60 minutes (configurable).
- Startup grace period of 2 minutes.

## Reading the reasoning

- `sensor.*_target_soc` attributes: forecast, spend need, PV after, reserve, target, profile.
- `sensor.*_desired_caps`: state is the comma list; attributes give each slot's cap and a
  reason such as `cloudy: hold 45% until 13:00`, `spike release`, `EV hold at 41%`,
  `price gate: defer charge, hold 30% until 04:30`.
- `sensor.*_pending_actions` shows what would be written (also in dry run).
- `sensor.*_load_profile` has the 24 hourly kWh values and a `provisional` flag.
- Diagnostics include the last 50 write attempts.

## Learning

Hourly median house load (EV sensors subtracted) over the learning window from recorder
statistics; falls back to a flat daily kWh on a default shape until 7 days of data exist
(`provisional`). The PV factor k (kWh per MJ/m2 of Open-Meteo shortwave) is the 14-day
median, clamped to [3, 9], default 6 until 5 valid days exist. Forecast vs actual pairs
are stored and the 14-day MAE is exposed. PV, load and EV sensors need a long-term
statistics-capable state class (`measurement`).

## Limitations

- Does not control EV chargers, exporting, multi-day optimization or delivery-rate modeling.
- Grid Charge flags are read (live switch or checkbox), never written. Future: manage the per-slot
  grid charging switches based on price.
- Slot start times are only written on request.
- Behavior depends on the inverter TOU semantics above.
- With an external forecast and no "today" entity, the same-day run keeps the previous plan.

## Development

```
pip install -r requirements_test.txt
pytest
ruff check .
```
