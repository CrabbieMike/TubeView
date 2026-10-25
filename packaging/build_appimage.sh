#!/bin/sh
# Wraps the PyInstaller build (dist/TubeView) into TubeView-x86_64.AppImage.
# Needs appimagetool on PATH (or APPIMAGETOOL=/path/to/appimagetool).
set -eu
cd "$(dirname "$0")/.."
APPDIR=build/TubeView.AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib"
cp -a dist/TubeView "$APPDIR/usr/lib/tubeview"
# Libraries every desktop has and that have to match its graphics drivers: bundled copies from the (older) build
# machine load first and break them, e.g. an old libstdc++ that the PC's Mesa drivers can't use, so Qt finds no
# working GLX setup and aborts. The AppImage project's exclude list says the same. The xcb-util libraries stay
# (libxcb-cursor and friends aren't installed everywhere).
find "$APPDIR/usr/lib/tubeview" -type f \( -name 'libstdc++.so*' -o -name 'libgcc_s.so*' -o -name 'libX11.so*' \
  -o -name 'libX11-xcb.so*' -o -name 'libxcb.so*' -o -name 'libxcb-dri[23].so*' -o -name 'libxcb-glx.so*' \
  -o -name 'libxcb-present.so*' -o -name 'libxcb-randr.so*' -o -name 'libxcb-render.so*' -o -name 'libxcb-shape.so*' \
  -o -name 'libxcb-shm.so*' -o -name 'libxcb-sync.so*' -o -name 'libxcb-xfixes.so*' -o -name 'libxcb-xkb.so*' \
  -o -name 'libxcb-xinerama.so*' -o -name 'libxcb-xinput.so*' -o -name 'libGL.so*' -o -name 'libGLX.so*' \
  -o -name 'libGLdispatch.so*' -o -name 'libOpenGL.so*' -o -name 'libEGL.so*' -o -name 'libGLESv2.so*' \
  -o -name 'libgbm.so*' -o -name 'libdrm.so*' -o -name 'libdrm_*.so*' -o -name 'libfontconfig.so*' \
  -o -name 'libfreetype.so*' -o -name 'libharfbuzz.so*' -o -name 'libexpat.so*' -o -name 'libdbus-1.so*' \
  -o -name 'libasound.so*' -o -name 'libz.so*' \) -print -delete
cp packaging/icons/tubeview.png "$APPDIR/tubeview.png"
cat > "$APPDIR/tubeview.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=TubeView
GenericName=YouTube Client
Comment=Watch YouTube with SponsorBlock, DeArrow and a Nuvio-style interface
Exec=TubeView
Icon=tubeview
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=tubeview
EOF
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
exec "$APPDIR/usr/lib/tubeview/TubeView" "$@"
EOF
chmod +x "$APPDIR/AppRun"
ARCH=x86_64 "${APPIMAGETOOL:-appimagetool}" "$APPDIR" dist/TubeView-x86_64.AppImage
echo "built dist/TubeView-x86_64.AppImage"
