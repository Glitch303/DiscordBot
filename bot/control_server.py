"""
Headless service control server.

Listens on a Unix socket and handles control commands (status, start/stop,
settings toggles, session management, log streaming) for the CLI menu.
Follows the WhatsBot pattern: local Unix socket, one JSON request per
connection (except log_stream), filesystem ACL as auth boundary.
"""

import json
import logging
import os
import socket
import socketserver
import threading
import time
from queue import Queue

logger = logging.getLogger("bot.control")

# Socket path (in data/ directory)
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SOCKET_PATH = os.path.join(_PROJECT_DIR, "..", "data", "discordbot.sock")


class LogStreamHandler(logging.Handler):
    """Logging handler that broadcasts formatted records to connected log_stream clients."""

    def __init__(self):
        super().__init__()
        self.clients = []
        self.lock = threading.Lock()

    def add_client(self, client):
        with self.lock:
            self.clients.append(client)

    def remove_client(self, client):
        with self.lock:
            try:
                self.clients.remove(client)
            except ValueError:
                pass

    def emit(self, record):
        try:
            msg = self.format(record)
            with self.lock:
                dead = []
                for client in self.clients:
                    try:
                        client.sendall((msg + "\n").encode("utf-8"))
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        dead.append(client)
                for client in dead:
                    self.clients.remove(client)
        except Exception:
            pass  # Never crash the logging thread


class ControlServer(socketserver.ThreadingUnixStreamServer):
    """Threading Unix socket server for headless service control."""

    allow_reuse_address = True

    def __init__(self, socket_path, callbacks, log_queue=None):
        # Remove stale socket
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass

        self.callbacks = callbacks
        self.log_queue = log_queue or Queue()
        self.log_handler = LogStreamHandler()
        self._stop_event = threading.Event()

        super().__init__(socket_path, self.RequestHandler)

    def handle_request(self):
        """Handle one request (for use in tests)."""
        super().handle_request()

    def shutdown(self):
        """Stop the server."""
        self._stop_event.set()
        try:
            os.unlink(self.server_address)
        except FileNotFoundError:
            pass
        super().shutdown()

    class RequestHandler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                # Read request
                data = self.rfile.readline().strip()
                if not data:
                    return
                request = json.loads(data.decode("utf-8"))

                verb = request.get("verb", "")
                cmd = request.get("cmd", "")
                args = request.get("args", {})

                # Handle log streaming separately (long-lived)
                if verb == "log_stream":
                    self._handle_log_stream()
                    return

                # Dispatch to callbacks
                response = {"ok": False, "error": "unknown command"}
                if verb == "command" and cmd in self.server.callbacks:
                    try:
                        result = self.server.callbacks[cmd](args)
                        response = {"ok": True, "data": result}
                    except Exception as e:
                        response = {"ok": False, "error": str(e)}
                elif verb == "status":
                    response = {"ok": True, "data": self.server.callbacks.get("status", lambda a: {})(args)}

                # Send response
                self.wfile.write(json.dumps(response).encode("utf-8"))
                self.wfile.write(b"\n")
                self.wfile.flush()
            except Exception as e:
                logger.error(f"Control server error: {e}")
                try:
                    self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
                    self.wfile.write(b"\n")
                    self.wfile.flush()
                except Exception:
                    pass

        def _handle_log_stream(self):
            """Stream logs to the client until they disconnect."""
            self.server.log_handler.add_client(self.request)
            try:
                # Keep connection open, reading heartbeat or just waiting
                while not self.server._stop_event.is_set():
                    try:
                        data = self.request.recv(1024)
                        if not data:
                            break
                    except (ConnectionResetError, OSError):
                        break
            finally:
                self.server.log_handler.remove_client(self.request)


def start_control_server(socket_path, callbacks, log_queue=None):
    """Start the control server. Returns the server instance."""
    server = ControlServer(socket_path, callbacks, log_queue)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Control server listening on {socket_path}")
    return server


def stop_control_server(server):
    """Stop the control server."""
    if server:
        server.shutdown()


def send_command(socket_path, verb, cmd=None, args=None):
    """Send a one-off command to the control server. Returns response dict."""
    if args is None:
        args = {}
    request = {"verb": verb, "cmd": cmd, "args": args}
    data = json.dumps(request).encode("utf-8") + b"\n"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(socket_path)
        sock.sendall(data)
        response_data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_data += chunk
            if response_data.endswith(b"\n"):
                break
        return json.loads(response_data.decode("utf-8"))
    finally:
        sock.close()


def stream_logs(socket_path):
    """Generator that yields log records from the control server."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(socket_path)
        request = {"verb": "log_stream"}
        sock.sendall(json.dumps(request).encode("utf-8") + b"\n")

        buffer = b""
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line:
                        yield line.decode("utf-8")
            except (ConnectionResetError, OSError):
                break
    finally:
        sock.close()
