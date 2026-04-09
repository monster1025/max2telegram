import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from health import HealthState

logger = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    health: HealthState

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Убираем спам от http.server, оставляем только наши логи.
        logger.debug("health_http " + format, *args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/livez", "/live", "/"}:
            snap = self.health.snapshot()
            self._send_json(
                200,
                {
                    "status": "live",
                    "uptime_sec": snap.uptime_sec,
                },
            )
            return

        if self.path in {"/healthz", "/health"}:
            snap = self.health.snapshot()
            status = 200 if snap.overall_healthy else 503
            self._send_json(
                status,
                {
                    "status": "ok" if snap.overall_healthy else "unhealthy",
                    "telegram": {
                        "healthy": snap.telegram_healthy,
                        "last_ok_ago_sec": snap.telegram_last_ok_ago_sec,
                        "last_error_ago_sec": snap.telegram_last_error_ago_sec,
                    },
                    "max": {
                        "healthy": snap.max_healthy,
                        "last_ok_ago_sec": snap.max_last_ok_ago_sec,
                        "last_error_ago_sec": snap.max_last_error_ago_sec,
                        "last_event_ago_sec": snap.max_last_event_ago_sec,
                    },
                    "uptime_sec": snap.uptime_sec,
                },
            )
            return

        self._send_json(404, {"error": "not_found"})


def start_health_server(*, host: str, port: int, health: HealthState) -> threading.Thread:
    class Handler(_Handler):
        pass

    Handler.health = health
    server = ThreadingHTTPServer((host, port), Handler)

    def _run() -> None:
        logger.info("Health server listening on %s:%s", host, port)
        server.serve_forever(poll_interval=0.5)

    thread = threading.Thread(target=_run, name="health-http", daemon=True)
    thread.start()
    return thread

