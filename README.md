# SolarEdge V2 for Home Assistant

[![Validate](https://github.com/aarondrabeck/ha-solaredge-v2/actions/workflows/validate.yml/badge.svg)](https://github.com/aarondrabeck/ha-solaredge-v2/actions/workflows/validate.yml)
[![Tests](https://github.com/aarondrabeck/ha-solaredge-v2/actions/workflows/tests.yml/badge.svg)](https://github.com/aarondrabeck/ha-solaredge-v2/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

A custom integration for the **SolarEdge ONE Developer Platform** (Monitoring API v2) that is careful with API credits.
It reads solar, home, grid and battery power, lifetime energy counters, and battery state of charge.
It also backfills **hourly Energy-dashboard statistics**, and plans every API call so the free tier's **2,000 credits/month** last the whole billing cycle.

> Not affiliated with SolarEdge. It replaces the V1 Monitoring API (retired November 2026) and the V2 Beta keys (retired 19 October 2026).

## Why another SolarEdge integration?

| | Core `solaredge` integration | This integration |
|---|---|---|
| API | V1 site API key | V2, OAuth 2.0 (the only option for homeowners) |
| Cost model | n/a | 1 credit per call; Free tier = 2,000/month, 10/min |
| Energy dashboard | sensors only | lifetime-counter sensors **and** hourly external statistics |
| Power for the dashboard | — | grid / solar / battery power with HA's standard polarity |

## How it saves credits

Every call costs one credit, so it polls in groups and spaces them out based on the budget that's left:

| Group | Endpoints | Credits | Default cadence |
|---|---|---|---|
| Power | `meters/telemetry` + `storage/telemetry` | 2 | about every 40 min in daylight, about every 3 h at night (adaptive) |
| Energy | `meters/lifetime-energy` + `storage/lifetime-energy` | 2 | every 3 h |
| Inventory | `sites/{id}` + `devices` | 2 | weekly (cached across restarts) |

- **Adaptive.** Each 5-minute tick recomputes intervals from the remaining credits and the days left in the cycle. If restarts or other usage eat credits, polling slows down instead of running out.
- **Restart-safe.** The last values, credit ledger and inventory are stored on disk, so restarting Home Assistant costs no credits.
- **No data loss from slow polling.** Lifetime counters carry 15-minute timestamps. Each fetch backfills every hour since the previous one into long-term statistics.
- **Hard stop.** On SolarEdge's `429 Monthly credit limit reached`, polling pauses and a repair issue appears until the cycle resets.
- **Bulk endpoints.** One telemetry call returns every meter or battery and every metric.

The ledger counts only this integration's calls. If other apps share your developer account, lower the budget in the options.

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/aarondrabeck/ha-solaredge-v2`, category **Integration**.
2. Install **SolarEdge V2** and restart Home Assistant.

Requires Home Assistant **2026.10** or newer.

## Setup

### 1. Create a SolarEdge developer app

1. Sign in at <https://developer.solaredge.com> with your **mySolarEdge / monitoring credentials**, and complete the developer profile. The Free tier is provisioned automatically.
2. **Create Application** → Service: *Monitoring API* → Access type: **Site Access**.
3. OAuth settings:
   - **Origin URL:** your Home Assistant external URL.
   - **Redirect URI:** `https://my.home-assistant.io/redirect/oauth`
   - **Scopes:** `SITE_DATA` and `DEVICE_DATA`
4. Copy the **Client ID** and **Client Secret**. The secret is shown only once.

### 2. Add the integration

1. Settings → Devices & services → **Add integration** → *SolarEdge V2*.
2. Enter the Client ID and Client Secret when asked. You can also add them first under *Application credentials*.
3. Click **Open SolarEdge sign-in** in the dialog. A new tab opens. Sign in with your **homeowner** account and approve access (the grant lasts 24 months).
4. SolarEdge sends that tab back to Home Assistant, which picks up the authorization automatically. The tab may go blank; just close it.
5. Back in the dialog, select **Submit**, then enter your site ID.

> Fallback: if Submit says no authorization was found, paste the full address of the tab SolarEdge sent you back to (it contains `code=`). If SolarEdge refuses the code, the dialog shows its error message.

### 3. Options (Configure)

| Option | Default | Notes |
|---|---|---|
| Credits per billing cycle | 1800 | Free tier = 2,000. Leave headroom. |
| Billing cycle start day | 1 | The day your credits reset (see *Usage* in the developer console). |
| Share of power polls during daylight | 75 % | Spends more refreshes while the sun is up. |
| Energy counter interval | 3 h | Statistics are backfilled per hour, so a longer interval only delays them. |

## Entities

All entities belong to the site device. Inverter, meters and batteries show up as sub-devices with model, firmware and serial number.

| Entity | Unit | Notes |
|---|---|---|
| Solar power | kW | Production meter, or site power if there's no production meter |
| Home power | kW | Needs a consumption meter |
| Grid power | kW | **+ import / − export** (HA standard) |
| Grid import power / Grid export power | kW | Both positive |
| Battery power | kW | **+ discharge / − charge** (HA standard) |
| Battery charge power / discharge power | kW | Both positive |
| Battery state of charge | % | Capacity-weighted across batteries |
| Battery remaining energy / usable capacity | kWh | Usable capacity = remaining ÷ SoC |
| Grid status / Battery status | enum | importing · exporting · idle / charging · discharging · idle |
| Solar / Home / Grid import / Grid export / Battery charge / Battery discharge energy | kWh | Lifetime onboard counters, `total_increasing` |
| API credits used / remaining / projected | credits | Diagnostic |
| Data timestamp / Next API update | timestamp | Diagnostic. SolarEdge data usually lags 15–30 min. |

### External statistics

These appear under *Settings → Dashboards → Energy* as `solaredge_v2:<site>_*`:

`solar`, `home`, `grid_import`, `grid_export`, `battery_charge`, `battery_discharge` (kWh, hourly).

## Energy dashboard

| Dashboard field | Recommended source |
|---|---|
| Grid consumption | statistic **Grid import** (`solaredge_v2:<site>_grid_import`) |
| Return to grid | statistic **Grid export** |
| Grid power | **Grid power** (standard polarity) |
| Solar production | statistic **Solar production**; power: **Solar power** |
| Battery energy in / out | statistics **Battery charge** / **Battery discharge** |
| Battery power | **Battery power** (standard polarity) |
| Battery state of charge / capacity | **Battery state of charge**; capacity from **Battery usable capacity** |

The external statistics put each kWh in the hour it was measured. The `*_energy` sensors work too, but they credit energy to the hour it was *fetched*, which can be up to 3 hours late with default settings.

## Migrating from `rest:` sensors on the V2 Beta API

| Old sensor | New entity |
|---|---|
| SolarEdge Site API PV Power | Solar power |
| … Home Consumption Power | Home power |
| … Battery Power (sign flipped by template) | Battery power (already + discharge) |
| … Grid Power (sign flipped by template) | Grid power (already + import) |
| … Battery Status / Grid Status | Battery status / Grid status |
| … Battery Charge Level | Battery state of charge |
| Total PV production / from grid / from battery … | external statistics, or the `*_energy` sensors |

`power-flow` (live, roughly 3-second data) is an *Advanced Monitoring* endpoint for Business Pro and Enterprise tiers only, so this integration uses 15-minute telemetry instead.

Remove the old `rest:` block, and revoke the Beta keys if you can.

## Troubleshooting

- **Diagnostics** (device page → Download diagnostics) show the credit ledger, the current poll plan, capability flags and the latest values. Tokens are redacted.
- **"Credits exhausted" repair:** the developer account hit its monthly limit. Lower the budget or check for other apps on the account.
- **Reauthentication requested:** the 24-month grant expired or was revoked. Sign in again from the notification.

## Development

```bash
python -m venv .venv && . .venv/bin/activate   # Python 3.14
pip install -r requirements_test.txt
ruff check . && ruff format --check . && pytest
```

## License

MIT
