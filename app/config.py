# SPDX-License-Identifier: GPL-2.0-only
import os

DISCOVERY_MODE_KEY = "discovery_mode"
MAINTENANCE_STATUS_KEY = "maintenance_status"
IPV6_ENABLED_KEY = "ipv6_enabled"
AUTO_REDISCOVER_ALL_KEY = "auto_rediscover_all_enabled"
CONFIGURATION_STATUS_KEY = "configuration_status"
THEME_SCHEDULE_ENABLED_KEY = "theme_schedule_enabled"
THEME_DARK_START_KEY = "theme_dark_start"
THEME_DARK_END_KEY = "theme_dark_end"

STATUS_REFRESH_INTERVAL_SECONDS = max(int(os.getenv("STATUS_REFRESH_INTERVAL_SECONDS", "3600")), 0)
STATUS_STALE_AFTER_SECONDS = max(int(os.getenv("STATUS_STALE_AFTER_SECONDS", "5400")), 60)
REDISCOVER_QUEUE_PARALLELISM = max(int(os.getenv("REDISCOVER_QUEUE_PARALLELISM", "4")), 1)
LATENCY_CHECK_INTERVAL_SECONDS = max(int(os.getenv("LATENCY_CHECK_INTERVAL_SECONDS", "60")), 10)
LATENCY_RETENTION_HOURS = max(int(os.getenv("LATENCY_RETENTION_HOURS", "24")), 1)

SITE_STATUS_PAUSED = "paused"
SITE_STATUS_ACTIVE = "active"
SITE_STATUS_PARTIAL = "partial"
SITE_STATUS_MISSING = "missing"
