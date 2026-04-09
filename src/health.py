import threading
import time
from dataclasses import dataclass


@dataclass
class HealthSnapshot:
    now: float
    uptime_sec: float
    telegram_last_ok_ago_sec: float | None
    telegram_last_error_ago_sec: float | None
    max_last_ok_ago_sec: float | None
    max_last_error_ago_sec: float | None
    max_last_event_ago_sec: float | None

    telegram_healthy: bool
    max_healthy: bool
    overall_healthy: bool


class HealthState:
    def __init__(self, *, unhealthy_after_sec: float = 15 * 60) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._unhealthy_after_sec = float(unhealthy_after_sec)

        self._telegram_last_ok: float | None = None
        self._telegram_last_error: float | None = None

        self._max_last_ok: float | None = None
        self._max_last_error: float | None = None
        self._max_last_event: float | None = None

    def mark_telegram_ok(self) -> None:
        with self._lock:
            self._telegram_last_ok = time.time()

    def mark_telegram_error(self) -> None:
        with self._lock:
            self._telegram_last_error = time.time()

    def mark_max_ok(self) -> None:
        with self._lock:
            self._max_last_ok = time.time()

    def mark_max_error(self) -> None:
        with self._lock:
            self._max_last_error = time.time()

    def mark_max_event(self) -> None:
        with self._lock:
            self._max_last_event = time.time()

    def snapshot(self) -> HealthSnapshot:
        now = time.time()
        with self._lock:
            started_at = self._started_at
            unhealthy_after = self._unhealthy_after_sec

            t_ok = self._telegram_last_ok
            t_err = self._telegram_last_error
            m_ok = self._max_last_ok
            m_err = self._max_last_error
            m_evt = self._max_last_event

        def ago(ts: float | None) -> float | None:
            if ts is None:
                return None
            return max(0.0, now - ts)

        telegram_last_ok_ago = ago(t_ok)
        max_last_ok_ago = ago(m_ok)

        telegram_healthy = telegram_last_ok_ago is not None and telegram_last_ok_ago <= unhealthy_after
        max_healthy = max_last_ok_ago is not None and max_last_ok_ago <= unhealthy_after

        overall_healthy = telegram_healthy and max_healthy

        return HealthSnapshot(
            now=now,
            uptime_sec=max(0.0, now - started_at),
            telegram_last_ok_ago_sec=telegram_last_ok_ago,
            telegram_last_error_ago_sec=ago(t_err),
            max_last_ok_ago_sec=max_last_ok_ago,
            max_last_error_ago_sec=ago(m_err),
            max_last_event_ago_sec=ago(m_evt),
            telegram_healthy=telegram_healthy,
            max_healthy=max_healthy,
            overall_healthy=overall_healthy,
        )

