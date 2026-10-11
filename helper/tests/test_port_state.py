"""The helper must tell its own running copy apart from another program on the same port (plan §17.1 __main__)."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from balloonlens.__main__ import port_state


def serve(respond):
    """Starts a throwaway HTTP server on a free port and returns (server, port)."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            status, body = respond(self.path)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode("utf-8"))

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_a_free_port_is_reported_free():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert port_state(port) == "free"


def test_our_helper_answers_the_pairing_call():
    server, port = serve(lambda path: (200, {"token": "t0ken"}) if path == "/v1/pair" else (404, {"error": "not_found"}))
    try:
        assert port_state(port) == "balloonlens"
    finally:
        server.shutdown()
        server.server_close()


def test_another_program_on_the_port_is_reported_other():
    # A different local server (for example a Node dev server) answers the same path with "not found".
    server, port = serve(lambda path: (404, {"error": "not_found"}))
    try:
        assert port_state(port) == "other"
    finally:
        server.shutdown()
        server.server_close()
