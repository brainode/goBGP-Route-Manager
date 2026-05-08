# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread
from typing import Optional

from .config import REDISCOVER_QUEUE_PARALLELISM
from .gobgp_client import GoBGPClient

# Serializes apply/rediscover-all/auto-rediscover maintenance operations
maintenance_lock = Lock()

# Prevents concurrent goBGP state refreshes
status_refresh_lock = Lock()

# Signals the periodic status-refresh thread to stop
status_refresh_stop = Event()
status_refresh_thread: Optional[Thread] = None

# Signals the periodic latency-check thread to stop
latency_check_stop = Event()
latency_check_thread: Optional[Thread] = None

# Prevents concurrent auto-rediscover cycles
auto_rediscover_lock = Lock()

# Protects lazy ThreadPoolExecutor creation
rediscover_executor_lock = Lock()
_rediscover_executor: Optional[ThreadPoolExecutor] = None

# Maps job_id → cancellation Event for in-flight rediscover jobs
cancel_flags: dict[int, Event] = {}

# Shared goBGP client singleton
gobgp = GoBGPClient()


def get_rediscover_executor() -> ThreadPoolExecutor:
    global _rediscover_executor
    with rediscover_executor_lock:
        if _rediscover_executor is None:
            _rediscover_executor = ThreadPoolExecutor(
                max_workers=REDISCOVER_QUEUE_PARALLELISM,
                thread_name_prefix="rediscover",
            )
        return _rediscover_executor


def shutdown_rediscover_executor() -> None:
    global _rediscover_executor
    with rediscover_executor_lock:
        if _rediscover_executor is not None:
            _rediscover_executor.shutdown(wait=False, cancel_futures=False)
            _rediscover_executor = None
