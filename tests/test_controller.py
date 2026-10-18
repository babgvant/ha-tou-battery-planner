"""Controller tests: reconciler, safety guards, debouncers."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.tou_battery_planner.const import DOMAIN
from custom_components.tou_battery_planner.controller import Debouncer

from .conftest import SLOT_ENTITIES


def _targets(calls):
    return {c.data["entity_id"]: c.data["value"] for c in calls}


async def test_writes_only_diffs(planner, hass):
    c, calls, _ = planner
    await c.async_reconcile()
    target = c.plan_result.target_soc
    assert target > 20
    # slots 3 and 4 already at floor: only 1, 2, 5, 6 written
    assert _targets(calls) == {SLOT_ENTITIES[i]: target for i in (0, 1, 4, 5)}
    assert c.last_action is not None


async def test_no_partial_writes_when_slot_unavailable(planner, hass):
    c, calls, _ = planner
    hass.states.async_set(SLOT_ENTITIES[3], "unavailable")
    await c.async_reconcile()
    assert calls == []
    assert not c.controls_available


async def test_slot_unavailable_raises_issue_after_30_min(planner, hass, freezer):
    c, calls, entry = planner
    hass.states.async_set(SLOT_ENTITIES[3], "unavailable")
    await c.async_reconcile()
    freezer.tick(timedelta(minutes=31))
    await c.async_reconcile()
    reg = ir.async_get(hass)
    assert reg.async_get_issue(DOMAIN, f"slot_unavailable_{entry.entry_id}") is not None
    assert calls == []


async def test_dry_run_calls_no_services(planner, hass):
    c, calls, _ = planner
    c.st.dry_run = True
    await c.async_reconcile()
    assert calls == []
    assert len(c.pending_actions) == 4
    assert c.status == "dry run"


async def test_disabled_calls_no_services(planner, hass):
    c, calls, _ = planner
    c.st.enabled = False
    await c.async_reconcile()
    assert calls == []
    assert len(c.pending_actions) == 4


async def test_inputs_unknown_writes_nothing(planner, hass):
    c, calls, _ = planner
    hass.states.async_set("sensor.battery_soc", "unavailable")
    await c.async_reconcile()
    assert c.desired is None
    assert calls == []


async def test_startup_grace_honored(planner, hass):
    c, calls, _ = planner
    c.started_at = datetime.now().astimezone()  # just started
    from homeassistant.util import dt as dt_util

    c.started_at = dt_util.utcnow()
    await c.async_reconcile()
    assert calls == []
    assert c.status == "startup grace period"


async def test_budget_trip_disables_planner(planner, hass):
    c, calls, entry = planner
    c.set_option("write_budget", 3)
    await hass.async_block_till_done()
    await c.async_reconcile()  # needs 4 writes > budget 3
    assert calls == []
    assert c.st.enabled is False
    reg = ir.async_get(hass)
    assert reg.async_get_issue(DOMAIN, f"write_budget_exceeded_{entry.entry_id}")


async def test_revert_retries_once_then_raises_issue(planner, hass, freezer):
    c, calls, entry = planner
    await c.async_reconcile()
    assert len(calls) == 4
    # entities never show the value (cloud revert): after 3 min retry once
    freezer.tick(timedelta(minutes=3, seconds=1))
    await c._async_verify_pending()
    assert len(calls) == 8
    freezer.tick(timedelta(minutes=3, seconds=1))
    await c._async_verify_pending()
    assert len(calls) == 8  # no third attempt
    reg = ir.async_get(hass)
    assert reg.async_get_issue(DOMAIN, f"write_reverted_{entry.entry_id}_1") is not None
    # blocked until next plan: reconcile writes nothing for those slots
    await c.async_reconcile()
    assert len(calls) == 8


async def test_confirmed_write_clears_pending(planner, hass, freezer):
    c, calls, _ = planner
    await c.async_reconcile()
    for ent, val in _targets(calls).items():
        hass.states.async_set(ent, str(val))
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=4))
    await c._async_verify_pending()
    assert c._pending == {}
    assert len(calls) == 4


async def test_manual_change_pauses_slot(planner, hass):
    c, calls, _ = planner
    await c.async_reconcile()
    for ent, val in _targets(calls).items():
        hass.states.async_set(ent, str(val))
    await hass.async_block_till_done()
    await c._async_verify_pending()
    calls.clear()
    # user sets slot 1 to 80 by hand
    hass.states.async_set(SLOT_ENTITIES[0], "80")
    await hass.async_block_till_done()
    assert 1 in c._paused_until
    await c.async_reconcile()
    assert SLOT_ENTITIES[0] not in _targets(calls)


async def test_spike_debounce_applies_after_five_minutes(planner, hass, freezer):
    c, calls, _ = planner
    await c.async_reconcile()
    hass.states.async_set("sensor.price", "40")
    await c.async_reconcile()
    assert not c.spike_active
    freezer.tick(timedelta(minutes=5, seconds=1))
    await c.async_reconcile()
    assert c.spike_active
    # slot 5 (current, 22:00) released to floor
    assert c.desired.caps[4] == 20


def test_debouncer_delays():
    t0 = datetime(2026, 1, 1)
    d = Debouncer(timedelta(minutes=2), timedelta(minutes=5))
    assert d.update(True, t0) is False
    assert d.update(True, t0 + timedelta(minutes=1)) is False
    assert d.update(True, t0 + timedelta(minutes=2)) is True
    assert d.update(False, t0 + timedelta(minutes=3)) is True
    assert d.update(True, t0 + timedelta(minutes=4)) is True  # flap resets timer
    assert d.update(False, t0 + timedelta(minutes=5)) is True
    assert d.update(False, t0 + timedelta(minutes=10)) is False


async def test_ev_hold_records_soc(planner, hass, freezer):
    c, _, _ = planner
    await c.async_reconcile()
    hass.states.async_set("sensor.ev_power", "7200", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.battery_soc", "41.6")
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=2, seconds=1))
    await c.async_reconcile()
    assert c.ev_charging
    assert c.hold_soc == 41
    assert c.desired.caps[4] >= 41


async def test_services_registered(planner, hass):
    assert hass.services.has_service(DOMAIN, "plan_now")
    assert hass.services.has_service(DOMAIN, "reconcile_now")
    assert hass.services.has_service(DOMAIN, "write_slot_times")
    _ = async_fire_time_changed


async def test_charge_switch_read_live_and_issue(planner, hass):
    c, _, entry = planner
    switches = [f"switch.sa_tou_{i}_grid_charging" for i in range(1, 7)]
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "charge_switches": switches, "charge_flags": [True] * 6}
    )
    c.skip_next_reload = True
    await hass.async_block_till_done()
    for s in switches:
        hass.states.async_set(s, "off")
    assert [c.charge_flag(i) for i in range(1, 7)] == [False] * 6
    await c.async_reconcile()
    reg = ir.async_get(hass)
    assert reg.async_get_issue(DOMAIN, f"no_charge_enabled_{entry.entry_id}")
    hass.states.async_set(switches[0], "on")
    assert c.charge_flag(1) is True
    await c.async_reconcile()
    assert reg.async_get_issue(DOMAIN, f"no_charge_enabled_{entry.entry_id}") is None
    # unavailable switch falls back to the static flag
    hass.states.async_set(switches[1], "unavailable")
    assert c.charge_flag(2) is True
