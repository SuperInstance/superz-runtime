"""Lightweight HTTP stub that stands in for un-cloned agents."""
import sys
import json
import signal
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler


class StubHandler(BaseHTTPRequestHandler):
    """Minimal health-check handler for placeholder agents."""

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy", "stub": True}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        pass  # Suppress request logs


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent stub server")
    parser.add_argument("--name", default="stub", help="Agent name")
    parser.add_argument("--port", type=int, default=8500, help="Listen port")
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), StubHandler)
    print(f"[{args.name}] Stub agent listening on :{args.port}", flush=True)

    def shutdown(sig, frame) -> None:  # type: ignore[no-untyped-def]
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
