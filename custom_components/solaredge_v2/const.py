"""Constants for the SolarEdge API Connector integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "solaredge_v2"

API_BASE_URL: Final = "https://monitoringapi.solaredge.com/v2"
OAUTH2_AUTHORIZE: Final = "https://connect.solaredge.com/authorize"
OAUTH2_TOKEN: Final = "https://monitoringapi.solaredge.com/v2/oauth2/token"
OAUTH2_SCOPES: Final = "SITE_DATA DEVICE_DATA"
# Months the consent grant stays valid (SolarEdge maximum).
OAUTH2_ACCESS_DURATION: Final = "24"

DEVELOPER_PORTAL_URL: Final = "https://developer.solaredge.com"
MONITORING_URL: Final = "https://monitoring.solaredge.com"

CONF_SITE_ID: Final = "site_id"
CONF_MONTHLY_BUDGET: Final = "monthly_budget"
CONF_BILLING_DAY: Final = "billing_day"
CONF_DAY_SHARE: Final = "day_share"
CONF_ENERGY_INTERVAL: Final = "energy_interval_hours"

DEFAULT_MONTHLY_BUDGET: Final = 1800
DEFAULT_BILLING_DAY: Final = 1
DEFAULT_DAY_SHARE: Final = 75
DEFAULT_ENERGY_INTERVAL: Final = 3

# The coordinator wakes up this often but only calls the API when a group is due.
TICK_INTERVAL: Final = timedelta(minutes=5)
# SolarEdge's finest data resolution; polling faster returns the same bucket.
MIN_POLL_INTERVAL: Final = timedelta(minutes=15)
INVENTORY_REFRESH: Final = timedelta(days=7)
# How far back to look for the latest telemetry bucket (data can lag).
TELEMETRY_LOOKBACK: Final = timedelta(hours=3)
# Counter backfill on first run, and the API's maximum counter window.
COUNTER_BACKFILL: Final = timedelta(hours=25)
COUNTER_MAX_WINDOW: Final = timedelta(days=7) - timedelta(minutes=5)

# Stay below the Free tier's 10 calls/minute.
MAX_CALLS_PER_MINUTE: Final = 8

STORAGE_VERSION: Final = 1

# Below this many watts a flow is reported as idle.
IDLE_THRESHOLD_W: Final = 10.0
