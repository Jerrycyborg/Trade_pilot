#!/usr/bin/env python3
"""Read-only local server for the Trade Pilot dashboard.

The browser can read service state through /api/<service>/ paths. Mutation
requests are refused: this process never reads, stores, forwards, or injects
service credentials.
"""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "apps" / "dashboard"
SERVICE_PORTS = {
    "policy": 8001,
    "execution": 8002,
    "strategy": 8003,
    "portfolio": 8004,
    "research": 8005,
    "audit": 8006,
    "orchestrator": 8007,
    "sentiment": 8008,
    "notification": 8009,
    "approval": 8010,
}


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.startswith("/api/"):
            self._proxy_get()
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        self.send_error(405, "Dashboard proxy is read-only")

    def do_PUT(self) -> None:  # noqa: N802
        self.send_error(405, "Dashboard proxy is read-only")

    def do_PATCH(self) -> None:  # noqa: N802
        self.send_error(405, "Dashboard proxy is read-only")

    def do_DELETE(self) -> None:  # noqa: N802
        self.send_error(405, "Dashboard proxy is read-only")

    def _proxy_get(self) -> None:
        remainder = self.path[len("/api/") :]
        service, _, rest = remainder.partition("/")
        port = SERVICE_PORTS.get(service)
        if port is None:
            self.send_error(404, f"Unknown service {service!r}")
            return

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/{rest}",
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response_body = response.read()
                status = response.status
                content_type = response.headers.get(
                    "Content-Type",
                    "application/json",
                )
        except urllib.error.HTTPError as exc:
            response_body = exc.read()
            status = exc.code
            content_type = exc.headers.get("Content-Type", "application/json")
        except Exception:
            response_body = json.dumps(
                {"detail": f"{service} is unavailable"}
            ).encode()
            status = 502
            content_type = "application/json"

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)


class DashboardServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if not DASHBOARD_DIR.exists():
        print(f"Dashboard not found at {DASHBOARD_DIR}", file=sys.stderr)
        return 1

    with DashboardServer(("127.0.0.1", args.port), DashboardHandler) as httpd:
        print(f"Trade Pilot dashboard: http://127.0.0.1:{args.port}")
        print("The API proxy is read-only and never handles service credentials.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
