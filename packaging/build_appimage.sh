#!/bin/sh
# Wraps the PyInstaller build (dist/TubeView) into TubeView-x86_64.AppImage.
# Needs appimagetool on PATH (or APPIMAGETOOL=/path/to/appimagetool).
set -eu
cd "$(dirname "$0")/.."
APPDIR=build/TubeView.AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib"
cp -a dist/TubeView "$APPDIR/usr/lib/tubeview"
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
