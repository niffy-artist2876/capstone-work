"""Start the LAPEG web app: python -m lapeg.app [--port 8000] [--no-browser]

Serves the API and the built web app (lapeg/web/dist; build it once with `npm install && npm run build` in
lapeg/web). Opens the browser when the server is up.
"""

from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from lapeg.app.api import WEB


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    url = f"http://127.0.0.1:{a.port}/"
    if not WEB.exists():
        print(f"The web app is not built yet ({WEB}). Run `npm install` and `npm run build` in lapeg/web.")
        print(f"The API alone is available at {url}api/overview")
    elif not a.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    print(f"LAPEG: {url}  (Ctrl+C to stop)")
    uvicorn.run("lapeg.app.api:app", host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
