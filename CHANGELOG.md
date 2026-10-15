# Changelog

## 0.1.0

Initial release.

- Daily planner: profile (sunny/cloudy) and target SOC from PV forecast and learned load profile.
- Desired slot caps with live overrides: price spike, overnight price gate, EV hold.
- Reconciler with dry-run, master enable, write budget, revert detection, manual-change pause.
- Learning from recorder statistics: hourly median load profile, PV calibration factor k.
- Forecast sources: built-in Open-Meteo or an external kWh sensor.
- Config flow, options flow, diagnostics, repair issues, services.
