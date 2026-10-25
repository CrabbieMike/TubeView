"""Starts TubeView's server inside the Android app (called from TubeViewApp.kt)."""
import os
import threading
from http.server import ThreadingHTTPServer


def start(conf, web, downloads, name=""):
    import certifi
    # tubeview reads these at import: the app's own folders instead of desktop ones
    os.environ.update(TUBEVIEW_CONF=conf, TUBEVIEW_WEB=web, TUBEVIEW_DOWNLOADS=downloads,
                      TUBEVIEW_PLATFORM="android", SSL_CERT_FILE=certifi.where(), TUBEVIEW_DEVICE_NAME=name)
    import tubeview
    server = ThreadingHTTPServer(("127.0.0.1", tubeview.PORT), tubeview.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # after the home page has loaded: a TV's CPU is busy enough with that
    prefetch = threading.Timer(30, tubeview.prefetch_music)
    prefetch.daemon = True
    prefetch.start()
