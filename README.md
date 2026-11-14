# SolarEdge API Connector for Home Assistant

[![Validate](https://github.com/aarondrabeck/ha-solaredge-api-connector/actions/workflows/validate.yml/badge.svg)](https://github.com/aarondrabeck/ha-solaredge-api-connector/actions/workflows/validate.yml)
[![Tests](https://github.com/aarondrabeck/ha-solaredge-api-connector/actions/workflows/tests.yml/badge.svg)](https://github.com/aarondrabeck/ha-solaredge-api-connector/actions/workflows/tests.yml)
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
| Power | `meters/telemetry` + `storage/telemetry` (+ `ev-chargers/telemetry`, `sensors/telemetry` if the site has them) | 2 | about every 40 min in daylight, about every 3 h at night (adaptive) |
| Energy | `meters/lifetime-energy` + `storage/lifetime-energy` (`inverters/lifetime-energy` without a production meter, + an EV charger total) | 2 | every 3 h |
| Status | `inverters/status` + `storage/status` | 2 | every 12 h (set 0 to turn off) |
| Inventory | `sites/{id}` + `devices` | 2 | weekly (cached across restarts) |

Calls for EV chargers, environmental sensors and batteries are only made when the inventory lists them. A call SolarEdge refuses (403, 404, 400) is skipped until the next weekly inventory refresh rather than retried.

- **Adaptive.** Each 5-minute tick recomputes intervals from the remaining credits and the days left in the cycle. If restarts or other usage eat credits, polling slows down instead of running out.
- **Restart-safe.** The last values, credit ledger and inventory are stored on disk, so restarting Home Assistant costs no credits.
- **No data loss from slow polling.** Lifetime counters carry 15-minute timestamps. Each fetch backfills every hour since the previous one into long-term statistics. SolarEdge allows 12 hours per counter request, so after a longer outage the gap is fetched one window per 5-minute tick, up to 7 days back.
- **Hard stop.** On SolarEdge's `429 Monthly credit limit reached` (or `x-ratelimit-remaining-month: 0`), polling pauses and a repair issue appears until the cycle resets.
- **Bulk endpoints.** One telemetry call returns every meter or battery and every metric.

The ledger counts only this integration's calls. If other apps share your developer account, lower the budget in the options.

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/aarondrabeck/ha-solaredge-api-connector`, category **Integration**.
2. Install **SolarEdge API Connector** and restart Home Assistant.

Requires Home Assistant **2026.10** or newer.

## Setup

### 1. Create a SolarEdge developer app

1. Sign in at <https://developer.solaredge.com> with your **mySolarEdge / monitoring credentials**, and complete the developer profile. The Free tier is provisioned automatically.
2. **Create Application** → Service: *Monitoring API* → Access type: **Site Access**.
3. OAuth settings:
   - **Origin URL:** your Home Assistant address, e.g. `https://<your HA address>`.
   - **Redirect URI** and **Returned URL**: both set to `https://<your HA address>/auth/solaredge_v2/callback`. The setup dialog shows the exact address to copy.
   - **Scopes:** `SITE_DATA` and `DEVICE_DATA`
4. Copy the **Client ID** and **Client Secret**. The secret is shown only once.

> Your Home Assistant only has to be reachable from the browser you sign in with, not from the internet. A local address such as `http://homeassistant.local:8123` works if that's how you open HA.

### 2. Add the integration

1. Settings → Devices & services → **Add integration** → *SolarEdge API Connector*.
2. Enter the Client ID and Client Secret when asked. You can also add them first under *Application credentials*.
3. The dialog shows the callback address to register in your SolarEdge app (step 1.3). Choose **Sign in automatically**.
4. A SolarEdge tab opens. Sign in with your **homeowner** account and approve access (the grant lasts 24 months).
5. SolarEdge sends the tab to the callback, which says *connected* and closes. The dialog finishes on its own, and the site ID is picked up automatically.

> **Manual sign-in:** choose **Sign in manually**, open the sign-in link, approve, then paste the full address of the tab SolarEdge sends you back to (it contains `code=` and `site_id=`). If Home Assistant rewrites the address, it is still in your browser history.

> **Why not my.home-assistant.io?** Home Assistant's standard OAuth callback and my.home-assistant.io both require the provider to return a `state` value. SolarEdge doesn't, and it redirects to the app's *Returned URL*. So the integration serves its own callback, like the core Plex integration does, and matches the sign-in using SolarEdge's `external_id`.

### 3. Options (Configure)

| Option | Default | Notes |
|---|---|---|
| Credits per billing cycle | 1800 | Free tier = 2,000. Leave headroom. |
| Billing cycle start day | 1 | The day your credits reset (see *Usage* in the developer console). |
| Share of power polls during daylight | 75 % | Spends more refreshes while the sun is up. |
| Energy counter interval | 3 h | Statistics are backfilled per hour, so a longer interval only delays them. Above 11 h it saves nothing, because each fetch is split into 12-hour windows. |
| Device status interval | 12 h | Inverter mode and temperature, battery temperature. 0 turns these calls off. |

## Entities

Site-wide entities belong to the site device. Meters are not added as devices because they would have no entities; their details are in the diagnostics. New devices get their entities as soon as they first report, without a restart.

**Each battery** gets its own device (model, firmware, serial) with **State of charge**, **Power** (+ discharge), **Remaining energy** and **Usable capacity** from the shared telemetry call, plus **Internal temperature** and a disabled-by-default **State code** from the status call.

**Each inverter** gets a device with **Mode** (`MPPT`, `STARTING`, `FAULT`, `STANDBY`, `LOCKED_*` …), **Temperature**, **Power limit** and a disabled-by-default **Operation mode code**.

**Each EV charger** gets a device with **Charging power** and **Charging energy** (lifetime, `total_increasing`).

**Environmental sensors** on a commercial gateway or Edge Hub (irradiance, module or ambient temperature, wind speed and direction) appear on the site device.

| Entity | Unit | Notes |
|---|---|---|
| Solar power | kW | Production meter, or site power if there's no production meter |
| Home power | kW | Consumption meter, or SolarEdge's calculated consumption if there isn't one |
| Grid power | kW | **+ import / − export** (HA standard) |
| Grid import power / Grid export power | kW | Both positive |
| Battery power | kW | **+ discharge / − charge** (HA standard) |
| Battery charge power / discharge power | kW | Both positive |
| Battery state of charge | % | Capacity-weighted across batteries |
| Battery remaining energy / usable capacity | kWh | Usable capacity = remaining ÷ SoC |
| Grid status / Battery status | enum | importing · exporting · idle / charging · discharging · idle |
| Solar / Home / Grid import / Grid export / Battery charge / Battery discharge energy | kWh | Lifetime onboard counters, `total_increasing`. Solar comes from the inverters when there's no production meter. |
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

- **Diagnostics** (device page → Download diagnostics) show the credit ledger, the current poll plan, capability flags, the latest values and the raw battery telemetry units. Tokens are redacted.
- **Credit counter:** the integration counts its own calls (1 per request) and resets on your billing day. SolarEdge only reports the remaining balance in the developer console, so calls from other apps on the same developer account are not included.
- **"Credits exhausted" repair:** the developer account hit its monthly limit. Lower the budget or check for other apps on the account.
- **Landed on the Home Assistant dashboard after approving?** The *Returned URL* in your SolarEdge app points at your HA root. Set it, and the Redirect URI, to the callback address shown in the setup dialog, or use manual sign-in.
- **Reauthentication requested:** the 24-month grant expired or was revoked. Sign in again from the notification.
- **Two devices for one SolarEdge Energy Bank:** SolarEdge shortened Energy Bank serials (for example `6308B0FB-16` became `6308B0FB`), which created a second device. From 0.2.0 the original device, with its history, area and entity IDs, takes over the short serial and the duplicate is removed on the next restart.
- **Huge one-hour spikes in battery statistics before 0.2.0:** battery counters sometimes step back a few kWh, and earlier versions took that for a meter reset and added the whole counter again. 0.2.0 stops new spikes. Past ones stay in the recorder; fix them under *Tools → Statistics* (adjust sum) if you chart those statistics.

## Development

```bash
python -m venv .venv && . .venv/bin/activate   # Python 3.14
pip install -r requirements_test.txt
ruff check . && ruff format --check . && pytest
```

## License

MIT
