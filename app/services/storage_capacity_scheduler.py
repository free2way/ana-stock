from __future__ import annotations

import logging
import threading

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.storage_capacity import capture_storage_capacity


logger = logging.getLogger(__name__)


class StorageCapacitySchedulerService:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not get_settings().storage_capacity_monitor_enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="storage-capacity-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def _capture(self) -> None:
        with SessionLocal() as db:
            capture_storage_capacity(db)

    def _loop(self) -> None:
        try:
            self._capture()
        except Exception:
            logger.exception("Initial PostgreSQL capacity capture failed.")
        interval = max(
            60,
            int(get_settings().storage_capacity_monitor_interval_seconds),
        )
        while not self._stop_event.wait(interval):
            try:
                self._capture()
            except Exception:
                logger.exception("PostgreSQL capacity capture failed; will retry.")


storage_capacity_scheduler_service = StorageCapacitySchedulerService()
