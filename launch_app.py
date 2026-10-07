"""Run the app and open its local page only after this child serves the app shell."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
READY_MARKER = b"<title>Library of Babel"


def local_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}/"


def port_is_in_use(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    family = socket.AF_INET6 if ":" in probe_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((probe_host, port))
        except OSError:
            return True
    return False


def wait_until_ready(process, url: str, timeout: float = 1800, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(request, timeout=1) as response:
                if response.status == 200 and READY_MARKER in response.read(512 * 1024):
                    # Check again after the response to avoid opening an unrelated process that
                    # took over the port while the app child was exiting.
                    return process.poll() is None
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(interval)
    return False


def main() -> int:
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("PORT", "8765"))
    except ValueError:
        print("PORT must be a number.", flush=True)
        return 2
    url = local_url(host, port)
    if port_is_in_use(host, port):
        print(f"Port {port} already has a listener; refusing to open an unrelated server at {url}.", flush=True)
        return 1

    process = subprocess.Popen([sys.executable, "app.py"], cwd=APP_ROOT)
    print(f"Waiting for the local app to finish loading at {url}...", flush=True)
    if not wait_until_ready(process, url):
        if process.poll() is None:
            print("The app did not become ready within 30 minutes; stopping this app process.", flush=True)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return process.wait()

    print(f"Opening {url}", flush=True)
    if not webbrowser.open(url):
        print(f"Open this address in your browser: {url}", flush=True)
    try:
        return process.wait()
    except KeyboardInterrupt:
        print("Stopping the app and finishing pending storage writes...", flush=True)
        try:
            return process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            print("The app did not stop cleanly; terminating it and skipping sync.", flush=True)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
