# PyInstaller build for the Windows .exe and the Linux AppImage. From the repo root:
#   pyinstaller packaging/tubeview.spec
# Put ffmpeg and deno (yt-dlp's JavaScript runtime) in packaging/bin/ first; the build workflow downloads them.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).parent
windows = sys.platform == "win32"

a = Analysis(
    [str(root / "tubeview.py")],
    pathex=[str(root)],
    binaries=[(str(p), "bin") for p in (root / "packaging" / "bin").glob("*") if p.is_file()],
    datas=[(str(root / f), ".") for f in ("index.html", "remote.html", "sponsorblock.lua")]
          + collect_data_files("yt_dlp_ejs"),  # yt-dlp's YouTube challenge solver scripts
    hiddenimports=["window"],  # imported on demand by tubeview.open_window
)
# PySide6 brings all of Qt; the web view needs only a slice of it. Drop the rest (about 170 MB).
import re
UNUSED = re.compile(r"Qt6(3D|Charts|DataVisualization|Graphs|Labs|Location|Multimedia|Pdf|Quick3D|QuickControls2|QuickDialogs2|"
                    r"QuickEffects|QuickLayouts|QuickParticles|QuickShapes|QuickTest|QuickTimeline|QuickVectorImage|RemoteObjects|"
                    r"Scxml|Sensors|SerialPort|ShaderTools|SpatialAudio|Sql|StateMachine|Test|TextToSpeech|VirtualKeyboard|"
                    r"WebEngineQuick|WebView|WebSockets|WaylandCompositor|Concurrent|QmlDesignSupport|QmlLocalStorage|"
                    r"QmlXmlListModel|QmlNetwork|QmlCore|PositioningQuick|WebChannelQuick)")


def keep(dest):
    d = dest.replace("\\", "/")
    if "/qml/" in d or UNUSED.search(d):
        return False
    # translations: Qt's UI strings aren't used; the web engine needs just its English locale
    return "/translations/" not in d or d.endswith("qtwebengine_locales/en-US.pak")


a.binaries = [b for b in a.binaries if keep(b[0])]
a.datas = [d for d in a.datas if keep(d[0])]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TubeView", console=False,
          icon=str(root / "packaging" / "icons" / ("tubeview.ico" if windows else "tubeview.png")))
coll = COLLECT(exe, a.binaries, a.datas, name="TubeView")
