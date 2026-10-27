"""Provide a controllable HTTP endpoint for local failure testing."""

from http.server import BaseHTTPRequestHandler, HTTPServer


class HealthHandler(BaseHTTPRequestHandler):
    """Return a successful response for health-monitoring requests."""

    def do_GET(self):
        """Respond with HTTP 200 while the test server is running."""
        body = b"Local test service is healthy."

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Keep the test server output concise."""
        print(f"[Test Endpoint] {args[0]}")


def main():
    """Start the local test endpoint on port 8000."""
    server = HTTPServer(("127.0.0.1", 8000), HealthHandler)

    print("Test endpoint running at http://127.0.0.1:8000/")
    print("Press Ctrl+C to stop the endpoint and simulate a failure.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nTest endpoint stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
