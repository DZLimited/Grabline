"""
Entry point for Grabline.exe: runs the app on this PC only and opens it in
the user's browser. Closing the console window stops it.
"""
import os
import secrets
import socket
import sys
import threading
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8765


def _secret_key() -> str:
    """Generated on first run and kept per user, so no two installs share a key."""
    folder = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Grabline"
    folder.mkdir(parents=True, exist_ok=True)
    key_file = folder / "secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(50), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()


def _listen() -> socket.socket:
    """Bind to localhost only, falling back to any free port if ours is taken."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", DEFAULT_PORT))
    except OSError:
        sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock


def main() -> None:
    os.environ.update(
        DJANGO_SETTINGS_MODULE="downloader.settings",
        GRABLINE_LOCAL="1",
        DJANGO_SECRET_KEY=_secret_key(),
    )
    # A cookies.txt placed next to the exe is picked up for login-only posts.
    os.environ.setdefault("GRABLINE_COOKIES", str(Path(sys.executable).parent / "cookies.txt"))

    from waitress import serve

    from downloader.wsgi import application

    sock = _listen()
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
    print(f"Grabline is running at {url}", flush=True)
    print("Keep this window open while you download. Close it to stop Grabline.", flush=True)
    if os.environ.get("GRABLINE_NO_BROWSER") != "1":
        threading.Timer(1, webbrowser.open, [url]).start()
    serve(application, sockets=[sock], threads=8)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - the window would close before it could be read
        print(f"Grabline could not start: {e}")
        input("Press Enter to close.")
        raise
