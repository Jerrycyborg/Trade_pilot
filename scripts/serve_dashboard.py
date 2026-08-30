#!/usr/bin/env python3
"""Local dev server for the Trade_pilot dashboard.

The dashboard calls its services through /api/<service>/ paths, which in
production nginx proxies to the individual ports. Opening index.html directly
therefore 404s on every request. This serves the static files and does the same
proxying, so `load the app locally` needs no nginx install.

    uv run python scripts/serve_dashboard.py           # http://localhost:8080
    uv run python scripts/serve_dashboard.py --port 9000

INTERNAL_API_KEY is read from the environment (or .env) and injected into
proxied requests, so it never sits in the page source. ADMIN_API_KEY is never
injected — admin actions such as the kill switch must supply it explicitly,
since this server does not authenticate its clients. Development only: it binds
localhost, is single-threaded, and does no origin checking.
"""

from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "apps" / "dashboard"

# Mirrors the location blocks in scripts/setup-nginx.sh.
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


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        # One concise line per request; the default is noisy.
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.startswith("/api/"):
            self._proxy("GET")
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy("DELETE")

    def _proxy(self, method: str) -> None:
        if not self.path.startswith("/api/"):
            self.send_error(404, "Not an API path")
            return

        remainder = self.path[len("/api/"):]
        service, _, rest = remainder.partition("/")
        port = SERVICE_PORTS.get(service)
        if port is None:
            self.send_error(404, f"Unknown service {service!r}")
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        request = urllib.request.Request(
            f"http://localhost:{port}/{rest}",
            data=body,
            method=method,
        )
        request.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))

        # INTERNAL_API_KEY is injected so it stays out of the page source.
        # ADMIN_API_KEY is only forwarded when the caller supplies it: this
        # server has no client authentication, so injecting it would grant
        # kill-switch and live-mode rights to anyone who can reach the port.
        internal = self.headers.get("X-Internal-Key") or os.environ.get(
            "INTERNAL_API_KEY", ""
        )
        if internal:
            request.add_header("X-Internal-Key", internal)
        if admin := self.headers.get("X-Admin-Key"):
            request.add_header("X-Admin-Key", admin)
        if idempotency := self.headers.get("Idempotency-Key"):
            request.add_header("Idempotency-Key", idempotency)

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                status = response.status
                content_type = response.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
            content_type = exc.headers.get("Content-Type", "application/json")
        except Exception as exc:
            message = f'{{"detail": "{service} unreachable on :{port} — {exc}"}}'
            payload = message.encode()
            status = 502
            content_type = "application/json"

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; leave as localhost unless you understand the exposure",
    )
    args = parser.parse_args()

    _load_dotenv()
    if not DASHBOARD_DIR.exists():
        print(f"Dashboard not found at {DASHBOARD_DIR}", file=sys.stderr)
        return 1

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((args.host, args.port), DashboardHandler) as httpd:
        print(f"Trade_pilot dashboard  ->  http://{args.host}:{args.port}")
        print(f"Proxying /api/<service>/ to {len(SERVICE_PORTS)} local services")
        if not os.environ.get("INTERNAL_API_KEY"):
            print("  warning: INTERNAL_API_KEY not set — authenticated calls will 401")
        print("  admin key is NOT injected; admin actions must send X-Admin-Key")
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
