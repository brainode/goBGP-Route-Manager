# SPDX-License-Identifier: GPL-2.0-only
import os

DISCOVERY_MODE_KEY = "discovery_mode"
MAINTENANCE_STATUS_KEY = "maintenance_status"
IPV6_ENABLED_KEY = "ipv6_enabled"
AUTO_REDISCOVER_ALL_KEY = "auto_rediscover_all_enabled"
CONFIGURATION_STATUS_KEY = "configuration_status"

STATUS_REFRESH_INTERVAL_SECONDS = max(int(os.getenv("STATUS_REFRESH_INTERVAL_SECONDS", "3600")), 0)
STATUS_STALE_AFTER_SECONDS = max(int(os.getenv("STATUS_STALE_AFTER_SECONDS", "5400")), 60)
REDISCOVER_QUEUE_PARALLELISM = max(int(os.getenv("REDISCOVER_QUEUE_PARALLELISM", "4")), 1)

SITE_STATUS_PAUSED = "paused"
SITE_STATUS_ACTIVE = "active"
SITE_STATUS_PARTIAL = "partial"
SITE_STATUS_MISSING = "missing"
