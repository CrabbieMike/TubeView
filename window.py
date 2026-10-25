#!/usr/bin/env python3
"""TubeView's own app window: a Qt WebEngine view of the local TubeView server.

Usage: window.py URL   (run with a Python that has PySide6), or window.main(url) from tubeview.py in the packaged app.
"""
import os
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRect, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

HOME = QUrl("http://127.0.0.1:8765")
PIP_TITLE = "TubeView · Picture in picture"  # set by index.html's setAppPip()
PIP_SIZE = (480, 270)


class Page(QWebEnginePage):
    # links to anywhere but TubeView itself (sign-in pages, channel links) open in the normal browser
    def acceptNavigationRequest(self, url, kind, main_frame):
        if main_frame and url.host() != HOME.host():
            QDesktopServices.openUrl(url)
            return False
        return True

    def createWindow(self, kind):  # target=_blank links
        page = Page(self.profile(), self)
        page.urlChanged.connect(lambda url: (QDesktopServices.openUrl(url), page.deleteLater()))
        return page


class PipControls(QObject):
    """The PiP window has no title bar: drag the video to move it, scroll to resize it.
    A click without dragging still reaches the page (play/pause, expand, close)."""

    def __init__(self, view, active):
        super().__init__()
        self.view, self.active, self.press = view, active, None

    def eventFilter(self, obj, ev):
        if not self.active():
            return False
        t = ev.type()
        if t == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            self.press = ev.globalPosition()
        elif t == QEvent.Type.MouseMove and self.press is not None and (ev.globalPosition() - self.press).manhattanLength() > 6:
            self.press = None
            self.view.windowHandle().startSystemMove()
            return True
        elif t == QEvent.Type.MouseButtonRelease:
            self.press = None
        elif t == QEvent.Type.Wheel:
            # grow/shrink in 16:9, keeping the bottom-right corner in place
            g, step = self.view.geometry(), 40 if ev.angleDelta().y() > 0 else -40
            w = max(320, min(1280, g.width() + step))
            h = w * 9 // 16
            self.view.setGeometry(QRect(g.right() - w + 1, g.bottom() - h + 1, w, h))
            return True
        return False


def main(url=None):
    global HOME
    HOME = QUrl(url or (sys.argv[1] if len(sys.argv) > 1 else HOME.toString()))
    # Wayland doesn't let apps keep a window on top or place it in a corner, which picture-in-picture needs;
    # XWayland (xcb) does, under KDE too
    if os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("TubeView")
    app.setDesktopFileName("tubeview")
    app.setWindowIcon(QIcon.fromTheme("multimedia-video-player"))
    # persistent profile: settings, history and resume points survive restarts
    store = (Path(os.environ["LOCALAPPDATA"]) / "TubeView" / "web" if os.name == "nt"
             else Path.home() / ".local/share/tubeview/web")
    profile = QWebEngineProfile("tubeview")
    profile.setPersistentStoragePath(str(store))
    profile.setCachePath(str(store / "cache"))
    view = QWebEngineView()
    page = Page(profile, view)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)  # "Copy link"

    def fullscreen(req):
        req.accept()
        view.showFullScreen() if req.toggleOn() else view.showNormal()
    page.fullScreenRequested.connect(fullscreen)

    saved = {}  # normal-window geometry while in picture-in-picture

    def pip(title):
        on = title == PIP_TITLE
        if on == bool(saved):
            return
        if on:
            saved["geometry"] = view.saveGeometry()
            if view.isFullScreen() or view.isMaximized():
                view.showNormal()
            view.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            view.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            area, (w, h) = view.screen().availableGeometry(), PIP_SIZE
            view.setGeometry(area.right() - w - 24, area.bottom() - h - 24, w, h)
        else:
            view.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
            view.setWindowFlag(Qt.WindowType.FramelessWindowHint, False)
            view.restoreGeometry(saved.pop("geometry"))
        view.show()  # changing window flags hides the window
    page.titleChanged.connect(pip)
    controls = PipControls(view, lambda: bool(saved))
    # mouse events go to the web view's render widget, which exists once a page has loaded
    # (an app-wide event filter would recurse inside PySide6 and crash)
    def hook(_ok):
        if view.focusProxy() and not getattr(view, "_pip_hooked", False):
            view.focusProxy().installEventFilter(controls)
            view._pip_hooked = True
    page.loadFinished.connect(hook)
    view.setPage(page)
    for keys in ("F5", "Ctrl+R"):  # reload, e.g. after TubeView's page is updated
        QShortcut(QKeySequence(keys), view, view.reload)
    view.setWindowTitle("TubeView")
    view.resize(1440, 900)
    view.load(HOME)
    view.show()
    code = app.exec()
    del view, page  # pages must go before their profile
    return code


if __name__ == "__main__":
    sys.exit(main())
