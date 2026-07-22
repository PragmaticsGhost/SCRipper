#!/usr/bin/env python3
"""Fixed-purpose Docker controller for the SCRipper login browser.

This is intentionally a tiny, separate service.  It is the only container
with access to the Docker socket and it exposes only three fixed operations:
start, stop, and status for one specifically configured browser container.
"""

import json
import logging
import os
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = os.environ.get("CONTROLLER_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("CONTROLLER_PORT", "8090"))
BROWSER_IMAGE = os.environ.get(
    "LOGIN_BROWSER_IMAGE",
    "jlesage/firefox@sha256:2bdbe30e355028eabb8a3a6f50a35a5d729e1de2376bc9c3c27c55fd2a7443d9",
)
BROWSER_NAME = "scripper-login-browser"
BROWSER_VOLUME = "scripper-firefox-profile"
BROWSER_PORT = 5800
MANAGED_LABEL_KEY = "com.scripper.login-browser"
MANAGED_LABEL_VALUE = "true"
MANAGED_LABEL = f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("browser-controller")
_docker_lock = threading.Lock()


class ControllerError(RuntimeError):
    pass


def _docker(*args, timeout=120, allow_not_found=False):
    try:
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControllerError(f"Docker command failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if allow_not_found and "No such" in detail:
            return None
        raise ControllerError(detail[-500:] or "Docker command failed")
    return result


def browser_state():
    """Return existence/running state after verifying container ownership."""
    result = _docker(
        "inspect",
        "--format",
        '{{ index .Config.Labels "' + MANAGED_LABEL_KEY + '" }}|{{.State.Running}}',
        BROWSER_NAME,
        allow_not_found=True,
    )
    if not result:
        return {"exists": False, "managed": False, "running": False}
    parts = result.stdout.strip().lower().split("|", 1)
    if len(parts) != 2:
        raise ControllerError("Docker returned invalid browser container state")
    managed = parts[0] == MANAGED_LABEL_VALUE
    if not managed:
        raise ControllerError(
            f"Refusing to manage container {BROWSER_NAME}: ownership label missing"
        )
    return {"exists": True, "managed": True, "running": parts[1] == "true"}


def browser_status():
    return browser_state()["running"]


def start_browser():
    with _docker_lock:
        state = browser_state()
        if state["running"]:
            return True
        # Only remove a stopped container after its ownership label has been
        # verified. A same-named foreign container is never touched.
        if state["exists"]:
            _docker("rm", "-f", BROWSER_NAME)
        args = [
            "run",
            "-d",
            "--rm",
            "--name",
            BROWSER_NAME,
            "--label",
            MANAGED_LABEL,
            "-p",
            f"127.0.0.1:{BROWSER_PORT}:5800",
            "-v",
            f"{BROWSER_VOLUME}:/config",
            "-e",
            "FF_OPEN_URL=https://soundcloud.com",
            "--shm-size",
            "1g",
        ]
        password = os.environ.get("LOGIN_BROWSER_PASSWORD", "")
        if password:
            args += ["-e", f"VNC_PASSWORD={password}"]
        args.append(BROWSER_IMAGE)
        _docker(*args, timeout=300)
        if not browser_status():
            raise ControllerError("Login browser exited before becoming ready")
        logger.info("login browser started")
        return True


def stop_browser():
    with _docker_lock:
        if not browser_state()["running"]:
            return False
        _docker("stop", "--time", "20", BROWSER_NAME, timeout=60)
        if browser_status():
            raise ControllerError("Login browser is still running after stop")
        logger.info("login browser stopped")
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "SCRipperBrowserController/1"

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _run(self, operation):
        try:
            payload = operation()
            self._send(200, payload)
        except ControllerError as exc:
            logger.error("controller operation failed: %s", exc)
            self._send(503, {"error": str(exc)})
        except Exception as exc:
            logger.exception("unexpected controller failure")
            self._send(500, {"error": str(exc)})

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True})
        elif self.path == "/status":
            self._run(lambda: {"running": browser_status()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > 1024:
            self._send(413, {"error": "request body too large"})
            return
        if length:
            self.rfile.read(length)
        if self.path == "/start":
            self._run(lambda: {"running": start_browser()})
        elif self.path == "/stop":
            self._run(lambda: {"running": stop_browser()})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


def main():
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)

    def shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    logger.info("browser controller listening on %s:%s", LISTEN_HOST, LISTEN_PORT)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            stop_browser()
        except ControllerError:
            logger.exception("could not stop login browser during shutdown")


if __name__ == "__main__":
    main()
